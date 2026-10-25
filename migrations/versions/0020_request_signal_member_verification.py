"""Record the verification result for each exact signal member.

Revision ID: 0020_signal_member_verify
Revises: 0019_request_derived_signals
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_signal_member_verify"
down_revision = "0019_request_derived_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_derived_signal_events",
        sa.Column("verification_status", sa.String(40)),
    )
    op.add_column(
        "request_derived_signal_events",
        sa.Column("verification_recorded_at", sa.DateTime(timezone=True)),
    )
    # Existing members were all checked before their parent signal was committed.
    # Their exact check time was not recorded; leave that timestamp null.
    op.execute(
        "UPDATE request_derived_signal_events "
        "SET verification_status = 'VERIFIED_SPAN_FACT'"
    )
    op.alter_column("request_derived_signal_events", "verification_status", nullable=False)
    op.create_check_constraint(
        "ck_request_derived_signal_event_verified",
        "request_derived_signal_events",
        "verification_status = 'VERIFIED_SPAN_FACT'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_request_derived_signal_event_verified",
        "request_derived_signal_events",
        type_="check",
    )
    op.drop_column("request_derived_signal_events", "verification_recorded_at")
    op.drop_column("request_derived_signal_events", "verification_status")
