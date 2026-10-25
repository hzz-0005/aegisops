"""Sequential acceptance: fresh fault variants, metrics, and three governed recoveries."""

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from aegisops.config import Settings, require_dev_runtime
from scripts.evaluate_fault_battery import DB_URL

ROOT = Path(__file__).resolve().parents[1]
STAGES = {
    "correlation": ("scripts.evaluate_correlation_holdout", [], "correlation-holdout"),
    "variants": ("scripts.evaluate_fault_battery", ["--cases", "inventory_process_frozen",
                 "gateway_process_frozen", "--skip-negative-control"], "fault-battery"),
    "concurrent": ("scripts.evaluate_concurrent_faults", ["--dependency-fault", "provider-timeout"],
                   "concurrent-faults"),
    "normal": ("scripts.evaluate_fault_battery", ["--normal-checkout-only"], "fault-battery"),
    "negative": ("scripts.evaluate_fault_battery", ["--negative-control-only"], "fault-battery"),
    "redis_recovery": ("scripts.verify_lab_executor", [], None),
    "pool_recovery": ("scripts.verify_order_pool_executor", [], None),
    "deployment_recovery": ("scripts.verify_bad_deployment_executor", [], None),
}


def rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def fault_metrics(cases: list[dict]) -> dict:
    faults = [case for case in cases if case.get("mode")]
    incidents: dict[str, dict] = {}
    for case in faults:
        allowed = set(case.get("expected_fault_domains", []))
        for item in case["alerts"]:
            if item["incident_id"]:
                key = f"{case['fault']}:{item['incident_id']}"
                incidents[key] = {**item, "allowed": allowed}
    judged = [item for item in incidents.values() if item["investigation_status"] is not None]
    supported = [item for item in judged if item["investigation_status"] == "SUPPORTED"]
    wrong = [item for item in supported if item["hypothesis"] not in item["allowed"]]
    proposed = [item for item in judged if item["plan_status"] is not None]
    wrong_actions = [item for item in proposed if item["investigation_status"] != "SUPPORTED"
                     or item["hypothesis"] not in item["allowed"]]
    delays = [case["detection_seconds_after_fault_activation"] for case in faults
              if case["detection_seconds_after_fault_activation"] is not None]
    return {
        "automatic_detection_rate": rate(sum(case["detected"] for case in faults), len(faults)),
        "detection_seconds": delays,
        "wrong_supported_domain_rate": rate(len(wrong), len(supported)),
        "wrong_supported_domain_per_judged_incident": rate(len(wrong), len(judged)),
        "abstention_rate": rate(sum(i["investigation_status"] == "INCONCLUSIVE" for i in judged),
                                len(judged)),
        "wrong_proposal_rate": rate(len(wrong_actions), len(proposed)),
        "wrong_proposal_per_judged_incident": rate(len(wrong_actions), len(judged)),
        "pending_incident_count": len(incidents) - len(judged),
        "interpretation": "领域兼容不等于精确根因；无提案分母时错误提案率未定义，不记为 0%。",
    }


def action_audit(action_id: str) -> dict:
    with psycopg.connect(DB_URL) as connection:
        row = connection.execute("""
            SELECT a.status, ap.status, ap.requested_by, ap.decided_by, ap.decision_reason,
                   count(t.id), bool_and(t.fencing_token > 0), bool_and(t.state='SUCCEEDED')
            FROM actions a JOIN approvals ap ON ap.action_id=a.id
            LEFT JOIN action_attempts t ON t.action_id=a.id
            WHERE a.id=%s GROUP BY a.status,ap.status,ap.requested_by,ap.decided_by,
                                   ap.decision_reason
        """, (action_id,)).fetchone()
    assert row is not None
    status, approval, requester, decider, reason, attempts, fenced, succeeded = row
    passed = (status == "SUCCEEDED" and approval == "APPROVED" and requester != decider
              and bool(reason) and attempts == 1 and fenced is True and succeeded is True)
    return {"action_id": action_id, "action_status": status, "approval_status": approval,
            "requested_by": requester, "decided_by": decider, "reason": reason,
            "attempt_count": attempts, "fenced": fenced, "passed": passed}


def main() -> None:
    require_dev_runtime(Settings())
    parser = argparse.ArgumentParser()
    parser.add_argument("--stages", nargs="+", choices=list(STAGES), default=list(STAGES))
    parser.add_argument("--merge-reports", nargs="+", type=Path)
    args = parser.parse_args()
    now = datetime.now(UTC)
    if args.merge_reports:
        latest: dict[str, dict] = {}
        failures = []
        for path in args.merge_reports:
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data["stages"]:
                if not entry["passed"]:
                    failures.append({"stage": entry["stage"], "source_report": str(path),
                                     "log": entry["log"]})
                latest[entry["stage"]] = {**entry, "source_batch": str(path)}
        missing = sorted(set(STAGES) - latest.keys())
        for entry in latest.values():
            if entry["stage"] == "variants":
                entry["metrics"] = fault_metrics(json.loads(
                    Path(entry["report"]).read_text(encoding="utf-8"),
                ))
                metrics = entry["metrics"]
                entry["passed"] &= (metrics["pending_incident_count"] == 0
                                     and metrics["wrong_supported_domain_rate"]["numerator"] == 0
                                     and metrics["wrong_proposal_rate"]["numerator"] == 0)
            if entry["stage"] == "concurrent":
                entry["passed"] &= entry["metrics"]["wrong_action_plan_count"] == 0
            if "action_audit" in entry:
                entry["action_audit"] = action_audit(entry["action_audit"]["action_id"])
                entry["passed"] &= entry["action_audit"]["passed"]
        final = {
            "captured_at": now.isoformat(), "scope": "local_lab_acceptance",
            "stages": [latest[name] for name in STAGES if name in latest],
            "missing_stages": missing, "superseded_failures": failures,
            "model_calls_exercised": False,
            "passed": not missing and all(entry["passed"] for entry in latest.values()),
        }
        path = ROOT / "eval" / f"lab-acceptance-final-{now:%Y%m%dT%H%M%SZ}.json"
        path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": final["passed"], "stages": len(latest), "report": str(path)}))
        if not final["passed"]:
            raise SystemExit(1)
        return
    report_path = ROOT / "eval" / f"lab-acceptance-{now:%Y%m%dT%H%M%SZ}.json"
    results = []
    for name in args.stages:
        module, arguments, prefix = STAGES[name]
        before = set((ROOT / "eval").glob(f"{prefix}-*.json")) if prefix else set()
        log = ROOT / ".tmp" / f"acceptance-{now:%Y%m%dT%H%M%SZ}-{name}.log"
        log.parent.mkdir(exist_ok=True)
        print(json.dumps({"running_stage": name}), flush=True)
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run([sys.executable, "-m", module, *arguments], cwd=ROOT,
                                    stdout=stream, stderr=subprocess.STDOUT, timeout=900)
        entry = {"stage": name, "exit_code": result.returncode, "log": str(log),
                       "passed": result.returncode == 0}
        if prefix:
            created = set((ROOT / "eval").glob(f"{prefix}-*.json")) - before
            if created:
                source = max(created, key=lambda path: path.stat().st_mtime)
                data = json.loads(source.read_text(encoding="utf-8"))
                entry["report"] = str(source)
                if name == "correlation":
                    entry["metrics"] = {"replays": len(data),
                                        "passed_replays": sum(item["passed"] for item in data)}
                    entry["passed"] &= all(item["passed"] for item in data)
                elif name == "variants":
                    entry["metrics"] = fault_metrics(data)
                    entry["passed"] = entry["passed"] and all(c["detected"] for c in data)
                    entry["passed"] &= all(c["wrong_supported_root_count"] == 0 for c in data)
                    entry["passed"] &= entry["metrics"]["pending_incident_count"] == 0
                elif name == "concurrent":
                    entry["metrics"] = {k: data[k] for k in [
                        "distinct_root_incidents", "wrong_supported_root_count",
                        "wrong_action_plan_count", "recovered",
                    ]}
                    entry["passed"] &= (data["distinct_root_incidents"]
                                        and data["wrong_supported_root_count"] == 0
                                        and data["wrong_action_plan_count"] == 0
                                        and all(v == 200 for v in data["recovered"].values()))
                else:
                    entry["passed"] &= all(not c["alerts"] for c in data)
            else:
                entry["passed"] = False
        elif result.returncode == 0:
            matches = re.findall(r"action=([a-f0-9-]{36})", log.read_text(encoding="utf-8"))
            if matches:
                entry["action_audit"] = action_audit(matches[-1])
                entry["passed"] &= entry["action_audit"]["passed"]
            else:
                entry["passed"] = False
        results.append(entry)
        report_path.write_text(json.dumps({"started_at": now.isoformat(), "stages": results,
                                          "requested_stages": args.stages,
                                          "passed": all(item["passed"] for item in results)},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"completed_stage": name, "passed": entry["passed"]}), flush=True)
    print(json.dumps({"report": str(report_path)}), flush=True)
    if not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
