"""Network tables: the people and companies around the business, and handshakes with other businesses here."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Partner(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "network_partners"

    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(12))  # supplier | partner | referrer | investor | advisor | other
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    cadence_days: Mapped[int | None] = mapped_column(Integer)  # stay in touch every N days
    last_contact_at: Mapped[datetime | None]
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="active")


class HandshakeInvite(IdMixin, Base):
    """A one-time code one business gives another to link their assistants. Exempt from RLS on purpose: the
    accepting business must find it before the two are linked. Holds a hash and the scopes, nothing personal."""

    __tablename__ = "handshake_invites"
    __table_args__ = (Index("uq_handshake_invite_hash", "code_hash", unique=True),)

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[Any]] = mapped_column(default=list)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class HandshakeLink(IdMixin, TenantMixin, TimestampMixin, Base):
    """One side of a trusted link. Each business keeps its own row, with what it lets the other side see."""

    __tablename__ = "handshake_links"
    __table_args__ = (Index("uq_handshake_link_peer", "business_id", "peer_business_id", unique=True),)

    peer_business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    peer_name: Mapped[str] = mapped_column(String(200))
    granted: Mapped[list[Any]] = mapped_column(default=list)  # what this business lets the peer do: ["availability"]
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | revoked


class HandshakeMeeting(IdMixin, TenantMixin, TimestampMixin, Base):
    """A meeting the two assistants agreed. Both sides keep a row sharing `ref`."""

    __tablename__ = "handshake_meetings"
    __table_args__ = (Index("uq_handshake_meeting_ref", "business_id", "ref", unique=True),)

    ref: Mapped[uuid.UUID]
    link_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("handshake_links.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))  # asked | invited
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(10), default="proposed")  # proposed | booked | declined
    event_id: Mapped[uuid.UUID | None]
    decision_id: Mapped[uuid.UUID | None]
