"""The delay transport preserves source facts and captures only its scoped endpoint."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx

from scripts.evaluate_request_event_delay import HoldingRelay


def test_relay_holds_original_payload_without_rewriting_source() -> None:
    relay = HoldingRelay(port=0)
    relay.start()
    payload = {
        "schema_version": 1, "source_event_id": str(uuid4()), "service": "order-service",
        "trace_id": uuid4().hex, "span_id": uuid4().hex[:16],
        "occurred_at": datetime.now(UTC).isoformat(), "failure_code": "dependency_timeout",
        "resource": "payment-service",
    }
    base = f"http://127.0.0.1:{relay.server.server_port}"
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            assert client.post(base + "/capture/wrong", json=payload).status_code == 404
            assert client.post(base + relay.route, json=payload).status_code == 400
            response = client.post(
                base + relay.route, json=payload, headers={"Authorization": "Bearer test-only"},
            )
            assert response.status_code == 202
        assert relay.records == [(payload, "Bearer test-only")]
    finally:
        relay.close()
