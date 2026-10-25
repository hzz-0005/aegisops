"""Verify a client retry reuses one Lab order and one simulated payment."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import httpx
import psycopg

GATEWAY = os.getenv("AEGISOPS_LAB_GATEWAY_URL", "http://127.0.0.1:8001")
DATABASE_URL = os.getenv(
    "AEGISOPS_LAB_ORDERS_DATABASE_URL",
    "postgresql://lab_orders:lab_orders_dev_only@127.0.0.1:5433/lab_orders",
)


def main() -> None:
    key = uuid4()
    payload = {"idempotency_key": str(key), "sku": "sku-demo", "quantity": 1}
    with httpx.Client(timeout=8) as client:
        missing = client.post(f"{GATEWAY}/checkout", json={"sku": "sku-demo", "quantity": 1})
        if missing.status_code != 422:
            raise RuntimeError("checkout accepted a request without a client idempotency key")
        first = client.post(f"{GATEWAY}/checkout", json=payload)
        first.raise_for_status()
        if first.json()["status"] != "PAID":
            raise RuntimeError("initial Lab checkout was not paid")
        repeated = client.post(f"{GATEWAY}/checkout", json=payload)
        repeated.raise_for_status()
        if repeated.json() != first.json():
            raise RuntimeError("same-key retry changed the order or receipt")
        conflict = client.post(f"{GATEWAY}/checkout", json={**payload, "quantity": 2})
        if conflict.status_code != 409:
            raise RuntimeError("same key accepted a different order request")

        order_id = UUID(first.json()["order_id"])
        payment_id = UUID(first.json()["payment_id"])
        try:
            # Reproduce a lost Order response after the provider committed payment.
            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute(
                    "UPDATE lab.orders SET status = 'PENDING', payment_id = NULL "
                    "WHERE id = %s AND idempotency_key = %s",
                    (order_id, key),
                )
            recovered = client.post(f"{GATEWAY}/checkout", json=payload)
            recovered.raise_for_status()
            if recovered.json() != first.json():
                raise RuntimeError("uncertain retry did not recover the original receipt")
        finally:
            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute(
                    "UPDATE lab.orders SET status = 'PAID', payment_id = %s "
                    "WHERE id = %s AND idempotency_key = %s",
                    (payment_id, order_id, key),
                )

    with psycopg.connect(DATABASE_URL) as connection:
        order_count = connection.execute(
            "SELECT count(*) FROM lab.orders WHERE idempotency_key = %s", (key,)
        ).fetchone()
        payment_count = connection.execute(
            "SELECT count(*) FROM lab.payments WHERE order_id = %s", (order_id,)
        ).fetchone()
        assert order_count is not None and payment_count is not None
        orders, payments = order_count[0], payment_count[0]
    if (orders, payments) != (1, 1):
        raise RuntimeError(f"retry created duplicate rows: orders={orders}, payments={payments}")

    concurrent_key = uuid4()
    concurrent_payload = {
        "idempotency_key": str(concurrent_key),
        "sku": "sku-demo",
        "quantity": 1,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: httpx.post(f"{GATEWAY}/checkout", json=concurrent_payload, timeout=8),
                range(2),
            )
        )
    if any(response.status_code != 200 for response in responses):
        raise RuntimeError(
            f"concurrent same-key checkout failed: {[r.status_code for r in responses]}"
        )
    if responses[0].json() != responses[1].json():
        raise RuntimeError("concurrent same-key requests returned different receipts")
    with psycopg.connect(DATABASE_URL) as connection:
        concurrent_counts = connection.execute(
            "SELECT count(*), count(DISTINCT p.id) FROM lab.orders o "
            "LEFT JOIN lab.payments p ON p.order_id = o.id WHERE o.idempotency_key = %s",
            (concurrent_key,),
        ).fetchone()
    if concurrent_counts != (1, 1):
        raise RuntimeError(f"concurrent retry created duplicate rows: {concurrent_counts}")
    print("PASS: required key, stable retry, conflict rejection and uncertain recovery")
    print("PASS: sequential and concurrent retries each create one order and payment")


if __name__ == "__main__":
    main()
