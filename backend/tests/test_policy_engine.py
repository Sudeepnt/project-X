"""The policy engine is the crown jewel: these tests pin down its guarantees."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from projectx.spine.policy.actions import ActionType
from projectx.spine.policy.engine import MoneyContext, PolicyInput, Violation, evaluate
from projectx.spine.policy.levels import Level

POST = ActionType(
    key="t.post",
    module="t",
    title="Publish",
    description="",
    default_level=Level.ASK_FIRST,
    max_level=Level.RUN,
    reversible=True,
    public=True,
    undo="Delete the post",
)
PAY = ActionType(
    key="t.pay",
    module="t",
    title="Pay vendor",
    description="",
    default_level=Level.ASK_FIRST,
    max_level=Level.ASK_FIRST,
    reversible=False,
    moves_money=True,
)
SORT = ActionType(
    key="t.sort",
    module="t",
    title="Sort",
    description="",
    default_level=Level.RUN,
    max_level=Level.RUN,
    reversible=True,
    undo="Re-sort",
)
FORBIDDEN = ActionType(
    key="t.file",
    module="t",
    title="File a tax return",
    description="",
    default_level=Level.NEVER,
    max_level=Level.NEVER,
    reversible=False,
)


def test_unknown_action_is_blocked() -> None:
    assert evaluate(PolicyInput(action=None, earned_level=Level.RUN)).level == Level.NEVER


def test_one_way_door_never_runs_alone_whatever_the_record() -> None:
    verdict = evaluate(PolicyInput(action=PAY, earned_level=Level.RUN, approval_mode="auto"))
    assert verdict.level == Level.ASK_FIRST
    assert "ceiling" in [r.code for r in verdict.reasons]


def test_forbidden_actions_stay_forbidden() -> None:
    assert evaluate(PolicyInput(action=FORBIDDEN, earned_level=Level.RUN, approval_mode="auto")).blocked


def test_approval_modes_apply_to_public_actions_only() -> None:
    assert (
        evaluate(PolicyInput(action=POST, earned_level=Level.RUN, approval_mode="approve_all")).level == Level.ASK_FIRST
    )
    assert evaluate(PolicyInput(action=SORT, earned_level=Level.RUN, approval_mode="approve_all")).level == Level.RUN
    assert (
        evaluate(PolicyInput(action=POST, earned_level=Level.RUN, approval_mode="approve_new", new_kind=True)).level
        == Level.ASK_FIRST
    )
    assert evaluate(PolicyInput(action=POST, earned_level=Level.RUN, approval_mode="approve_new")).level == Level.RUN
    assert (
        evaluate(PolicyInput(action=POST, earned_level=Level.RUN_AND_TELL, approval_mode="auto")).level
        == Level.RUN_AND_TELL
    )


def test_money_limits_force_approval() -> None:
    within = MoneyContext(
        amount_minor=1_000, per_action_limit_minor=5_000, per_day_limit_minor=20_000, spent_today_minor=0
    )
    over_action = MoneyContext(
        amount_minor=6_000, per_action_limit_minor=5_000, per_day_limit_minor=20_000, spent_today_minor=0
    )
    over_day = MoneyContext(
        amount_minor=1_000, per_action_limit_minor=5_000, per_day_limit_minor=20_000, spent_today_minor=19_500
    )
    assert evaluate(PolicyInput(action=SORT, earned_level=Level.RUN, money=within)).level == Level.RUN
    assert evaluate(PolicyInput(action=SORT, earned_level=Level.RUN, money=over_action)).level == Level.ASK_FIRST
    verdict = evaluate(PolicyInput(action=SORT, earned_level=Level.RUN, money=over_day))
    assert verdict.level == Level.ASK_FIRST
    assert [r.code for r in verdict.reasons] == ["over_daily_limit"]


def test_violations_block_or_require_approval() -> None:
    hard = Violation("platform_not_allowed", "Instagram is not an allowed platform")
    soft = Violation("near_budget", "Close to the daily budget", hard=False)
    assert evaluate(PolicyInput(action=SORT, earned_level=Level.RUN, violations=(hard,))).blocked
    assert evaluate(PolicyInput(action=SORT, earned_level=Level.RUN, violations=(soft,))).needs_approval


def test_degraded_mode_drops_one_level_but_not_below_ask_first() -> None:
    assert evaluate(PolicyInput(action=SORT, earned_level=Level.RUN, degraded=True)).level == Level.RUN_AND_TELL
    assert evaluate(PolicyInput(action=SORT, earned_level=Level.ASK_FIRST, degraded=True)).level == Level.ASK_FIRST


def test_founder_cap_lowers_level() -> None:
    verdict = evaluate(PolicyInput(action=SORT, earned_level=Level.RUN, founder_cap=Level.DRAFT))
    assert verdict.level == Level.DRAFT
    assert verdict.draft_only


def test_action_type_rejects_unsafe_definitions() -> None:
    with pytest.raises(ValueError, match="one-way door"):
        ActionType(
            key="x",
            module="t",
            title="",
            description="",
            default_level=Level.ASK_FIRST,
            max_level=Level.RUN,
            reversible=False,
        )
    with pytest.raises(ValueError, match="undo"):
        ActionType(
            key="y",
            module="t",
            title="",
            description="",
            default_level=Level.ASK_FIRST,
            max_level=Level.RUN,
            reversible=True,
        )


levels = st.sampled_from(list(Level))
actions = st.sampled_from([POST, PAY, SORT, FORBIDDEN])
modes = st.sampled_from(["approve_all", "approve_new", "auto"])


@given(
    action=actions,
    earned=levels,
    mode=modes,
    new_kind=st.booleans(),
    degraded=st.booleans(),
    cap=st.none() | levels,
    amount=st.integers(0, 50_000),
    spent=st.integers(0, 50_000),
    hard=st.booleans(),
)
def test_properties(
    action: ActionType,
    earned: Level,
    mode: str,
    new_kind: bool,
    degraded: bool,
    cap: Level | None,
    amount: int,
    spent: int,
    hard: bool,
) -> None:
    money = MoneyContext(amount, 5_000, 20_000, spent)
    base = PolicyInput(
        action=action,
        earned_level=earned,
        approval_mode=mode,  # type: ignore[arg-type]
        founder_cap=cap,
        money=money,
        new_kind=new_kind,
        degraded=degraded,
    )
    verdict = evaluate(base)
    # Never above what was earned, the action's ceiling, or the founder's cap.
    assert verdict.level <= earned
    assert verdict.level <= action.max_level
    if cap is not None:
        assert verdict.level <= cap
    # One-way doors never run without a person.
    if action.one_way_door:
        assert verdict.level <= Level.ASK_FIRST
    # Adding a violation can only lower the level (monotonicity).
    worse = evaluate(replace(base, violations=(Violation("x", "x", hard=hard),)))
    assert worse.level <= verdict.level
    if hard:
        assert worse.blocked
