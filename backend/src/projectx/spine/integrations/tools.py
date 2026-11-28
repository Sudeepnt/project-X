"""The other tools a business can bring its own keys for. Each test makes one cheap, read-only call."""

from __future__ import annotations

from typing import Any

import aiosmtplib

from projectx.services import Services
from projectx.spine.integrations.catalog import KeyField, Tool, register_tool


async def _test_razorpay(services: Services, v: dict[str, Any]) -> str | None:
    res = await services.http.get(
        "https://api.razorpay.com/v1/payments", params={"count": 1}, auth=(v["key_id"], v["key_secret"])
    )
    return None if res.status_code < 400 else f"Razorpay answered {res.status_code}"


async def _test_smtp(services: Services, v: dict[str, Any]) -> str | None:
    try:
        smtp = aiosmtplib.SMTP(
            hostname=v["host"], port=int(v.get("port") or 587), start_tls=bool(v.get("starttls", True)), timeout=10
        )
        await smtp.connect()
        if v.get("username"):
            await smtp.login(v["username"], v["password"])
        await smtp.quit()
    except Exception as exc:
        return f"couldn't sign in to the mail server ({type(exc).__name__})"
    return None


async def _test_anthropic(services: Services, v: dict[str, Any]) -> str | None:
    res = await services.http.get(
        "https://api.anthropic.com/v1/models", headers={"x-api-key": v["api_key"], "anthropic-version": "2023-06-01"}
    )
    return None if res.status_code < 400 else f"Anthropic answered {res.status_code}"


async def _test_openai_compat(services: Services, v: dict[str, Any]) -> str | None:
    headers = {"Authorization": f"Bearer {v['api_key']}"} if v.get("api_key") else {}
    res = await services.http.get(str(v["base_url"]).rstrip("/") + "/models", headers=headers)
    return None if res.status_code < 400 else f"the model server answered {res.status_code}"


register_tool(
    Tool(
        key="razorpay",
        title="Razorpay",
        category="payments",
        description="Take payments in India: UPI, cards, netbanking.",
        fields=(
            KeyField("key_id", "Key id", secret=False),
            KeyField("key_secret", "Key secret"),
            KeyField("webhook_secret", "Webhook secret"),
        ),
        test=_test_razorpay,
        webhook_path="/webhooks/payments/razorpay/{slug}",
    )
)
register_tool(
    Tool(
        key="smtp",
        title="Your email sending",
        category="email",
        description="Send invoices, replies and reminders from your own domain "
        "(any SMTP: Google, Microsoft, SES, Postmark…).",
        fields=(
            KeyField("host", "Server", secret=False),
            KeyField("port", "Port", secret=False, hint="587"),
            KeyField("username", "Username", secret=False, required=False),
            KeyField("password", "Password or app password", required=False),
            KeyField("from_address", "Send as", secret=False, hint="Your Business <hello@yourdomain.com>"),
            KeyField("starttls", "Use STARTTLS", secret=False, required=False),
        ),
        platform=lambda s: True,
        platform_note="Use ours: mail goes out from the platform's address with your business name.",
        test=_test_smtp,
    )
)
register_tool(
    Tool(
        key="openai_compat",
        title="Your open model server",
        category="ai",
        description="Your own open-weight models (vLLM, Ollama, Together, Fireworks…) for everyday work.",
        fields=(
            KeyField("base_url", "Server URL", secret=False, hint="https://…/v1"),
            KeyField("api_key", "API key", required=False),
            KeyField("small", "Small model", secret=False),
            KeyField("mid", "Mid model", secret=False, required=False),
            KeyField("frontier", "Large model", secret=False, required=False),
        ),
        platform=lambda s: s.openai_compat_base_url is not None,
        platform_note="Use ours: the platform's open models, within your plan's daily AI budget.",
        test=_test_openai_compat,
    )
)
register_tool(
    Tool(
        key="anthropic",
        title="Claude (hard jobs)",
        category="ai",
        description="Your own Claude key for hard jobs like specs and code. Everyday work stays on open models.",
        fields=(
            KeyField("api_key", "API key", hint="sk-ant-…"),
            KeyField("model", "Model", secret=False, required=False, hint="claude-opus-5-5"),
        ),
        platform=lambda s: s.anthropic_api_key is not None,
        platform_note="Use ours: hard jobs run on the platform's Claude account within your plan's budget.",
        test=_test_anthropic,
    )
)
