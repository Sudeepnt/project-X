from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class ChannelAccount(IdMixin, TimestampMixin, TenantMixin, Base):
    """A connected platform account. Tokens live in the vault (secret_id), never here."""

    __tablename__ = "gtm_channel_accounts"
    __table_args__ = (Index("uq_gtm_channel_account", "business_id", "platform", "external_id", unique=True),)

    platform: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    handle: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="connected")  # connected|expired|revoked|error|practice
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list, server_default="{}")
    secret_id: Mapped[uuid.UUID | None]
    token_expires_at: Mapped[datetime | None]
    capabilities: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    followers: Mapped[int | None]
    connected_by: Mapped[uuid.UUID | None]
    last_checked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)


class Post(IdMixin, TimestampMixin, TenantMixin, Base):
    """One publication of a variant on one account. Published only once (idempotency key) and
    verified by reading it back from the platform, never by trusting our own call."""

    __tablename__ = "gtm_posts"
    __table_args__ = (
        Index("uq_gtm_posts_idempotency", "business_id", "idempotency_key", unique=True),
        Index("ix_gtm_posts_queue", "business_id", "status", "scheduled_at"),
        Index("ix_gtm_posts_account_day", "business_id", "account_id", "published_at"),
    )

    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_content_variants.id", ondelete="CASCADE"))
    content_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_content_items.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gtm_channel_accounts.id", ondelete="SET NULL"))
    campaign_id: Mapped[uuid.UUID | None]
    platform: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    external_id: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(600))
    scheduled_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]
    verified_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    decision_id: Mapped[uuid.UUID | None]
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    metrics_at: Mapped[datetime | None]
    score: Mapped[float | None]


class PostMetricSnapshot(IdMixin, TenantMixin, Base):
    __tablename__ = "gtm_post_metric_snapshots"
    __table_args__ = (Index("ix_gtm_post_metrics", "business_id", "post_id", "captured_at"),)

    post_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_posts.id", ondelete="CASCADE"))
    captured_at: Mapped[datetime] = mapped_column(server_default=func.now())
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
