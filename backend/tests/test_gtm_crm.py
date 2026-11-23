"""GTM CRM: funnel stages with causes, scoring, automations, nurture, deals and payments."""

from __future__ import annotations

import io
import uuid
from typing import Any

from sqlalchemy import select

from projectx.modules.gtm.models import Message, Sequence, SequenceEnrollment
from projectx.modules.gtm.nurture.sequences import parse_unsubscribe_token, unsubscribe_token
from projectx.services import Services
from projectx.spine.jobs.worker import Worker
from projectx.spine.twin.models import Contact, ContactStageChange, Pipeline


def gtm(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}/gtm{path}"


async def test_new_business_gets_defaults(services: Services, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        keys = set((await s.execute(select(Pipeline.key).where(Pipeline.business_id == bid))).scalars())
        sequences = set((await s.execute(select(Sequence.key).where(Sequence.business_id == bid))).scalars())
    assert {"sales", "advertisers", "partners"} <= keys  # media business gets the advertiser pipeline
    assert {"welcome", "nurture_to_convert", "onboarding", "win_back", "referral_request"} <= sequences


async def test_contact_scoring_reaches_mql_and_starts_nurture(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    res = await founder.post(
        gtm(business, "/contacts"),
        json={
            "email": f"lead-{uuid.uuid4().hex[:6]}@example.com",
            "name": "Lena Lead",
            "stage": "lead",
            "consent": {"channel": "email", "purpose": "marketing", "evidence": "Ticked the box on the webinar form"},
        },
    )
    assert res.status_code == 201, res.text
    contact_id = uuid.UUID(res.json()["id"])
    bid = uuid.UUID(business["id"])
    from projectx.modules.gtm.crm.lifecycle import record_touch
    from projectx.spine.tenancy.models import Business

    async with services.db.tenant(bid) as s:
        biz = await s.get(Business, bid)
        contact = await s.get(Contact, contact_id)
        assert biz and contact
        for _ in range(3):
            await record_touch(s, biz, contact, kind="form_submit")
        assert contact.stage == "mql"
        assert contact.lead_score >= 50
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        enrollment = await s.scalar(select(SequenceEnrollment).where(SequenceEnrollment.contact_id == contact_id))
        causes = (
            (await s.execute(select(ContactStageChange.cause_type).where(ContactStageChange.contact_id == contact_id)))
            .scalars()
            .all()
        )
    assert enrollment is not None
    assert "automation" in causes  # the MQL move records that the score caused it


async def test_sequence_step_is_sent_with_one_click_unsubscribe(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    email = f"sub-{uuid.uuid4().hex[:6]}@example.com"
    res = await founder.post(
        gtm(business, "/contacts"),
        json={
            "email": email,
            "stage": "subscriber",
            "consent": {"channel": "email", "purpose": "newsletter", "evidence": "Signed up on the landing page"},
        },
    )
    assert res.status_code == 201
    worker = Worker(services)
    await worker.run_until_idle()  # automations enroll in "welcome" (first step has no delay)
    bid = uuid.UUID(business["id"])
    async with services.db.session() as s:
        from projectx.spine.jobs.queue import enqueue

        await enqueue(s, "gtm.process_sequences", business_id=bid)
    await worker.run_until_idle()
    sent = [m for m in services.sent_emails if m["to"] == email]  # type: ignore[attr-defined]
    assert sent, "welcome email should go out"
    assert sent[0]["subject"].startswith("Welcome to")
    assert sent[0]["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    async with services.db.tenant(bid) as s:
        statuses = (await s.execute(select(Message.status).where(Message.business_id == bid))).scalars().all()
    assert "sent" in statuses


async def test_no_consent_means_no_nurture(services: Services, founder: Any, business: dict[str, Any]) -> None:
    res = await founder.post(
        gtm(business, "/contacts"), json={"email": f"x-{uuid.uuid4().hex[:6]}@example.com", "stage": "subscriber"}
    )
    contact_id = uuid.UUID(res.json()["id"])
    await Worker(services).run_until_idle()
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        assert await s.scalar(select(SequenceEnrollment).where(SequenceEnrollment.contact_id == contact_id)) is None


async def test_advertiser_deal_moves_contact_through_the_funnel(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    pipelines = (await founder.get(gtm(business, "/pipelines"))).json()["items"]
    advertisers = next(p for p in pipelines if p["key"] == "advertisers")
    contact = (
        await founder.post(
            gtm(business, "/contacts"),
            json={"email": f"brand-{uuid.uuid4().hex[:6]}@example.com", "name": "Acme Brand", "stage": "lead"},
        )
    ).json()
    deal = (
        await founder.post(
            gtm(business, "/deals"),
            json={
                "pipeline_id": advertisers["id"],
                "title": "Acme sponsorship, March",
                "contact_id": contact["id"],
                "value_minor": 150_000,
                "deal_type": "sponsorship",
            },
        )
    ).json()
    assert deal["stage_key"] == "prospect"
    moved = await founder.patch(gtm(business, f"/deals/{deal['id']}"), json={"stage_key": "proposal"})
    assert moved.status_code == 200
    c = (await founder.get(gtm(business, f"/contacts/{contact['id']}"))).json()
    assert (c["stage"], c["status"]) == ("opportunity", "proposal_sent")
    lost = await founder.patch(gtm(business, f"/deals/{deal['id']}"), json={"stage_key": "lost"})
    assert lost.status_code == 422  # a loss without a reason teaches nothing
    won = await founder.patch(gtm(business, f"/deals/{deal['id']}"), json={"stage_key": "advertiser"})
    assert won.json()["status"] == "won"
    c = (await founder.get(gtm(business, f"/contacts/{contact['id']}"))).json()
    assert c["stage"] == "customer"
    assert any(e["type"] == "stage" and e["cause_type"] == "deal" for e in c["timeline"])


async def test_payments_are_idempotent_and_attributed(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    email = f"buyer-{uuid.uuid4().hex[:6]}@example.com"
    from projectx.modules.gtm.crm.payments import record_payment
    from projectx.spine.tenancy.models import Business

    async with services.db.tenant(bid) as s:
        biz = await s.get(Business, bid)
        assert biz
        p1, created1 = await record_payment(
            s, biz, provider="stripe", external_id="pi_1", amount_minor=4900, currency="INR", email=email
        )
        p2, created2 = await record_payment(
            s, biz, provider="stripe", external_id="pi_1", amount_minor=4900, currency="INR", email=email
        )
        contact = await s.get(Contact, p1.contact_id)
    assert created1 and not created2 and p1.id == p2.id
    assert contact is not None and contact.stage == "customer" and contact.lifetime_value_minor == 4900


async def test_funnel_and_import(founder: Any, business: dict[str, Any]) -> None:
    csv_data = "email,name,company\na1@example.com,A One,Co\nnot-an-email,B,Co\na2@example.com,A Two,\n"
    res = await founder.client.post(
        gtm(business, "/contacts/import"),
        headers={"X-CSRF-Token": founder.csrf},
        files={"file": ("list.csv", io.BytesIO(csv_data.encode()), "text/csv")},
        data={"consent_confirmed": "true", "stage": "subscriber"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"created": 2, "updated": 0, "skipped": 1, "consent_recorded": True}
    funnel = (await founder.get(gtm(business, "/funnel"))).json()
    subscriber = next(s for s in funnel["stages"] if s["stage"] == "subscriber")
    assert subscriber["count"] >= 2
    listed = (await founder.get(gtm(business, "/contacts?stage=subscriber&q=A%20One"))).json()
    assert listed["total"] == 1


def test_unsubscribe_tokens_are_signed() -> None:
    b, c = uuid.uuid4(), uuid.uuid4()
    token = unsubscribe_token("secret", b, c)
    assert parse_unsubscribe_token("secret", token) == (b, c)
    assert parse_unsubscribe_token("other-secret", token) is None
    assert parse_unsubscribe_token("secret", token[:-2] + "AA") is None
