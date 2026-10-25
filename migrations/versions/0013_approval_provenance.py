"""Track independent approver identity, reason and linked policy decision.

Revision ID: 0013_approval_provenance
Revises: 0012_policy_decisions
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0013_approval_provenance"
down_revision = "0012_policy_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_policy_decisions_tenant_id", "policy_decisions", ["tenant_id", "id"]
    )
    op.add_column("approvals", sa.Column("policy_decision_id", UUID(as_uuid=True)))
    op.add_column("approvals", sa.Column("requested_by", sa.String(120)))
    op.add_column("approvals", sa.Column("decided_by", sa.String(120)))
    op.add_column("approvals", sa.Column("decision_reason", sa.String(300)))
    op.add_column("approvals", sa.Column("decided_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE approvals AS a
        SET policy_decision_id = (
            SELECT p.id FROM policy_decisions AS p
            WHERE p.tenant_id = a.tenant_id
              AND p.action_id = a.action_id
              AND p.snapshot_hash = a.snapshot_hash
              AND p.outcome = 'REQUIRE_APPROVAL'
            ORDER BY p.created_at DESC, p.id DESC LIMIT 1
        ),
        requested_by = (
            SELECT p.input #>> '{actor,id}' FROM policy_decisions AS p
            WHERE p.tenant_id = a.tenant_id
              AND p.action_id = a.action_id
              AND p.snapshot_hash = a.snapshot_hash
              AND p.outcome = 'REQUIRE_APPROVAL'
            ORDER BY p.created_at DESC, p.id DESC LIMIT 1
        )
        WHERE a.status = 'REQUESTED'
        """
    )
    op.create_foreign_key(
        "fk_approvals_policy_decision",
        "approvals",
        "policy_decisions",
        ["tenant_id", "policy_decision_id"],
        ["tenant_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_approvals_policy_decision", "approvals", type_="foreignkey")
    op.drop_column("approvals", "decided_at")
    op.drop_column("approvals", "decision_reason")
    op.drop_column("approvals", "decided_by")
    op.drop_column("approvals", "requested_by")
    op.drop_column("approvals", "policy_decision_id")
    op.drop_constraint("uq_policy_decisions_tenant_id", "policy_decisions", type_="unique")
