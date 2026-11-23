"""Registry of action types: everything an agent may ask to do, with its safety metadata.

An action that is not registered cannot run: the policy engine treats it as NEVER.
"""

from __future__ import annotations

from dataclasses import dataclass

from projectx.spine.policy.levels import Level


@dataclass(frozen=True)
class ActionType:
    key: str
    module: str
    title: str
    description: str
    default_level: Level
    max_level: Level
    reversible: bool
    moves_money: bool = False
    public: bool = False
    undo: str | None = None
    pack: str | None = None
    promotable: bool = True

    def __post_init__(self) -> None:
        if self.default_level > self.max_level:
            raise ValueError(f"{self.key}: default level is above the ceiling")
        if not self.reversible and self.max_level > Level.ASK_FIRST:
            raise ValueError(f"{self.key}: an irreversible action (one-way door) can never run without approval")
        if self.reversible and self.max_level >= Level.RUN_AND_TELL and not self.undo:
            raise ValueError(f"{self.key}: actions that may run alone must say how to undo them")

    @property
    def one_way_door(self) -> bool:
        return not self.reversible


_REGISTRY: dict[str, ActionType] = {}


def register_action(action: ActionType) -> ActionType:
    existing = _REGISTRY.get(action.key)
    if existing is not None and existing != action:
        raise ValueError(f"action {action.key!r} registered twice with different definitions")
    _REGISTRY[action.key] = action
    return action


def get_action(key: str) -> ActionType | None:
    return _REGISTRY.get(key)


def all_actions() -> list[ActionType]:
    return sorted(_REGISTRY.values(), key=lambda a: a.key)
