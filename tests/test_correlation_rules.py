from datetime import UTC, datetime, timedelta

from aegisops.application.correlation_rules import evaluate_pair, has_ambiguous_candidates
from aegisops.application.normalization import NormalizedAlert


def fact(
    name: str,
    service: str,
    *,
    resource: str | None = None,
    change: str | None = None,
    occurred_at: datetime | None = None,
    signal_type: str = "availability",
) -> NormalizedAlert:
    time = occurred_at or datetime.now(UTC)
    return NormalizedAlert(
        source="prometheus",
        source_event_id=f"{name}:{service}:{time.isoformat()}",
        service_id=service,
        resource_id=resource,
        change_id=change,
        signal_type=signal_type,
        alert_name=name,
        fingerprint="fp",
        severity="P1",
        occurred_at=time,
        received_at=time,
    )


def test_shared_resource_needs_temporal_support_and_conflicts_can_override() -> None:
    time = datetime.now(UTC)
    redis = fact("RedisDown", "payment-service", resource="redis-main", occurred_at=time)
    payment = fact(
        "PaymentErrors",
        "order-service",
        resource="redis-main",
        occurred_at=time + timedelta(seconds=10),
    )
    score = evaluate_pair(redis, payment, shared_dependency=True)
    assert score.should_attach
    assert score.matched_features == {
        "within_60_seconds": 0.15,
        "same_resource": 0.25,
        "shared_dependency": 0.25,
    }
    assert score.conflicting_features == {}
    assert not evaluate_pair(redis, payment).should_attach

    unrelated_change = fact(
        "PaymentErrors",
        "payment-service",
        resource="redis-main",
        change="deploy-2",
        occurred_at=time + timedelta(seconds=10),
    )
    old_change = fact(
        "PaymentErrors",
        "payment-service",
        resource="redis-main",
        change="deploy-1",
        occurred_at=time,
    )
    conflicted = evaluate_pair(unrelated_change, old_change)
    assert not conflicted.should_attach
    assert conflicted.conflicting_features["different_change"] == -0.50


def test_distant_events_do_not_merge_even_with_same_resource() -> None:
    time = datetime.now(UTC)
    first = fact("RedisDown", "payment-service", resource="redis-main", occurred_at=time)
    later = fact(
        "RedisDown",
        "payment-service",
        resource="redis-main",
        occurred_at=time + timedelta(minutes=16),
    )
    assert not evaluate_pair(first, later).should_attach


def test_close_incident_candidates_require_a_clear_winner() -> None:
    assert has_ambiguous_candidates(0.7, 0.65)
    assert has_ambiguous_candidates(0.7, 0.6)
    assert not has_ambiguous_candidates(0.7, 0.59)
    assert not has_ambiguous_candidates(0.7, 0.49)


def test_alert_with_change_does_not_join_older_unattributed_failure() -> None:
    now = datetime.now(UTC)
    old_gateway = fact(
        "GatewayOrderFailures", "lab-gateway", resource="order-service", occurred_at=now
    )
    new_gateway = fact(
        "GatewayOrderFailures", "lab-gateway", resource="order-service",
        change="deploy-new", occurred_at=now + timedelta(seconds=10),
    )
    score = evaluate_pair(new_gateway, old_gateway)
    assert not score.should_attach
    assert score.conflicting_features["missing_change_context"] == -0.45


def test_same_resource_different_signal_types_are_separate_without_shared_change() -> None:
    now = datetime.now(UTC)
    connectivity = fact(
        "DatabaseReachabilityHoldout", "order-service", resource="orders-postgres",
        occurred_at=now, signal_type="availability",
    )
    saturation = fact(
        "PoolSaturationHoldout", "order-service", resource="orders-postgres",
        occurred_at=now + timedelta(seconds=5), signal_type="saturation",
    )
    score = evaluate_pair(connectivity, saturation)
    assert not score.should_attach
    assert score.conflicting_features["different_signal_type"] == -0.80

    same_change = evaluate_pair(
        connectivity.model_copy(update={"change_id": "deploy-1"}),
        saturation.model_copy(update={"change_id": "deploy-1"}),
    )
    assert not same_change.should_attach
    assert same_change.conflicting_features["different_signal_type"] == -0.80


def test_shared_change_cannot_replace_service_relation() -> None:
    left = fact("SameAlert", "service-a", resource="shared", change="release",
                signal_type="deployment")
    right = fact("SameAlert", "service-b", resource="shared", change="release",
                 signal_type="deployment", occurred_at=left.occurred_at)
    assert not evaluate_pair(left, right).should_attach
    assert evaluate_pair(left, right, shared_dependency=True).should_attach


def test_dependent_symptom_requires_role_owner_relation_and_matching_change() -> None:
    fault = fact("Release", "order-service", resource="order-service", change="release",
                 signal_type="deployment").model_copy(update={"labels": {"signal_role": "fault"}})
    symptom = fact("Errors", "gateway", resource="order-service", change="release",
                   signal_type="availability", occurred_at=fault.occurred_at).model_copy(
                       update={"labels": {"signal_role": "symptom"}})
    assert evaluate_pair(fault, symptom, shared_dependency=True).should_attach
    assert evaluate_pair(symptom, fault, shared_dependency=True).should_attach
    assert not evaluate_pair(fault, symptom).should_attach
    assert not evaluate_pair(fault, symptom.model_copy(update={"labels": {}}),
                             shared_dependency=True).should_attach
    assert not evaluate_pair(fault, symptom.model_copy(update={"change_id": "other"}),
                             shared_dependency=True).should_attach
    assert not evaluate_pair(fault, symptom.model_copy(update={"resource_id": "another"}),
                             shared_dependency=True).should_attach
