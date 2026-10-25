"""Replay three real Lab faults and compare their root-alert Incident partition."""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import psycopg

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)
SCENARIOS = [
    ("redis_down", "PaymentRedisUnavailable"),
    ("db_pool_exhaustion", "OrderDbPoolExhausted"),
    ("bad_deployment", "OrderBadDeployment"),
]


def await_root_incident(alert_name: str, since: datetime) -> UUID:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with psycopg.connect(DATABASE_URL) as connection:
            rows = connection.execute(
                """
                SELECT a.payload, ia.incident_id
                FROM alerts a JOIN incident_alerts ia ON ia.alert_id = a.id
                WHERE a.received_at >= %s
                ORDER BY a.received_at DESC
                """,
                (since,),
            ).fetchall()
        for payload, incident_id in rows:
            if payload.get("alertname") == alert_name:
                return incident_id
        time.sleep(1)
    raise RuntimeError(f"real {alert_name} alert did not reach an Incident")


def main() -> None:
    result: dict[str, UUID] = {}
    for scenario, alert_name in SCENARIOS:
        started = datetime.now(UTC)
        subprocess.run(
            [sys.executable, "-m", f"lab.scenarios.{scenario}"],
            cwd=ROOT,
            check=True,
        )
        result[scenario] = await_root_incident(alert_name, started)
    if len(set(result.values())) != 3:
        raise RuntimeError(f"three real fault domains merged into one Incident: {result}")
    print("three real fault domains formed separate Incidents:", result)


if __name__ == "__main__":
    main()
