"""Benchmarks: anonymous weekly contributions from businesses that opted in.

No business id is stored; contributors are identified by a salted one-way hash so they can update or
withdraw their own rows. Holds aggregates only (rates and averages), never names, content or contacts.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_contributions",
        sa.Column("contributor", sa.String(64), nullable=False),
        sa.Column("week", sa.Date(), nullable=False),
        sa.Column("business_model", sa.String(30), nullable=False),
        sa.Column("metric", sa.String(40), nullable=False),
        sa.Column("platform", sa.String(30), nullable=False, server_default=""),
        sa.Column("value", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("contributor", "week", "metric", "platform", name=op.f("pk_benchmark_contributions")),
    )
    op.create_index(
        op.f("ix_benchmark_contributions_lookup"), "benchmark_contributions", ["business_model", "metric", "week"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_benchmark_contributions_lookup"), table_name="benchmark_contributions")
    op.drop_table("benchmark_contributions")
