"""Replay held-out alert partitions through the real ingest and correlation workers."""

import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

API = "http://127.0.0.1:8000/api/v1/alerts/ingest"
DATABASE_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)
TOKEN = os.getenv("AEGISOPS_ALERT_WEBHOOK_TOKEN", "aegisops_dev_only")
ROOT = Path(__file__).resolve().parents[1]

CASES: dict[str, list[tuple[str, str, str, str, str | None, str]]] = {
    "related_deployment_symptom": [
        ("fault", "ReleaseRelatedHoldout", "order-service", "service:order-service", "same",
         "deployment"),
        ("symptom", "SymptomRelatedHoldout", "lab-gateway", "service:order-service", "same",
         "availability"),
    ],
    "related_symptom_conflicting_changes": [
        ("fault", "ReleaseConflictHoldout", "order-service", "service:order-service", "first",
         "deployment"),
        ("symptom", "SymptomConflictHoldout", "lab-gateway", "service:order-service", "second",
         "availability"),
    ],
    "same_change_conflicting_fault_domains": [
        ("network", "NetworkBoundaryHoldout", "order-service", "shared", "same", "availability"),
        ("pool", "PoolBoundaryHoldout", "order-service", "shared", "same", "saturation"),
    ],
    "same_change_unrelated_services": [
        ("order", "OrderBoundaryHoldout", "lab-gateway", "shared", "same", "deployment"),
        ("inventory", "InventoryBoundaryHoldout", "inventory-service", "shared", "same",
         "deployment"),
    ],
    "three_domains_shared_change": [
        ("network", "NetworkTripleHoldout", "order-service", "shared", "same", "availability"),
        ("pool", "PoolTripleHoldout", "order-service", "shared", "same", "saturation"),
        ("release", "ReleaseTripleHoldout", "order-service", "shared", "same", "deployment"),
    ],
    "same_resource_different_faults": [
        (
            "database_unreachable", "DatabaseReachabilityHoldout", "order-service",
            "shared", None, "availability",
        ),
        (
            "pool_saturated", "PoolSaturationHoldout", "order-service",
            "shared", None, "saturation",
        ),
    ],
    "different_resources_same_change": [
        ("api_error", "OrderApiFailureHoldout", "order-service", "api", "same", "deployment"),
        ("cache_error", "OrderCacheFailureHoldout", "order-service", "cache", "same", "deployment"),
    ],
    "same_resource_different_changes": [
        (
            "first_change", "FirstDeploymentHoldout", "order-service",
            "shared", "first", "deployment",
        ),
        (
            "second_change", "SecondDeploymentHoldout", "order-service",
            "shared", "second", "deployment",
        ),
    ],
}
EXPECTED = {
    "related_deployment_symptom": [["fault", "symptom"]],
    "related_symptom_conflicting_changes": [["fault"], ["symptom"]],
    "same_change_conflicting_fault_domains": [["network"], ["pool"]],
    "same_change_unrelated_services": [["inventory"], ["order"]],
    "three_domains_shared_change": [["network"], ["pool"], ["release"]],
    "same_resource_different_faults": [["database_unreachable"], ["pool_saturated"]],
    "different_resources_same_change": [["api_error", "cache_error"]],
    "same_resource_different_changes": [["first_change"], ["second_change"]],
}


def partition(fingerprints: dict[str, str]) -> tuple[list[list[str]], list[dict]] | None:
    with psycopg.connect(DATABASE_URL) as connection:
        rows = connection.execute(
            """
            SELECT a.fingerprint, ia.incident_id::text,
                   d.decision, d.total_score, d.matched_features, d.conflicting_features
            FROM alerts a
            JOIN incident_alerts ia ON ia.alert_id = a.id
            JOIN correlation_decisions d ON d.alert_id = a.id
            WHERE a.fingerprint = ANY(%s)
            """,
            (list(fingerprints.values()),),
        ).fetchall()
    if len(rows) != len(fingerprints):
        return None
    reverse = {value: key for key, value in fingerprints.items()}
    groups: dict[str, list[str]] = defaultdict(list)
    decisions = []
    for fingerprint, incident_id, decision, score, matched, conflicting in rows:
        logical_id = reverse[fingerprint]
        groups[incident_id].append(logical_id)
        decisions.append(
            {
                "alert": logical_id,
                "decision": decision,
                "score": float(score),
                "matched": matched,
                "conflicting": conflicting,
            }
        )
    return sorted([sorted(group) for group in groups.values()]), sorted(
        decisions, key=lambda item: item["alert"]
    )


def replay(name: str, *, reversed_order: bool) -> dict:
    run = uuid4().hex[:12]
    specs = CASES[name]
    positions = {spec[0]: position for position, spec in enumerate(specs)}
    fingerprints = {logical_id: uuid4().hex for logical_id, *_ in specs}
    occurred = datetime.now(UTC) - timedelta(minutes=1)
    ordered = list(reversed(specs)) if reversed_order else specs
    with httpx.Client(timeout=15) as client:
        for logical_id, alert_name, service, resource, change, signal_type in ordered:
            labels = {
                "alertname": alert_name,
                "service": service,
                "severity": "critical",
                "resource": resource.split(":", 1)[1] if resource.startswith("service:")
                else f"{resource}-{run}",
                "signal_type": signal_type,
            }
            if change:
                labels["change_id"] = f"{change}-{run}"
            if name in {"related_deployment_symptom", "related_symptom_conflicting_changes"}:
                labels["signal_role"] = logical_id
            body = {
                "version": "4",
                "status": "firing",
                "alerts": [{
                    "status": "firing",
                    "fingerprint": fingerprints[logical_id],
                    "startsAt": (occurred + timedelta(seconds=positions[logical_id])).isoformat(),
                    "labels": labels,
                    "annotations": {"summary": "Held-out checkout failure signal"},
                }],
            }
            response = client.post(API, headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
            response.raise_for_status()
            if response.json().get("accepted") != 1:
                raise RuntimeError(f"alert not accepted: {name}/{logical_id}")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = partition(fingerprints)
        if result is not None:
            actual, decisions = result
            return {
                "case": name,
                "delivery": "reverse" if reversed_order else "normal",
                "expected": EXPECTED[name],
                "actual": actual,
                "passed": actual == sorted([sorted(group) for group in EXPECTED[name]]),
                "decisions": decisions,
            }
        time.sleep(1)
    raise RuntimeError(f"correlation timed out: {name}")


def main() -> None:
    results = [
        replay(name, reversed_order=reverse)
        for name in CASES
        for reverse in (False, True)
    ]
    output = ROOT / "eval" / f"correlation-holdout-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report={output}")
    summary = [
        {"case": item["case"], "delivery": item["delivery"], "passed": item["passed"]}
        for item in results
    ]
    print(json.dumps(summary, ensure_ascii=False))
    if not all(result["passed"] for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
