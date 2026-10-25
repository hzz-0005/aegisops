"""Aggregate blind-eval report files into one published table with full denominators."""

import argparse
import glob
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.agent_blind_scoring import (
    CORRECT,
    FALSE_POSITIVE,
    NO_REPORT,
    SCOPE_LIMITED_UNKNOWN,
    UNKNOWN,
    WRONG,
    load_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
STATUS_ORDER = [CORRECT, WRONG, UNKNOWN, SCOPE_LIMITED_UNKNOWN, FALSE_POSITIVE,
                NO_REPORT]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path,
                        default=Path("eval/agent-blind-manifest-v2.json"))
    parser.add_argument("--reports", nargs="+",
                        help="eval report files; default = all agent-blind-*.json "
                             "created after the manifest's frozen_at")
    args = parser.parse_args()
    load_manifest(args.manifest)  # validate the version before summarizing
    name_by_id = {
        "ec9600c1-af74-5b82-b668-fa8e9527fc7b": "inventory-paused",
        "bb720e1f-2888-5512-8842-25d90d8b006e": "inventory-stopped",
        "fb9dbba2-ad09-54c7-8bfb-677fc3bc8680": "gateway-paused",
        "b4064db5-a2b0-567d-b1b0-34f64506b41e": "quiet-control",
        "afd096bf-2ba0-5a7a-a252-e972ceb830ae": "payment-paused",
        "cf598aff-fe0d-5e1d-a037-bfb55722b922": "payment-stopped",
        "a96e37eb-783c-5e1a-aad5-3bb49fac6066": "provider-latency",
    }
    files = [Path(p) for p in args.reports] if args.reports else sorted(
        Path(f) for f in glob.glob(str(ROOT / "eval" / "agent-blind-*.json")))
    rows = []
    for path in files:
        if path.name.endswith("manifest-v1.json") or "manifest" in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for case in data.get("cases", []):
            score = case.get("score") or {}
            trace = case.get("trace") or {}
            rows.append({
                "file": path.name,
                "case": name_by_id.get(case["case_id"], case["case_id"][:8]),
                "case_id": case["case_id"],
                "score": score.get("localisation"),
                "mechanism_exact": score.get("mechanism_exact"),
                "model_calls": trace.get("model_calls"),
                "tool_calls": trace.get("tool_calls"),
                "tokens": trace.get("total_tokens"),
                "real_model": data.get("real_model"),
            })
    # The published denominator: only real-model runs on the current config
    # (prompt v5 + fixed verifier). Older-config files are listed separately.
    current_files = {
        "agent-blind-20261025T073231Z.json",
        "agent-blind-20261025T082242Z.json",
        "agent-blind-20261025T091223Z.json",
        "agent-blind-20261025T091139Z.json",
        "agent-blind-20261025T104311Z.json",
        "agent-blind-20261025T104919Z.json",
    }
    result: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest": str(args.manifest),
        "protocol": {"unknown_never_correct": True, "truth_separated": True,
                     "scoring": "backend-verified claims only"},
        "runs": rows,
    }
    for label, selected in (("current_config_denominator",
                             [r for r in rows if r["file"] in current_files]),
                            ("historical_v1_config",
                             [r for r in rows if r["file"] not in current_files])):
        fault = [r for r in selected if r["case"] != "quiet-control"
                 and r["score"] not in {None, NO_REPORT}]
        counts = {status: sum(1 for r in selected if r["score"] == status)
                  for status in STATUS_ORDER}
        tokens = [r["tokens"] for r in selected if r["tokens"]]
        result[label] = {
            "runs": len(selected),
            "fault_runs": len(fault),
            "counts": counts,
            "localisation_accuracy": (round(sum(1 for r in fault
                                                if r["score"] == CORRECT) / len(fault), 4)
                                      if fault else None),
            "mechanism_exact": sum(1 for r in fault if r["mechanism_exact"]),
            "unknown_rate": (round(sum(1 for r in fault if r["score"] in
                                       {UNKNOWN, SCOPE_LIMITED_UNKNOWN}) / len(fault), 4)
                             if fault else None),
            "mean_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
        }
    output = ROOT / "eval" / (
        f"agent-blind-summary-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(json.dumps({k: result[k] for k in
                      ("current_config_denominator", "historical_v1_config")},
                     ensure_ascii=False, indent=1))
    print("saved:", output.name)


if __name__ == "__main__":
    main()
