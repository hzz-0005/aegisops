"""Payment provider seam. order_id is the stable provider idempotency key."""

import os
import time
from threading import Lock
from typing import Protocol

from lab.services.common import database
from lab.services.contracts import PaymentIntent, PaymentReceipt, PaymentState


class PaymentConflict(Exception):
    """The same order was submitted with a different amount."""


_delay_lock = Lock()
_delay_seconds = 0.0
_delay_deadline = 0.0


def set_simulated_provider_delay(seconds: float, ttl_seconds: float) -> None:
    """Bounded Lab fault injection; never used by a real payment adapter."""
    global _delay_seconds, _delay_deadline

    with _delay_lock:
        _delay_seconds = seconds
        _delay_deadline = time.monotonic() + ttl_seconds if seconds else 0.0


def simulated_provider_delay() -> float:
    with _delay_lock:
        if time.monotonic() < _delay_deadline:
            return _delay_seconds
    return 0.0


class PaymentProvider(Protocol):
    def charge(self, intent: PaymentIntent) -> PaymentReceipt:
        """Return SUCCEEDED or PENDING; retries with the same order must be safe."""


class SimulatedPostgresPaymentProvider:
    """Local deterministic provider, with PostgreSQL as its payment source of truth."""

    def charge(self, intent: PaymentIntent) -> PaymentReceipt:
        if delay := simulated_provider_delay():
            time.sleep(delay)
        with database() as connection:
            row = connection.execute(
                """
                INSERT INTO lab.payments (id, order_id, amount_cents, currency, status)
                VALUES (gen_random_uuid(), %s, %s, %s, 'SUCCEEDED')
                ON CONFLICT (order_id) DO NOTHING
                RETURNING id, amount_cents, currency, status
                """,
                (intent.order_id, intent.amount_cents, intent.currency),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT id, amount_cents, currency, status "
                    "FROM lab.payments WHERE order_id = %s",
                    (intent.order_id,),
                ).fetchone()
            assert row is not None
            if row[1] != intent.amount_cents or row[2] != intent.currency:
                raise PaymentConflict
        return PaymentReceipt(
            payment_id=row[0],
            order_id=intent.order_id,
            state=PaymentState(row[3]),
            amount_cents=row[1],
            currency=row[2],
        )


def configured_provider() -> PaymentProvider:
    # A real provider adapter can be selected here after secret handling,
    # callbacks, reconciliation, and integration tests have been implemented.
    provider = os.getenv("LAB_PAYMENT_PROVIDER", "simulated")
    if provider != "simulated":
        raise ValueError(f"unsupported payment provider: {provider}")
    return SimulatedPostgresPaymentProvider()
