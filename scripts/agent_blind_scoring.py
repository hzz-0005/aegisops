"""Frozen blind-test scoring: verified claims against injected truth, nothing else.

Scores follow the manifest protocol (eval/agent-blind-manifest-v1.json): only
backend-verified claims can score, unknown never counts as correct, and the
quiet control fails closed on any root or health claim. Free-text hypothesis
labels are recorded but never scored.
"""

import json
from pathlib import Path
from typing import Any

LOCALISATION = "localisation"
CORRECT = "CORRECT"
WRONG = "WRONG"
UNKNOWN = "UNKNOWN"
SCOPE_LIMITED_UNKNOWN = "SCOPE_LIMITED_UNKNOWN"
FALSE_POSITIVE = "FALSE_POSITIVE"
NO_REPORT = "NO_REPORT"

VERIFIED = "OBSERVATION_VERIFIED"


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("version") not in {"agent-blind-manifest-v1",
                                       "agent-blind-manifest-v2"}:
        raise ValueError("unknown blind manifest version")
    ids = [case["case_id"] for case in manifest["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in blind manifest")
    return manifest


def claim_matches_family(claim: dict[str, Any], family: dict[str, Any]) -> bool:
    for key, value in family.items():
        if key == "failure_codes":
            if claim.get("failure_code") not in value:
                return False
        elif claim.get(key) != value:
            return False
    return True


def _verified_claims(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not report:
        return []
    claims = []
    for check in report.get("candidate_verifications", []):
        if check.get("status") != VERIFIED or not check.get("claim"):
            continue
        claims.append(check["claim"])
    return claims


def _references_root(claim: dict[str, Any], root_service: str) -> bool:
    return claim.get("service") == root_service or claim.get("dependency") == root_service


def score_report(report: dict[str, Any] | None, case: dict[str, Any]) -> dict[str, Any]:
    """Score one persisted report dict against one frozen manifest case."""
    truth = case.get("truth")
    claims = _verified_claims(report)
    result: dict[str, Any] = {
        "case_id": case["case_id"],
        "verified_claims": claims,
        LOCALISATION: UNKNOWN,
        "mechanism_exact": None,
        "unhealthy_or_root_confidence": None,
    }
    if report is None:
        result[LOCALISATION] = NO_REPORT
        return result
    result["unhealthy_or_root_confidence"] = bool(
        report.get("primary_hypothesis") or (report.get("confidence") or 0) > 0
    )
    if truth is None:
        # Quiet control: any verified claim or root/confidence assertion is a
        # false positive; a clean inconclusive finish is the correct behavior.
        result[LOCALISATION] = FALSE_POSITIVE if claims or result[
            "unhealthy_or_root_confidence"] else CORRECT
        return result
    root_service = truth["root_service"]
    matching = [claim for claim in claims
                if any(claim_matches_family(claim, family)
                       for family in truth["localisation_claim_families"])]
    if matching:
        result[LOCALISATION] = CORRECT
        result["mechanism_exact"] = any(
            any(claim_matches_family(claim, exact)
                for exact in truth["mechanism_exact_claims"])
            for claim in matching)
    elif any(_references_root(claim, root_service) for claim in claims):
        # A verified claim touching the root service outside the frozen family is
        # recorded as correct localisation with unknown mechanism rather than wrong.
        result[LOCALISATION] = CORRECT
        result["mechanism_exact"] = False
    elif claims:
        result[LOCALISATION] = WRONG
    elif case.get("scope_limited"):
        result[LOCALISATION] = SCOPE_LIMITED_UNKNOWN
    else:
        result[LOCALISATION] = UNKNOWN
    return result


def aggregate(scores: list[dict[str, Any]], manifest: dict) -> dict[str, Any]:
    fault_scores = [s for s in scores if s["case_id"] != _control_id(manifest)]
    counts = {name: 0 for name in
              (CORRECT, WRONG, UNKNOWN, SCOPE_LIMITED_UNKNOWN, FALSE_POSITIVE, NO_REPORT)}
    for score in scores:
        counts[score[LOCALISATION]] += 1
    exact_hits = sum(1 for s in fault_scores if s.get("mechanism_exact"))
    chance = None
    plausible = manifest.get("plausible_root_services") or []
    if plausible and fault_scores:
        chance = round(1 / len(plausible), 4)
    # Accuracy counts only fault cases; the control's clean exit is not a hit.
    localization_correct = sum(1 for s in fault_scores if s[LOCALISATION] == CORRECT)
    fault_total = len(fault_scores)
    accuracy = round(localization_correct / fault_total, 4) if fault_total else None
    lift = (round(accuracy / chance, 3)
            if accuracy is not None and chance and fault_scores else None)
    return {
        "case_count": len(scores),
        "fault_case_count": fault_total,
        "localisation_counts": counts,
        "localisation_accuracy": accuracy,
        "mechanism_exact_count": exact_hits,
        "chance_at_1": chance,
        "lift_over_chance": lift,
        "unknown_rate": (round(
            (counts[UNKNOWN] + counts[SCOPE_LIMITED_UNKNOWN]) / fault_total, 4)
            if fault_total else None),
    }


def _control_id(manifest: dict) -> str | None:
    for case in manifest["cases"]:
        if case.get("truth") is None:
            return case["case_id"]
    return None
