"""Offline propagation partition, with no cross-request root or incident inference."""

from collections import Counter, defaultdict
from typing import TypedDict
from uuid import UUID

from aegisops.application.request_events import SPAN_ID, TRACE_ID, RequestFailureEventView
from aegisops.application.request_failure_links import MAX_PATH, RequestFailureLink

from .request_candidates import Anchor

RULE_VERSION = "request-propagation-shadow-v1"
MAX_MEMBERS = 1000
MAX_PROOFS = 2048
MAX_ANCHORS = 64


class ShadowGroup(TypedDict):
    members: list[str]
    status: str
    trace_id: str | None


def shadow_partition(*, tenant_id: UUID, anchors: list[Anchor],
                     events: list[RequestFailureEventView],
                     proofs: list[RequestFailureLink]) -> dict:
    """Consume previously verified ancestry; same trace and source cohort never join events."""
    if (len(anchors) > MAX_ANCHORS or len(proofs) > MAX_PROOFS or len(events) > MAX_MEMBERS
            or len({event.id for event in events}) != len(events)
            or len({anchor.alert_id for anchor in anchors}) != len(anchors)
            or any(anchor.tenant_id != tenant_id or not anchor.members for anchor in anchors)):
        raise ValueError("shadow input scope, identity or budget invalid")
    members = set().union(*(anchor.members for anchor in anchors)) if anchors else set()
    if len(members) > MAX_MEMBERS:
        raise ValueError("shadow member budget exceeded")
    facts = {event.id: event for event in events}
    memberships = Counter(member for anchor in anchors for member in anchor.members)
    if not set(facts) <= members:
        raise ValueError("event facts must belong to source membership")
    adjacency: dict[UUID, set[UUID]] = defaultdict(set)
    outgoing: dict[UUID, set[UUID]] = defaultdict(set)
    incoming: dict[UUID, set[UUID]] = defaultdict(set)
    invalid: set[UUID] = set()
    accepted: dict[tuple[UUID, UUID], RequestFailureLink] = {}
    rejected = 0
    distinct_proofs = {proof.model_dump_json(): proof for proof in proofs}
    for key in sorted(distinct_proofs):
        proof = distinct_proofs[key]
        a, b = proof.upstream_event_id, proof.downstream_event_id
        up, down = facts.get(a), facts.get(b)
        valid = (up is not None and down is not None and a != b
            and TRACE_ID.fullmatch(proof.trace_id) is not None
            and up.trace_id == down.trace_id == proof.trace_id
            and up.service == proof.upstream_service
            and down.service == proof.downstream_service
            and up.service != down.service and up.resource == down.service
            and up.failure_code in {"upstream_http_5xx", "dependency_http_5xx"}
            and down.failure_code == proof.downstream_failure_code
            and down.resource == proof.downstream_resource
            and up.occurred_at.utcoffset() is not None
            and down.occurred_at.utcoffset() is not None
            and down.occurred_at <= up.occurred_at == proof.observed_at
            and 2 <= len(proof.span_path) <= MAX_PATH
            and len(set(proof.span_path)) == len(proof.span_path)
            and all(SPAN_ID.fullmatch(span) for span in proof.span_path)
            and proof.span_path[0] == up.span_id and proof.span_path[-1] == down.span_id
            and proof.relationship_status == "VERIFIED_REQUEST_ANCESTRY")
        if not valid:
            rejected += 1
            invalid.update({a, b} & members)
            continue
        previous = accepted.get((a, b))
        if previous and previous.span_path != proof.span_path:
            invalid.update({a, b})
            rejected += 1
        else:
            accepted[(a, b)] = proof
        adjacency[a].add(b)
        adjacency[b].add(a)
        outgoing[a].add(b)
        incoming[b].add(a)
    # A branch/convergence or identity conflict quarantines its complete connected component.
    invalid.update(node for node in members
                   if len(outgoing[node]) > 1 or len(incoming[node]) > 1)
    visited: set[UUID] = set()
    groups: list[ShadowGroup] = []
    joined: set[UUID] = set()
    ambiguous: set[UUID] = set()
    missing = members - facts.keys()
    for member in sorted(members, key=str):
        if member in visited:
            continue
        component, pending = set(), [member]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(adjacency[current] - component)
        visited.update(component)
        # For a directed path, a component has one start and exactly n-1 edges.
        edge_count = sum(len(outgoing[node]) for node in component)
        starts = sum(not incoming[node] for node in component)
        bad = bool(component & invalid) or (len(component) > 1 and (
            edge_count != len(component) - 1 or starts != 1))
        if bad:
            ambiguous.update(component)
            for node in sorted(component, key=str):
                groups.append({"members": [str(node)], "status": "AMBIGUOUS",
                               "trace_id": facts[node].trace_id if node in facts else None})
        elif len(component) > 1:
            joined.update(component)
            groups.append({"members": sorted(map(str, component)),
                           "status": "VERIFIED_PROPAGATION_GROUP",
                           "trace_id": facts[member].trace_id})
        else:
            groups.append({"members": [str(member)],
                           "status": "FACT_UNAVAILABLE" if member in missing else "UNLINKED",
                           "trace_id": facts[member].trace_id if member in facts else None})
    groups.sort(key=lambda group: tuple(group["members"]))
    projected = []
    for anchor in sorted(anchors, key=lambda item: str(item.alert_id)):
        anchor_ids = set(map(str, anchor.members))
        touched = [group for group in groups if group["status"] == "VERIFIED_PROPAGATION_GROUP"
                   and anchor_ids.intersection(group["members"])]
        covered = anchor.members & joined
        projected.append({"alert_id": str(anchor.alert_id), "signal_id": str(anchor.signal_id),
            "service": anchor.service, "failure_code": anchor.failure_code,
            "member_count": len(anchor.members), "covered_member_count": len(covered),
            "request_group_count": len(touched),
            "unresolved_member_ids": sorted(map(str, anchor.members - joined)),
            "interpretation": "多个请求组不等于多个根因；未连接成员也不等于独立故障。"})
    return {"rule_version": RULE_VERSION, "scope": "REQUEST_PROPAGATION_ONLY",
        "member_count": len(members), "joined_member_count": len(joined),
        "unresolved_member_count": len(members - joined),
        "ambiguous_member_count": len(ambiguous), "missing_fact_count": len(missing),
        "overlapping_source_member_count": sum(count > 1 for count in memberships.values()),
        "accepted_distinct_proof_count": len(accepted), "rejected_proof_count": rejected,
        "joined_proof_count": sum(a in joined and b in joined for a, b in accepted),
        "groups": groups, "source_cohorts": projected,
        "root_cause_status": "UNDETERMINED", "automatic_merge_allowed": False,
        "interpretation": "只划分已证实的单次请求失败传播；不跨追踪合并，不修改事故。"
                          "未判定成员保留单例，占位单例不能解释为已证明的独立根因。"}
