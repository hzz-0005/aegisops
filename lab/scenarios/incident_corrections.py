"""Exercise versioned split and merge while retaining the original correlation trail."""

import os
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import psycopg

API = "http://127.0.0.1:8000/api/v1"
DATABASE_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)
WEBHOOK_TOKEN = os.getenv("AEGISOPS_ALERT_WEBHOOK_TOKEN", "aegisops_dev_only")
OPERATOR_TOKEN = os.getenv("AEGISOPS_OPERATOR_TOKEN", "aegisops_operator_dev_only")


def wait_for_incident(fingerprints: list[str]) -> tuple[UUID, dict[str, UUID]]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with psycopg.connect(DATABASE_URL) as connection:
            rows = connection.execute(
                """
                SELECT a.fingerprint, a.id, ia.incident_id
                FROM alerts a JOIN incident_alerts ia ON ia.alert_id = a.id
                WHERE a.fingerprint = ANY(%s)
                """,
                (fingerprints,),
            ).fetchall()
        if len(rows) == 2:
            incident_ids = {row[2] for row in rows}
            if len(incident_ids) != 1:
                raise RuntimeError("setup alerts did not correlate")
            return next(iter(incident_ids)), {row[0]: row[1] for row in rows}
        time.sleep(1)
    raise RuntimeError("setup alerts were not correlated")


def wait_for_alert_incident(fingerprint: str) -> tuple[UUID, str]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with psycopg.connect(DATABASE_URL) as connection:
            row = connection.execute(
                """
                SELECT ia.incident_id, cd.decision
                FROM alerts a
                JOIN incident_alerts ia ON ia.alert_id = a.id
                JOIN correlation_decisions cd ON cd.alert_id = a.id
                WHERE a.fingerprint = %s
                """,
                (fingerprint,),
            ).fetchone()
        if row:
            return row[0], row[1]
        time.sleep(1)
    raise RuntimeError("follow-up alert was not correlated")


def main() -> None:
    run = uuid4().hex
    fingerprints = [uuid4().hex, uuid4().hex]
    started = datetime.now(UTC).isoformat()
    operator = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
    reader = {"Authorization": f"Bearer {WEBHOOK_TOKEN}"}
    with httpx.Client(timeout=5) as client:
        for index, fingerprint in enumerate(fingerprints):
            response = client.post(
                f"{API}/alerts/ingest",
                headers=reader,
                json={
                    "version": "4",
                    "status": "firing",
                    "alerts": [
                        {
                            "status": "firing",
                            "fingerprint": fingerprint,
                            "startsAt": started,
                            "labels": {
                                "alertname": f"CorrectionProbe{index}",
                                "service": "order-service",
                                "resource": f"probe-{run}",
                                "severity": "warning",
                            },
                        }
                    ],
                },
            )
            if response.status_code != 202:
                raise RuntimeError(f"webhook failed: {response.status_code}")
        source_id, alert_ids = wait_for_incident(fingerprints)
        source = client.get(f"{API}/incidents/{source_id}", headers=reader).json()
        split_body = {
            "expected_version": source["version"],
            "alert_ids": [str(alert_ids[fingerprints[1]])],
            "title": "Separate correction probe",
            "reason": "Second alert has a separate verified cause",
        }
        unauthorized = client.post(f"{API}/incidents/{source_id}/split", json=split_body)
        if unauthorized.status_code != 401:
            raise RuntimeError("split accepted a request without operator credentials")
        stale = client.post(
            f"{API}/incidents/{source_id}/split",
            headers=operator,
            json={**split_body, "expected_version": source["version"] + 1},
        )
        if stale.status_code != 409:
            raise RuntimeError("split accepted a stale or incorrect version")
        split = client.post(f"{API}/incidents/{source_id}/split", headers=operator, json=split_body)
        if split.status_code != 200 or split.json()["alerts_moved"] != 1:
            raise RuntimeError(f"split failed: {split.status_code} {split.text}")
        new_id = split.json()["incident_id"]
        old_after = client.get(f"{API}/incidents/{source_id}", headers=reader).json()
        new_after = client.get(f"{API}/incidents/{new_id}", headers=reader).json()
        if old_after["alert_count"] != 1 or new_after["alert_count"] != 1:
            raise RuntimeError("split did not move exactly one current alert link")
        follow_up_fingerprint = uuid4().hex
        follow_up = client.post(
            f"{API}/alerts/ingest",
            headers=reader,
            json={
                "version": "4",
                "status": "firing",
                "alerts": [
                    {
                        "status": "firing",
                        "fingerprint": follow_up_fingerprint,
                        "startsAt": started,
                        "labels": {
                            "alertname": "CorrectionFollowUp",
                            "service": "order-service",
                            "resource": f"probe-{run}",
                            "severity": "warning",
                        },
                    }
                ],
            },
        )
        if follow_up.status_code != 202:
            raise RuntimeError(f"follow-up webhook failed: {follow_up.status_code}")
        follow_up_incident, decision = wait_for_alert_incident(follow_up_fingerprint)
        if follow_up_incident in (source_id, UUID(new_id)) or decision != "CREATE_NEW_AMBIGUOUS":
            raise RuntimeError("ambiguous follow-up undid the manual split")
        merged = client.post(
            f"{API}/incidents/{new_id}/merge",
            headers=operator,
            json={
                "target_incident_id": str(source_id),
                "expected_source_version": new_after["version"],
                "expected_target_version": old_after["version"],
                "reason": "Manual review confirmed a common incident",
            },
        )
        if merged.status_code != 200 or merged.json()["alerts_moved"] != 1:
            raise RuntimeError(f"merge failed: {merged.status_code} {merged.text}")
        source_final = client.get(f"{API}/incidents/{source_id}", headers=reader).json()
        superseded = client.get(f"{API}/incidents/{new_id}", headers=reader).json()
        relation_response = client.get(f"{API}/incidents/{new_id}/relations", headers=reader)
        timeline_response = client.get(f"{API}/incidents/{new_id}/timeline", headers=reader)
        if source_final["alert_count"] != 2 or superseded["status"] != "SUPERSEDED":
            raise RuntimeError("merge final state is incorrect")
        relations = relation_response.json()
        timeline = timeline_response.json()
        if {item["relation_type"] for item in relations} != {"SPLIT_FROM", "MERGED_INTO"}:
            raise RuntimeError("split/merge relations are incomplete")
        if not {"SPLIT_IN", "MERGED_OUT"}.issubset({item["event_type"] for item in timeline}):
            raise RuntimeError("immutable correction timeline is incomplete")
        if not all(item["reason"] and item["operator_id"] for item in relations):
            raise RuntimeError("operator or reason missing from relation audit")
        print(f"split and merge verified; source={source_id} superseded={new_id}")


if __name__ == "__main__":
    main()
