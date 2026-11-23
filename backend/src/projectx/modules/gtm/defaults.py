"""What a new business starts with: pipelines, nurture sequences and content themes.
Everything here is editable; it exists so the loop can run on day one."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.modules.gtm.crm.deals import ensure_pipeline
from projectx.modules.gtm.models import ContentTheme, Sequence
from projectx.spine.tenancy.models import Business

DEFAULT_SEQUENCES: list[dict[str, Any]] = [
    {
        "key": "welcome",
        "name": "Welcome",
        "trigger": {"type": "stage_entered", "stage": "subscriber"},
        "stop_on": {"stages": ["customer"]},
        "steps": [
            {
                "delay_hours": 0,
                "subject": "Welcome to {{ business_name }}",
                "body": "Hi {{ first_name }},\n\nThanks for joining {{ business_name }}. You'll hear from us when we "
                "have something worth your time, and never more than that.\n\nReply to this email any time: "
                "a real person reads every reply.",
            },
            {
                "delay_hours": 72,
                "subject": "What people read first",
                "body": "Hi {{ first_name }},\n\nHere's where most people start with {{ business_name }}: "
                "{{ business_website }}\n\nIf there's something you'd like us to cover, just reply.",
            },
        ],
    },
    {
        "key": "nurture_to_convert",
        "name": "Nurture to convert",
        "trigger": {"type": "stage_entered", "stage": "mql"},
        "stop_on": {"stages": ["customer", "active", "expansion", "advocate"]},
        "steps": [
            {
                "delay_hours": 2,
                "subject": "A quick question, {{ first_name }}",
                "body": "Hi {{ first_name }},\n\nYou've been reading a lot from {{ business_name }} lately. What are "
                "you trying to get done? If it helps, reply with one line and we'll point you to the right thing.",
            },
            {
                "delay_hours": 72,
                "subject": "How others solved it",
                "body": "Hi {{ first_name }},\n\nA short story from someone in a similar spot, and what changed for "
                "them after working with us.\n\nWant the same? Reply and we'll set up a call.",
            },
            {
                "delay_hours": 120,
                "subject": "Should I close your file?",
                "body": "Hi {{ first_name }},\n\nI haven't heard back, so I'll assume now isn't the right time. If "
                "that changes, reply to this email and it comes straight to us.",
            },
        ],
    },
    {
        "key": "onboarding",
        "name": "Onboarding",
        "trigger": {"type": "stage_entered", "stage": "customer"},
        "stop_on": {},
        "steps": [
            {
                "delay_hours": 0,
                "subject": "You're in: here's how to get your first win",
                "body": "Hi {{ first_name }},\n\nWelcome aboard, and thank you. The fastest first win is usually the "
                "simplest one. Reply with what you most want from {{ business_name }} and we'll make it happen.",
            },
            {
                "delay_hours": 96,
                "subject": "How is it going?",
                "body": "Hi {{ first_name }},\n\nA quick check-in: is everything working the way you hoped? "
                "If anything is unclear, reply and we'll fix it.",
            },
        ],
    },
    {
        "key": "win_back",
        "name": "Win-back",
        "trigger": {"type": "side_state_entered", "value": "churned"},
        "stop_on": {},
        "steps": [
            {
                "delay_hours": 24,
                "subject": "We miss you at {{ business_name }}",
                "body": "Hi {{ first_name }},\n\nIt's been a while. Here's what's new since you were last here. "
                "If something didn't work for you, we'd honestly like to know: just reply.",
            },
        ],
    },
    {
        "key": "referral_request",
        "name": "Referral request",
        "trigger": {"type": "satisfaction", "min": 9},
        "stop_on": {},
        "steps": [
            {
                "delay_hours": 4,
                "subject": "Know someone who'd like {{ business_name }}?",
                "body": "Hi {{ first_name }},\n\nThank you for the kind words. If you know someone who'd enjoy "
                "{{ business_name }}, share your link and you'll both get something good:\n\n{{ referral_link }}",
            },
        ],
    },
]

THEMES_BY_MODEL: dict[str, list[tuple[str, str, str]]] = {
    "media": [
        ("Daily stories", "educate", "The day's most important stories, told natively for each platform."),
        ("Explainers", "educate", "Why a story matters, in plain words, with numbers."),
        ("Debate of the day", "entertain", "One question that splits the audience; invites replies."),
        ("Behind the newsroom", "behind_the_scenes", "How stories are found, checked and made."),
        ("Reader voices", "proof", "Readers' reactions, replies and wins."),
        ("Advertise with us", "offer", "Audience proof and open ad slots for sponsors."),
    ],
    "service": [
        ("How we'd do it", "educate", "Practical walkthroughs of problems clients bring to us."),
        ("Client results", "proof", "Before-and-after stories with real numbers."),
        ("Mistakes to avoid", "educate", "Common errors and how to fix them."),
        ("Behind the work", "behind_the_scenes", "The team, the process and the tools."),
        ("Founder takes", "inspire", "Opinions and lessons from the founder."),
        ("Work with us", "offer", "Free audits, limited spots and how to start."),
    ],
}
THEMES_BY_MODEL["software"] = [
    ("Product tips", "educate", "Small ways to get more from the product."),
    ("Customer stories", "proof", "How customers use it and what changed."),
    ("Building in public", "behind_the_scenes", "What we shipped, what broke, what we learned."),
    ("Industry takes", "inspire", "Where the market is going and why."),
    ("Launches", "offer", "New features, trials and offers."),
]
THEMES_BY_MODEL["default"] = THEMES_BY_MODEL["service"]


async def seed_defaults(session: AsyncSession, business: Business) -> None:
    await ensure_pipeline(session, business.id, "sales", default=business.business_model != "media")
    if business.business_model == "media":
        await ensure_pipeline(session, business.id, "advertisers", default=True)
    await ensure_pipeline(session, business.id, "partners")

    existing = set((await session.execute(select(Sequence.key).where(Sequence.business_id == business.id))).scalars())
    for spec in DEFAULT_SEQUENCES:
        if spec["key"] in existing:
            continue
        session.add(
            Sequence(
                business_id=business.id,
                key=spec["key"],
                name=spec["name"],
                channel="email",
                trigger=spec["trigger"],
                steps=spec["steps"],
                stop_on=spec["stop_on"],
                active=True,
            )
        )

    has_themes = await session.scalar(select(ContentTheme.id).where(ContentTheme.business_id == business.id).limit(1))
    if has_themes is None:
        for name, pillar, description in THEMES_BY_MODEL.get(business.business_model, THEMES_BY_MODEL["default"]):
            session.add(ContentTheme(business_id=business.id, name=name, pillar=pillar, description=description))
    await session.flush()
