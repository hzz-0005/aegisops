"""Evaluation-only ground truth never enters the investigation or correlation engine."""

from collections import Counter, defaultdict

from aegisops.application.partition_evaluation import evaluate_partition


def ratio(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def evaluate_fault_sample(sample: dict, *, alert_roots: dict[str, str],
                          compatible_domains: set[str], exact_hypotheses: set[str],
                          allowed_actions: set[str]) -> dict:
    alerts = sample["alerts"]
    ids = [a["alert_id"] for a in alerts]
    if len(set(ids)) != len(ids) or any(not value for value in ids):
        raise ValueError("observed alerts require unique persisted identities")
    if not set(alert_roots) <= set(ids) or any(not value for value in alert_roots.values()):
        raise ValueError("oracle identities must refer to observed alerts")
    expected = defaultdict(list)
    actual = defaultdict(list)
    incidents = defaultdict(list)
    missing = []
    for alert in alerts:
        alert_id = alert["alert_id"]
        incident_id = alert["incident_id"]
        if incident_id:
            incidents[incident_id].append(alert)
        if alert_id not in alert_roots:
            continue
        expected[alert_roots[alert_id]].append(alert_id)
        if incident_id:
            actual[incident_id].append(alert_id)
        else:
            missing.append(alert_id)
    judgments = []
    for incident_id, members in incidents.items():
        conclusions = {(a["investigation_status"], a["hypothesis"]) for a in members}
        if len(conclusions) != 1:
            raise ValueError("inconsistent incident conclusion in one captured sample")
        status, key = conclusions.pop()
        if status is None:
            verdict = "PENDING"
        elif status == "INCONCLUSIVE":
            verdict = "UNKNOWN"
        elif status != "SUPPORTED" or not key:
            raise ValueError("invalid investigation conclusion")
        elif (any(a["alert_id"] not in alert_roots for a in members)
              or len({alert_roots[a["alert_id"]] for a in members}) != 1):
            verdict = "UNSCORABLE_ORACLE"
        elif key in exact_hypotheses:
            verdict = "EXACT"
        elif key in compatible_domains:
            verdict = "DOMAIN_ONLY"
        else:
            verdict = "WRONG"
        proposed = any(a["plan_status"] is not None for a in members)
        action_types = {value for a in members for value in (a.get("action_types") or [])}
        wrong_proposal = proposed and (
            verdict != "EXACT" or not action_types or not action_types <= allowed_actions)
        judgments.append({"incident_id": incident_id, "root_verdict": verdict,
                          "primary_hypothesis": key, "proposed": proposed,
                          "wrong_proposal": wrong_proposal})
    counts = Counter(item["root_verdict"] for item in judgments)
    supported = counts["EXACT"] + counts["DOMAIN_ONLY"] + counts["WRONG"]
    judged = len(judgments) - counts["PENDING"]
    scorable = judged - counts["UNSCORABLE_ORACLE"]
    proposal_count = sum(item["proposed"] for item in judgments)
    partition = (evaluate_partition(list(expected.values()), list(actual.values()))
                 if alert_roots and not missing else None)
    return {
        "detected": bool(alerts), "recovered": sample.get("recovered", False),
        "detection_seconds": sample.get("detection_seconds_after_fault_activation"),
        "observed_alert_count": len(alerts),
        "oracle_coverage": ratio(len(alert_roots), len(alerts)),
        "unscored_alert_ids": sorted(set(ids) - set(alert_roots)),
        "unassigned_alert_ids": sorted(missing), "partition": partition,
        "judgments": judgments, "verdict_counts": dict(counts),
        "root_judgment_coverage": ratio(scorable, judged),
        "exact_root_coverage": ratio(counts["EXACT"], scorable),
        "wrong_supported_claim_rate": ratio(counts["WRONG"], supported),
        "unknown_rate": ratio(counts["UNKNOWN"], judged),
        "wrong_proposal_rate": ratio(sum(j["wrong_proposal"] for j in judgments), proposal_count),
        "pending_incident_count": counts["PENDING"],
    }
