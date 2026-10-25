"""Audited operator attachment of verified request facts to incidents.

Revision ID: 0017_incident_request_evidence
Revises: 0016_request_failure_events
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0017_incident_request_evidence"
down_revision = "0016_request_failure_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_request_failure_tenant_id", "request_failure_events", ["tenant_id", "id"]
    )
    op.create_table(
        "incident_request_evidence",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_event_id", UUID(as_uuid=True), nullable=False),
        sa.Column("operator_id", sa.String(120), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("incident_version_at_link", sa.BigInteger(), nullable=False),
        sa.Column("verification_status", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_request_evidence_incident",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_event_id"],
            ["request_failure_events.tenant_id", "request_failure_events.id"],
            name="fk_incident_request_evidence_event",
        ),
        sa.UniqueConstraint(
            "tenant_id", "incident_id", "request_event_id",
            name="uq_incident_request_evidence_pair",
        ),
    )


def downgrade() -> None:
    op.drop_table("incident_request_evidence")
    op.drop_constraint("uq_request_failure_tenant_id", "request_failure_events")
