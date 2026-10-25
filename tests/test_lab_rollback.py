"""Only the approved faulty Order container may be recreated as the baseline."""

from copy import deepcopy

import pytest

from aegisops.application.remediation.lab_rollback import (
    RollbackPreconditionFailed,
    baseline_create_spec,
)
from aegisops.application.remediation.service import _rollback_parameters
from aegisops.domain.models import ActionSnapshot


def current_container() -> dict:
    return {
        "Image": f"sha256:{'a' * 64}",
        "State": {"Running": True},
        "Config": {
            "Env": [
                "LAB_BAD_DEPLOYMENT=true",
                "LAB_DEPLOYMENT_CHANGE_ID=change-123",
                "LAB_DATABASE_URL=postgresql://example.invalid/lab",
            ],
            "Cmd": ["uvicorn", "lab.services.order:app", "--host", "0.0.0.0", "--port", "8000"],
            "Labels": {
                "com.docker.compose.project": "aegisops",
                "com.docker.compose.service": "order-service",
                "com.docker.compose.config-hash": "c" * 64,
            },
            "WorkingDir": "/app",
        },
        "HostConfig": {
            "NetworkMode": "aegisops_default",
            "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8003"}]},
            "RestartPolicy": {"Name": "no"},
            "Binds": None,
            "Mounts": None,
            "Privileged": False,
        },
        "NetworkSettings": {"Networks": {"aegisops_default": {}}},
    }


def approved_snapshot() -> ActionSnapshot:
    deployment = {
        "from_ref": "baseline",
        "to_ref": "change-123",
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
    return ActionSnapshot(
        action_type="rollback_service",
        target="service/order-service-id",
        parameters=_rollback_parameters(deployment),
        resource_version=1,
        plan_hash="approved-plan",
        risk="HIGH",
    )


def test_baseline_spec_restricts_command_network_and_release() -> None:
    spec = baseline_create_spec(current_container(), approved_snapshot())
    assert spec["Image"] == f"sha256:{'a' * 64}"
    assert spec["Labels"]["com.docker.compose.config-hash"] == "b" * 64
    assert "LAB_BAD_DEPLOYMENT=false" in spec["Env"]
    assert "LAB_DEPLOYMENT_CHANGE_ID=baseline" in spec["Env"]
    assert "LAB_BAD_DEPLOYMENT=true" not in spec["Env"]
    assert spec["HostConfig"]["PortBindings"] == {
        "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8003"}]
    }


@pytest.mark.parametrize("mutation", ["different_change", "wrong_service", "extra_mount"])
def test_baseline_spec_rejects_scope_drift(mutation: str) -> None:
    current = deepcopy(current_container())
    if mutation == "different_change":
        current["Config"]["Env"][1] = "LAB_DEPLOYMENT_CHANGE_ID=another-change"
    elif mutation == "wrong_service":
        current["Config"]["Labels"]["com.docker.compose.service"] = "payment-service"
    else:
        current["HostConfig"]["Binds"] = ["/host:/app"]
    with pytest.raises(RollbackPreconditionFailed):
        baseline_create_spec(current, approved_snapshot())
