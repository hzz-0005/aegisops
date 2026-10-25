import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import Header, HTTPException, Request
from opentelemetry import metrics, propagate, trace
from psycopg_pool import ConnectionPool, PoolTimeout

from lab.services.business_contracts import ASSERTIONS, checkout_currency_assertion
from lab.services.common import create_app, mark_request_failure
from lab.services.contracts import (
    CreateOrder,
    InventoryItem,
    OrderResult,
    PaymentReceipt,
    PaymentState,
)

app = create_app("order-service")
POOL_MAX_SIZE = int(os.getenv("LAB_DB_POOL_MAX_SIZE", "8"))
order_pool = ConnectionPool(
    conninfo=os.environ["LAB_DATABASE_URL"],
    min_size=1,
    max_size=POOL_MAX_SIZE,
    timeout=float(os.getenv("LAB_DB_POOL_TIMEOUT_SECONDS", "2")),
    open=True,
)
app.router.add_event_handler("shutdown", order_pool.close)
pool_exhaustions = metrics.get_meter("aegisops.lab.order").create_counter(
    "lab_order_db_pool_lease_saturation",
    description="Pool timeouts with all leases held and an independently reachable database",
)
pool_exhaustions.add(0, {"service": "order-service"})
pool_ambiguous_timeouts = metrics.get_meter("aegisops.lab.order").create_counter(
    "lab_order_db_pool_ambiguous_timeouts",
    description="Pool timeouts without enough evidence for lease saturation",
)
pool_ambiguous_timeouts.add(0, {"service": "order-service"})
db_connectivity_failures = metrics.get_meter("aegisops.lab.order").create_counter(
    "lab_order_db_connectivity_failures", description="Order database connectivity failures"
)
db_connectivity_failures.add(0, {"service": "order-service"})
deployment_failures = metrics.get_meter("aegisops.lab.order").create_counter(
    "lab_order_deployment_failures", description="Order requests failed by a faulty deployment"
)
deployment_failures.add(
    0,
    {"service": "order-service", "change_id": os.getenv("LAB_DEPLOYMENT_CHANGE_ID", "baseline")},
)
inventory_failures = metrics.get_meter("aegisops.lab.order").create_counter(
    "lab_order_inventory_failures", description="Orders blocked by inventory service failures"
)
inventory_failures.add(0, {"service": "order-service"})
dependency_failures = metrics.get_meter("aegisops.lab.order").create_counter(
    "lab_order_dependency_failures",
    description="Failed outbound dependency calls by dependency and failure class",
)
dependency_failures.add(
    0, {"service": "order-service", "dependency": "payment-service", "failure": "timeout"}
)
contract_rejections = metrics.get_meter("aegisops.lab.order").create_counter(
    "lab_order_business_contract_rejections",
    description="Checkout requests rejected before payment by an independent business contract",
)
for contract in ASSERTIONS:
    if contract.get("checkout_sku") and contract.get("field") == "currency":
        contract_rejections.add(0, {
            "service": "order-service", "contract_id": contract["name"]
        })
contract_rejections.add(0, {
    "service": "order-service", "contract_id": "missing-currency-contract"
})
lease_lock = Lock()
active_leases = 0
payment_dns_fault_lock = Lock()
payment_dns_fault_deadline = 0.0


def payment_service_url() -> str:
    with payment_dns_fault_lock:
        if time.monotonic() < payment_dns_fault_deadline:
            return "http://payment-service.invalid:8000"
    return os.environ["LAB_PAYMENT_URL"]


@contextmanager
def tracked_order_connection() -> Iterator[psycopg.Connection]:
    global active_leases
    with order_pool.connection() as connection:
        with lease_lock:
            active_leases += 1
        try:
            yield connection
        finally:
            with lease_lock:
                active_leases -= 1


@contextmanager
def order_database() -> Iterator[psycopg.Connection]:
    try:
        with tracked_order_connection() as connection:
            yield connection
    except PoolTimeout as exc:
        with lease_lock:
            all_leases_held = active_leases >= POOL_MAX_SIZE
        try:
            with psycopg.connect(os.environ["LAB_DATABASE_URL"], connect_timeout=1):
                pass
        except psycopg.OperationalError as db_exc:
            db_connectivity_failures.add(1, {"service": "order-service"})
            raise HTTPException(status_code=503, detail="order database unavailable") from db_exc
        if all_leases_held:
            pool_exhaustions.add(1, {"service": "order-service"})
            raise HTTPException(status_code=503, detail="order database pool exhausted") from exc
        pool_ambiguous_timeouts.add(1, {"service": "order-service"})
        raise HTTPException(status_code=503, detail="order database acquisition uncertain") from exc
    except psycopg.OperationalError as exc:
        db_connectivity_failures.add(1, {"service": "order-service"})
        raise HTTPException(status_code=503, detail="order database unavailable") from exc


@dataclass(frozen=True)
class StoredOrder:
    id: UUID
    sku: str
    quantity: int
    amount_cents: int
    currency: str
    status: str
    payment_id: UUID | None


def find_order(connection: psycopg.Connection, key: UUID) -> StoredOrder | None:
    row = connection.execute(
        "SELECT id, sku, quantity, amount_cents, currency, status, payment_id "
        "FROM lab.orders WHERE idempotency_key = %s",
        (key,),
    ).fetchone()
    return StoredOrder(*row) if row is not None else None


def check_reused_order(order: StoredOrder, request: CreateOrder) -> None:
    if order.sku != request.sku or order.quantity != request.quantity:
        raise HTTPException(status_code=409, detail="idempotency key belongs to another order")


def order_result(order: StoredOrder) -> OrderResult:
    return OrderResult(
        order_id=order.id,
        status=order.status,
        payment_id=order.payment_id,
        amount_cents=order.amount_cents,
        currency=order.currency,
    )


def require_fault_token(token: str | None) -> None:
    configured = os.getenv("LAB_FAULT_TOKEN")
    if not configured or token != configured:
        raise HTTPException(status_code=404, detail="not found")


@app.post("/lab/faults/db-pool/hold")
def hold_order_connection(seconds: float = 5, x_lab_fault_token: str | None = Header(None)) -> dict:
    require_fault_token(x_lab_fault_token)
    if seconds <= 0 or seconds > 240:
        raise HTTPException(status_code=422, detail="seconds must be between 0 and 240")
    with tracked_order_connection():
        time.sleep(seconds)
    return {"held_seconds": seconds}


@app.get("/lab/faults/db-pool/stats")
def order_pool_stats(x_lab_fault_token: str | None = Header(None)) -> dict[str, int]:
    require_fault_token(x_lab_fault_token)
    return order_pool.get_stats()


@app.post("/lab/faults/payment-dns")
def set_payment_dns_fault(
    enabled: bool,
    ttl_seconds: float = 90,
    x_lab_fault_token: str | None = Header(None),
) -> dict[str, bool]:
    global payment_dns_fault_deadline

    require_fault_token(x_lab_fault_token)
    if not 0 < ttl_seconds <= 120:
        raise HTTPException(status_code=422, detail="TTL out of range")
    with payment_dns_fault_lock:
        payment_dns_fault_deadline = time.monotonic() + ttl_seconds if enabled else 0.0
    return {"enabled": enabled}


@app.get("/health/ready")
def order_ready() -> dict[str, str]:
    with order_database() as connection:
        connection.execute("SELECT 1")
    return {"status": "ready", "service": "order-service"}


@app.get("/deployment")
def deployment_info() -> dict[str, str | bool]:
    return {
        "change_id": os.getenv("LAB_DEPLOYMENT_CHANGE_ID", "baseline"),
        "bad_deployment": os.getenv("LAB_BAD_DEPLOYMENT", "false") == "true",
    }


@app.post("/orders", response_model=OrderResult)
def create_order(request: CreateOrder, http_request: Request) -> OrderResult:
    if os.getenv("LAB_BAD_DEPLOYMENT", "false") == "true":
        deployment_failures.add(
            1,
            {
                "service": "order-service",
                "change_id": os.environ["LAB_DEPLOYMENT_CHANGE_ID"],
            },
        )
        raise HTTPException(
            status_code=500,
            detail="order deployment rejected checkout",
            headers={"X-Lab-Change-Id": os.environ["LAB_DEPLOYMENT_CHANGE_ID"]},
        )
    with order_database() as connection:
        order = find_order(connection, request.idempotency_key)
    if order is not None:
        check_reused_order(order, request)
        if order.status in {"PAID", "PENDING_PAYMENT", "PAYMENT_FAILED"}:
            return order_result(order)

    headers: dict[str, str] = {}
    propagate.inject(headers)
    with httpx.Client(timeout=3) as client:
        if order is None:
            with trace.get_tracer("aegisops.lab.order").start_as_current_span("inventory.lookup"):
                try:
                    inventory = client.get(
                        f"{os.environ['LAB_INVENTORY_URL']}/inventory/{request.sku}",
                        headers=headers,
                    )
                except httpx.RequestError as exc:
                    inventory_failures.add(1, {"service": "order-service"})
                    failure = "timeout" if isinstance(exc, httpx.TimeoutException) else "transport"
                    dependency_failures.add(1, {
                        "service": "order-service",
                        "dependency": "inventory-service",
                        "failure": failure,
                    })
                    mark_request_failure(
                        http_request, service="order-service", failure_code=f"dependency_{failure}",
                        resource="inventory-service",
                    )
                    raise HTTPException(status_code=502, detail="inventory unavailable") from exc
            if inventory.status_code >= 500:
                inventory_failures.add(1, {"service": "order-service"})
                dependency_failures.add(1, {
                    "service": "order-service", "dependency": "inventory-service",
                    "failure": "http_5xx",
                })
                mark_request_failure(
                    http_request, service="order-service", failure_code="dependency_http_5xx",
                    resource="inventory-service",
                )
                raise HTTPException(status_code=502, detail="inventory unavailable")
            if inventory.status_code != 200:
                raise HTTPException(
                    status_code=inventory.status_code, detail="inventory lookup failed"
                )
            item = InventoryItem.model_validate(inventory.json())
            currency_assertion = checkout_currency_assertion(request.sku)
            if currency_assertion is None:
                contract_rejections.add(1, {
                    "service": "order-service", "contract_id": "missing-currency-contract"
                })
                raise HTTPException(status_code=503, detail="checkout contract not configured")
            if item.currency != currency_assertion["equals"]:
                contract_rejections.add(1, {
                    "service": "order-service", "contract_id": currency_assertion["name"]
                })
                mark_request_failure(
                    http_request, service="order-service",
                    failure_code="business_contract_rejected",
                    resource=currency_assertion["name"],
                )
                raise HTTPException(status_code=502, detail="inventory contract mismatch")
            if item.available < request.quantity:
                raise HTTPException(status_code=409, detail="insufficient inventory")
            with order_database() as connection:
                connection.execute(
                    """
                    INSERT INTO lab.orders
                        (id, idempotency_key, sku, quantity, amount_cents, currency, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        uuid4(),
                        request.idempotency_key,
                        request.sku,
                        request.quantity,
                        item.unit_price_cents * request.quantity,
                        item.currency,
                    ),
                )
                order = find_order(connection, request.idempotency_key)
            assert order is not None
            check_reused_order(order, request)
            if order.status in {"PAID", "PENDING_PAYMENT", "PAYMENT_FAILED"}:
                return order_result(order)

        with trace.get_tracer("aegisops.lab.order").start_as_current_span("payment.charge"):
            try:
                payment = client.post(
                    f"{payment_service_url()}/payments",
                    json={
                        "order_id": str(order.id),
                        "amount_cents": order.amount_cents,
                        "currency": order.currency,
                    },
                    headers=headers,
                )
            except httpx.RequestError as exc:
                failure = "timeout" if isinstance(exc, httpx.TimeoutException) else "transport"
                dependency_failures.add(1, {
                    "service": "order-service",
                    "dependency": "payment-service",
                    "failure": failure,
                })
                mark_request_failure(
                    http_request, service="order-service",
                    failure_code=f"dependency_{failure}", resource="payment-service",
                )
                raise HTTPException(status_code=502, detail="payment result uncertain") from exc
        if payment.status_code != 200:
            dependency_failures.add(1, {
                "service": "order-service", "dependency": "payment-service",
                "failure": "http_5xx" if payment.status_code >= 500 else "http_error",
            })
            raise HTTPException(status_code=502, detail="payment result uncertain")

    receipt = PaymentReceipt.model_validate(payment.json())
    if (
        receipt.order_id != order.id
        or receipt.amount_cents != order.amount_cents
        or receipt.currency != order.currency
    ):
        dependency_failures.add(1, {
            "service": "order-service", "dependency": "payment-service",
            "failure": "invalid_receipt",
        })
        raise HTTPException(status_code=502, detail="payment receipt does not match order")
    order_status = {
        PaymentState.SUCCEEDED: "PAID",
        PaymentState.PENDING: "PENDING_PAYMENT",
        PaymentState.FAILED: "PAYMENT_FAILED",
    }[receipt.state]
    with order_database() as connection:
        connection.execute(
            """
            UPDATE lab.orders SET status = %s, payment_id = %s WHERE id = %s
            """,
            (order_status, receipt.payment_id, order.id),
        )
    return OrderResult(
        order_id=order.id,
        status=order_status,
        payment_id=receipt.payment_id,
        amount_cents=order.amount_cents,
        currency=order.currency,
    )
