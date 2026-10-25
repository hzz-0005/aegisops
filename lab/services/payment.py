import logging
import os

import redis
from fastapi import Header, HTTPException, Request
from opentelemetry import metrics, trace

from lab.services.common import create_app, mark_request_failure
from lab.services.contracts import PaymentIntent, PaymentReceipt
from lab.services.payment_provider import (
    PaymentConflict,
    configured_provider,
    set_simulated_provider_delay,
)

app = create_app("payment-service")
logger = logging.getLogger("lab.payment-service")
cache_failures = metrics.get_meter("aegisops.lab").create_counter(
    "lab_payment_redis_failures", description="Payment requests blocked by Redis unavailability"
)
cache_failures.add(0, {"service": "payment-service"})


@app.post("/lab/faults/provider-delay")
def set_provider_delay(
    seconds: float,
    ttl_seconds: float = 60,
    x_lab_fault_token: str | None = Header(None),
) -> dict[str, float]:
    if not os.getenv("LAB_FAULT_TOKEN") or x_lab_fault_token != os.getenv("LAB_FAULT_TOKEN"):
        raise HTTPException(status_code=404, detail="not found")
    if not 0 <= seconds <= 30 or not 0 < ttl_seconds <= 120:
        raise HTTPException(status_code=422, detail="delay or TTL out of range")
    if os.getenv("LAB_PAYMENT_PROVIDER", "simulated") != "simulated":
        raise HTTPException(status_code=409, detail="fault requires simulated provider")
    set_simulated_provider_delay(seconds, ttl_seconds)
    return {"seconds": seconds, "ttl_seconds": ttl_seconds}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    cache = redis.Redis.from_url(
        os.environ["LAB_REDIS_URL"], socket_connect_timeout=0.5, socket_timeout=0.5
    )
    try:
        cache.ping()
    except redis.exceptions.RedisError as exc:
        raise HTTPException(status_code=503, detail="payment cache unavailable") from exc
    return {"status": "ready", "service": "payment-service"}


@app.post("/payments", response_model=PaymentReceipt)
def pay(request: PaymentIntent, http_request: Request) -> PaymentReceipt:
    cache = redis.Redis.from_url(
        os.environ["LAB_REDIS_URL"], socket_connect_timeout=0.5, socket_timeout=0.5
    )
    with trace.get_tracer("aegisops.lab.payment").start_as_current_span("redis.cache_read") as span:
        try:
            cache.get(f"payment:{request.order_id}")
        except redis.exceptions.RedisError as exc:
            span.record_exception(exc)
            cache_failures.add(1, {"service": "payment-service"})
            # The failing business request is the observable fact; health probes
            # stay unmarked so probe samples cannot crowd out caller evidence.
            failure = "timeout" if isinstance(exc, redis.TimeoutError) else "transport"
            mark_request_failure(
                http_request, service="payment-service",
                failure_code=f"dependency_{failure}", resource="redis-main",
            )
            logger.exception("redis connection refused or unavailable")
            raise HTTPException(status_code=500, detail="payment cache unavailable") from exc

    try:
        receipt = configured_provider().charge(request)
    except PaymentConflict as exc:
        raise HTTPException(
            status_code=409, detail="order already paid with another amount"
        ) from exc
    try:
        cache.setex(f"payment:{request.order_id}", 60, receipt.state.value)
    except redis.exceptions.RedisError:
        # PostgreSQL remains the payment fact; a cache write failure does not undo it.
        logger.warning("redis cache write unavailable after committed payment")
    return receipt
