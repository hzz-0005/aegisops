"""Bounded restart identities; an unreachable endpoint alone never permits a restart."""

import re
from dataclasses import dataclass
from datetime import datetime

RESTART_TARGETS = {"restart_cache": ("redis", "aegisops-redis-1"),
                   "restart_service": ("order-service", "aegisops-order-service-1")}
MAX_PREFLIGHT_AGE_SECONDS = 15


class RestartPreconditionFailed(ValueError):
    pass


@dataclass(frozen=True)
class RestartIdentity:
    container_id: str
    image_id: str
    config_hash: str
    started_at: datetime
    state: str


def restart_identity(info: dict, action_type: str) -> RestartIdentity:
    if action_type not in RESTART_TARGETS:
        raise RestartPreconditionFailed("unsupported restart target")
    service, name = RESTART_TARGETS[action_type]
    labels = info.get("Config", {}).get("Labels", {})
    state = info.get("State", {})
    ident, image = info.get("Id", ""), info.get("Image", "")
    config_hash = labels.get("com.docker.compose.config-hash", "")
    if (info.get("Name") != f"/{name}"
            or labels.get("com.docker.compose.project") != "aegisops"
            or labels.get("com.docker.compose.service") != service
            or not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{64}", ident)
            or not isinstance(image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            or not isinstance(config_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", config_hash)):
        raise RestartPreconditionFailed("container identity is outside the approved Lab target")
    expected = "exited" if action_type == "restart_cache" else "running"
    if (state.get("Status") != expected or state.get("Running") is not (expected == "running")
            or state.get("Paused") is not False or state.get("Restarting") is not False
            or state.get("Dead") is not False):
        raise RestartPreconditionFailed("container lifecycle does not permit this restart")
    try:
        started_at = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
        if started_at.utcoffset() is None:
            raise ValueError("missing timezone")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise RestartPreconditionFailed("container start identity is unavailable") from exc
    return RestartIdentity(ident, image, config_hash, started_at, expected)
