"""Auditable Incident merge and split relationships.

Revision ID: 0008_incident_relations
Revises: 0007_incident_context
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0008_incident_relations"
down_revision = "0007_incident_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_relations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("from_incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("to_incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.String(40), nullable=False),
        sa.Column("operator_id", sa.String(120), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("from_version_before", sa.BigInteger(), nullable=False),
        sa.Column("to_version_before", sa.BigInteger()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "from_incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_relations_from",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "to_incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_relations_to",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "from_incident_id",
            "to_incident_id",
            "relation_type",
            name="uq_incident_relation",
        ),
        sa.CheckConstraint(
            "from_incident_id <> to_incident_id", name="ck_incident_relation_distinct"
        ),
    )


def downgrade() -> None:
    op.drop_table("incident_relations")
