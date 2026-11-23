from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin


class DataRequest(IdMixin, TenantMixin, Base):
    """A data-subject request: export or erase one person's data (GDPR / DPDP style)."""

    __tablename__ = "gtm_data_requests"

    kind: Mapped[str] = mapped_column(String(10))  # export | erase
    contact_id: Mapped[uuid.UUID | None]
    email_hash: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(30), default="cockpit")  # cockpit | public_form | platform_callback
    status: Mapped[str] = mapped_column(String(20), default="pending")
    confirmation_code: Mapped[str] = mapped_column(String(32))
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
