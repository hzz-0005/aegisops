"""Score only root-specific alerts; a mixed gateway symptom has no single-root oracle."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from aegisops.application.fault_evaluation import evaluate_fault_sample
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import AlertRecord, IncidentAlertRecord


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_report", type=Path)
    args = parser.parse_args()
    source = json.loads(args.source_report.read_text(encoding="utf-8"))
    if source["sample"] != "concurrent_inventory_currency_drift_and_payment_provider-timeout":
        raise ValueError("this oracle requires the named controlled concurrent experiment")
    settings = Settings()
    require_dev_runtime(settings)
    roots_by_alert = {"OrderBusinessContractRejected": "inventory_currency_drift",
                      "LabBusinessContractViolation": "inventory_currency_drift",
                      "OrderDependencyFailure": "payment_provider_timeout"}
    alerts = []
    roots = {}
    with session_for(settings.database_url) as session:
        for captured in source["alerts"]:
            rows = session.execute(select(AlertRecord.id, IncidentAlertRecord.incident_id)
                .outerjoin(IncidentAlertRecord,
                    (IncidentAlertRecord.alert_id == AlertRecord.id)
                    & (IncidentAlertRecord.tenant_id == AlertRecord.tenant_id))
                .where(AlertRecord.tenant_id == settings.dev_tenant_id,
                       AlertRecord.service_name == captured["service"],
                       AlertRecord.received_at == datetime.fromisoformat(captured["received_at"]),
                       AlertRecord.payload["alertname"].astext == captured["alert"])).all()
            if len(rows) != 1 or str(rows[0].incident_id) != captured["incident_id"]:
                raise ValueError("captured membership no longer matches persisted alert")
            key = str(rows[0].id)
            alerts.append({**captured, "alert_id": key})
            if captured["alert"] in roots_by_alert:
                roots[key] = roots_by_alert[captured["alert"]]
    metrics = evaluate_fault_sample({"alerts": alerts, "recovered": all(
        s == 200 for s in source["recovered"].values())}, alert_roots=roots,
        compatible_domains=set(), exact_hypotheses=set(), allowed_actions=set())
    report = {"source_report": str(args.source_report),
        "scope": "retrospective_audit_of_existing_real_concurrent_sample_not_new_blind_test",
        "oracle_basis": "root-specific business-contract and dependency alerts; gateway unscored",
        "metrics": metrics}
    path = Path(__file__).resolve().parents[1] / "eval" / (
        f"concurrent-partition-audit-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"coverage": metrics["oracle_coverage"],
                      "partition": metrics["partition"], "report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
