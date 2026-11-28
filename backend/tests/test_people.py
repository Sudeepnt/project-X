"""People with the other departments: payroll computed → approved → booked and paid by Finance → payslips;
leave checked against allowance and team cover; pay maths in integers."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from projectx.kernel.time import today_in
from projectx.modules.people import service as people
from projectx.modules.people.models import PayrollRun
from projectx.services import Services
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.worker import Worker
from projectx.spine.tenancy.models import Business


def url(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}{path}"


def test_pay_maths() -> None:
    nov_start, nov_end = date(2026, 11, 1), date(2026, 11, 30)
    full = people.payslip_for(300_000, date(2026, 1, 1), None, nov_start, nov_end, 0, 1000)
    assert full == {
        "days_employed": 30,
        "period_days": 30,
        "unpaid_days": 0,
        "gross_minor": 300_000,
        "withheld_minor": 30_000,
        "net_minor": 270_000,
    }
    joiner = people.payslip_for(300_000, date(2026, 11, 16), None, nov_start, nov_end, 0, 1000)
    assert joiner["days_employed"] == 15 and joiner["gross_minor"] == 150_000
    leaver = people.payslip_for(300_000, date(2025, 1, 1), date(2026, 11, 10), nov_start, nov_end, 0, 0)
    assert leaver["gross_minor"] == 100_000
    unpaid = people.payslip_for(
        300_000, date(2026, 1, 1), None, nov_start, nov_end, 3, 0
    )  # 21 working days in Nov 2026
    assert unpaid["gross_minor"] == 300_000 - 300_000 * 3 // 21
    assert people.working_days(date(2026, 11, 28), date(2026, 12, 1)) == 2  # Sat, Sun, Mon, Tue


async def test_payroll_runs_through_people_and_finance_to_payslips(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    for name, salary in (("Ravi Kumar", 200_000), ("Asha Rao", 300_000)):
        res = await founder.post(
            url(business, "/people/employees"),
            json={
                "name": name,
                "email": f"{name.split()[0].lower()}-{uuid.uuid4().hex[:4]}@example.com",
                "role": "Editor",
                "start_date": "2026-01-05",
                "monthly_salary_minor": salary,
            },
        )
        assert res.status_code == 201, res.text
    async with services.db.tenant(bid) as s:
        biz = await s.get(Business, bid)
        assert biz is not None
        settings = await people.settings_for(s, bid)
        today = today_in(biz.timezone)
        run = await people.prepare_payroll(s, biz, today.year, today.month, settings)
        assert run is not None
        assert (run.employees, run.gross_minor, run.withheld_minor, run.net_minor) == (2, 500_000, 50_000, 450_000)
        assert await people.prepare_payroll(s, biz, today.year, today.month, settings) is None  # once per month
        card_id = run.decision_id
    card = (await founder.get(url(business, f"/decisions/{card_id}"))).json()
    assert card["status"] == "pending" and card["cost_minor"] == 450_000
    assert (await founder.post(url(business, f"/decisions/{card_id}/approve"), json={})).status_code == 200
    await Worker(services).run_until_idle()  # People → bus → Finance books payroll, proposes paying it

    bills = (await founder.get(url(business, "/finance/bills"))).json()["items"]
    assert [(b["category"], b["amount_minor"], b["status"]) for b in bills] == [("payroll", 450_000, "open")]
    before = len(services.sent_emails)  # type: ignore[attr-defined]
    assert (
        await founder.post(url(business, f"/decisions/{bills[0]['decision_id']}/approve"), json={})
    ).status_code == 200
    await Worker(services).run_until_idle()  # Finance pays → bus → People sends payslips

    slips = [m for m in services.sent_emails[before:] if m["subject"].startswith("Payslip ")]  # type: ignore[attr-defined]
    assert len(slips) == 2 and any("Net paid:" in m["text"] for m in slips)
    async with services.db.tenant(bid) as s:
        run = (await s.execute(select(PayrollRun))).scalar_one()
    assert run.status == "paid"
    sheet = (await founder.get(url(business, "/finance/reports/balance-sheet"))).json()
    assert sheet["balanced"] is True
    assert sheet["accounts"]["Salaries and benefits"] == 500_000
    assert sheet["accounts"]["Cash and bank"] == -450_000
    assert sheet["accounts"]["Tax payable"] == 50_000  # withheld, owed to the tax office
    assert "Payroll liabilities" not in sheet["accounts"]  # fully paid


async def test_leave_is_checked_against_allowance_and_team_cover(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    ids = []
    for name in ("Kiran", "Lata"):
        e = (
            await founder.post(
                url(business, "/people/employees"),
                json={
                    "name": name,
                    "email": f"{name.lower()}-{uuid.uuid4().hex[:4]}@example.com",
                    "role": "Reporter",
                    "team": "news",
                    "start_date": "2026-01-05",
                    "monthly_salary_minor": 100_000,
                },
            )
        ).json()
        ids.append(e["id"])
    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 + 14)
    first = (
        await founder.post(
            url(business, f"/people/employees/{ids[0]}/leave"),
            json={"start_date": monday.isoformat(), "end_date": (monday + timedelta(days=4)).isoformat()},
        )
    ).json()
    assert first["days"] == 5
    card = (await founder.get(url(business, f"/decisions/{first['decision_id']}"))).json()
    assert card["reasons"] == [] or all(r["code"] not in ("team_cover", "over_allowance") for r in card["reasons"])
    await founder.post(url(business, f"/decisions/{first['decision_id']}/approve"), json={})
    await Worker(services).run_until_idle()

    clash = (
        await founder.post(
            url(business, f"/people/employees/{ids[1]}/leave"),
            json={
                "start_date": (monday + timedelta(days=2)).isoformat(),
                "end_date": (monday + timedelta(days=30)).isoformat(),
            },
        )
    ).json()
    reasons = {
        r["code"] for r in (await founder.get(url(business, f"/decisions/{clash['decision_id']}"))).json()["reasons"]
    }
    assert {"team_cover", "over_allowance"} <= reasons
    leave = (await founder.get(url(business, "/people/leave"))).json()["items"]
    assert sorted(r["status"] for r in leave) == ["approved", "pending"]
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        cards = (
            (await s.execute(select(Decision).where(Decision.action_type == "people.approve_leave"))).scalars().all()
        )
    assert len(cards) == 2
