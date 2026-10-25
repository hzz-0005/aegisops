"""Alert ingestion, incident timeline, and durable event delivery.

Revision ID: 0004_incident_ingestion
Revises: 0003_lab_money_contract
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004_incident_ingestion"
down_revision = "0003_lab_money_contract"
branch_labels = None
depends_on = None


def core_columns() -> list[sa.Column]:
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "alerts",
        *core_columns(),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("external_id", sa.String(250), nullable=False),
        sa.Column("fingerprint", sa.String(120), nullable=False),
        sa.Column("service_name", sa.String(120), nullable=False),
        sa.Column("severity", sa.String(2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_alerts_tenant_id"),
        sa.UniqueConstraint("tenant_id", "source", "external_id", name="uq_alerts_external"),
    )
    op.create_table(
        "incident_alerts",
        *core_columns(),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("alert_id", UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_alerts_incident",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "alert_id"],
            ["alerts.tenant_id", "alerts.id"],
            name="fk_incident_alerts_alert",
        ),
        sa.UniqueConstraint("tenant_id", "alert_id", name="uq_incident_alerts_alert"),
    )
    op.create_table(
        "incident_timeline",
        *core_columns(),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_timeline_incident",
        ),
    )
    op.create_table(
        "outbox_events",
        *core_columns(),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_outbox_unpublished", "outbox_events", ["published_at"])
    op.create_table(
        "inbox_messages",
        *core_columns(),
        sa.Column("consumer", sa.String(80), nullable=False),
        sa.Column("message_id", UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "consumer", "message_id", name="uq_inbox_message"),
    )


def downgrade() -> None:
    for table in (
        "inbox_messages",
        "outbox_events",
        "incident_timeline",
        "incident_alerts",
        "alerts",
    ):
        op.drop_table(table)
