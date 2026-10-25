"""Independent exact-fact checks. Observation verification never proves a root cause."""

import re
from datetime import datetime
from uuid import UUID

from .agent_contracts import AgentTrace, ClaimVerification, ObservationClaim
from .contracts import Evidence
from .request_context import CLIENT_FAILURE_CODES


def _time(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _scoped(item: Evidence, snapshot: dict, alert: dict) -> bool:
    scope = item.payload.get("investigation_scope", {})
    return (scope.get("tenant_id") == str(snapshot["tenant_id"])
            and scope.get("alert_id") == str(alert["id"])
            and _time(scope.get("occurred_at")) == alert["occurred_at"]
            and scope.get("target_service") in {
                alert["service"], *snapshot["dependencies"].get(alert["service"], [])})


def _fact(item: Evidence, claim: ObservationClaim, alert: dict) -> bool:
    p = item.payload
    if item.observed_at is None or item.observed_at.tzinfo is None:
        return False
    delta = (item.observed_at - alert["occurred_at"]).total_seconds()
    if p.get("service") != claim.service:
        return False
    if claim.kind == "RUNTIME_STATE":
        return (item.kind == "RUNTIME_STATE_OBSERVATION" and item.source_type == "METRIC"
                and item.tool_name == "prometheus.query_range" and -15 <= delta <= 0
                and p.get("state") in {"running", "paused", "exited", "restarting",
                                       "dead", "created"}
                and isinstance(p.get("sample_count"), int) and p["sample_count"] > 0
                and p.get("investigation_scope", {}).get("tool") == "runtime_state")
    try:
        event = str(UUID(p.get("event_id", "")))
    except (ValueError, TypeError, AttributeError):
        return False
    return (item.kind == "VERIFIED_DEPENDENCY_REQUEST" and item.source_type == "TRACE"
            and item.tool_name == "request_events_and_tempo" and abs(delta) <= 30
            and p.get("investigation_scope", {}).get("tool") in {
                "dependency_requests", "incoming_requests"}
            and p.get("provenance_status") == "VERIFIED_SPAN_FACT"
            and p.get("failure_code") in CLIENT_FAILURE_CODES
            and p.get("dependency") == claim.dependency
            and bool(re.fullmatch(r"[a-f0-9]{32}", str(p.get("trace_id", ""))))
            and bool(re.fullmatch(r"[a-f0-9]{16}", str(p.get("span_id", ""))))
            and item.source_ref == f"request-event:{event}:{p['span_id']}")


def verify_candidates(snapshot: dict, trace: AgentTrace, *, fresh: bool) -> list[ClaimVerification]:
    """Only completed backend tool records count, never planner text or supplied labels."""
    facts = [Evidence.model_validate(e) for step in trace.steps
             if step.phase == "TOOL" and step.status == "COMPLETED"
             for e in step.evidence if UUID(e["id"]) in step.evidence_ids]
    results = []
    for index, candidate in enumerate(trace.hypotheses, start=1):
        claim = candidate.claim
        result = ClaimVerification(candidate_index=index, claim=claim, status="INSUFFICIENT")
        results.append(result)
        if not fresh:
            result.reason_codes = ["SNAPSHOT_CHANGED"]
            continue
        if claim is None:
            result.status, result.reason_codes = "UNSUPPORTED_CLAIM", ["NO_STRUCTURED_CLAIM"]
            continue
        alert = next((a for a in snapshot["alerts"] if a["id"] == claim.alert_id), None)
        allowed = ({alert["service"], *snapshot["dependencies"].get(alert["service"], [])}
                   if alert else set())
        # An incoming adapter may observe a registered caller outside tool target scope.
        # This only permits verifying its returned fact; it grants no new query authority.
        incoming_observed = alert is not None and claim.kind == "REQUEST_FAILURE" and any(
            _scoped(e, snapshot, alert) and _fact(e, claim, alert)
            and e.payload.get("investigation_scope", {}).get("tool") == "incoming_requests"
            and claim.dependency == e.payload["investigation_scope"]["target_service"]
            for e in facts)
        if alert is None or (claim.service not in allowed and not incoming_observed):
            result.reason_codes = ["CLAIM_OUTSIDE_SCOPE"]
            continue
        if (claim.kind == "REQUEST_FAILURE"
                and (claim.failure_code not in CLIENT_FAILURE_CODES or not claim.dependency
                     or claim.state is not None)) or (
                claim.kind == "RUNTIME_STATE"
                and (claim.state is None or claim.failure_code is not None
                     or claim.dependency is not None)):
            result.status, result.reason_codes = "UNSUPPORTED_CLAIM", ["INVALID_CLAIM_SHAPE"]
            continue
        eligible = [e for e in facts if _scoped(e, snapshot, alert) and _fact(e, claim, alert)]
        attribute = "state" if claim.kind == "RUNTIME_STATE" else "failure_code"
        expected = claim.state if claim.kind == "RUNTIME_STATE" else claim.failure_code
        supporting = [e for e in eligible if e.payload.get(attribute) == expected
                      and e.polarity != "CONFLICT"]
        # A different request failure code can coexist; it is not automatically a contradiction.
        conflicting = [e for e in eligible if e.polarity == "CONFLICT" or (
            claim.kind == "RUNTIME_STATE" and e.payload.get(attribute) != expected)]
        cited = candidate.supporting_evidence_ids
        # Citations must be persisted backend records: completed tool results or the
        # snapshot's initial evidence. The exact claim additionally needs at least one
        # completed, claim-matching tool fact; context citations alone never verify.
        backend_ids = {e.id for e in facts} | {
            UUID(item["id"]) for item in trace.initial_evidence if "id" in item}
        if not cited or any(e not in backend_ids for e in cited):
            result.reason_codes.append("MISSING_COMPLETED_TOOL_CITATION")
        elif not {e for e in cited} & {e.id for e in supporting}:
            result.reason_codes.append("CITATION_DOES_NOT_MATCH_CLAIM")
        else:
            result.supporting_evidence_ids = cited
        # Model-declared conflicts are advisory bookkeeping: they are recorded only
        # when the referenced evidence exists, and can never block a candidate by
        # themselves — CONFLICT polarity is decided by the backend records above.
        declared = [e for e in candidate.conflicting_evidence_ids if e in backend_ids]
        result.conflicting_evidence_ids = sorted(
            {e.id for e in conflicting} | set(declared), key=str)
        if conflicting:
            result.status = "CONFLICTED"
            result.reason_codes.append("CONFLICT_REQUIRES_REVIEW")
        elif result.supporting_evidence_ids:
            result.status = "OBSERVATION_VERIFIED"
        else:
            result.reason_codes.append("NO_MATCHING_VERIFIED_OBSERVATION")
        memberships = [e for e in facts if _scoped(e, snapshot, alert)
                       and e.kind == "REQUEST_SIGNAL_MEMBERSHIP" and e.source_type == "ALERT"
                       and e.tool_name == "request_signal_membership"
                       and e.payload.get("investigation_scope", {}).get("tool")
                       == "request_membership"
                       and e.payload.get("alert_id") == str(alert["id"])
                       and e.payload.get("alert_binding_status") == "DIRECT_SOURCE_MEMBERSHIP"
                       and e.payload.get("service") == claim.service
                       and e.payload.get("resource") == claim.dependency
                       and e.payload.get("failure_code") == claim.failure_code]
        if claim.kind == "REQUEST_FAILURE" and len(memberships) == 1:
            p = memberships[0].payload
            members = p.get("member_event_ids", [])
            start, end = _time(p.get("window_start")), _time(p.get("window_end"))
            if (start is not None and end is not None and start == alert["occurred_at"]
                    and start < end and isinstance(members, list) and len(members) >= 2
                    and len(set(members)) == len(members) == p.get("event_count")
                    and memberships[0].source_ref == alert["source_event_id"]
                    and p.get("verification_basis") == "PERSISTED_VERIFIED_WINDOW_MEMBERS"):
                matched = {e.payload["event_id"] for e in supporting
                           if e.id in result.supporting_evidence_ids
                           and e.observed_at is not None
                           and start <= e.observed_at < end} & set(members)
                result.source_member_count = len(members)
                result.verified_member_count = len(matched)
                if matched:
                    result.alert_binding_status = "DIRECT_SOURCE_MEMBERSHIP"
                if len(matched) < len(members):
                    result.reason_codes.append("PARTIAL_SOURCE_COVERAGE")
        if result.alert_binding_status == "UNBOUND":
            result.reason_codes.append("ALERT_SOURCE_UNBOUND")
        result.reason_codes.extend(["HISTORICAL_OBSERVATION_ONLY", "ROOT_MECHANISM_NOT_VERIFIED"])
    return results
