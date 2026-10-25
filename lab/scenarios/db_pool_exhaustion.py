"""Exhaust the order service's real bounded DB pool and verify recovery."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

ORDER = "http://127.0.0.1:8003"
GATEWAY = "http://127.0.0.1:8001"
PROMETHEUS = "http://127.0.0.1:9090"
TOKEN = os.getenv("LAB_FAULT_TOKEN", "aegisops_lab_fault_dev_only")
HEADERS = {"x-lab-fault-token": TOKEN}


def checkout(client: httpx.Client) -> httpx.Response:
    return client.post(
        f"{GATEWAY}/checkout",
        json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
    )


def hold_connection() -> None:
    response = httpx.post(
        f"{ORDER}/lab/faults/db-pool/hold",
        params={"seconds": 8},
        headers=HEADERS,
        timeout=12,
    )
    if response.status_code != 200:
        raise RuntimeError(f"pool hold failed: {response.status_code} {response.text}")


def await_exhausted(client: httpx.Client) -> dict[str, int]:
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        response = client.get(f"{ORDER}/lab/faults/db-pool/stats", headers=HEADERS)
        response.raise_for_status()
        stats: dict[str, int] = response.json()
        if stats.get("pool_size") == 2 and stats.get("pool_available", 0) == 0:
            return stats
        time.sleep(0.1)
    raise RuntimeError("two order database connections were not held concurrently")


def await_metric(client: httpx.Client) -> None:
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        response = client.get(
            f"{PROMETHEUS}/api/v1/query",
            params={"query": "lab_order_db_pool_lease_saturation_total"},
        )
        response.raise_for_status()
        if any(
            float(result["value"][1]) >= 1
            for result in response.json()["data"]["result"]
        ):
            return
        time.sleep(1)
    raise RuntimeError("pool exhaustion metric was not visible in Prometheus")


def main() -> None:
    with httpx.Client(timeout=10) as client:
        baseline = checkout(client)
        if baseline.status_code != 200:
            raise RuntimeError(f"baseline checkout failed: {baseline.status_code} {baseline.text}")
        if client.get(f"{ORDER}/lab/faults/db-pool/stats").status_code != 404:
            raise RuntimeError("fault injection endpoint accepted an unauthenticated request")
        with ThreadPoolExecutor(max_workers=2) as executor:
            holds = [executor.submit(hold_connection) for _ in range(2)]
            stats = await_exhausted(client)
            failed = checkout(client)
            if failed.status_code != 503 or "pool exhausted" not in failed.text:
                raise RuntimeError(
                    f"exhausted checkout did not return 503: {failed.status_code} {failed.text}"
                )
            for hold in holds:
                hold.result()
        recovered = checkout(client)
        if recovered.status_code != 200:
            raise RuntimeError(
                f"checkout did not recover: {recovered.status_code} {recovered.text}"
            )
        await_metric(client)
        control = client.get("http://127.0.0.1:8000/api/v1/health/ready")
        if control.status_code != 200:
            raise RuntimeError("control plane became unavailable during business pool fault")
    print(f"order DB pool exhausted and recovered; pool_size={stats['pool_size']}")


if __name__ == "__main__":
    main()
