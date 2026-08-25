"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import datetime as dt
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped failover event and mirror it to stdout."""
    now = time.time()
    rec = {
        "ts": now,
        "iso": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(rec, ensure_ascii=False)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(encoded + "\n")
        log.flush()
    print("FAILOVER", encoded, flush=True)
    return rec


def state_of(region: str) -> dict:
    """Read the target's current serving state before changing anything."""
    if region not in URL:
        raise ValueError(f"unknown region: {region!r}")
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    if response.status_code != 200:
        raise RuntimeError(f"region-{region} state returned HTTP {response.status_code}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"region-{region} returned a non-object state payload")
    return body


def _error(exc: BaseException) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _response_body(response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, warm, verify, and only then cut traffic over to ``target``."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target!r}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unsupported snapshot backend: {backend!r}")
    if wait < 0:
        raise ValueError("wait cannot be negative")

    primary = "b" if target == "a" else "a"
    result = {
        "ok": False,
        "target": target,
        "primary": primary,
        "backend": backend,
    }

    # 1. Querying /v1/state also establishes the serving process's current pool
    # state, which makes a subsequent warm -> full transition observable.
    try:
        initial_state = state_of(target)
    except Exception as exc:
        reason = _error(exc)
        emit(step="1_verify_target", ok=False, target=target, reason=reason)
        result.update(failed_step="1_verify_target", reason=reason)
        return result

    result["initial_state"] = initial_state
    emit(
        step="1_verify_target",
        ok=True,
        target=target,
        pool_state=initial_state.get("pool_state"),
        weights=initial_state.get("weights"),
        vector_count=initial_state.get("count"),
        latest_doc_ts=initial_state.get("latest_doc_ts"),
    )

    # 2. Restore both state layers, then measure loss against the primary DB.
    restore_started = time.monotonic()
    try:
        restore_meta = snapshot.get(target, backend)
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
    except (Exception, SystemExit) as exc:
        reason = _error(exc)
        emit(
            step="2_restore_snapshot",
            ok=False,
            target=target,
            backend=backend,
            elapsed_s=round(time.monotonic() - restore_started, 3),
            reason=reason,
        )
        result.update(failed_step="2_restore_snapshot", reason=reason)
        return result

    if not isinstance(restore_meta, dict):
        restore_meta = {}
    if not isinstance(rpo, dict):
        rpo = {}
    version = restore_meta.get("embed_model_version")
    version_file = pathlib.Path(f"state/region-{target}/weights/VERSION")
    try:
        if version is None and version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
    except Exception as exc:
        reason = _error(exc)
        emit(
            step="2_restore_snapshot",
            ok=False,
            target=target,
            backend=backend,
            elapsed_s=round(time.monotonic() - restore_started, 3),
            reason=reason,
        )
        result.update(failed_step="2_restore_snapshot", reason=reason)
        return result
    source_region = restore_meta.get("source_region")
    invalid_restore = []
    if source_region is not None and source_region != primary:
        invalid_restore.append(
            f"snapshot source is region-{source_region}, expected region-{primary}"
        )
    if not version:
        invalid_restore.append("embed_model_version is unavailable")
    if invalid_restore:
        reason = "; ".join(invalid_restore)
        emit(
            step="2_restore_snapshot",
            ok=False,
            target=target,
            backend=backend,
            elapsed_s=round(time.monotonic() - restore_started, 3),
            rpo_seconds=rpo.get("rpo_seconds"),
            docs_lost=rpo.get("docs_lost"),
            embed_model_version=version,
            reason=reason,
        )
        result.update(failed_step="2_restore_snapshot", reason=reason)
        return result
    restore_event = emit(
        step="2_restore_snapshot",
        ok=True,
        target=target,
        backend=backend,
        elapsed_s=round(time.monotonic() - restore_started, 3),
        snapshot_at=restore_meta.get("snapshot_at"),
        restored_at=restore_meta.get("restored_at"),
        rpo_seconds=rpo.get("rpo_seconds"),
        docs_lost=rpo.get("docs_lost"),
        embed_model_version=version,
    )
    result["restore"] = {
        **restore_meta,
        "rpo_seconds": rpo.get("rpo_seconds"),
        "docs_lost": rpo.get("docs_lost"),
        "embed_model_version": version,
        "event_ts": restore_event["ts"],
    }

    # 3. Scale compute only after state is present on disk.
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    try:
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        previous_pool_state = (
            pool_file.read_text(encoding="utf-8").strip() if pool_file.exists() else "cold"
        )
        pool_file.write_text("full\n", encoding="utf-8")
    except Exception as exc:
        reason = _error(exc)
        emit(step="3_scale_pool", ok=False, target=target, reason=reason)
        result.update(failed_step="3_scale_pool", reason=reason)
        return result

    emit(
        step="3_scale_pool",
        ok=True,
        target=target,
        from_pool_state=previous_pool_state,
        to_pool_state="full",
    )

    # 4. Readiness, rather than liveness or elapsed warm-up time, is the gate.
    ready_started = time.monotonic()
    ready_deadline = ready_started + wait
    attempts = 0
    ready_body = {}
    last_reason = "readiness timeout"
    while True:
        remaining = max(0.0, ready_deadline - time.monotonic())
        request_timeout = min(2.0, max(0.05, remaining))
        attempts += 1
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=request_timeout)
            ready_body = _response_body(response)
            if response.status_code == 200:
                vectors = ready_body.get("vectors")
                vectors = vectors if isinstance(vectors, dict) else {}
                vector_count = vectors.get("count")
                within_deadline = time.monotonic() <= ready_deadline
                if wait == 0 and attempts == 1:
                    # A zero wait budget means "one immediate check", useful for
                    # callers that know the pool is already hot.
                    within_deadline = True
                if not within_deadline:
                    last_reason = "ready response arrived after wait deadline"
                elif ready_body.get("region") not in {None, target}:
                    last_reason = "readiness response came from the wrong region"
                elif ready_body.get("ready") is False:
                    last_reason = "HTTP 200 readiness payload declared ready=false"
                elif ready_body.get("pool_state") not in {None, "full"}:
                    last_reason = "HTTP 200 readiness payload reported a non-full pool"
                elif vector_count is not None and (
                    not isinstance(vector_count, int) or vector_count <= 0
                ):
                    last_reason = "HTTP 200 readiness payload reported no restored vectors"
                else:
                    break
            else:
                reasons = ready_body.get("reasons")
                if isinstance(reasons, list) and reasons:
                    last_reason = ", ".join(str(item) for item in reasons)
                else:
                    last_reason = f"http_status={response.status_code}"
        except Exception as exc:
            last_reason = _error(exc)

        remaining = ready_deadline - time.monotonic()
        if remaining <= 0:
            waited = round(time.monotonic() - ready_started, 3)
            emit(
                step="4_wait_ready",
                ok=False,
                target=target,
                attempts=attempts,
                waited_s=waited,
                reason=last_reason,
            )
            result.update(
                failed_step="4_wait_ready",
                reason=last_reason,
                waited_s=waited,
            )
            return result
        time.sleep(min(0.5, remaining))

    waited = round(time.monotonic() - ready_started, 3)
    vectors = ready_body.get("vectors")
    vectors = vectors if isinstance(vectors, dict) else {}
    final_state = {
        "region": ready_body.get("region", target),
        "ready": True,
        "pool_state": ready_body.get("pool_state", "full"),
        "weights": pathlib.Path(
            f"state/region-{target}/weights/model.bin"
        ).exists(),
        "count": vectors.get("count"),
        "latest_doc_ts": vectors.get("latest_doc_ts"),
    }
    emit(
        step="4_wait_ready",
        ok=True,
        target=target,
        attempts=attempts,
        waited_s=waited,
        vector_count=final_state["count"],
    )
    result["state"] = final_state
    result["waited_s"] = waited

    # 5. This is the sole traffic-changing operation and is unreachable until
    # /readyz has returned 200.
    active_file = pathlib.Path("edge/active_region")
    cutover_tmp = active_file.with_name(
        f".{active_file.name}.{time.time_ns()}.tmp"
    )
    previous_contents = None
    previous_region = None
    cutover_applied = False
    try:
        active_file.parent.mkdir(parents=True, exist_ok=True)
        if active_file.exists():
            previous_contents = active_file.read_text(encoding="utf-8")
            previous_region = previous_contents.strip()
        # Same-directory replace is atomic: the edge can observe the old pointer
        # or the new pointer, never a transient empty/truncated file.
        cutover_tmp.write_text(target, encoding="utf-8")
        cutover_tmp.replace(active_file)
        cutover_applied = True
        active_region = active_file.read_text(encoding="utf-8").strip()
        if active_region != target:
            raise RuntimeError(
                f"cutover verification returned {active_region!r}, expected {target!r}"
            )
    except Exception as exc:
        reason = _error(exc)
        rollback_ok = True
        rollback_error = None
        rollback_tmp = None
        try:
            cutover_tmp.unlink(missing_ok=True)
            if cutover_applied:
                if previous_contents is None:
                    active_file.unlink(missing_ok=True)
                else:
                    rollback_tmp = active_file.with_name(
                        f".{active_file.name}.{time.time_ns()}.rollback"
                    )
                    rollback_tmp.write_text(previous_contents, encoding="utf-8")
                    rollback_tmp.replace(active_file)
        except Exception as rollback_exc:
            rollback_ok = False
            rollback_error = _error(rollback_exc)
            reason = f"{reason}; rollback failed: {rollback_error}"
            if rollback_tmp is not None:
                try:
                    rollback_tmp.unlink(missing_ok=True)
                except Exception:
                    pass
        emit(
            step="5_dns_cutover",
            ok=False,
            target=target,
            reason=reason,
            rollback_ok=rollback_ok,
            rollback_error=rollback_error,
        )
        result.update(failed_step="5_dns_cutover", reason=reason)
        return result

    cutover_event = emit(
        step="5_dns_cutover",
        ok=True,
        target=target,
        from_region=previous_region,
        active_region=active_region,
    )
    result.update(
        ok=True,
        cutover={
            "ok": True,
            "active_region": active_region,
            "previous_region": previous_region,
            "ts": cutover_event["ts"],
        },
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    outcome = failover(a.target, a.backend, a.wait)
    print(json.dumps(outcome, indent=2))
    raise SystemExit(0 if outcome.get("ok") else 1)
