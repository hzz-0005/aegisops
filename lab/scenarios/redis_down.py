"""Reproducible Redis Down acceptance scenario for the local AegisOps lab."""

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = "http://127.0.0.1:8001"
PAYMENT = "http://127.0.0.1:8002"
PROMETHEUS = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
TEMPO = "http://127.0.0.1:3200"


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=PROJECT_ROOT, check=True)


def wait_for(name: str, predicate, *, seconds: int = 35) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {name}")


def query_metric(client: httpx.Client) -> float:
    response = client.get(
        f"{PROMETHEUS}/api/v1/query",
        params={"query": 'sum(lab_payment_redis_failures_total{service="payment-service"})'},
    )
    response.raise_for_status()
    results = response.json()["data"]["result"]
    return float(results[0]["value"][1]) if results else 0.0


def metric_ready(client: httpx.Client) -> bool:
    response = client.get(
        f"{PROMETHEUS}/api/v1/query",
        params={"query": 'lab_payment_redis_failures_total{service_instance_id="payment-service"}'},
    )
    response.raise_for_status()
    return bool(response.json()["data"]["result"])


def has_failure_log(client: httpx.Client, *, since_ns: int) -> bool:
    response = client.get(
        f"{LOKI}/loki/api/v1/query_range",
        params={
            "query": '{service_name="payment-service"} |= "redis"',
            "start": since_ns,
            "limit": 10,
        },
    )
    response.raise_for_status()
    return bool(response.json()["data"]["result"])


def has_failure_trace(client: httpx.Client, *, since_seconds: int) -> bool:
    response = client.get(
        f"{TEMPO}/api/search",
        params={
            "tags": "service.name=payment-service",
            "start": since_seconds,
            "end": int(time.time()) + 10,
            "limit": 20,
        },
    )
    response.raise_for_status()
    for item in response.json().get("traces", []):
        trace_response = client.get(f"{TEMPO}/api/traces/{item['traceID']}")
        trace_response.raise_for_status()
        services: set[str] = set()
        redis_error = False
        for batch in trace_response.json().get("batches", []):
            for attribute in batch.get("resource", {}).get("attributes", []):
                if attribute.get("key") == "service.name":
                    services.add(attribute["value"]["stringValue"])
            for scope in batch.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    if (
                        span.get("name") == "redis.cache_read"
                        and span.get("status", {}).get("code") == "STATUS_CODE_ERROR"
                    ):
                        redis_error = True
        if (
            redis_error
            and {
                "lab-gateway",
                "order-service",
                "inventory-service",
                "payment-service",
            }
            <= services
        ):
            return True
    return False


def alert_firing(client: httpx.Client) -> bool:
    response = client.get(f"{PROMETHEUS}/api/v1/alerts")
    response.raise_for_status()
    return any(
        alert["labels"]["alertname"] == "PaymentRedisUnavailable" and alert["state"] == "firing"
        for alert in response.json()["data"]["alerts"]
    )


def checkout(client: httpx.Client) -> httpx.Response:
    return client.post(
        f"{GATEWAY}/checkout",
        json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
    )


def main() -> None:
    with httpx.Client(timeout=15, trust_env=False) as client:
        baseline = checkout(client)
        if (
            baseline.status_code != 200
            or baseline.json()["status"] != "PAID"
            or baseline.json()["amount_cents"] != 100
            or baseline.json()["currency"] != "CNY"
        ):
            raise RuntimeError(f"baseline checkout failed: {baseline.status_code} {baseline.text}")

        # The simulated provider uses the order ID as its idempotency key.
        order_id = str(uuid4())
        payment_payload = {"order_id": order_id, "amount_cents": 100}
        first = client.post(f"{PAYMENT}/payments", json=payment_payload)
        repeated = client.post(f"{PAYMENT}/payments", json=payment_payload)
        conflict = client.post(
            f"{PAYMENT}/payments", json={"order_id": order_id, "amount_cents": 200}
        )
        if (
            first.status_code != 200
            or repeated.status_code != 200
            or first.json()["payment_id"] != repeated.json()["payment_id"]
            or first.json()["amount_cents"] != 100
            or first.json()["currency"] != "CNY"
            or conflict.status_code != 409
        ):
            raise RuntimeError("payment provider idempotency contract failed")

        wait_for("payment failure metric baseline", lambda: metric_ready(client))
        metric_before = query_metric(client)
        fault_start = datetime.now(UTC)
        since_seconds = int(fault_start.timestamp())
        since_ns = int(fault_start.timestamp() * 1_000_000_000)

        compose("stop", "redis")
        try:
            failed_payment = client.post(
                f"{PAYMENT}/payments",
                json={"order_id": str(uuid4()), "amount_cents": 100},
            )
            failed_checkout = checkout(client)
            if failed_payment.status_code != 500 or failed_checkout.status_code != 502:
                raise RuntimeError(
                    f"unexpected fault responses: payment={failed_payment.status_code}, "
                    f"checkout={failed_checkout.status_code}"
                )
            wait_for("Prometheus failure metric", lambda: query_metric(client) > metric_before)
            wait_for("Prometheus alert", lambda: alert_firing(client))
            wait_for("Loki failure log", lambda: has_failure_log(client, since_ns=since_ns))
            wait_for(
                "Tempo failed Redis span",
                lambda: has_failure_trace(client, since_seconds=since_seconds),
            )
        finally:
            compose("start", "redis")

        wait_for("Redis recovery", lambda: checkout(client).status_code == 200)
        print(
            json.dumps(
                {
                    "baseline": "PAID",
                    "payment_during_fault": failed_payment.status_code,
                    "checkout_during_fault": failed_checkout.status_code,
                    "payment_redis_failures": query_metric(client),
                    "prometheus_alert": "firing_during_fault",
                    "loki_failure_log": True,
                    "tempo_four_service_failure_trace": True,
                    "payment_retry_idempotent": True,
                    "recovery_checkout": "PAID",
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
