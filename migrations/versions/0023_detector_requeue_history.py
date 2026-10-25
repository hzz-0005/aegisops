"""Audit detector retries across manually started generations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023_detector_requeue_history"
down_revision = "0022_request_signal_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_signal_detector_runs", sa.Column(
        "requeue_history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"),
    ))


def downgrade() -> None:
    op.drop_column("request_signal_detector_runs", "requeue_history")
