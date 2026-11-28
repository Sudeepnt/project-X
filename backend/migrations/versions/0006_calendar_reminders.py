"""Calendar (settings, events) and reminders, each sealed to its business by row-level security.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from projectx.db.rls import enable_rls_statements

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None

TABLES = ("calendar_settings", "calendar_events", "reminders")
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _business_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["business_id"], ["businesses.id"], name=op.f(f"fk_{table}_business_id_businesses"), ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "calendar_settings",
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("working_hours", JSONB, nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False),
        sa.Column("min_notice_minutes", sa.Integer(), nullable=False),
        sa.Column("slot_step_minutes", sa.Integer(), nullable=False),
        sa.Column("default_duration_minutes", sa.Integer(), nullable=False),
        *_timestamps(),
        _business_fk("calendar_settings"),
        sa.PrimaryKeyConstraint("business_id", name=op.f("pk_calendar_settings")),
    )
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("location", sa.String(300), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attendees", JSONB, nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("invite_sequence_sent", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        _business_fk("calendar_events"),
        sa.ForeignKeyConstraint(
            ["contact_id"], ["contacts.id"], name=op.f("fk_calendar_events_contact_id_contacts"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_events")),
    )
    op.create_index(op.f("ix_calendar_events_business_id"), "calendar_events", ["business_id"])
    op.create_index("ix_calendar_events_range", "calendar_events", ["business_id", "starts_at", "ends_at"])
    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("importance", sa.String(10), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False),
        sa.Column("notify_before_minutes", JSONB, nullable=False),
        sa.Column("repeat", sa.String(10), nullable=False),
        sa.Column("next_notify_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("dedupe_key", sa.String(120), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        _business_fk("reminders"),
        sa.ForeignKeyConstraint(
            ["event_id"], ["calendar_events.id"], name=op.f("fk_reminders_event_id_calendar_events"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
    )
    op.create_index(op.f("ix_reminders_business_id"), "reminders", ["business_id"])
    op.create_index("ix_reminders_due", "reminders", ["business_id", "status", "next_notify_at"])
    op.create_index("uq_reminders_dedupe", "reminders", ["business_id", "dedupe_key"], unique=True)
    for statement in enable_rls_statements(TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("reminders")
    op.drop_table("calendar_events")
    op.drop_table("calendar_settings")
