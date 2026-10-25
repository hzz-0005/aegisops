"""Read persisted acceptance reports, API fences and whole-session provider accounting."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from aegisops.application.investigation.agent_contracts import (
    AgentHypothesis,
    AgentStep,
    ObservationClaim,
    ToolRequest,
)
from aegisops.application.investigation.candidate_verifier import verify_candidates
from aegisops.application.investigation.graph import load_snapshot
from aegisops.application.investigation.read_tools import ReadOnlyTelemetryTools
from aegisops.application.investigation.service import latest_report
from aegisops.application.investigation.tool_registry import ToolRegistry
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import InvestigationAgentRunRecord
from scripts.evaluate_fault_battery import checkout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", nargs="+", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    artifacts = [json.loads(p.read_text(encoding="utf-8")) for p in args.reports]
    since = min(datetime.fromisoformat(a["started_at"]) for a in artifacts)
    results: dict = {"at": datetime.now(UTC).isoformat(), "accounting_since": since.isoformat(),
                    "reports": [], "checks": {}, "real_model_calls_in_audit": 0}
    with httpx.Client(timeout=15, trust_env=False) as client:
        for artifact in artifacts:
            for stored in artifact["reports"]:
                incident_id = UUID(stored["incident_id"])
                first = latest_report(settings, settings.dev_tenant_id, incident_id)
                second = latest_report(settings, settings.dev_tenant_id, incident_id)
                assert first and second and first.agent_trace
                ids = {e.id for e in first.evidence}
                path = f"{os.getenv('AEGISOPS_API_URL', 'http://127.0.0.1:8000')}/api/v1/incidents/{incident_id}"
                response = client.get(f"{path}/investigation", headers={
                    "Authorization": f"Bearer {settings.alert_webhook_token}"})
                response.raise_for_status()
                checks = {
                    "same_version_read": first.id == second.id,
                    "not_stale": not first.is_stale,
                    "no_root_authority": first.status == "INCONCLUSIVE"
                        and first.primary_hypothesis is None and first.confidence == 0,
                    "verification_persisted": first.candidate_verifications
                        == second.candidate_verifications
                        == first.agent_trace.candidate_verifications,
                    "gates_persisted": first.root_cause_gates
                        == second.root_cause_gates
                        == first.agent_trace.root_cause_gates,
                    "api_verification_matches": response.json()["candidate_verifications"]
                        == [v.model_dump(mode="json") for v in first.candidate_verifications],
                    "api_gates_match": response.json()["root_cause_gates"]
                        == [g.model_dump(mode="json") for g in first.root_cause_gates],
                    "gates_never_authorize_actions": all(
                        g.action_eligible is False
                        and g.root_cause_status == "UNDETERMINED"
                        and g.current_fault_status == "UNKNOWN"
                        for g in first.root_cause_gates),
                    "citation_integrity": all(
                        set(v.supporting_evidence_ids + v.conflicting_evidence_ids) <= ids
                        for v in first.candidate_verifications),
                    "foreign_tenant_hidden": latest_report(settings, uuid4(), incident_id) is None,
                    "anonymous_denied": client.get(f"{path}/investigation").status_code == 401,
                }
                if checks["no_root_authority"]:
                    checks["plan_denied"] = client.post(f"{path}/remediation-plan", headers={
                        "Authorization": f"Bearer {settings.operator_token}"}).status_code == 409
                else:
                    checks["plan_denied"] = False
                entry = {"incident_id": str(incident_id), "checks": checks,
                         "verifications": [v.model_dump(mode="json")
                                           for v in first.candidate_verifications]}
                if artifact["case"] == "inventory-unavailable":
                    snap: dict = dict(load_snapshot(settings, settings.dev_tenant_id, incident_id))
                    alert = next(a for a in snap["alerts"] if a["service"] == "order-service")
                    with ReadOnlyTelemetryTools(settings) as tools:
                        facts = ToolRegistry(snap).execute(tools, ToolRequest(
                            name="incoming_requests", service="order-service",
                            alert_id=alert["id"]))
                    entry["incoming_gateway_observations"] = [f.model_dump(mode="json")
                                                              for f in facts]
                    checks["gateway_failure_gap_filled"] = any(
                        f.payload["failure_code"] in {"upstream_http_5xx", "upstream_transport"}
                        for f in facts)
                    if facts:
                        e = facts[0]
                        probe = first.agent_trace.model_copy(deep=True)
                        probe.hypotheses = [AgentHypothesis(
                            label="后端历史事实核验探测，不是模型诊断", state="CANDIDATE",
                            rationale="只核验适配器实际返回的精确身份，不判断根因",
                            supporting_evidence_ids=[e.id], claim=ObservationClaim(
                                kind="REQUEST_FAILURE", alert_id=alert["id"],
                                service=e.payload["service"], dependency=e.payload["dependency"],
                                failure_code=e.payload["failure_code"]))]
                        probe.steps = [AgentStep(number=1, phase="TOOL", status="COMPLETED",
                            input_hash="backend-verifier-probe", evidence_ids=[e.id],
                            evidence=[e.model_dump(mode="json")])]
                        verification = verify_candidates(snap, probe, fresh=True)[0]
                        entry["backend_verifier_probe_not_model_candidate"] = (
                            verification.model_dump(mode="json"))
                        checks["real_span_verifier_probe"] = (
                            verification.status == "OBSERVATION_VERIFIED"
                            and verification.root_cause_status == "UNDETERMINED"
                            and verification.alert_binding_status == "UNBOUND")
                results["reports"].append(entry)
        results["checks"]["api_ready"] = client.get(
            f"{os.getenv('AEGISOPS_API_URL', 'http://127.0.0.1:8000')}"
            "/api/v1/health/ready").status_code == 200
        results["checks"]["console_ready"] = client.get(os.getenv(
            "AEGISOPS_CONSOLE_URL", "http://127.0.0.1:8010") + "/").status_code == 200
        results["checks"]["checkout_recovered"] = checkout(client) == 200
        results["active_alerts"] = len(client.get("http://127.0.0.1:9093/api/v2/alerts").json())
    with session_for(settings.database_url) as session:
        rows = session.scalars(select(InvestigationAgentRunRecord).where(
            InvestigationAgentRunRecord.tenant_id == settings.dev_tenant_id,
            InvestigationAgentRunRecord.created_at >= since,
        )).all()
        results["provider_accounting"] = {
            "model_call_reservations": sum(r.model_calls for r in rows),
            "returned_total_tokens": sum(s.get("usage", {}).get("total_tokens", 0)
                for r in rows for s in r.trace.get("steps", []) if s["phase"] == "MODEL"),
            "model_calls_without_usage": sum(1 for r in rows for s in r.trace.get("steps", [])
                if s["phase"] == "MODEL" and "total_tokens" not in s.get("usage", {})),
            "running_ledgers": sum(r.status == "RUNNING" for r in rows),
        }
    results["own_containers"] = subprocess.check_output(
        ["docker", "compose", "ps", "--format", "{{.Service}} {{.State}}"], text=True).splitlines()
    source = Path("src/aegisops/application/investigation/agent_loop.py")
    local_hash = sha256(source.read_bytes()).hexdigest()
    deployed_hash = subprocess.check_output(["docker", "compose", "exec", "-T", "workflow-worker",
        "python", "-c", "import inspect,hashlib; from aegisops.application.investigation "
        "import agent_loop; print(hashlib.sha256(open(inspect.getfile(agent_loop),'rb').read())."
        "hexdigest())"], text=True).strip()
    results["deployment_hashes"] = {"local": local_hash, "workflow_worker": deployed_hash}
    results["checks"]["deployed_source_matches"] = local_hash == deployed_hash
    results["passed"] = all(results["checks"].values()) and all(
        all(r["checks"].values()) for r in results["reports"])
    output = Path("eval") / f"agent-observation-audit-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output.resolve()), "passed": results["passed"],
                      "provider_accounting": results["provider_accounting"]}, ensure_ascii=False))
    if not results["passed"]:
        raise AssertionError("audit failed; actual evidence retained")


if __name__ == "__main__":
    main()
