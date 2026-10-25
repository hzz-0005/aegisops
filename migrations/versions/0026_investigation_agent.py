"""Durable agent reservations survive activity retry and provider uncertainty."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0026_investigation_agent"
down_revision = "0025_late_snapshot_complete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("investigation_reports", sa.Column("agent_trace", pg.JSONB()))
    op.create_table(
        "investigation_agent_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("incident_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("source_incident_version", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("model_calls", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("trace", pg.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "incident_id"],
                                ["incidents.tenant_id", "incidents.id"],
                                name="fk_agent_runs_incident"),
        sa.UniqueConstraint("tenant_id", "incident_id", "source_incident_version",
                            name="uq_agent_run_incident_version"),
        sa.CheckConstraint("model_calls >= 0 AND model_calls <= 4", name="ck_agent_model_budget"),
        sa.CheckConstraint("tool_calls >= 0 AND tool_calls <= 8", name="ck_agent_tool_budget"),
    )


def downgrade() -> None:
    op.drop_table("investigation_agent_runs")
    op.drop_column("investigation_reports", "agent_trace")
