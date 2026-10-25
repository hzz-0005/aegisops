"""Persist traceable read-only investigation reports and evidence.

Revision ID: 0010_investigation_reports
Revises: 0009_incident_workflow_starts
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0010_investigation_reports"
down_revision = "0009_incident_workflow_starts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investigation_reports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_incident_version", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("graph_version", sa.String(80), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("primary_hypothesis", sa.String(120)),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("hypotheses", JSONB(), nullable=False),
        sa.Column("limitations", JSONB(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_investigation_reports_incident",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_investigation_reports_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "incident_id",
            "source_incident_version",
            name="uq_investigation_report_incident_version",
        ),
    )
    op.create_table(
        "investigation_evidence",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("report_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_ref", sa.String(500), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("subject", sa.String(160), nullable=False),
        sa.Column("polarity", sa.String(20), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("tool_version", sa.String(60), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "report_id"],
            ["investigation_reports.tenant_id", "investigation_reports.id"],
            name="fk_investigation_evidence_report",
        ),
    )
    op.create_index(
        "ix_investigation_evidence_report",
        "investigation_evidence",
        ["tenant_id", "report_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_investigation_evidence_report", table_name="investigation_evidence")
    op.drop_table("investigation_evidence")
    op.drop_table("investigation_reports")
