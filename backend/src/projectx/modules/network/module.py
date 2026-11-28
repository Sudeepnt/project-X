"""Registers the Network department with the spine."""

from __future__ import annotations

from projectx.modules.network import service
from projectx.modules.network.api import router
from projectx.spine.modules import Module, register_module

MODULE = register_module(
    Module(
        key="network",
        title="Network",
        description="Partners and relationships, and handshakes with other businesses' assistants.",
        routers=[router],
        daily_cycle=service.daily,
    )
)
