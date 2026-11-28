"""Bring your own keys, or use ours: one place every department asks "how do I reach this tool for this business?".

    creds = await resolve(services, business_id, "stripe")   # -> Resolved(mode="own"|"platform", secrets, settings)

- Own keys: checked against the field list, tested with a live call, sealed in the vault, never shown again
  (only the last four characters are kept for display). Every change is a ledger event without the secret.
- Platform: the business uses the platform's account (from server settings); some tools need a per-business
  setup step first (Stripe Connect onboarding), tracked as status "pending" until done.
- Nothing configured: a clear `NotConfigured` saying what to add.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotConfigured, NotFound, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.services import Services
from projectx.spine.integrations.catalog import Tool, all_tools, get_tool
from projectx.spine.integrations.models import Integration
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append


class IntegrationChanged(EventData):
    tool: str
    mode: str
    status: str


register_event(
    "spine.integration.changed", "A business connected, switched or removed an outside tool", IntegrationChanged
)


@dataclass(frozen=True)
class Resolved:
    tool: str
    mode: str  # own | platform
    secrets: dict[str, Any] = field(repr=False)  # never log or return these
    settings: dict[str, Any] = field(default_factory=dict)


def tool_or_404(key: str) -> Tool:
    tool = get_tool(key)
    if tool is None:
        raise NotFound(f"Unknown tool {key!r}")
    return tool


async def _row(session: AsyncSession, tool: str, *, lock: bool = False) -> Integration | None:
    stmt = select(Integration).where(Integration.tool == tool)
    if lock:
        stmt = stmt.with_for_update()
    row: Integration | None = await session.scalar(stmt)
    return row


async def _event(session: AsyncSession, business_id: uuid.UUID, row: Integration, actor: Actor) -> None:
    await append(
        session,
        business_id=business_id,
        type="spine.integration.changed",
        actor=actor,
        subject=("integration", row.id),
        data={"tool": row.tool, "mode": row.mode, "status": row.status},
    )


def clean_values(tool: Tool, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split submitted values into (secrets, safe settings), refusing unknown or missing fields."""
    known = {f.name: f for f in tool.fields}
    unknown = set(values) - set(known)
    if unknown:
        raise ValidationFailed(f"Unknown field(s) for {tool.title}: {', '.join(sorted(unknown))}")
    secrets: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    for name, f in known.items():
        value = values.get(name)
        if isinstance(value, str):
            value = value.strip()
        if value in (None, ""):
            if f.required:
                raise ValidationFailed(f"{tool.title} needs {f.label}")
            continue
        if f.secret:
            secrets[name] = value
            settings[f"{name}_hint"] = "…" + str(value)[-4:]
        else:
            settings[name] = value
    return secrets, settings


async def save_own(
    session: AsyncSession,
    services: Services,
    business_id: uuid.UUID,
    tool_key: str,
    values: dict[str, Any],
    *,
    actor: Actor,
) -> Integration:
    tool = tool_or_404(tool_key)
    secrets, settings = clean_values(tool, values)
    problem = await tool.test(services, {**secrets, **settings}) if tool.test else None
    if problem:
        raise ValidationFailed(f"{tool.title} refused these keys: {problem}", code="integration_test_failed")
    row = await _row(session, tool.key, lock=True)
    if row is None:
        row = Integration(business_id=business_id, tool=tool.key)
        session.add(row)
    if row.secret_id is not None:
        await services.vault.update(session, business_id, row.secret_id, secrets)
    else:
        row.secret_id = await services.vault.put(
            session, business_id, kind="api_key", label=f"tool:{tool.key}", secret=secrets
        )
    row.mode, row.settings, row.status = "own", settings, "ok" if tool.test else "unverified"
    row.last_error, row.checked_at = None, utcnow()
    await session.flush()
    await _event(session, business_id, row, actor)
    return row


async def use_platform(
    session: AsyncSession,
    services: Services,
    business_id: uuid.UUID,
    tool_key: str,
    *,
    actor: Actor,
    status: str = "ok",
    settings: dict[str, Any] | None = None,
) -> Integration:
    tool = tool_or_404(tool_key)
    if not tool.platform(services.settings):
        raise NotConfigured(f"The platform doesn't offer {tool.title} yet; add your own keys instead.")
    row = await _row(session, tool.key, lock=True)
    if row is None:
        row = Integration(business_id=business_id, tool=tool.key)
        session.add(row)
    if row.secret_id is not None:  # switching to ours: the business's own keys are destroyed, not kept around
        await services.vault.delete(session, business_id, row.secret_id)
        row.secret_id = None
    kept = dict(row.settings) if row.mode == "platform" else {}  # own-key hints don't carry over
    row.mode, row.status, row.last_error, row.checked_at = "platform", status, None, utcnow()
    row.settings = {**kept, **(settings or {})}
    await session.flush()
    await _event(session, business_id, row, actor)
    return row


async def turn_off(
    session: AsyncSession, services: Services, business_id: uuid.UUID, tool_key: str, *, actor: Actor
) -> None:
    row = await _row(session, tool_or_404(tool_key).key, lock=True)
    if row is None:
        return
    if row.secret_id is not None:
        await services.vault.delete(session, business_id, row.secret_id)
    row.secret_id, row.mode, row.status, row.settings = None, "off", "unverified", {}
    await _event(session, business_id, row, actor)


async def resolve(services: Services, business_id: uuid.UUID, tool_key: str, *, purpose: str = "use") -> Resolved:
    """The credentials to use right now. Opens its own short transaction; never hold the result longer than a call."""
    tool = tool_or_404(tool_key)
    async with services.db.tenant(business_id) as session:
        row = await _row(session, tool.key)
        if row is None or row.mode == "off":
            raise NotConfigured(f"{tool.title} isn't set up for this business. Add your keys or use ours.")
        if row.mode == "own":
            assert row.secret_id is not None
            secrets = await services.vault.borrow(
                session, business_id, row.secret_id, purpose=purpose, actor=f"tool:{tool.key}"
            )
            return Resolved(tool.key, "own", secrets, dict(row.settings))
        if row.status == "pending":
            raise NotConfigured(f"{tool.title} setup isn't finished yet.", code="integration_pending")
        return Resolved(tool.key, "platform", platform_secrets(services, tool.key), dict(row.settings))


async def is_ready(services: Services, business_id: uuid.UUID, tool_key: str) -> bool:
    try:
        await resolve(services, business_id, tool_key, purpose="check")
    except NotConfigured:
        return False
    return True


def platform_secrets(services: Services, tool_key: str) -> dict[str, Any]:
    s = services.settings

    def get(v: Any) -> str | None:
        return v.get_secret_value() if v is not None else None

    return {
        "stripe": {"secret_key": get(s.stripe_secret_key)},
        "smtp": {},
        "anthropic": {"api_key": get(s.anthropic_api_key)},
        "openai_compat": {"api_key": get(s.openai_compat_api_key), "base_url": s.openai_compat_base_url},
    }.get(tool_key, {})


async def overview(session: AsyncSession, services: Services) -> list[dict[str, Any]]:
    rows = {r.tool: r for r in (await session.execute(select(Integration))).scalars()}
    out = []
    for tool in all_tools():
        row = rows.get(tool.key)
        out.append(
            {
                "tool": tool.key,
                "title": tool.title,
                "category": tool.category,
                "description": tool.description,
                "fields": [
                    {"name": f.name, "label": f.label, "secret": f.secret, "required": f.required, "hint": f.hint}
                    for f in tool.fields
                ],
                "platform_available": tool.platform(services.settings),
                "platform_note": tool.platform_note,
                "mode": row.mode if row else "off",
                "status": row.status if row else "unverified",
                "settings": row.settings if row else {},
                "last_error": row.last_error if row else None,
                "webhook_path": tool.webhook_path,
            }
        )
    return out


# ---------------------------------------------------------------- the business's own models and mail server


def model_route_resolver(services: Services) -> Any:
    """For the gateway: a business with its own model server or Claude key uses them; others use the platform's."""
    from projectx.spine.gateway.gateway import Route, parse_rung
    from projectx.spine.gateway.providers import AnthropicProvider, OpenAICompatibleProvider

    clients: dict[str, Any] = {}  # one client per key, reused across businesses' calls

    async def resolver(business_id: uuid.UUID) -> Route | None:
        async with services.db.tenant(business_id) as session:
            rows = {
                r.tool: r
                for r in (
                    await session.execute(
                        select(Integration).where(Integration.tool.in_(("openai_compat", "anthropic")))
                    )
                ).scalars()
            }
        own = {t: r for t, r in rows.items() if r.mode == "own" and r.secret_id}
        if not own:
            return None
        s = services.settings
        rungs = {"small": parse_rung(s.llm_small), "mid": parse_rung(s.llm_mid), "frontier": parse_rung(s.llm_frontier)}
        hard = parse_rung(s.llm_hard)
        providers: dict[str, Any] = {}
        if "openai_compat" in own:
            creds = await resolve(services, business_id, "openai_compat", purpose="model call")
            ident = f"oai:{creds.settings['base_url']}:{hash(creds.secrets.get('api_key'))}"
            providers["own_openai"] = clients.setdefault(
                ident, OpenAICompatibleProvider(creds.settings["base_url"], creds.secrets.get("api_key"))
            )
            small = creds.settings["small"]
            mid = creds.settings.get("mid") or small
            rungs = {
                "small": ("own_openai", small),
                "mid": ("own_openai", mid),
                "frontier": ("own_openai", creds.settings.get("frontier") or mid),
            }
        if "anthropic" in own:
            creds = await resolve(services, business_id, "anthropic", purpose="model call")
            ident = f"ant:{hash(creds.secrets['api_key'])}"
            providers["own_anthropic"] = clients.setdefault(ident, AnthropicProvider(creds.secrets["api_key"]))
            hard = (
                "own_anthropic",
                creds.settings.get("model") or (hard[1] if hard[0] == "anthropic" else "claude-opus-5-5"),
            )
        return Route(rungs=rungs, hard=hard, providers=providers)  # type: ignore[arg-type]

    return resolver


async def mail_server(services: Services, business_id: uuid.UUID | None) -> dict[str, Any] | None:
    """The business's own SMTP server, if it set one; None means send through the platform's."""
    if business_id is None:
        return None
    async with services.db.tenant(business_id) as session:
        row = await _row(session, "smtp")
        if row is None or row.mode != "own":
            return None
    creds = await resolve(services, business_id, "smtp", purpose="send email")
    return {**creds.settings, **creds.secrets}
