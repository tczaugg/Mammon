"""The Retirement FAQ: the one place the rules and their provenance are read.

Two things are pinned here, and they are two halves of one requirement (SRD
5.8o).

The FAQ must be COMPLETE. It answers the household's own questions as rule
exposition with both sides named, and its last table carries one row per rule
table in :mod:`mammon.retirement`. That last assertion enumerates the tables
from the domain module rather than listing them here on purpose: a table added
to ``retirement.py`` and forgotten in the FAQ has to fail something, and this is
the something.

The screens must be CLEAN. Provenance used to print beside the figures - the
edition of every published table behind a benefit, with an amber triangle once
an indexed table was a year behind. It was unreadable at the point of use, and
the triangle means MISSING OR CONFLICTING DATA everywhere else in Mammon, which
an old-but-correct published table is not. So the planner page and both
retirement dialogs are swept for a publisher, a source or a staleness mark, and
for any icon at all.

Everything constructed here is synthetic: an "ANON" household, round balances,
a flat wage history.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import datetime as _dt

import pytest

from mammon import ledger, rebalance, retirement
from mammon.tests import fresh_db
from mammon.ui import retirement_faq as faq

TODAY = _dt.date(2026, 9, 23)
BIRTH_YEAR = 1955          # applicable age 73, so every rule below is live
WAY_LATER = _dt.date(2031, 6, 1)   # every indexed table is behind by here


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "faq.db")
    yield c
    c.close()


@pytest.fixture
def household(conn):
    """One deferred account, one Roth, one owner, one planned draw."""
    ira = ledger.create_account(conn, "ANON Rollover IRA", "investment")
    roth = ledger.create_account(conn, "ANON Roth IRA", "investment")
    rebalance.set_account_treatment(conn, ira, "deferred")
    rebalance.set_account_treatment(conn, roth, "roth")
    ledger.add_transaction(conn, ira, "2024-01-05", 100_000_000)
    ledger.add_transaction(conn, roth, "2024-01-05", 20_000_000)
    person = retirement.add_person(
        conn, "ANON Saver", "self", birth_year=BIRTH_YEAR, birth_month=6,
        planned_claim_age_months=67 * 12)
    for year in range(1990, 2026):
        retirement.set_earnings(conn, person, year, 6_000_000)
    retirement.set_withdrawal(conn, ira, 2029, 4_000_000)
    retirement.set_conversion(conn, ira, roth, 2028, 2_500_000)
    return {"ira": ira, "roth": roth, "person": person}


@pytest.fixture
def window(qapp):
    w = faq.RetirementFaqWindow(today=TODAY)
    yield w
    w.deleteLater()


# ---------------------------------------------------------------------------
# the sheet itself
# ---------------------------------------------------------------------------
def test_the_faq_builds_headlessly_and_is_not_a_modal(window):
    """A reader keeps it open beside the planner, so it is a plain window.

    Also the headless-modal hazard (CLAUDE.md): an ``exec_()``-ed dialog here
    would hang every test that opened it under the offscreen platform.
    """
    from PyQt5.QtWidgets import QDialog

    assert not isinstance(window, QDialog)
    assert window.windowTitle() == faq.WINDOW_TITLE
    window.show()
    assert window.isVisible()
    window.close()


def test_every_question_the_household_asked_has_its_own_section(window):
    text = window.text()
    for section in faq.SECTIONS:
        assert section.question in text, section.key
        # A question with no answer under it is worse than no question.
        assert section.rules, section.key
        for paragraph in section.rules:
            assert paragraph in text, section.key


def test_the_questions_cover_what_was_asked():
    """The keys are pinned so a section cannot quietly be dropped."""
    assert {s.key for s in faq.SECTIONS} == {
        "medicare", "rmd", "conversion", "ordering", "rollover",
        "consolidation", "trust", "will", "life_insurance", "working",
        "shortfall",
    }


def test_consolidating_several_plans_shows_the_single_rmd_gain(window):
    """The reader asked why one IRA is simpler; the answer is one RMD.

    Both columns are required: the same move gives up the still-working
    deferral, ERISA's protection, plan loans, NUA and a clean pro-rata
    position, and an entry that printed only the gain would be counsel.
    """
    section = next(s for s in faq.SECTIONS if s.key == "consolidation")
    text = window.text()

    assert section.question in text
    assert "rolling several 401(k)s into one IRA" in section.question

    # Both sides render, under labels that name the choice, not a verdict.
    assert section.for_points and section.against_points
    assert section.for_label in text and section.against_label in text
    for point in section.for_points + section.against_points:
        assert point in text, point[:60]

    # The gain the reader noticed was missing: one RMD, not one per plan.
    one_rmd = next(p for p in section.for_points
                   if "instead of one per plan" in p)
    assert "1.401(a)(9)-1(a)(2)" in one_rmd   # plans cannot be aggregated
    assert "1.408-8(e)(1)(i)" in one_rmd      # IRAs can be

    # The cons the same move carries, each with its governing rule.
    against = " ".join(section.against_points)
    for cite in ("401(a)(9)(C)(i)(II)",      # still-working deferral
                 "29 U.S.C. 1056(d)(1)",     # ERISA anti-alienation
                 "72(p)",                    # plan loans
                 "72(t)(2)(A)(v)",           # separation at 55
                 "402(e)(4)(B)",             # net unrealized appreciation
                 "408(d)(2)"):               # pro-rata on a later conversion
        assert cite in against, cite


def test_a_choice_shows_both_sides_and_never_picks_one(window):
    """Pros and cons of the CHOICE, not of a preferred answer."""
    for section in faq.SECTIONS:
        if section.for_points or section.against_points:
            assert section.for_points and section.against_points, section.key
            assert section.for_label and section.against_label, section.key
            # Columns are labeled for what each side gains, not "Pros"/"Cons".
            for label in (section.for_label, section.against_label):
                assert label.lower() not in ("pros", "cons"), section.key

    text = window.text().lower()
    for word in ("we recommend", "you should", "the best option",
                 "mammon suggests"):
        assert word not in text


# ---------------------------------------------------------------------------
# the provenance table: the ONLY place provenance is rendered
# ---------------------------------------------------------------------------
def test_the_table_names_every_rule_table_in_the_domain_module(window):
    """Enumerated from ``retirement``, so a new table cannot be omitted."""
    found = {
        name for name in dir(retirement)
        if not name.startswith("__")
        and isinstance(getattr(retirement, name), retirement.RuleTable)
    }
    assert found, "no RuleTable found - the scan itself is broken"
    assert {name for name, _ in faq.rule_tables()} == found

    text = window.text()
    for name in sorted(found):
        record = getattr(retirement, name).provenance
        assert record.table in text, name
        assert record.publisher in text, name
        assert str(record.effective_year) in text, name


def test_each_row_says_when_it_was_last_checked_without_crying_wolf():
    """A stable table says it cannot change; an indexed one dates itself."""
    rows = {r["attribute"]: r for r in faq.provenance_rows(TODAY)}
    for name, table in faq.rule_tables():
        note = rows[name]["checked"]
        assert note
        if table.provenance.volatility == "stable":
            assert "does not change" in note, name
        else:
            assert ("Last checked" in note or "No check on record" in note), name

    # Far enough out that every indexed table really is behind, the wording
    # stays a note about editions - never a warning, and never an icon.
    later = {r["attribute"]: r for r in faq.provenance_rows(WAY_LATER)}
    stale = [n for n, t in faq.rule_tables()
             if t.provenance.is_stale(WAY_LATER)]
    assert stale, "no table ages - the fixture date is wrong"
    for name in stale:
        assert "may be out of date" in later[name]["checked"], name


def test_the_source_is_reachable_from_the_sheet(window):
    """Every published table links to where the current figure lives."""
    html = faq.faq_html(TODAY)
    for _name, table in faq.rule_tables():
        source = table.provenance.source
        assert source
        if source.startswith("http"):
            assert f'href="{source}"' in html, source


# ---------------------------------------------------------------------------
# the screens: no provenance, no staleness mark
# ---------------------------------------------------------------------------
def _forbidden_strings():
    """Every publisher and source, plus the staleness wording that went away."""
    banned = {
        "Tables:", "Last checked", "may be out of date",
        "Check for a newer table", "behind the current year",
        "worth re-checking",
    }
    for _name, table in faq.rule_tables():
        record = table.provenance
        banned.add(record.source)
        if len(record.publisher) > 10:
            banned.add(record.publisher)
    return banned


def _rendered(widget):
    """Label text and table-cell text, plus whether anything carries an icon."""
    from PyQt5.QtWidgets import QLabel, QTableWidget

    texts = []
    icons = []
    for label in widget.findChildren(QLabel):
        texts.append(label.text())
        pix = label.pixmap()
        if pix is not None and not pix.isNull():
            icons.append(label.objectName() or "a label")
    for table in widget.findChildren(QTableWidget):
        for row in range(table.rowCount()):
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item is None:
                    continue
                texts.append(item.text())
                if not item.icon().isNull():
                    icons.append(f"cell {row},{col}")
    return "\n".join(texts), icons


def _assert_clean(widget, what):
    text, icons = _rendered(widget)
    assert not icons, f"{what} still marks a figure with an icon: {icons}"
    for phrase in _forbidden_strings():
        assert phrase not in text, f"{what} still prints provenance: {phrase}"


def test_the_planner_page_shows_no_provenance_and_no_triangle(qapp, conn,
                                                              household):
    from mammon.ui.retirement_planner import RetirementPlannerPage

    page = RetirementPlannerPage(conn, today=WAY_LATER)
    try:
        page.refresh()
        _assert_clean(page, "the planner page")
        # What it does still print is the mechanism behind the chart.
        assert "where each dollar comes from" in page.income_caption.text()
    finally:
        page.deleteLater()


def test_the_social_security_dialog_shows_no_provenance_and_no_triangle(
        qapp, conn, household):
    from mammon.ui.social_security_dialog import SocialSecurityDialog

    dlg = SocialSecurityDialog(conn, today=WAY_LATER)
    try:
        _assert_clean(dlg, "the Social Security dialog")
        # The INPUT assumptions a user can correct do survive (SRD 5.8m).
        assert "Average indexed monthly earnings" in dlg.benefit_notes.text()
    finally:
        dlg.deleteLater()


def test_the_roth_section_shows_no_provenance_and_no_triangle(qapp, conn,
                                                              household):
    """The conversion schedule is part of the page now (SRD 5.8n), not a dialog,
    and the same rule binds it: no table name, no staleness, no triangle.
    """
    from mammon.ui.retirement_planner import RetirementPlannerPage

    page = RetirementPlannerPage(conn, today=WAY_LATER)
    try:
        page.refresh()
        year = page.schedule.years_shown()[0]
        page.show_schedule_for(year)
        assert page.schedule.focused_year() == year
        _assert_clean(page.schedule, "the conversion schedule")
        # The rule that raises a floored cell is a mechanism, and it stays.
        assert "Uniform Lifetime Table" in page.schedule.floor_citation(
            household["ira"], year)
    finally:
        page.deleteLater()


def test_the_domain_layer_keeps_its_provenance_records():
    """Only the RENDERING moved. The data is still on every rule table."""
    for name, table in faq.rule_tables():
        record = table.provenance
        assert record.table and record.publisher and record.source, name
        assert record.effective_year > 1900, name
        assert hasattr(record, "is_stale")
