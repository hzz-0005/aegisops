"""Request scoped failure facts with direct trace and span provenance.

Revision ID: 0016_request_failure_events
Revises: 0015_model_reviews
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0016_request_failure_events"
down_revision = "0015_model_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_failure_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("source_event_id", UUID(as_uuid=True), nullable=False),
        sa.Column("service_name", sa.String(120), nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("failure_code", sa.String(80), nullable=False),
        sa.Column("resource_name", sa.String(120)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "service_name", "source_event_id",
            name="uq_request_failure_source_event",
        ),
    )
    op.create_index(
        "ix_request_failure_trace", "request_failure_events", ["tenant_id", "trace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_request_failure_trace", table_name="request_failure_events")
    op.drop_table("request_failure_events")
