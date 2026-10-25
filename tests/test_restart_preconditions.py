"""Reject unreachable-only diagnosis and target changes before any Docker POST."""

import time
from copy import deepcopy

import httpx
import pytest

from aegisops.application.remediation import lab_executor as module
from aegisops.application.remediation.restart_preconditions import (
    RestartPreconditionFailed,
    restart_identity,
)


def container(action="restart_cache"):
    service = "redis" if action == "restart_cache" else "order-service"
    running = action == "restart_service"
    return {"Id": "a" * 64, "Name": f"/aegisops-{service}-1", "Image": "sha256:" + "b" * 64,
        "Config": {"Labels": {"com.docker.compose.project": "aegisops",
            "com.docker.compose.service": service, "com.docker.compose.config-hash": "c" * 64}},
        "State": {"Status": "running" if running else "exited", "Running": running,
            "Paused": False, "Dead": False, "Restarting": False,
            "StartedAt": "2026-10-08T10:00:00Z"}}


def setup_adapter(monkeypatch, info):
    calls = []
    def respond(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=deepcopy(info))
    client = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **_: client(
        transport=httpx.MockTransport(respond)))
    adapter = module.LabDockerAdapter()
    monkeypatch.setattr(adapter, "redis_healthy", lambda: False)
    monkeypatch.setattr(adapter, "order_pool_exhausted", lambda: True)
    return adapter, calls


@pytest.mark.parametrize("action", ["restart_cache", "restart_service"])
def test_verified_restart_uses_exact_container_id_once(monkeypatch, action):
    adapter, calls = setup_adapter(monkeypatch, container(action))
    assert adapter.preflight_restart(action)
    adapter._restart_verified(action)
    assert [path for method, path in calls if method == "POST"] == [
        f"/containers/{'a' * 64}/restart"]
    with pytest.raises(RestartPreconditionFailed):
        adapter._restart_verified(action)
    assert sum(method == "POST" for method, _ in calls) == 1


def test_unreachable_running_redis_is_not_a_process_exit(monkeypatch):
    info = container()
    info["State"].update(Status="running", Running=True)
    adapter, calls = setup_adapter(monkeypatch, info)
    assert not adapter.preflight_restart("restart_cache")
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("field", ["project", "service", "name", "id", "image", "paused",
                                  "restarting", "dead", "timestamp", "config"])
def test_wrong_or_incomplete_target_is_rejected(field):
    info = container()
    if field in {"project", "service", "config"}:
        key = "config-hash" if field == "config" else field
        info["Config"]["Labels"][f"com.docker.compose.{key}"] = "foreign"
    elif field in {"name", "id", "image"}:
        info[{"name": "Name", "id": "Id", "image": "Image"}[field]] = "foreign"
    elif field == "timestamp":
        info["State"]["StartedAt"] = "2026-10-08T10:00:00"
    else:
        info["State"][field.capitalize()] = True
    with pytest.raises(RestartPreconditionFailed):
        restart_identity(info, "restart_cache")


@pytest.mark.parametrize("change", ["id", "image", "config", "started", "healthy", "stale"])
def test_change_between_preflight_and_restart_makes_no_post(monkeypatch, change):
    info = container()
    adapter, calls = setup_adapter(monkeypatch, info)
    assert adapter.preflight_restart("restart_cache")
    if change == "id":
        info["Id"] = "d" * 64
    elif change == "image":
        info["Image"] = "sha256:" + "d" * 64
    elif change == "config":
        info["Config"]["Labels"]["com.docker.compose.config-hash"] = "d" * 64
    elif change == "started":
        info["State"]["StartedAt"] = "2026-10-11T10:00:00Z"
    elif change == "healthy":
        monkeypatch.setattr(adapter, "redis_healthy", lambda: True)
    else:
        identity, _ = adapter._restart_preflight["restart_cache"]
        adapter._restart_preflight["restart_cache"] = (identity, time.monotonic() - 16)
    with pytest.raises(RestartPreconditionFailed):
        adapter.restart_redis()
    assert all(method == "GET" for method, _ in calls)
