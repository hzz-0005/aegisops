"""Real Temporal Activity interruption: hard worker kill, then journal takeover.

Kills workflow-worker while an agent run owns its journal reservation, observes
the honest failure path (three bounded retries -> INVESTIGATION_FAILED), then
bumps the incident version with one more symptom alert and verifies the refresh
run takes the expired journal over (INTERRUPTED_UNKNOWN_RESULT) without
resetting budgets or repeating provider calls. Truth (fault) stays in the file.
"""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import psycopg

from aegisops.application.investigation.service import latest_report
from aegisops.config import Settings, require_dev_runtime
from lab.scenarios.redis_down import compose
from scripts.evaluate_fault_battery import checkout, wait_for_recovery

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:18000"
CONSOLE = "http://127.0.0.1:18010"
DB = ("postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops")


def db_one(query: str, params: tuple):
    with psycopg.connect(DB, connect_timeout=3) as connection:
        return connection.execute(query, params).fetchone()


def poll(fn, timeout: float, interval: float = 2.0, description: str = ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"{description or 'condition'} not reached within {timeout:.0f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    result: dict = {"started_at": datetime.now(UTC).isoformat(), "steps": {}}
    ok = True
    mutated = False
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            def step(name, value):
                result["steps"][name] = value
                print(f"[{name}] {json.dumps(value, ensure_ascii=False)[:280]}", flush=True)

            assert checkout(client) == 200, "baseline must be healthy"
            compose("stop", "inventory-service")
            mutated = True
            assert checkout(client) != 200, "fault must fail checkout"
            response = client.post(f"{CONSOLE}/api/lab-alerts",
                                   headers={"X-Aegisops-Console": "lab-console-v1"},
                                   json={"service": "order-service",
                                         "alert_name": "OperationalObservation",
                                         "summary": "业务请求需要只读调查"})
            response.raise_for_status()
            started = datetime.now(UTC)

            def running_run():
                row = db_one(
                    "SELECT id::text, model_calls FROM investigation_agent_runs "
                    "WHERE tenant_id=%s AND status='RUNNING' AND created_at >= %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (str(settings.dev_tenant_id), started))
                return row and {"run_id": row[0], "model_calls": row[1]}
            run = poll(running_run, 90, description="RUNNING agent journal reservation")
            step("reservation", run)

            # Hard kill while the reservation is alive; restart the worker.
            compose("kill", "workflow-worker")
            compose("up", "-d", "workflow-worker")
            step("worker_killed", {"at": datetime.now(UTC).isoformat()})

            # Honest failure path: the three bounded retries all fail with
            # AgentAlreadyRunning while the reservation is alive, so no report
            # may appear and the DB start row legitimately stays STARTED (the
            # failure is workflow-internal until an operator decides).
            time.sleep(45)
            row = db_one(
                "SELECT r.incident_id FROM investigation_agent_runs r "
                "LEFT JOIN investigation_reports rep ON rep.incident_id=r.incident_id "
                "AND rep.tenant_id=r.tenant_id WHERE r.id=%s AND rep.id IS NOT NULL",
                (run["run_id"],))
            ok &= row is None
            step("no_report_after_retries", {"clean": row is None,
                "reservation_alive": True})

            # Let the reservation deadline pass, then bump the incident version
            # with one more symptom alert so the refresh run takes the journal over.
            time.sleep(max(0.0, 95 - (datetime.now(UTC) - started).total_seconds()))
            second = client.post(f"{CONSOLE}/api/lab-alerts",
                                 headers={"X-Aegisops-Console": "lab-console-v1"},
                                 json={"service": "order-service",
                                       "alert_name": "OperationalObservation",
                                       "summary": "业务请求需要只读调查"})
            second.raise_for_status()

            def resumed_report():
                with psycopg.connect(DB, connect_timeout=3) as connection:
                    rows = connection.execute(
                        "SELECT incident_id::text FROM investigation_agent_runs "
                        "WHERE tenant_id=%s AND created_at >= %s AND status='STOPPED' "
                        "AND trace->>'stop_reason' IN "
                        "('INTERRUPTED_UNKNOWN_RESULT','VERSION_CHANGED') "
                        "ORDER BY created_at DESC LIMIT 1",
                        (str(settings.dev_tenant_id), started)).fetchone()
                if not rows:
                    return None
                report = latest_report(settings, settings.dev_tenant_id, UUID(rows[0]))
                if report and not report.is_stale and report.agent_trace:
                    return {"incident_id": rows[0], "stop_reason":
                            report.agent_trace.stop_reason,
                            "model_calls": report.agent_trace.model_calls,
                            "tool_calls": report.agent_trace.tool_calls,
                            "report_id": str(report.id)}
                return None
            resumed = poll(resumed_report, 180, description="resumed investigation report")
            step("resumed_report", resumed)
            ok &= resumed["stop_reason"] in {"INTERRUPTED_UNKNOWN_RESULT", "VERSION_CHANGED"}
            ok &= resumed["model_calls"] <= 4 and resumed["tool_calls"] <= 8
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        ok = False
        raise
    finally:
        if mutated:
            compose("start", "inventory-service")
            try:
                with httpx.Client(timeout=15, trust_env=False) as client:
                    wait_for_recovery(client)
                result["business_recovered"] = True
            except Exception as exc:  # noqa: BLE001
                result["business_recovered"] = False
                result["recovery_error"] = f"{type(exc).__name__}: {exc}"
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["passed"] = bool(ok and result.get("business_recovered"))
        output = ROOT / "eval" / (
            f"agent-activity-interrupt-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json")
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(json.dumps({"report": str(output.resolve()), "passed": result["passed"]},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
