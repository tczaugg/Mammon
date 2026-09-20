"""Predictions from history (mammon.predictions) and how the calendar and
projection carry them: what recurs, what is trusted, what is dismissed, how a
prediction becomes a definition, and the two calendar defects that prompted
the work -- the unreadable today cell in dark mode and the loans and assets
summed into "all spending accounts"."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger, loans, predictions, projection, scheduled
from mammon.ui import projection_dialogs, style
from mammon.ui.projection_dialogs import (CalendarPanel, ProjectedBalancesDialog,
                                          spending_accounts)
from mammon.tests import fresh_db

TODAY = "2026-09-02"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "pred.db")
    yield c
    c.close()


def _add(conn, aid, date, amount, payee, **kw):
    return ledger.add_transaction(conn, aid, date, amount, payee=payee, **kw)


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=5000_00)
    util = ledger.resolve_category(conn, "Utilities")
    # A monthly bill whose amount drifts: three occurrences.
    for date, amt in (("2026-06-05", -80_00), ("2026-07-06", -85_00), ("2026-08-05", -82_00)):
        _add(conn, chk, date, amt, "Power Co", category_id=util)
    # Paydays every other Friday, marked as direct deposit by the bank.
    d = "2026-06-12"
    for _ in range(6):
        _add(conn, chk, d, 2100_00, "Employer", memo="DIRECT DEP PAYROLL")
        d = scheduled.advance_date(d, "biweekly")
    # Two similar rows a month apart: enough only because they nearly match.
    _add(conn, chk, "2026-07-20", -15_99, "Streaming")
    _add(conn, chk, "2026-08-20", -15_99, "Streaming")
    # Two loosely similar rows: not a pattern.
    _add(conn, chk, "2026-07-11", -60_00, "Hardware")
    _add(conn, chk, "2026-08-11", -45_00, "Hardware")
    # Two loosely similar rows the bank calls automatic: trusted anyway.
    _add(conn, chk, "2026-07-15", -40_00, "Water Dist", memo="AUTOPAY")
    _add(conn, chk, "2026-08-15", -55_00, "Water Dist", memo="AUTOPAY")
    return {"chk": chk, "util": util}


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def test_predict_recurring_finds_steady_payees_and_weighs_trust(conn, world):
    found = {p.payee: p for p in predictions.predict_recurring(conn, TODAY)}
    assert set(found) == {"Power Co", "Employer", "Streaming", "Water Dist"}
    power = found["Power Co"]
    assert (power.frequency, power.amount, power.next_date, power.count, power.automatic,
            power.category_id, power.varies) == \
        ("monthly", -82_00, "2026-09-05", 3, False, world["util"], True)
    pay = found["Employer"]
    assert (pay.frequency, pay.amount, pay.automatic, pay.count) == ("biweekly", 2100_00, True, 6)
    assert pay.next_date >= TODAY
    assert found["Streaming"].count == 2 and not found["Streaming"].automatic
    assert found["Water Dist"].automatic and found["Water Dist"].amount == -47_50   # the median
    assert predictions.looks_automatic("ONLINE PMT ACH") and not predictions.looks_automatic("CACHE")


def test_series_are_read_the_way_a_person_reads_them(conn):
    """Real series are untidy: a tenant pays 290 one month, a paycheck posts
    twice three days apart and moves for a holiday, an automatic deposit has
    one outsized month. Each was a miss until the detector collapsed
    near-duplicates, fitted the period on a grid and took the typical amount."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    for date, amt in (("2026-05-05", 1550_00), ("2026-06-06", 1550_00),
                      ("2026-07-05", 290_00), ("2026-08-04", 1550_00)):
        _add(conn, chk, date, amt, "Tenant A", memo="Rent")
    for date, amt in (("2026-05-08", 3342_52), ("2026-05-11", 3342_53), ("2026-06-02", 3344_53),
                      ("2026-06-05", 3344_53), ("2026-06-22", 3344_54), ("2026-07-03", 3344_53),
                      ("2026-07-20", 3344_53), ("2026-08-03", 3344_53), ("2026-08-17", 3344_54)):
        _add(conn, chk, date, amt, "Employer Inc.")
    for date, amt in (("2026-04-28", 1852_02), ("2026-05-28", 1503_09),
                      ("2026-06-26", 1178_81), ("2026-07-28", 5094_93)):
        _add(conn, chk, date, amt, "Marketplace", memo="AUTOMATIC DEPOSIT, MARKETPLACE PAYME")
    # Rent paid in two parts within three days is one occurrence, summed.
    for date, amt in (("2026-06-01", 1000_00), ("2026-06-03", 200_00), ("2026-07-01", 1200_00),
                      ("2026-08-02", 1200_00)):
        _add(conn, chk, date, amt, "Tenant B")
    found = {p.payee: p for p in predictions.predict_recurring(conn, "2026-09-02")}
    a = found["Tenant A"]
    assert (a.frequency, a.amount, a.count, a.next_date, a.varies) == \
        ("monthly", 1550_00, 4, "2026-09-04", True)
    pay = found["Employer Inc."]
    assert (pay.frequency, pay.amount, pay.count) == ("biweekly", 3344_54, 7)   # latest of the usual
    assert pay.next_date == "2026-09-14"                       # from the last real payday
    m = found["Marketplace"]
    # The median of the four, not the outsized July, is what to expect.
    assert (m.frequency, m.automatic, m.amount, m.count) == \
        ("monthly", True, (1503_09 + 1852_02) // 2, 4)
    b = found["Tenant B"]
    assert (b.frequency, b.amount, b.count) == ("monthly", 1200_00, 3)
    assert predictions.fit_period(["2026-01-05", "2026-02-20", "2026-03-04"]) is None
    assert predictions.typical_amount([80_00, 85_00, 82_00]) == 82_00
    assert predictions.typical_amount([1550_00, 1550_00, 290_00, 1550_00]) == 1550_00
    assert predictions.typical_amount([1500_00, 1500_00, 1500_00, 1600_00, 1600_00]) == 1600_00


def test_a_split_month_counts_for_its_usual_category_line(conn):
    """A tenant's July deposit is a split: the rent, and a credit for the
    refrigerator he bought. The deposit total (290) is not the rent; the rent
    line (1,550) is, and the series is regular once it is read that way."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    rent = ledger.resolve_category(conn, "Rental Income")
    appliance = ledger.resolve_category(conn, "Appliances")
    for date in ("2026-05-05", "2026-06-06", "2026-08-04"):
        _add(conn, chk, date, 1550_00, "Tenant", category_id=rent)
    july = _add(conn, chk, "2026-07-05", 290_00, "Tenant")
    ledger.set_splits(conn, july, [{"category_id": rent, "amount": 1550_00},
                                   {"category_id": appliance, "amount": -1260_00,
                                    "memo": "new refrigerator"}])
    # A payee that is always split keeps its total: the net paycheck is what lands.
    gross = ledger.resolve_category(conn, "Salary")
    tax = ledger.resolve_category(conn, "Tax:Federal")
    for date in ("2026-06-05", "2026-06-19", "2026-07-03", "2026-07-17", "2026-07-31",
                 "2026-08-14", "2026-08-28"):
        pid = _add(conn, chk, date, 3300_00, "Payroll")
        ledger.set_splits(conn, pid, [{"category_id": gross, "amount": 5000_00},
                                      {"category_id": tax, "amount": -1700_00}])
    found = {p.payee: p for p in predictions.predict_recurring(conn, "2026-09-02")}
    t = found["Tenant"]
    assert (t.amount, t.varies, t.category_id, t.count) == (1550_00, False, rent, 4)
    p = found["Payroll"]
    assert (p.amount, p.frequency, p.category_id) == (3300_00, "biweekly", gross)


def test_definitions_loans_and_dismissals_are_left_out(conn, world):
    chk = world["chk"]
    # A scheduled definition supersedes the prediction of the same payee.
    scheduled.add_scheduled(conn, chk, payee="Power Co", amount=-82_00, frequency="monthly",
                            next_date="2026-09-05")
    assert "Power Co" not in {p.payee for p in predictions.predict_recurring(conn, TODAY)}
    # A loan payment belongs to the loan schedule, however regular.
    loan = ledger.create_account(conn, "Mortgage", "liability", opening_balance=-100000_00)
    loans.set_loan_params(conn, loan, original_principal=100000_00, origination_date="2026-01-01",
                          term_months=360, payment_amount=800_00, interval="monthly",
                          rates=[("2026-01-01", "6.0")])
    for date in ("2026-06-01", "2026-07-01", "2026-08-01"):
        tid = _add(conn, chk, date, -800_00, "US Bank")
        ledger.set_splits(conn, tid, [
            {"category_id": ledger.resolve_category(conn, "Int Exp"), "amount": -500_00},
            {"transfer_account_id": loan, "amount": -300_00}])
    assert "US Bank" not in {p.payee for p in predictions.predict_recurring(conn, TODAY)}
    # Dismissed: gone until restored.
    predictions.dismiss(conn, chk, "Streaming")
    names = {p.payee for p in predictions.predict_recurring(conn, TODAY)}
    assert "Streaming" not in names and "Employer" in names
    assert [(d["payee"], d["account_name"]) for d in predictions.dismissed(conn)] == \
        [("Streaming", "Checking")]
    assert "Streaming" in {p.payee for p in predictions.predict_recurring(
        conn, TODAY, include_dismissed=True)}
    predictions.restore(conn, chk, predictions.payee_key("Streaming"))
    assert "Streaming" in {p.payee for p in predictions.predict_recurring(conn, TODAY)}


# ---------------------------------------------------------------------------
# the projection carries predictions
# ---------------------------------------------------------------------------
def test_projection_places_predictions_and_skips_entered_occurrences(conn, world):
    chk = world["chk"]
    events = projection.projected_events(conn, [chk], TODAY, "2026-09-30", today=TODAY)
    predicted = [(e.date, e.payee, e.amount, e.automatic) for e in events
                 if e.source == projection.PREDICTED]
    assert ("2026-09-05", "Power Co", -82_00, False) in predicted
    assert ("2026-09-20", "Streaming", -15_99, False) in predicted
    assert any(p[1] == "Employer" and p[3] for p in predicted)
    # The user already entered September's power bill two days early: no estimate beside it.
    _add(conn, chk, "2026-09-03", -83_50, "Power Co")
    events = projection.projected_events(conn, [chk], TODAY, "2026-09-30", today=TODAY)
    assert not any(e.payee == "Power Co" and e.source == projection.PREDICTED for e in events)
    assert any(e.payee == "Power Co" and e.source == projection.ENTERED for e in events)
    # Reminders-only view on request.
    plain = projection.projected_events(conn, [chk], TODAY, "2026-09-30", today=TODAY,
                                        include_predictions=False)
    assert not any(e.source == projection.PREDICTED for e in plain)
    p = projection.project(conn, [chk], TODAY, "2026-09-30", today=TODAY)
    assert p.closing == p.opening + sum(e.amount for e in p.events())


# ---------------------------------------------------------------------------
# the calendar and projected balances
# ---------------------------------------------------------------------------
def test_spending_accounts_leave_out_assets_loans_and_investments(qapp, conn, world):
    ledger.create_account(conn, "House", "asset", opening_balance=900000_00)
    ledger.create_account(conn, "Mortgage", "liability", opening_balance=-400000_00)
    ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ledger.create_account(conn, "Visa", "credit", opening_balance=-2616_00)
    assert [a["name"] for a in spending_accounts(conn)] == ["Checking", "Visa"]
    dlg = ProjectedBalancesDialog(conn, today=TODAY)
    assert dlg.projection.opening == 5000_00 + sum(
        r["amount"] for r in conn.execute("SELECT amount FROM transactions")) - 2616_00
    dlg.deleteLater()


def test_calendar_colours_events_by_kind_and_theme_and_reads_today(qapp, conn, world, monkeypatch):
    chk = world["chk"]
    scheduled.add_scheduled(conn, chk, payee="Rent", amount=-1500_00, frequency="monthly",
                            next_date="2026-09-10")
    scheduled.add_scheduled(conn, chk, payee="Refund", amount=200_00, frequency="monthly",
                            next_date="2026-09-12")
    monkeypatch.setattr(style, "theme", lambda: "light")
    dlg = CalendarPanel(conn, year=2026, month=9, today=TODAY)
    assert dlg.cell_text(5).startswith("5\n~ Power Co -82.00")
    assert "· Rent -1,500.00" in dlg.cell_text(10) and "· Refund 200.00" in dlg.cell_text(12)
    assert [e.payee for e in dlg.predictions_on(5)] == ["Power Co"]
    html5 = dlg.grid.cellWidget(*_pos(2026, 9, 5)).text()
    assert "#a8701a" in html5                                   # predicted payment, yellow
    assert "#b2382c" in dlg.grid.cellWidget(*_pos(2026, 9, 10)).text()   # scheduled payment
    assert "#2e6b4e" in dlg.grid.cellWidget(*_pos(2026, 9, 12)).text()   # scheduled deposit
    pay_day = next(d for d in range(1, 31) if any(e.payee == "Employer" for e in dlg.events_on(d)))
    assert "#1f5fa8" in dlg.grid.cellWidget(*_pos(2026, 9, pay_day)).text()  # predicted deposit
    today_cell = dlg.grid.cellWidget(*_pos(2026, 9, 2))
    # Read the expected colour from the palette rather than repeating its hex:
    # what matters is that today is highlighted with the LIGHT set and that the
    # two themes differ. Hardcoding the value made a deliberate palette tweak
    # (commit 9a2016c, calendar highlight) look like a regression.
    light_bg, dark_bg = projection_dialogs._TODAY_BG
    assert light_bg in today_cell.styleSheet()
    dlg.deleteLater()
    # Dark theme: every colour and the today highlight come from the dark set.
    monkeypatch.setattr(style, "theme", lambda: "dark")
    dark = CalendarPanel(conn, year=2026, month=9, today=TODAY)
    assert "#e5c07b" in dark.grid.cellWidget(*_pos(2026, 9, 5)).text()
    assert "#ff6b6b" in dark.grid.cellWidget(*_pos(2026, 9, 10)).text()
    today_dark = dark.grid.cellWidget(*_pos(2026, 9, 2))
    assert dark_bg in today_dark.styleSheet() and light_bg not in today_dark.styleSheet()
    assert "predicted payment" in dark.legend.text()
    dark.deleteLater()


def test_calendar_dismisses_and_schedules_predictions(qapp, conn, world, monkeypatch):
    chk = world["chk"]
    monkeypatch.setattr(style, "theme", lambda: "light")
    dlg = CalendarPanel(conn, year=2026, month=9, today=TODAY)
    assert [e.payee for e in dlg.predictions_on(20)] == ["Streaming"]
    dlg.dismiss_prediction(chk, "Streaming")
    assert dlg.predictions_on(20) == []
    assert [d["payee"] for d in predictions.dismissed(conn)] == ["Streaming"]
    # Schedule the power bill from its prediction, correcting the amount in the editor.
    [power] = dlg.predictions_on(5)
    seen = {}

    def fake_editor(entry):
        seen.update(entry)
        return {"account_id": entry["account_id"], "payee": entry["payee"], "amount": -90_00,
                "frequency": entry["frequency"], "next_date": entry["next_date"],
                "category_id": entry["category_id"]}

    dlg._edit_definition = fake_editor
    sid = dlg.schedule_prediction(power)
    assert (seen["payee"], seen["amount"], seen["frequency"], seen["next_date"],
            seen["category_label"]) == ("Power Co", -82_00, "monthly", "2026-09-05", "Utilities")
    d = scheduled.get_scheduled(conn, sid)
    assert (d["payee"], d["amount"], d["next_date"]) == ("Power Co", -90_00, "2026-09-05")
    assert dlg.predictions_on(5) == []                          # superseded by the definition
    assert "· Power Co -90.00" in dlg.cell_text(5)
    # Cancelling the editor changes nothing.
    dlg._edit_definition = lambda entry: None
    [pay] = [e for e in dlg.predictions_on(
        next(d for d in range(1, 31) if any(e.payee == "Employer" for e in dlg.predictions_on(d))))]
    assert dlg.schedule_prediction(pay) is None
    assert scheduled.get_scheduled(conn, sid + 1) is None
    dlg.include_predictions.setChecked(False)
    assert all(not dlg.predictions_on(d) for d in range(1, 31))
    dlg.deleteLater()


def _pos(year, month, day):
    import datetime as _dt
    first = _dt.date(year, month, 1)
    offset = (first.weekday() + 1) % 7
    p = offset + day - 1
    return p // 7, p % 7
