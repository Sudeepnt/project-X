"""Registers the GTM module with the spine."""

from __future__ import annotations

from projectx.spine.modules import Module, register_module

MODULE = register_module(
    Module(
        key="gtm",
        title="Go-to-market",
        description="Brand, goals, campaigns, content, publishing, CRM, nurture, analytics and learning.",
    )
)
