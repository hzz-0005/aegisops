"""Audit selected real holdout samples and their time-scoped persisted investigation facts."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    InvestigationEvidenceRecord,
    InvestigationReportRecord,
)
from scripts.complete_lab_acceptance import fault_metrics

EXPECTED = {"inventory_process_frozen": ("inventory-service", "paused"),
            "payment_process_frozen": ("payment-service", "paused"),
            "simulated_payment_provider_timeout": ("payment-service", "running")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    samples: dict[tuple[str, int], dict[str, Any]] = {}
    superseded = []
    for path in args.reports:
        for case in json.loads(path.read_text(encoding="utf-8")):
            if case["fault"] not in EXPECTED:
                continue
            key = (case["fault"], case["repeat_index"])
            if key in samples:
                superseded.append({"fault": key[0], "repeat_index": key[1],
                                   "source_report": samples[key]["source_report"]})
            samples[key] = {**case, "source_report": str(path)}
    outputs = []
    with session_for(settings.database_url) as session:
        for case in samples.values():
            target, expected_state = EXPECTED[case["fault"]]
            ids = {UUID(a["incident_id"]) for a in case["alerts"] if a["incident_id"]}
            start = datetime.fromisoformat(case["started_at"])
            end = max(datetime.fromisoformat(a["received_at"]) for a in case["alerts"])
            rows = session.execute(select(InvestigationReportRecord, InvestigationEvidenceRecord)
                .join(InvestigationEvidenceRecord,
                      (InvestigationEvidenceRecord.report_id == InvestigationReportRecord.id)
                      & (InvestigationEvidenceRecord.tenant_id ==
                         InvestigationReportRecord.tenant_id))
                .where(InvestigationReportRecord.tenant_id == settings.dev_tenant_id,
                       InvestigationReportRecord.incident_id.in_(ids),
                       InvestigationReportRecord.graph_version ==
                       "readonly-investigation-v6-runtime-requests",
                       InvestigationEvidenceRecord.observed_at >= start,
                       InvestigationEvidenceRecord.observed_at <= end,
                       InvestigationEvidenceRecord.kind.in_(
                           ["RUNTIME_STATE_OBSERVATION", "VERIFIED_DEPENDENCY_REQUEST"]))).all()
            facts = [{"incident_id": str(r.incident_id), "report_id": str(r.id),
                      "evidence_id": str(e.id), "kind": e.kind, "polarity": e.polarity,
                      "observed_at": e.observed_at.isoformat(), "payload": e.payload}
                     for r, e in rows if e.observed_at is not None]
            runtime = [e for e in facts if e["kind"] == "RUNTIME_STATE_OBSERVATION"
                       and e["payload"].get("service") == target]
            requests = [e for e in facts if e["kind"] == "VERIFIED_DEPENDENCY_REQUEST"
                        and e["payload"].get("dependency") == target]
            checks = {
                "discovered_and_recovered": case["detected"] and case["recovered"],
                "no_pending": all(a["investigation_status"] is not None for a in case["alerts"]),
                "expected_runtime_observed": any(e["payload"].get("state") == expected_state
                                                  for e in runtime),
                "no_pause_in_provider_delay": case["mode"] != "provider-delay" or not any(
                    e["payload"].get("state") == "paused" for e in runtime),
                "request_timeout_verified": any(
                    e["payload"].get("failure_code") == "dependency_timeout"
                    and e["payload"].get("provenance_status") == "VERIFIED_SPAN_FACT"
                    and e["payload"].get("alert_binding_status") == "UNBOUND" for e in requests),
                "context_only": bool(facts) and all(e["polarity"] == "CONTEXT" for e in facts),
                "no_wrong_supported_root": case["wrong_supported_root_count"] == 0,
                "no_wrong_plan": case["wrong_action_plan_count"] == 0,
                "no_false_integrity_alert": not any(
                    a["alert"] == "LabBusinessContractViolation" for a in case["alerts"]),
            }
            outputs.append({"fault": case["fault"], "repeat_index": case["repeat_index"],
                            "source_report": case["source_report"], "checks": checks,
                            "passed": all(checks.values()), "facts": facts})
    complete = set(samples) == {(name, repeat) for name in EXPECTED for repeat in (1, 2)}
    result = {"captured_at": datetime.now(UTC).isoformat(), "samples": outputs,
              "complete_expected_set": complete, "superseded_samples": superseded,
              "metrics": fault_metrics(list(samples.values())), "model_calls_exercised": False,
              "passed": complete and all(item["passed"] for item in outputs)}
    path = Path("eval") / f"runtime-holdout-audit-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "passed": result["passed"],
                      "checks": [{"fault": s["fault"], "repeat": s["repeat_index"],
                                  **s["checks"]} for s in outputs]}, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
