"""Funnel automations: each stage triggers its own next step.

MQL starts a sales sequence, Customer starts onboarding, 14 days of inactivity moves a customer
to At-risk and starts a win-back, a high satisfaction score triggers a referral request. Rules
are per business (``settings.gtm.automations``) and editable.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.modules.gtm.config import gtm_settings
from projectx.modules.gtm.crm.lifecycle import Cause, change
from projectx.modules.gtm.nurture.sequences import enroll
from projectx.spine.jobs.registry import JobContext, job
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact


def matching_rules(rules: list[dict[str, Any]], event: str, value: Any) -> list[dict[str, Any]]:
    out = []
    for rule in rules:
        if not rule.get("active", True):
            continue
        trigger = rule.get("trigger", {})
        kind = trigger.get("type")
        if (
            (event == "stage_entered" and kind == "stage_entered" and trigger.get("stage") == value)
            or (event == "side_state_entered" and kind == "side_state_entered" and trigger.get("value") == value)
            or (event == "satisfaction" and kind == "satisfaction" and float(value) >= float(trigger.get("min", 9)))
        ):
            out.append(rule)
    return out


@job("gtm.run_automations", max_attempts=5, priority=40)
async def run_automations(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    contact_id = uuid.UUID(ctx.payload["contact_id"])
    event, value = ctx.payload["event"], ctx.payload.get("value")
    fired: list[str] = []
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        contact = await session.get(Contact, contact_id)
        if business is None or contact is None or contact.erased_at is not None:
            return None
        rules = matching_rules(gtm_settings(business)["automations"], event, value)
        for rule in rules:
            action = rule.get("action", {})
            cause = {"automation": rule["key"], "event": event, "value": value}
            if action.get("type") == "enroll":
                if await enroll(session, business, contact, action["sequence"], cause=cause):
                    fired.append(rule["key"])
            elif action.get("type") == "side_state":
                changed = await change(
                    session, contact, side_state=action["value"], cause=Cause("automation", reason=rule["key"])
                )
                if changed and action.get("then_enroll"):
                    await enroll(session, business, contact, action["then_enroll"], cause=cause)
                fired.append(rule["key"])
    return {"fired": fired}


async def mark_inactive_customers(session: Any, business: Business) -> int:
    """Daily: customers with no activity for N days become At-risk (and start a win-back)."""
    count = 0
    for rule in gtm_settings(business)["automations"]:
        trigger = rule.get("trigger", {})
        if not rule.get("active", True) or trigger.get("type") != "inactive_days":
            continue
        cutoff = utcnow() - timedelta(days=int(trigger.get("days", 14)))
        stages = trigger.get("stages", ["customer", "active"])
        rows = (
            (
                await session.execute(
                    select(Contact)
                    .where(
                        Contact.business_id == business.id,
                        Contact.stage.in_(stages),
                        Contact.side_state.is_(None),
                        Contact.erased_at.is_(None),
                        Contact.last_activity_at < cutoff,
                    )
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        action = rule.get("action", {})
        for contact in rows:
            if await change(
                session,
                contact,
                side_state=action.get("value", "at_risk"),
                cause=Cause("automation", reason=rule["key"]),
            ):
                count += 1
    return count
