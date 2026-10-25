"""Exercise webhook deduplication, outbox, RabbitMQ, inbox, and incident correlation."""

import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg

API = "http://127.0.0.1:8000"
DATABASE_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)
TOKEN = os.getenv("AEGISOPS_ALERT_WEBHOOK_TOKEN", "aegisops_dev_only")


def notification(fingerprint: str, started: str) -> dict:
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": fingerprint,
                "startsAt": started,
                "labels": {
                    "alertname": "PipelineProbe",
                    "service": "payment-service",
                    "severity": "warning",
                },
                "annotations": {"summary": "Synthetic ingestion probe"},
            }
        ],
    }


def wait_for_rows(fingerprints: tuple[str, str]) -> tuple[str, int]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with psycopg.connect(DATABASE_URL) as connection:
            rows = connection.execute(
                """
                SELECT a.fingerprint, ia.incident_id::text, o.published_at, im.id,
                       a.source_event_id, a.schema_version, a.occurred_at, a.received_at
                FROM alerts a
                JOIN incident_alerts ia ON ia.alert_id = a.id
                JOIN outbox_events o ON o.payload->>'alert_id' = a.id::text
                JOIN inbox_messages im ON im.message_id = o.id
                WHERE a.fingerprint IN (%s, %s)
                """,
                fingerprints,
            ).fetchall()
        if len(rows) == 2 and all(row[2] is not None for row in rows):
            if any(
                row[5] != 1 or row[6] > row[7] or not row[4].startswith(row[0] + ":")
                for row in rows
            ):
                raise RuntimeError("normalized alert identity or event timestamps are invalid")
            incident_ids = {row[1] for row in rows}
            if len(incident_ids) != 1:
                raise RuntimeError("same-service alerts did not correlate to one incident")
            return rows[0][1], len(rows)
        time.sleep(1)
    raise RuntimeError("alert pipeline did not finish within 30 seconds")


def main() -> None:
    fingerprints = (uuid4().hex, uuid4().hex)
    started = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with httpx.Client(timeout=5) as client:
        unauthorized = client.post(
            f"{API}/api/v1/alerts/ingest", json=notification(fingerprints[0], started)
        )
        if unauthorized.status_code != 401:
            raise RuntimeError("webhook accepted an unauthenticated request")
        first = client.post(
            f"{API}/api/v1/alerts/ingest",
            json=notification(fingerprints[0], started),
            headers=headers,
        )
        duplicate = client.post(
            f"{API}/api/v1/alerts/ingest",
            json=notification(fingerprints[0], started),
            headers=headers,
        )
        second = client.post(
            f"{API}/api/v1/alerts/ingest",
            json=notification(fingerprints[1], started),
            headers=headers,
        )
        if (
            first.status_code != 202
            or first.json() != {"accepted": 1, "duplicates": 0}
            or duplicate.json() != {"accepted": 0, "duplicates": 1}
            or second.json() != {"accepted": 1, "duplicates": 0}
        ):
            raise RuntimeError("webhook acceptance or deduplication failed")
        incident_id, attached = wait_for_rows(fingerprints)
        detail = client.get(f"{API}/api/v1/incidents/{incident_id}", headers=headers)
        timeline = client.get(f"{API}/api/v1/incidents/{incident_id}/timeline", headers=headers)
        if detail.status_code != 200 or timeline.status_code != 200:
            raise RuntimeError("incident read API failed")
        if len([item for item in timeline.json() if item["event_type"] == "ALERT_ATTACHED"]) < 2:
            raise RuntimeError("incident timeline is missing attached alerts")
        print(
            f"incident={incident_id} attached={attached} "
            f"status={detail.json()['status']} outbox=inbox=verified"
        )


if __name__ == "__main__":
    main()
