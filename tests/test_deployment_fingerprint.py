"""Deployment evidence must bind the observed container state to release refs."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aegisops.application.contracts import ChangeEventInput
from aegisops.application.remediation.service import PlanRejected, _rollback_parameters


def deployment_event() -> dict:
    return {
        "source": "lab-deployment",
        "external_id": "change-123",
        "service": "order-service",
        "environment": "lab",
        "change_type": "DEPLOYMENT",
        "from_ref": "baseline",
        "to_ref": "change-123",
        "occurred_at": datetime.now(UTC),
        "deployment": {
            "before": {
                "image_id": f"sha256:{'a' * 64}",
                "compose_config_hash": "b" * 64,
                "change_id": "baseline",
                "bad_deployment": False,
            },
            "after": {
                "image_id": f"sha256:{'a' * 64}",
                "compose_config_hash": "c" * 64,
                "change_id": "change-123",
                "bad_deployment": True,
            },
        },
    }


def test_deployment_fingerprint_matches_release_refs() -> None:
    event = ChangeEventInput.model_validate(deployment_event())
    assert event.deployment is not None
    assert event.deployment.before.change_id == event.from_ref
    assert event.deployment.after.compose_config_hash == "c" * 64


@pytest.mark.parametrize("field,value", [("change_id", "other"), ("image_id", "latest")])
def test_deployment_fingerprint_rejects_inconsistent_provenance(field: str, value: str) -> None:
    data = deployment_event()
    data["deployment"]["after"][field] = value
    with pytest.raises(ValidationError):
        ChangeEventInput.model_validate(data)


def test_rollback_snapshot_binds_both_observed_releases() -> None:
    event = deployment_event()
    parameters = _rollback_parameters(event)
    assert parameters["to_ref"] == "baseline"
    assert parameters["target_config_hash"] == "b" * 64
    assert parameters["current_config_hash"] == "c" * 64
    assert parameters["current_change_id"] == "change-123"


def test_rollback_snapshot_rejects_missing_or_conflicting_fingerprint() -> None:
    event = deployment_event()
    event.pop("deployment")
    with pytest.raises(PlanRejected):
        _rollback_parameters(event)
    event = deployment_event()
    event["deployment"]["after"]["bad_deployment"] = False
    with pytest.raises(PlanRejected):
        _rollback_parameters(event)
