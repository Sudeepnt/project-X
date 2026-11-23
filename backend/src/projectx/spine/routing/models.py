from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base


class RoutingKey(Base):
    """Maps a public key to the business that owns it, before any tenant context exists.

    Namespaces: ``link`` (tracked-link slug), ``page`` (business-slug/page-slug), ``account``
    (platform:external-account-id, for inbound webhooks), ``bot`` (chat bot ids),
    ``billing`` (billing customer id). Holds no personal data.
    """

    __tablename__ = "routing_keys"

    namespace: Mapped[str] = mapped_column(String(30), primary_key=True)
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
