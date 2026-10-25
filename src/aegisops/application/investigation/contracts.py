"""Versioned, bounded facts used by the investigation graph and public report."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .agent_contracts import AgentTrace, ClaimVerification, RootCauseGateVerdict


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    source_type: Literal["ALERT", "METRIC", "LOG", "TRACE", "CHANGE", "CATALOG"]
    source_ref: str = Field(max_length=500)
    kind: str = Field(max_length=80)
    subject: str = Field(max_length=160)
    polarity: Literal["SUPPORT", "CONFLICT", "CONTEXT"]
    observed_at: datetime | None
    collected_at: datetime
    tool_name: str = Field(max_length=100)
    tool_version: str = Field(max_length=60)
    payload: dict[str, Any]


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    score: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[UUID]
    conflicting_evidence_ids: list[UUID]
    rationale: str


class InvestigationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    incident_id: UUID
    source_incident_version: int
    current_incident_version: int | None = None
    is_stale: bool = False
    status: Literal["SUPPORTED", "INCONCLUSIVE"]
    graph_version: str
    summary: str = Field(max_length=500)
    primary_hypothesis: str | None
    confidence: float = Field(ge=0, le=1)
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    limitations: list[str]
    completed_at: datetime
    agent_trace: AgentTrace | None = None
    candidate_verifications: list[ClaimVerification] = Field(default_factory=list)
    root_cause_gates: list[RootCauseGateVerdict] = Field(default_factory=list)
