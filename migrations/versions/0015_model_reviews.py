"""Bounded model commentary tied to an immutable investigation report.

Revision ID: 0015_model_reviews
Revises: 0014_execution_ledger
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0015_model_reviews"
down_revision = "0014_execution_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investigation_model_reviews",
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
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("model_provider", sa.String(80), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False),
        sa.Column("summary", sa.String(500)),
        sa.Column("cited_evidence_ids", JSONB(), nullable=False),
        sa.Column("uncertainties", JSONB(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "report_id"],
            ["investigation_reports.tenant_id", "investigation_reports.id"],
            name="fk_investigation_model_reviews_report",
        ),
        sa.UniqueConstraint("tenant_id", "report_id", name="uq_model_review_report"),
    )


def downgrade() -> None:
    op.drop_table("investigation_model_reviews")
