"""Persist provider-neutral alert identity and event time.

Revision ID: 0006_normalized_alert_v1
Revises: 0005_incident_status_phase
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_normalized_alert_v1"
down_revision = "0005_incident_status_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("source_event_id", sa.String(250), nullable=True))
    op.add_column("alerts", sa.Column("schema_version", sa.BigInteger(), nullable=True))
    op.add_column("alerts", sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE alerts SET source_event_id = external_id,
                          schema_version = 1,
                          occurred_at = starts_at
        """
    )
    op.alter_column("alerts", "source_event_id", nullable=False)
    op.alter_column("alerts", "schema_version", nullable=False)
    op.alter_column("alerts", "occurred_at", nullable=False)


def downgrade() -> None:
    op.drop_column("alerts", "occurred_at")
    op.drop_column("alerts", "schema_version")
    op.drop_column("alerts", "source_event_id")
