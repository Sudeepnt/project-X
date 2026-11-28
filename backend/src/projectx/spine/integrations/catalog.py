"""Every outside tool the product can use, what keys it needs, and whether the platform offers its own.

A business picks, per tool: its own keys (stored in the vault, tested on save), the platform's ("ours"), or off.
Adding a tool is one entry here plus, if it has a live check, one `test` function.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from projectx.services import Services
    from projectx.settings import Settings


@dataclass(frozen=True)
class KeyField:
    name: str
    label: str
    secret: bool = True
    required: bool = True
    hint: str = ""


Tester = Callable[["Services", dict[str, Any]], Awaitable[str | None]]  # returns a problem, or None when it works


@dataclass(frozen=True)
class Tool:
    key: str
    title: str
    category: str  # payments | email | ai | calendar | messaging
    description: str
    fields: tuple[KeyField, ...]
    platform: Callable[[Settings], bool] = field(default=lambda s: False)  # does the platform offer its own?
    platform_note: str = ""
    test: Tester | None = None
    webhook_path: str | None = None  # where the provider should send events for own keys ({slug} is filled in)


_TOOLS: dict[str, Tool] = {}


def register_tool(tool: Tool) -> Tool:
    _TOOLS[tool.key] = tool
    return tool


def get_tool(key: str) -> Tool | None:
    return _TOOLS.get(key)


def all_tools() -> list[Tool]:
    return sorted(_TOOLS.values(), key=lambda t: (t.category, t.key))
