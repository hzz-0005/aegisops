"""One-shot action attempts and fenced resource leases.

Revision ID: 0014_execution_ledger
Revises: 0013_approval_provenance
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0014_execution_ledger"
down_revision = "0013_approval_provenance"
branch_labels = None
depends_on = None


def core_columns() -> list[sa.Column]:
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "resource_leases",
        *core_columns(),
        sa.Column("resource_key", sa.String(200), nullable=False),
        sa.Column("holder_action_id", UUID(as_uuid=True)),
        sa.Column("fencing_token", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "holder_action_id"],
            ["actions.tenant_id", "actions.id"],
            name="fk_resource_leases_action",
        ),
        sa.UniqueConstraint("tenant_id", "resource_key", name="uq_resource_lease_key"),
    )
    op.create_table(
        "action_attempts",
        *core_columns(),
        sa.Column("action_id", UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("executor_id", sa.String(100), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "action_id"],
            ["actions.tenant_id", "actions.id"],
            name="fk_action_attempts_action",
        ),
        sa.UniqueConstraint(
            "tenant_id", "action_id", "attempt_number", name="uq_action_attempt_number"
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_action_attempt_idempotency"),
    )


def downgrade() -> None:
    op.drop_table("action_attempts")
    op.drop_table("resource_leases")
