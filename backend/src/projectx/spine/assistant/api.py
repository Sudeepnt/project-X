"""Talk to the assistant: one endpoint for a message, one for the recent conversation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep
from projectx.spine.assistant.models import AssistantTurn
from projectx.spine.assistant.service import handle_message, turn_view

router = APIRouter(prefix="/api/v1/b/{business_id}", tags=["assistant"])


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/assistant/messages")
async def send_message(body: MessageIn, ctx: EditorDep) -> dict[str, Any]:
    # The request's own transaction stays idle; the assistant opens short ones so no lock waits on the model.
    return await handle_message(ctx.services, ctx.business_id, ctx.user.id, body.text)


@router.get("/assistant/turns")
async def recent_turns(ctx: BusinessDep, limit: int = 20) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            select(AssistantTurn)
            .where(AssistantTurn.user_id == ctx.user.id)
            .order_by(AssistantTurn.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
    ).scalars()
    return {"items": [turn_view(t) for t in rows]}
