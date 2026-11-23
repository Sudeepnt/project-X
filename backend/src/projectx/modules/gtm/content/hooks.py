"""The hook library: the first line (or first two seconds) decides whether anyone keeps reading.
Hooks are A/B tested continuously: the planner picks hook types with Thompson sampling over each
business's own results (see learning)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hook:
    key: str
    label: str
    pattern: str
    example: str


HOOKS: dict[str, Hook] = {
    h.key: h
    for h in (
        Hook(
            "curiosity_gap",
            "Curiosity gap",
            "Tease the answer without giving it away.",
            "Most people get this wrong about {topic}. Here's what actually happens.",
        ),
        Hook(
            "contrarian",
            "Contrarian take",
            "Disagree with the common belief, then prove it.",
            "Unpopular opinion: {topic} isn't the problem. This is.",
        ),
        Hook(
            "numbered_list",
            "Numbered list",
            "Promise a specific number of useful things.",
            "3 things about {topic} nobody tells you:",
        ),
        Hook(
            "mistake",
            "The mistake everyone makes",
            "Name a costly mistake and how to avoid it.",
            "The mistake everyone makes with {topic} (and the 10-second fix).",
        ),
        Hook(
            "mini_story",
            "Mini-story",
            "Open with a moment, a person and a turn.",
            "Last week a reader asked us one question about {topic}. The answer surprised us.",
        ),
        Hook(
            "question",
            "Direct question",
            "Ask the question the audience is already asking.",
            "Is {topic} actually worth it? We checked.",
        ),
        Hook(
            "stat_shock",
            "Surprising number",
            "Lead with a number that reframes the topic.",
            "One number explains {topic} better than any headline.",
        ),
        Hook(
            "how_to",
            "How-to",
            "Promise a concrete outcome in a short time.",
            "How to make sense of {topic} in under a minute.",
        ),
        Hook(
            "before_after",
            "Before and after",
            "Show the change, then how it happened.",
            "Before: confused about {topic}. After: this one idea.",
        ),
        Hook(
            "myth_bust",
            "Myth vs fact",
            "State a myth, then the fact.",
            "Myth: {topic} is simple. Fact: it's simple once you see this.",
        ),
    )
}

DEFAULT_HOOKS = ("curiosity_gap", "numbered_list", "mistake", "mini_story", "contrarian", "question")
