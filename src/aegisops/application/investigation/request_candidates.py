"""Source anchored request propagation observations; never a root or merge decision."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel
from sqlalchemy import String, cast, select

from aegisops.application.request_failure_links import (
    RequestFailureLink,
    RequestLinkUnverifiable,
    links_for_events,
    trace_events,
)
from aegisops.application.request_signal_alerts import SOURCE, membership_evidence
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    AlertRecord,
    IncidentAlertRecord,
    IncidentRecord,
    RequestDerivedSignalEventRecord,
    RequestDerivedSignalRecord,
    RequestFailureEventRecord,
)

RULE_VERSION = "source-request-candidates-v1"
MAX_ANCHORS, MAX_PEERS, MAX_TRACES = 8, 12, 16


@dataclass(frozen=True)
class Anchor:
    tenant_id: UUID
    incident_id: UUID
    incident_version: int
    alert_id: UUID
    signal_id: UUID
    members: frozenset[UUID]
    service: str = ""
    failure_code: str = ""


class RequestCandidate(BaseModel):
    other_incident_id: UUID
    other_incident_version: int
    source_alert_id: UUID
    other_alert_id: UUID
    source_signal_id: UUID
    other_signal_id: UUID
    source_service: str
    source_failure_code: str
    other_service: str
    other_failure_code: str
    direction: Literal["DOWNSTREAM", "UPSTREAM"]
    source_member_count: int
    source_covered_event_ids: list[UUID]
    other_member_count: int
    other_covered_event_ids: list[UUID]
    distinct_trace_count: int
    supporting_features: list[str]
    conflicting_features: list[str]
    decision: str
    proofs: list[RequestFailureLink]
    root_cause_status: Literal["UNDETERMINED"] = "UNDETERMINED"
    current_fault_status: Literal["UNKNOWN"] = "UNKNOWN"
    automatic_merge_allowed: Literal[False] = False


class RequestCandidateObservation(BaseModel):
    incident_id: UUID
    source_incident_version: int
    rule_version: str = RULE_VERSION
    observed_at: datetime
    status: Literal["NO_DIRECT_SOURCE", "OBSERVED", "INCOMPLETE", "STALE"]
    source_alert_count: int
    inspected_source_alert_count: int
    selected_trace_count: int = 0
    verified_trace_count: int = 0
    limitations: list[str]
    candidates: list[RequestCandidate]
    interpretation: str = (
        "仅证明来源成员之间的请求失败传播路径；覆盖不足或多个来源组不能整体并案。"
        "根因与当前状态仍未知，任何候选均不授权自动合并或修复。"
    )


def summarize_candidates(source: list[Anchor], peers: list[Anchor],
                         links: list[RequestFailureLink]) -> list[RequestCandidate]:
    """Membership on both ends is mandatory; broad-window trace overlap has no vote."""
    output = []
    for anchor in source:
        for peer in peers:
            if peer.tenant_id != anchor.tenant_id or peer.incident_id == anchor.incident_id:
                continue
            directions: tuple[Literal["DOWNSTREAM", "UPSTREAM"], ...] = (
                "DOWNSTREAM", "UPSTREAM")
            for direction in directions:
                matched = {}
                for link in links:
                    source_id, other_id = ((link.upstream_event_id, link.downstream_event_id)
                        if direction == "DOWNSTREAM" else
                        (link.downstream_event_id, link.upstream_event_id))
                    if source_id in anchor.members and other_id in peer.members:
                        matched[(link.trace_id, source_id, other_id)] = link
                if not matched:
                    continue
                proofs = sorted(matched.values(), key=lambda p: (
                    p.trace_id, str(p.upstream_event_id), str(p.downstream_event_id)))
                source_ids = {p.upstream_event_id if direction == "DOWNSTREAM" else
                              p.downstream_event_id for p in proofs}
                other_ids = {p.downstream_event_id if direction == "DOWNSTREAM" else
                             p.upstream_event_id for p in proofs}
                traces = len({p.trace_id for p in proofs})
                conflicts = []
                if source_ids != anchor.members:
                    conflicts.append("SOURCE_MEMBERS_UNCOVERED")
                if other_ids != peer.members:
                    conflicts.append("OTHER_MEMBERS_UNCOVERED")
                if traces < 2:
                    conflicts.append("SINGLE_TRACE_ONLY")
                output.append(RequestCandidate(
                    other_incident_id=peer.incident_id,
                    other_incident_version=peer.incident_version,
                    source_alert_id=anchor.alert_id, other_alert_id=peer.alert_id,
                    source_signal_id=anchor.signal_id, other_signal_id=peer.signal_id,
                    source_service=anchor.service, source_failure_code=anchor.failure_code,
                    other_service=peer.service, other_failure_code=peer.failure_code,
                    direction=direction, source_member_count=len(anchor.members),
                    source_covered_event_ids=sorted(source_ids, key=str),
                    other_member_count=len(peer.members),
                    other_covered_event_ids=sorted(other_ids, key=str),
                    distinct_trace_count=traces,
                    supporting_features=["DIRECT_SOURCE_MEMBERSHIP_BOTH_ENDS",
                                         "VERIFIED_REQUEST_ANCESTRY"],
                    conflicting_features=conflicts,
                    decision="REVIEW_PARTIAL_PATH" if conflicts else "REVIEW_REQUEST_PATH",
                    proofs=proofs,
                ))
    for candidate in output:
        # Incoming and outgoing neighbours of one chain are not conflicting directions.
        alternatives = {(p.other_incident_id, p.other_alert_id) for p in output
            if p.source_alert_id == candidate.source_alert_id
            and p.direction == candidate.direction}
        if len(alternatives) > 1:
            candidate.conflicting_features.append("MULTIPLE_PEER_SOURCE_GROUPS")
            candidate.decision = "REVIEW_AMBIGUOUS_COHORT"
    return sorted(output, key=lambda p: (
        str(p.source_alert_id), p.direction, str(p.other_incident_id), str(p.other_alert_id)))


def _snapshot(settings: Settings, tenant_id: UUID, incident_id: UUID
              ) -> tuple[int, list[UUID]]:
    with session_for(settings.database_url) as session:
        rows = session.execute(select(IncidentRecord.version, IncidentAlertRecord.alert_id)
            .outerjoin(IncidentAlertRecord,
                (IncidentAlertRecord.tenant_id == IncidentRecord.tenant_id)
                & (IncidentAlertRecord.incident_id == IncidentRecord.id))
            .where(IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id)
            .order_by(IncidentAlertRecord.alert_id)).all()
        if not rows:
            raise LookupError("incident not found")
        return rows[0][0], [ident for _, ident in rows if ident is not None]


def _anchor(settings: Settings, tenant_id: UUID, incident_id: UUID,
            version: int, alert_id: UUID) -> Anchor:
    evidence = membership_evidence(settings, tenant_id=tenant_id, alert_id=alert_id)
    if len(evidence) != 1:
        raise ValueError("direct source required")
    payload = evidence[0].payload
    return Anchor(tenant_id, incident_id, version, alert_id, UUID(payload["signal_id"]),
                  frozenset(UUID(value) for value in payload["member_event_ids"]),
                  payload["service"], payload["failure_code"])


def request_candidates(settings: Settings, tenant_id: UUID,
                       incident_id: UUID) -> RequestCandidateObservation:
    snapshot = _snapshot(settings, tenant_id, incident_id)
    version, ids = snapshot
    limitations: list[str] = []
    with session_for(settings.database_url) as session:
        source_ids = list(session.scalars(select(AlertRecord.id).where(
            AlertRecord.tenant_id == tenant_id, AlertRecord.id.in_(ids),
            AlertRecord.source == SOURCE,
        ).order_by(AlertRecord.id)).all())
    source = []
    for ident in source_ids[:MAX_ANCHORS]:
        try:
            source.append(_anchor(settings, tenant_id, incident_id, version, ident))
        except (ValueError, KeyError, TypeError):
            limitations.append(f"来源成员核验失败：{ident}")
    if len(source_ids) > MAX_ANCHORS:
        limitations.append("来源告警超出8条核验预算")
    result = RequestCandidateObservation(
        incident_id=incident_id, source_incident_version=version, observed_at=datetime.now(UTC),
        status="NO_DIRECT_SOURCE" if not source_ids else "INCOMPLETE",
        source_alert_count=len(source_ids), inspected_source_alert_count=len(source),
        limitations=limitations, candidates=[],
    )
    if not source:
        return result
    members = set().union(*(a.members for a in source))
    with session_for(settings.database_url) as session:
        traces = list(session.scalars(select(RequestFailureEventRecord.trace_id).where(
            RequestFailureEventRecord.tenant_id == tenant_id,
            RequestFailureEventRecord.id.in_(members),
        ).distinct().order_by(RequestFailureEventRecord.trace_id)).all())
        selected = traces[:MAX_TRACES]
        peer_rows = session.execute(select(IncidentAlertRecord.incident_id,
            IncidentRecord.version, AlertRecord.id).join(AlertRecord,
                (AlertRecord.id == IncidentAlertRecord.alert_id)
                & (AlertRecord.tenant_id == IncidentAlertRecord.tenant_id))
            .join(IncidentRecord, (IncidentRecord.id == IncidentAlertRecord.incident_id)
                & (IncidentRecord.tenant_id == IncidentAlertRecord.tenant_id))
            .join(RequestDerivedSignalRecord,
                (AlertRecord.external_id == "request-signal:" + cast(
                    RequestDerivedSignalRecord.id, String))
                & (RequestDerivedSignalRecord.tenant_id == AlertRecord.tenant_id))
            .join(RequestDerivedSignalEventRecord,
                (RequestDerivedSignalEventRecord.signal_id == RequestDerivedSignalRecord.id)
                & (RequestDerivedSignalEventRecord.tenant_id == AlertRecord.tenant_id))
            .join(RequestFailureEventRecord,
                (RequestFailureEventRecord.id == RequestDerivedSignalEventRecord.request_event_id)
                & (RequestFailureEventRecord.tenant_id == AlertRecord.tenant_id))
            .where(AlertRecord.tenant_id == tenant_id, AlertRecord.source == SOURCE,
                IncidentAlertRecord.incident_id != incident_id,
                RequestFailureEventRecord.trace_id.in_(selected))
            .distinct().order_by(IncidentAlertRecord.incident_id, AlertRecord.id)
            .limit(MAX_PEERS + 1)).all()
    if len(traces) > MAX_TRACES:
        limitations.append("追踪超出16条核验预算，覆盖不完整")
    if len(peer_rows) > MAX_PEERS:
        limitations.append("其他来源组超出12条核验预算，候选不完整")
    peers = []
    peer_snapshots = {}
    for other, other_version, alert in peer_rows[:MAX_PEERS]:
        try:
            peer_snapshots[other] = _snapshot(settings, tenant_id, other)
            if (peer_snapshots[other][0] != other_version
                    or alert not in peer_snapshots[other][1]):
                limitations.append(f"其他事故核验期间已变化：{other}")
                continue
            peers.append(_anchor(settings, tenant_id, other, other_version, alert))
        except (LookupError, ValueError, KeyError, TypeError):
            limitations.append(f"其他来源成员核验失败：{alert}")

    def verify(trace_id: str) -> tuple[list[RequestFailureLink], str | None]:
        try:
            events = trace_events(settings, tenant_id=tenant_id, trace_id=trace_id)
            with httpx.Client(timeout=1.5, trust_env=False) as client:
                response = client.get(f"{settings.tempo_url.rstrip('/')}/api/traces/{trace_id}")
                response.raise_for_status()
                batches = response.json()["batches"]
            return links_for_events(events, batches), None
        except (httpx.HTTPError, RequestLinkUnverifiable, ValueError, KeyError, TypeError):
            return [], f"追踪核验不可用：{trace_id}"

    result.selected_trace_count = len(selected)
    links = []
    if peers:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for proofs, error in pool.map(verify, selected):
                if error:
                    limitations.append(error)
                else:
                    result.verified_trace_count += 1
                    links.extend(proofs)
    result.candidates = summarize_candidates(source, peers, links)
    result.status = "INCOMPLETE" if limitations else "OBSERVED"
    try:
        stale = _snapshot(settings, tenant_id, incident_id) != snapshot or any(
            _snapshot(settings, tenant_id, other) != saved
            for other, saved in peer_snapshots.items())
    except LookupError:
        stale = True
    if stale:
        result.status = "STALE"
        limitations.append("事故版本或成员在核验期间变化，请重新读取")
    if result.status in {"INCOMPLETE", "STALE"}:
        for candidate in result.candidates:
            candidate.conflicting_features.append(result.status + "_OBSERVATION")
            candidate.decision = "REVIEW_INCOMPLETE_OBSERVATION"
    result.limitations = limitations
    return result
