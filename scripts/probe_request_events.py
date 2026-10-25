"""Verify request-level failure facts from real Lab DNS injection and Tempo."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg

from aegisops.config import Settings
from scripts.evaluate_fault_battery import (
    DB_URL,
    checkout,
    set_payment_dns_fault,
)

ROOT = Path(__file__).resolve().parents[1]


def new_event_ids(started_at: datetime) -> list[str]:
    with psycopg.connect(DB_URL) as connection:
        rows = connection.execute(
            "SELECT id::text FROM request_failure_events WHERE created_at >= %s "
            "ORDER BY created_at, id",
            (started_at,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def main() -> None:
    token = Settings().alert_webhook_token
    if not token:
        raise RuntimeError("read token is unavailable")
    with httpx.Client(timeout=12, trust_env=False) as client:
        if checkout(client, sku="sku-headphones") != 200:
            raise RuntimeError("baseline checkout failed")
        started_at = datetime.now(UTC)
        try:
            set_payment_dns_fault(client, True)
            fault_status = checkout(client, sku="sku-headphones")
        finally:
            set_payment_dns_fault(client, False)
        if checkout(client, sku="sku-headphones") != 200:
            raise RuntimeError("checkout did not recover")
        deadline = time.monotonic() + 30
        ids: list[str] = []
        while time.monotonic() < deadline:
            ids = new_event_ids(started_at)
            if len(ids) >= 2:
                break
            time.sleep(1)
        records = []
        for event_id in ids:
            path = f"http://127.0.0.1:8000/api/v1/request-events/{event_id}"
            response = client.get(path, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            records.append(response.json())
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for record in records:
                response = client.get(
                    f"http://127.0.0.1:8000/api/v1/request-events/{record['id']}/verification",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                record["provenance_status"] = response.json()["provenance_status"]
            if records and all(
                record["provenance_status"] == "VERIFIED_SPAN_FACT" for record in records
            ):
                break
            time.sleep(2)
    result = {
        "started_at": started_at.isoformat(),
        "fault_checkout_status": fault_status,
        "events": records,
        "same_request_trace": len({record["trace_id"] for record in records}) == 1,
    }
    output = ROOT / "eval" / f"request-events-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "fault_checkout_status": fault_status,
        "events": [(record["service"], record["failure_code"], record["provenance_status"])
                   for record in records],
        "same_request_trace": result["same_request_trace"],
    }, ensure_ascii=False))
    print(f"report={output}")


if __name__ == "__main__":
    main()
