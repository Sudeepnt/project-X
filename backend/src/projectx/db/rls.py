"""Row-level security helpers used by migrations and by the isolation test.

Rule: every table with a ``business_id`` column is protected by RLS (enabled *and* forced, so
the table owner obeys it too) unless it is listed in ``RLS_EXEMPT`` with a reason.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import MetaData

# Tables that carry business_id but must be readable without a tenant context.
RLS_EXEMPT: dict[str, str] = {
    "memberships": "listing a user's businesses happens before a tenant is chosen",
    "jobs": "workers claim jobs across tenants, then set the tenant context to run them",
    "routing_keys": "public routes (links, pages, webhooks) resolve the business first",
    "subscriptions": "billing webhooks arrive without a tenant; holds no customer data",
    "usage_counters": "metering is written by system processes; holds counts only",
    "content_fingerprints": "cross-account duplicate checks; holds hashes only, no content",
}

TENANT_PREDICATE = "business_id = NULLIF(current_setting('app.business_id', true), '')::uuid"


def tenant_tables(metadata: MetaData) -> list[str]:
    return sorted(
        name for name, table in metadata.tables.items() if "business_id" in table.columns and name not in RLS_EXEMPT
    )


def enable_rls_sql(table: str) -> list[str]:
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"',
        f'CREATE POLICY tenant_isolation ON "{table}" USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})',
    ]


def enable_rls_statements(tables: Iterable[str]) -> list[str]:
    statements: list[str] = []
    for table in tables:
        statements.extend(enable_rls_sql(table))
    return statements
