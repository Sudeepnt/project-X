"""`projectx seed-demo`: a practice company with realistic data, so the cockpit can be explored at once.

Everything runs in practice mode: platforms are simulated, models use offline drafts, and no
message leaves the machine (email goes to the configured SMTP server, e.g. Mailpit locally).
"""

from __future__ import annotations

import random
import secrets
from datetime import date, timedelta

from sqlalchemy import select, text, update

from projectx.logconfig import configure_logging
from projectx.services import Services
from projectx.settings import get_settings
from projectx.spine.identity.models import User
from projectx.spine.identity.security import hash_password
from projectx.spine.ledger.service import Actor
from projectx.spine.modules import load_modules

DEMO_EMAIL = "demo@projectx.local"

FIRST = [
    "Aarav",
    "Diya",
    "Kabir",
    "Meera",
    "Rohan",
    "Sara",
    "Vikram",
    "Anaya",
    "Ishaan",
    "Priya",
    "Liam",
    "Emma",
    "Noah",
    "Olivia",
    "Zoe",
    "Arjun",
    "Nisha",
    "Dev",
    "Tara",
    "Omar",
]
LAST = ["Shah", "Iyer", "Khan", "Patel", "Rao", "Menon", "Gupta", "Singh", "Das", "Nair", "Reed", "Clark"]


async def run_seed() -> None:
    settings = get_settings()
    configure_logging(settings)
    load_modules()
    services = Services.create(settings)
    try:
        await _seed(services)
    finally:
        await services.close()


async def _seed(services: Services) -> None:
    from projectx.modules.gtm.brand.service import approve_book, generate_draft, save_draft
    from projectx.modules.gtm.campaigns.service import create_campaign, start_campaign
    from projectx.modules.gtm.channels.accounts import connect_practice
    from projectx.modules.gtm.crm.deals import create_deal, ensure_pipeline, move_deal
    from projectx.modules.gtm.crm.lifecycle import Attribution, Cause, ConsentInput, record_touch, upsert_contact
    from projectx.modules.gtm.crm.payments import record_payment
    from projectx.modules.gtm.goals.service import activate_goal, create_goal
    from projectx.modules.gtm.growth.landing import create_page, publish_page
    from projectx.modules.gtm.inbox.service import ingest
    from projectx.modules.gtm.models import ContentItem, Post
    from projectx.spine.decisions.models import Decision
    from projectx.spine.decisions.service import approve
    from projectx.spine.jobs.queue import enqueue
    from projectx.spine.jobs.worker import Worker
    from projectx.spine.tenancy.models import Business
    from projectx.spine.tenancy.service import create_business

    rng = random.Random(2026)  # noqa: S311 - demo data
    password = secrets.token_urlsafe(9)
    async with services.db.session() as session:
        user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if user is not None:
            user.password_hash = hash_password(password)
            print(f"Demo already exists. Password reset.\n  email:    {DEMO_EMAIL}\n  password: {password}")
            return
        user = User(email=DEMO_EMAIL, name="Demo Founder", password_hash=hash_password(password))
        session.add(user)
        await session.flush()
        business = await create_business(
            session,
            owner=user,
            name="Morning Ledger",
            website=None,
            industry="News media",
            business_model="media",
            country="IN",
            timezone="Asia/Kolkata",
            currency="INR",
            practice_mode=True,
            plan="scale",
            description="A five-minute morning briefing on business and money for busy professionals in India.",
        )
        business.settings = {
            **business.settings,
            "reply_to_email": "editor@morningledger.example",
            "postal_address": "Bengaluru, India",
            "gtm": {
                "newsletter": {"enabled": True, "send_hour": 7, "double_opt_in": True},
                "practice_failure_rate": 0.05,
                "public_dashboard": {"enabled": True, "metrics": ["revenue", "subscribers", "leads", "posts"]},
            },
        }
        bid, uid = business.id, user.id
    actor = Actor.user(uid)

    async with services.db.tenant(bid) as session:
        loaded = await session.get(Business, bid)
        assert loaded is not None
        business = loaded
        for platform, followers in (
            ("instagram", 8400),
            ("x", 3100),
            ("telegram", 1900),
            ("threads", 1200),
            ("linkedin", 2600),
            ("tiktok", 5200),
        ):
            await connect_practice(session, services, business, platform=platform, followers=followers, user_id=uid)
    draft = await generate_draft(services, business, site=None, notes="Plain, sharp, trustworthy. No hype.")
    async with services.db.tenant(bid) as session:
        loaded = await session.get(Business, bid)
        assert loaded is not None
        business = loaded
        book = await save_draft(session, business, draft, sources={}, created_by="agent:gtm.brand")
        await approve_book(session, business, book, user_id=uid)
        page = await create_page(
            session, business, title="The Morning Ledger newsletter", kind="newsletter", slug="newsletter"
        )
        await publish_page(session, business, page)

        today = date.today()
        goal, _ = await create_goal(
            session,
            business,
            goal_type="demand",
            metric="new_subscribers",
            target=600,
            deadline=today + timedelta(days=60),
            budget_minor=0,
            title=None,
            growth_approach="content_community",
            approval_mode="approve_new",
            guardrail=None,
            stop_rules=[],
            channels=None,
            paid_channels=None,
            diagnosis=None,
            ai_credit_budget_cents=None,
            actor=actor,
        )
        await activate_goal(session, business, goal, actor=actor)
        main = await create_campaign(
            session,
            business,
            playbook_key="newsletter_growth",
            goal_id=goal.id,
            channels=["instagram", "x", "telegram", "threads", "newsletter"],
        )
        support = await create_campaign(
            session,
            business,
            playbook_key="always_on_organic",
            goal_id=goal.id,
            role="supporting",
            channels=["instagram", "tiktok", "linkedin"],
        )
        await start_campaign(session, business, main, actor=actor)
        await start_campaign(session, business, support, actor=actor)

        # Contacts across the funnel, attributed to platforms, over the last 30 days.
        stages = ["subscriber"] * 70 + ["lead"] * 25 + ["mql"] * 10 + ["customer"] * 8 + ["advocate"] * 3
        platforms = ["instagram", "x", "telegram", "threads", "linkedin", "tiktok"]
        for i, stage in enumerate(stages):
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            contact, _ = await upsert_contact(
                session,
                business,
                source="landing_page",
                cause=Cause("landing_page", page.id, actor=Actor.public()),
                email=f"reader{i}@example.com",
                name=name,
                stage=stage,
                attribution=Attribution(campaign_id=main.id, platform=rng.choice(platforms)),
                consent=ConsentInput(
                    channel="email",
                    purpose="newsletter",
                    source="landing_page",
                    evidence={"page_id": str(page.id), "demo": True},
                ),
            )
            for _ in range(rng.randint(0, 4)):
                await record_touch(
                    session,
                    business,
                    contact,
                    kind=rng.choice(["link_click", "email_click", "comment"]),
                    campaign_id=main.id,
                )
        await session.execute(
            text("UPDATE contacts SET created_at = now() - (random() * interval '30 days') WHERE business_id = :b"),
            {"b": bid},
        )

        advertisers = await ensure_pipeline(session, bid, "advertisers")
        for j, (brand, value, stage) in enumerate(
            [
                ("Fintech Co", 250_000, "advertiser"),
                ("Neo Bank", 180_000, "proposal"),
                ("SaaS Tools", 90_000, "meeting"),
                ("Coffee Roasters", 60_000, "contacted"),
                ("Travel Club", 120_000, "prospect"),
            ]
        ):
            sponsor, _ = await upsert_contact(
                session,
                business,
                source="manual",
                cause=Cause("manual", actor=actor),
                email=f"partnerships@sponsor{j}.example",
                name=brand,
                stage="lead",
            )
            deal = await create_deal(
                session,
                business_id=bid,
                pipeline=advertisers,
                title=f"{brand} — newsletter slot",
                contact=sponsor,
                value_minor=value,
                currency="INR",
                deal_type="sponsorship",
                actor=actor,
            )
            if stage != "prospect":
                await move_deal(session, deal, stage_key=stage, actor=actor)
            if stage == "advertiser":
                await record_payment(
                    session,
                    business,
                    provider="manual",
                    external_id=f"demo-{j}",
                    amount_minor=value,
                    currency="INR",
                    contact_id=sponsor.id,
                    kind="one_time",
                    actor=actor,
                )
        for k in range(6):
            await record_payment(
                session,
                business,
                provider="razorpay",
                external_id=f"demo-member-{k}",
                amount_minor=49_900,
                currency="INR",
                email=f"reader{70 + k}@example.com",
                kind="subscription",
            )

    worker = Worker(services, concurrency=4)
    for _ in range(6):  # plan → generate → checks → proposals
        async with services.db.session() as session:
            await session.execute(
                text("UPDATE jobs SET run_at = now() WHERE status='queued' AND business_id = :b"), {"b": bid}
            )
        if await worker.run_until_idle() == 0:
            break
    async with services.db.tenant(bid) as session:
        cards = (
            (
                await session.execute(
                    select(Decision)
                    .where(
                        Decision.business_id == bid,
                        Decision.action_type == "gtm.publish_post",
                        Decision.status == "pending",
                    )
                    .order_by(Decision.created_at)
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        for card in cards[:6]:
            await approve(session, bid, card.id, uid)
        await session.execute(
            update(ContentItem).where(ContentItem.business_id == bid).values(planned_for=ContentItem.planned_for)
        )
    for _ in range(4):
        async with services.db.session() as session:
            await session.execute(
                text("UPDATE jobs SET run_at = now() WHERE status='queued' AND business_id = :b"), {"b": bid}
            )
        if await worker.run_until_idle() == 0:
            break
    async with services.db.tenant(bid) as session:
        await session.execute(
            update(Post)
            .where(Post.business_id == bid, Post.status == "published")
            .values(published_at=text("now() - (random() * interval '6 days') - interval '2 days'"))
        )
        await enqueue(session, "gtm.sync_metrics", business_id=bid)
        await enqueue(session, "gtm.weekly_report", business_id=bid)
        loaded = await session.get(Business, bid)
        assert loaded is not None
        business = loaded
        account_id = await session.scalar(
            text("SELECT id FROM gtm_channel_accounts WHERE business_id=:b AND platform='instagram'"), {"b": bid}
        )
        for kind, msg in (
            ("comment", "What time does the newsletter arrive?"),
            ("dm", "Hi, how much is a sponsor slot next month?"),
            ("comment", "This summary was brilliant, thank you!"),
        ):
            await ingest(
                session,
                business,
                platform="instagram",
                kind=kind,
                external_id=f"demo-{secrets.token_hex(4)}",
                text=msg,
                author_handle=f"reader_{secrets.token_hex(2)}",
                account_id=account_id,
            )
    await worker.run_until_idle()
    print("Practice company ready: Morning Ledger (media, practice mode).")
    print(f"  Sign in at {services.settings.web_url}")
    print(f"  email:    {DEMO_EMAIL}")
    print(f"  password: {password}")
