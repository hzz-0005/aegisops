"""Trusted source membership, separate from root cause and current fault state."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aegisops.application.contracts import nonblank_reason
from aegisops.application.investigation.contracts import Evidence
from aegisops.application.request_derived_signals import (
    MAX_SIGNAL_MEMBERS,
    signal_identity,
    snapshot_digest,
)
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    AlertRecord,
    IncidentAlertRecord,
    IncidentTimelineRecord,
    OutboxEventRecord,
)
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalEventRecord as Member,
)
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalRecord as Signal,
)
from aegisops.infrastructure.database.models import (
    RequestFailureEventRecord as Event,
)
from aegisops.infrastructure.database.models import (
    RequestSignalWindowRecord as Window,
)

SOURCE = "request-detector"


class SignalPromotionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        return nonblank_reason(value)


def validate_members(signal: Signal, members: list[Member], events: list[Event]) -> None:
    ids = {member.request_event_id for member in members}
    if (not 2 <= len(members) <= MAX_SIGNAL_MEMBERS
            or len(ids) != len(members) or len(members) != signal.event_count
            or len(events) != len(members) or {event.id for event in events} != ids
            or signal.verification_status != "VERIFIED_SPAN_FACT"
            or signal.identity_key != signal_identity(signal.service_name, signal.failure_code,
                                                     signal.resource_name)):
        raise ValueError("signal membership integrity failed")
    for member in members:
        if (member.tenant_id != signal.tenant_id or member.signal_id != signal.id
                or member.verification_status != "VERIFIED_SPAN_FACT"
                or member.verification_recorded_at is None):
            raise ValueError("signal member verification missing")
    for event in events:
        if (event.tenant_id != signal.tenant_id or event.service_name != signal.service_name
                or event.failure_code != signal.failure_code
                or event.resource_name != signal.resource_name
                or not signal.window_start <= event.occurred_at < signal.window_end):
            raise ValueError("signal member scope mismatch")
    if (signal.distinct_trace_count < 2
            or len({event.trace_id for event in events}) != signal.distinct_trace_count):
        raise ValueError("signal trace count mismatch")


def _members(session, signal: Signal) -> tuple[list[Member], list[Event]]:
    members = list(session.scalars(select(Member).where(
        Member.tenant_id == signal.tenant_id, Member.signal_id == signal.id,
    ).limit(MAX_SIGNAL_MEMBERS + 1)).all())
    events = list(session.scalars(select(Event).where(
        Event.tenant_id == signal.tenant_id,
        Event.id.in_([member.request_event_id for member in members]),
    )).all())
    validate_members(signal, members, events)
    window = session.scalar(select(Window).where(
        Window.tenant_id == signal.tenant_id, Window.detector_version == signal.detector_version,
        Window.window_start == signal.window_start,
    ))
    if (window is None or window.window_end != signal.window_end
            or len(window.snapshot_event_ids) != window.event_count
            or snapshot_digest([UUID(value) for value in window.snapshot_event_ids])
            != window.snapshot_digest
            or not {str(event.id) for event in events}.issubset(set(window.snapshot_event_ids))):
        raise ValueError("finalized source window missing or inconsistent")
    return members, events


def promote_signal(settings: Settings, signal_id: UUID, *, reason: str,
                   operator_id: str) -> UUID:
    """Create one observed-window alert and its outbox event atomically, idempotently."""
    require_dev_runtime(settings)
    reason = nonblank_reason(reason)
    if len(reason) > 300:
        raise ValueError("reason exceeds 300 characters")
    with session_for(settings.database_url) as session, session.begin():
        signal = session.scalar(select(Signal).where(
            Signal.tenant_id == settings.dev_tenant_id, Signal.id == signal_id,
        ).with_for_update())
        if signal is None:
            raise LookupError("signal not found")
        _members(session, signal)
        external_id = f"request-signal:{signal.id}"
        alert_id = uuid4()
        inserted = session.execute(insert(AlertRecord).values(
            id=alert_id, tenant_id=signal.tenant_id, source=SOURCE, external_id=external_id,
            source_event_id=external_id, schema_version=1, fingerprint=signal.identity_key,
            service_name=signal.service_name, severity="P3", status="OBSERVED",
            starts_at=signal.window_start, occurred_at=signal.window_start,
            received_at=datetime.now(UTC), payload={
                "alertname": "VerifiedRequestFailureWindow", "service": signal.service_name,
                "resource": signal.resource_name, "change_id": None,
                "signal_type": signal.failure_code,
                "summary": "封闭窗口内发生多次已核验请求失败，当前故障状态尚未确定。",
                "labels": {"signal_role": "symptom"}, "request_signal_id": str(signal.id),
                "operator_id": operator_id, "promotion_reason": reason,
            },
        ).on_conflict_do_nothing(constraint="uq_alerts_external")
          .returning(AlertRecord.id)).scalar_one_or_none()
        if inserted is not None:
            session.add(OutboxEventRecord(id=uuid4(), tenant_id=signal.tenant_id,
                event_type="alert.received.v1", payload={"alert_id": str(alert_id)}))
            return alert_id
        return session.execute(select(AlertRecord.id).where(
            AlertRecord.tenant_id == signal.tenant_id, AlertRecord.source == SOURCE,
            AlertRecord.external_id == external_id,
        )).scalar_one()


def membership_evidence(settings: Settings, *, tenant_id: UUID, alert_id: UUID) -> list[Evidence]:
    with session_for(settings.database_url) as session:
        alert = session.scalar(select(AlertRecord).where(
            AlertRecord.tenant_id == tenant_id, AlertRecord.id == alert_id,
        ))
        if alert is None or alert.source != SOURCE:
            return []  # Prometheus labels and operator selection are never direct provenance.
        signal_id = UUID(alert.payload["request_signal_id"])
        if alert.source_event_id != f"request-signal:{signal_id}":
            raise ValueError("request alert source identity mismatch")
        signal = session.scalar(select(Signal).where(
            Signal.tenant_id == tenant_id, Signal.id == signal_id,
        ))
        if signal is None:
            raise ValueError("request alert source missing")
        members, _events = _members(session, signal)
        if (alert.external_id != alert.source_event_id
                or alert.fingerprint != signal.identity_key or alert.schema_version != 1
                or alert.occurred_at != signal.window_start
                or alert.service_name != signal.service_name
                or alert.payload.get("resource") != signal.resource_name
                or alert.payload.get("signal_type") != signal.failure_code):
            raise ValueError("request alert source scope mismatch")
        return [Evidence(source_type="ALERT", source_ref=f"request-signal:{signal.id}",
            kind="REQUEST_SIGNAL_MEMBERSHIP", subject="unclassified", polarity="CONTEXT",
            observed_at=signal.window_end, collected_at=datetime.now(UTC),
            tool_name="request_signal_membership", tool_version="membership-v1", payload={
                "alert_id": str(alert_id), "signal_id": str(signal.id),
                "member_event_ids": sorted(str(member.request_event_id) for member in members),
                "service": signal.service_name, "failure_code": signal.failure_code,
                "resource": signal.resource_name, "event_count": signal.event_count,
                "window_start": signal.window_start.isoformat(),
                "window_end": signal.window_end.isoformat(),
                "alert_binding_status": "DIRECT_SOURCE_MEMBERSHIP",
                "root_cause_status": "UNDETERMINED", "current_fault_status": "UNKNOWN",
                "verification_basis": "PERSISTED_VERIFIED_WINDOW_MEMBERS",
            })]


class AlertResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        return nonblank_reason(value)


def resolve_signal_alert(settings: Settings, tenant_id: UUID, alert_id: UUID, *,
                         reason: str, operator_id: str) -> dict:
    """Operator-verified close of a promoted symptom window.

    Promoted alerts have no Alertmanager lifecycle, so without this audited
    action their incidents can never satisfy the recovery assessment. Only the
    request-detector source may be resolved here; Prometheus alerts keep their
    own lifecycle. Idempotent: an already-resolved alert returns unchanged.
    """
    require_dev_runtime(settings)
    reason = nonblank_reason(reason)
    with session_for(settings.database_url) as session, session.begin():
        alert = session.scalar(select(AlertRecord).where(
            AlertRecord.tenant_id == tenant_id, AlertRecord.id == alert_id,
        ).with_for_update())
        if alert is None:
            raise LookupError("alert not found")
        if alert.source != SOURCE:
            raise ValueError("only promoted request-signal alerts resolve here")
        if alert.status == "RESOLVED":
            return {
                "alert_id": str(alert.id), "status": alert.status,
                "resolved_at": alert.payload.get("resolved_at"),
                "already_resolved": True,
            }
        if alert.status != "OBSERVED":
            raise ValueError(f"alert status {alert.status} cannot be resolved here")
        alert.status = "RESOLVED"
        alert.ends_at = datetime.now(UTC)
        alert.version += 1
        alert.updated_at = datetime.now(UTC)
        alert.payload = {**alert.payload, "resolved_at": alert.ends_at.isoformat(),
                         "resolved_by": operator_id, "resolution_reason": reason}
        linked = session.scalars(select(IncidentAlertRecord.incident_id).where(
            IncidentAlertRecord.tenant_id == tenant_id,
            IncidentAlertRecord.alert_id == alert.id,
        )).all()
        for incident_id in sorted(set(linked)):
            session.add(IncidentTimelineRecord(
                tenant_id=tenant_id, incident_id=incident_id,
                event_type="INCIDENT_ALERT_RESOLVED",
                summary="操作员核实症状窗口已结束，解除已提升的请求症状告警",
                details={"alert_id": str(alert.id), "operator_id": operator_id,
                         "reason": reason, "recorded_at": datetime.now(UTC).isoformat()},
            ))
        return {"alert_id": str(alert.id), "status": alert.status,
                "resolved_at": alert.ends_at.isoformat(), "already_resolved": False}
