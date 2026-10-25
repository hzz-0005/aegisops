"""Durable reservation and start ledger for each Incident workflow.

Revision ID: 0009_incident_workflow_starts
Revises: 0008_incident_relations
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0009_incident_workflow_starts"
down_revision = "0008_incident_relations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_workflow_starts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", sa.String(120), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("first_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("run_id", sa.String(80)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_workflow_starts_incident",
        ),
        sa.UniqueConstraint("tenant_id", "incident_id", name="uq_incident_workflow_start"),
        sa.UniqueConstraint("workflow_id", name="uq_incident_workflow_id"),
    )
    op.execute(
        sa.text(
            """INSERT INTO incident_workflow_starts
               (id, tenant_id, incident_id, workflow_id, state,
                first_attempted_at, started_at, created_at, updated_at, version)
               SELECT DISTINCT ON (tenant_id, incident_id)
                   id, tenant_id, incident_id,
                   'incident/' || tenant_id::text || '/' || incident_id::text,
                   'STARTED', created_at, created_at, created_at, created_at, 1
               FROM incident_timeline
               WHERE event_type = 'WORKFLOW_STARTED'
               ORDER BY tenant_id, incident_id, created_at, id"""
        )
    )


def downgrade() -> None:
    op.drop_table("incident_workflow_starts")
