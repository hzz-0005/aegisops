"""Immutable, verified request event members for a detector window.

Revision ID: 0019_request_derived_signals
Revises: 0018_verified_request_evidence
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0019_request_derived_signals"
down_revision = "0018_verified_request_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_derived_signals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("detector_version", sa.String(80), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("service_name", sa.String(120), nullable=False),
        sa.Column("failure_code", sa.String(80), nullable=False),
        sa.Column("resource_name", sa.String(120)),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_count", sa.BigInteger(), nullable=False),
        sa.Column("distinct_trace_count", sa.BigInteger(), nullable=False),
        sa.Column("verification_status", sa.String(40), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_request_derived_signal_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "detector_version", "identity_key", "window_start",
            name="uq_request_derived_signal_window",
        ),
        sa.CheckConstraint("event_count >= 2", name="ck_request_derived_signal_event_count"),
        sa.CheckConstraint(
            "distinct_trace_count >= 2", name="ck_request_derived_signal_trace_count"
        ),
        sa.CheckConstraint(
            "verification_status = 'VERIFIED_SPAN_FACT'",
            name="ck_request_derived_signal_verified",
        ),
        sa.CheckConstraint("window_end > window_start", name="ck_request_derived_signal_window"),
    )
    op.create_table(
        "request_derived_signal_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("signal_id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_event_id", UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "signal_id"],
            ["request_derived_signals.tenant_id", "request_derived_signals.id"],
            name="fk_request_derived_signal_event_signal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_event_id"],
            ["request_failure_events.tenant_id", "request_failure_events.id"],
            name="fk_request_derived_signal_event_request",
        ),
        sa.UniqueConstraint(
            "tenant_id", "signal_id", "request_event_id",
            name="uq_request_derived_signal_event_member",
        ),
    )


def downgrade() -> None:
    op.drop_table("request_derived_signal_events")
    op.drop_table("request_derived_signals")
