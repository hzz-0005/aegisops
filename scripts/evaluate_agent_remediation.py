"""End-to-end governed remediation over an autonomous investigation.

Real fault, real signal, real agent, real approval, real executor. The truth
(red redis path) lives here in the harness; the agent only ever sees the
promoted symptom alert and business telemetry. Every step is recorded; any
failure keeps the evidence file and restores the Lab in `finally`.
"""

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import psycopg

from aegisops.application.investigation.service import latest_report
from aegisops.config import Settings, require_dev_runtime
from lab.scenarios.redis_down import compose
from scripts.evaluate_fault_battery import DB_URL, checkout, wait_for_recovery

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:18000"


def wait_until_next_boundary_plus(seconds: float) -> None:
    now = datetime.now(UTC)
    delta = 600 - (now.minute % 10 * 60 + now.second)
    target = now + timedelta(seconds=delta + seconds)
    while datetime.now(UTC) < target:
        time.sleep(1)


def poll(fn, timeout: float, interval: float = 2.0, description: str = ""):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"{description or 'condition'} not reached within {timeout:.0f}s")


def db_one(query: str, params: tuple):
    with psycopg.connect(DB_URL, connect_timeout=3) as connection:
        return connection.execute(query, params).fetchone()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reject-check-incident", type=UUID, default=None,
                        help="an existing ungated agent incident; its proposal must 409")
    parser.add_argument("--skip-reject-check", action="store_true",
                        help="demo mode: the ungated-probe step is omitted")
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    result: dict = {"started_at": datetime.now(UTC).isoformat(), "steps": {}}
    ok = True
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            operator = {"Authorization": f"Bearer {settings.operator_token}"}
            approver = {"Authorization": f"Bearer {settings.approver_token}"}
            dev = {"Authorization": f"Bearer {settings.alert_webhook_token}"}

            def step(name, value):
                result["steps"][name] = value
                print(f"[{name}] {json.dumps(value, ensure_ascii=False)[:300]}", flush=True)

            # Reject path first (acceptance mode): an ungated agent report must
            # stay unproposable; the demo run skips this probe.
            if args.reject_check_incident is not None and not args.skip_reject_check:
                response = client.post(
                    f"{API}/api/v1/incidents/{args.reject_check_incident}/remediation-plan",
                    headers=operator)
                step("ungated_proposal_rejected", {
                    "incident_id": str(args.reject_check_incident),
                    "status": response.status_code,
                    "detail": response.json().get("detail") if response.status_code == 409
                              else response.text[:200]})
                ok &= response.status_code == 409

            assert checkout(client) == 200, "baseline business must be healthy"

            # Land the two failing traces at the very start of a fresh bucket so
            # both member events fall inside the agent's +/-30s fact window.
            wait_until_next_boundary_plus(12)
            window_start = (datetime.now(UTC).replace(second=0, microsecond=0)
                            - timedelta(minutes=datetime.now(UTC).minute % 10))
            compose("stop", "redis")
            for index in range(2):
                status = checkout(client)
                step(f"fault_checkout_{index}", {"status": status})
                assert status != 200, "redis outage must fail checkout"
            time.sleep(4)  # let the event delivery middleware flush both facts

            # The window matures 2 minutes after its end; the detector drains it.
            deadline = window_start + timedelta(minutes=13, seconds=30)
            wait_seconds = max(30.0, (deadline - datetime.now(UTC)).total_seconds())
            def find_signal():
                rows = client.get(f"{API}/api/v1/request-failure-signals",
                                  headers=dev).json()
                for row in rows:
                    if (row["service"] == "payment-service"
                            and row.get("resource") == "redis-main"
                            and datetime.fromisoformat(row["window_start"]) == window_start):
                        return row
                return None
            signal = poll(find_signal, wait_seconds, description="detector signal")
            step("signal", {"id": signal["id"], "members": signal["event_count"],
                            "code": signal["failure_code"],
                            "traces": signal["distinct_trace_count"]})
            ok &= signal["event_count"] == 2 and signal["distinct_trace_count"] == 2

            response = client.post(
                f"{API}/api/v1/request-failure-signals/{signal['id']}/promote",
                headers=operator,
                json={"reason": "封闭窗口内两次已核验支付缓存请求失败，送入自主调查"})
            response.raise_for_status()
            alert_id = UUID(response.json()["alert_id"])
            step("promoted", {"alert_id": str(alert_id)})

            def find_incident():
                row = db_one(
                    "SELECT ia.incident_id::text, i.status, i.version FROM alerts a "
                    "JOIN incident_alerts ia ON ia.tenant_id=a.tenant_id AND ia.alert_id=a.id "
                    "JOIN incidents i ON i.tenant_id=a.tenant_id AND i.id=ia.incident_id "
                    "WHERE a.id=%s", (str(alert_id),))
                return row and {"incident_id": row[0], "status": row[1], "version": row[2]}
            incident = poll(find_incident, 120, description="incident for promoted alert")
            incident_id = UUID(incident["incident_id"])
            step("incident", incident)

            def find_report():
                report = latest_report(settings, settings.dev_tenant_id, incident_id)
                if report and not report.is_stale and report.agent_trace \
                        and report.agent_trace.stop_reason:
                    return report
                return None
            report = poll(find_report, 180, description="autonomous investigation report")
            gates = [g.model_dump(mode="json") for g in report.root_cause_gates]
            passed = any(g["decision"] == "MECHANISM_SCOPE_SUPPORTED"
                         for g in gates)
            step("report", {"id": str(report.id), "status": report.status,
                            "stop_reason": report.agent_trace.stop_reason,
                            "model_calls": report.agent_trace.model_calls,
                            "tool_calls": report.agent_trace.tool_calls,
                            "gates": gates})
            ok &= passed

            # Proposal through the reviewed mapping only.
            response = client.post(
                f"{API}/api/v1/incidents/{incident_id}/remediation-plan", headers=operator)
            step("proposal", {"status": response.status_code,
                              "body": response.json()
                              if response.status_code == 201 else response.text[:200]})
            ok &= response.status_code == 201
            if response.status_code != 201:
                raise RuntimeError("gated candidate was not proposed")
            plan = response.json()
            action = plan["actions"][0]
            ok &= action["snapshot"]["action_type"] == "restart_cache"
            step("action", {"id": action["id"], "type": action["snapshot"]["action_type"],
                            "target": action["snapshot"]["target"]})

            response = client.post(
                f"{API}/api/v1/incidents/{incident_id}/remediation-plan/authorize",
                headers=operator)
            response.raise_for_status()
            decision_view = response.json()
            approval_id = decision_view["approval_id"]
            step("authorized", {"outcome": decision_view["outcome"],
                                "approval_id": str(approval_id),
                                "policy": decision_view["policy_version"]})

            current = client.get(f"{API}/api/v1/incidents/{incident_id}",
                                 headers=dev).json()
            response = client.post(f"{API}/api/v1/approvals/{approval_id}/decision",
                                   headers=approver,
                                   json={"decision": "APPROVE",
                                         "expected_version": 1,
                                         "reason": "独立审批：已核验缓存依赖失败，"
                                                   "同意受控重启支付缓存"})
            step("approval", {"status": response.status_code,
                              "body": response.json()
                              if response.status_code == 200 else response.text[:300]})
            ok &= response.status_code == 200
            _ = current

            action_id = UUID(action["id"])
            def action_status():
                row = db_one("SELECT status FROM actions WHERE id=%s AND tenant_id=%s",
                             (str(action_id), str(settings.dev_tenant_id)))
                return row and row[0]
            # Wait for a terminal state: the first poll can observe AUTHORIZED
            # moments before the executor claims it.
            final_status = "AUTHORIZED"
            try:
                final_status = poll(
                    lambda: action_status() in {"SUCCEEDED", "RECOVERY_OBSERVED",
                                                "RECONCILIATION_REQUIRED",
                                                    "PRECONDITION_FAILED"}
                    and action_status(),
                    180, description="executor attempt")
            except TimeoutError:
                pass
            step("action_status", {"status": final_status})
            ok &= final_status in {"SUCCEEDED", "RECOVERY_OBSERVED"}

            wait_for_recovery(client)

            response = client.post(f"{API}/api/v1/alerts/{alert_id}/resolve",
                                   headers=operator,
                                   json={"reason": "Redis已由受控动作恢复，"
                                                   "结账与支付就绪均正常，解除症状告警"})
            step("alert_resolved", response.json())
            ok &= response.status_code == 200

            response = client.get(f"{API}/api/v1/incidents/{incident_id}/recovery-assessment",
                                  headers=operator)
            assessment = response.json()
            step("recovery", {"ready": assessment.get("recovery_ready"),
                              "limitations": assessment.get("limitations")})
            ok &= assessment.get("recovery_ready") is True

            current = client.get(f"{API}/api/v1/incidents/{incident_id}", headers=dev).json()
            response = client.post(f"{API}/api/v1/incidents/{incident_id}/workflow/decision",
                                   headers=operator,
                                   json={"decision": "RESOLVE",
                                         "expected_version": current["version"],
                                         "reason": "恢复证据齐备：受控重启成功，"
                                                   "症状告警已核实解除"})
            step("resolve_decision", {"status": response.status_code})
            ok &= response.status_code == 202

            def resolved():
                row = db_one("SELECT status FROM incidents WHERE id=%s AND tenant_id=%s",
                             (str(incident_id), str(settings.dev_tenant_id)))
                return row and row[0]
            final_incident_status = poll(resolved, 120, description="incident RESOLVED")
            step("incident_final", {"status": final_incident_status})
            ok &= final_incident_status == "RESOLVED"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        ok = False
        raise
    finally:
        try:
            compose("start", "redis")
            with httpx.Client(timeout=15, trust_env=False) as client:
                wait_for_recovery(client)
            result["business_recovered"] = True
        except Exception as exc:  # noqa: BLE001
            result["business_recovered"] = False
            result["recovery_error"] = f"{type(exc).__name__}: {exc}"
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["passed"] = bool(ok and result.get("business_recovered"))
        output = ROOT / "eval" / (
            f"agent-remediation-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json")
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(json.dumps({"report": str(output.resolve()), "passed": result["passed"]},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
