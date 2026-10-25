"""Broad telemetry scans must not be presented as causal incident relations."""

from types import SimpleNamespace
from uuid import uuid4

from aegisops.application.investigation.trace_overlaps import (
    TraceOverlapObservation,
    _trace_evidence,
)


def test_overlap_contract_does_not_claim_incident_relation() -> None:
    observation = TraceOverlapObservation(
        other_incident_id=uuid4(),
        source_report_id=uuid4(),
        other_report_id=uuid4(),
        overlapping_traces=[],
    )
    assert observation.match_basis == "SERVICE_TIME_WINDOW"
    assert "未证明告警共享请求" in observation.interpretation
    assert "relation_type" not in observation.model_dump()


def test_only_valid_error_trace_refs_can_overlap() -> None:
    valid = SimpleNamespace(
        source_type="TRACE", kind="ERROR_SPAN_ACTIVITY", source_ref="tempo:" + "a" * 32
    )
    assert _trace_evidence(valid)
    assert not _trace_evidence(SimpleNamespace(**{**vars(valid), "source_type": "LOG"}))
    assert not _trace_evidence(SimpleNamespace(**{**vars(valid), "kind": "NORMALIZED_ALERT"}))
    malformed = SimpleNamespace(**{**vars(valid), "source_ref": "tempo:" + "z" * 32})
    assert not _trace_evidence(malformed)
