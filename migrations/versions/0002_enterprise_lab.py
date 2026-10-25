"""Synthetic business data for the enterprise lab.

Revision ID: 0002_enterprise_lab
Revises: 0001_foundations
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_enterprise_lab"
down_revision = "0001_foundations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA lab")
    op.create_table(
        "inventory_items",
        sa.Column("sku", sa.String(80), primary_key=True),
        sa.Column("available", sa.Integer(), nullable=False),
        sa.CheckConstraint("available >= 0", name="ck_inventory_nonnegative"),
        schema="lab",
    )
    op.create_table(
        "payments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("amount_cents > 0", name="ck_payment_positive"),
        schema="lab",
    )
    op.create_table(
        "orders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("sku", sa.String(80), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payment_id", UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["sku"], ["lab.inventory_items.sku"]),
        sa.ForeignKeyConstraint(["payment_id"], ["lab.payments.id"]),
        sa.CheckConstraint("quantity > 0", name="ck_order_quantity_positive"),
        schema="lab",
    )
    op.execute("INSERT INTO lab.inventory_items (sku, available) VALUES ('sku-demo', 100)")


def downgrade() -> None:
    op.drop_table("orders", schema="lab")
    op.drop_table("payments", schema="lab")
    op.drop_table("inventory_items", schema="lab")
    op.execute("DROP SCHEMA lab")
