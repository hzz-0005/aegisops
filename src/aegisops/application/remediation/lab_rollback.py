"""Docker Engine adapter for one allowlisted Lab Order deployment rollback."""

import time
from datetime import UTC, datetime
from uuid import UUID

import httpx
from pydantic import BaseModel

from aegisops.application.contracts import DeploymentFingerprint
from aegisops.domain.models import ActionSnapshot

ORDER_CONTAINER = "aegisops-order-service-1"
ORDER_NETWORK = "aegisops_default"
ORDER_COMMAND = ["uvicorn", "lab.services.order:app", "--host", "0.0.0.0", "--port", "8000"]


class RollbackPreconditionFailed(ValueError):
    pass


class RollbackReadiness(BaseModel):
    checked_at: datetime
    backup_container: str | None = None
    order_ready: bool
    payment_ready: bool
    baseline_deployed: bool
    target_fingerprint_matches: bool

    @property
    def passed(self) -> bool:
        return (
            self.order_ready
            and self.payment_ready
            and self.baseline_deployed
            and self.target_fingerprint_matches
        )


def fingerprint_from_inspect(info: dict) -> DeploymentFingerprint:
    env = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
    return DeploymentFingerprint(
        image_id=info["Image"],
        compose_config_hash=info["Config"]["Labels"]["com.docker.compose.config-hash"],
        change_id=env["LAB_DEPLOYMENT_CHANGE_ID"],
        bad_deployment=env["LAB_BAD_DEPLOYMENT"] == "true",
    )


def _expected(snapshot: ActionSnapshot, prefix: str) -> DeploymentFingerprint:
    params = snapshot.parameters
    image_id = params.get(f"{prefix}_image_id")
    config_hash = params.get(f"{prefix}_config_hash")
    change_id = params.get("current_change_id" if prefix == "current" else "to_ref")
    bad = params.get(f"{prefix}_bad_deployment")
    if not (isinstance(image_id, str) and isinstance(config_hash, str)):
        raise RollbackPreconditionFailed("rollback snapshot lacks image and config digests")
    if not isinstance(change_id, str) or not isinstance(bad, bool):
        raise RollbackPreconditionFailed("rollback snapshot lacks release identity")
    return DeploymentFingerprint(
        image_id=image_id,
        compose_config_hash=config_hash,
        change_id=change_id,
        bad_deployment=bad,
    )


def baseline_create_spec(info: dict, snapshot: ActionSnapshot) -> dict:
    """Build a minimal container spec from the observed Lab service, never arbitrary commands."""
    target = _expected(snapshot, "target")
    current = _expected(snapshot, "current")
    labels = info["Config"]["Labels"]
    host = info["HostConfig"]
    ports = host.get("PortBindings")
    networks = info["NetworkSettings"]["Networks"]
    if (
        snapshot.action_type != "rollback_service"
        or snapshot.parameters.get("to_ref") != "baseline"
        or current.image_id != target.image_id
        or not current.bad_deployment
        or target.bad_deployment
        or fingerprint_from_inspect(info) != current
        or labels.get("com.docker.compose.project") != "aegisops"
        or labels.get("com.docker.compose.service") != "order-service"
        or info["Config"].get("Cmd") != ORDER_COMMAND
        or host.get("NetworkMode") != ORDER_NETWORK
        or ports != {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8003"}]}
        or host.get("Binds")
        or host.get("Mounts")
        or host.get("Privileged")
        or set(networks) != {ORDER_NETWORK}
        or info["State"].get("Running") is not True
    ):
        raise RollbackPreconditionFailed("current Order container differs from approved Lab scope")
    env = [
        item
        for item in info["Config"]["Env"]
        if not item.startswith(("LAB_BAD_DEPLOYMENT=", "LAB_DEPLOYMENT_CHANGE_ID="))
    ]
    env.extend(["LAB_BAD_DEPLOYMENT=false", "LAB_DEPLOYMENT_CHANGE_ID=baseline"])
    replacement_labels = dict(labels)
    replacement_labels["com.docker.compose.config-hash"] = target.compose_config_hash
    return {
        "Image": target.image_id,
        "Env": env,
        "Cmd": ORDER_COMMAND,
        "Entrypoint": info["Config"].get("Entrypoint"),
        "WorkingDir": info["Config"].get("WorkingDir") or "/app",
        "User": info["Config"].get("User") or "",
        "ExposedPorts": {"8000/tcp": {}},
        "Labels": replacement_labels,
        "HostConfig": {
            "NetworkMode": ORDER_NETWORK,
            "PortBindings": ports,
            "RestartPolicy": host.get("RestartPolicy", {"Name": "no"}),
        },
        "NetworkingConfig": {
            "EndpointsConfig": {
                ORDER_NETWORK: {"Aliases": [ORDER_CONTAINER, "order-service"]}
            }
        },
    }


class LabOrderRollbackAdapter:
    def __init__(
        self,
        socket_path: str = "/var/run/docker.sock",
        order_url: str = "http://order-service:8000",
        payment_url: str = "http://payment-service:8000",
    ) -> None:
        self.socket_path = socket_path
        self.order_url = order_url
        self.payment_url = payment_url

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.HTTPTransport(uds=self.socket_path), timeout=20)

    def inspect_order(self) -> dict:
        with self._client() as client:
            response = client.get(f"http://docker/containers/{ORDER_CONTAINER}/json")
            response.raise_for_status()
            return response.json()

    def preflight(self, snapshot: ActionSnapshot) -> bool:
        try:
            baseline_create_spec(self.inspect_order(), snapshot)
            return True
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return False

    def rollback(self, snapshot: ActionSnapshot, action_id: UUID) -> str:
        """One side-effect attempt. Any uncertainty is left for operator reconciliation."""
        info = self.inspect_order()
        spec = baseline_create_spec(info, snapshot)
        backup_name = f"{ORDER_CONTAINER}-before-{action_id.hex[:12]}"
        with self._client() as client:
            image = client.get(f"http://docker/images/{spec['Image']}/json")
            image.raise_for_status()
            response = client.post(
                f"http://docker/containers/{ORDER_CONTAINER}/stop", params={"t": 3}
            )
            response.raise_for_status()
            response = client.post(
                f"http://docker/containers/{ORDER_CONTAINER}/rename", params={"name": backup_name}
            )
            response.raise_for_status()
            response = client.post(
                "http://docker/containers/create", params={"name": ORDER_CONTAINER}, json=spec
            )
            response.raise_for_status()
            new_id = response.json()["Id"]
            response = client.post(f"http://docker/containers/{new_id}/start")
            response.raise_for_status()
        return backup_name

    def verify(self, snapshot: ActionSnapshot) -> RollbackReadiness:
        target = _expected(snapshot, "target")
        result = RollbackReadiness(
            checked_at=datetime.now(UTC),
            order_ready=False,
            payment_ready=False,
            baseline_deployed=False,
            target_fingerprint_matches=False,
        )
        for _ in range(20):
            order_ready = payment_ready = baseline_deployed = fingerprint_matches = False
            try:
                info = self.inspect_order()
                fingerprint_matches = fingerprint_from_inspect(info) == target
                with httpx.Client(timeout=3) as client:
                    response = client.get(f"{self.order_url.rstrip('/')}/deployment")
                    baseline_deployed = response.status_code == 200 and response.json() == {
                        "change_id": "baseline",
                        "bad_deployment": False,
                    }
                    response = client.get(f"{self.order_url.rstrip('/')}/health/ready")
                    order_ready = (
                        response.status_code == 200 and response.json().get("status") == "ready"
                    )
                    response = client.get(f"{self.payment_url.rstrip('/')}/health/ready")
                    payment_ready = (
                        response.status_code == 200 and response.json().get("status") == "ready"
                    )
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                pass
            result = RollbackReadiness(
                checked_at=datetime.now(UTC),
                order_ready=order_ready,
                payment_ready=payment_ready,
                baseline_deployed=baseline_deployed,
                target_fingerprint_matches=fingerprint_matches,
            )
            if result.passed:
                return result
            time.sleep(1)
        return result
