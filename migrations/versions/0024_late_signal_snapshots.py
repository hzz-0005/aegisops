"""Persist immutable compensation work, scoped to an original finalized window."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0024_late_signal_snapshots"
down_revision = "0023_detector_requeue_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_request_signal_window_tenant_id",
                                "request_signal_windows", ["tenant_id", "id"])
    op.add_column("request_signal_detector_runs", sa.Column("source_window_id", UUID()))
    op.add_column("request_signal_detector_runs", sa.Column("snapshot_event_ids", JSONB))
    op.create_foreign_key("fk_request_signal_run_source_window", "request_signal_detector_runs",
                          "request_signal_windows", ["tenant_id", "source_window_id"],
                          ["tenant_id", "id"])
    op.create_check_constraint("ck_request_signal_run_snapshot_kind",
        "request_signal_detector_runs",
        "(source_window_id IS NULL AND snapshot_event_ids IS NULL "
        "AND detector_version = 'request-failure-window-v1') OR "
        "(source_window_id IS NOT NULL AND snapshot_event_ids IS NOT NULL "
        "AND detector_version LIKE 'late-v1:%')")
    op.create_index("ix_request_signal_run_source", "request_signal_detector_runs",
                    ["tenant_id", "source_window_id"])
    op.create_index("ix_request_failure_window", "request_failure_events",
                    ["tenant_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_request_failure_window", table_name="request_failure_events")
    op.drop_index("ix_request_signal_run_source", table_name="request_signal_detector_runs")
    op.drop_constraint("ck_request_signal_run_snapshot_kind", "request_signal_detector_runs")
    op.drop_constraint("fk_request_signal_run_source_window", "request_signal_detector_runs")
    op.drop_column("request_signal_detector_runs", "snapshot_event_ids")
    op.drop_column("request_signal_detector_runs", "source_window_id")
    op.drop_constraint("uq_request_signal_window_tenant_id", "request_signal_windows")
