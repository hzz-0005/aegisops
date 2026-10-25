"""Separate Incident lifecycle from processing progress.

Revision ID: 0005_incident_status_phase
Revises: 0004_incident_ingestion
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_incident_status_phase"
down_revision = "0004_incident_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("processing_phase", sa.String(40), nullable=True))
    op.execute(
        """
        UPDATE incidents SET processing_phase = CASE status
          WHEN 'NEW' THEN 'CORRELATING'
          WHEN 'CORRELATING' THEN 'READY_FOR_INVESTIGATION'
          WHEN 'REINVESTIGATING' THEN 'INVESTIGATING'
          WHEN 'READY_TO_EXECUTE' THEN 'READY_TO_EXECUTE'
          WHEN 'ESCALATED' THEN 'ESCALATED'
          WHEN 'RESOLVED' THEN 'NONE'
          WHEN 'CANCELLED' THEN 'NONE'
          WHEN 'SUPERSEDED' THEN 'NONE'
          ELSE status END
        """
    )
    op.execute(
        """
        UPDATE incidents SET status = CASE
          WHEN status IN ('EXECUTING', 'VERIFYING') THEN 'MITIGATING'
          WHEN status IN ('RESOLVED', 'CANCELLED', 'SUPERSEDED') THEN status
          ELSE 'OPEN' END
        """
    )
    op.alter_column("incidents", "processing_phase", nullable=False)


def downgrade() -> None:
    op.execute(
        """
        UPDATE incidents SET status = CASE
          WHEN status = 'RESOLVED' THEN 'RESOLVED'
          WHEN status = 'CLOSED' THEN 'RESOLVED'
          WHEN status IN ('CANCELLED', 'SUPERSEDED') THEN status
          WHEN processing_phase = 'READY_FOR_INVESTIGATION' THEN 'CORRELATING'
          WHEN processing_phase = 'ESCALATED' THEN 'ESCALATED'
          WHEN processing_phase = 'NONE' THEN 'CORRELATING'
          ELSE processing_phase END
        """
    )
    op.drop_column("incidents", "processing_phase")
