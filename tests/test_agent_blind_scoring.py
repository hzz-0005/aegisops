"""Blind scoring cannot inflate: unknown is never correct, control fails closed."""

from pathlib import Path

from scripts.agent_blind_scoring import (
    CORRECT,
    FALSE_POSITIVE,
    NO_REPORT,
    SCOPE_LIMITED_UNKNOWN,
    UNKNOWN,
    WRONG,
    aggregate,
    claim_matches_family,
    load_manifest,
    score_report,
)

MANIFEST = Path("eval/agent-blind-manifest-v1.json")


def verified_claim(claim):
    return {"status": "OBSERVATION_VERIFIED", "claim": claim,
            "supporting_evidence_ids": ["e1"], "conflicting_evidence_ids": []}


def report(claims, confidence=0):
    return {"candidate_verifications": [verified_claim(c) for c in claims],
            "primary_hypothesis": None, "confidence": confidence}


def pause_case():
    manifest = load_manifest(MANIFEST)
    return next(c for c in manifest["cases"]
                if c["case_id"] == "ec9600c1-af74-5b82-b668-fa8e9527fc7b")


def control_case():
    manifest = load_manifest(MANIFEST)
    return next(c for c in manifest["cases"] if c["truth"] is None)


def gateway_case():
    manifest = load_manifest(MANIFEST)
    return next(c for c in manifest["cases"]
                if c["case_id"] == "fb9dbba2-ad09-54c7-8bfb-677fc3bc8680")


def test_exact_mechanism_claim_scores_correct_and_exact():
    score = score_report(report(
        [{"kind": "RUNTIME_STATE", "alert_id": "a", "service": "inventory-service",
          "state": "paused"}]), pause_case())
    assert score["localisation"] == CORRECT and score["mechanism_exact"] is True


def test_shallow_dependency_claim_is_correct_localisation_but_not_exact():
    score = score_report(report(
        [{"kind": "REQUEST_FAILURE", "alert_id": "a", "service": "order-service",
          "dependency": "inventory-service", "failure_code": "dependency_timeout"}]),
        pause_case())
    assert score["localisation"] == CORRECT and score["mechanism_exact"] is False


def test_wrong_dependency_is_wrong():
    score = score_report(report(
        [{"kind": "REQUEST_FAILURE", "alert_id": "a", "service": "order-service",
          "dependency": "payment-service", "failure_code": "dependency_transport"}]),
        pause_case())
    assert score["localisation"] == WRONG


def test_no_verified_claim_is_unknown_even_with_labels():
    rep = report([])
    rep["primary_hypothesis"] = None
    rep["hypotheses"] = [{"label": "自由文本提到库存"}]
    assert score_report(rep, pause_case())["localisation"] == UNKNOWN


def test_missing_report_is_no_report():
    assert score_report(None, pause_case())["localisation"] == NO_REPORT


def test_scope_limited_case_without_claims_is_scope_limited_unknown():
    assert score_report(report([]), gateway_case())["localisation"] == SCOPE_LIMITED_UNKNOWN


def test_control_is_correct_when_clean_and_false_positive_on_any_claim():
    assert score_report(report([]), control_case())["localisation"] == CORRECT
    assert score_report(report([]), control_case())["localisation"] == CORRECT
    noisy = report([])
    noisy["confidence"] = 0.4
    assert score_report(noisy, control_case())["localisation"] == FALSE_POSITIVE
    assert score_report(report(
        [{"kind": "RUNTIME_STATE", "alert_id": "a", "service": "inventory-service",
          "state": "paused"}]), control_case())["localisation"] == FALSE_POSITIVE


def test_unverified_or_unfinished_tool_results_never_score():
    rep = {"candidate_verifications": [
        {"status": "INSUFFICIENT", "claim": {"kind": "RUNTIME_STATE",
            "alert_id": "a", "service": "inventory-service", "state": "paused"},
         "supporting_evidence_ids": [], "conflicting_evidence_ids": []}],
        "primary_hypothesis": None, "confidence": 0}
    assert score_report(rep, pause_case())["localisation"] == UNKNOWN


def test_family_matcher_requires_exact_attribute_and_code_membership():
    assert claim_matches_family({"state": "paused"}, {"state": "paused"})
    assert not claim_matches_family({"state": "running"}, {"state": "paused"})
    assert claim_matches_family({"failure_code": "dependency_transport"},
                               {"failure_codes": ["dependency_transport",
                                                  "dependency_timeout"]})
    assert not claim_matches_family({"failure_code": "database_error"},
                                    {"failure_codes": ["dependency_transport"]})


def test_aggregate_never_counts_unknown_as_correct():
    manifest = load_manifest(MANIFEST)
    scores = [
        {"case_id": "ec9600c1-af74-5b82-b668-fa8e9527fc7b",
         "localisation": CORRECT, "mechanism_exact": True},
        {"case_id": "bb720e1f-2888-5512-8842-25d90d8b006e",
         "localisation": UNKNOWN, "mechanism_exact": None},
        {"case_id": "fb9dbba2-ad09-54c7-8bfb-677fc3bc8680",
         "localisation": SCOPE_LIMITED_UNKNOWN, "mechanism_exact": None},
        {"case_id": "b4064db5-a2b0-567d-b1b0-34f64506b41e",
         "localisation": CORRECT, "mechanism_exact": None},
    ]
    result = aggregate(scores, manifest)
    assert result["fault_case_count"] == 3
    assert result["localisation_accuracy"] == round(1 / 3, 4)
    assert result["unknown_rate"] == round(2 / 3, 4)
    assert result["mechanism_exact_count"] == 1
    assert result["chance_at_1"] == 0.2
    assert result["lift_over_chance"] == round(round(1 / 3, 4) / 0.2, 3)
