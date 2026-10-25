"""Delay original Lab HTTP events until the automatic detector finalizes their window."""

import argparse
import json
import secrets
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from aegisops.application.request_derived_signals import (
    get_request_signal_window,
    list_request_signals,
    materialize_closed_window,
)
from aegisops.application.request_events import verify_request_event
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import RequestFailureEventRecord
from scripts.evaluate_detector_tempo_outage import wait_until
from scripts.evaluate_fault_battery import checkout, set_payment_dns_fault
from scripts.evaluate_signal_collision import failure_events

ROOT = Path(__file__).resolve().parents[1]
PORT = 18768


def window_events(settings: Settings, start: datetime, end: datetime) -> list:
    with session_for(settings.database_url) as session:
        return list(session.scalars(select(RequestFailureEventRecord).where(
            RequestFailureEventRecord.tenant_id == settings.dev_tenant_id,
            RequestFailureEventRecord.occurred_at >= start,
            RequestFailureEventRecord.occurred_at < end,
        )).all())


def await_compensation(settings: Settings, start: datetime, ids: set[UUID]) -> dict:
    """Observe the running detector, never call its materialization path here."""

    from aegisops.application.request_signal_scheduler import list_detector_runs
    from aegisops.infrastructure.database.engine import session_for
    from aegisops.infrastructure.database.models import RequestSignalDetectorRunRecord
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        for run in list_detector_runs(settings, compensation=True):
            if run.window_start != start or run.status != "FINALIZED":
                continue
            with session_for(settings.database_url) as session:
                row = session.get(RequestSignalDetectorRunRecord, run.id)
                if (row is None or row.tenant_id != settings.dev_tenant_id
                        or row.snapshot_event_ids is None):
                    continue
                if {UUID(value) for value in row.snapshot_event_ids} != ids:
                    continue
            window = get_request_signal_window(settings, start, version=run.detector_version)
            assert window is not None and set(window.snapshot_event_ids) == ids
            return {"run": run.model_dump(mode="json"),
                    "window": window.model_dump(mode="json")}
        time.sleep(1)
    raise RuntimeError("automatic compensation did not finalize the expected snapshot")


class HoldingRelay:
    """Disposable test transport; acknowledges capture, never synthesizes source events."""

    def __init__(self, port: int = PORT) -> None:
        self.route = f"/capture/{secrets.token_hex(16)}"
        self.records: list[tuple[dict, str]] = []
        self.lock = threading.Lock()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                pass  # Headers include service credentials; keep them out of logs.

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != relay.route or not 0 < length <= 16384:
                    self.send_error(404)
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                    authorization = self.headers["Authorization"]
                    if payload["service"] not in {"order-service", "lab-gateway"}:
                        raise ValueError("unexpected source")
                    if not authorization or not authorization.startswith("Bearer "):
                        raise ValueError("missing source credential")
                    with relay.lock:
                        if len(relay.records) >= 16:
                            raise ValueError("capture limit")
                        relay.records.append((payload, authorization))
                except (ValueError, KeyError, TypeError):
                    self.send_error(400)
                    return
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def compose_up(override: Path | None = None) -> None:
    command = ["docker", "compose", "-f", str(ROOT / "compose.yaml")]
    if override is not None:
        command += ["-f", str(override)]
    subprocess.run(
        command + ["up", "-d", "--no-build", "--no-deps", "--force-recreate",
                   "order-service", "lab-gateway"],
        cwd=ROOT, check=True, timeout=60, stdout=subprocess.DEVNULL,
    )


def wait_for_business(client: httpx.Client) -> None:
    for _ in range(30):
        try:
            if client.get("http://127.0.0.1:8001/health/live").status_code == 200:
                if checkout(client) == 200:
                    return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("business services did not become ready")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-compensation", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    now = datetime.now(UTC)
    report: dict = {"started_at": now.isoformat()}
    report_path = ROOT / "eval" / f"request-event-delay-{now:%Y%m%dT%H%M%SZ}.json"
    relay = HoldingRelay()
    override = ROOT / ".tmp" / f"event-relay-{uuid4().hex}.compose.json"
    override.parent.mkdir(exist_ok=True)
    override.write_text(json.dumps({"services": {
        service: {"environment": {
            "LAB_REQUEST_EVENT_URL": f"http://host.docker.internal:{PORT}{relay.route}",
        }} for service in ["order-service", "lab-gateway"]
    }}), encoding="utf-8")
    restore_required = False
    relay.start()
    try:
        restore_required = True
        compose_up(override)
        with httpx.Client(timeout=12, trust_env=False) as client:
            wait_for_business(client)
            now = datetime.now(UTC)
            start = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
            end = start + timedelta(minutes=10)
            if (end - now).total_seconds() < 75:
                start, end = end, end + timedelta(minutes=10)
            report["window_start"] = start.isoformat()
            wait_until(end - timedelta(seconds=40))
            traces = []
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
            assert checkout(client) == 200
            for _ in range(15):
                with relay.lock:
                    records = [(p, a) for p, a in relay.records if p["trace_id"] in traces]
                if len(records) == 4:
                    break
                time.sleep(1)
            assert len(records) == 4, "relay did not capture all real source events"
            assert all(start <= datetime.fromisoformat(p["occurred_at"]) < end for p, _ in records)
            assert failure_events(traces) == [], "held events reached the control plane early"
            report["source_payloads"] = [payload for payload, _ in records]
            print(json.dumps({"captured_real_events": 4, "window": start.isoformat()}), flush=True)
            wait_until(end + timedelta(minutes=2))
            for _ in range(30):
                before = get_request_signal_window(settings, start)
                if before is not None:
                    break
                time.sleep(1)
            assert before is not None, "automatic detector did not finalize the held window"
            assert before.late_event_count == 0, "unrelated late events contaminate sample"
            baseline_ids = set(before.snapshot_event_ids)
            baseline_signals = [s for s in list_request_signals(settings)
                                if s.window_start == start]
            report["before_delivery"] = before.model_dump(mode="json")
            delivery_statuses = []
            first_batch = [(p, a) for p, a in records if p["service"] == "lab-gateway"]
            second_batch = [(p, a) for p, a in records if p["service"] == "order-service"]
            batches = [first_batch, second_batch] if args.verify_compensation else [records]
            compensations = []
            for batch in batches:
                delivered_at = datetime.now(UTC)
                for payload, authorization in batch:
                    response = client.post(
                        "http://127.0.0.1:8000/api/v1/request-events", json=payload,
                        headers={"Authorization": authorization},
                    )
                    response.raise_for_status()
                    delivery_statuses.append(response.status_code)
                if args.verify_compensation:
                    ids_so_far = baseline_ids | {
                        UUID(event["id"]) for event in failure_events(traces)}
                    compensation = await_compensation(settings, start, ids_so_far)
                    compensation["delivery_started_at"] = delivered_at.isoformat()
                    compensations.append(compensation)
            if args.verify_compensation:
                from aegisops.application.request_signal_scheduler import list_detector_runs
                before_duplicate = {
                    str(run.id) for run in list_detector_runs(settings, compensation=True)
                    if run.window_start == start
                }
                signals_before = {str(signal.id) for signal in list_request_signals(settings)
                                  if signal.window_start == start}
                for payload, authorization in records:
                    response = client.post(
                        "http://127.0.0.1:8000/api/v1/request-events", json=payload,
                        headers={"Authorization": authorization},
                    )
                    response.raise_for_status()
                time.sleep(12)
                after_duplicate = {str(run.id) for run in list_detector_runs(
                    settings, compensation=True) if run.window_start == start}
                signals = [signal for signal in list_request_signals(settings)
                           if signal.window_start == start]
                assert before_duplicate == after_duplicate
                assert signals_before == {str(signal.id) for signal in signals}
                from collections import defaultdict
                groups = defaultdict(list)
                for event in window_events(settings, start, end):
                    groups[(event.service_name, event.failure_code,
                            event.resource_name)].append(event)
                previous_sets = {frozenset(s.member_event_ids) for s in baseline_signals}
                expected_sets = {frozenset(e.id for e in members)
                    for members in groups.values() if len({e.trace_id for e in members}) >= 2
                    and frozenset(e.id for e in members) not in previous_sets}
                late_signals = [s for s in signals if s.detection_mode == "LATE_COMPENSATION"]
                actual_sets = [frozenset(s.member_event_ids) for s in late_signals]
                assert len(actual_sets) == len(set(actual_sets)), "duplicate symptom members"
                assert set(actual_sets) == expected_sets, "compensated membership mismatch"
                report.update({"compensations": compensations,
                    "compensated_signals": [s.model_dump(mode="json") for s in late_signals],
                    "expected_compensated_member_sets": [sorted(str(i) for i in group)
                                                         for group in expected_sets],
                    "duplicate_delivery_created_runs": 0,
                    "duplicate_delivery_created_signals": 0})
            events = failure_events(traces)
            assert len(events) == 4
            after = get_request_signal_window(settings, start)
            assert after is not None
            ids = {UUID(event["id"]) for event in events}
            assert set(after.late_event_ids) == ids
            assert after.snapshot_digest == before.snapshot_digest
            assert after.event_count == before.event_count
            assert after.signal_count == before.signal_count
            verified = [verify_request_event(settings, event_id) for event_id in sorted(ids)]
            assert all(item and item.provenance_status == "VERIFIED_SPAN_FACT" for item in verified)
            assert {s.id for s in materialize_closed_window(settings, start)} == {
                s.id for s in baseline_signals}
            report.update({
                "events": events, "delivery_statuses": delivery_statuses,
                "after_delivery": after.model_dump(mode="json"),
                "verified_event_count": len(verified),
                "original_source_ids_preserved": {
                    p["source_event_id"] for p, _ in records
                } == {str(item.source_event_id) for item in verified if item},
                "passed": True,
            })
            assert report["original_source_ids_preserved"] is True
    except Exception as error:
        report["passed"] = False
        report["error_code"] = type(error).__name__
        raise
    finally:
        try:
            if restore_required:
                compose_up()
            report["business_delivery_configuration_restored"] = True
        finally:
            relay.close()
            override.unlink(missing_ok=True)
            report["finished_at"] = datetime.now(UTC).isoformat()
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            print(json.dumps({"passed": report.get("passed"), "report": str(report_path)}),
                  flush=True)


if __name__ == "__main__":
    main()
