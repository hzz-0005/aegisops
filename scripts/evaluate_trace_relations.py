"""Audit broad-window trace overlap observations against recorded Lab fault windows."""

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from aegisops.config import Settings

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8000"


def recorded_cases() -> list[dict]:
    cases: list[dict] = []
    for path in sorted((ROOT / "eval").glob("fault-battery-*.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))
        for index, row in enumerate(rows, start=1):
            if (
                row.get("fault") in {"normal_checkout_control", "unknown_sku_negative_control"}
                or row.get("preexisting_alerts")
                or not row.get("recovered")
            ):
                continue
            ids = sorted({
                item["incident_id"]
                for item in row.get("alerts", [])
                if item.get("incident_id")
            })
            if ids:
                cases.append({
                    "key": f"{path.name}#{index}",
                    "fault": row["fault"],
                    "incident_ids": ids,
                })
    return cases


def concurrent_root_pairs() -> list[dict]:
    pairs: list[dict] = []
    for path in sorted((ROOT / "eval").glob("concurrent-faults-*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        alerts = row.get("alerts", [])
        contract_ids = {
            item["incident_id"] for item in alerts
            if item["alert"] == "OrderBusinessContractRejected" and item.get("incident_id")
        }
        dependency_ids = {
            item["incident_id"] for item in alerts
            if item["alert"] == "OrderDependencyFailure" and item.get("incident_id")
        }
        if len(contract_ids) != 1 or len(dependency_ids) != 1:
            continue
        pairs.append({
            "report": path.name,
            "contract_incident_id": next(iter(contract_ids)),
            "dependency_incident_id": next(iter(dependency_ids)),
            "root_incidents_distinct": contract_ids.isdisjoint(dependency_ids),
        })
    return pairs


def main() -> None:
    settings = Settings()
    if not settings.alert_webhook_token:
        raise RuntimeError("AEGISOPS_ALERT_WEBHOOK_TOKEN is required")
    cases = recorded_cases()
    owner: dict[str, dict] = {}
    for case in cases:
        for incident_id in case["incident_ids"]:
            if incident_id in owner:
                raise RuntimeError(f"incident belongs to multiple fault windows: {incident_id}")
            owner[incident_id] = case
    edges: list[dict] = []
    concurrent_pairs = concurrent_root_pairs()
    unmapped_kind: dict[str, str] = {}
    with httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {settings.alert_webhook_token}"},
        timeout=20,
        trust_env=False,
    ) as client:
        for incident_id, source in sorted(owner.items()):
            response = client.get(f"/api/v1/incidents/{incident_id}/trace-overlaps")
            response.raise_for_status()
            for observation in response.json():
                target_id = observation["other_incident_id"]
                target = owner.get(target_id)
                if target is None and target_id not in unmapped_kind:
                    target_response = client.get(
                        f"/api/v1/incidents/{target_id}/investigation"
                    )
                    target_response.raise_for_status()
                    alert_names = [
                        item.get("payload", {}).get("alert_name", "")
                        for item in target_response.json().get("evidence", [])
                        if item.get("source_type") == "ALERT"
                    ]
                    unmapped_kind[target_id] = (
                        "SYNTHETIC_HOLDOUT"
                        if alert_names and all(name.endswith("Holdout") for name in alert_names)
                        else "UNKNOWN"
                    )
                classification = (
                    f"UNMAPPED_{unmapped_kind[target_id]}" if target is None else
                    "SAME_FAULT_WINDOW" if target["key"] == source["key"] else
                    "CROSS_FAULT_WINDOW"
                )
                edges.append({
                    "source_incident_id": incident_id,
                    "target_incident_id": target_id,
                    "source_case": source["key"],
                    "source_fault": source["fault"],
                    "target_case": target["key"] if target else None,
                    "target_fault": target["fault"] if target else None,
                    "classification": classification,
                    "shared_trace_refs": [
                        item["source_ref"] for item in observation["overlapping_traces"]
                    ],
                    "source_report_id": observation["source_report_id"],
                    "target_report_id": observation["other_report_id"],
                })
        for pair in concurrent_pairs:
            response = client.get(
                f"/api/v1/incidents/{pair['contract_incident_id']}/trace-overlaps"
            )
            response.raise_for_status()
            cross = next(
                (
                    item for item in response.json()
                    if item["other_incident_id"] == pair["dependency_incident_id"]
                ),
                None,
            )
            pair["cross_root_trace_overlap_count"] = (
                len(cross["overlapping_traces"]) if cross else 0
            )
    counts: dict[str, int] = defaultdict(int)
    for edge in edges:
        counts[edge["classification"]] += 1
    result = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "rule_version": "trace-observation-overlap-v2",
        "fault_windows": len(cases),
        "incidents": len(owner),
        "directed_edge_counts": dict(counts),
        "concurrent_root_pairs": concurrent_pairs,
        "edges": edges,
    }
    output = ROOT / "eval" / f"trace-relations-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "edges"}))
    print(f"report={output}")


if __name__ == "__main__":
    main()
