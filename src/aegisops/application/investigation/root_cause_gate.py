"""Independent mechanism×scope gate over verified observations.

Passing the gate means a candidate carries an exact verified historical fact whose
alert has full direct source coverage. It still proves no root cause: the current
fault state stays unknown and no action becomes eligible. Only candidates that pass
this gate may later be discussed as supported root-cause candidates and mapped to
governed actions, and only after the current-fact dimension is resolved elsewhere.
"""

from typing import Literal

from .agent_contracts import AgentTrace, ClaimVerification, RootCauseGateVerdict

ROOT_CAUSE_GATE_VERSION = "root-cause-gate-v1"

ScopeStatus = Literal["FULL_DIRECT_SOURCE", "PARTIAL_DIRECT_SOURCE", "UNBOUND"]


def _source_scope(check: ClaimVerification) -> ScopeStatus:
    if check.alert_binding_status != "DIRECT_SOURCE_MEMBERSHIP":
        return "UNBOUND"
    if check.source_member_count is None:
        return "UNBOUND"
    if check.verified_member_count >= check.source_member_count:
        return "FULL_DIRECT_SOURCE"
    return "PARTIAL_DIRECT_SOURCE"


def evaluate_root_cause_gate(trace: AgentTrace) -> list[RootCauseGateVerdict]:
    """Pure function of persisted verifications; never reads planner text."""
    verdicts: list[RootCauseGateVerdict] = []
    for check in trace.candidate_verifications:
        scope = _source_scope(check)
        blocking: list[str] = []
        if check.status == "OBSERVATION_VERIFIED":
            mechanism_supported = True
        else:
            mechanism_supported = False
            blocking.append({
                "UNSUPPORTED_CLAIM": "NO_VERIFIED_CLAIM",
                "INSUFFICIENT": "MECHANISM_UNPROVEN",
                "CONFLICTED": "CONFLICTED_OBSERVATIONS",
            }[check.status])
        if scope == "FULL_DIRECT_SOURCE":
            scope_supported = True
        else:
            scope_supported = False
            blocking.append("SOURCE_COVERAGE_PARTIAL" if scope == "PARTIAL_DIRECT_SOURCE"
                            else "SOURCE_SCOPE_UNBOUND")
        # Every candidate stays blocked from actions while the current fault state
        # is unknown; this dimension is resolved by recovery evidence, not the trace.
        blocking.append("CURRENT_FAULT_STATUS_UNKNOWN")
        verdicts.append(RootCauseGateVerdict(
            candidate_index=check.candidate_index,
            decision=("MECHANISM_SCOPE_SUPPORTED"
                      if mechanism_supported and scope_supported else "GATE_NOT_PASSED"),
            mechanism_status=check.status,
            source_scope_status=scope,
            blocking_reasons=blocking,
        ))
    return verdicts
