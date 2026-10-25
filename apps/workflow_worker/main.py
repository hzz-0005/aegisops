"""Temporal owns durable Incident orchestration; database writes stay in activities."""

import asyncio
import logging
from uuid import UUID

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from aegisops.application.incident_workflow import (
    apply_operator_decision,
    expire_operator_wait,
    mark_investigating,
)
from aegisops.application.investigation.service import investigate_incident
from aegisops.application.workflows import IncidentRefreshWorkflow, IncidentWorkflow
from aegisops.config import Settings, require_dev_runtime

logger = logging.getLogger(__name__)


@activity.defn
async def enter_investigation(tenant_id: str, incident_id: str) -> str:
    return await asyncio.to_thread(
        mark_investigating, Settings(), UUID(tenant_id), UUID(incident_id)
    )


@activity.defn(name="apply_operator_decision")
async def apply_operator_decision_activity(
    tenant_id: str,
    incident_id: str,
    decision: str,
    expected_version: int,
    reason: str,
    operator_id: str,
) -> str:
    return await asyncio.to_thread(
        apply_operator_decision,
        Settings(),
        UUID(tenant_id),
        UUID(incident_id),
        decision,
        expected_version,
        reason,
        operator_id,
    )


@activity.defn(name="investigate_incident")
async def investigate_incident_activity(tenant_id: str, incident_id: str) -> str:
    return await asyncio.to_thread(
        investigate_incident, Settings(), UUID(tenant_id), UUID(incident_id)
    )


@activity.defn(name="expire_operator_wait")
async def expire_operator_wait_activity(
    tenant_id: str, incident_id: str, review_timeout_seconds: int
) -> str:
    return await asyncio.to_thread(
        expire_operator_wait,
        Settings(),
        UUID(tenant_id),
        UUID(incident_id),
        review_timeout_seconds,
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    require_dev_runtime(settings)
    while True:
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            break
        except Exception:
            logger.exception("Temporal unavailable at startup; retrying")
            await asyncio.sleep(2)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[IncidentWorkflow, IncidentRefreshWorkflow],
        activities=[
            enter_investigation,
            investigate_incident_activity,
            apply_operator_decision_activity,
            expire_operator_wait_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
