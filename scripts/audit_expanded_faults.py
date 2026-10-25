"""Apply an explicit evaluation manifest to persisted real-fault samples."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from aegisops.application.fault_evaluation import evaluate_fault_sample, ratio

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    samples = []
    for entry in manifest["samples"]:
        source = ROOT / entry["source_report"]
        matches = [s for s in json.loads(source.read_text(encoding="utf-8"))
                   if s["fault"] == entry["fault"] and s["repeat_index"] == entry["source_repeat"]]
        if len(matches) != 1 or matches[0].get("evaluation_failed"):
            raise ValueError("manifest must identify exactly one completed sample")
        case = matches[0]
        if case.get("preexisting_alerts"):
            raise ValueError("active baseline alerts contaminate sample")
        start = datetime.fromisoformat(case["started_at"])
        if any(datetime.fromisoformat(a["occurred_at"]) < start for a in case["alerts"]):
            raise ValueError("old alert occurrence contaminates single-fault oracle")
        roots = {a["alert_id"]: entry["injected_root"] for a in case["alerts"]}
        metrics = evaluate_fault_sample(case, alert_roots=roots,
            compatible_domains=set(entry["compatible_domains"]),
            exact_hypotheses=set(entry["exact_hypotheses"]),
            allowed_actions=set(entry["allowed_actions"]))
        samples.append({"sample_id": entry["sample_id"], "source_report": str(source),
                        "injected_root": entry["injected_root"], **metrics})
    counts: Counter[str] = Counter()
    for s in samples:
        counts.update(s["verdict_counts"])
    scored = [s["partition"] for s in samples if s["partition"] is not None]
    false_merge = sum(p["false_merged_pairs"] for p in scored)
    false_split = sum(p["false_split_pairs"] for p in scored)
    joined = sum(p["correct_joined_pairs"] for p in scored)
    judged = sum(len(s["judgments"]) - s["pending_incident_count"] for s in samples)
    report = {
        "captured_at": datetime.now(UTC).isoformat(), "manifest": str(args.manifest),
        "scope": manifest.get("evaluation_scope",
                              "additional_real_fault_variants_not_model_training"),
        "samples": samples, "aggregate": {
            "discovery_rate": ratio(sum(s["detected"] for s in samples), len(samples)),
            "detection_seconds": [s["detection_seconds"] for s in samples],
            "partition_scored_samples": ratio(len(scored), len(samples)),
            "exact_partition_match": ratio(sum(p["exact_match"] for p in scored), len(scored)),
            "false_merge_pairs": false_merge, "false_split_pairs": false_split,
            "false_merge_rate": ratio(false_merge, joined + false_merge),
            "false_split_rate": ratio(false_split, joined + false_split),
            "verdict_counts": dict(counts),
            "exact_root_coverage": ratio(counts["EXACT"], judged-counts["UNSCORABLE_ORACLE"]),
            "wrong_supported_claim_rate": ratio(counts["WRONG"],
                counts["EXACT"] + counts["DOMAIN_ONLY"] + counts["WRONG"]),
            "wrong_proposal_rate": ratio(
                sum(s["wrong_proposal_rate"]["numerator"] for s in samples),
                sum(s["wrong_proposal_rate"]["denominator"] for s in samples)),
        },
        "interpretation": ("单故障受控注入真值；症状集合不是根因真值。领域兼容不计精确根因，"
                           "未知不计成功，无动作时错误动作率未定义。"),
    }
    path = ROOT / "eval" / f"expanded-fault-audit-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"aggregate": report["aggregate"], "report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
