"""Read saved investigations and separately replay exact request context collection."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from aegisops.application.investigation.read_tools import ReadOnlyTelemetryTools
from aegisops.application.investigation.service import latest_report
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    InvestigationEvidenceRecord,
    InvestigationReportRecord,
)

KINDS = {"VERIFIED_DATABASE_OPERATION", "VERIFIED_DEPENDENCY_REQUEST"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--require-inventory-evidence", action="store_true")
    parser.add_argument("--require-database-ancestry", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    results: list[dict[str, Any]] = []
    with session_for(settings.database_url) as session, ReadOnlyTelemetryTools(settings) as tools:
        for path in args.reports:
            for case in json.loads(path.read_text(encoding="utf-8")):
                if case.get("evaluation_failed") or not case.get("alerts"):
                    continue
                start = datetime.fromisoformat(case["started_at"])
                end = max(datetime.fromisoformat(a["received_at"]) for a in case["alerts"])
                completed = datetime.fromisoformat(case["evaluation_completed_at"]) if (
                    case.get("evaluation_completed_at")
                ) else end + timedelta(seconds=90)
                incidents = {UUID(a["incident_id"]) for a in case["alerts"] if a["incident_id"]}
                reports = session.scalars(select(InvestigationReportRecord).where(
                    InvestigationReportRecord.tenant_id == settings.dev_tenant_id,
                    InvestigationReportRecord.incident_id.in_(incidents),
                    InvestigationReportRecord.completed_at >= start,
                    InvestigationReportRecord.completed_at <= completed,
                ).order_by(InvestigationReportRecord.source_incident_version.desc(),
                           InvestigationReportRecord.completed_at.desc())).all()
                # One latest saved report per incident; older attempts are not current evidence.
                latest: dict[UUID, InvestigationReportRecord] = {}
                for report in reports:
                    latest.setdefault(report.incident_id, report)
                saved = []
                for report in latest.values():
                    stored_facts = session.scalars(select(InvestigationEvidenceRecord).where(
                        InvestigationEvidenceRecord.tenant_id == settings.dev_tenant_id,
                        InvestigationEvidenceRecord.report_id == report.id,
                        InvestigationEvidenceRecord.kind.in_(KINDS),
                        InvestigationEvidenceRecord.observed_at >= start,
                        InvestigationEvidenceRecord.observed_at <= end,
                    )).all()
                    current = latest_report(settings, settings.dev_tenant_id, report.incident_id)
                    saved.append({"incident_id": str(report.incident_id),
                        "report_id": str(report.id), "graph_version": report.graph_version,
                        "source_version": report.source_incident_version,
                        "current_version": current.current_incident_version if current else None,
                        "stale": (current is None or current.id != report.id or current.is_stale),
                        "status": report.status, "root": report.primary_hypothesis,
                        "completed_at": report.completed_at.isoformat(),
                        "facts": [{"kind": e.kind, "payload": e.payload,
                                   "observed_at": e.observed_at.isoformat()} for e in stored_facts
                                  if e.observed_at is not None]})
                replay = []
                for alert in case["alerts"]:
                    if alert["service"] == "inventory-service":
                        replay.extend(tools.generic_incoming_requests(
                            tenant_id=settings.dev_tenant_id, service=alert["service"],
                            occurred_at=datetime.fromisoformat(alert["occurred_at"])))
                results.append({"source_report": str(path), "fault": case["fault"],
                    "repeat_index": case["repeat_index"], "saved_reports": saved,
                    "all_incidents_have_saved_report": set(latest) == incidents,
                    "report_collection_deadline": completed.isoformat(),
                    "readonly_context_replay_not_saved_report": [e.model_dump(mode="json")
                                                                 for e in replay]})
    checks = []
    for sample in results:
        facts = [e for report in sample["saved_reports"] for e in report["facts"]]
        database = [e for e in facts if e["kind"] == "VERIFIED_DATABASE_OPERATION"]
        incoming = [e for e in facts if e["payload"].get("query_direction") == "INCOMING"
                    and e["payload"].get("dependency") == "inventory-service"]
        fresh = sample["all_incidents_have_saved_report"] and not any(
            report["stale"] for report in sample["saved_reports"])
        if sample["fault"] == "inventory_process_stopped":
            checks.append({"fault": sample["fault"], "repeat_index": sample["repeat_index"],
                           "passed": fresh and bool(incoming) and not database})
        elif sample["fault"] == "inventory_table_read_blocked":
            lock = [e for e in database if e["payload"].get("sqlstate") == "55P03"
                    and e["payload"].get("failure_code") == "database_lock_timeout"
                    and e["payload"].get("alert_binding_status") == "UNBOUND"
                    and e["payload"].get("current_state") == "UNKNOWN"]
            ancestry = [link for e in lock for link in e["payload"].get("caller_links", [])
                        if link.get("downstream_event_id") == e["payload"].get("event_id")
                        and link.get("downstream_service") == e["payload"].get("service")
                        and link.get("relationship_status") == "VERIFIED_REQUEST_ANCESTRY"
                        and link.get("alert_binding_status") == "UNBOUND"
                        and link.get("root_cause_status") == "UNDETERMINED"]
            checks.append({"fault": sample["fault"], "repeat_index": sample["repeat_index"],
                           "passed": fresh and bool(lock) and (
                               bool(ancestry) if args.require_database_ancestry else True),
                           "persisted_database_caller_links": len(ancestry),
                           "distinct_database_event_ids": len({e["payload"]["event_id"]
                                                                for e in lock}),
                           "distinct_caller_pairs": len({(link["upstream_event_id"],
                                                          link["downstream_event_id"])
                                                         for link in ancestry})})
    result: dict[str, Any] = {"captured_at": datetime.now(UTC).isoformat(), "samples": results}
    result["inventory_evidence_checks"] = checks
    result["passed"] = bool(checks) and all(check["passed"] for check in checks)
    output = Path("eval") / f"database-context-audit-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output.resolve()), "passed": result["passed"], "samples": [{
        "fault": s["fault"], "repeat_index": s["repeat_index"],
        "saved_database_facts": sum(e["kind"] == "VERIFIED_DATABASE_OPERATION"
            for r in s["saved_reports"] for e in r["facts"]),
        "saved_incoming_inventory_facts": sum(e["payload"].get("query_direction") == "INCOMING"
            and e["payload"].get("dependency") == "inventory-service"
            for r in s["saved_reports"] for e in r["facts"]),
        "stale_reports": sum(r["stale"] for r in s["saved_reports"]),
    } for s in results]}, ensure_ascii=False))
    if (args.require_inventory_evidence or args.require_database_ancestry) and not result["passed"]:
        raise AssertionError("latest saved inventory evidence acceptance failed")


if __name__ == "__main__":
    main()
