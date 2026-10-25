import asyncio
import logging
import os
from datetime import UTC, datetime

import aio_pika
from sqlalchemy import select

from aegisops.application.contracts import (
    AlertReceivedEvent,
    IncidentCreatedEvent,
    IncidentUpdatedEvent,
)
from aegisops.application.correlation import correlate_alert
from aegisops.config import Profile, Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import OutboxEventRecord

logger = logging.getLogger(__name__)


async def publish_pending(settings: Settings, channel: aio_pika.abc.AbstractChannel) -> None:
    with session_for(settings.database_url) as session:
        pending = session.scalars(
            select(OutboxEventRecord)
            .where(OutboxEventRecord.published_at.is_(None))
            .order_by(OutboxEventRecord.created_at, OutboxEventRecord.id)
            .limit(50)
        ).all()
        for row in pending:
            envelope: AlertReceivedEvent | IncidentCreatedEvent | IncidentUpdatedEvent
            if row.event_type == "alert.received.v1":
                envelope = AlertReceivedEvent(
                    message_id=row.id,
                    tenant_id=row.tenant_id,
                    alert_id=row.payload["alert_id"],
                )
            elif row.event_type == "incident.created.v1":
                envelope = IncidentCreatedEvent(
                    message_id=row.id,
                    tenant_id=row.tenant_id,
                    incident_id=row.payload["incident_id"],
                )
            elif row.event_type == "incident.updated.v1":
                envelope = IncidentUpdatedEvent(
                    message_id=row.id, tenant_id=row.tenant_id,
                    incident_id=row.payload["incident_id"],
                    incident_version=row.payload["incident_version"],
                )
            else:
                raise ValueError(f"unknown outbox event: {row.event_type}")
            message = aio_pika.Message(
                body=envelope.model_dump_json().encode(),
                message_id=str(row.id),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            await channel.default_exchange.publish(
                message, routing_key=row.event_type, mandatory=True
            )
            row.published_at = datetime.now(UTC)
            row.version += 1
            session.commit()


async def publisher_loop(settings: Settings, channel: aio_pika.abc.AbstractChannel) -> None:
    while True:
        try:
            await publish_pending(settings, channel)
        except Exception:
            logger.exception("outbox publication failed; will retry")
        await asyncio.sleep(1)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    require_dev_runtime(settings)
    while True:
        try:
            connection = await aio_pika.connect_robust(settings.rabbitmq_url)
            break
        except (OSError, aio_pika.AMQPException):
            logger.warning("RabbitMQ unavailable at startup; retrying")
            await asyncio.sleep(2)
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=10)
        queue = await channel.declare_queue(
            "alert.received.v1", durable=True, arguments={"x-queue-type": "quorum"}
        )
        await channel.declare_queue(
            "incident.created.v1", durable=True, arguments={"x-queue-type": "quorum"}
        )
        await channel.declare_queue(
            "incident.updated.v1", durable=True, arguments={"x-queue-type": "quorum"}
        )

        async def on_alert(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            try:
                event = AlertReceivedEvent.model_validate_json(message.body)
                await asyncio.to_thread(correlate_alert, settings, event)
                if (
                    settings.profile is Profile.DEV
                    and os.getenv("AEGISOPS_CRASH_AFTER_COMMIT_MESSAGE_ID") == str(event.message_id)
                ):
                    os._exit(77)
            except Exception:
                logger.exception("alert event processing failed; requeueing")
                await asyncio.sleep(1)
                await message.nack(requeue=True)
            else:
                await message.ack()

        await queue.consume(on_alert, no_ack=False)
        await publisher_loop(settings, channel)


if __name__ == "__main__":
    asyncio.run(main())
