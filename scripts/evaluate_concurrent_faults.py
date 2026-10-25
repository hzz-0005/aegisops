"""Two independent Lab faults in one time window, with guarded cleanup."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from scripts.evaluate_fault_battery import (
    active_alerts,
    checkout,
    checkout_details,
    inventory_currency,
    observed_since,
    persisted_checkout_counts,
    set_inventory_currency,
    set_payment_dns_fault,
    set_provider_delay,
)

ROOT = Path(__file__).resolve().parents[1]


def run(dependency_fault: str = "dns") -> dict:
    with httpx.Client(timeout=12, trust_env=False) as client:
        quiet_deadline = time.monotonic() + 85
        while active_alerts(client) and time.monotonic() < quiet_deadline:
            time.sleep(5)
        if active := active_alerts(client):
            raise RuntimeError(f"active alerts contaminate concurrent sample: {active}")
        original_currency = inventory_currency()
        if original_currency != "CNY":
            raise RuntimeError("sku-demo currency baseline is not CNY")
        baseline = {
            "sku-demo": checkout(client, sku="sku-demo"),
            "sku-headphones": checkout(client, sku="sku-headphones"),
        }
        if any(status != 200 for status in baseline.values()):
            raise RuntimeError(f"concurrent sample baseline failed: {baseline}")
        started_at = datetime.now(UTC)
        drift_enabled = False
        drift_keys: list[UUID] = []
        statuses: dict[str, list[int | None]] = {
            "sku-demo": [], "sku-headphones": []
        }
        try:
            set_inventory_currency("USD")
            drift_enabled = True
            if dependency_fault == "provider-timeout":
                set_provider_delay(client, 8)
            else:
                set_payment_dns_fault(client, True)
            for _ in range(8):
                key = uuid4()
                drift_keys.append(key)
                statuses["sku-demo"].append(
                    checkout_details(client, sku="sku-demo", key=key)[0]
                )
                statuses["sku-headphones"].append(
                    checkout_details(client, sku="sku-headphones")[0]
                )
                time.sleep(3)
            time.sleep(8)
        finally:
            try:
                if dependency_fault == "provider-timeout":
                    set_provider_delay(client, 0)
                else:
                    set_payment_dns_fault(client, False)
            finally:
                if drift_enabled:
                    set_inventory_currency(original_currency)
        recovered = {
            "sku-demo": checkout(client, sku="sku-demo"),
            "sku-headphones": checkout(client, sku="sku-headphones"),
        }
        fault_orders, fault_payments = persisted_checkout_counts(drift_keys)
        time.sleep(12)
        alerts = observed_since(started_at)
        deadline = time.monotonic() + 30
        while any(
            item["incident_id"] and item["investigation_status"] is None
            for item in alerts
        ) and time.monotonic() < deadline:
            time.sleep(2)
            alerts = observed_since(started_at)
        groups: dict[str, list[str]] = {}
        for item in alerts:
            if item["incident_id"]:
                groups.setdefault(item["incident_id"], []).append(item["alert"])
        contract_ids = {
            item["incident_id"] for item in alerts
            if item["alert"] == "OrderBusinessContractRejected"
        }
        dependency_ids = {
            item["incident_id"] for item in alerts
            if item["alert"] == "OrderDependencyFailure"
        }
        wrong_supported = [
            item for item in alerts if item["investigation_status"] == "SUPPORTED"
        ]
        return {
            "sample": f"concurrent_inventory_currency_drift_and_payment_{dependency_fault}",
            "started_at": started_at.isoformat(),
            "baseline": baseline,
            "fault_checkout_statuses": statuses,
            "drift_fault_orders": fault_orders,
            "drift_fault_payments": fault_payments,
            "alerts": alerts,
            "incident_partition": sorted(sorted(names) for names in groups.values()),
            "distinct_root_incidents": bool(contract_ids and dependency_ids)
            and contract_ids.isdisjoint(dependency_ids),
            "wrong_supported_root_count": len({
                item["incident_id"] for item in wrong_supported if item["incident_id"]
            }),
            "wrong_action_plan_count": len({
                item["incident_id"] for item in wrong_supported
                if item["incident_id"] and item["plan_status"]
            }),
            "recovered": recovered,
            "currency_restored": inventory_currency() == original_currency,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dependency-fault", choices=["dns", "provider-timeout"], default="dns")
    args = parser.parse_args()
    result = run(args.dependency_fault)
    output = ROOT / "eval" / f"concurrent-faults-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(
        {key: value for key, value in result.items() if key != "alerts"},
        ensure_ascii=False,
    ))
    print(f"report={output}")


if __name__ == "__main__":
    main()
