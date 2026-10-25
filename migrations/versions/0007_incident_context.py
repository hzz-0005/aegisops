"""Service topology, change events, and explainable correlation decisions.

Revision ID: 0007_incident_context
Revises: 0006_normalized_alert_v1
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0007_incident_context"
down_revision = "0006_normalized_alert_v1"
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
        "services",
        *core_columns(),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("environment", sa.String(80), nullable=False),
        sa.Column("owner", sa.String(120), nullable=False),
        sa.Column("criticality", sa.String(20), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_services_tenant_id"),
        sa.UniqueConstraint("tenant_id", "environment", "slug", name="uq_services_slug"),
    )
    op.create_table(
        "resources",
        *core_columns(),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("environment", sa.String(80), nullable=False),
        sa.Column("resource_type", sa.String(80), nullable=False),
        sa.Column("owner", sa.String(120), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_resources_tenant_id"),
        sa.UniqueConstraint("tenant_id", "environment", "slug", name="uq_resources_slug"),
    )
    op.create_table(
        "service_dependencies",
        *core_columns(),
        sa.Column("service_id", UUID(as_uuid=True), nullable=False),
        sa.Column("depends_on_service_id", UUID(as_uuid=True)),
        sa.Column("depends_on_resource_id", UUID(as_uuid=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            name="fk_service_dependencies_service",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "depends_on_service_id"],
            ["services.tenant_id", "services.id"],
            name="fk_service_dependencies_target_service",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "depends_on_resource_id"],
            ["resources.tenant_id", "resources.id"],
            name="fk_service_dependencies_target_resource",
        ),
        sa.CheckConstraint(
            "(depends_on_service_id IS NULL) <> (depends_on_resource_id IS NULL)",
            name="ck_service_dependency_one_target",
        ),
    )
    op.create_table(
        "change_events",
        *core_columns(),
        sa.Column("service_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("external_id", sa.String(250), nullable=False),
        sa.Column("change_type", sa.String(80), nullable=False),
        sa.Column("from_ref", sa.String(250)),
        sa.Column("to_ref", sa.String(250)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attributes", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            name="fk_change_events_service",
        ),
        sa.UniqueConstraint("tenant_id", "source", "external_id", name="uq_change_events_source"),
    )
    op.create_index(
        "ix_change_events_service_time", "change_events", ["tenant_id", "service_id", "occurred_at"]
    )
    op.create_table(
        "correlation_decisions",
        *core_columns(),
        sa.Column("alert_id", UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_incident_id", UUID(as_uuid=True)),
        sa.Column("result_incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("total_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("matched_features", JSONB(), nullable=False),
        sa.Column("conflicting_features", JSONB(), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("decided_by", sa.String(80), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "alert_id"],
            ["alerts.tenant_id", "alerts.id"],
            name="fk_correlation_decisions_alert",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_correlation_decisions_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "result_incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_correlation_decisions_result",
        ),
        sa.UniqueConstraint("tenant_id", "alert_id", name="uq_correlation_decision_alert"),
    )


def downgrade() -> None:
    for table in (
        "correlation_decisions",
        "change_events",
        "service_dependencies",
        "resources",
        "services",
    ):
        op.drop_table(table)
