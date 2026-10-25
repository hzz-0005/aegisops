"""Exercise three root-cause groups through the real webhook, queue, and database."""

import os
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg

API = "http://127.0.0.1:8000/api/v1/alerts/ingest"
DATABASE_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)
TOKEN = os.getenv("AEGISOPS_ALERT_WEBHOOK_TOKEN", "aegisops_dev_only")


def main() -> None:
    run = uuid4().hex[:12]
    groups = {
        "cache": [
            ("RedisDown", "payment-service", {"resource": f"redis-{run}"}),
            ("PaymentErrorRateHigh", "order-service", {"resource": f"redis-{run}"}),
        ],
        "database": [
            ("PostgresConnectionsHigh", "order-service", {"resource": f"postgres-{run}"}),
            ("OrderDatabaseTimeout", "inventory-service", {"resource": f"postgres-{run}"}),
        ],
        "deployment": [
            ("DeploymentChanged", "payment-service", {"change_id": f"deploy-{run}"}),
            ("PaymentErrorRateHigh", "payment-service", {"change_id": f"deploy-{run}"}),
        ],
    }
    fingerprint_groups: dict[str, str] = {}
    started = datetime.now(UTC).isoformat()
    with httpx.Client(timeout=5) as client:
        for group, alerts in groups.items():
            for name, service, extra in alerts:
                fingerprint = uuid4().hex
                fingerprint_groups[fingerprint] = group
                response = client.post(
                    API,
                    headers={"Authorization": f"Bearer {TOKEN}"},
                    json={
                        "version": "4",
                        "status": "firing",
                        "alerts": [
                            {
                                "status": "firing",
                                "fingerprint": fingerprint,
                                "startsAt": started,
                                "labels": {
                                    "alertname": name,
                                    "service": service,
                                    "severity": "critical",
                                    **extra,
                                },
                            }
                        ],
                    },
                )
                if response.status_code != 202 or response.json()["accepted"] != 1:
                    raise RuntimeError(f"alert rejected: {response.status_code} {response.text}")

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with psycopg.connect(DATABASE_URL) as connection:
            rows = connection.execute(
                """
                SELECT a.fingerprint, ia.incident_id::text, o.published_at, im.id
                FROM alerts a
                JOIN incident_alerts ia ON ia.alert_id = a.id
                JOIN outbox_events o ON o.payload->>'alert_id' = a.id::text
                JOIN inbox_messages im ON im.message_id = o.id
                WHERE a.fingerprint = ANY(%s)
                """,
                (list(fingerprint_groups),),
            ).fetchall()
        if len(rows) == 6 and all(row[2] is not None for row in rows):
            by_group: dict[str, set[str]] = {group: set() for group in groups}
            for fingerprint, incident_id, _, _ in rows:
                by_group[fingerprint_groups[fingerprint]].add(incident_id)
            if any(len(ids) != 1 for ids in by_group.values()):
                raise RuntimeError(f"related alerts were not grouped: {by_group}")
            if len({next(iter(ids)) for ids in by_group.values()}) != 3:
                raise RuntimeError(f"unrelated fault groups were merged: {by_group}")
            print(f"three distinct incidents, two related alerts each: {by_group}")
            return
        time.sleep(1)
    raise RuntimeError("multi-fault correlation pipeline did not finish")


if __name__ == "__main__":
    main()
