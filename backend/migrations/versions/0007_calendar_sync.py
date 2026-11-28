"""Calendar sync: connected Google / Microsoft calendars, their busy time, and links to our copies there.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from projectx.db.rls import enable_rls_statements

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_events",
        sa.Column(
            "external_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("secret_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("push_events", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("connected_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_calendar_connections_business_id_businesses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_connections")),
    )
    op.create_index(op.f("ix_calendar_connections_business_id"), "calendar_connections", ["business_id"])
    op.create_table(
        "calendar_busy",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_calendar_busy_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["calendar_connections.id"],
            name=op.f("fk_calendar_busy_connection_id_calendar_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_busy")),
    )
    op.create_index(op.f("ix_calendar_busy_business_id"), "calendar_busy", ["business_id"])
    op.create_index("ix_calendar_busy_range", "calendar_busy", ["business_id", "starts_at", "ends_at"])
    for statement in enable_rls_statements(("calendar_connections", "calendar_busy")):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("calendar_busy")
    op.drop_table("calendar_connections")
    op.drop_column("calendar_events", "external_refs")
