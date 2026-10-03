"""The Social Security dialog (mammon/ui/social_security_dialog.py).

Headless throughout: ``QT_QPA_PLATFORM=offscreen`` is set before Qt is imported
and the one modal the dialog owns -- the delete confirmation -- is the
overridable ``_confirm`` seam, which these tests answer by assignment rather
than by trying to click a message box the offscreen platform never shows.

What is actually being pinned here is the promise the dialog makes to its
reader: a benefit figure DIFFERS across claim ages (a single number invites the
question Mammon refuses to answer), every figure carries the line naming the
tables that produced it, and the basis under the earnings -- typed off the SSA
statement, or estimated from this ledger -- is stated on screen rather than
remembered. The estimate must also never overwrite a year the user typed, which
is one guarantee written in two places: ``replace_earnings`` skips a year held
under another source, and typing over an estimated year promotes it to
reported.

All names, amounts and earnings histories are synthetic.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import datetime as _dt

import pytest

from mammon import ledger, retirement
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "ss.db")
    yield c
    c.close()


TODAY = _dt.date(2026, 9, 23)
BIRTH_YEAR = 1964
WAGE_CENTS = 6_000_000  # $60,000 a year, flat, so the arithmetic is checkable


def make_dialog(conn, today=TODAY):
    from mammon.ui.social_security_dialog import SocialSecurityDialog

    return SocialSecurityDialog(conn, today=today)


def person_with_history(dlg, first=1990, last=2025, cents=WAGE_CENTS):
    """Add one person with a birth month/year and a full earnings history.

    Drives the widgets, not the database: the point of most of these tests is
    that what the user types lands in :mod:`mammon.retirement`.
    """
    from PyQt5.QtCore import Qt

    person_id = dlg.on_add_person()
    dlg.people_table.item(0, 0).setText("Test Person")
    dlg.people_table.cellWidget(0, 2).setCurrentIndex(6)     # June
    dlg.people_table.cellWidget(0, 3).setValue(BIRTH_YEAR)
    dlg.people_table.item(0, 4).setCheckState(Qt.Unchecked)
    for year in range(first, last + 1):
        dlg.on_add_year(year, cents)
    return person_id


# ---------------------------------------------------------------------------
# the end-to-end case: a benefit that differs across claim ages, and says why
# ---------------------------------------------------------------------------
def test_a_person_and_an_earnings_history_produce_a_benefit_per_claim_age(
        qapp, conn):
    dlg = make_dialog(conn)
    person_with_history(dlg)

    rows = dlg.benefit_rows()
    assert len(rows) >= 3                       # 62, full retirement age, 70
    labels = [r["label"] for r in rows]
    assert labels[0] == "62" and labels[-1] == "70"
    assert "full retirement age" in labels[1]

    cents = [r["cents"] for r in rows]
    assert all(c > 0 for c in cents)
    # Claiming later pays more per month; that IS the trade the panel shows.
    assert cents == sorted(cents)
    assert len(set(cents)) == len(cents)

    # Two columns, and only two: the claim age and what it pays. The third
    # column used to name the tables behind the figure; that is provenance and
    # it lives in the Retirement FAQ now (SRD 5.8o).
    assert dlg.benefit_table.rowCount() == len(rows)
    assert dlg.benefit_table.columnCount() == 2
    for row in range(dlg.benefit_table.rowCount()):
        assert dlg.benefit_table.item(row, 1).text()


def test_every_assumption_under_the_benefit_is_printed(qapp, conn):
    dlg = make_dialog(conn)
    person_with_history(dlg)
    notes = dlg.benefit_notes.text()

    assert "Average indexed monthly earnings" in notes
    # 36 years entered, so SSA's 35 averaging years are full and the
    # zero-years warning must NOT appear.
    assert "zero year" not in notes
    assert "indexed to" in notes
    assert "this person's own record only" in notes
    assert "Basis:" in notes


def test_a_short_history_says_how_many_zero_years_are_averaged_in(qapp, conn):
    dlg = make_dialog(conn)
    person_with_history(dlg, first=2016, last=2025)   # 10 years
    notes = dlg.benefit_notes.text()
    assert "%d zero years" % (retirement.SS_AIME_YEARS - 10) in notes


def test_the_panel_explains_itself_before_there_is_anything_to_compute(qapp, conn):
    dlg = make_dialog(conn)
    assert dlg.benefit_rows() == []
    assert "Add a person" in dlg.benefit_notes.text()

    dlg.on_add_person()
    assert "birth year" in dlg.benefit_notes.text()

    dlg.people_table.cellWidget(0, 3).setValue(BIRTH_YEAR)
    assert "SSA Earnings Report" in dlg.benefit_notes.text()


# ---------------------------------------------------------------------------
# the people table writes through mammon/retirement.py
# ---------------------------------------------------------------------------
def test_the_people_table_writes_through_the_domain_layer(qapp, conn):
    from PyQt5.QtCore import Qt

    dlg = make_dialog(conn)
    person_id = dlg.on_add_person()
    dlg.people_table.item(0, 0).setText("Synthetic Person")
    dlg.people_table.cellWidget(0, 1).setCurrentText("spouse")
    dlg.people_table.cellWidget(0, 2).setCurrentIndex(3)     # March
    dlg.people_table.cellWidget(0, 3).setValue(1971)
    dlg.people_table.item(0, 4).setCheckState(Qt.Checked)

    person = retirement.get_person(conn, person_id)
    assert person["name"] == "Synthetic Person"
    assert person["relationship"] == "spouse"
    assert person["birth_month"] == 3
    assert person["birth_year"] == 1971
    assert person["born_on_the_first"] == 1


def test_a_planned_claim_age_is_saved_and_marked_in_the_benefit_table(qapp, conn):
    dlg = make_dialog(conn)
    person_id = person_with_history(dlg)

    claim = dlg.people_table.cellWidget(0, 5)
    index = claim.findData(70 * 12)
    assert index > 0
    claim.setCurrentIndex(index)

    assert retirement.get_person(conn, person_id)["planned_claim_age_months"] == 840
    planned = [r for r in dlg.benefit_rows() if r["planned"]]
    assert [r["label"] for r in planned] == ["70"]
    assert "(your plan)" in dlg.benefit_table.item(2, 0).text()


def test_a_claim_age_outside_the_standard_three_gets_its_own_row(qapp, conn):
    dlg = make_dialog(conn)
    person_id = person_with_history(dlg)
    retirement.update_person(conn, person_id, planned_claim_age_months=65 * 12)
    dlg.reload()

    rows = dlg.benefit_rows()
    assert len(rows) == 4
    planned = [r for r in rows if r["planned"]]
    assert len(planned) == 1 and planned[0]["claim_age_months"] == 780
    # Sorted by age, so the extra row lands between 62 and full retirement age.
    assert [r["claim_age_months"] for r in rows] == sorted(
        r["claim_age_months"] for r in rows
    )


def test_a_birth_year_relabels_the_claim_ages_without_losing_the_editor(qapp, conn):
    """Changing the birth year moves full retirement age, which is a LABEL.

    The combo is rebuilt from inside the spin box's own ``valueChanged``, so
    this also exercises the rule that only the claim cell may be rebuilt there
    -- rebuilding the row would delete the spin box mid-signal.
    """
    dlg = make_dialog(conn)
    dlg.on_add_person()
    spin = dlg.people_table.cellWidget(0, 3)
    spin.setValue(1955)
    labels = [
        dlg.people_table.cellWidget(0, 5).itemText(i)
        for i in range(dlg.people_table.cellWidget(0, 5).count())
    ]
    assert any("and 2mo (full retirement age)" in t for t in labels)   # 66 and 2mo

    spin.setValue(1964)                       # the same spin box is still alive
    labels = [
        dlg.people_table.cellWidget(0, 5).itemText(i)
        for i in range(dlg.people_table.cellWidget(0, 5).count())
    ]
    assert "67 (full retirement age)" in labels


def test_removing_a_person_goes_through_the_confirm_seam(qapp, conn):
    dlg = make_dialog(conn)
    person_id = person_with_history(dlg)

    dlg._confirm = lambda title, text: False
    dlg.on_remove_person()
    assert retirement.get_person(conn, person_id) is not None

    dlg._confirm = lambda title, text: True
    dlg.on_remove_person()
    assert retirement.get_person(conn, person_id) is None
    assert dlg.benefit_rows() == []


# ---------------------------------------------------------------------------
# the earnings history: which basis is in force, stated inline
# ---------------------------------------------------------------------------
def seed_wages(conn, years=(2023, 2024), cents=5_000_000):
    """A synthetic ledger with wage deposits the estimator can find."""
    account = ledger.create_account(conn, "Everyday Checking", "checking")
    category = ledger.resolve_category(conn, "Salary")
    for year in years:
        ledger.add_transaction(
            conn, account, "%d-03-15" % year, cents, category_id=category
        )
    return account, category


def test_the_basis_in_force_is_stated_above_the_table(qapp, conn):
    dlg = make_dialog(conn)
    person_with_history(dlg, first=2020, last=2022)
    assert "from your SSA Earnings Report" in dlg.basis_label.text()
    assert "2020-2022" in dlg.basis_label.text()

    seed_wages(conn)
    dlg.on_estimate()
    basis = dlg.basis_label.text()
    assert "from your SSA Earnings Report" in basis        # the typed years
    assert "estimated by Mammon from this ledger" in basis  # and the new ones


def test_an_estimate_prints_what_it_assumed(qapp, conn):
    dlg = make_dialog(conn)
    dlg.on_add_person()
    dlg.people_table.cellWidget(0, 3).setValue(BIRTH_YEAR)
    seed_wages(conn)

    estimate = dlg.on_estimate()
    assert estimate.rows == {2023: 5_000_000, 2024: 5_000_000}
    assert estimate.category_names == ("Salary",)
    notes = dlg.earnings_notes.text()
    assert "Summed from deposits categorized Salary" in notes
    assert "gross pay as it landed in the ledger" in notes
    assert "Estimated 2 years from this ledger." in dlg.notice.text()
    # An estimated year still produces a benefit, with its assumptions shown.
    assert dlg.benefit_rows()[0]["cents"] > 0
    assert "Summed from deposits" in dlg.benefit_notes.text()


def test_an_estimate_with_no_wage_category_changes_nothing_and_says_so(qapp, conn):
    dlg = make_dialog(conn)
    person_id = person_with_history(dlg, first=2020, last=2021)
    estimate = dlg.on_estimate()
    assert estimate.rows == {}
    assert "No income category here looks like wages" in dlg.notice.text()
    assert len(retirement.list_earnings(conn, person_id)) == 2


def test_an_estimate_leaves_a_year_the_user_typed_alone(qapp, conn):
    dlg = make_dialog(conn)
    person_id = dlg.on_add_person()
    dlg.people_table.cellWidget(0, 3).setValue(BIRTH_YEAR)
    dlg.on_add_year(2023, 9_900_000)          # off the SSA statement
    seed_wages(conn)

    dlg.on_estimate()
    held = {int(r["year"]): dict(r) for r in retirement.list_earnings(conn, person_id)}
    assert held[2023]["earnings_cents"] == 9_900_000
    assert held[2023]["source"] == "reported"
    assert held[2024]["earnings_cents"] == 5_000_000
    assert held[2024]["source"] == "estimated"
    assert "1 year left as you entered them." in dlg.notice.text()


def test_typing_over_an_estimated_year_promotes_it_to_reported(qapp, conn):
    dlg = make_dialog(conn)
    person_id = dlg.on_add_person()
    dlg.people_table.cellWidget(0, 3).setValue(BIRTH_YEAR)
    seed_wages(conn)
    dlg.on_estimate()

    row = [i for i, r in enumerate(dlg._earnings) if int(r["year"]) == 2024][0]
    dlg.earnings_table.item(row, 1).setText("70,000.00")

    held = {int(r["year"]): dict(r) for r in retirement.list_earnings(conn, person_id)}
    assert held[2024]["earnings_cents"] == 7_000_000
    assert held[2024]["source"] == "reported"
    # The combo beside it says so too, without the table having been rebuilt.
    assert dlg.earnings_table.cellWidget(row, 2).currentData() == "reported"
    # And the correction now survives a re-estimate.
    dlg.on_estimate()
    assert retirement.earnings_map(conn, person_id)[2024] == 7_000_000


def test_a_projected_year_keeps_its_source_when_edited(qapp, conn):
    dlg = make_dialog(conn)
    person_id = dlg.on_add_person()
    dlg.people_table.cellWidget(0, 3).setValue(BIRTH_YEAR)
    dlg.on_add_year(2026, 6_000_000, "projected")

    dlg.earnings_table.item(0, 1).setText("65,000.00")
    held = dict(retirement.list_earnings(conn, person_id)[0])
    assert held["earnings_cents"] == 6_500_000
    assert held["source"] == "projected"
    assert "projected by you" in dlg.basis_label.text()


def test_the_source_combo_writes_through_retirement(qapp, conn):
    dlg = make_dialog(conn)
    person_id = dlg.on_add_person()
    dlg.people_table.cellWidget(0, 3).setValue(BIRTH_YEAR)
    dlg.on_add_year(2026, 6_000_000)

    combo = dlg.earnings_table.cellWidget(0, 2)
    combo.setCurrentIndex(list(retirement.EARNINGS_SOURCES).index("projected"))
    assert dict(retirement.list_earnings(conn, person_id)[0])["source"] == "projected"
    assert "projected by you" in dlg.basis_label.text()


def test_removing_a_year_goes_through_the_confirm_seam(qapp, conn):
    dlg = make_dialog(conn)
    person_id = person_with_history(dlg, first=2020, last=2022)
    dlg.earnings_table.selectRow(1)

    dlg._confirm = lambda title, text: False
    dlg.on_remove_year()
    assert len(retirement.list_earnings(conn, person_id)) == 3

    dlg._confirm = lambda title, text: True
    dlg.on_remove_year()
    years = [int(r["year"]) for r in retirement.list_earnings(conn, person_id)]
    assert years == [2020, 2022]


def test_an_amount_is_read_as_cents_and_kept_positive(qapp, conn):
    dlg = make_dialog(conn)
    person_id = dlg.on_add_person()
    dlg.on_add_year(2024, 0)
    # SSA reports earnings, never a signed ledger amount: a stray minus sign is
    # a typo, not a negative wage.
    dlg.earnings_table.item(0, 1).setText("-1,234.56")
    assert retirement.earnings_map(conn, person_id) == {2024: 123_456}


def test_a_year_can_be_retyped_and_the_table_reorders(qapp, conn):
    dlg = make_dialog(conn)
    person_id = person_with_history(dlg, first=2020, last=2021)
    dlg.earnings_table.item(1, 0).setText("2014")
    # The rebuild is deferred off the editor; run the event loop to let it land.
    qapp.processEvents()

    years = [int(r["year"]) for r in retirement.list_earnings(conn, person_id)]
    assert years == [2014, 2020]
    assert [dlg.earnings_table.item(r, 0).text() for r in range(2)] == ["2014", "2020"]


def test_a_year_that_is_not_a_year_is_refused_without_a_modal(qapp, conn):
    dlg = make_dialog(conn)
    person_id = person_with_history(dlg, first=2020, last=2020)
    dlg.earnings_table.item(0, 0).setText("last year")
    qapp.processEvents()

    assert "A year is four digits" in dlg.notice.text()
    assert [int(r["year"]) for r in retirement.list_earnings(conn, person_id)] == [2020]
    assert dlg.earnings_table.item(0, 0).text() == "2020"


def test_the_statement_date_is_stored_as_iso_and_shown_as_a_preference(qapp, conn):
    from mammon.ui.delegates import date_edit_iso
    from mammon.ui.models import fmt_date

    dlg = make_dialog(conn)
    person_id = person_with_history(dlg, first=2020, last=2021)
    dlg.statement_date.setDate(_qdate("2026-02-14"))

    assert retirement.get_person(conn, person_id)["ss_statement_date"] == "2026-02-14"
    assert date_edit_iso(dlg.statement_date) == "2026-02-14"
    assert fmt_date("2026-02-14") in dlg.benefit_notes.text()

    # It survives a reload, still ISO in storage.
    dlg.reload()
    assert date_edit_iso(dlg.statement_date) == "2026-02-14"


def _qdate(iso):
    from PyQt5.QtCore import QDate

    return QDate.fromString(iso, "yyyy-MM-dd")


# ---------------------------------------------------------------------------
# provenance lives in the FAQ, not on the benefit
# ---------------------------------------------------------------------------
def test_the_benefit_table_carries_no_citation_and_no_staleness_mark(qapp, conn):
    """An old-but-correct published table is not missing or conflicting data.

    The bend points and PIA factors used to be named on every benefit row, with
    an amber triangle once an indexed table was an edition behind. Both moved to
    the Retirement FAQ (SRD 5.8o); a date far enough out that every indexed
    table IS behind must still produce a clean table here.
    """
    fresh = make_dialog(conn, today=TODAY)
    person_with_history(fresh)
    assert fresh.benefit_table.columnCount() == 2
    assert all("stale" not in row for row in fresh.benefit_rows())

    later = make_dialog(conn, today=_dt.date(2030, 3, 1))
    assert later.benefit_table.rowCount()
    for row in range(later.benefit_table.rowCount()):
        for col in range(later.benefit_table.columnCount()):
            assert later.benefit_table.item(row, col).icon().isNull()
    notes = later.benefit_notes.text()
    assert "Check for a newer table" not in notes
    # The INPUT assumptions the user can correct do stay (SRD 5.8m).
    assert "Average indexed monthly earnings" in notes


def test_prior_year_magi_is_saved_by_year(qapp, conn):
    """Audit: IRMAA in the plan's first two years is set by returns from
    before the plan, typed here."""
    dlg = make_dialog(conn)
    year = dlg._today.year - 2
    assert year in dlg.prior_magi_edits and (year + 1) in dlg.prior_magi_edits
    dlg.prior_magi_edits[year].setText("120,000")
    dlg._save_assumptions()
    assert retirement.get_prior_magi(conn, year) == 120_000_00
    dlg.prior_magi_edits[year].setText("0")
    dlg._save_assumptions()
    assert retirement.get_prior_magi(conn, year) is None


def test_part_d_and_the_three_household_flags_write_through(qapp, conn):
    from PyQt5.QtCore import Qt

    dlg = make_dialog(conn)
    pid = person_with_history(dlg)
    assert retirement.get_person(conn, pid)["part_d"] == 1
    dlg.people_table.item(0, 6).setCheckState(Qt.Unchecked)
    assert retirement.get_person(conn, pid)["part_d"] == 0
    dlg.defer_rmd_check.setChecked(True)
    dlg.community_check.setChecked(True)
    dlg.state_retirement_check.setChecked(True)
    assert retirement.get_defer_first_rmd(conn)
    assert retirement.get_community_property(conn)
    assert retirement.get_state_excludes_retirement(conn)
    # The "62" row is 62 and one month for someone not born on the first.
    rows = dlg.benefit_rows()
    earnings = retirement.earnings_map(conn, pid)
    assert rows[0]["cents"] == retirement.monthly_benefit(earnings, BIRTH_YEAR, 62 * 12 + 1)
