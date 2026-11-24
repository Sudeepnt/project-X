"""A/B lab: version B joins the same approval, publishes two days later, and a winner needs strong evidence."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text, update

from projectx.modules.gtm.learning.experiments import evaluate
from projectx.modules.gtm.models import ContentItem, Experiment, Post
from projectx.services import Services
from projectx.spine.jobs.worker import Worker


async def _run(services: Services) -> None:
    for _ in range(5):
        async with services.db.session() as s:
            await s.execute(text("UPDATE jobs SET run_at = now() WHERE status = 'queued' AND run_at > now()"))
        if await Worker(services).run_until_idle() == 0:
            return


async def test_hook_test_end_to_end(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    gtm = f"/api/v1/b/{business['id']}/gtm"
    await founder.post(f"{gtm}/channels/practice", json={"platform": "instagram"})
    async with services.db.tenant(bid) as s:  # no random failures in this test
        await s.execute(
            text(
                "UPDATE businesses SET settings = jsonb_set(coalesce(settings, '{}'), "
                "'{gtm,practice_failure_rate}', '0') WHERE id = :b"
            ),
            {"b": bid},
        )
    content = (
        await founder.post(f"{gtm}/content", json={"title": "Morning habits", "platforms": ["instagram"]})
    ).json()
    await _run(services)
    async with services.db.tenant(bid) as s:
        item = await s.get(ContentItem, uuid.UUID(content["id"]))
        assert item is not None and item.status == "needs_review", item.status if item else None
        decision_id = item.decision_id

    same = await founder.post(
        f"{gtm}/experiments", json={"content_id": content["id"], "platform": "instagram", "text_b": ""}
    )
    assert same.status_code == 422
    res = await founder.post(
        f"{gtm}/experiments",
        json={
            "content_id": content["id"],
            "platform": "instagram",
            "text_b": "Three habits that changed my mornings. #MorningLedger",
            "hypothesis": "A numbered opening gets more engagement",
        },
    )
    assert res.status_code == 201, res.text
    exp_id = uuid.UUID(res.json()["id"])
    dup = await founder.post(
        f"{gtm}/experiments", json={"content_id": content["id"], "platform": "instagram", "text_b": "Other"}
    )
    assert dup.status_code == 409

    card = (await founder.get(f"/api/v1/b/{business['id']}/decisions/{decision_id}")).json()
    assert len(card["payload"]["variant_ids"]) == 2 and any(v.get("ab") == "B" for v in card["preview"]["variants"])
    assert (
        await founder.post(f"/api/v1/b/{business['id']}/decisions/{decision_id}/approve", json={})
    ).status_code == 200
    await _run(services)

    async with services.db.tenant(bid) as s:
        posts = (
            (
                await s.execute(
                    select(Post).where(Post.variant_id.in_([uuid.UUID(v) for v in card["payload"]["variant_ids"]]))
                )
            )
            .scalars()
            .all()
        )
        assert len(posts) == 2 and all(p.status == "published" for p in posts), [
            (p.status, p.last_error) for p in posts
        ]
        experiment = await s.get(Experiment, exp_id)
        assert experiment is not None
        variant_of = {v["key"]: v for v in (await founder.get(f"{gtm}/experiments")).json()["items"][0]["variants"]}
        assert set(variant_of) == {"A", "B"}
        # Too little reach: no verdict yet.
        await s.execute(
            update(Post)
            .where(Post.content_id == uuid.UUID(content["id"]))
            .values(metrics={"reach": 100, "engagements": 5})
        )
        await evaluate(s, experiment)
        assert experiment.status == "running"
        # Plenty of reach and a clear gap: B wins.
        a_id = uuid.UUID(card["payload"]["variant_ids"][0])
        for p in posts:
            p.metrics = {"reach": 2000, "engagements": 40 if p.variant_id == a_id else 120}
        await s.flush()
        await evaluate(s, experiment)
        assert experiment.status == "concluded" and experiment.winner_key == "B"
        assert experiment.result["indicative"] is True
        siblings = (await s.execute(select(ContentItem).where(ContentItem.source_type == "ab_test"))).scalars().all()
        assert siblings and all(i.status in ("scheduled", "published") for i in siblings), (
            "version B's item doesn't stay stuck in review"
        )
