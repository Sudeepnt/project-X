"""Public, unauthenticated routes: tracked-link redirects, landing pages, double opt-in
confirmation, one-click unsubscribe (RFC 8058) and referral links. Server-rendered HTML with no
JavaScript: fast on any phone, private by default."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from projectx.api.deps import client_ip
from projectx.kernel.errors import NotFound, RateLimited
from projectx.modules.gtm.crm.lifecycle import (
    Cause,
    ConsentInput,
    grant_consent,
    record_touch,
    revoke_consent,
    upsert_contact,
)
from projectx.modules.gtm.growth.landing import (
    TOUCH_FOR_KIND,
    choose_variant,
    confirm_token,
    parse_confirm_token,
    record_submit,
    record_view,
)
from projectx.modules.gtm.growth.links import attribution_for_click, destination_with_params, record_click, visitor_hash
from projectx.modules.gtm.models import BrandBook, LandingPage, LandingVariant, TrackedLink
from projectx.modules.gtm.nurture.sequences import parse_unsubscribe_token, stop_enrollments
from projectx.services import Services
from projectx.spine.identity.security import hash_ip, throttle
from projectx.spine.jobs.queue import enqueue
from projectx.spine.ledger.service import Actor
from projectx.spine.lifecycle import queue_email
from projectx.spine.mail import render
from projectx.spine.routing.models import RoutingKey
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "templates"))


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


async def _business_by_slug(services: Services, slug: str) -> Business:
    async with services.db.session() as session:
        business = await session.scalar(select(Business).where(Business.slug == slug, Business.status == "active"))
    if business is None:
        raise NotFound("Page not found")
    return business


async def _brand(session: Any, business_id: uuid.UUID) -> dict[str, Any]:
    book = await session.scalar(
        select(BrandBook)
        .where(BrandBook.business_id == business_id, BrandBook.status == "approved")
        .order_by(BrandBook.version.desc())
        .limit(1)
    )
    colors = (book.visual or {}).get("colors", []) if book else []
    primary = next((c.get("hex") for c in colors if c.get("role") == "primary"), None) or (
        colors[0].get("hex") if colors else None
    )
    return {"primary": primary or "#0071e3"}


def _form_token(secret: str, page_id: uuid.UUID, ts: int) -> str:
    return hmac.new(secret.encode(), f"form:{page_id}:{ts}".encode(), hashlib.sha256).hexdigest()[:24]


def _message_page(
    request: Request, *, title: str, body: str, business: Business, status_code: int = 200, primary: str = "#0071e3"
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "public/message.html",
        {"title": title, "body": body, "business": business, "primary": primary},
        status_code=status_code,
    )


# ----------------------------------------------------------------- tracked links


@router.get("/l/{slug}")
async def follow_link(slug: str, request: Request) -> Response:
    services = _services(request)
    async with services.db.session() as session:
        route = await session.get(RoutingKey, ("link", slug[:16]))
    if route is None or route.target_id is None:
        return HTMLResponse("<h1>Link not found</h1>", status_code=404)
    async with services.db.tenant(route.business_id) as session:
        link = await session.get(TrackedLink, route.target_id)
        if link is None:
            return HTMLResponse("<h1>Link not found</h1>", status_code=404)
        click_id = await record_click(
            session,
            link,
            user_agent=request.headers.get("user-agent", ""),
            referrer=request.headers.get("referer"),
            country=request.headers.get("cf-ipcountry") or request.headers.get("x-vercel-ip-country"),
            ip=client_ip(request),
            secret=services.settings.secret_key.get_secret_value(),
        )
        target = destination_with_params(link, click_id)
    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})


# ----------------------------------------------------------------- landing pages


async def _load_page(
    services: Services, business: Business, page_slug: str
) -> tuple[LandingPage, list[LandingVariant]]:
    async with services.db.tenant(business.id) as session:
        page = await session.scalar(
            select(LandingPage).where(
                LandingPage.business_id == business.id, LandingPage.slug == page_slug, LandingPage.status == "published"
            )
        )
        if page is None:
            raise NotFound("Page not found")
        variants = list(
            (
                await session.execute(
                    select(LandingVariant).where(LandingVariant.page_id == page.id).order_by(LandingVariant.key)
                )
            ).scalars()
        )
    return page, variants


@router.get("/p/{business_slug}/{page_slug}")
async def landing_page(business_slug: str, page_slug: str, request: Request) -> Response:
    services = _services(request)
    business = await _business_by_slug(services, business_slug)
    page, variants = await _load_page(services, business, page_slug)
    cookie_name = f"pxv_{str(page.id)[:8]}"
    variant = choose_variant(variants, request.cookies.get(cookie_name))
    click_id = request.query_params.get("pxc")
    secret = services.settings.secret_key.get_secret_value()
    visitor = visitor_hash(secret, client_ip(request), request.headers.get("user-agent", ""))
    async with services.db.tenant(business.id) as session:
        await record_view(session, page, variant, click_id=click_id, visitor=visitor)
        brand = await _brand(session, business.id)
    ts = int(time.time())
    response = templates.TemplateResponse(
        request,
        "public/landing.html",
        {
            "business": business,
            "page": page,
            "variant": variant,
            "content": variant.content,
            "brand": brand,
            "consent_text": page.settings.get("consent_text"),
            "fields": page.settings.get("fields", ["email", "name"]),
            "hidden": {
                "pxc": click_id or "",
                "v": variant.key,
                "ts": str(ts),
                "tk": _form_token(secret, page.id, ts),
                "ref": request.query_params.get("ref") or request.cookies.get("pxr", ""),
                "utm_source": request.query_params.get("utm_source", ""),
                "utm_campaign": request.query_params.get("utm_campaign", ""),
            },
            "primary": brand["primary"],
        },
    )
    response.set_cookie(
        cookie_name,
        variant.key,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=services.settings.secure_cookies,
    )
    return response


@router.post("/p/{business_slug}/{page_slug}")
async def submit_landing_page(business_slug: str, page_slug: str, request: Request) -> Response:
    services = _services(request)
    business = await _business_by_slug(services, business_slug)
    page, variants = await _load_page(services, business, page_slug)
    form = await request.form()
    values = {k: str(v).strip() for k, v in form.items()}
    secret = services.settings.secret_key.get_secret_value()
    primary = "#0071e3"
    # Spam defences: honeypot field, signed render timestamp (too fast or too old = bot), per-IP limit.
    try:
        ts = int(values.get("ts", "0"))
    except ValueError:
        ts = 0
    age = time.time() - ts
    bot = bool(values.get("website")) or not hmac.compare_digest(values.get("tk", ""), _form_token(secret, page.id, ts))
    if bot or age < 1.5 or age > 86400:
        return _message_page(request, title="Thanks!", body="We got your message.", business=business)
    email = values.get("email", "").lower()
    if "@" not in email or len(email) > 254:
        return _message_page(
            request,
            title="Please check your email address",
            body="That doesn't look like a valid email. Go back and try again.",
            business=business,
            status_code=422,
        )
    ip_hash = hash_ip(client_ip(request), secret) or "unknown"
    async with services.db.session() as session:
        try:
            await throttle(session, f"landing:{page.id}:{ip_hash}", limit=10, window=timedelta(hours=1))
        except RateLimited:
            return _message_page(
                request,
                title="Too many tries",
                body="Please wait a little and try again.",
                business=business,
                status_code=429,
            )
    consent_given = values.get("consent") in ("on", "true", "1", "yes")
    double_opt_in = bool(page.settings.get("double_opt_in"))
    stage = page.settings.get("stage_on_submit", "lead")
    actor = Actor.public()
    async with services.db.tenant(business.id) as session:
        attribution, click = await attribution_for_click(session, business.id, values.get("pxc"))
        evidence = {
            "page_id": str(page.id),
            "variant": values.get("v"),
            "text": page.settings.get("consent_text"),
            "ip_hash": ip_hash,
            "user_agent": request.headers.get("user-agent", "")[:200],
        }
        consent = (
            ConsentInput(
                channel="email",
                purpose="newsletter" if page.kind == "newsletter" else "marketing",
                source="landing_page",
                evidence=evidence,
            )
            if consent_given and not double_opt_in
            else None
        )
        contact, _ = await upsert_contact(
            session,
            business,
            source="landing_page",
            cause=Cause("landing_page", page.id, actor=actor),
            email=email,
            name=values.get("name") or None,
            stage=stage,
            attribution=attribution,
            consent=consent,
            attributes={k: values[k][:500] for k in ("company", "message", "phone") if values.get(k)},
            referred_by_code=values.get("ref") or None,
        )
        if click is not None:
            click.contact_id = contact.id
            await record_touch(
                session,
                business,
                contact,
                kind="link_click",
                channel=attribution.platform if attribution else None,
                campaign_id=attribution.campaign_id if attribution else None,
                post_id=attribution.post_id if attribution else None,
                link_id=attribution.link_id if attribution else None,
            )
        await record_touch(
            session,
            business,
            contact,
            kind=TOUCH_FOR_KIND.get(page.kind, "form_submit"),
            campaign_id=page.campaign_id,
            data={"page_id": str(page.id), "variant": values.get("v")},
            cause=Cause("landing_page", page.id, actor=actor),
        )
        await record_submit(session, page, values.get("v"), click_id=values.get("pxc"), visitor=None)
        if consent_given and double_opt_in:
            contact.attributes = {**(contact.attributes or {}), "pending_consent": evidence}
            token = confirm_token(secret, business.id, contact.id, page.id)
            link = f"{services.settings.base_url}/c/{token}"
            await queue_email(
                session,
                to=email,
                subject=f"Confirm your subscription to {business.name}",
                html=render(
                    "email/action.html",
                    title="One more step",
                    name=contact.name or "there",
                    body=f"Confirm you want emails from {business.name}.",
                    action_label="Confirm subscription",
                    action_url=link,
                    app_name=business.name,
                ),
                text=f"Confirm your subscription to {business.name}: {link}",
                business_id=business.id,
            )
        brand = await _brand(session, business.id)
        primary = brand["primary"]
    variant_content = next(
        (v.content for v in variants if v.key == values.get("v")), variants[0].content if variants else {}
    )
    thank_you = (variant_content or {}).get("thank_you") or "Thank you. We'll be in touch."
    redirect_url = page.settings.get("redirect_url")
    if redirect_url:
        return RedirectResponse(redirect_url, status_code=303)
    return _message_page(request, title="Thank you", body=thank_you, business=business, primary=primary)


@router.get("/c/{token}")
async def confirm_subscription(token: str, request: Request) -> Response:
    services = _services(request)
    parsed = parse_confirm_token(services.settings.secret_key.get_secret_value(), token)
    if parsed is None:
        return HTMLResponse("<h1>This link is invalid</h1>", status_code=400)
    business_id, contact_id, _page_id = parsed
    async with services.db.session() as session:
        business = await session.get(Business, business_id)
    if business is None:
        return HTMLResponse("<h1>This link is invalid</h1>", status_code=400)
    async with services.db.tenant(business_id) as session:
        contact = await session.get(Contact, contact_id)
        if contact is None or contact.erased_at is not None:
            return HTMLResponse("<h1>This link is invalid</h1>", status_code=400)
        pending = (contact.attributes or {}).get("pending_consent")
        if pending:
            await grant_consent(
                session,
                contact,
                ConsentInput(channel="email", purpose="newsletter", source="double_opt_in", evidence=pending),
                actor=Actor.public(),
            )
            attrs = dict(contact.attributes or {})
            attrs.pop("pending_consent", None)
            contact.attributes = attrs
            await enqueue(
                session,
                "gtm.run_automations",
                {"contact_id": str(contact.id), "event": "stage_entered", "value": contact.stage},
                business_id=business_id,
            )
    return _message_page(
        request,
        title="You're subscribed",
        body=f"Thanks for confirming. Welcome to {business.name}.",
        business=business,
    )


# ----------------------------------------------------------------- unsubscribe


async def _unsubscribe_target(services: Services, token: str) -> tuple[Business, uuid.UUID] | None:
    parsed = parse_unsubscribe_token(services.settings.secret_key.get_secret_value(), token)
    if parsed is None:
        return None
    business_id, contact_id = parsed
    async with services.db.session() as session:
        business = await session.get(Business, business_id)
    return (business, contact_id) if business else None


@router.get("/u/{token}")
async def unsubscribe_page(token: str, request: Request) -> Response:
    services = _services(request)
    target = await _unsubscribe_target(services, token)
    if target is None:
        return HTMLResponse("<h1>This link is invalid</h1>", status_code=400)
    business, _ = target
    return templates.TemplateResponse(
        request, "public/unsubscribe.html", {"business": business, "token": token, "primary": "#0071e3"}
    )


@router.post("/u/{token}")
async def unsubscribe(token: str, request: Request) -> Response:
    services = _services(request)
    target = await _unsubscribe_target(services, token)
    if target is None:
        return HTMLResponse("<h1>This link is invalid</h1>", status_code=400)
    business, contact_id = target
    async with services.db.tenant(business.id) as session:
        contact = await session.get(Contact, contact_id)
        if contact is not None and contact.unsubscribed_at is None:
            await revoke_consent(session, contact, channel="email", source="unsubscribe_link", actor=Actor.public())
            await stop_enrollments(session, business.id, contact.id, reason="unsubscribed")
    return _message_page(
        request,
        title="You're unsubscribed",
        body=f"You won't get marketing emails from {business.name} any more.",
        business=business,
    )


# ----------------------------------------------------------------- referrals


@router.get("/r/{business_slug}/{code}")
async def referral(business_slug: str, code: str, request: Request) -> Response:
    services = _services(request)
    business = await _business_by_slug(services, business_slug)
    destination = (
        (business.settings or {}).get("gtm", {}).get("referral_destination")
        or business.website
        or f"{services.settings.base_url}/p/{business.slug}"
    )
    separator = "&" if "?" in destination else "?"
    response = RedirectResponse(f"{destination}{separator}{urlencode({'ref': code[:16]})}", status_code=302)
    response.set_cookie(
        "pxr",
        code[:16],
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=services.settings.secure_cookies,
    )
    return response
