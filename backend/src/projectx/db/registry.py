"""Imports every model module so `Base.metadata` is complete (Alembic, tests, RLS checks)."""

from __future__ import annotations

import importlib

from projectx.db.base import Base

MODEL_MODULES = (
    "projectx.spine.identity.models",
    "projectx.spine.tenancy.models",
    "projectx.spine.routing.models",
    "projectx.spine.ledger.models",
    "projectx.spine.jobs.models",
    "projectx.spine.policy.models",
    "projectx.spine.decisions.models",
    "projectx.spine.vault.models",
    "projectx.spine.gateway.models",
    "projectx.spine.notifications.models",
    "projectx.spine.memory.models",
    "projectx.spine.twin.models",
    "projectx.spine.billing.models",
    "projectx.spine.calendar.models",
    "projectx.spine.assistant.models",
    "projectx.spine.context.models",
    "projectx.spine.bus",
)


DEPARTMENT_MODELS = (
    "projectx.modules.finance.models",
    "projectx.modules.ops.models",
    "projectx.modules.people.models",
    "projectx.modules.legal.models",
    "projectx.modules.product.models",
)


def load_all_models() -> None:
    for name in MODEL_MODULES:
        importlib.import_module(name)
    for name in DEPARTMENT_MODELS:
        importlib.import_module(name)
    try:
        importlib.import_module("projectx.modules.gtm.models")
    except ModuleNotFoundError as exc:
        if exc.name != "projectx.modules.gtm.models":
            raise


load_all_models()
metadata = Base.metadata
