from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegisops.application.contracts import AlertmanagerAlert
from aegisops.application.correlation_rules import evaluate_pair
from aegisops.application.normalization import normalize_prometheus


def alert(name: str, service: str, **labels: str) -> AlertmanagerAlert:
    return AlertmanagerAlert(
        status="firing",
        fingerprint=uuid4().hex,
        startsAt=datetime.now(UTC),
        labels={"alertname": name, "service": service, **labels},
    )


def test_shared_resource_merges_different_symptoms() -> None:
    redis_down = normalize_prometheus(alert("RedisDown", "payment-service", resource="redis-main"))
    payment_errors = normalize_prometheus(
        alert("PaymentErrorRateHigh", "order-service", resource="redis-main")
    )
    database_timeout = normalize_prometheus(
        alert("OrderDatabaseTimeout", "order-service", resource="orders-postgres")
    )
    assert redis_down.correlation_key() == payment_errors.correlation_key()
    assert redis_down.correlation_key() != database_timeout.correlation_key()


def test_change_event_groups_deployment_symptoms_without_redis_merge() -> None:
    deployment = normalize_prometheus(
        alert("DeploymentChanged", "payment-service", change_id="deploy-123")
    )
    errors = normalize_prometheus(alert("PaymentErrors", "payment-service", change_id="deploy-123"))
    redis_fault = normalize_prometheus(
        alert("PaymentErrors", "payment-service", resource="redis-main")
    )
    assert deployment.correlation_key() == errors.correlation_key()
    assert deployment.correlation_key() != redis_fault.correlation_key()


def test_source_identity_and_event_time_survive_delayed_delivery() -> None:
    occurred = datetime.now(UTC) - timedelta(minutes=3)
    delivered = datetime.now(UTC)
    incoming = AlertmanagerAlert(
        status="firing",
        fingerprint="signal-123",
        startsAt=occurred,
        labels={"alertname": "DatabaseTimeout", "service": "order-service"},
    )
    normalized = normalize_prometheus(incoming, received_at=delivered)
    repeated = normalize_prometheus(incoming, received_at=delivered + timedelta(seconds=30))
    assert normalized.schema_version == 1
    assert normalized.source_event_id == repeated.source_event_id
    assert normalized.occurred_at == occurred
    assert normalized.received_at == delivered
    assert normalized.received_at != repeated.received_at


def test_unknown_change_placeholder_is_not_deployment_evidence() -> None:
    gateway = normalize_prometheus(
        alert(
            "GatewayOrderFailures", "lab-gateway",
            resource="order-service", change_id="unknown",
        )
    )
    probe = normalize_prometheus(
        alert("LabServiceUnreachable", "order-service", resource="order-service")
    )
    assert gateway.change_id is None
    assert evaluate_pair(gateway, probe, shared_dependency=True).should_attach


def test_alertmanager_timestamp_requires_timezone() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        AlertmanagerAlert(
            status="firing",
            fingerprint="signal-123",
            startsAt=datetime.now(UTC).replace(tzinfo=None),
            labels={"alertname": "DatabaseTimeout", "service": "order-service"},
        )
