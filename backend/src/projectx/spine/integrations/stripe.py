"""Stripe for each business's own customers, with the business's keys or through the platform (Stripe Connect).

- Own keys: calls use the business's secret key; Stripe sends events to /webhooks/stripe/<slug>, signed with
  the business's webhook secret.
- Ours: the platform creates a connected account per business (Express); the founder finishes Stripe's own
  onboarding; money goes to their account, charges carry the `Stripe-Account` header and an optional platform
  fee; events arrive once at /webhooks/stripe/connect and are routed by account id.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from projectx.kernel.errors import ExternalServiceError, NotConfigured
from projectx.services import Services
from projectx.spine.integrations.catalog import KeyField, Tool, register_tool
from projectx.spine.integrations.service import Resolved, resolve

API = "https://api.stripe.com/v1"


def form(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: nested dicts and lists become `a[b][0][c]=…`."""
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if v is None:
            continue
        if isinstance(v, dict):
            out += form(v, key)
        elif isinstance(v, list | tuple):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out += form(item, f"{key}[{i}]")
                else:
                    out.append((f"{key}[{i}]", str(item)))
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        else:
            out.append((key, str(v)))
    return out


async def call(
    http: httpx.AsyncClient,
    secret_key: str,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    *,
    account: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {secret_key}"}
    if account:
        headers["Stripe-Account"] = account
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    pairs = form(data or {})
    if method == "GET":
        res = await http.request(method, f"{API}{path}", params=urlencode(pairs), headers=headers)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        res = await http.request(method, f"{API}{path}", content=urlencode(pairs), headers=headers)
    body: dict[str, Any] = res.json() if res.content else {}
    if res.status_code >= 400:
        message = (body.get("error") or {}).get("message", res.text[:200])
        raise ExternalServiceError(f"Stripe: {message}", extra={"status": res.status_code})
    return body


class StripeFor:
    """Stripe as one business sees it: the right key, and the connected account when using ours."""

    def __init__(self, services: Services, resolved: Resolved) -> None:
        self.services, self.r = services, resolved
        key = resolved.secrets.get("secret_key")
        if not key:
            raise NotConfigured("Stripe has no secret key configured")
        self.key: str = key
        self.account: str | None = resolved.settings.get("account_id") if resolved.mode == "platform" else None

    @classmethod
    async def for_business(cls, services: Services, business_id: uuid.UUID) -> StripeFor:
        return cls(services, await resolve(services, business_id, "stripe", purpose="stripe api"))

    @property
    def fee_bp(self) -> int:
        return self.services.settings.stripe_application_fee_bp if self.r.mode == "platform" else 0

    async def __call__(
        self, method: str, path: str, data: dict[str, Any] | None = None, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await call(
            self.services.http, self.key, method, path, data, account=self.account, idempotency_key=idempotency_key
        )


async def _test_keys(services: Services, values: dict[str, Any]) -> str | None:
    key = str(values.get("secret_key", ""))
    if not key.startswith(("sk_", "rk_")):
        return "the secret key starts with sk_ (or rk_ for a restricted key)"
    try:
        await call(services.http, key, "GET", "/balance")
    except ExternalServiceError as exc:
        return exc.detail
    return None


register_tool(
    Tool(
        key="stripe",
        title="Stripe",
        category="payments",
        description="Take card payments for invoices and subscriptions.",
        fields=(
            KeyField("secret_key", "Secret key", hint="sk_live_… or a restricted key"),
            KeyField("webhook_secret", "Webhook signing secret", hint="whsec_… from the endpoint you add in Stripe"),
        ),
        platform=lambda s: s.stripe_secret_key is not None and s.stripe_connect_webhook_secret is not None,
        platform_note="Use ours: you finish a short Stripe sign-up and the money lands in your own Stripe account.",
        test=_test_keys,
        webhook_path="/webhooks/stripe/{slug}",
    )
)


async def start_connect(services: Services, business: Any, *, return_url: str) -> tuple[str, str]:
    """Create (or reuse) the business's connected account and return (account id, onboarding link)."""
    key = services.settings.stripe_secret_key
    if key is None:
        raise NotConfigured("The platform's Stripe isn't set up")
    secret = key.get_secret_value()
    from projectx.spine.integrations.service import _row

    async with services.db.tenant(business.id) as session:
        row = await _row(session, "stripe")
        account = (row.settings or {}).get("account_id") if row and row.mode == "platform" else None
    if not account:
        created = await call(
            services.http,
            secret,
            "POST",
            "/accounts",
            {
                "type": "express",
                "country": (business.country or "US").upper(),
                "business_profile": {"name": business.name},
                "metadata": {"business_id": str(business.id)},
            },
            idempotency_key=f"connect:{business.id}",
        )
        account = str(created["id"])
    link = await call(
        services.http,
        secret,
        "POST",
        "/account_links",
        {"account": account, "refresh_url": return_url, "return_url": return_url, "type": "account_onboarding"},
    )
    return account, str(link["url"])


async def connect_ready(services: Services, account: str) -> bool:
    key = services.settings.stripe_secret_key
    assert key is not None
    acct = await call(services.http, key.get_secret_value(), "GET", f"/accounts/{account}")
    return bool(acct.get("charges_enabled"))
