"""Initial tenant and incident control-plane schema.

Revision ID: 0001_foundations
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001_foundations"
down_revision = None
branch_labels = None
depends_on = None


def core_columns() -> list[sa.Column]:
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
    ]


def tenant_fk(table: str, key: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", key], [f"{table}.tenant_id", f"{table}.id"], name=name
    )


def upgrade() -> None:
    op.create_table(
        "tenants",
        *core_columns(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.UniqueConstraint("name", name="uq_tenants_name"),
    )
    op.create_table(
        "incidents",
        *core_columns(),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(2), nullable=False),
        sa.Column("correlation_key", sa.String(300)),
        sa.Column("primary_service_id", UUID(as_uuid=True)),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "id", name="uq_incidents_tenant_id"),
    )
    op.create_index("ix_incidents_tenant_status", "incidents", ["tenant_id", "status"])
    op.create_table(
        "agent_runs",
        *core_columns(),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("graph_version", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("model_provider", sa.String(100), nullable=False),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("max_llm_calls", sa.BigInteger(), nullable=False),
        sa.Column("max_tool_calls", sa.BigInteger(), nullable=False),
        tenant_fk("incidents", "incident_id", "fk_agent_runs_incident"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_runs_tenant_id"),
        sa.UniqueConstraint("thread_id", name="uq_agent_runs_thread_id"),
    )
    op.create_table(
        "remediation_plans",
        *core_columns(),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("rationale", sa.String(), nullable=False),
        tenant_fk("incidents", "incident_id", "fk_remediation_plans_incident"),
        tenant_fk("agent_runs", "agent_run_id", "fk_remediation_plans_agent_run"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_remediation_plans_tenant_id"),
    )
    op.create_table(
        "actions",
        *core_columns(),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        tenant_fk("incidents", "incident_id", "fk_actions_incident"),
        tenant_fk("remediation_plans", "plan_id", "fk_actions_plan"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_actions_tenant_id"),
    )
    op.create_table(
        "approvals",
        *core_columns(),
        sa.Column("action_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        tenant_fk("actions", "action_id", "fk_approvals_action"),
    )


def downgrade() -> None:
    for table in (
        "approvals",
        "actions",
        "remediation_plans",
        "agent_runs",
        "incidents",
        "tenants",
    ):
        op.drop_table(table)
