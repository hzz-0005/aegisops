"""Trusted signal definitions extend scenarios without changing Incident Core."""

import tomllib
from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field


class SignalDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_name: str
    service: str
    hypothesis_key: str
    hypothesis_label: str
    metric_name: str
    metric_change_label: bool = False
    requires_change: bool = False
    root_candidate: bool = True
    log_phrase: str | None = None
    trace_span: str | None = None


class SignalCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    signals: list[SignalDefinition]

    def find(self, alert_name: str, service: str) -> SignalDefinition | None:
        exact = next(
            (signal for signal in self.signals
             if signal.alert_name == alert_name and signal.service == service), None
        )
        if exact is not None:
            return exact
        return next(
            (signal for signal in self.signals
             if signal.alert_name == alert_name and signal.service == "*"), None
        )


def load_signal_catalog() -> SignalCatalog:
    path = files("aegisops.application.investigation").joinpath("signals.toml")
    return SignalCatalog.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
