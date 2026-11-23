"""Billing: accepted provider webhooks (for replay protection and audit).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_events",
        sa.Column("event_id", sa.String(200), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("type", sa.String(80), nullable=False),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="SET NULL", name=op.f("fk_billing_events_business_id_businesses")),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(200), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(op.f("ix_billing_events_business_id"), "billing_events", ["business_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_billing_events_business_id"), table_name="billing_events")
    op.drop_table("billing_events")
