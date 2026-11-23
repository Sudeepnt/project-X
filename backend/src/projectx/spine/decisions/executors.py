"""Executors perform approved actions; undoers reverse them.

Executors manage their own short transactions: they must not hold a database transaction open
while calling an external API.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from projectx.services import Services


@dataclass
class ExecutionContext:
    services: Services
    business_id: uuid.UUID
    decision_id: uuid.UUID
    action_type: str
    payload: dict[str, Any]
    option: str | None = None
    undo_payload: dict[str, Any] | None = None


@dataclass
class ExecutionResult:
    data: dict[str, Any] = field(default_factory=dict)
    undo_payload: dict[str, Any] | None = None
    amount_minor: int | None = None
    currency: str | None = None
    external_ref: str | None = None
    summary: str | None = None


Executor = Callable[[ExecutionContext], Awaitable[ExecutionResult]]
Undoer = Callable[[ExecutionContext], Awaitable[dict[str, Any]]]

_EXECUTORS: dict[str, Executor] = {}
_UNDOERS: dict[str, Undoer] = {}


def executor(action_type: str) -> Callable[[Executor], Executor]:
    def decorator(fn: Executor) -> Executor:
        _EXECUTORS[action_type] = fn
        return fn

    return decorator


def undoer(action_type: str) -> Callable[[Undoer], Undoer]:
    def decorator(fn: Undoer) -> Undoer:
        _UNDOERS[action_type] = fn
        return fn

    return decorator


def get_executor(action_type: str) -> Executor | None:
    return _EXECUTORS.get(action_type)


def get_undoer(action_type: str) -> Undoer | None:
    return _UNDOERS.get(action_type)
