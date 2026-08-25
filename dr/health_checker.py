"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import datetime as dt
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Return the readiness of one region without leaking transport errors.

    A failed readiness response is deliberately different from a liveness check:
    a process can answer HTTP while its model, vector data, or compute pool is not
    ready.  Network failures are observations, not errors in the checker itself,
    so callers always receive a ``(ready, reason)`` tuple.
    """
    if region not in URL:
        raise ValueError(f"unknown region: {region!r}")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except Exception as exc:  # timeout/connect errors are failed probes
        detail = str(exc).strip()
        reason = type(exc).__name__
        if detail:
            reason = f"{reason}: {detail}"
        return False, reason

    if response.status_code == 200:
        return True, "ready"

    reason = f"http_status={response.status_code}"
    try:
        body = response.json()
        reasons = body.get("reasons") if isinstance(body, dict) else None
        if isinstance(reasons, list) and reasons:
            reason = ", ".join(str(item) for item in reasons)
        elif reasons:
            reason = str(reasons)
        elif isinstance(body, dict) and body.get("error"):
            reason = str(body["error"])
    except Exception:
        # A malformed error body must not crash the monitoring loop.
        pass
    return False, reason


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll both regions and append only readiness state transitions to JSONL."""
    if interval <= 0:
        raise ValueError("interval must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if threshold <= 0:
        raise ValueError("threshold must be greater than zero")
    if duration < 0:
        raise ValueError("duration cannot be negative")

    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # The stack starts in its declared healthy baseline.  Suppressing an initial
    # HEALTHY observation keeps this file a transition log rather than a poll log.
    states = {region: "HEALTHY" for region in URL}
    consecutive_fails = {region: 0 for region in URL}
    started = time.monotonic()
    deadline = started + duration
    poll_number = 1
    boundary_tolerance = max(1e-9, abs(duration) * 1e-12)

    with out.open("a", encoding="utf-8") as log:
        while True:
            next_poll = started + poll_number * interval
            if next_poll > deadline + boundary_tolerance:
                break
            sleep_for = next_poll - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

            for region in URL:
                ready, reason = probe(region, timeout)
                if ready:
                    consecutive_fails[region] = 0
                    if states[region] == "UNHEALTHY":
                        now = time.time()
                        event = {
                            "event": "state_change",
                            "ts": now,
                            "iso": _iso(now),
                            "region": region,
                            "from": "UNHEALTHY",
                            "to": "HEALTHY",
                            "reason": reason,
                            "consecutive_fails": 0,
                            "interval_s": interval,
                            "threshold": threshold,
                        }
                        log.write(json.dumps(event, ensure_ascii=False) + "\n")
                        log.flush()
                        states[region] = "HEALTHY"
                    continue

                consecutive_fails[region] += 1
                if (states[region] == "HEALTHY"
                        and consecutive_fails[region] >= threshold):
                    now = time.time()
                    event = {
                        "event": "state_change",
                        "ts": now,
                        "iso": _iso(now),
                        "region": region,
                        "from": "HEALTHY",
                        "to": "UNHEALTHY",
                        "reason": reason,
                        "consecutive_fails": consecutive_fails[region],
                        "interval_s": interval,
                        "threshold": threshold,
                    }
                    log.write(json.dumps(event, ensure_ascii=False) + "\n")
                    log.flush()
                    states[region] = "UNHEALTHY"

            # Keep a fixed cadence, but skip slots missed by slow probes.  Never
            # run back-to-back catch-up probes and mistake them for independent
            # failures separated by a real monitoring interval.
            poll_number += 1
            now = time.monotonic()
            next_poll = started + poll_number * interval
            if next_poll <= now:
                first_future_slot = int((now - started) // interval) + 1
                poll_number = max(poll_number, first_future_slot)

    return {
        "states": states,
        "consecutive_fails": consecutive_fails,
        "elapsed_s": round(time.monotonic() - started, 3),
        "out": str(out),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
