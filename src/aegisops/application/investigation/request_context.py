"""Bounded tenant-scoped request facts; time proximity never binds an aggregate alert."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import aliased

from aegisops.application.request_events import RequestFailureEventView, _view
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    RequestFailureEventRecord,
    ServiceDependencyRecord,
    ServiceRecord,
)

DEPENDENCY_CODES = {"dependency_timeout", "dependency_transport", "dependency_http_5xx"}
CLIENT_FAILURE_CODES = DEPENDENCY_CODES | {"upstream_http_5xx", "upstream_transport"}
DATABASE_CODES = {"database_lock_timeout", "database_query_cancelled", "database_error"}
DATABASE_CANDIDATE_LIMIT = 8


def incoming_candidates(settings: Settings, *, tenant_id: UUID, service: str,
                        occurred_at: datetime) -> list[RequestFailureEventView]:
    """Only callers with an explicit same-tenant catalog dependency on this service."""
    with session_for(settings.database_url) as session:
        target = session.scalar(select(ServiceRecord.id).where(
            ServiceRecord.tenant_id == tenant_id, ServiceRecord.slug == service,
        ))
        if target is None:
            return []
        callers = select(ServiceRecord.slug).join(ServiceDependencyRecord,
            (ServiceDependencyRecord.service_id == ServiceRecord.id)
            & (ServiceDependencyRecord.tenant_id == ServiceRecord.tenant_id)).where(
                ServiceRecord.tenant_id == tenant_id,
                ServiceDependencyRecord.depends_on_service_id == target,
            )
        rows = session.scalars(select(RequestFailureEventRecord).where(
            RequestFailureEventRecord.tenant_id == tenant_id,
            RequestFailureEventRecord.service_name.in_(callers),
            RequestFailureEventRecord.resource_name == service,
            RequestFailureEventRecord.failure_code.in_(CLIENT_FAILURE_CODES),
            RequestFailureEventRecord.occurred_at >= occurred_at - timedelta(seconds=30),
            RequestFailureEventRecord.occurred_at <= occurred_at + timedelta(seconds=30),
        ).order_by(RequestFailureEventRecord.occurred_at.desc(), RequestFailureEventRecord.id)
          .limit(4)).all()
        return [_view(row, "SOURCE_ASSERTED") for row in rows]


def database_candidates(settings: Settings, *, tenant_id: UUID, services: list[str],
                        occurred_at: datetime) -> list[RequestFailureEventView]:
    with session_for(settings.database_url) as session:
        known = select(ServiceRecord.slug).where(ServiceRecord.tenant_id == tenant_id,
                                                ServiceRecord.slug.in_(services))
        caller = aliased(RequestFailureEventRecord)
        # Prioritize request candidates over frequent probes. This join is a search hint;
        # only exact span ancestry can establish a caller relationship.
        has_caller = select(caller.id).where(
            caller.tenant_id == RequestFailureEventRecord.tenant_id,
            caller.trace_id == RequestFailureEventRecord.trace_id,
            caller.resource_name == RequestFailureEventRecord.service_name,
            caller.service_name != RequestFailureEventRecord.service_name,
            caller.failure_code.in_(["dependency_http_5xx", "upstream_http_5xx"]),
        ).exists()
        rows = session.scalars(select(RequestFailureEventRecord).where(
            RequestFailureEventRecord.tenant_id == tenant_id,
            RequestFailureEventRecord.service_name.in_(known),
            RequestFailureEventRecord.failure_code.in_(DATABASE_CODES),
            RequestFailureEventRecord.occurred_at >= occurred_at - timedelta(seconds=30),
            RequestFailureEventRecord.occurred_at <= occurred_at + timedelta(seconds=30),
        ).order_by(has_caller.desc(), RequestFailureEventRecord.occurred_at.desc(),
                   RequestFailureEventRecord.id)
          .limit(DATABASE_CANDIDATE_LIMIT)).all()
        return [_view(row, "SOURCE_ASSERTED") for row in rows]


def dependency_candidates(settings: Settings, *, tenant_id: UUID, service: str,
                          dependencies: list[str], occurred_at: datetime
                          ) -> list[RequestFailureEventView]:
    with session_for(settings.database_url) as session:
        rows = session.scalars(select(RequestFailureEventRecord).where(
            RequestFailureEventRecord.tenant_id == tenant_id,
            RequestFailureEventRecord.service_name == service,
            RequestFailureEventRecord.resource_name.in_(dependencies),
            RequestFailureEventRecord.failure_code.in_(CLIENT_FAILURE_CODES),
            RequestFailureEventRecord.occurred_at >= occurred_at - timedelta(seconds=30),
            RequestFailureEventRecord.occurred_at <= occurred_at + timedelta(seconds=30),
        ).order_by(RequestFailureEventRecord.occurred_at.desc(), RequestFailureEventRecord.id)
          .limit(4)).all()
        return [_view(row, "SOURCE_ASSERTED") for row in rows]
