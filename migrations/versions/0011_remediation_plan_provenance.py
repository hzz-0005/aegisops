"""Bind proposed remediation plans to immutable investigation reports.

Revision ID: 0011_remediation_plan_provenance
Revises: 0010_investigation_reports
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0011_remediation_plan_provenance"
down_revision = "0010_investigation_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "remediation_plans", sa.Column("source_report_id", UUID(as_uuid=True))
    )
    op.add_column(
        "remediation_plans", sa.Column("source_incident_version", sa.BigInteger())
    )
    op.add_column("remediation_plans", sa.Column("plan_hash", sa.String(64)))
    op.add_column("remediation_plans", sa.Column("catalog_version", sa.String(80)))
    op.add_column("remediation_plans", sa.Column("root_cause_summary", sa.String(500)))
    op.add_column("remediation_plans", sa.Column("risk_summary", sa.String(500)))
    op.add_column("remediation_plans", sa.Column("blast_radius", sa.String(500)))
    op.create_foreign_key(
        "fk_remediation_plans_report",
        "remediation_plans",
        "investigation_reports",
        ["tenant_id", "source_report_id"],
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_remediation_plan_report", "remediation_plans", ["tenant_id", "source_report_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_remediation_plan_report", "remediation_plans", type_="unique")
    op.drop_constraint("fk_remediation_plans_report", "remediation_plans", type_="foreignkey")
    op.drop_column("remediation_plans", "catalog_version")
    op.drop_column("remediation_plans", "blast_radius")
    op.drop_column("remediation_plans", "risk_summary")
    op.drop_column("remediation_plans", "root_cause_summary")
    op.drop_column("remediation_plans", "plan_hash")
    op.drop_column("remediation_plans", "source_incident_version")
    op.drop_column("remediation_plans", "source_report_id")
