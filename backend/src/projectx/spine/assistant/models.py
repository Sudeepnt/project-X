from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin


class AssistantTurn(IdMixin, TenantMixin, Base):
    """One request to the assistant and what came of it: the plan, each trial's reflection, the outcome.

    The recent turns are the assistant's short-term memory (it sees what it just said and did, so
    "no, the other one" makes sense). Durable lessons go to the memory shelves instead.
    """

    __tablename__ = "assistant_turns"
    __table_args__ = (Index("ix_assistant_turns_recent", "business_id", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None]
    channel: Mapped[str] = mapped_column(String(20), default="app")  # app | email | whatsapp
    text: Mapped[str] = mapped_column(Text)
    understood: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[dict[str, Any]] = mapped_column(default=dict)
    reflections: Mapped[list[Any]] = mapped_column(default=list)
    trials: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10), default="done")  # done | asked | failed
    reply: Mapped[str] = mapped_column(Text, default="")
    results: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
