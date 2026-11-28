"""Registers the Strategy department with the spine."""

from __future__ import annotations

from projectx.modules.strategy import service
from projectx.modules.strategy.api import router
from projectx.spine.modules import Module, register_module

MODULE = register_module(
    Module(
        key="strategy",
        title="Strategy",
        description="The company in one view: numbers from every department, objectives, risks, weekly review.",
        routers=[router],
        weekly_cycle=service.weekly,
    )
)
