"""Blind agent evaluation: frozen manifest, hidden truth, post-hoc scoring.

The harness reads the manifest to inject faults and to score finished reports.
The agent only ever sees a constant neutral alert; fault names, case ids and
truth never enter any model input. Methodology follows RCAEval (ground truth
kept out of the method API, post-hoc scoring) and AIOpsLab (correctness plus
efficiency metrics): see docs/agent-blind-eval-2026-10-24.md.
"""

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg

from aegisops.application.investigation.service import latest_report
from aegisops.config import Settings, require_dev_runtime
from lab.scenarios.redis_down import compose
from scripts.agent_blind_scoring import aggregate, load_manifest, score_report
from scripts.evaluate_agent_live import DB_URL
from scripts.evaluate_fault_battery import (
    active_alerts,
    checkout,
    wait_for_recovery,
)

ROOT = Path(__file__).resolve().parents[1]
CONSOLE_URL = os.getenv("AEGISOPS_CONSOLE_URL", "http://127.0.0.1:8010")


def deployed_agent_enabled() -> bool:
    output = subprocess.check_output(
        ["docker", "compose", "exec", "-T", "workflow-worker", "python", "-c",
         "from aegisops.config import Settings; print(Settings().investigation_agent_enabled)"],
        text=True, cwd=ROOT, timeout=60,
    )
    return output.strip().endswith("True")


def poll(fn, timeout: float, interval: float = 2.0, description: str = ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"{description or 'condition'} not reached within {timeout:.0f}s")


def wait_quiet_alerts(client: httpx.Client) -> None:
    deadline = time.monotonic() + 85
    while active_alerts(client) and time.monotonic() < deadline:
        time.sleep(5)
    if active_alerts(client):
        raise RuntimeError("active alerts would contaminate the blind case")


def inject(client: httpx.Client, injection: dict | None):
    """Dispatch one frozen injection; returns the undo state (None for compose)."""
    if not injection:
        return None
    mode = injection["mode"]
    if mode == "provider-delay":
        from scripts.evaluate_fault_battery import set_provider_delay

        set_provider_delay(client, injection.get("seconds", 8))
        return None
    if mode == "cpu-pressure":
        from scripts.evaluate_fault_battery import cpu_pressure_start

        return cpu_pressure_start()
    compose(mode, injection["target"])
    return None


def recover(client: httpx.Client, injection: dict | None, state) -> None:
    if not injection:
        return
    mode = injection["mode"]
    if mode == "provider-delay":
        from scripts.evaluate_fault_battery import set_provider_delay

        set_provider_delay(client, 0)
        return
    if mode == "cpu-pressure":
        from scripts.evaluate_fault_battery import cpu_pressure_stop

        cpu_pressure_stop(state)
        return
    compose("unpause" if mode == "pause" else "start", injection["target"])


def post_neutral_alert(client: httpx.Client, alert_service: str, alert: dict) -> None:
    response = client.post(
        f"{CONSOLE_URL}/api/lab-alerts",
        headers={"X-Aegisops-Console": "lab-console-v1"},
        json={"service": alert_service, "alert_name": alert["alert_name"],
              "summary": alert["summary"]},
    )
    response.raise_for_status()


def find_promoted_report(settings: Settings, alert_id: UUID, timeout: float):
    def incident_row():
        with psycopg.connect(DB_URL, connect_timeout=2) as connection:
            return connection.execute(
                "SELECT ia.incident_id::text FROM alerts a "
                "JOIN incident_alerts ia ON ia.tenant_id=a.tenant_id AND ia.alert_id=a.id "
                "WHERE a.id=%s", (str(alert_id),),
            ).fetchone()
    row = poll(incident_row, timeout, description="incident for promoted alert")
    incident_id = UUID(row[0])

    def report():
        candidate = latest_report(settings, settings.dev_tenant_id, incident_id)
        if (candidate and not candidate.is_stale and candidate.agent_trace
                and candidate.agent_trace.stop_reason):
            return candidate
        return None
    return str(incident_id), poll(report, timeout, description="agent report")


def find_report(settings: Settings, alert_service: str, started: datetime,
                timeout: float):
    def incident_row():
        with psycopg.connect(DB_URL, connect_timeout=2) as connection:
            rows = connection.execute("""
                SELECT DISTINCT ia.incident_id::text FROM alerts a
                JOIN incident_alerts ia ON ia.tenant_id=a.tenant_id AND ia.alert_id=a.id
                WHERE a.tenant_id=%s AND a.received_at >= %s
                  AND a.service_name=%s AND a.payload->>'alertname'='OperationalObservation'
            """, (settings.dev_tenant_id, started, alert_service)).fetchall()
            for row in rows:
                candidate = latest_report(settings, settings.dev_tenant_id, UUID(row[0]))
                if candidate and not candidate.is_stale:
                    return row[0]
            return None
    incident_id = poll(incident_row, timeout, description="neutral-alert incident")

    def report():
        candidate = latest_report(settings, settings.dev_tenant_id, UUID(incident_id))
        if (candidate and not candidate.is_stale and candidate.agent_trace
                and candidate.agent_trace.stop_reason):
            return candidate
        return None
    return incident_id, poll(report, timeout, description="agent report")


def trace_summary(report) -> dict:
    trace = report.agent_trace
    if trace is None:
        return {"agent_mode": False}
    tokens = sum(step.usage.get("total_tokens", 0)
                 for step in trace.steps if step.phase == "MODEL")
    return {
        "agent_mode": True,
        "model_calls": trace.model_calls,
        "tool_calls": trace.tool_calls,
        "stop_reason": trace.stop_reason,
        "total_tokens": tokens,
        "elapsed_seconds": round(
            (report.completed_at - trace.started_at).total_seconds(), 1),
        "tool_sequence": [step.decision.tool.name for step in trace.steps
                          if step.phase == "TOOL" and step.decision],
    }


def run_case(settings: Settings, client: httpx.Client, case: dict, alert: dict,
             timeout: float) -> dict:
    baseline = checkout(client)
    if baseline != 200:
        raise RuntimeError("business baseline not healthy before blind case")
    wait_quiet_alerts(client)
    alert_service = case.get("alert_service") or f"observed-service-{uuid4().hex[:8]}"
    record: dict = {"case_id": case["case_id"], "alert_service": alert_service}
    started = datetime.now(UTC)
    try:
        if case.get("promotion", True):
            # Align injection to the start of a fresh 10-minute bucket so every
            # member event lands inside the promoted alert's +/-30s fact window.
            now = datetime.now(UTC)
            delta = 600 - (now.minute % 10 * 60 + now.second)
            time.sleep(delta + 12)
        undo_state = inject(client, case.get("injection"))
        if case.get("expected_symptom"):
            symptom = checkout(client)
            record["fault_checkout_status"] = symptom
            print(f"[fault_checkout] {json.dumps(
                {'case': case['case_id'][:8], 'status': symptom})}", flush=True)
            # A fault that still lets checkout succeed did not take effect;
            # transport errors (None) count as not-ok for paused gateways.
            if symptom == 200:
                raise RuntimeError("fault did not produce the expected business symptom")
        if case.get("promotion", True) and case.get("expected_symptom"):
            # The detector needs two distinct failing traces; one checkout cannot
            # materialize a signal on its own.
            record["fault_checkout_status_2"] = checkout(client)
            time.sleep(4)
        post_neutral_alert(client, alert_service, alert)
        if case.get("promotion", True):
            # Failure-signal cases: wait for the mature window, then promote the
            # signal so the incident carries direct source membership.
            def find_signal():
                rows = client.get(
                    f"{os.getenv('AEGISOPS_API_URL', 'http://127.0.0.1:8000')}"
                    "/api/v1/request-failure-signals",
                    headers={"Authorization": f"Bearer {settings.alert_webhook_token}"},
                ).json()
                for row in rows:
                    if (row["service"] == alert_service
                            and row.get("window_start")
                            and datetime.fromisoformat(row["window_start"]) >= started
                            - timedelta(seconds=45)):
                        return row
                return None
            bucket_start = (datetime.now(UTC).replace(second=0, microsecond=0)
                            - timedelta(minutes=datetime.now(UTC).minute % 10))
            signal_deadline = (bucket_start + timedelta(minutes=12, seconds=30)
                               - datetime.now(UTC)).total_seconds()
            signal = poll(find_signal, max(timeout, signal_deadline + 30),
                          description="detector signal")
            record["signal"] = {"id": signal["id"], "members": signal["event_count"],
                                "code": signal["failure_code"],
                                "traces": signal["distinct_trace_count"]}
            promoted = client.post(
                f"{os.getenv('AEGISOPS_API_URL', 'http://127.0.0.1:8000')}"
                f"/api/v1/request-failure-signals/{signal['id']}/promote",
                headers={"Authorization": f"Bearer {settings.operator_token}"},
                json={"reason": "封闭窗口内多次已核验请求失败，送入自主调查"},
            )
            promoted.raise_for_status()
            record["promoted_alert_id"] = promoted.json()["alert_id"]
            incident_id, report = find_promoted_report(
                settings, UUID(record["promoted_alert_id"]), timeout)
        else:
            # Latency-style faults never fail requests, so no signal can exist;
            # the neutral alert enters its own incident for the honest unknown.
            incident_id, report = find_report(settings, alert_service, started, timeout)
        record["incident_id"] = incident_id
        if report is None:
            record["report"] = None
        else:
            record["report"] = {
                "id": str(report.id),
                "status": report.status,
                "graph_version": report.graph_version,
                "primary_hypothesis": report.primary_hypothesis,
                "confidence": report.confidence,
                "candidate_verifications": [v.model_dump(mode="json")
                                            for v in report.candidate_verifications],
                "root_cause_gates": [g.model_dump(mode="json")
                                     for g in report.root_cause_gates],
                "hypothesis_labels": [h.label for h in report.hypotheses],
                "summary": report.summary,
            }
            record["trace"] = trace_summary(report)
            record["proposal_status"] = client.post(
                f"{os.getenv('AEGISOPS_API_URL', 'http://127.0.0.1:8000')}"
                f"/api/v1/incidents/{incident_id}/remediation-plan",
                headers={"Authorization": f"Bearer {settings.operator_token}"},
            ).status_code
    finally:
        recover(client, case.get("injection"), undo_state)
        try:
            wait_for_recovery(client)
            record["business_recovered"] = True
        except Exception as exc:  # noqa: BLE001 - recorded, fails the batch below
            record["business_recovered"] = False
            record["recovery_error"] = type(exc).__name__
        time.sleep(10)
    record["score"] = score_report(record["report"], case)
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path,
                        default=Path("eval/agent-blind-manifest-v1.json"))
    parser.add_argument("--case", action="append",
                        help="case_id to run; repeatable, default all")
    parser.add_argument("--timeout", type=float, default=150)
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    manifest = load_manifest(args.manifest)
    cases = manifest["cases"]
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case["case_id"] in wanted]
        if len(cases) != len(wanted):
            raise ValueError("unknown case id requested")
    agent_enabled = deployed_agent_enabled()
    result: dict = {
        "started_at": datetime.now(UTC).isoformat(),
        "manifest": str(args.manifest),
        "deployed_agent_enabled": agent_enabled,
        "real_model": agent_enabled,
        "cases": [],
    }
    ok = True
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            for case in cases:
                record = run_case(settings, client, case,
                                  manifest["protocol"]["alert"], args.timeout)
                result["cases"].append(record)
                if record.get("business_recovered") is False:
                    ok = False
                # An inconclusive incident must never yield an actionable plan.
                if case.get("expected_symptom") and record.get("proposal_status") != 409:
                    ok = False
    finally:
        result["aggregate"] = aggregate(
            [case["score"] for case in result["cases"]], manifest)
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["passed"] = ok
        output = ROOT / "eval" / (
            f"agent-blind-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json")
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(json.dumps({"report": str(output.resolve()),
                          "aggregate": result["aggregate"],
                          "deployed_agent_enabled": agent_enabled,
                          "passed": ok}, ensure_ascii=False), flush=True)
    if not ok:
        raise AssertionError("blind evaluation batch incomplete; evidence retained")


if __name__ == "__main__":
    main()
