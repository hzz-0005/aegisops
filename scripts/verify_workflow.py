"""Check Temporal execution and duplicate event idempotence against the live dev stack."""

import asyncio
from uuid import uuid4

import aio_pika
from sqlalchemy import func, select
from temporalio.client import Client

from aegisops.application.contracts import IncidentCreatedEvent
from aegisops.application.incident_workflow import (
    mark_investigating,
    reserve_workflow_start,
    workflow_id_for,
)
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import IncidentRecord, IncidentTimelineRecord


def snapshot(settings: Settings):
    with session_for(settings.database_url) as session:
        row = session.scalar(
            select(IncidentTimelineRecord)
            .where(IncidentTimelineRecord.event_type == "WORKFLOW_STARTED")
            .order_by(IncidentTimelineRecord.created_at)
            .limit(1)
        )
        if row is None:
            raise RuntimeError("no workflow has entered investigation")
        incident = session.get(IncidentRecord, row.incident_id)
        assert incident is not None
        count = session.scalar(
            select(func.count(IncidentTimelineRecord.id)).where(
                IncidentTimelineRecord.tenant_id == row.tenant_id,
                IncidentTimelineRecord.incident_id == row.incident_id,
                IncidentTimelineRecord.event_type == "WORKFLOW_STARTED",
            )
        )
        return row.tenant_id, row.incident_id, incident.version, count


async def main() -> None:
    settings = Settings()
    tenant_id, incident_id, version, count = snapshot(settings)
    assert count == 1
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    description = await client.get_workflow_handle(
        workflow_id_for(tenant_id, incident_id)
    ).describe()
    assert description.status is not None and description.status.name == "COMPLETED"

    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        event = IncidentCreatedEvent(
            message_id=uuid4(), tenant_id=tenant_id, incident_id=incident_id
        )
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=event.model_dump_json().encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key="incident.created.v1",
            mandatory=True,
        )
    await asyncio.sleep(3)
    assert snapshot(settings) == (tenant_id, incident_id, version, 1)
    assert mark_investigating(settings, tenant_id, incident_id) == "ALREADY_ADVANCED"
    assert snapshot(settings) == (tenant_id, incident_id, version, 1)
    assert reserve_workflow_start(settings, tenant_id, incident_id) == "STARTED"
    print(f"PASS workflow={workflow_id_for(tenant_id, incident_id)} status=COMPLETED")
    print("PASS duplicate incident.created.v1: one timeline event, unchanged Incident version")
    print("PASS repeated activity: no additional phase transition or timeline event")
    print("PASS durable start ledger: repeated start is blocked in PostgreSQL")


if __name__ == "__main__":
    asyncio.run(main())
