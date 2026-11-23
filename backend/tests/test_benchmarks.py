"""Benchmarks: opt-in only, anonymous contributors, shown only with enough businesses, withdrawable."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, text

from projectx.kernel.time import utcnow
from projectx.modules.gtm.analytics.benchmarks import MIN_CONTRIBUTORS, benchmarks_for, contributor_id
from projectx.modules.gtm.models import BenchmarkContribution, ContentVariant, Post
from projectx.services import Services
from projectx.spine.jobs.worker import Worker


def test_contributor_ids_are_salted_and_stable() -> None:
    b = uuid.uuid4()
    assert contributor_id("secret", b) == contributor_id("secret", b)
    assert contributor_id("secret", b) != contributor_id("other-secret", b)
    assert str(b) not in contributor_id("secret", b) and b.hex not in contributor_id("secret", b)


async def test_figures_need_enough_businesses(services: Services) -> None:
    week = date.today() - timedelta(days=date.today().weekday())
    async with services.db.session() as s:
        await s.execute(text("DELETE FROM benchmark_contributions WHERE business_model = 'creator'"))
        for i in range(MIN_CONTRIBUTORS - 1):
            s.add(
                BenchmarkContribution(
                    contributor=f"c{i}",
                    week=week,
                    business_model="creator",
                    metric="engagement_rate",
                    platform="instagram",
                    value=0.02 + i / 100,
                )
            )
        await s.flush()
        assert await benchmarks_for(s, "creator", week - timedelta(weeks=1)) == {}, "below the minimum: hidden"
        s.add(
            BenchmarkContribution(
                contributor="c-last",
                week=week,
                business_model="creator",
                metric="engagement_rate",
                platform="instagram",
                value=0.10,
            )
        )
        await s.flush()
        stats = (await benchmarks_for(s, "creator", week - timedelta(weeks=1)))[("engagement_rate", "instagram")]
        assert stats["businesses"] == MIN_CONTRIBUTORS
        assert stats["p25"] <= stats["median"] <= stats["p75"]
        await s.execute(text("DELETE FROM benchmark_contributions WHERE business_model = 'creator'"))


async def test_opt_in_contribute_and_withdraw(services: Services, founder: Any, business: dict[str, Any]) -> None:
    base = f"/api/v1/b/{business['id']}/gtm/analytics/benchmarks"
    assert (await founder.get(base)).json() == {"sharing": False, "min_businesses": MIN_CONTRIBUTORS, "items": []}
    gtm = f"/api/v1/b/{business['id']}/gtm"
    await founder.post(f"{gtm}/channels/practice", json={"platform": "instagram"})
    bid = uuid.UUID(business["id"])
    await founder.post(f"{gtm}/content", json={"title": "Numbers", "platforms": ["instagram"]})
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:  # one published post with numbers
        variant = await s.scalar(select(ContentVariant).where(ContentVariant.business_id == bid))
        account_id = await s.scalar(text("SELECT id FROM gtm_channel_accounts WHERE business_id = :b"), {"b": bid})
        assert variant is not None
        s.add(
            Post(
                business_id=bid,
                variant_id=variant.id,
                content_id=variant.content_id,
                account_id=account_id,
                platform="instagram",
                status="published",
                published_at=utcnow() - timedelta(days=1),
                metrics={"reach": 1000, "engagements": 50, "clicks": 5},
                idempotency_key=uuid.uuid4().hex,
            )
        )
    on = await founder.put(base, json={"share": True})
    assert on.status_code == 200, on.text
    await Worker(services).run_until_idle()
    who = contributor_id(services.settings.secret_key.get_secret_value(), bid)
    async with services.db.session() as s:
        mine = (
            (await s.execute(select(BenchmarkContribution).where(BenchmarkContribution.contributor == who)))
            .scalars()
            .all()
        )
        columns = {
            c["name"]
            for c in (
                await s.execute(
                    text(
                        "SELECT column_name AS name FROM information_schema.columns "
                        "WHERE table_name = 'benchmark_contributions'"
                    )
                )
            ).mappings()
        }
    assert {m.metric for m in mine} >= {"engagement_rate", "reach_per_post"}
    assert "business_id" not in columns, "contributions can't be traced back to a business"
    assert (await founder.get(base)).json()["sharing"] is True

    off = (await founder.put(base, json={"share": False})).json()
    assert off["removed"] == len(mine)
    async with services.db.session() as s:
        left = await s.scalar(
            select(func.count()).select_from(BenchmarkContribution).where(BenchmarkContribution.contributor == who)
        )
    assert left == 0, "withdrawing deletes everything this business shared"
