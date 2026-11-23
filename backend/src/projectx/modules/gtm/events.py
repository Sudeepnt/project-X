"""GTM ledger events. Ids only, never personal data (the ledger is permanent)."""

from __future__ import annotations

from projectx.spine.ledger.events import EventData, register_event


class Ref(EventData):
    id: str


class BrandBookChanged(EventData):
    brand_book_id: str
    version: int


class GoalChanged(EventData):
    goal_id: str
    type: str
    status: str
    target: float | None = None
    reason: str | None = None


class GoalPace(EventData):
    goal_id: str
    actual: float
    expected_to_date: float
    projected: float
    status: str


class CampaignChanged(EventData):
    campaign_id: str
    playbook: str
    status: str
    reason: str | None = None


class ContentChanged(EventData):
    content_id: str
    status: str
    campaign_id: str | None = None
    intent: str | None = None


class PostChanged(EventData):
    post_id: str
    platform: str
    status: str
    external_ref: str | None = None
    campaign_id: str | None = None


class ContactCreated(EventData):
    contact_id: str
    source: str
    stage: str
    campaign_id: str | None = None


class StageChanged(EventData):
    contact_id: str
    from_stage: str | None = None
    to_stage: str
    from_status: str | None = None
    to_status: str
    side_state: str | None = None
    cause_type: str
    cause_id: str | None = None


class DealChanged(EventData):
    deal_id: str
    pipeline: str
    stage: str
    status: str
    value_minor: int
    currency: str


class PaymentRecorded(EventData):
    payment_id: str
    contact_id: str | None = None
    amount_minor: int
    currency: str
    kind: str
    campaign_id: str | None = None


class MessageSent(EventData):
    message_id: str
    channel: str
    kind: str
    sequence_id: str | None = None
    issue_id: str | None = None


class NewsletterSent(EventData):
    issue_id: str
    recipients: int


class RuleFired(EventData):
    rule: str
    scope: str
    scope_id: str | None = None
    detail: str


class AccountChanged(EventData):
    account_id: str
    platform: str
    status: str


class ConsentChanged(EventData):
    contact_id: str
    channel: str
    purpose: str
    status: str
    source: str


class ExperimentChanged(EventData):
    experiment_id: str
    status: str
    winner: str | None = None


class AdChanged(EventData):
    ad_campaign_id: str
    platform: str
    status: str
    daily_budget_minor: int | None = None
    reason: str | None = None


class DataRequestDone(EventData):
    request_id: str
    kind: str
    status: str


for key, description, schema in (
    ("gtm.brand_book.drafted", "A brand book draft was prepared", BrandBookChanged),
    ("gtm.brand_book.approved", "The owner approved a brand book version", BrandBookChanged),
    ("gtm.goal.created", "A goal was set", GoalChanged),
    ("gtm.goal.changed", "A goal changed status or target", GoalChanged),
    ("gtm.goal.paced", "The daily pace check ran", GoalPace),
    ("gtm.campaign.changed", "A campaign started, paused, stopped or completed", CampaignChanged),
    ("gtm.content.changed", "A content item changed status", ContentChanged),
    ("gtm.post.changed", "A post was queued, published, verified, failed or deleted", PostChanged),
    ("gtm.contact.created", "A new contact entered the funnel", ContactCreated),
    ("gtm.contact.stage_changed", "A contact moved stage or status", StageChanged),
    ("gtm.contact.erased", "A contact's personal data was erased", Ref),
    ("gtm.deal.changed", "A deal was created or moved", DealChanged),
    ("gtm.payment.recorded", "A payment was received", PaymentRecorded),
    ("gtm.message.sent", "A message was sent to a contact", MessageSent),
    ("gtm.newsletter.sent", "A newsletter issue went out", NewsletterSent),
    ("gtm.rule.fired", "A stop rule, guardrail, restriction or safety trigger fired", RuleFired),
    ("gtm.account.changed", "A channel account was connected, refreshed or disconnected", AccountChanged),
    ("gtm.consent.changed", "Consent was granted or revoked", ConsentChanged),
    ("gtm.experiment.changed", "An experiment started or concluded", ExperimentChanged),
    ("gtm.ad.changed", "An ad campaign was created, paused or changed", AdChanged),
    ("gtm.data_request.done", "A data export or erasure finished", DataRequestDone),
    ("gtm.report.generated", "The weekly report was generated", Ref),
):
    register_event(key, description, schema)
