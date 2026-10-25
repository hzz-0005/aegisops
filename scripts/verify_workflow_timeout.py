"""Run a short review timer against one unstarted dev Incident."""

import asyncio

from sqlalchemy import func, select
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from aegisops.application.incident_workflow import (
    confirm_workflow_start,
    expire_operator_wait,
    reserve_workflow_start,
    workflow_id_for,
)
from aegisops.application.workflows import IncidentWorkflow
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    IncidentRecord,
    IncidentTimelineRecord,
    IncidentWorkflowStartRecord,
)


async def main() -> None:
    settings = Settings()
    with session_for(settings.database_url) as session:
        incident_id = session.scalar(
            select(IncidentRecord.id)
            .outerjoin(
                IncidentWorkflowStartRecord,
                IncidentWorkflowStartRecord.incident_id == IncidentRecord.id,
            )
            .where(
                IncidentRecord.tenant_id == settings.dev_tenant_id,
                IncidentRecord.status == "OPEN",
                IncidentRecord.processing_phase == "READY_FOR_INVESTIGATION",
                IncidentWorkflowStartRecord.id.is_(None),
            )
            .limit(1)
        )
    if incident_id is None:
        raise RuntimeError("no unstarted READY_FOR_INVESTIGATION Incident in dev database")
    assert reserve_workflow_start(settings, settings.dev_tenant_id, incident_id) == "START"
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    handle = await client.start_workflow(
        IncidentWorkflow.run,
        args=[str(settings.dev_tenant_id), str(incident_id), 3],
        id=workflow_id_for(settings.dev_tenant_id, incident_id),
        task_queue=settings.temporal_task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    confirm_workflow_start(settings, settings.dev_tenant_id, incident_id, handle.run_id)
    assert await asyncio.wait_for(handle.result(), timeout=30) == "ESCALATED"
    with session_for(settings.database_url) as session:
        incident = session.get(IncidentRecord, incident_id)
        assert incident is not None and incident.processing_phase == "ESCALATED"
        count = session.scalar(
            select(func.count(IncidentTimelineRecord.id)).where(
                IncidentTimelineRecord.incident_id == incident_id,
                IncidentTimelineRecord.event_type == "PHASE_CHANGED",
            )
        )
    assert count == 1
    assert (
        expire_operator_wait(settings, settings.dev_tenant_id, incident_id, 3)
        == "ALREADY_ADVANCED"
    )
    print(f"PASS review timeout incident={incident_id}: escalated once after 3 seconds")


if __name__ == "__main__":
    asyncio.run(main())
