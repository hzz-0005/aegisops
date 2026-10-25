from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class CreateOrder(BaseModel):
    idempotency_key: UUID
    sku: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    quantity: int = Field(gt=0, le=100)


class InventoryItem(BaseModel):
    sku: str
    available: int
    unit_price_cents: int
    currency: str


class CatalogProduct(InventoryItem):
    name: str
    description: str
    category: str


class OrderResult(BaseModel):
    order_id: UUID
    status: str
    payment_id: UUID | None
    amount_cents: int
    currency: str


class PaymentState(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PENDING = "PENDING"
    FAILED = "FAILED"


class PaymentIntent(BaseModel):
    order_id: UUID
    amount_cents: int = Field(gt=0, le=1_000_000)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")


class PaymentReceipt(BaseModel):
    payment_id: UUID
    order_id: UUID
    state: PaymentState
    amount_cents: int
    currency: str
