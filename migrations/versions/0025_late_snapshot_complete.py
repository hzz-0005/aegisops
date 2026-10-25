"""Persist whether a bounded late snapshot represents the complete window."""
import sqlalchemy as sa
from alembic import op

revision = "0025_late_snapshot_complete"
down_revision = "0024_late_signal_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_signal_detector_runs", sa.Column("snapshot_complete", sa.Boolean(),
                   nullable=False, server_default=sa.true()))
    op.execute("UPDATE request_signal_detector_runs SET snapshot_complete = false "
               "WHERE source_window_id IS NOT NULL AND "
               "jsonb_array_length(snapshot_event_ids) > 1000")
    op.create_check_constraint("ck_incomplete_snapshot_blocked", "request_signal_detector_runs",
                                "snapshot_complete OR status = 'BLOCKED'")


def downgrade() -> None:
    op.drop_constraint("ck_incomplete_snapshot_blocked", "request_signal_detector_runs")
    op.drop_column("request_signal_detector_runs", "snapshot_complete")
