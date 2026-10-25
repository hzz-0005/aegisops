"""Tenant-scoped, read-only execution ledger views for operator review."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import ActionAttemptRecord, ActionRecord


class ActionAttemptView(BaseModel):
    id: UUID
    action_id: UUID
    state: str
    executor_id: str
    fencing_token: int
    started_at: datetime
    finished_at: datetime | None
    result: dict


def list_action_attempts(
    settings: Settings, tenant_id: UUID, incident_id: UUID, action_id: UUID
) -> list[ActionAttemptView] | None:
    with session_for(settings.database_url) as session:
        action = session.scalar(
            select(ActionRecord.id).where(
                ActionRecord.tenant_id == tenant_id,
                ActionRecord.incident_id == incident_id,
                ActionRecord.id == action_id,
            )
        )
        if action is None:
            return None
        rows = session.scalars(
            select(ActionAttemptRecord)
            .where(
                ActionAttemptRecord.tenant_id == tenant_id,
                ActionAttemptRecord.action_id == action_id,
            )
            .order_by(ActionAttemptRecord.attempt_number)
        ).all()
        return [
            ActionAttemptView(
                id=row.id,
                action_id=row.action_id,
                state=row.state,
                executor_id=row.executor_id,
                fencing_token=row.fencing_token,
                started_at=row.started_at,
                finished_at=row.finished_at,
                result=row.result,
            )
            for row in rows
        ]
