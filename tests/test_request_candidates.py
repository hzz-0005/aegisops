"""Cohort coverage and propagation must not become common-root attribution."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from aegisops.application.investigation import request_candidates as module
from aegisops.application.investigation.request_candidates import Anchor, summarize_candidates
from aegisops.application.request_failure_links import RequestFailureLink
from aegisops.config import Settings


def anchor(tenant, events, incident=None):
    return Anchor(tenant, incident or uuid4(), 3, uuid4(), uuid4(), frozenset(events))


def link(up, down, trace):
    return RequestFailureLink(trace_id=trace, upstream_event_id=up, downstream_event_id=down,
        upstream_service="gateway", downstream_service="order-service",
        downstream_failure_code="dependency_http_5xx", downstream_resource="inventory-service",
        span_path=["1" * 16, "2" * 16], observed_at=datetime.now(UTC))


def test_complete_path_is_review_only_and_duplicate_proofs_do_not_inflate_coverage():
    tenant = uuid4()
    up, down = [uuid4(), uuid4()], [uuid4(), uuid4()]
    source, peer = anchor(tenant, up), anchor(tenant, down)
    proofs = [link(up[0], down[0], "a" * 32), link(up[1], down[1], "b" * 32)]
    result = summarize_candidates([source], [peer], proofs + proofs)
    assert len(result) == 1 and result[0].distinct_trace_count == 2
    assert len(result[0].proofs) == 2 and result[0].decision == "REVIEW_REQUEST_PATH"
    assert not result[0].automatic_merge_allowed
    assert result[0].root_cause_status == "UNDETERMINED"
    assert result[0].current_fault_status == "UNKNOWN"


def test_same_trace_or_resource_without_verified_event_pair_cannot_form_candidate():
    tenant = uuid4()
    source, peer = anchor(tenant, [uuid4(), uuid4()]), anchor(tenant, [uuid4(), uuid4()])
    assert summarize_candidates([source], [peer], []) == []
    unrelated = link(uuid4(), uuid4(), "a" * 32)
    assert summarize_candidates([source], [peer], [unrelated]) == []


def test_gateway_mixed_roots_exposes_partial_alternative_groups_without_merging():
    tenant = uuid4()
    gateway = [uuid4() for _ in range(4)]
    exit_events, lock_events = [uuid4(), uuid4()], [uuid4(), uuid4()]
    source = anchor(tenant, gateway)
    peers = [anchor(tenant, exit_events), anchor(tenant, lock_events)]
    proofs = [link(gateway[i], (exit_events + lock_events)[i], f"{i + 1:032x}")
              for i in range(4)]
    result = summarize_candidates([source], peers, proofs)
    assert len(result) == 2
    for candidate in result:
        assert len(candidate.source_covered_event_ids) == 2 and candidate.source_member_count == 4
        assert "SOURCE_MEMBERS_UNCOVERED" in candidate.conflicting_features
        assert "MULTIPLE_PEER_SOURCE_GROUPS" in candidate.conflicting_features
        assert candidate.decision == "REVIEW_AMBIGUOUS_COHORT"
        assert not candidate.automatic_merge_allowed


def test_cross_tenant_members_even_with_matching_ids_are_excluded():
    up, down = uuid4(), uuid4()
    assert summarize_candidates([anchor(uuid4(), [up])], [anchor(uuid4(), [down])],
                                [link(up, down, "a" * 32)]) == []


def test_one_covered_request_does_not_cover_whole_source_window():
    tenant = uuid4()
    up, down = uuid4(), uuid4()
    result = summarize_candidates([anchor(tenant, [up, uuid4()])],
        [anchor(tenant, [down, uuid4()])], [link(up, down, "a" * 32)])
    assert set(result[0].conflicting_features) == {
        "SOURCE_MEMBERS_UNCOVERED", "OTHER_MEMBERS_UNCOVERED", "SINGLE_TRACE_ONLY"}


def test_incoming_and_outgoing_of_one_chain_are_not_alternative_downstream_groups():
    tenant = uuid4()
    first, middle, last = uuid4(), uuid4(), uuid4()
    result = summarize_candidates([anchor(tenant, [middle])],
        [anchor(tenant, [first]), anchor(tenant, [last])],
        [link(first, middle, "a" * 32), link(middle, last, "a" * 32)])
    assert {candidate.direction for candidate in result} == {"UPSTREAM", "DOWNSTREAM"}
    assert all("MULTIPLE_PEER_SOURCE_GROUPS" not in p.conflicting_features for p in result)


def test_reordered_sources_and_proofs_produce_same_observations():
    tenant = uuid4()
    up, down = [uuid4(), uuid4()], [uuid4(), uuid4()]
    source, peer = anchor(tenant, up), anchor(tenant, down)
    proofs = [link(up[0], down[0], "a" * 32), link(up[1], down[1], "b" * 32)]
    assert summarize_candidates([source], [peer], proofs) == summarize_candidates(
        [source], [peer], list(reversed(proofs)))


def test_same_incident_is_not_presented_as_external_candidate():
    tenant, incident, up, down = uuid4(), uuid4(), uuid4(), uuid4()
    assert summarize_candidates([anchor(tenant, [up], incident)],
        [anchor(tenant, [down], incident)], [link(up, down, "a" * 32)]) == []


@pytest.mark.parametrize("failure", ["source_changed", "peer_changed", "trace_unavailable",
                                    "trace_budget", "corrupt_membership", "peer_deleted",
                                    "peer_member_removed"])
def test_live_observation_failures_are_visible_and_never_eligible_for_merge(monkeypatch, failure):
    tenant = uuid4()
    count = 17 if failure == "trace_budget" else 2
    up, down = [uuid4() for _ in range(count)], [uuid4() for _ in range(count)]
    source, peer = anchor(tenant, up), anchor(tenant, down)
    traces = [f"{i + 1:032x}" for i in range(count)]
    proofs = [link(a, b, trace) for a, b, trace in zip(up, down, traces, strict=True)]
    reads = {source.incident_id: 0, peer.incident_id: 0}

    def snapshot(settings, scoped_tenant, incident):
        assert scoped_tenant == tenant
        selected = source if incident == source.incident_id else peer
        reads[incident] += 1
        if failure == "peer_deleted" and selected == peer and reads[incident] > 1:
            raise LookupError("peer deleted during verification")
        changed = ((failure == "source_changed" and selected == source)
                   or (failure == "peer_changed" and selected == peer))
        members = [] if failure == "peer_member_removed" and selected == peer else [
            selected.alert_id]
        return selected.incident_version + int(changed and reads[incident] > 1), members

    def load_anchor(settings, scoped_tenant, incident, version, ident):
        assert scoped_tenant == tenant
        if failure == "corrupt_membership":
            raise ValueError("invalid persisted membership")
        return source if ident == source.alert_id else peer

    session = MagicMock()
    session.scalars.return_value.all.side_effect = [[source.alert_id], traces]
    session.execute.return_value.all.return_value = [
        (peer.incident_id, peer.incident_version, peer.alert_id)]
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(module, "session_for", lambda _: context)
    monkeypatch.setattr(module, "_snapshot", snapshot)
    monkeypatch.setattr(module, "_anchor", load_anchor)
    monkeypatch.setattr(module, "trace_events", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "links_for_events", lambda events, batches:
                        [p for p in proofs if p.trace_id == batches[0]["trace"]])

    client = MagicMock()
    client.__enter__.return_value = client

    def fetch(url):
        if failure == "trace_unavailable":
            raise httpx.ReadTimeout("unavailable")
        response = MagicMock()
        response.json.return_value = {"batches": [{"trace": url.rsplit("/", 1)[-1]}]}
        return response

    client.get.side_effect = fetch
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client)
    result = module.request_candidates(Settings(), tenant, source.incident_id)
    expected = ("STALE" if failure in {"source_changed", "peer_changed", "peer_deleted"}
                else "INCOMPLETE")
    assert result.status == expected and result.limitations
    assert all(not c.automatic_merge_allowed for c in result.candidates)
    assert all(c.decision == "REVIEW_INCOMPLETE_OBSERVATION" for c in result.candidates)
    if failure in {"trace_unavailable", "corrupt_membership", "peer_member_removed"}:
        assert not result.candidates and result.verified_trace_count == 0
    if failure == "trace_budget":
        assert result.selected_trace_count == result.verified_trace_count == 16
        assert result.candidates[0].source_member_count == 17
        assert len(result.candidates[0].source_covered_event_ids) == 16


def test_unanchored_prometheus_incident_does_not_fetch_or_claim_request_candidates(monkeypatch):
    tenant, incident = uuid4(), uuid4()
    monkeypatch.setattr(module, "_snapshot", lambda *args: (4, [uuid4()]))
    session = MagicMock()
    session.scalars.return_value.all.return_value = []
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(module, "session_for", lambda _: context)
    fetch = MagicMock(side_effect=AssertionError("unanchored trace query is forbidden"))
    monkeypatch.setattr(module.httpx, "Client", fetch)
    result = module.request_candidates(Settings(), tenant, incident)
    assert result.status == "NO_DIRECT_SOURCE" and not result.candidates
    assert result.source_alert_count == result.selected_trace_count == 0
    fetch.assert_not_called()
