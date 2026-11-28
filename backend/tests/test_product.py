"""Product across four departments: customer asks (Operations) → roadmap item ranked (Product) → spec on the
hard-jobs model → release → customers told (Operations) and an announcement drafted (GTM)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from projectx.modules.product import service as product
from projectx.services import Services
from projectx.spine.decisions.models import Decision
from projectx.spine.gateway.models import ModelCall
from projectx.spine.jobs.worker import Worker


def url(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}{path}"


async def test_customer_requests_become_a_shipped_feature_everyone_hears_about(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    await founder.post(url(business, "/gtm/channels/practice"), json={"platform": "linkedin"})
    contacts = []
    for i in range(2):
        contacts.append(
            (
                await founder.post(
                    url(business, "/gtm/contacts"),
                    json={"email": f"fan{i}-{uuid.uuid4().hex[:4]}@example.com", "stage": "customer"},
                )
            ).json()
        )
    for c, body in zip(
        contacts,
        ("Would love a weekly email report of my ads", "Could you add a weekly email report feature?"),
        strict=True,
    ):
        await founder.post(url(business, "/ops/tickets"), json={"subject": "Idea", "body": body, "contact_id": c["id"]})
    await Worker(services).run_until_idle()

    items = (await founder.get(url(business, "/product/items"))).json()["items"]
    assert len(items) == 1, items  # both asks are the same feature
    item = items[0]
    assert item["requesters"] == 2 and item["kind"] == "feature" and item["score"] > 0

    assert (await founder.post(url(business, f"/product/items/{item['id']}/spec"))).status_code == 202
    await Worker(services).run_until_idle()
    item = next(
        i for i in (await founder.get(url(business, "/product/items"))).json()["items"] if i["id"] == item["id"]
    )
    assert item["spec_status"] == "ready" and "Acceptance criteria" in item["spec"]
    async with services.db.tenant(bid) as s:
        call = (await s.execute(select(ModelCall).where(ModelCall.task == "product.spec"))).scalars().first()
    assert call is not None and call.model == "draft-hard"  # the hard-jobs rung, never the everyday models

    res = await founder.post(
        url(business, "/product/releases"),
        json={"version": "2.4", "notes": "Weekly email reports are here.", "item_ids": [item["id"]]},
    )
    assert res.status_code == 201, res.text
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        told = (
            (
                await s.execute(
                    select(Decision).where(
                        Decision.action_type == "ops.reply_ticket", Decision.title.like("Tell them it shipped%")
                    )
                )
            )
            .scalars()
            .all()
        )
        from projectx.modules.gtm.models import ContentItem

        drafts = (await s.execute(select(ContentItem).where(ContentItem.source_type == "release"))).scalars().all()
    assert len(told) == 2
    assert [d.title for d in drafts] == ["What's new: 2.4"]


def test_score_is_plain_arithmetic() -> None:
    assert product.score(2, 0, 1) == 20.0
    assert product.score(2, 50_000, 5) == 14.0  # (20 + 50) / 5
