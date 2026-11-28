"""The policy engine: a pure function from (action, context) to an autonomy level.

No model, no prompt and no network call is involved, so nothing an agent reads or writes can
talk its way past it. Each constraint can only *lower* the level (take the minimum), which
makes the result independent of evaluation order and easy to reason about:

    level = min(earned, ceiling, founder cap, approval mode, money limits, restrictions, …)

Every constraint that binds is returned as a reason, so the founder sees exactly why a card
is waiting for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from projectx.spine.policy.actions import ActionType
from projectx.spine.policy.levels import Level

ApprovalMode = Literal["approve_all", "approve_new", "auto"]


@dataclass(frozen=True, slots=True)
class Violation:
    """A rule the proposed action breaks. Hard violations block; soft ones require approval."""

    code: str
    message: str
    hard: bool = True


@dataclass(frozen=True, slots=True)
class MoneyContext:
    amount_minor: int
    per_action_limit_minor: int
    per_day_limit_minor: int
    spent_today_minor: int


@dataclass(frozen=True, slots=True)
class PolicyInput:
    action: ActionType | None
    earned_level: Level
    approval_mode: ApprovalMode = "approve_all"
    founder_cap: Level | None = None
    money: MoneyContext | None = None
    new_kind: bool = False
    degraded: bool = False
    violations: tuple[Violation, ...] = ()


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    message: str
    cap: Level


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    level: Level
    reasons: tuple[Reason, ...] = field(default_factory=tuple)

    @property
    def runs_now(self) -> bool:
        return self.level >= Level.RUN_AND_TELL

    @property
    def needs_approval(self) -> bool:
        return self.level == Level.ASK_FIRST

    @property
    def draft_only(self) -> bool:
        return self.level == Level.DRAFT

    @property
    def blocked(self) -> bool:
        return self.level == Level.NEVER

    def explain(self) -> str:
        if not self.reasons:
            return f"{self.level.label}: earned through a clean record."
        return f"{self.level.label}: " + "; ".join(r.message for r in self.reasons)


def evaluate(inp: PolicyInput) -> PolicyVerdict:
    if inp.action is None:
        return PolicyVerdict(Level.NEVER, (Reason("unknown_action", "This action is not registered.", Level.NEVER),))

    action = inp.action
    caps: list[Reason] = [
        Reason("ceiling", _ceiling_message(action), action.max_level),
    ]
    if inp.founder_cap is not None:
        caps.append(
            Reason("founder_rule", f"Your rule caps this action at '{inp.founder_cap.label}'.", inp.founder_cap)
        )
    if inp.degraded:
        # Conditions changed: lose one level of independence, but never below Ask first.
        lowered = Level(max(int(inp.earned_level) - 1, int(Level.ASK_FIRST)))
        caps.append(
            Reason("conditions_changed", "Conditions changed recently, so the system is being careful.", lowered)
        )
    if action.public:
        if inp.approval_mode == "approve_all":
            caps.append(Reason("approval_mode", "You approve every public action.", Level.ASK_FIRST))
        elif inp.approval_mode == "approve_new" and inp.new_kind:
            caps.append(Reason("new_kind", "This is a new kind of public content.", Level.ASK_FIRST))
    if inp.money is not None and inp.money.amount_minor > 0:
        m = inp.money
        if m.amount_minor > m.per_action_limit_minor:
            caps.append(
                Reason("over_action_limit", "The amount is above your per-action money limit.", Level.ASK_FIRST)
            )
        if m.spent_today_minor + m.amount_minor > m.per_day_limit_minor:
            caps.append(Reason("over_daily_limit", "This would pass your daily money limit.", Level.ASK_FIRST))
    for v in inp.violations:
        caps.append(Reason(v.code, v.message, Level.NEVER if v.hard else Level.ASK_FIRST))

    level = inp.earned_level
    for reason in caps:
        level = min(level, reason.cap)
    # A constraint explains the card when it lowered the level. Broken rules are always shown, even when the
    # action would have waited anyway, so the person deciding sees why it needs care.
    violation_codes = {v.code for v in inp.violations}
    binding = tuple(r for r in caps if r.cap < inp.earned_level or r.code in violation_codes)
    return PolicyVerdict(Level(level), binding)


def _ceiling_message(action: ActionType) -> str:
    if action.max_level == Level.NEVER:
        return "Agents may never do this."
    if action.one_way_door:
        return "This cannot be undone, so a person always decides."
    return f"This action never goes above '{action.max_level.label}'."
