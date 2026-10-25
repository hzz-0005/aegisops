"""Start one Temporal workflow for each incident.created.v1 event."""

import asyncio
import logging

import aio_pika
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from aegisops.application.contracts import IncidentCreatedEvent, IncidentUpdatedEvent
from aegisops.application.incident_workflow import (
    confirm_workflow_start,
    reserve_workflow_start,
    workflow_id_for,
)
from aegisops.application.workflows import IncidentRefreshWorkflow, IncidentWorkflow
from aegisops.config import Settings, require_dev_runtime

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    require_dev_runtime(settings)
    while True:
        try:
            temporal = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            rabbitmq = await aio_pika.connect_robust(settings.rabbitmq_url)
            break
        except Exception:
            logger.exception("workflow dependencies unavailable at startup; retrying")
            await asyncio.sleep(2)
    async with rabbitmq:
        channel = await rabbitmq.channel()
        await channel.set_qos(prefetch_count=10)
        queue = await channel.declare_queue(
            "incident.created.v1", durable=True, arguments={"x-queue-type": "quorum"}
        )
        updates = await channel.declare_queue(
            "incident.updated.v1", durable=True, arguments={"x-queue-type": "quorum"}
        )

        async def on_update(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            try:
                event = IncidentUpdatedEvent.model_validate_json(message.body)
                identity = (f"incident-refresh:{event.tenant_id}:{event.incident_id}:"
                            f"v{event.incident_version}")
                try:
                    await temporal.start_workflow(
                        IncidentRefreshWorkflow.run,
                        args=[str(event.tenant_id), str(event.incident_id)],
                        id=identity, task_queue=settings.temporal_task_queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
            except Exception:
                logger.exception("incident refresh start failed; requeueing")
                await asyncio.sleep(1)
                await message.nack(requeue=True)
            else:
                await message.ack()

        async def on_incident(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            try:
                event = IncidentCreatedEvent.model_validate_json(message.body)
                workflow_id = workflow_id_for(event.tenant_id, event.incident_id)
                reservation = await asyncio.to_thread(
                    reserve_workflow_start, settings, event.tenant_id, event.incident_id
                )
                if reservation == "START":
                    try:
                        handle = await temporal.start_workflow(
                            IncidentWorkflow.run,
                            args=[
                                str(event.tenant_id),
                                str(event.incident_id),
                                settings.workflow_review_timeout_seconds,
                            ],
                            id=workflow_id,
                            task_queue=settings.temporal_task_queue,
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        )
                        run_id = handle.result_run_id
                        logger.info("started incident workflow %s", workflow_id)
                    except WorkflowAlreadyStartedError as exc:
                        run_id = exc.run_id
                        logger.info("incident workflow already exists: %s", workflow_id)
                    await asyncio.to_thread(
                        confirm_workflow_start,
                        settings,
                        event.tenant_id,
                        event.incident_id,
                        run_id,
                    )
                else:
                    logger.info("incident workflow %s start state: %s", workflow_id, reservation)
            except Exception:
                logger.exception("incident workflow start failed; requeueing")
                await asyncio.sleep(1)
                await message.nack(requeue=True)
            else:
                await message.ack()

        await queue.consume(on_incident, no_ack=False)
        await updates.consume(on_update, no_ack=False)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
