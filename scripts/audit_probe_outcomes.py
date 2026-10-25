"""Read-only audit of a frozen/contract holdout battery and persisted probe evidence."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    InvestigationEvidenceRecord,
    InvestigationReportRecord,
)
from scripts.complete_lab_acceptance import fault_metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    cases = json.loads(args.report.read_text(encoding="utf-8"))
    frozen = [c for c in cases if c["mode"] == "pause"]
    drift = [c for c in cases if c["mode"] == "inventory-currency-drift"]
    ids = {UUID(a["incident_id"]) for c in cases for a in c["alerts"] if a["incident_id"]}
    observations = []
    with session_for(settings.database_url) as session:
        rows = session.execute(select(InvestigationReportRecord, InvestigationEvidenceRecord)
            .join(InvestigationEvidenceRecord,
                  (InvestigationEvidenceRecord.report_id == InvestigationReportRecord.id)
                  & (InvestigationEvidenceRecord.tenant_id == InvestigationReportRecord.tenant_id))
            .where(InvestigationReportRecord.tenant_id == settings.dev_tenant_id,
                   InvestigationReportRecord.incident_id.in_(ids),
                   InvestigationEvidenceRecord.kind == "DEPENDENCY_PROBE_ACTIVITY")
            .order_by(InvestigationReportRecord.completed_at)).all()
        for report, evidence in rows:
            observations.append({"incident_id": str(report.incident_id),
                                 "report_id": str(report.id), "graph_version": report.graph_version,
                                 "status": report.status, "polarity": evidence.polarity,
                                 "payload": evidence.payload})
    metrics = fault_metrics(frozen)
    checks = {
        "no_pending_frozen_investigations": metrics["pending_incident_count"] == 0,
        "four_frozen_trials": len(frozen) == 4,
        "all_frozen_discovered_and_recovered": all(
            c["detected"] and c["recovered"] for c in frozen),
        "no_false_integrity_alerts": all(
            a["alert"] != "LabBusinessContractViolation" for c in frozen for a in c["alerts"]),
        "two_true_drift_trials_still_alert": len(drift) == 2 and all(
            any(a["alert"] == "LabBusinessContractViolation" for a in c["alerts"]) for c in drift),
        "all_cases_recovered": all(c["recovered"] for c in cases),
        "no_wrong_supported_domains": all(c["wrong_supported_root_count"] == 0 for c in cases),
        "no_wrong_plans": all(c["wrong_action_plan_count"] == 0 for c in cases),
        "persisted_timeout_evidence": any(
            e["payload"].get("failure_reason_counts", {}).get("timeout", 0) > 0
            and e["graph_version"] == "readonly-investigation-v5-probe-outcomes"
            for e in observations),
        "probe_observations_context_only": bool(observations) and all(
            e["polarity"] == "CONTEXT" for e in observations),
    }
    result = {"captured_at": datetime.now(UTC).isoformat(), "source_report": str(args.report),
              "checks": checks, "passed": all(checks.values()),
              "frozen_metrics": metrics, "probe_observations": observations,
              "model_calls_exercised": False}
    path = Path("eval") / f"probe-outcomes-audit-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "passed": result["passed"], "checks": checks},
                     ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
