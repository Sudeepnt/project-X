"""Our own long-term memory (context entries with history), assistant settings, and self-scheduled wakeups.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from projectx.db.rls import enable_rls_statements

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None
JSONB = postgresql.JSONB(astext_type=sa.Text())
NO_USER = "00000000-0000-0000-0000-000000000000"


def _ts(*names: str) -> list[sa.Column[sa.DateTime]]:
    return [sa.Column(n, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False) for n in names]


def upgrade() -> None:
    op.create_table(
        "context_entries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_ts("confirmed_at", "created_at"),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(key, '') || ' ' || coalesce(value, ''))", persisted=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_context_entries_business_id_businesses"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["context_entries.id"],
            name=op.f("fk_context_entries_supersedes_id_context_entries"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_entries")),
    )
    op.create_index(op.f("ix_context_entries_business_id"), "context_entries", ["business_id"])
    op.create_index("ix_context_scope", "context_entries", ["business_id", "user_id", "status", "kind"])
    op.create_index("ix_context_search", "context_entries", ["search"], postgresql_using="gin")
    op.create_index(
        "uq_context_active_key",
        "context_entries",
        ["business_id", sa.text(f"coalesce(user_id, '{NO_USER}'::uuid)"), "key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "assistant_settings",
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("brief_time", sa.String(5), nullable=False),
        sa.Column("brief_days", JSONB, nullable=False),
        sa.Column("quiet_start", sa.String(5), nullable=False),
        sa.Column("quiet_end", sa.String(5), nullable=False),
        sa.Column("channels", JSONB, nullable=False),
        sa.Column("max_nudges_per_day", sa.BigInteger(), nullable=False),
        sa.Column("told", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_ts("created_at", "updated_at"),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_assistant_settings_business_id_businesses"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_assistant_settings_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("business_id", "user_id", name=op.f("pk_assistant_settings")),
    )
    op.create_table(
        "agent_wakeups",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=False),
        sa.Column("result", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        *_ts("created_at", "updated_at"),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_agent_wakeups_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_agent_wakeups_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_wakeups")),
    )
    op.create_index(op.f("ix_agent_wakeups_business_id"), "agent_wakeups", ["business_id"])
    op.create_index("ix_agent_wakeups_due", "agent_wakeups", ["business_id", "status", "wake_at"])
    op.create_index("uq_agent_wakeups_key", "agent_wakeups", ["business_id", "dedupe_key"], unique=True)
    for statement in enable_rls_statements(("context_entries", "assistant_settings", "agent_wakeups")):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("agent_wakeups")
    op.drop_table("assistant_settings")
    op.drop_table("context_entries")
