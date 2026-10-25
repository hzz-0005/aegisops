"""Real DNS requests and a short Tempo outage exercise nonempty window recovery."""

import argparse
import json
import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select

from aegisops.application.request_derived_signals import (
    DETECTOR_VERSION,
    get_request_signal_window,
    list_request_signals,
    materialize_closed_window,
)
from aegisops.application.request_signal_scheduler import run_one_due_window
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import RequestDerivedSignalRecord as Signal
from aegisops.infrastructure.database.models import RequestSignalDetectorRunRecord as Run
from scripts.evaluate_fault_battery import checkout, set_payment_dns_fault
from scripts.evaluate_signal_collision import failure_events

ROOT = Path(__file__).resolve().parents[1]


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, timeout=60,
        stdout=subprocess.DEVNULL,
    )


def wait_until(ready: datetime) -> None:
    while datetime.now(UTC) < ready:
        remaining = (ready - datetime.now(UTC)).total_seconds()
        print(json.dumps({"waiting_for_watermark_seconds": round(remaining)}), flush=True)
        time.sleep(min(20, max(0.1, remaining)))


def target_run(settings: Settings, start: datetime) -> dict:
    with session_for(settings.database_url) as session:
        row = session.scalar(select(Run).where(
            Run.tenant_id == settings.dev_tenant_id, Run.detector_version == DETECTOR_VERSION,
            Run.window_start == start,
        ))
        assert row is not None
        return {"id": str(row.id), "status": row.status, "attempt": row.attempt_count,
                "error": row.last_error_code,
                "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None}


def process_target(settings: Settings, start: datetime) -> None:
    for _ in range(12):
        tick = run_one_due_window(settings)
        if tick is not None and tick.window_start == start:
            return
        if tick is None:
            raise RuntimeError("target window not due or another attempt holds its lease")
    raise RuntimeError("unexpected scheduler backlog")


def eligible_event_ids(events: list[dict]) -> set[UUID]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for event in events:
        groups[(event["service"], event["failure_code"], event["resource"])].append(event)
    return {UUID(event["id"]) for group in groups.values()
            if len({event["trace_id"] for event in group}) >= 2 for event in group}


def audit_recovered_sample(settings: Settings, source: dict) -> dict:
    start = datetime.fromisoformat(source["window_start"])
    completed = target_run(settings, start)
    failed = source["failed_run"]
    assert source["failure_left_no_snapshot_or_signals"] is True
    assert failed["status"] == "RETRYABLE" and failed["attempt"] == 1
    assert completed["id"] == failed["id"]
    assert completed["status"] == "FINALIZED" and completed["attempt"] == 2
    signals = [signal for signal in list_request_signals(settings) if signal.window_start == start]
    expected_ids = eligible_event_ids(source["events"])
    actual_ids = {event_id for signal in signals for event_id in signal.member_event_ids}
    assert expected_ids and expected_ids <= actual_ids
    assert all(member.verification_status == "VERIFIED_SPAN_FACT"
               for signal in signals for member in signal.member_verifications)
    window = get_request_signal_window(settings, start)
    assert window is not None
    all_ids = {UUID(event["id"]) for event in source["events"]}
    assert all_ids <= set(window.snapshot_event_ids)
    replayed = materialize_closed_window(settings, start)
    replay_window = get_request_signal_window(settings, start)
    assert replay_window is not None
    assert {item.id for item in replayed} == {item.id for item in signals}
    assert replay_window.snapshot_digest == window.snapshot_digest
    return {
        "completed_run": completed, "signals": [s.model_dump(mode="json") for s in signals],
        "window": window.model_dump(mode="json"),
        "below_threshold_event_ids": sorted(str(item) for item in all_ids - expected_ids),
        "replay_preserved_signal_ids_and_snapshot": True,
        "passed": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--audit-report", type=Path, help="Reaudit saved facts without injecting faults",
    )
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    if args.audit_report:
        source = json.loads(args.audit_report.read_text(encoding="utf-8"))
        audit = {
            "source_report": str(args.audit_report), **audit_recovered_sample(settings, source),
        }
        path = ROOT / "eval" / f"detector-tempo-audit-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": audit["passed"], "report": str(path)}), flush=True)
        return
    now = datetime.now(UTC)
    start = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
    end = start + timedelta(minutes=10)
    if (end - now).total_seconds() < 45:
        raise RuntimeError("too close to window end; run again in next window")
    report: dict = {"started_at": now.isoformat(), "window_start": start.isoformat()}
    path = ROOT / "eval" / f"detector-tempo-outage-{now:%Y%m%dT%H%M%SZ}.json"
    detector_stopped = False
    tempo_stopped = False
    try:
        detector_stopped = True
        compose("stop", "request-signal-detector")
        traces = []
        with httpx.Client(timeout=12, trust_env=False) as client:
            assert checkout(client) == 200, "baseline checkout failed"
            try:
                set_payment_dns_fault(client, True)
                for _ in range(2):
                    trace = uuid4().hex
                    response = client.post(
                        "http://127.0.0.1:8001/checkout",
                        headers={"traceparent": f"00-{trace}-{uuid4().hex[:16]}-01"},
                        json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
                    )
                    assert response.status_code == 502
                    traces.append(trace)
            finally:
                set_payment_dns_fault(client, False)
            report["recovered_checkout_status"] = checkout(client)
            assert report["recovered_checkout_status"] == 200
        for _ in range(15):
            events = failure_events(traces)
            if len(events) == 4:
                break
            time.sleep(1)
        assert len(events) == 4, "expected two source events per real request"
        assert all(start <= datetime.fromisoformat(e["occurred_at"]) < end for e in events)
        report["events"] = events
        print(json.dumps({"real_request_event_count": len(events), "window": start.isoformat()}),
              flush=True)
        wait_until(end + timedelta(minutes=2, seconds=1))
        tempo_stopped = True
        compose("stop", "tempo")
        process_target(settings, start)
        failed = target_run(settings, start)
        report["failed_run"] = failed
        assert failed["status"] == "RETRYABLE" and failed["attempt"] == 1
        assert get_request_signal_window(settings, start) is None
        with session_for(settings.database_url) as session:
            count = session.scalar(select(func.count()).select_from(Signal).where(
                Signal.tenant_id == settings.dev_tenant_id, Signal.window_start == start,
                Signal.detector_version == DETECTOR_VERSION,
            ))
        assert count == 0, "failed verification left partial signals"
        report["failure_left_no_snapshot_or_signals"] = True
        print(json.dumps({"outage_run": failed, "partial_signals": count}), flush=True)
        compose("start", "tempo")
        tempo_stopped = False
        with httpx.Client(timeout=3, trust_env=False) as client:
            for _ in range(30):
                try:
                    if client.get(f"{settings.tempo_url}/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Tempo did not recover")
        wait_until(datetime.fromisoformat(failed["next_retry_at"]) + timedelta(seconds=1))
        process_target(settings, start)
        completed = target_run(settings, start)
        report["completed_run"] = completed
        report.update(audit_recovered_sample(settings, report))
    except Exception as error:
        report["passed"] = False
        report["error_code"] = type(error).__name__
        raise
    finally:
        try:
            if tempo_stopped:
                compose("start", "tempo")
        finally:
            try:
                if detector_stopped:
                    compose("start", "request-signal-detector")
            finally:
                report["finished_at"] = datetime.now(UTC).isoformat()
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"passed": report.get("passed"), "report": str(path)}), flush=True)


if __name__ == "__main__":
    main()
