"""Per-business GTM settings, stored in ``businesses.settings["gtm"]`` with safe defaults."""

from __future__ import annotations

import copy
from typing import Any

from projectx.spine.tenancy.models import Business

DEFAULT_AUTOMATIONS: list[dict[str, Any]] = [
    {
        "key": "subscriber_welcome",
        "active": True,
        "trigger": {"type": "stage_entered", "stage": "subscriber"},
        "action": {"type": "enroll", "sequence": "welcome"},
    },
    {
        "key": "mql_sales_sequence",
        "active": True,
        "trigger": {"type": "stage_entered", "stage": "mql"},
        "action": {"type": "enroll", "sequence": "nurture_to_convert"},
    },
    {
        "key": "customer_onboarding",
        "active": True,
        "trigger": {"type": "stage_entered", "stage": "customer"},
        "action": {"type": "enroll", "sequence": "onboarding"},
    },
    {
        "key": "inactive_at_risk",
        "active": True,
        "trigger": {"type": "inactive_days", "days": 14, "stages": ["customer", "active"]},
        "action": {"type": "side_state", "value": "at_risk", "then_enroll": "win_back"},
    },
    {
        "key": "happy_referral",
        "active": True,
        "trigger": {"type": "satisfaction", "min": 9},
        "action": {"type": "enroll", "sequence": "referral_request"},
    },
    {
        "key": "churned_win_back",
        "active": True,
        "trigger": {"type": "side_state_entered", "value": "churned"},
        "action": {"type": "enroll", "sequence": "win_back"},
    },
]

DEFAULTS: dict[str, Any] = {
    "mql_threshold": 50,
    "score_half_life_days": 30,
    "value_ratio": None,  # learned per business; None = use the goal default
    "ai_label": "platform",  # platform: use platform AI labels | text: add a text disclosure | off
    "require_consent_cookies": True,
    "public_dashboard": {"enabled": False, "metrics": ["revenue", "subscribers", "leads", "customers"]},
    "automations": DEFAULT_AUTOMATIONS,
    "posting_hours": {"default": [9, 12, 18]},
    "newsletter": {"enabled": False, "send_hour": 7, "double_opt_in": True},
    "inbox": {"auto_reply_confidence": 0.85},
}


def gtm_settings(business: Business) -> dict[str, Any]:
    merged = copy.deepcopy(DEFAULTS)
    merged.update((business.settings or {}).get("gtm", {}))
    return merged


def update_gtm_settings(business: Business, patch: dict[str, Any]) -> dict[str, Any]:
    settings = dict(business.settings or {})
    current = dict(settings.get("gtm", {}))
    current.update(patch)
    settings["gtm"] = current
    business.settings = settings
    return gtm_settings(business)
