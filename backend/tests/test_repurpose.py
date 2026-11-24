"""Repurposing: one long piece becomes several distinct posts that go through the normal pipeline."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from projectx.modules.gtm.models import ContentItem
from projectx.services import Services
from projectx.spine.jobs.worker import Worker

ARTICLE = "\n\n".join(
    f"Section {i}. Small businesses that answer customer questions within an hour keep more of their buyers. "
    f"In our own data from last quarter, point {i} mattered most for repeat orders and referrals. "
    "The fix is simple: a shared inbox, saved replies, and one person responsible each day."
    for i in range(1, 7)
)


async def test_one_article_becomes_several_reviewed_posts(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    gtm = f"/api/v1/b/{business['id']}/gtm"
    await founder.post(f"{gtm}/channels/practice", json={"platform": "linkedin"})
    too_short = await founder.post(f"{gtm}/content/repurpose", json={"text": "Too short.", "count": 3})
    assert too_short.status_code == 422
    res = await founder.post(
        f"{gtm}/content/repurpose",
        json={
            "title": "Answer fast",
            "text": ARTICLE,
            "count": 4,
            "platforms": ["linkedin"],
            "source_url": "https://example.com/answer-fast",
        },
    )
    assert res.status_code == 202, res.text
    again = await founder.post(f"{gtm}/content/repurpose", json={"title": "Answer fast", "text": ARTICLE, "count": 4})
    assert again.json()["job_id"] is None, "the same piece isn't split twice"
    for _ in range(3):
        await Worker(services).run_until_idle()
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        items = (await s.execute(select(ContentItem).where(ContentItem.source_type == "repurposed"))).scalars().all()
    assert len(items) == 4
    assert len({i.title for i in items}) == 4, "every post takes a different angle"
    assert len({i.planned_for for i in items}) == 4, "spread out, not posted at once"
    assert all(i.source_ref["url"] == "https://example.com/answer-fast" for i in items)
    assert all(i.status in ("needs_review", "approved", "scheduled", "blocked") for i in items), [
        i.status for i in items
    ]
