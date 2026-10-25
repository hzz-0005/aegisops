"""Exercise a new alert through correlation, durable start ledger, and Temporal."""

import asyncio
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

import httpx
from sqlalchemy import select
from temporalio.client import Client

from aegisops.application.incident_workflow import workflow_id_for
from aegisops.application.investigation.service import latest_report
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    AlertRecord,
    IncidentAlertRecord,
    IncidentRecord,
    IncidentWorkflowStartRecord,
)


async def main() -> None:
    settings = Settings()
    fingerprint = uuid4().hex
    service = f"workflow-probe-{fingerprint[:10]}"
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": fingerprint,
                "startsAt": datetime.now(UTC).isoformat(),
                "labels": {
                    "alertname": "WorkflowStartProbe",
                    "service": service,
                    "severity": "warning",
                },
                "annotations": {"summary": "Workflow start verification"},
            }
        ],
    }
    async with httpx.AsyncClient(timeout=5) as http:
        response = await http.post(
            "http://127.0.0.1:8000/api/v1/alerts/ingest",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.alert_webhook_token or 'aegisops_dev_only'}"
            },
        )
        response.raise_for_status()
        assert response.json() == {"accepted": 1, "duplicates": 0}

    deadline = monotonic() + 45
    while monotonic() < deadline:
        with session_for(settings.database_url) as session:
            row = session.execute(
                select(IncidentRecord, IncidentWorkflowStartRecord)
                .join(IncidentAlertRecord, IncidentAlertRecord.incident_id == IncidentRecord.id)
                .join(AlertRecord, AlertRecord.id == IncidentAlertRecord.alert_id)
                .outerjoin(
                    IncidentWorkflowStartRecord,
                    IncidentWorkflowStartRecord.incident_id == IncidentRecord.id,
                )
                .where(AlertRecord.fingerprint == fingerprint)
            ).first()
            if row is not None:
                incident, start = row
                if start is not None and start.state == "STARTED":
                    assert incident.processing_phase == "INVESTIGATING"
                    client = await Client.connect(
                        settings.temporal_address, namespace=settings.temporal_namespace
                    )
                    description = await client.get_workflow_handle(
                        workflow_id_for(incident.tenant_id, incident.id)
                    ).describe()
                    assert description.status is not None
                    assert description.status.name == "RUNNING"
                    review_state = await client.get_workflow_handle(
                        workflow_id_for(incident.tenant_id, incident.id)
                    ).query("review_state")
                    report = latest_report(settings, incident.tenant_id, incident.id)
                    if report is None or not review_state.startswith("WAITING_OPERATOR:"):
                        await asyncio.sleep(1)
                        continue
                    assert report.status == "INCONCLUSIVE"
                    assert report.primary_hypothesis is None
                    assert review_state == "WAITING_OPERATOR:INCONCLUSIVE"
                    assert any(item.source_type == "ALERT" for item in report.evidence)
                    print(f"PASS incident={incident.id}: inconclusive report, waiting for operator")
                    return
        await asyncio.sleep(1)
    raise RuntimeError("new incident workflow did not complete within 45 seconds")


if __name__ == "__main__":
    asyncio.run(main())
