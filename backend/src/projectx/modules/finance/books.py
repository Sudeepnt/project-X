"""Double-entry books. Every number in a finance report is computed here from journal lines, never by a model."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import ValidationFailed
from projectx.modules.finance.models import Account, FinanceSettings, JournalEntry, JournalLine
from projectx.spine.tenancy.models import Business

CASH, AR, AP, TAX, PAYROLL_LIAB, EQUITY = "1000", "1100", "2000", "2100", "2200", "3000"
REVENUE, COST, SALARIES, SOFTWARE, OTHER = "4000", "5000", "6000", "6100", "6200"
CHART = (
    (CASH, "Cash and bank", "asset"),
    (AR, "Accounts receivable", "asset"),
    (AP, "Accounts payable", "liability"),
    (TAX, "Tax payable", "liability"),
    (PAYROLL_LIAB, "Payroll liabilities", "liability"),
    (EQUITY, "Owner's equity", "equity"),
    (REVENUE, "Revenue", "income"),
    (COST, "Cost of sales", "expense"),
    (SALARIES, "Salaries and benefits", "expense"),
    (SOFTWARE, "Software and subscriptions", "expense"),
    (OTHER, "Other expenses", "expense"),
)
CATEGORY_ACCOUNT = {"payroll": PAYROLL_LIAB, "software": SOFTWARE, "tax": TAX, "cost": COST}
DEFAULT_TAX_BP = {"IN": 1800, "GB": 2000, "DE": 1900, "FR": 2000, "AU": 1000, "SG": 900, "NZ": 1500, "AE": 500}


async def setup(session: AsyncSession, business: Business) -> FinanceSettings:
    """Chart of accounts and settings, created once per business."""
    settings = await session.get(FinanceSettings, business.id)
    if settings is None:
        settings = FinanceSettings(
            business_id=business.id,
            tax_rate_bp=DEFAULT_TAX_BP.get((business.country or "").upper(), 0),
            tax_name="GST"
            if (business.country or "").upper() in ("IN", "AU", "SG", "NZ")
            else "VAT"
            if (business.country or "").upper() in ("GB", "DE", "FR", "AE")
            else "Tax",
        )
        session.add(settings)
    existing = set((await session.execute(select(Account.code))).scalars())
    for code, name, kind in CHART:
        if code not in existing:
            session.add(Account(business_id=business.id, code=code, name=name, type=kind))
    await session.flush()
    return settings


async def _accounts(session: AsyncSession) -> dict[str, Account]:
    return {a.code: a for a in (await session.execute(select(Account))).scalars()}


async def post(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    entry_date: date,
    memo: str,
    source: tuple[str, uuid.UUID, str],
    currency: str,
    lines: list[tuple[str, int, int]],
) -> JournalEntry | None:
    """Post a balanced entry: lines are (account code, debit, credit). Idempotent per (source type, id, purpose)."""
    source_type, source_id, purpose = source
    existing = await session.scalar(
        select(JournalEntry).where(
            JournalEntry.source_type == source_type,
            JournalEntry.source_id == source_id,
            JournalEntry.purpose == purpose,
        )
    )
    if existing is not None:
        return existing
    lines = [(code, d, c) for code, d, c in lines if d or c]
    debit, credit = sum(d for _, d, _ in lines), sum(c for _, _, c in lines)
    if not lines or debit != credit:
        raise ValidationFailed(f"Unbalanced entry: debits {debit} vs credits {credit}")
    if any(d < 0 or c < 0 or (d and c) for _, d, c in lines):
        raise ValidationFailed("Each line is a debit or a credit, never negative")
    accounts = await _accounts(session)
    entry = JournalEntry(
        business_id=business_id,
        entry_date=entry_date,
        memo=memo[:300],
        source_type=source_type,
        source_id=source_id,
        purpose=purpose,
        currency=currency,
    )
    session.add(entry)
    await session.flush()
    for code, d, c in lines:
        session.add(
            JournalLine(
                business_id=business_id, entry_id=entry.id, account_id=accounts[code].id, debit_minor=d, credit_minor=c
            )
        )
    await session.flush()
    return entry


async def balances(session: AsyncSession, *, start: date | None = None, end: date | None = None) -> dict[str, int]:
    """Signed balance per account code: debit-normal for assets and expenses, credit-normal otherwise."""
    stmt = (
        select(
            Account.code,
            Account.type,
            func.coalesce(func.sum(JournalLine.debit_minor), 0),
            func.coalesce(func.sum(JournalLine.credit_minor), 0),
        )
        .join(JournalLine, JournalLine.account_id == Account.id, isouter=True)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id, isouter=True)
        .group_by(Account.code, Account.type)
    )
    if start is not None:
        stmt = stmt.where((JournalEntry.entry_date >= start) | (JournalEntry.id.is_(None)))
    if end is not None:
        stmt = stmt.where((JournalEntry.entry_date < end) | (JournalEntry.id.is_(None)))
    out: dict[str, int] = {}
    for code, kind, d, c in (await session.execute(stmt)).all():
        out[code] = int(d) - int(c) if kind in ("asset", "expense") else int(c) - int(d)
    return out


async def profit_and_loss(session: AsyncSession, start: date, end: date) -> dict[str, Any]:
    b = await balances(session, start=start, end=end)
    accounts = await _accounts(session)
    income = {c: v for c, v in b.items() if accounts[c].type == "income"}
    expenses = {c: v for c, v in b.items() if accounts[c].type == "expense"}
    total_income, total_expenses = sum(income.values()), sum(expenses.values())
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "income": {accounts[c].name: v for c, v in income.items() if v},
        "expenses": {accounts[c].name: v for c, v in expenses.items() if v},
        "total_income_minor": total_income,
        "total_expenses_minor": total_expenses,
        "net_minor": total_income - total_expenses,
    }


async def balance_sheet(session: AsyncSession) -> dict[str, Any]:
    b = await balances(session)
    accounts = await _accounts(session)
    sums = {"asset": 0, "liability": 0, "equity": 0, "income": 0, "expense": 0}
    for code, value in b.items():
        sums[accounts[code].type] += value
    retained = sums["income"] - sums["expense"]
    return {
        "assets_minor": sums["asset"],
        "liabilities_minor": sums["liability"],
        "equity_minor": sums["equity"] + retained,
        "balanced": sums["asset"] == sums["liability"] + sums["equity"] + retained,
        "accounts": {accounts[c].name: v for c, v in b.items() if v},
    }


async def runway(session: AsyncSession, today: date) -> dict[str, Any]:
    """Months of cash left at the average net cash burn of the last three full months."""
    cash = (await balances(session)).get(CASH, 0)
    first = today.replace(day=1)
    start = (first - timedelta(days=1)).replace(day=1)
    for _ in range(2):
        start = (start - timedelta(days=1)).replace(day=1)
    accounts = await _accounts(session)
    flow = await session.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            JournalLine.account_id == accounts[CASH].id,
                            JournalLine.debit_minor - JournalLine.credit_minor,
                        ),
                        else_=0,
                    )
                ),
                0,
            )
        )
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalEntry.entry_date >= start, JournalEntry.entry_date < first)
    )
    net_3m = int(flow.scalar_one())
    burn = -net_3m // 3 if net_3m < 0 else 0
    return {
        "cash_minor": cash,
        "monthly_burn_minor": burn,
        "months": round(cash / burn, 1) if burn > 0 else None,  # None: not burning cash
    }
