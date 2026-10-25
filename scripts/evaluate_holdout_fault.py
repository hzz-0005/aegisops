"""Measure a real unseen Lab outage without adding a root-cause rule first."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg

from lab.scenarios.redis_down import GATEWAY, compose, wait_for

CONSOLE = "http://127.0.0.1:8010"
DB_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)


def checkout(client: httpx.Client) -> int:
    return client.post(
        f"{GATEWAY}/checkout",
        json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
    ).status_code


def incident_for(fingerprint: str) -> str | None:
    with psycopg.connect(DB_URL) as connection:
        row = connection.execute(
            """
            SELECT ia.incident_id::text FROM alerts a
            JOIN incident_alerts ia ON ia.alert_id = a.id
            WHERE a.fingerprint = %s ORDER BY a.created_at DESC LIMIT 1
            """,
            (fingerprint,),
        ).fetchone()
    return row[0] if row else None


def details(client: httpx.Client, incident_id: str) -> dict:
    response = client.get(f"{CONSOLE}/api/incidents/{incident_id}")
    response.raise_for_status()
    return response.json()


def metric(client: httpx.Client, name: str) -> float:
    response = client.get(
        "http://127.0.0.1:9090/api/v1/query", params={"query": f"sum({name})"}
    )
    response.raise_for_status()
    values = response.json()["data"]["result"]
    return float(values[0]["value"][1]) if values else 0.0


def active_alert_names(client: httpx.Client) -> list[str]:
    response = client.get("http://127.0.0.1:9093/api/v2/alerts")
    response.raise_for_status()
    return sorted({item["labels"].get("alertname", "") for item in response.json()})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fault", choices=["payment-service", "lab-orders-postgres"])
    args = parser.parse_args()
    result: dict = {"fault": args.fault, "started_at": datetime.now(UTC).isoformat()}
    with httpx.Client(timeout=15) as client:
        if checkout(client) != 200:
            raise RuntimeError("healthy baseline checkout required")
        metrics = (
            "lab_order_db_pool_lease_saturation_total",
            "lab_order_db_connectivity_failures_total",
        )
        before = {name: metric(client, name) for name in metrics}
        before_alerts = set(active_alert_names(client))
        result["preexisting_prometheus_alerts"] = sorted(before_alerts)
        compose("stop", args.fault)
        try:
            result["checkout_during_fault"] = checkout(client)
            result["control_api_during_fault"] = client.get(
                "http://127.0.0.1:8000/api/v1/health/ready"
            ).status_code
            time.sleep(8)
            result["second_checkout_during_fault"] = checkout(client)
            time.sleep(15)  # Let the existing scrape and rule intervals observe both requests.
            active_alerts = set(active_alert_names(client))
            result["active_prometheus_alerts"] = sorted(active_alerts)
            result["new_prometheus_alert_names"] = sorted(active_alerts - before_alerts)
            result["metric_deltas"] = {
                name: metric(client, name) - before[name] for name in metrics
            }
            marker = uuid4().hex[:8]
            response = client.post(
                f"{CONSOLE}/api/lab-alerts",
                headers={"X-AegisOps-Console": "lab-console-v1"},
                json={
                    "service": "order-service",
                    "alert_name": "CheckoutFailureSpike",
                    "resource": f"checkout-window-{marker}",
                    "signal_type": "availability",
                    "severity": "critical",
                    "summary": "Checkout requests returning 5xx",
                },
            )
            response.raise_for_status()
            fingerprint = response.json()["fingerprint"]
            wait_for("holdout incident", lambda: incident_for(fingerprint) is not None, seconds=50)
            incident_id = incident_for(fingerprint)
            assert incident_id
            wait_for(
                "holdout investigation",
                lambda: details(client, incident_id)["investigation"] is not None,
                seconds=50,
            )
            detail = details(client, incident_id)
            report = detail["investigation"]
            advice = detail["advice"]
            result.update(
                {
                    "injected_alert": "CheckoutFailureSpike",
                    "incident_id": incident_id,
                    "incident_alert_count": detail["incident"]["alert_count"],
                    "investigation_status": report["status"],
                    "hypothesis": report["primary_hypothesis"],
                    "confidence": report["confidence"],
                    "evidence_kinds": sorted({item["kind"] for item in report["evidence"]}),
                    "limitations": report["limitations"],
                    "advice_mode": advice["mode"],
                    "advice_titles": [item["title"] for item in advice["items"]],
                    "execution_allowed": advice["execution_allowed"],
                    "remediation_plan": detail["remediation_plan"] is not None,
                }
            )
        finally:
            compose("start", args.fault)
        wait_for("checkout recovery", lambda: checkout(client) == 200, seconds=50)
        result["recovered"] = True
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
