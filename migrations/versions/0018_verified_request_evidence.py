"""Require an exact verified span fact for manually attached request evidence.

Revision ID: 0018_verified_request_evidence
Revises: 0017_incident_request_evidence
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_verified_request_evidence"
down_revision = "0017_incident_request_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_incident_request_evidence_verified",
        "incident_request_evidence",
        sa.text("verification_status = 'VERIFIED_SPAN_FACT'"),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_incident_request_evidence_verified", "incident_request_evidence", type_="check"
    )
