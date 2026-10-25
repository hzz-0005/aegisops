"""Verify a second real fault reaches Incident without a Redis-specific ingestion path."""

import json
import os
import time
from uuid import uuid4

import httpx
import psycopg

from lab.scenarios.redis_down import GATEWAY, PROMETHEUS, compose, wait_for

ALERTMANAGER = "http://127.0.0.1:9093"
DATABASE_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)


def checkout(client: httpx.Client) -> httpx.Response:
    return client.post(
        f"{GATEWAY}/checkout",
        json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
    )


def metric_value(client: httpx.Client) -> float:
    response = client.get(
        f"{PROMETHEUS}/api/v1/query",
        params={"query": 'sum(lab_order_inventory_failures_total{service="order-service"})'},
    )
    response.raise_for_status()
    results = response.json()["data"]["result"]
    return float(results[0]["value"][1]) if results else 0.0


def alert_fingerprint(client: httpx.Client) -> str | None:
    response = client.get(f"{ALERTMANAGER}/api/v2/alerts")
    response.raise_for_status()
    for alert in response.json():
        if alert["labels"].get("alertname") == "OrderInventoryUnavailable":
            return str(alert["fingerprint"])
    return None


def incident_for(fingerprint: str) -> str | None:
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            """
            SELECT ia.incident_id::text
            FROM alerts a
            JOIN incident_alerts ia ON ia.alert_id = a.id
            JOIN outbox_events o ON o.payload->>'alert_id' = a.id::text
            JOIN inbox_messages im ON im.message_id = o.id
            WHERE a.fingerprint = %s AND a.payload->>'alertname' = 'OrderInventoryUnavailable'
              AND o.published_at IS NOT NULL
            ORDER BY a.created_at DESC LIMIT 1
            """,
            (fingerprint,),
        ).fetchone()
    return row[0] if row else None


def main() -> None:
    with httpx.Client(timeout=15) as client:
        if checkout(client).status_code != 200:
            raise RuntimeError("baseline checkout failed")
        wait_for("inventory failure metric", lambda: metric_value(client) >= 0)
        before = metric_value(client)
        compose("stop", "inventory-service")
        try:
            failed = checkout(client)
            if failed.status_code != 502:
                raise RuntimeError(f"expected checkout 502, got {failed.status_code}")
            wait_for("inventory failure metric increment", lambda: metric_value(client) > before)
            fingerprint: str | None = None

            def delivered() -> bool:
                nonlocal fingerprint
                fingerprint = alert_fingerprint(client)
                return fingerprint is not None and incident_for(fingerprint) is not None

            wait_for("inventory alert and Incident", delivered, seconds=45)
            assert fingerprint is not None
            incident_id = incident_for(fingerprint)
        finally:
            compose("start", "inventory-service")
        wait_for("inventory recovery", lambda: checkout(client).status_code == 200)
        print(
            json.dumps(
                {
                    "fault": "inventory-service unavailable",
                    "checkout_during_fault": 502,
                    "alert": "OrderInventoryUnavailable",
                    "incident_id": incident_id,
                    "outbox_inbox": "verified",
                    "recovered": True,
                    "verified_at": int(time.time()),
                }
            )
        )


if __name__ == "__main__":
    main()
