"""A DNS injection need not produce a single identical source failure code."""

from uuid import UUID, uuid4

from scripts.evaluate_detector_tempo_outage import eligible_event_ids


def test_mixed_order_symptoms_only_qualify_at_gateway() -> None:
    first, second = uuid4().hex, uuid4().hex
    rows = [
        {"id": str(uuid4()), "service": "order", "failure_code": code,
         "resource": "payment", "trace_id": trace}
        for code, trace in [("timeout", first), ("transport", second)]
    ]
    gateway = [
        {"id": str(uuid4()), "service": "gateway", "failure_code": "5xx",
         "resource": "order", "trace_id": trace}
        for trace in [first, second]
    ]
    assert eligible_event_ids(rows + gateway) == {UUID(e["id"]) for e in gateway}


def test_duplicate_trace_cannot_satisfy_threshold() -> None:
    rows = [
        {"id": str(uuid4()), "service": "order", "failure_code": "transport",
         "resource": "payment", "trace_id": "same-trace"}
        for _ in range(2)
    ]
    assert eligible_event_ids(rows) == set()
