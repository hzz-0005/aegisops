"""Untrusted planner output and persisted, bounded investigation trace."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ToolName = Literal[
    "http_activity", "error_logs", "error_traces", "probe_health", "runtime_state",
    "incoming_requests", "dependency_requests", "database_requests", "request_links",
    "request_membership",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolRequest(StrictModel):
    name: ToolName
    service: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    alert_id: UUID


class ObservationClaim(StrictModel):
    """An exact historical observation; free-text root candidates remain separate."""

    kind: Literal["REQUEST_FAILURE", "RUNTIME_STATE"]
    alert_id: UUID
    service: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    dependency: str | None = Field(default=None, max_length=120)
    failure_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,79}$")
    state: Literal["running", "paused", "exited", "restarting", "dead", "created"] | None = None


class ClaimVerification(StrictModel):
    candidate_index: int
    claim: ObservationClaim | None
    status: Literal["OBSERVATION_VERIFIED", "CONFLICTED", "INSUFFICIENT", "UNSUPPORTED_CLAIM"]
    supporting_evidence_ids: list[UUID] = Field(default_factory=list)
    conflicting_evidence_ids: list[UUID] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    alert_binding_status: Literal["DIRECT_SOURCE_MEMBERSHIP", "UNBOUND"] = "UNBOUND"
    source_member_count: int | None = None
    verified_member_count: int = 0
    root_cause_status: Literal["UNDETERMINED"] = "UNDETERMINED"
    current_fault_status: Literal["UNKNOWN"] = "UNKNOWN"


class RootCauseGateVerdict(StrictModel):
    """Independent mechanism×scope gate; passing it never confirms a root cause."""

    candidate_index: int
    decision: Literal["MECHANISM_SCOPE_SUPPORTED", "GATE_NOT_PASSED"]
    mechanism_status: Literal[
        "OBSERVATION_VERIFIED", "CONFLICTED", "INSUFFICIENT", "UNSUPPORTED_CLAIM"
    ]
    source_scope_status: Literal["FULL_DIRECT_SOURCE", "PARTIAL_DIRECT_SOURCE", "UNBOUND"]
    current_fault_status: Literal["UNKNOWN"] = "UNKNOWN"
    root_cause_status: Literal["UNDETERMINED"] = "UNDETERMINED"
    action_eligible: Literal[False] = False
    blocking_reasons: list[str] = Field(default_factory=list)


class AgentHypothesis(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    state: Literal["CANDIDATE", "WEAKENED", "REJECTED"]
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=8)
    conflicting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=1, max_length=300)
    claim: ObservationClaim | None = None


class AgentDecision(StrictModel):
    decision: Literal["TOOL", "FINISH"]
    reason: str = Field(min_length=1, max_length=300)
    tool: ToolRequest | None = None
    hypotheses: list[AgentHypothesis] = Field(default_factory=list, max_length=4)
    summary: str = Field(default="", max_length=500)
    missing_evidence: list[str] = Field(default_factory=list, max_length=5)
    cited_evidence_ids: list[UUID] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def coherent_decision(self) -> "AgentDecision":
        if (self.decision == "TOOL") != (self.tool is not None):
            raise ValueError("TOOL requires tool; FINISH forbids tool")
        if self.decision == "FINISH" and not self.summary:
            raise ValueError("FINISH requires summary")
        if any(len(item) > 300 for item in self.missing_evidence):
            raise ValueError("missing evidence text exceeds budget")
        return self


class AgentStep(StrictModel):
    number: int
    phase: Literal["MODEL", "TOOL"]
    status: Literal["RESERVED", "COMPLETED", "FAILED", "NO_DATA", "REJECTED"]
    input_hash: str
    input_summary: dict = Field(default_factory=dict)
    decision: AgentDecision | None = None
    evidence_ids: list[UUID] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    error_code: str | None = None
    elapsed_ms: int = 0
    usage: dict[str, int] = Field(default_factory=dict)


class AgentTrace(StrictModel):
    run_id: UUID
    model_name: str | None
    prompt_version: str = "readonly-planner-v5-zh"
    started_at: datetime
    deadline_at: datetime
    model_calls: int = 0
    tool_calls: int = 0
    stop_reason: str | None = None
    steps: list[AgentStep] = Field(default_factory=list)
    summary: str = ""
    missing_evidence: list[str] = Field(default_factory=list)
    hypotheses: list[AgentHypothesis] = Field(default_factory=list)
    initial_evidence: list[dict] = Field(default_factory=list)
    candidate_verifications: list[ClaimVerification] = Field(default_factory=list)
    root_cause_gates: list[RootCauseGateVerdict] = Field(default_factory=list)

