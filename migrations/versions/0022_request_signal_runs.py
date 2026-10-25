"""Persist retryable detector work without creating incidents.

Revision ID: 0022_request_signal_runs
Revises: 0021_signal_window_snapshot
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0022_request_signal_runs"
down_revision = "0021_signal_window_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_signal_detector_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("detector_version", sa.String(80), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id", "detector_version", "window_start",
            name="uq_request_signal_detector_run",
        ),
        sa.CheckConstraint("window_end > window_start", name="ck_request_signal_run_bounds"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_request_signal_run_attempts"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'RETRYABLE', 'BLOCKED', 'FINALIZED')",
            name="ck_request_signal_run_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("request_signal_detector_runs")
