"""Audit every remediation authorization decision.

Revision ID: 0012_policy_decisions
Revises: 0011_remediation_plan_provenance
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0012_policy_decisions"
down_revision = "0011_remediation_plan_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_decisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("action_id", UUID(as_uuid=True), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("required_role", sa.String(80)),
        sa.Column("required_approvals", sa.BigInteger(), nullable=False),
        sa.Column("input", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "action_id"],
            ["actions.tenant_id", "actions.id"],
            name="fk_policy_decisions_action",
        ),
    )
    op.create_index(
        "ix_policy_decisions_action", "policy_decisions", ["tenant_id", "action_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_policy_decisions_action", table_name="policy_decisions")
    op.drop_table("policy_decisions")
