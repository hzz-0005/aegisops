"""Atomically finalize a detector window with its exact event snapshot.

Revision ID: 0021_signal_window_snapshot
Revises: 0020_signal_member_verify
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0021_signal_window_snapshot"
down_revision = "0020_signal_member_verify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_signal_windows",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("detector_version", sa.String(80), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("snapshot_event_ids", JSONB(), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("event_count", sa.BigInteger(), nullable=False),
        sa.Column("signal_count", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "detector_version", "window_start",
            name="uq_request_signal_window",
        ),
        sa.CheckConstraint("window_end > window_start", name="ck_request_signal_window_bounds"),
        sa.CheckConstraint("event_count >= 0", name="ck_request_signal_window_events"),
        sa.CheckConstraint("signal_count >= 0", name="ck_request_signal_window_signals"),
    )


def downgrade() -> None:
    op.drop_table("request_signal_windows")
