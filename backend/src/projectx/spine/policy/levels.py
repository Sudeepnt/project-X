"""Autonomy levels, ordered from least to most independent."""

from __future__ import annotations

from enum import IntEnum


class Level(IntEnum):
    NEVER = 0  # blocked for agents, always
    DRAFT = 1  # the agent prepares it; a person performs it outside the platform
    ASK_FIRST = 2  # the agent prepares everything and runs it after one tap of approval
    RUN_AND_TELL = 3  # the agent runs it and tells the founder at once
    RUN = 4  # the agent runs it; it appears in the daily log

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def slug(self) -> str:
        return self.name.lower()

    @classmethod
    def from_slug(cls, slug: str) -> Level:
        try:
            return cls[slug.upper()]
        except KeyError:
            raise ValueError(f"unknown autonomy level {slug!r}") from None


_LABELS = {
    Level.NEVER: "Never",
    Level.DRAFT: "Draft only",
    Level.ASK_FIRST: "Ask first",
    Level.RUN_AND_TELL: "Run and tell",
    Level.RUN: "Run",
}
