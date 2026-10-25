"""Deterministic bridge from gate-passed verified claims to governed actions.

The model never proposes or names actions. A candidate may only reach the
remediation catalog when the independent verifier marked its claim
OBSERVATION_VERIFIED and the root-cause gate confirmed mechanism plus full
direct-source coverage, and only when this reviewed mapping table recognizes
the exact claim. Everything unmapped stays non-actionable (proposal rejected).
"""

from ..investigation.agent_contracts import (
    ClaimVerification,
    ObservationClaim,
    RootCauseGateVerdict,
)
from ..investigation.contracts import InvestigationReport
from .catalog import ActionTemplate, RemediationCatalog, load_remediation_catalog

# Verified "payment failed to reach redis" is the mechanism restart_cache
# addresses; the executor re-validates the current fault before any effect.
REDIS_CACHE_CLAIM = {
    "kind": "REQUEST_FAILURE",
    "dependency": "redis-main",
    "failure_codes": {"dependency_transport", "dependency_timeout"},
}


def map_claim(
    claim: ObservationClaim | None, catalog: RemediationCatalog | None = None
) -> ActionTemplate | None:
    if claim is None or claim.kind != REDIS_CACHE_CLAIM["kind"]:
        return None
    if claim.dependency != REDIS_CACHE_CLAIM["dependency"]:
        return None
    if claim.failure_code not in REDIS_CACHE_CLAIM["failure_codes"]:
        return None
    catalog = catalog or load_remediation_catalog()
    return catalog.find("redis_unavailable")


def agent_action_candidate(
    report: InvestigationReport, catalog: RemediationCatalog | None = None
) -> tuple[ClaimVerification, RootCauseGateVerdict, ActionTemplate] | None:
    """First gate-passed candidate whose verified claim maps to a reviewed action."""
    gates = {gate.candidate_index: gate for gate in report.root_cause_gates}
    for check in report.candidate_verifications:
        gate = gates.get(check.candidate_index)
        if gate is None or gate.decision != "MECHANISM_SCOPE_SUPPORTED":
            continue
        if check.status != "OBSERVATION_VERIFIED" or check.claim is None:
            continue
        template = map_claim(check.claim, catalog)
        if template is not None:
            return check, gate, template
    return None
