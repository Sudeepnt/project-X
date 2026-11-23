"""Ledger event types owned by the spine."""

from __future__ import annotations

from projectx.spine.ledger.events import EventData, register_event


class BusinessCreated(EventData):
    plan: str
    business_model: str


class MemberChanged(EventData):
    user_id: str
    role: str | None = None


class ActionProposed(EventData):
    action_type: str
    level: str
    decision_id: str | None = None
    reasons: list[str] = []


class ActionBlocked(EventData):
    action_type: str
    reasons: list[str]


class ActionExecuted(EventData):
    action_type: str
    decision_id: str
    amount_minor: int | None = None
    currency: str | None = None
    external_ref: str | None = None


class ActionFailed(EventData):
    action_type: str
    decision_id: str
    error: str


class DecisionMade(EventData):
    decision_id: str
    action_type: str
    kind: str
    reason_code: str | None = None
    edited: bool = False
    option: str | None = None


class AutonomyChanged(EventData):
    action_type: str
    from_level: str
    to_level: str
    reason: str


class PolicyUpdated(EventData):
    fields: list[str]


class ConditionsChanged(EventData):
    module: str
    signals: list[str]
    active: bool


class CredentialChanged(EventData):
    secret_id: str
    kind: str
    change: str


register_event("spine.business.created", "A business was created", BusinessCreated)
register_event("spine.member.added", "A person joined the business", MemberChanged)
register_event("spine.member.removed", "A person left the business", MemberChanged)
register_event("spine.action.proposed", "An agent proposed an action and the policy engine sorted it", ActionProposed)
register_event("spine.action.blocked", "The policy engine blocked an action", ActionBlocked)
register_event("spine.action.executed", "An action ran", ActionExecuted)
register_event("spine.action.failed", "An action failed to run", ActionFailed)
register_event("spine.action.undone", "An action was undone", ActionExecuted)
register_event("spine.decision.approved", "The founder approved a decision", DecisionMade)
register_event("spine.decision.rejected", "The founder rejected a decision", DecisionMade)
register_event("spine.decision.completed", "A person completed a manual task", DecisionMade)
register_event("spine.decision.expired", "A decision expired unanswered", DecisionMade)
register_event("spine.autonomy.changed", "An action type moved up or down a level", AutonomyChanged)
register_event("spine.policy.updated", "The founder changed policy settings", PolicyUpdated)
register_event("spine.conditions.changed", "The change detector raised or cleared an alert", ConditionsChanged)
register_event("spine.credential.changed", "A credential was stored, rotated or removed", CredentialChanged)
