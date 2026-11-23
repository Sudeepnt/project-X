"""Registry of ledger event types.

Every event type is declared once, with a schema for its data. Appending an undeclared type
fails, which catches typos and keeps the ledger queryable for years. Schemas may not declare
fields that look like personal data: the ledger is permanent, personal data must be erasable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

_PII_FIELD_NAMES = frozenset(
    {
        "email",
        "phone",
        "name",
        "first_name",
        "last_name",
        "full_name",
        "address",
        "ip",
        "ip_address",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "dob",
        "birthday",
    }
)


class EventData(BaseModel):
    """Base class for event payload schemas. Extra keys are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class EventType:
    key: str
    description: str
    schema: type[BaseModel] | None = None
    version: int = 1

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.schema is None:
            return data
        return self.schema.model_validate(data).model_dump(mode="json", exclude_none=True)


_REGISTRY: dict[str, EventType] = {}


def register_event(key: str, description: str, schema: type[BaseModel] | None = None, *, version: int = 1) -> EventType:
    if "." not in key:
        raise ValueError(f"event type must be namespaced like 'module.thing.happened': {key!r}")
    if schema is not None:
        bad = _PII_FIELD_NAMES.intersection(schema.model_fields)
        if bad:
            raise ValueError(f"event {key!r} declares personal-data fields {sorted(bad)}; store ids instead")
    existing = _REGISTRY.get(key)
    event_type = EventType(key=key, description=description, schema=schema, version=version)
    if existing is not None and existing != event_type:
        raise ValueError(f"event type {key!r} registered twice with different definitions")
    _REGISTRY[key] = event_type
    return event_type


def get_event_type(key: str) -> EventType:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise KeyError(f"unknown ledger event type {key!r}; declare it with register_event()") from None


def all_event_types() -> list[EventType]:
    return sorted(_REGISTRY.values(), key=lambda e: e.key)
