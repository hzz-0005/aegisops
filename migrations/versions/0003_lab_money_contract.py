"""Make synthetic order and payment amounts explicit.

Revision ID: 0003_lab_money_contract
Revises: 0002_enterprise_lab
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_lab_money_contract"
down_revision = "0002_enterprise_lab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_items",
        sa.Column("unit_price_cents", sa.Integer(), nullable=False, server_default="100"),
        schema="lab",
    )
    op.add_column(
        "inventory_items",
        sa.Column("currency", sa.String(3), nullable=False, server_default="CNY"),
        schema="lab",
    )
    op.add_column(
        "payments",
        sa.Column("currency", sa.String(3), nullable=False, server_default="CNY"),
        schema="lab",
    )
    op.add_column("orders", sa.Column("amount_cents", sa.Integer()), schema="lab")
    op.add_column(
        "orders",
        sa.Column("currency", sa.String(3), nullable=False, server_default="CNY"),
        schema="lab",
    )
    op.execute("UPDATE lab.orders SET amount_cents = quantity * 100")
    op.alter_column("orders", "amount_cents", nullable=False, schema="lab")
    op.create_check_constraint(
        "ck_inventory_price_positive", "inventory_items", "unit_price_cents > 0", schema="lab"
    )
    op.create_check_constraint(
        "ck_order_amount_positive", "orders", "amount_cents > 0", schema="lab"
    )


def downgrade() -> None:
    op.drop_constraint("ck_order_amount_positive", "orders", schema="lab")
    op.drop_constraint("ck_inventory_price_positive", "inventory_items", schema="lab")
    op.drop_column("orders", "currency", schema="lab")
    op.drop_column("orders", "amount_cents", schema="lab")
    op.drop_column("payments", "currency", schema="lab")
    op.drop_column("inventory_items", "currency", schema="lab")
    op.drop_column("inventory_items", "unit_price_cents", schema="lab")
