"""Reviewed action templates; unknown hypotheses cannot produce a write proposal."""

import tomllib
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_key: str
    action_type: Literal["restart_cache", "restart_service", "rollback_service"]
    target_kind: Literal["resource", "service"]
    target_slug: str
    risk: Literal["HIGH"]
    rationale: str = Field(min_length=1)
    blast_radius: str = Field(min_length=1)
    parameter_from_change: Literal["from_ref"] | None = None


class RemediationCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    actions: list[ActionTemplate]

    def find(self, hypothesis_key: str) -> ActionTemplate | None:
        return next((item for item in self.actions if item.hypothesis_key == hypothesis_key), None)


def load_remediation_catalog() -> RemediationCatalog:
    path = files("aegisops.application.remediation").joinpath("catalog.toml")
    return RemediationCatalog.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
