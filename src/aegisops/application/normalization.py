"""Provider-neutral alert facts and conservative correlation identity."""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aegisops.application.contracts import AlertmanagerAlert
from aegisops.infrastructure.database.models import AlertRecord


class NormalizedAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source: str
    source_event_id: str
    service_id: str
    resource_id: str | None = None
    change_id: str | None = None
    signal_type: str
    alert_name: str
    fingerprint: str
    severity: str
    summary: str = ""
    occurred_at: datetime
    received_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)

    def correlation_key(self) -> str:
        """Stable candidate hint for diagnostics; the scoring rule decides attachment."""
        identity: tuple[str, ...]
        if self.change_id:
            identity = ("change", self.change_id)
        elif self.resource_id:
            identity = ("resource", self.resource_id)
        else:
            identity = ("service-alert", self.service_id, self.alert_name)
        return f"v2:{sha256(repr(identity).encode()).hexdigest()}"


def meaningful_change_id(value: str | None) -> str | None:
    if value is None or value.strip().lower() in {"", "unknown", "none", "null"}:
        return None
    return value


def normalize_prometheus(
    item: AlertmanagerAlert, *, received_at: datetime | None = None
) -> NormalizedAlert:
    labels = {
        key: value[:120]
        for key, value in item.labels.items()
        if key in {"alertname", "service", "resource", "change_id", "signal_type", "severity",
                   "signal_role"}
    }
    return NormalizedAlert(
        source="prometheus",
        source_event_id=f"{item.fingerprint}:{item.startsAt.astimezone(UTC).isoformat()}",
        service_id=labels["service"],
        resource_id=labels.get("resource") or None,
        change_id=meaningful_change_id(labels.get("change_id")),
        signal_type=labels.get("signal_type") or "unknown",
        alert_name=labels["alertname"],
        fingerprint=item.fingerprint,
        severity={"critical": "P1", "warning": "P2"}.get(labels.get("severity", ""), "P3"),
        summary=item.annotations.get("summary", "")[:300],
        occurred_at=item.startsAt.astimezone(UTC),
        received_at=received_at or datetime.now(UTC),
        labels=labels,
    )


def normalized_from_record(alert: AlertRecord) -> NormalizedAlert:
    if alert.schema_version != 1:
        raise ValueError(f"unsupported normalized alert version: {alert.schema_version}")
    return NormalizedAlert(
        schema_version=1,
        source=alert.source,
        source_event_id=alert.source_event_id,
        service_id=alert.service_name,
        resource_id=alert.payload.get("resource"),
        change_id=meaningful_change_id(alert.payload.get("change_id")),
        signal_type=alert.payload.get("signal_type", "unknown"),
        alert_name=alert.payload["alertname"],
        fingerprint=alert.fingerprint,
        severity=alert.severity,
        summary=alert.payload.get("summary", ""),
        occurred_at=alert.occurred_at,
        received_at=alert.received_at,
        labels=alert.payload.get("labels", {}),
    )
