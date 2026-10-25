"""Probe whether two independent faults share a gateway symptom signal."""

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import psycopg

from aegisops.application.request_derived_signals import list_request_signals
from aegisops.config import Settings
from scripts.evaluate_fault_battery import (
    DB_URL,
    checkout,
    inventory_currency,
    set_inventory_currency,
    set_payment_dns_fault,
)

ROOT = Path(__file__).resolve().parents[1]


def failure_events(trace_ids: list[str]) -> list[dict]:
    with psycopg.connect(DB_URL) as connection:
        rows = connection.execute(
            "SELECT id, trace_id, service_name, failure_code, resource_name, occurred_at "
            "FROM request_failure_events WHERE tenant_id = %s AND trace_id = ANY(%s) "
            "ORDER BY occurred_at, id",
            (Settings().dev_tenant_id, trace_ids),
        ).fetchall()
    return [
        {
            "id": str(row[0]), "trace_id": row[1], "service": row[2],
            "failure_code": row[3], "resource": row[4],
            "occurred_at": row[5].isoformat(),
        }
        for row in rows
    ]


def run() -> dict:
    now = datetime.now(UTC)
    window_start = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
    if window_start + timedelta(minutes=10) - now < timedelta(seconds=90):
        raise RuntimeError("less than 90 seconds remain in detector window; retry next window")
    if inventory_currency() != "CNY":
        raise RuntimeError("sku-demo currency baseline is not CNY")
    traces: dict[str, list[str]] = {"contract_drift": [], "payment_dns": []}
    statuses: dict[str, list[int]] = {"contract_drift": [], "payment_dns": []}
    with httpx.Client(timeout=12, trust_env=False) as client:
        if checkout(client, sku="sku-demo") != 200:
            raise RuntimeError("baseline checkout failed")
        currency_changed = False
        try:
            set_inventory_currency("USD")
            currency_changed = True
            set_payment_dns_fault(client, True)
            for _ in range(2):
                for root, sku in (
                    ("contract_drift", "sku-demo"),
                    ("payment_dns", "sku-headphones"),
                ):
                    trace_id = uuid4().hex
                    parent_span = uuid4().hex[:16]
                    response = client.post(
                        "http://127.0.0.1:8001/checkout",
                        headers={"traceparent": f"00-{trace_id}-{parent_span}-01"},
                        json={
                            "idempotency_key": str(uuid4()), "sku": sku, "quantity": 1,
                        },
                    )
                    traces[root].append(trace_id)
                    statuses[root].append(response.status_code)
                    if response.status_code != 502:
                        raise RuntimeError(
                            f"unexpected {root} checkout status: {response.status_code}"
                        )
        finally:
            try:
                set_payment_dns_fault(client, False)
            finally:
                if currency_changed:
                    set_inventory_currency("CNY")
        recovered = {
            "sku-demo": checkout(client, sku="sku-demo"),
            "sku-headphones": checkout(client, sku="sku-headphones"),
        }
    trace_ids = traces["contract_drift"] + traces["payment_dns"]
    deadline = time.monotonic() + 15
    while True:
        events = failure_events(trace_ids)
        if len(events) >= 8 or time.monotonic() >= deadline:
            break
        time.sleep(1)
    return {
        "sample": "same_gateway_symptom_independent_business_roots",
        "window_start": window_start.isoformat(),
        "window_end": (window_start + timedelta(minutes=10)).isoformat(),
        "traces_by_injected_root": traces,
        "fault_checkout_statuses": statuses,
        "events": events,
        "recovered": recovered,
        "currency_restored": inventory_currency() == "CNY",
    }


def audit(source: dict) -> dict:
    trace_roots = {
        trace: root
        for root, trace_ids in source["traces_by_injected_root"].items()
        for trace in trace_ids
    }
    event_roots = {
        event["id"]: trace_roots[event["trace_id"]]
        for event in source["events"]
    }
    window_start = datetime.fromisoformat(source["window_start"])
    signals = [
        signal for signal in list_request_signals(Settings())
        if signal.window_start == window_start
    ]
    audited = [
        {
            "id": str(signal.id), "signal_kind": signal.signal_kind,
            "service": signal.service,
            "failure_code": signal.failure_code, "resource": signal.resource,
            "member_event_ids": [str(event_id) for event_id in signal.member_event_ids],
            "injected_roots": sorted({
                event_roots[str(event_id)] for event_id in signal.member_event_ids
                if str(event_id) in event_roots
            }),
            "verified_member_count": sum(
                member.verification_status == "VERIFIED_SPAN_FACT"
                for member in signal.member_verifications
            ),
            "alert_binding_status": signal.alert_binding_status,
            "root_cause_status": signal.root_cause_status,
        }
        for signal in signals
    ]
    return {
        "source_report": source["sample"],
        "window_start": source["window_start"],
        "signals": audited,
        "mixed_root_signal_count": sum(
            len(cast(list[str], item["injected_roots"])) > 1 for item in audited
        ),
        "interpretation": (
            "A shared gateway symptom can include independent roots; "
            "these signals must remain unbound with undetermined cause."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-report", type=Path)
    args = parser.parse_args()
    if args.audit_report:
        source = json.loads(args.audit_report.read_text(encoding="utf-8"))
        report = audit(source)
        prefix = "signal-collision-audit"
    else:
        report = run()
        prefix = "signal-collision"
    path = ROOT / "eval" / f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary: dict[str, object]
    if args.audit_report:
        summary = {
            "window_start": report["window_start"],
            "signal_count": len(cast(list[dict], report["signals"])),
            "mixed_root_signal_count": report["mixed_root_signal_count"],
        }
    else:
        summary = {
            "window_start": report["window_start"],
            "statuses": report["fault_checkout_statuses"],
            "event_count": len(report["events"]),
            "recovered": report["recovered"],
        }
    print(json.dumps({**summary, "report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
