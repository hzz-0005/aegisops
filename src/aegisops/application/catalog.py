"""Trusted change-event recording against the service catalog."""

from datetime import UTC
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aegisops.application.contracts import ChangeEventInput, ChangeEventView
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import ChangeEventRecord, ServiceRecord


class UnknownService(Exception):
    pass


class ChangeConflict(Exception):
    pass


def record_change(settings: Settings, body: ChangeEventInput) -> ChangeEventView:
    with session_for(settings.database_url) as session, session.begin():
        service = session.scalar(
            select(ServiceRecord).where(
                ServiceRecord.tenant_id == settings.dev_tenant_id,
                ServiceRecord.environment == body.environment,
                ServiceRecord.slug == body.service,
            )
        )
        if service is None:
            raise UnknownService
        occurred_at = body.occurred_at.astimezone(UTC)
        change_id = uuid4()
        inserted = session.execute(
            insert(ChangeEventRecord)
            .values(
                id=change_id,
                tenant_id=settings.dev_tenant_id,
                service_id=service.id,
                source=body.source,
                external_id=body.external_id,
                change_type=body.change_type,
                from_ref=body.from_ref,
                to_ref=body.to_ref,
                occurred_at=occurred_at,
                attributes=body.deployment.model_dump() if body.deployment else {},
            )
            .on_conflict_do_nothing(constraint="uq_change_events_source")
            .returning(ChangeEventRecord.id)
        ).scalar_one_or_none()
        if inserted is None:
            previous = session.scalar(
                select(ChangeEventRecord).where(
                    ChangeEventRecord.tenant_id == settings.dev_tenant_id,
                    ChangeEventRecord.source == body.source,
                    ChangeEventRecord.external_id == body.external_id,
                )
            )
            if previous is None or (
                previous.service_id != service.id
                or previous.change_type != body.change_type
                or previous.from_ref != body.from_ref
                or previous.to_ref != body.to_ref
                or previous.occurred_at != occurred_at
                or previous.attributes != (
                    body.deployment.model_dump() if body.deployment else {}
                )
            ):
                raise ChangeConflict
            change_id = previous.id
    return ChangeEventView(
        id=change_id,
        source=body.source,
        external_id=body.external_id,
        service=body.service,
        change_type=body.change_type,
        occurred_at=occurred_at,
    )
