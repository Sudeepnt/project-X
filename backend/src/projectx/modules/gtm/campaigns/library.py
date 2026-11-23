"""Loads and validates the campaign library (library.yaml) once at import."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

CATEGORIES = ("awareness", "leads", "sales", "partnerships", "customer_success")
GOAL_TYPES = ("awareness", "demand", "sales", "partnerships", "retention", "reputation")
TIERS = ("free", "autopilot", "growth", "scale")
CATEGORY_FOR_GOAL = {
    "awareness": "awareness",
    "demand": "leads",
    "sales": "sales",
    "partnerships": "partnerships",
    "retention": "customer_success",
    "reputation": "customer_success",
}


class Playbook(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    category: Literal["awareness", "leads", "sales", "partnerships", "customer_success"]
    goals: list[str]
    choose_when: str
    when: dict[str, Any] = Field(default_factory=dict)
    strategies: list[str]
    runs: Literal["ai", "ai_human", "human"]
    tier: Literal["free", "autopilot", "growth", "scale"]
    channels: list[str]
    content_mix: dict[str, float]
    frequency: dict[str, float]
    value_ratio: float = Field(gt=0, le=20)
    metrics: list[str]
    budget_split: dict[str, float] = Field(default_factory=dict)
    page_kind: str | None = None
    sequence: str | None = None
    pipeline: str | None = None
    stop_rules: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[str]
    approval_note: str | None = None

    @field_validator("goals")
    @classmethod
    def _goals(cls, value: list[str]) -> list[str]:
        unknown = set(value) - set(GOAL_TYPES)
        if unknown:
            raise ValueError(f"unknown goal types {sorted(unknown)}")
        return value

    @field_validator("content_mix")
    @classmethod
    def _mix(cls, value: dict[str, float]) -> dict[str, float]:
        if value and abs(sum(value.values()) - 1.0) > 0.01:
            raise ValueError("content mix shares must add up to 1")
        return value

    @property
    def paid(self) -> bool:
        return bool(self.when.get("needs_budget"))

    def view(self) -> dict[str, Any]:
        return {**self.model_dump(), "paid": self.paid}


@lru_cache
def library() -> dict[str, Playbook]:
    data = yaml.safe_load((Path(__file__).with_name("library.yaml")).read_text())
    books = [Playbook.model_validate(item) for item in data["playbooks"]]
    keys = [b.key for b in books]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate playbook keys in library.yaml")
    return {b.key: b for b in books}


def get_playbook(key: str) -> Playbook | None:
    return library().get(key)


def playbooks(*, category: str | None = None, goal: str | None = None) -> list[Playbook]:
    out = list(library().values())
    if category:
        out = [p for p in out if p.category == category]
    if goal:
        out = [p for p in out if goal in p.goals]
    return out
