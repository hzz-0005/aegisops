"""Live model/telemetry acceptance; fault names and truth remain in the evaluator."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg

from aegisops.application.investigation.service import latest_report
from aegisops.config import Settings, require_dev_runtime
from lab.scenarios.redis_down import compose
from scripts.evaluate_fault_battery import DB_URL, checkout, wait_for_recovery


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["inventory-unavailable", "missing-telemetry"],
                        required=True)
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    result: dict = {"started_at": datetime.now(UTC).isoformat(), "case": args.case,
                    "real_model": True, "passed": False, "reports": []}
    mutated = False
    report = None
    path = Path("eval") / f"agent-live-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            result["baseline_checkout_status"] = checkout(client)
            if result["baseline_checkout_status"] != 200:
                raise RuntimeError("business baseline not healthy")
            if args.case == "inventory-unavailable":
                # The Agent sees actual business facts, never this injection operation.
                mutated = True
                compose("stop", "inventory-service")
                result["fault_checkout_status"] = checkout(client)
                if result["fault_checkout_status"] != 502:
                    raise RuntimeError("fault did not produce expected business failure")
                service = "order-service"
            else:
                # Neutral identity: the control's name must not disclose expected diagnosis.
                service = f"observed-service-{uuid4().hex[:8]}"
            result["observed_service"] = service
            started = datetime.now(UTC)
            response = client.post(os.getenv(
                "AEGISOPS_CONSOLE_URL", "http://127.0.0.1:8010") + "/api/lab-alerts",
                                   headers={"X-Aegisops-Console": "lab-console-v1"},
                                   json={"service": service, "alert_name": "OperationalObservation",
                                         "summary": "业务请求需要只读调查"})
            response.raise_for_status()
            result["ingestion_response"] = response.json()
            deadline = time.monotonic() + 115
            while time.monotonic() < deadline:
                with psycopg.connect(DB_URL, connect_timeout=2) as connection:
                    rows = connection.execute("""
                        SELECT DISTINCT ia.incident_id::text FROM alerts a
                        JOIN incident_alerts ia ON ia.tenant_id=a.tenant_id AND ia.alert_id=a.id
                        WHERE a.tenant_id=%s AND a.received_at >= %s
                          AND a.service_name=%s AND a.payload->>'alertname'='OperationalObservation'
                    """, (settings.dev_tenant_id, started, service)).fetchall()
                for row in rows:
                    candidate = latest_report(settings, settings.dev_tenant_id, UUID(row[0]))
                    if candidate and candidate.agent_trace and not candidate.is_stale:
                        report = candidate
                        break
                if report is not None:
                    break
                time.sleep(1)
            if report is None:
                raise RuntimeError("agent report not ready within bounded acceptance wait")
            result["reports"] = [report.model_dump(mode="json")]
            trace = report.agent_trace
            assert trace is not None
            completed_tools = [s for s in trace.steps
                               if s.phase == "TOOL" and s.status == "COMPLETED" and s.evidence_ids]
            model_decisions = [s for s in trace.steps if s.phase == "MODEL" and s.decision]
            result["checks"] = {
                "bounded_calls": trace.model_calls <= 4 and trace.tool_calls <= 8,
                "persisted_no_authority": report.status == "INCONCLUSIVE"
                                           and report.primary_hypothesis is None,
                "not_stale": not report.is_stale,
                "controlled_stop": trace.stop_reason in {"MODEL_FINISHED", "MODEL_BUDGET",
                                                         "TOOL_BUDGET", "REPEATED_QUERY"},
                "last_observation_summarized": bool(model_decisions)
                    and model_decisions[-1].decision is not None
                    and model_decisions[-1].decision.decision == "FINISH"
                    and not any(s.phase == "TOOL" and s.number > model_decisions[-1].number
                                for s in trace.steps),
                "candidate_verifications_persisted": len(report.candidate_verifications)
                    == len(trace.hypotheses),
                "supplied_evidence_visible_in_later_round": all(
                    {str(e) for e in s.evidence_ids} <= {
                        e["id"] for m in model_decisions if m.number > s.number
                        for e in m.input_summary.get("evidence", [])
                    } for s in completed_tools),
            }
            if args.case == "inventory-unavailable":
                result["checks"]["two_actual_observations"] = len(completed_tools) >= 2
                result["checks"]["model_updated_after_observation"] = len(model_decisions) >= 3
            else:
                result["checks"]["no_root_or_health_claim"] = report.confidence == 0
            result["passed"] = all(result["checks"].values())
    except Exception as exc:
        result["error_code"] = type(exc).__name__
        raise
    finally:
        if mutated:
            compose("start", "inventory-service")
        try:
            with httpx.Client(timeout=15, trust_env=False) as client:
                wait_for_recovery(client)
            result["business_recovered"] = True
        except Exception as exc:
            result["business_recovered"] = False
            result["recovery_error_code"] = type(exc).__name__
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["passed"] = result["passed"] and result.get("business_recovered", False)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(path.resolve()), "passed": result["passed"],
                          "checks": result.get("checks"),
                          "business_recovered": result.get("business_recovered")},
                         ensure_ascii=False), flush=True)
    if not result["passed"]:
        raise AssertionError("live Agent acceptance did not pass; failure evidence retained")


if __name__ == "__main__":
    main()
