"""The assistant's conversation: each request, the plan, its reflections and the outcome.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from projectx.db.rls import enable_rls_statements

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "assistant_turns",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("understood", sa.Text(), nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("reflections", JSONB, nullable=False),
        sa.Column("trials", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("reply", sa.Text(), nullable=False),
        sa.Column("results", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_assistant_turns_business_id_businesses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_turns")),
    )
    op.create_index(op.f("ix_assistant_turns_business_id"), "assistant_turns", ["business_id"])
    op.create_index("ix_assistant_turns_recent", "assistant_turns", ["business_id", "user_id", "created_at"])
    for statement in enable_rls_statements(("assistant_turns",)):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("assistant_turns")
