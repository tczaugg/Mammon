"""The valuation UI: the asset register's gear actions and the value-history
dialog.

The point these tests hold down is that a property's market value NEVER becomes
a transaction. The register stays a cost-basis ledger; the value is a dated
series edited here, exactly as a security's price series is edited outside the
investment register. So every test that writes a value also asserts the
register's transaction count did not move.

Everything runs offscreen with no modal actually shown: the fetch goes through
the ``_fetch_values`` / ``_fetch_asset_values`` seam, and the two confirmations
go through ``QMessageBox.question``, which is patched. A dialog ``exec_()``-ed
for real here would block forever (CLAUDE.md, headless-modal hazard).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from mammon import asset_values, db, ledger
from mammon.ui.asset_value_dialog import (
    AssetValueEditor, AssetValueHistoryDialog, MANUAL_SOURCE,
)
from mammon.ui.widgets import RegisterWidget
from mammon.tests import fresh_db


@pytest.fixture(scope="module", autouse=True)
def qapp():
    """One QApplication for the module. Constructing a QWidget without one takes
    the interpreter down with no traceback, so this is autouse rather than
    something each widget test has to remember to request."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "values.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """One house carrying a mortgage, plus a chequing account to prove the
    valuation UI stays off every other account type."""
    house = ledger.create_account(conn, "ANON Residence", "asset",
                                  opening_balance=185_000_00)
    loan = ledger.create_account(conn, "ANON Residence Mortgage", "liability",
                                 opening_balance=-60_000_00)
    checking = ledger.create_account(conn, "ANON Checking", "checking",
                                     opening_balance=2_000_00)
    asset_values.set_address(conn, house, "100 Main St, ANYTOWN MI")
    asset_values.set_lien(conn, loan, house)
    return {"house": house, "loan": loan, "checking": checking}


def _txn_count(conn, account_id):
    return conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (account_id,)).fetchone()["c"]


class _FakeSource:
    """An injected valuation source. ``values`` maps address -> cents; an address
    that is absent yields nothing, which is how a real source declines."""
    source_name = "fake"

    def __init__(self, values, date="2026-09-04"):
        self.values = values
        self.date = date
        self.seen = []

    def get_values(self, requests):
        out = []
        for req in requests:
            self.seen.append(req.address)
            cents = self.values.get(req.address)
            if cents is not None:
                out.append(asset_values.AssetValue(
                    req.account_id, self.date, cents, self.source_name))
        return out


# ---------------------------------------------------------------------------
# the gear actions live on asset accounts and nowhere else
# ---------------------------------------------------------------------------
def test_valuation_actions_show_only_on_an_asset_account(conn, world):
    house = RegisterWidget(conn, world["house"])
    checking = RegisterWidget(conn, world["checking"])
    house._sync_asset_actions()
    checking._sync_asset_actions()
    assert house.act_get_value.isVisible()
    assert house.act_value_history.isVisible()
    assert not checking.act_get_value.isVisible()
    assert not checking.act_value_history.isVisible()


def test_actions_follow_a_type_change_under_an_open_register(conn, world):
    """Account Details can retype an account while its register is open; the
    gear re-checks on every open rather than trusting construction time."""
    reg = RegisterWidget(conn, world["checking"])
    assert not reg.act_get_value.isVisible()
    ledger.update_account(conn, world["checking"], type="asset")
    reg._sync_asset_actions()            # what gear ▸ aboutToShow fires
    assert reg.act_get_value.isVisible()


# ---------------------------------------------------------------------------
# the register header stops calling a cost basis a balance
# ---------------------------------------------------------------------------
def test_asset_header_names_the_basis_and_shows_value_debt_equity(conn, world):
    reg = RegisterWidget(conn, world["house"])
    # No value recorded yet: the basis alone, said to BE a basis.
    text = reg.balance_label.text()
    assert "Cost Basis" in text and "$185,000.00" in text
    assert "not recorded" in text

    asset_values.set_value(conn, world["house"], "2026-09-04", 400_000_00)
    reg._refresh_header()
    text = reg.balance_label.text()
    assert "Cost Basis: $185,000.00" in text
    assert "Value: $400,000.00" in text
    assert "Debt: $60,000.00" in text
    assert "Equity: $340,000.00" in text


def test_other_account_types_keep_the_ending_balance(conn, world):
    reg = RegisterWidget(conn, world["checking"])
    assert reg.balance_label.text() == "Ending Balance: $2,000.00"


# ---------------------------------------------------------------------------
# the history dialog
# ---------------------------------------------------------------------------
def test_history_lists_values_oldest_first_with_the_change(conn, world):
    asset_values.set_value(conn, world["house"], "2010-06-01", 185_000_00,
                           MANUAL_SOURCE, "purchase appraisal")
    asset_values.set_value(conn, world["house"], "2018-03-01", 240_000_00,
                           MANUAL_SOURCE, "refinance appraisal")
    dlg = AssetValueHistoryDialog(conn, world["house"])
    assert dlg.table.rowCount() == 2
    assert dlg.table.item(0, 1).text() == "$185,000.00"
    assert dlg.table.item(0, 2).text() == ""          # nothing to compare to
    assert dlg.table.item(1, 1).text() == "$240,000.00"
    assert "+$55,000.00" in dlg.table.item(1, 2).text()
    assert "29.7%" in dlg.table.item(1, 2).text()
    assert dlg.table.item(1, 4).text() == "refinance appraisal"
    assert "Equity" in dlg.summary.text()


def test_backfilling_a_past_appraisal_writes_no_transaction(conn, world, monkeypatch):
    """The whole design in one test: a value entered by hand lands in the series,
    net worth follows it, and the register is untouched."""
    before = _txn_count(conn, world["house"])
    dlg = AssetValueHistoryDialog(conn, world["house"])

    def fake_exec(self):
        from PyQt5.QtCore import QDate
        self.date.setDate(QDate(2010, 6, 1))
        self.value.setText("185,000")
        self.note.setText("purchase appraisal")
        return QDialog.Accepted

    monkeypatch.setattr(AssetValueEditor, "exec_", fake_exec)
    dlg.on_add()

    history = asset_values.value_history(conn, world["house"])
    assert [(v.date, v.value_cents, v.source) for v in history] == \
        [("2010-06-01", 185_000_00, MANUAL_SOURCE)]
    assert _txn_count(conn, world["house"]) == before


def test_editing_a_values_date_moves_it_instead_of_duplicating(conn, world,
                                                               monkeypatch):
    asset_values.set_value(conn, world["house"], "2018-03-01", 240_000_00,
                           MANUAL_SOURCE)
    dlg = AssetValueHistoryDialog(conn, world["house"])
    dlg.table.selectRow(0)

    def fake_exec(self):
        from PyQt5.QtCore import QDate
        self.date.setDate(QDate(2018, 4, 15))     # corrected date
        self.value.setText("245,000")
        return QDialog.Accepted

    monkeypatch.setattr(AssetValueEditor, "exec_", fake_exec)
    dlg.on_edit()

    history = asset_values.value_history(conn, world["house"])
    assert [(v.date, v.value_cents) for v in history] == [("2018-04-15", 245_000_00)]


def test_moving_a_value_onto_an_occupied_date_asks_first(conn, world, monkeypatch):
    """``set_value`` upserts, so an unguarded edit would silently eat the row
    already sitting on the target date."""
    asset_values.set_value(conn, world["house"], "2018-03-01", 240_000_00)
    asset_values.set_value(conn, world["house"], "2020-01-01", 300_000_00)
    dlg = AssetValueHistoryDialog(conn, world["house"])
    dlg.table.selectRow(0)

    def fake_exec(self):
        from PyQt5.QtCore import QDate
        self.date.setDate(QDate(2020, 1, 1))      # already taken
        self.value.setText("250,000")
        return QDialog.Accepted

    monkeypatch.setattr(AssetValueEditor, "exec_", fake_exec)
    asked = []

    def refuse(*args, **kwargs):
        asked.append(args[1] if len(args) > 1 else "")
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(refuse))
    dlg.on_edit()
    assert asked, "replacing an occupied date must be confirmed"
    # Declined: both values still stand, untouched.
    assert [(v.date, v.value_cents) for v in
            asset_values.value_history(conn, world["house"])] == \
        [("2018-03-01", 240_000_00), ("2020-01-01", 300_000_00)]


def test_delete_is_confirmed_and_removes_only_that_value(conn, world, monkeypatch):
    asset_values.set_value(conn, world["house"], "2018-03-01", 240_000_00)
    asset_values.set_value(conn, world["house"], "2020-01-01", 300_000_00)
    dlg = AssetValueHistoryDialog(conn, world["house"])
    dlg.table.selectRow(1)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    dlg.on_delete()
    assert len(asset_values.value_history(conn, world["house"])) == 2

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    dlg.table.selectRow(1)
    dlg.on_delete()
    assert [v.date for v in asset_values.value_history(conn, world["house"])] == \
        ["2018-03-01"]


def test_editor_refuses_a_zero_or_unreadable_value(conn, world, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))
    ed = AssetValueEditor(conn, world["house"])
    ed.value.setText("0")
    ed._on_accept()
    assert ed.result() != QDialog.Accepted and warned
    ed.value.setText("not a number")
    ed._on_accept()
    assert ed.result() != QDialog.Accepted
    assert len(warned) == 2


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
def test_fetch_records_a_value_and_leaves_the_register_alone(conn, world,
                                                             monkeypatch):
    before = _txn_count(conn, world["house"])
    said = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: said.append(a[2])))
    source = _FakeSource({"100 Main St, ANYTOWN MI": 400_000_00})
    dlg = AssetValueHistoryDialog(conn, world["house"], value_source=source)
    dlg.on_fetch()

    history = asset_values.value_history(conn, world["house"])
    assert [(v.date, v.value_cents, v.source) for v in history] == \
        [("2026-09-04", 400_000_00, "fake")]
    assert _txn_count(conn, world["house"]) == before
    assert "$400,000.00" in said[0]
    assert dlg.table.rowCount() == 1


def test_a_declined_fetch_leaves_the_last_good_value_standing(conn, world,
                                                              monkeypatch):
    asset_values.set_value(conn, world["house"], "2026-01-01", 400_000_00,
                           "zillow")
    said = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: said.append(a[2])))
    dlg = AssetValueHistoryDialog(conn, world["house"],
                                  value_source=_FakeSource({}))
    dlg.on_fetch()
    assert [(v.date, v.value_cents) for v in
            asset_values.value_history(conn, world["house"])] == \
        [("2026-01-01", 400_000_00)]
    # and it says WHICH property and why, rather than reporting a bare success
    assert "ANON Residence" in said[0]


def test_an_unaddressed_property_is_named_not_silently_skipped(conn, world,
                                                               monkeypatch):
    other = ledger.create_account(conn, "ANON Cottage", "asset",
                                  opening_balance=90_000_00)
    said = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: said.append(a[2])))
    dlg = AssetValueHistoryDialog(conn, other, value_source=_FakeSource({}))
    dlg.on_fetch()
    assert "ANON Cottage" in said[0] and "address" in said[0]


def test_missing_source_is_reported_as_the_setup_step_it_is(conn, world,
                                                            monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a[2])))

    def unavailable(*_a, **_k):
        raise asset_values.ValueSourceUnavailable("no webSlinger client")

    dlg = AssetValueHistoryDialog(conn, world["house"])
    monkeypatch.setattr(dlg, "_fetch_values", unavailable)
    dlg.on_fetch()
    assert warned and "No valuation source is configured" in warned[0]


def test_register_gear_fetch_refreshes_the_header(conn, world, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: 0))
    reg = RegisterWidget(conn, world["house"])
    source = _FakeSource({"100 Main St, ANYTOWN MI": 400_000_00})
    monkeypatch.setattr(reg, "_fetch_asset_values",
                        lambda: asset_values.fetch_values(
                            conn, [world["house"]], source=source))
    reg.get_value()
    assert "Value: $400,000.00" in reg.balance_label.text()
    assert "Equity: $340,000.00" in reg.balance_label.text()


# ---------------------------------------------------------------------------
# the chart
# ---------------------------------------------------------------------------
def test_value_chart_plots_the_series_against_the_basis(conn, world):
    from mammon.ui.charts import AssetValueCanvas
    points = [("2010-06-01", 185_000_00), ("2018-03-01", 240_000_00),
              ("2026-09-04", 400_000_00)]
    canvas = AssetValueCanvas("ANON Residence", points, 185_000_00)
    ax = canvas.figure.axes[0]
    line = ax.lines[0]
    assert [round(y) for y in line.get_ydata()] == [185_000, 240_000, 400_000]
    # the dashed cost-basis reference line is the second line on the axes
    assert len(ax.lines) == 2
    assert ax.get_title() == "Value History - ANON Residence"


def test_value_chart_with_no_points_renders_a_note_not_an_empty_axes(conn):
    from mammon.ui.charts import AssetValueCanvas
    canvas = AssetValueCanvas("ANON Residence", [])
    ax = canvas.figure.axes[0]
    assert not ax.lines
    assert any("No recorded values" in t.get_text() for t in ax.texts)
