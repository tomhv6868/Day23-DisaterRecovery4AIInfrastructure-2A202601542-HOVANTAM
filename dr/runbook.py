"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import datetime as dt
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
OUTAGE_PROBE_ATTEMPTS = 3
OUTAGE_PROBE_MAX_ATTEMPTS = 5
OUTAGE_PROBE_INTERVAL_S = 5.0
OUTAGE_PROBE_TIMEOUT_S = 2.0
GOLDEN_REQUESTS = 10
GOLDEN_REQUEST_TIMEOUT_S = 2.0


def step(n, name, *, at: float | None = None, **kw):
    """Append one runbook timeline entry and return the exact record written."""
    now = time.time() if at is None else at
    rec = dict(kw)
    rec.update(
        ts=now,
        iso=dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        step=n,
        name=name,
    )
    LOG.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(rec, ensure_ascii=False)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(encoded + "\n")
        log.flush()
    print("RUNBOOK", encoded, flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Require an explicit operator decision unless running the graded CI flow."""
    if auto:
        return True
    try:
        answer = input(f"{msg} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _latest_outage(primary: str) -> dict | None:
    if not CHAOS_LOG.exists():
        return None
    latest = None
    for line in CHAOS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("region") != primary:
            continue
        event_ts = event.get("ts")
        if (event.get("action") == "kill"
                and type(event_ts) in {int, float}):
            latest = event
        elif event.get("action") == "restore" and latest is not None:
            # A restored drill event is not the start of a current incident.
            latest = None
    return latest


def _confirm_outage(primary: str, target: str) -> tuple[bool, list[dict], int]:
    """Require three consecutive primary readiness failures, polling both sides."""
    observations = []
    consecutive_primary_fails = 0
    started = time.monotonic()
    next_probe = started

    for attempt in range(1, OUTAGE_PROBE_MAX_ATTEMPTS + 1):
        primary_ready, primary_reason = hc.probe(primary, OUTAGE_PROBE_TIMEOUT_S)
        target_ready, target_reason = hc.probe(target, OUTAGE_PROBE_TIMEOUT_S)
        if primary_ready:
            consecutive_primary_fails = 0
        else:
            consecutive_primary_fails += 1
        observations.append(
            {
                "attempt": attempt,
                "primary_ready": primary_ready,
                "primary_reason": primary_reason,
                "target_ready": target_ready,
                "target_reason": target_reason,
            }
        )

        if consecutive_primary_fails >= OUTAGE_PROBE_ATTEMPTS:
            break
        if attempt < OUTAGE_PROBE_MAX_ATTEMPTS:
            next_probe += OUTAGE_PROBE_INTERVAL_S
            sleep_for = next_probe - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

    return (
        consecutive_primary_fails >= OUTAGE_PROBE_ATTEMPTS,
        observations,
        consecutive_primary_fails,
    )


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return round(ordered[index], 1)


def _golden_signals(target: str) -> dict:
    samples = []
    latencies = []
    errors = 0
    endpoint = f"{URL[target]}/v1/infer"

    for request_no in range(1, GOLDEN_REQUESTS + 1):
        started = time.perf_counter()
        sample = {"request": request_no}
        try:
            response = httpx.get(
                endpoint,
                params={"q": f"dr-golden-signal-{request_no}"},
                timeout=GOLDEN_REQUEST_TIMEOUT_S,
            )
            sample["status"] = response.status_code
            body = None
            try:
                body = response.json()
                if isinstance(body, dict):
                    sample["served_by"] = body.get("region")
                    sample["error"] = body.get("error")
            except Exception as exc:
                sample["error"] = f"invalid JSON response: {type(exc).__name__}"
            sample["ok"] = (
                response.status_code == 200
                and isinstance(body, dict)
                and body.get("region") == target
                and not body.get("error")
            )
            if response.status_code == 200 and not sample["ok"] and not sample.get("error"):
                sample["error"] = "response was not served by the target region"
        except Exception as exc:
            sample.update(status=None, ok=False, error=f"{type(exc).__name__}: {exc}")

        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        sample["latency_ms"] = latency_ms
        latencies.append(latency_ms)
        if not sample["ok"]:
            errors += 1
        samples.append(sample)

    return {
        "requests": GOLDEN_REQUESTS,
        "successes": GOLDEN_REQUESTS - errors,
        "errors": errors,
        "error_rate": round(errors / GOLDEN_REQUESTS, 4),
        "error_rate_pct": round(errors * 100.0 / GOLDEN_REQUESTS, 1),
        "p95_latency_ms": _p95(latencies),
        "samples": samples,
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven incident-response steps once, in order."""
    if primary not in URL or target not in URL:
        raise ValueError("primary and target must each be 'a' or 'b'")
    if primary == target:
        raise ValueError("primary and target must be different regions")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unsupported snapshot backend: {backend!r}")

    run_started = time.monotonic()

    # 1. One timeout can be a transient.  Three consecutive observations are the
    # operator-facing confirmation, while both regions are sampled every round.
    outage_confirmed, probes, consecutive_fails = _confirm_outage(primary, target)
    step(
        1,
        "xac_nhan_outage",
        ok=outage_confirmed,
        primary=primary,
        target=target,
        consecutive_primary_fails=consecutive_fails,
        required_consecutive_fails=OUTAGE_PROBE_ATTEMPTS,
        probes=probes,
    )
    if not outage_confirmed:
        return {
            "ok": False,
            "primary": primary,
            "target": target,
            "failed_step": "xac_nhan_outage",
            "reason": "primary outage was not confirmed by consecutive probes",
        }

    # 2. The incident clock is an operator timestamp and therefore follows the
    # outage timestamp captured by the chaos injector (when this is a drill).
    outage = _latest_outage(primary)
    outage_ts = outage.get("ts") if outage else None
    operator_ts = time.time()
    if outage_ts is not None and outage_ts > operator_ts:
        outage = None
        outage_ts = None
    notification_delay = (
        None if outage_ts is None else round(max(0.0, operator_ts - outage_ts), 3)
    )
    detection_gate_s = OUTAGE_PROBE_INTERVAL_S * OUTAGE_PROBE_ATTEMPTS
    if outage_ts is not None:
        # Include one network timeout so a fast/warm target cannot cut over just
        # before the independent checker records its third failed readiness probe.
        detection_gate_s += OUTAGE_PROBE_TIMEOUT_S
    approved = confirm(
        auto,
        f"Region {primary} failed {consecutive_fails} consecutive readiness probes. "
        f"Fail over to region {target}?",
    )
    decision_ts = time.time()
    step(
        2,
        "thong_bao_incident",
        ok=True,
        primary=primary,
        target=target,
        outage_ts=outage_ts,
        outage_iso=outage.get("iso") if outage else None,
        operator_ts=operator_ts,
        notification_delay_s=notification_delay,
        operator_confirmed=approved,
        operator_decision_ts=decision_ts,
        confirmation_wait_s=round(max(0.0, decision_ts - operator_ts), 3),
        mode="auto" if auto else "manual",
        detection_gate_s=detection_gate_s if outage_ts is not None else None,
        at=operator_ts,
    )
    if not approved:
        return {
            "ok": False,
            "primary": primary,
            "target": target,
            "failed_step": "operator_confirmation",
            "reason": "operator declined failover",
        }

    detection_gate_waited = 0.0
    if outage_ts is not None:
        gate_remaining = outage_ts + detection_gate_s - time.time()
        if gate_remaining > 0:
            time.sleep(gate_remaining)
            detection_gate_waited = round(gate_remaining, 3)

    # 3. This is the only invocation.  The function owns restore, scale, readiness,
    # and cutover; the following runbook steps merely record its returned evidence.
    try:
        failover_result = fo.failover(target, backend, 60.0)
    except (Exception, SystemExit) as exc:
        failover_result = {
            "ok": False,
            "target": target,
            "failed_step": "failover_exception",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    step(
        3,
        "scale_gpu_pool",
        ok=bool(failover_result.get("ok")),
        target=target,
        failover_failed_step=failover_result.get("failed_step"),
        reason=failover_result.get("reason"),
        waited_s=failover_result.get("waited_s"),
        detection_gate_waited_s=detection_gate_waited,
    )

    # 4. Do not call failover or query the target again here.  Its verified final
    # state is part of the result returned by step 3.
    target_state = failover_result.get("state")
    if not isinstance(target_state, dict):
        target_state = failover_result.get("target_state")
    if not isinstance(target_state, dict):
        target_state = {}
    vector_count = target_state.get("count")
    weights = target_state.get("weights")
    replica_ok = weights is True and type(vector_count) is int and vector_count > 0
    step(
        4,
        "verify_state_replica",
        ok=replica_ok,
        target=target,
        vector_count=vector_count,
        weights=weights,
        embed_model_version=(failover_result.get("restore") or {}).get(
            "embed_model_version"
        ),
    )

    # 5. Likewise, record the already-completed cutover instead of touching DNS a
    # second time from the orchestration layer.
    cutover = failover_result.get("cutover")
    cutover = cutover if isinstance(cutover, dict) else {}
    active_region = cutover.get("active_region", failover_result.get("active_region"))
    cutover_ok = bool(failover_result.get("ok")) and active_region == target
    step(
        5,
        "dns_cutover",
        ok=cutover_ok,
        target=target,
        active_region=active_region,
        cutover_ts=cutover.get("ts"),
    )

    # 6. Ten direct inference requests provide actual golden-signal measurements.
    golden = _golden_signals(target)
    golden_ok = golden["errors"] == 0
    step(6, "verify_golden_signals", ok=golden_ok, target=target, **golden)

    # 7. Preserve the exact command needed to compute user-visible RTO afterward.
    elapsed = round(time.monotonic() - run_started, 3)
    incident_elapsed = (
        None if outage_ts is None else round(max(0.0, time.time() - outage_ts), 3)
    )
    measure_command = (
        "python3 tools/measure_rto.py "
        "--loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    )
    overall_ok = bool(failover_result.get("ok")) and replica_ok and cutover_ok and golden_ok
    step(
        7,
        "post_incident",
        ok=overall_ok,
        elapsed_s=elapsed,
        incident_elapsed_s=incident_elapsed,
        measure_rto_command=measure_command,
    )

    return {
        "ok": overall_ok,
        "primary": primary,
        "target": target,
        "backend": backend,
        "outage_ts": outage_ts,
        "notification_delay_s": notification_delay,
        "failover": failover_result,
        "state_replica_ok": replica_ok,
        "cutover_ok": cutover_ok,
        "golden_signals": golden,
        "elapsed_s": elapsed,
        "incident_elapsed_s": incident_elapsed,
        "measure_rto_command": measure_command,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a", choices=["a", "b"])
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    outcome = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(outcome, indent=2))
    raise SystemExit(0 if outcome.get("ok") else 1)
