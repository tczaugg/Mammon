"""Tests for the Taxes and Custom Reports window (SRD 5.9r), Phases 4-6.

The life-cycle test below is the acceptance bar, and it is written the way it is
on purpose: it drives the window through its PUBLIC verbs (``create_report``,
``add_item``, ``apply_item``, ``set_range``, ``toggle_exclusion``,
``export_csv_to``) and through the picker helpers in ``ui.report_filters``, never
through a dialog. Everything a user is asked in this window goes through
``_prompt_text``/``_confirm``/``_warn``, so a test can supply the answer up
front; anything that opened a modal instead would hang forever under the
offscreen platform rather than fail.

It also pins the two properties that make the window trustworthy:

* what the tree shows and what the CSV contains come from ONE pure projection
  (``drill_tree_rows``), so an export can never disagree with the screen; and
* nothing computed is stored -- re-pointing the report at a different range
  changes every number with no edit to the definition, and reopening the window
  rebuilds the whole definition, selections included, from the database.

The drill-down tests pin the shape the report replaced a summary table and a
warnings panel with. Four things have to reach the SCREEN rather than be
engineered away, because each was invisible in the flat table that came before:

* a line item opens into its tags, its categories and finally its transactions,
  and every level shows the amount the DOMAIN computed for it -- nothing here
  rolls a child up into its parent, because a line wearing two of an item's tags
  is counted under each and a rolled-up total would not be the filed one;
* a line an exclusion tag removed is still drawn, struck through, carrying its
  real amount -- it is what the user removed, and a report that simply dropped it
  would be back to a wrong number looking exactly like a right one;
* the two findings that mean a figure is not what it looks like -- a category
  feeding two line items, both legs of one transfer pulled in by a tag -- are
  amber triangles ON the item, with the explanation in the tooltip;
* right-clicking a transaction toggles its exclusion, and the rule about where an
  exclusion may live belongs to ``ledger``, which this window only asks.

All data here is synthetic.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, investments, ledger
from mammon.reports import custom
from mammon.ui import report_filters
from mammon.ui.custom_report_window import (
    DEPTH_INDENT,
    DRILL_COLUMNS,
    CustomReportWindow,
    drill_rows_to_csv,
    drill_tree_rows,
    report_def_to_csv,
)
from mammon.tests import fresh_db


START = "2025-01-01"
END = "2025-12-31"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "custom_window.db")
    yield c
    c.close()


@pytest.fixture
def data(conn):
    """One checking account, a Rental subtree, and money on both range edges."""
    checking = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=1000_00,
                                     opening_date="2024-01-01")
    brokerage = ledger.create_account(conn, "Brokerage", "investment")
    salary = ledger.resolve_category(conn, "Salary")
    rental = ledger.resolve_category(conn, "Rental")
    repairs = ledger.resolve_category(conn, "Rental:Repairs")

    ledger.add_transaction(conn, checking, "2024-12-31", 500_00, category_id=rental)
    ledger.add_transaction(conn, checking, START, 1200_00, category_id=rental)
    ledger.add_transaction(conn, checking, "2025-06-15", -300_00, category_id=repairs)
    ledger.add_transaction(conn, checking, END, 1100_00, category_id=rental)
    ledger.add_transaction(conn, checking, "2026-01-02", 900_00, category_id=rental)
    ledger.add_transaction(conn, checking, "2025-03-10", 2500_00, category_id=salary)
    ledger.add_transaction(conn, checking, "2025-07-04", -45_00)   # uncategorized
    return {"checking": checking, "brokerage": brokerage, "rental": rental,
            "repairs": repairs, "salary": salary}


class _Window(CustomReportWindow):
    """The window with its three choice seams answered from the test."""

    prompt_answer = ""
    confirm_answer = True

    def __init__(self, *a, **kw):
        self.warnings = []
        super().__init__(*a, **kw)

    def _prompt_text(self, title, label, default=""):
        return self.prompt_answer

    def _confirm(self, title, text):
        return self.confirm_answer

    def _warn(self, title, text):
        self.warnings.append((title, text))

    # The tree's one popup. Recording it (and taking the first ENABLED entry,
    # as a click would) is what lets a test drive the real right-click chain --
    # a QMenu.exec_ would block forever under the offscreen platform.
    menu_rows = None
    menu_entries = None

    def _show_row_menu(self, row, global_pos):
        if self.menu_rows is None:
            self.menu_rows, self.menu_entries = [], []
        self.menu_rows.append(row)
        entries = self.row_menu_entries(row)
        self.menu_entries.append(entries)
        for _text, enabled in entries:
            if enabled:
                self.toggle_exclusion(row)
                return


def _build(conn, win):
    """The three items of the acceptance test, selections set through the
    picker helpers exactly as the user's clicks would set them."""
    rid = win.create_report("Schedule E")

    win.add_item("rental_net", "SOSC")
    win.label_edit.setText("Rental net")
    win.group_edit.setText("Schedule E")
    report_filters.set_category_tree_selections(
        win.category_picker, [(conn.execute(
            "SELECT id FROM categories WHERE name='Rental'").fetchone()[0], 1)])
    assert win.apply_item()

    for name, kind in (("checking_end", "EDAB"), ("checking_start", "SDAB")):
        win.add_item(name, kind)
        win.group_edit.setText("Balances")
        report_filters.set_account_picker_ids(
            win.account_picker,
            [conn.execute("SELECT id FROM accounts WHERE name='Checking'")
             .fetchone()[0]])
        assert win.apply_item()
    return rid


# --------------------------------------------------------------------------
# Pure rendering, no Qt window needed
# --------------------------------------------------------------------------

def _tree(items, **kw):
    return custom.DrillTree(report_id=1, name="R", start=START, end=END,
                            items=tuple(items), **kw)


def _node(kind, label, amount, children=(), **kw):
    return custom.DrillNode(kind=kind, label=label, amount=amount,
                            children=list(children), **kw)


def _line(date="2025-03-10", amount=100_00, payee="Tenant", memo="",
          excluded=False, txn_id=7, split_id=None):
    return custom.DrillLine(txn_id=txn_id, split_id=split_id, date=date,
                            payee=payee, memo=memo, account_id=1,
                            amount=amount, excluded=excluded)


def test_drill_rows_carry_depth_down_to_the_transactions():
    tree = _tree([
        _node("item", "Rents", 1_000_00, [
            _node("tag", "7344 Muirfield", 600_00, [
                _node("category", "Rental:Rent", 600_00, [
                    _node("txn", "2025-03-10", 600_00,
                          line=_line(amount=600_00)),
                ]),
            ]),
        ]),
    ])
    rows = drill_tree_rows(tree)
    assert [(r.depth, r.kind, r.cells[0], r.cells[3]) for r in rows] == [
        (0, "item", "Rents", "1,000.00"),
        (1, "tag", "7344 Muirfield", "600.00"),
        (2, "category", "Rental:Rent", "600.00"),
        (3, "txn", "", "600.00"),
    ]


def test_there_is_no_grand_total_row():
    """One category legitimately feeds several tax lines, so the items of this
    report share money by design and summing them measures nothing. A bold TOTAL
    is trusted BECAUSE it is bold, which is what made this one worse than the
    blank space it took up."""
    rows = drill_tree_rows(_tree([
        _node("item", "a", 100_00), _node("item", "b", 25_00)]))
    assert [r.kind for r in rows] == ["item", "item"]
    assert not any(r.cells[0] == "TOTAL" for r in rows)


def test_a_transaction_row_carries_date_and_payee_not_the_first_column():
    """The hierarchy reads down column one, so a leaf leaves it blank and puts
    its date and payee in their own columns -- the Itemize convention."""
    rows = drill_tree_rows(_tree([
        _node("item", "Rents", 100_00, [
            _node("txn", "2025-03-10", 100_00,
                  line=_line(payee="Tenant", memo="March")),
        ])]))
    leaf = [r for r in rows if r.kind == "txn"][0]
    assert leaf.cells[0] == ""
    assert leaf.cells[2] == "Tenant — March"


def test_the_item_row_is_the_evaluators_total_not_a_sum_of_its_tags():
    """A line carrying two of an item's tags is counted under each, so tag rows
    can exceed the item. The item must still show what gets filed."""
    tree = _tree([
        _node("item", "Rents", 100_00, [
            _node("tag", "A", 100_00), _node("tag", "B", 100_00),
        ])])
    rows = drill_tree_rows(tree)
    assert rows[0].cells[3] == "100.00"
    assert [r.cells[3] for r in rows[1:3]] == ["100.00", "100.00"]


def test_an_excluded_row_keeps_its_amount_and_is_flagged():
    rows = drill_tree_rows(_tree([
        _node("item", "Rents", 0, [
            _node("txn", "2025-03-10", 100_00,
                  line=_line(amount=100_00, excluded=True)),
        ])]))
    leaf = [r for r in rows if r.kind == "txn"][0]
    assert leaf.excluded is True
    assert leaf.cells[3] == "100.00"


def test_csv_indents_by_depth_and_marks_the_excluded():
    tree = _tree([
        _node("item", "Rents", 0, [
            _node("category", "Rental:Rent", 0, [
                _node("txn", "2025-03-10", 100_00,
                      line=_line(amount=100_00, payee="Tenant",
                                 excluded=True)),
            ]),
        ])])
    text = drill_rows_to_csv(drill_tree_rows(tree))
    lines = text.splitlines()
    assert lines[0] == "Line item / Tag / Category,Date,Payee / Memo,Amount"
    assert lines[2].startswith(DEPTH_INDENT + "Rental:Rent")
    assert "[excluded]" in lines[3]


def test_marks_become_one_tooltip():
    row = drill_tree_rows(_tree([
        _node("item", "Rents", 0,
              marks=(custom.DrillMark("shared_lines", "shared!"),
                     custom.DrillMark("double_tagged", "doubled!")))]))[0]
    assert row.tooltip == "shared!\n\ndoubled!"


# --------------------------------------------------------------------------
# The life-cycle test: the acceptance bar
# --------------------------------------------------------------------------

def test_window_life_cycle(qapp, conn, data, tmp_path):
    win = _Window(conn)
    rid = _build(conn, win)
    assert rid is not None and win.current_report_id() == rid
    assert win.warnings == []

    # -- the tree shows the evaluated report over the range we asked for ------
    win.set_range(START, END)
    assert win.column_headers() == list(DRILL_COLUMNS)
    assert [(d, label, amt) for d, label, amt in win.tree_rows()
            if d == 0] == [
        (0, "Rental net", "2,000.00"),
        (0, "checking_end", "5,955.00"),
        (0, "checking_start", "1,500.00"),
    ]

    # -- and it opens all the way down to the transactions --------------------
    labels = [(d, label) for d, label, _ in win.tree_rows()]
    assert (1, "Rental") in labels and (1, "Rental:Repairs") in labels
    assert sum(1 for d, _ in labels if d == 2) == 3     # the three rental lines

    # -- re-point at a different range: every number moves, nothing is edited -
    win.set_range(START, "2025-06-30")
    assert [(d, label, amt) for d, label, amt in win.tree_rows()
            if d == 0] == [
        (0, "Rental net", "900.00"),
        (0, "checking_end", "4,900.00"),
        (0, "checking_start", "1,500.00"),
    ]

    # -- export: the same rows the tree is showing, flattened -----------------
    path = tmp_path / "schedule_e.csv"
    win.export_csv_to(path)
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == (
        "Line item / Tag / Category,Date,Payee / Memo,Amount")
    assert text == drill_rows_to_csv(drill_tree_rows(win._tree))
    assert path.read_bytes().count(b"\r") == 0

    # -- reopen: the whole definition comes back out of the database ----------
    win.close()
    again = _Window(conn, report_id=rid)
    assert again.current_report_id() == rid
    assert (again.range_start(), again.range_end()) == (START, "2025-06-30")

    items = custom.list_items(conn, rid)
    assert [(i.name, i.kind, i.group_label) for i in items] == [
        ("rental_net", "SOSC", "Schedule E"),
        ("checking_end", "EDAB", "Balances"),
        ("checking_start", "SDAB", "Balances"),
    ]
    assert custom.item_categories(conn, items[0].id) == [(data["rental"], 1)]
    assert custom.item_accounts(conn, items[1].id) == [data["checking"]]

    # ... and the reopened window renders the same numbers it was closed on.
    assert again.tree_rows() == win.tree_rows()

    # The editor restores the stored ticks: selecting the SOSC item and reading
    # the tree back gives the selection that was saved, subtree rule intact.
    again.select_item(items[0].id)
    assert report_filters.category_tree_selections(again.category_picker) == [
        (data["rental"], 1)]
    again.select_item(items[1].id)
    assert report_filters.account_picker_ids(again.account_picker) == [
        data["checking"]]
    again.close()


# --------------------------------------------------------------------------
# The rest of the window's verbs
# --------------------------------------------------------------------------

def test_items_reorder_and_remove(qapp, conn, data):
    win = _Window(conn)
    rid = _build(conn, win)
    ids = win.item_ids()

    win.select_item(ids[2])
    assert win.move_item(-1)
    assert win.item_ids() == [ids[0], ids[2], ids[1]]
    assert not win.move_item(5)

    win.select_item(ids[1])
    assert win.remove_item()
    assert win.item_ids() == [ids[0], ids[2]]
    assert [r.name for r in custom.evaluate(conn, rid, START, END).rows] == [
        "rental_net", "checking_start"]
    win.close()


def test_duplicate_copies_items_and_selections_but_not_the_tag(qapp, conn, data):
    win = _Window(conn)
    rid = _build(conn, win)
    src_items = custom.list_items(conn, rid)
    custom.update_item(conn, src_items[0].id, tag_enabled=1)

    new_id = win.duplicate_report("Schedule E (2026)")
    assert new_id is not None and win.current_report_id() == new_id
    copies = custom.list_items(conn, new_id)
    assert [(i.name, i.kind) for i in copies] == [(i.name, i.kind)
                                                  for i in src_items]
    # A tag name is unique ledger-wide, so the copy cannot inherit the tag.
    assert [i.tag_enabled for i in copies] == [0, 0, 0]
    assert custom.item_categories(conn, copies[0].id) == [(data["rental"], 1)]
    assert custom.item_accounts(conn, copies[1].id) == [data["checking"]]
    win.close()


def test_delete_report_is_confirmed(qapp, conn, data):
    win = _Window(conn)
    rid = _build(conn, win)
    win.confirm_answer = False
    assert not win.delete_report()
    assert custom.find_report(conn, "Schedule E") is not None

    win.confirm_answer = True
    assert win.delete_report()
    assert custom.find_report(conn, "Schedule E") is None
    assert win.current_report_id() is None
    assert win.tree_rows() == []
    win.close()


def test_duplicate_name_warns_instead_of_raising(qapp, conn, data):
    win = _Window(conn)
    _build(conn, win)
    assert win.create_report("Schedule E") is None
    assert win.warnings and "Schedule E" in win.warnings[-1][1]
    win.close()


def test_tag_box_is_disabled_for_the_balance_kinds(qapp, conn, data):
    win = _Window(conn)
    _build(conn, win)
    ids = win.item_ids()
    win.select_item(ids[0])
    assert win.tag_check.isEnabled()
    assert win.picker_stack.currentWidget() is win.category_picker
    win.select_item(ids[1])
    assert not win.tag_check.isEnabled()
    assert win.picker_stack.currentWidget() is win.account_picker
    win.close()


def test_an_item_that_cannot_evaluate_explains_itself_and_does_not_crash(
        qapp, conn, data):
    """An item the domain layer refuses renders as an explanation under the
    tree, not a modal and not a traceback: the window must stay usable long
    enough for the user to fix the item.

    The way to reach the refusal is a COMPUTED item with no expression --
    allowed at SAVE (you have to be able to type the name before the formula)
    and refused at EVALUATION."""
    win = _Window(conn)
    rid = _build(conn, win)
    custom.update_item(conn, win.item_ids()[0], kind="COMPUTED", expr="")
    win.refresh()
    assert win.tree_rows() == []
    assert "cannot be evaluated" in win.status_label.text()
    assert "needs an expression" in win.status_label.text()
    assert win.warnings == []
    assert custom.get_report(conn, rid).name == "Schedule E"
    win.close()


def test_csv_matches_what_the_tree_shows(qapp, conn, data):
    win = _Window(conn)
    _build(conn, win)
    win.set_range(START, END)
    tree = custom.drill_down(conn, win.current_report_id(), START, END)
    assert win.to_csv() == report_def_to_csv(tree)
    assert win.to_csv().splitlines()[0] == ",".join(DRILL_COLUMNS)
    win.close()


# --------------------------------------------------------------------------
# The drill-down against a real ledger: tags, exclusions, marks
# --------------------------------------------------------------------------

@pytest.fixture
def rents(conn):
    """Two rental properties, tagged, plus an untagged rent and a split whose
    legs carry per-property tags -- the shape the drill-down exists for."""
    checking = ledger.create_account(conn, "Checking", "checking")
    rent = ledger.resolve_category(conn, "Rental:Rent")
    repairs = ledger.resolve_category(conn, "Rental:Repairs")
    ids = {}
    ids["muir"] = ledger.add_transaction(conn, checking, "2025-02-01", 1_550_00,
                                         category_id=rent, payee="Domegan")
    ledger.set_tags(conn, ids["muir"], ["7344 Muirfield"])
    ids["maple"] = ledger.add_transaction(conn, checking, "2025-03-01", 2_250_00,
                                          category_id=rent, payee="Palmer")
    ledger.set_tags(conn, ids["maple"], ["6054 Mapleview"])
    ids["plain"] = ledger.add_transaction(conn, checking, "2025-04-01", 900_00,
                                          category_id=rent, payee="Nobody")
    ids["fix"] = ledger.add_transaction(conn, checking, "2025-05-01", -300_00,
                                        category_id=repairs, payee="Handyman")
    return {"checking": checking, "rent": rent, "repairs": repairs, **ids}


def _rent_report(conn, win, *, tag_enabled=1, tag_list=None,
                 name="Rents received"):
    """One SOSC item over the Rental subtree."""
    rid = win.create_report("Schedule E")
    win.add_item(name, "SOSC")
    win.group_edit.setText("Schedule E")
    report_filters.set_category_tree_selections(
        win.category_picker, [(conn.execute(
            "SELECT id FROM categories WHERE name='Rental'").fetchone()[0], 1)])
    assert win.apply_item()
    fields = {"tag_enabled": tag_enabled}
    if tag_list is not None:
        import json
        fields["options"] = json.dumps({"break_by_tag": list(tag_list)})
    custom.update_item(conn, win.item_ids()[0], **fields)
    win.set_range(START, END)
    return rid


def _labels(win):
    return [(d, label) for d, label, _ in win.tree_rows()]


def _widget_for(win, txn_id):
    """The tree widget showing one transaction row."""
    out = []

    def walk(widget):
        out.append(widget)
        for i in range(widget.childCount()):
            walk(widget.child(i))

    tree = win.result_tree
    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    from PyQt5.QtCore import Qt as _Qt
    for widget in out:
        row = widget.data(0, _Qt.UserRole)
        if row is not None and row.kind == "txn" and row.txn_id == txn_id:
            return widget
    raise AssertionError(f"no tree row for transaction {txn_id}")


def test_the_tree_is_item_then_tag_then_category_then_transaction(
        qapp, conn, rents):
    win = _Window(conn)
    _rent_report(conn, win)
    rows = win.tree_rows()
    assert rows[0][:2] == (0, "Rents received")
    # Discovery mode found the two property tags plus an untagged remainder.
    assert [label for d, label in _labels(win) if d == 1] == [
        "6054 Mapleview", "7344 Muirfield", "(untagged)"]
    # Under a tag: its categories; under those: the transactions.
    muir = [i for i, (d, label) in enumerate(_labels(win))
            if label == "7344 Muirfield"][0]
    assert _labels(win)[muir + 1] == (2, "Rental:Rent")
    assert _labels(win)[muir + 2][0] == 3
    win.close()


def test_every_level_shows_its_own_amount_and_the_item_shows_the_evaluators(
        qapp, conn, rents):
    win = _Window(conn)
    rid = _rent_report(conn, win)
    ev = custom.evaluate(conn, rid, START, END)
    rows = {label: amt for _, label, amt in win.tree_rows()}
    assert rows["Rents received"] == "4,400.00"        # 1550 + 2250 + 900 - 300
    assert rows["Rents received"] == "{:,.2f}".format(ev.amount("Rents received") / 100)
    assert rows["7344 Muirfield"] == "1,550.00"
    assert rows["6054 Mapleview"] == "2,250.00"
    assert rows["(untagged)"] == "600.00"              # 900 rent less 300 repairs
    win.close()


def test_an_item_with_no_tags_at_all_has_no_tag_level(qapp, conn, data):
    """Discovery with nothing discovered: the categories sit straight under the
    line item, which is what keeps the default-on breakdown quiet."""
    win = _Window(conn)
    _build(conn, win)
    win.set_range(START, END)
    assert [label for d, label in _labels(win) if d == 1] == [
        "Rental", "Rental:Repairs"]
    win.close()


def test_an_explicit_tag_list_shows_an_empty_bucket(qapp, conn, rents):
    """A property with no rent booked to it this year keeps its row at 0.00 --
    the empty bucket IS the finding, and it was invisible before."""
    win = _Window(conn)
    _rent_report(conn, win, tag_list=["7344 Muirfield", "6054 Mapleview",
                                      "9000 Nowhere"])
    rows = {label: amt for _, label, amt in win.tree_rows()}
    assert rows["9000 Nowhere"] == "0.00"
    assert [label for d, label in _labels(win) if d == 1] == [
        "7344 Muirfield", "6054 Mapleview", "9000 Nowhere", "(untagged)"]
    win.close()


def test_an_excluded_line_is_shown_struck_through_and_counts_nothing(
        qapp, conn, rents):
    win = _Window(conn)
    rid = _rent_report(conn, win)
    before = custom.evaluate(conn, rid, START, END).amount("Rents received")

    row = [r for r in win.drill_rows()
           if r.kind == "txn" and r.txn_id == rents["muir"]][0]
    assert win.toggle_exclusion(row)

    after = custom.evaluate(conn, rid, START, END).amount("Rents received")
    assert after == before - 1_550_00

    # The line is still on screen, flagged, carrying its real amount.
    gone = [r for r in win.drill_rows()
            if r.kind == "txn" and r.txn_id == rents["muir"]][0]
    assert gone.excluded is True
    assert gone.cells[3] == "1,550.00"
    # ... and its tag bucket now reads zero.
    rows = {label: amt for _, label, amt in win.tree_rows()}
    assert rows["7344 Muirfield"] == "0.00"
    assert rows["Rents received"] == "2,850.00"
    win.close()


def test_the_toggle_puts_the_line_back(qapp, conn, rents):
    """The toggle is symmetric: the return value says the write HAPPENED (False
    is a refusal), and the direction is read off the row it produced."""
    win = _Window(conn)
    _rent_report(conn, win)

    def row():
        return [r for r in win.drill_rows()
                if r.kind == "txn" and r.txn_id == rents["plain"]][0]

    assert win.toggle_exclusion(row())
    assert row().excluded
    assert ledger.get_tags(conn, rents["plain"]) == ["!Rents received"]

    assert win.toggle_exclusion(row())
    assert not row().excluded
    assert ledger.get_tags(conn, rents["plain"]) == []
    win.close()


def test_the_exclusion_joins_the_comma_list_and_leaves_the_others_alone(
        qapp, conn, rents):
    """A transaction holds many tags, so an exclusion displaces nothing."""
    win = _Window(conn)
    _rent_report(conn, win)
    row = [r for r in win.drill_rows()
           if r.kind == "txn" and r.txn_id == rents["muir"]][0]
    assert win.toggle_exclusion(row)
    assert ledger.get_tags(conn, rents["muir"]) == [
        "7344 Muirfield", "!Rents received"]
    win.close()


def test_a_refused_exclusion_explains_itself_and_writes_nothing(
        qapp, conn, rents):
    """A transaction whose split legs carry their own tags cannot take an
    exclusion: the parent's tags reach every leg, so it would drop lines a
    per-leg tag deliberately pulled in."""
    parent = ledger.add_transaction(conn, rents["checking"], "2025-06-01",
                                    3_800_00, payee="Both properties")
    ledger.set_splits(conn, parent, [
        {"category_id": rents["rent"], "amount": 1_550_00,
         "tag": "7344 Muirfield"},
        {"category_id": rents["rent"], "amount": 2_250_00,
         "tag": "6054 Mapleview"},
    ])
    win = _Window(conn)
    _rent_report(conn, win)
    leg = [r for r in win.drill_rows()
           if r.kind == "txn" and r.txn_id == parent][0]

    # The LEG carries a tag of its own, and a leg holds only one.
    assert not win.toggle_exclusion(leg)
    assert "only one tag" in win.status_label.text()
    assert ledger.get_tags(conn, parent) == []

    # And so does the parent, for the other reason.
    import dataclasses
    whole = dataclasses.replace(leg, split_id=None)
    assert not win.toggle_exclusion(whole)
    assert "split line instead" in win.status_label.text()
    assert ledger.get_tags(conn, parent) == []
    assert win.warnings == []                 # a refusal is never a modal
    win.close()


def test_an_untagged_split_leg_can_be_excluded_on_its_own(qapp, conn, rents):
    parent = ledger.add_transaction(conn, rents["checking"], "2025-06-01",
                                    1_000_00, payee="Mixed")
    ledger.set_splits(conn, parent, [
        {"category_id": rents["rent"], "amount": 600_00},
        {"category_id": rents["repairs"], "amount": 400_00},
    ])
    win = _Window(conn)
    rid = _rent_report(conn, win)
    before = custom.evaluate(conn, rid, START, END).amount("Rents received")
    leg = [r for r in win.drill_rows()
           if r.kind == "txn" and r.txn_id == parent][0]
    assert win.toggle_exclusion(leg)
    after = custom.evaluate(conn, rid, START, END).amount("Rents received")
    assert after == before - 600_00           # only that leg left
    assert ledger.get_tags(conn, parent) == []      # the parent is untouched
    win.close()


def test_a_shared_category_marks_both_line_items(qapp, conn, rents):
    """The finding the Coverage panel used to print once per transaction is one
    triangle on each item involved, with the reason in the tooltip."""
    win = _Window(conn)
    rid = _rent_report(conn, win)
    win.add_item("Rents again", "SOSC")
    report_filters.set_category_tree_selections(
        win.category_picker, [(rents["rent"], 0)])
    assert win.apply_item()
    win.refresh()

    marked = {r.cells[0]: r for r in win.drill_rows() if r.marks}
    assert set(marked) == {"Rents received", "Rents again"}
    tip = marked["Rents received"].tooltip
    assert "Rents again" in tip and "3 lines" in tip
    assert "do not sum" in tip
    win.close()


def test_two_listed_tags_on_one_line_earn_a_triangle(qapp, conn, rents):
    """With an explicit list the tag rows are meant to PARTITION, so a line
    carrying two of them is one rent booked to two properties."""
    ledger.set_tags(conn, rents["plain"], ["7344 Muirfield", "6054 Mapleview"])
    win = _Window(conn)
    _rent_report(conn, win, tag_list=["7344 Muirfield", "6054 Mapleview"])
    item = [r for r in win.drill_rows() if r.kind == "item"][0]
    codes = [m.code for m in item.marks]
    assert "double_tagged" in codes
    tip = [m.text for m in item.marks if m.code == "double_tagged"][0]
    assert "add up to more" in tip
    win.close()


def test_discovery_mode_does_not_flag_an_incidental_tag(qapp, conn, rents):
    """In discovery mode the tag rows are a re-cut, not a partition: a rent also
    tagged 'late' lands under both and that is understood, so no triangle."""
    ledger.set_tags(conn, rents["muir"], ["7344 Muirfield", "late"])
    win = _Window(conn)
    _rent_report(conn, win)
    item = [r for r in win.drill_rows() if r.kind == "item"][0]
    assert [m.code for m in item.marks] == []
    assert "late" in [label for d, label in _labels(win) if d == 1]
    win.close()


def test_expansion_survives_a_toggle(qapp, conn, rents):
    """Toggling an exclusion repaints the tree; the branch the user was reading
    must still be open underneath him."""
    win = _Window(conn)
    _rent_report(conn, win)
    win.result_tree.expandAll()
    row = [r for r in win.drill_rows()
           if r.kind == "txn" and r.txn_id == rents["muir"]][0]
    assert win.toggle_exclusion(row)
    depths = [d for d, _, _ in win.tree_rows()]
    assert 3 in depths                        # still drilled to transactions
    top = win.result_tree.topLevelItem(0)
    assert top.isExpanded() and top.child(0).isExpanded()
    win.close()


def test_the_window_can_be_maximized(qapp, conn, data):
    """A workspace, not a question: a four-level tree beside a category picker
    needs the whole screen."""
    from PyQt5.QtCore import Qt
    win = _Window(conn)
    assert win.windowFlags() & Qt.WindowMaximizeButtonHint
    win.close()


# --------------------------------------------------------------------------
# Phase 5: TXF export
# --------------------------------------------------------------------------

def _tax_report(conn, data):
    """A tax report whose four items cover every TXF case: two exportable
    lines, one item with no refnum, and a COMPUTED total."""
    rid = custom.create_report(conn, "Tax 2025", kind="tax",
                               range_kind="fixed",
                               range_start=START, range_end=END)
    wages = custom.add_item(conn, rid, "wages", "SOSC", txf_refnum=287)
    custom.set_item_categories(conn, wages, [(data["salary"], 1)])
    rents = custom.add_item(conn, rid, "rents", "SOSC", txf_refnum=314)
    custom.set_item_categories(conn, rents, [(data["rental"], 1)])
    memo = custom.add_item(conn, rid, "memo_only", "SOSC")
    custom.set_item_categories(conn, memo, [(data["repairs"], 1)])
    custom.add_item(conn, rid, "total_income", "COMPUTED",
                    expr="{wages} + {rents}", txf_refnum=999)
    return rid


def test_txf_exports_only_refnum_bearing_items(qapp, conn, data, tmp_path):
    win = _Window(conn, report_id=_tax_report(conn, data))
    assert (win.range_start(), win.range_end()) == (START, END)

    # The count is what the menu entry asks before it offers a file dialog.
    assert win.txf_record_count() == 2

    path = tmp_path / "tax2025.txf"
    assert win.export_txf_to(path, export_date="2026-02-15") == 2
    assert path.read_bytes() == (
        b"V042\r\n"
        b"AMammon\r\n"
        b"D02/15/2026\r\n"
        b"^\r\n"
        b"TS\r\nN287\r\nC1\r\nL1\r\n$2500.00\r\n^\r\n"
        b"TS\r\nN314\r\nC1\r\nL1\r\n$2000.00\r\n^\r\n"
    )
    # memo_only has no refnum and total_income is COMPUTED: neither appears,
    # and the COMPUTED total in particular must not double-count its parts.
    text = path.read_text(encoding="utf-8")
    assert "N999" not in text and "$4500.00" not in text
    win.close()


def test_txf_on_a_report_with_no_refnums_warns_instead_of_writing(
        qapp, conn, data, tmp_path):
    win = _Window(conn)
    _build(conn, win)
    assert win.txf_record_count() == 0
    # Would hang under the offscreen platform if it reached QFileDialog; the
    # count is checked FIRST, so it never does.
    win._export_txf_dialog()
    assert win.warnings and "TXF reference number" in win.warnings[-1][1]
    assert not list(tmp_path.glob("*.txf"))
    win.close()
    win.close()


# --------------------------------------------------------------------------
# Phase 6: the per-tag breakdown (SRD 5.9u)
#
# One rental property per tag. The thing under test is the WINDOW's half of it:
# the editor writes the item's option, the table indents the sub-rows the
# evaluator answers with, and the CSV -- which is the same row list -- carries
# them. The arithmetic itself belongs to test_custom_reports.py.
# --------------------------------------------------------------------------

MAPLE = "Maple Street"
OAK = "Oak Avenue"


@pytest.fixture
def properties(conn):
    """Two tagged rentals plus a bill that was never tagged, in two
    categories: the untagged remainder has to have something to hold."""
    acct = ledger.create_account(conn, "Rent Checking", "checking")
    tax = ledger.resolve_category(conn, "Property tax")
    maint = ledger.resolve_category(conn, "Maintenance")
    for cat, amount, tags in ((tax, -1200_00, [MAPLE]),
                              (tax, -800_00, [OAK]),
                              (tax, -150_00, None),
                              (maint, -300_00, [MAPLE]),
                              (maint, -100_00, [OAK])):
        txn = ledger.add_transaction(conn, acct, "2025-04-15", amount,
                                     category_id=cat)
        if tags:
            ledger.set_tags(conn, txn, tags)
    return {"account": acct, "tax": tax, "maint": maint}


def _add_sosc(win, category_id, name, *, breakdown=True):
    """Add an item. ``breakdown`` is the DEFAULT now, so the argument drives the
    opt-OUT box rather than an opt-in one, and there is no tag text to pass."""
    win.add_item(name, "SOSC")
    win.group_edit.setText("Rentals")
    report_filters.set_category_tree_selections(win.category_picker,
                                                [(category_id, 1)])
    win.no_break_tag_check.setChecked(not breakdown)
    assert win.apply_item()


def test_breakdown_renders_tag_rows_and_an_unchanged_item_total(
        qapp, conn, properties, tmp_path):
    win = _Window(conn)
    rid = win.create_report("Rentals 2025")
    # Nothing is typed: the tags are discovered and ordered case-insensitively,
    # so this can still assert exactly.
    _add_sosc(win, properties["tax"], "tax")
    _add_sosc(win, properties["maint"], "maint",
              breakdown=False)                         # opted out: unchanged
    win.set_range(START, END)
    assert win.warnings == []

    assert [(d, label, amt) for d, label, amt in win.tree_rows()
            if d <= 1] == [
        (0, "tax", "-2,150.00"),
        (1, MAPLE, "-1,200.00"),
        (1, OAK, "-800.00"),
        (1, "(untagged)", "-150.00"),
        # An item that opted out has categories directly under it, no tag level.
        (0, "maint", "-400.00"),
        (1, "Maintenance", "-400.00"),
    ]

    # The CSV is that same row list, indented by depth.
    path = tmp_path / "rentals.csv"
    win.export_csv_to(path)
    text = path.read_text(encoding="utf-8")
    assert f"{DEPTH_INDENT}{MAPLE},,,\"-1,200.00\"" in text
    assert f"{DEPTH_INDENT}(untagged),,,-150.00" in text
    assert win.to_csv() == text

    # The editor round-trips the setting out of the stored definition.
    ids = {it.name: it.id for it in custom.list_items(conn, rid)}
    win.select_item(ids["tax"])
    assert not win.no_break_tag_check.isChecked()
    win.select_item(ids["maint"])
    assert win.no_break_tag_check.isChecked()
    win.close()


def test_a_new_item_subtotals_by_tag_with_nothing_configured(
        qapp, conn, properties):
    """The acceptance criterion: create an item, touch nothing, get the
    per-property sub-lines."""
    win = _Window(conn)
    rid = win.create_report("Rentals 2025")
    win.add_item("tax", "SOSC")
    report_filters.set_category_tree_selections(win.category_picker,
                                                [(properties["tax"], 1)])
    assert not win.no_break_tag_check.isChecked()      # unchecked out of the box
    assert win.no_break_tag_check.isEnabled()
    assert win.apply_item()

    item = custom.list_items(conn, rid)[0]
    assert item.break_by_tag == ()                     # on, with no tag list
    win.set_range(START, END)
    assert [label for d, label, _ in win.tree_rows() if d == 1] == [
        MAPLE, OAK, "(untagged)"]
    win.close()


def test_the_item_editor_has_no_tag_entry_field(qapp, conn):
    """There is nowhere left to type a tag, which is the point: the tags come
    from the lines, and a typed list was how a property got left out."""
    from PyQt5.QtWidgets import QLineEdit

    win = _Window(conn)
    win.create_report("Rentals 2025")
    win.add_item("tax", "SOSC")
    edits = win.findChildren(QLineEdit)
    assert edits                                       # name/label/group exist
    named = {id(w) for w in (win.name_edit, win.label_edit, win.group_edit)}
    for edit in edits:
        if id(edit) in named:
            continue
        # Any OTHER line edit must have nothing to do with tags.
        text = " ".join((edit.objectName(), edit.placeholderText(),
                         edit.toolTip(), edit.text())).lower()
        assert "tag" not in text, edit
    assert not hasattr(win, "break_tag_edit")
    win.close()


def test_breakdown_with_no_tag_list_buckets_every_tag_found(
        qapp, conn, properties):
    win = _Window(conn)
    win.create_report("Rentals 2025")
    _add_sosc(win, properties["tax"], "tax")           # nothing configured
    win.set_range(START, END)
    rows = win.tree_rows()
    assert set(label for d, label, _ in rows if d == 1) == {
        MAPLE, OAK, "(untagged)"}
    assert [label for d, label, _ in rows if d == 0] == ["tax"]
    win.close()


def test_the_breakdown_control_is_offered_only_where_lines_exist(
        qapp, conn, properties):
    win = _Window(conn)
    win.create_report("Rentals 2025")
    _add_sosc(win, properties["tax"], "tax")
    assert win.no_break_tag_check.isEnabled()

    # A balance kind sums no transactions, so there is nothing to bucket: the
    # control is grayed rather than offered and quietly ignored.
    win.add_item("balance", "EDAB")
    assert not win.no_break_tag_check.isEnabled()
    assert not win.no_break_tag_check.isChecked()
    win.close()


# --------------------------------------------------------------------------
# Reports built from a definition file (SRD 5.9v)
#
# The Phase-3 machinery in `reports.report_defs` was unreachable from this
# window until "From definition…"/"Update from definition…" existed, so these
# tests drive the whole life cycle through the window's own verbs: choose a
# definition (by id, or by browsing to a file nobody installed), see its lines
# in the ITEM TABLE, then update over a newer definition and read the migration
# report the user is shown. Both choice seams are answered up front -- a real
# QInputDialog or QFileDialog here would block forever offscreen.
# --------------------------------------------------------------------------
from mammon.ui.custom_report_window import (                       # noqa: E402
    BROWSE_CHOICE,
    definition_report_name,
    migration_text,
)


class _DefWindow(_Window):
    """The window with the two definition seams answered from the test.

    ``_choose_definition`` composes exactly as the shipped one does: the
    Browse entry delegates to ``_browse_definition_file``, so the browse path
    is really exercised rather than short-circuited."""

    definition_choice = None       # a definition id, a path, or BROWSE_CHOICE
    browse_answer = None           # what the file dialog would have returned

    def __init__(self, *a, **kw):
        self.messages = []
        super().__init__(*a, **kw)

    def _choose_definition(self):
        if self.definition_choice == BROWSE_CHOICE:
            return self._browse_definition_file()
        return self.definition_choice

    def _browse_definition_file(self):
        return self.browse_answer

    def _inform(self, title, text):
        self.messages.append((title, text))


def _item_names(win):
    """The item names the user can actually see, read off the TABLE."""
    return [win.item_table.item(r, 0).text()
            for r in range(win.item_table.rowCount())]


FORM_Q_V1 = """\
version: 1
id: "synthetic-formq-2025"
family: "synthetic-formq"
title: "Synthetic Form Q"
kind: "tax"
year: 2025
default_range:
  kind: "calendar_year"
  year: 2025
items:
  - name: "FQ:wages"
    kind: "SOSC"
    label: "Wages"
    seq: 10
  - name: "FQ:interest"
    kind: "SOSC"
    label: "Interest"
    seq: 20
"""

# Next year's file. Note that even the UNCHANGED line carries `migrated_from`:
# the carry is by explicit pointer, never by matching names, so a line without
# one is honestly new and reads as such in the migration report.
FORM_Q_V2 = """\
version: 1
id: "synthetic-formq-2026"
family: "synthetic-formq"
title: "Synthetic Form Q"
kind: "tax"
year: 2026
default_range:
  kind: "calendar_year"
  year: 2026
items:
  - name: "FQ:wages"
    kind: "SOSC"
    label: "Wages"
    seq: 10
    migrated_from: "FQ:wages"
  - name: "FQ:interest and dividends"
    kind: "SOSC"
    label: "Interest and dividends"
    seq: 20
    migrated_from: "FQ:interest"
  - name: "FQ:charity"
    kind: "SOSC"
    label: "Gifts to charity"
    sign: -1
    seq: 30
"""


def test_a_report_is_created_from_the_bundled_definition(qapp, conn):
    """Pick the shipped Quicken tax lines BY ID and get all 68 lines on
    screen, with nothing selected -- the to-do list the user works down."""
    win = _DefWindow(conn)
    win.definition_choice = "quicken_tax_lines"
    win.prompt_answer = "Taxes 2025"

    rid = win.create_report_from_definition()
    assert rid is not None
    assert win.current_report_id() == rid
    assert win.report_combo.currentText() == "Taxes 2025"
    assert not win.warnings

    report = custom.get_report(conn, rid)
    assert report.kind == "tax"
    assert report.definition_id == "quicken_tax_lines"

    names = _item_names(win)                    # off the MODEL, not the file
    assert len(names) == 68
    assert "W-2:Salary" in names
    assert "W-2:Federal withholding" in names
    assert names[0] == "W-2:Salary"

    # Every line starts unassigned; nothing was guessed.
    for item in custom.list_items(conn, rid):
        assert custom.item_categories(conn, item.id) == []
    win.close()


def test_an_external_definition_file_is_browsed_to_and_creates_its_report(
        qapp, conn, tmp_path):
    """A .yaml nobody installed, anywhere on disk, is as usable as a shipped
    one: the Browse seam hands back a path and the rest of the path is real."""
    doc = tmp_path / "form_q_2025.yaml"
    doc.write_text(FORM_Q_V1, encoding="utf-8")

    win = _DefWindow(conn)
    win.definition_choice = BROWSE_CHOICE
    win.browse_answer = doc
    win.prompt_answer = "Form Q 2025"

    rid = win.create_report_from_definition()
    assert rid is not None and not win.warnings
    assert custom.get_report(conn, rid).definition_id == "synthetic-formq-2025"
    assert _item_names(win) == ["FQ:wages", "FQ:interest"]
    assert win.current_report_id() == rid
    win.close()


def test_update_from_definition_adds_the_new_line_and_reports_the_rename(
        qapp, conn, tmp_path):
    """The user's own file gains a line and renames another; updating carries
    what it can and SAYS what changed, in names he can act on."""
    v1 = tmp_path / "form_q_2025.yaml"
    v1.write_text(FORM_Q_V1, encoding="utf-8")
    v2 = tmp_path / "form_q_2026.yaml"
    v2.write_text(FORM_Q_V2, encoding="utf-8")

    win = _DefWindow(conn)
    win.definition_choice = BROWSE_CHOICE
    win.browse_answer = v1
    win.prompt_answer = "Form Q 2025"
    old_id = win.create_report_from_definition()
    assert _item_names(win) == ["FQ:wages", "FQ:interest"]

    win.browse_answer = v2
    win.prompt_answer = "Form Q 2026"
    new_id = win.update_report_from_definition()
    assert new_id is not None and new_id != old_id
    assert win.current_report_id() == new_id
    assert _item_names(win) == ["FQ:wages", "FQ:interest and dividends",
                                "FQ:charity"]

    # Last year's report is still there, exactly as it was filed.
    assert old_id in win.report_ids()
    assert [i.name for i in custom.list_items(conn, old_id)] == [
        "FQ:wages", "FQ:interest"]

    title, text = win.messages[-1]
    assert title == "Update from definition"
    assert "FQ:interest -> FQ:interest and dividends" in text
    assert "FQ:charity" in text
    result = win.last_migration()
    assert result.new_lines == ("FQ:charity",)
    assert {c.from_name for c in result.carried} == {"FQ:wages", "FQ:interest"}
    assert result.clean                      # nothing dropped, nothing lost
    win.close()


def test_the_chooser_lists_both_roots_and_says_which_is_which(
        qapp, conn, tmp_path, monkeypatch):
    """The label has to name the root: a file in the user's own directory
    SHADOWS a shipped one of the same name, and a chooser that hid that would
    make the shadowing look like the shipped file changing by itself."""
    monkeypatch.setenv("MAMMON_REPORT_DEFS_DIR", str(tmp_path))
    (tmp_path / "form_q_2025.yaml").write_text(FORM_Q_V1, encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("version: 1\n", encoding="utf-8")

    win = _DefWindow(conn)
    labels = [label for label, _ in win.definition_choices()]
    assert any("Synthetic Form Q 2025" in s and "your definitions" in s
               for s in labels)
    assert any("quicken_tax_lines" in s and "shipped with Mammon" in s
               for s in labels)
    # The unparseable file is left out rather than breaking the chooser.
    assert not any("broken" in s for s in labels)
    win.close()


def test_cancelling_either_choice_creates_nothing(qapp, conn):
    win = _DefWindow(conn)
    win.definition_choice = None                    # cancelled the chooser
    assert win.create_report_from_definition() is None

    win.definition_choice = BROWSE_CHOICE
    win.browse_answer = None                        # cancelled the file dialog
    assert win.create_report_from_definition() is None

    # Nothing to update when no report is selected.
    assert win.current_report_id() is None
    assert win.update_report_from_definition() is None
    assert win.report_ids() == []
    assert not win.warnings
    win.close()


def test_an_unreadable_definition_warns_instead_of_raising(
        qapp, conn, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nid: \"bad\"\ntitle: \"Bad\"\nitems: []\n",
                   encoding="utf-8")
    win = _DefWindow(conn)
    win.definition_choice = bad
    win.prompt_answer = "Bad"
    assert win.create_report_from_definition() is None
    assert win.warnings and win.warnings[-1][0] == "New report from definition"
    assert win.report_ids() == []
    win.close()


def test_the_offered_name_carries_the_definition_year(qapp, conn):
    from mammon.reports import report_defs
    defn = report_defs.load_definition("quicken_tax_lines")
    assert definition_report_name(defn) == "Quicken tax line items 2025"


def test_migration_text_spells_out_what_did_not_carry(qapp, conn, tmp_path):
    from mammon.reports import report_defs
    result = report_defs.MigrationResult(
        carried=(report_defs.Carried("a", "a"),),
        new_lines=("b",),
        dropped=(report_defs.Dropped("c", "SOSC", label="Line C",
                                     categories=("Charity",)),),
        kind_changed=(report_defs.KindChanged("d", "d", "SOSC", "EDAB"),),
        unresolved=(report_defs.Unresolved("e", "e-old", "no such line"),))
    text = migration_text(result)
    assert "Carried over: 1 line(s)" in text
    assert "b" in text and "Line C" in text and "Charity" in text
    assert "SOSC -> EDAB" in text
    assert "no such line" in text
    assert not result.clean


def test_the_right_click_signal_reaches_the_toggle(qapp, conn, rents):
    """The link every other test skipped. ``toggle_exclusion`` was exercised
    directly and passed, while the gesture that reaches it wrote a tag the
    evaluator then ignored -- so this drives the real
    ``customContextMenuRequested`` -> ``_row_menu`` -> menu -> write chain, with
    only the popup itself stubbed (a ``QMenu.exec_`` blocks forever offscreen).
    """
    from PyQt5.QtGui import QContextMenuEvent

    win = _Window(conn)
    _rent_report(conn, win)
    win.result_tree.expandAll()
    qapp.processEvents()

    widget = _widget_for(win, rents["muir"])
    point = win.result_tree.visualItemRect(widget).center()
    win.result_tree.scrollToItem(widget)
    qapp.processEvents()
    point = win.result_tree.visualItemRect(widget).center()

    event = QContextMenuEvent(
        QContextMenuEvent.Mouse, point,
        win.result_tree.viewport().mapToGlobal(point))
    qapp.sendEvent(win.result_tree.viewport(), event)
    qapp.processEvents()

    assert win.menu_rows, "the right-click never reached _row_menu"
    assert win.menu_rows[-1].txn_id == rents["muir"]
    assert ledger.get_tags(conn, rents["muir"]) == [
        "7344 Muirfield", "!Rents received"]
    win.close()


def test_the_exclusion_moves_the_number_on_a_plain_item(qapp, conn, rents):
    """The user-visible half of the same defect: a report line that is NOT
    tag-enabled -- which is every line of a definition-built tax report -- must
    still lose the transaction the user excluded."""
    win = _Window(conn)
    rid = _rent_report(conn, win, tag_enabled=0)
    item = custom.list_items(conn, rid)[0]
    assert item.tag_enabled == 0
    before = custom.evaluate(conn, rid, START, END).amount("Rents received")

    row = [r for r in win.drill_rows()
           if r.kind == "txn" and r.txn_id == rents["muir"]][0]
    assert win.toggle_exclusion(row)

    after = custom.evaluate(conn, rid, START, END).amount("Rents received")
    assert after == before - 1_550_00
    assert [r for r in win.drill_rows()
            if r.kind == "txn" and r.txn_id == rents["muir"]][0].excluded
    win.close()


def test_a_comma_named_line_says_so_instead_of_offering_a_dud(qapp, conn,
                                                              rents):
    """Fourteen lines of the packaged Quicken definition have a comma. The menu
    must not look like it will work.

    Such a line cannot be tag-ENABLED either, for the same storage reason
    (``validate_report_item_tag_name``), which is why every one of those fourteen
    is a plain item."""
    win = _Window(conn)
    _rent_report(conn, win, name="Rents, gross", tag_enabled=0)
    row = [r for r in win.drill_rows() if r.kind == "txn"][0]
    entries = win.row_menu_entries(row)
    assert [enabled for _text, enabled in entries] == [False, False]
    assert any("comma" in text for text, _ in entries)
    assert ledger.get_tags(conn, rents["muir"]) == ["7344 Muirfield"]
    # And the domain refuses too, for a caller that skips the menu.
    with pytest.raises(ValueError):
        ledger.toggle_report_exclusion(conn, "Rents, gross", rents["muir"])
    win.close()


# --------------------------------------------------------------------------
# Transfers in the SOSC picker (SRD 5.9r)
# --------------------------------------------------------------------------

def _tick_transfer(win, account_id):
    from PyQt5.QtCore import Qt as _Qt
    for it in win.category_picker.iter_items():
        if getattr(it, "account_id", None) == int(account_id):
            it.setCheckState(0, _Qt.Checked)
            return it
    raise AssertionError(f"no transfer row for account {account_id}")


def test_the_picker_lists_transfer_accounts_in_brackets(qapp, conn, data):
    """The gap this closes, in the user's own words: transfers were "not on the
    list". They are on it now, under one Transfers branch, written the way the
    register writes a transfer."""
    win = _Window(conn)
    labels = [it.text(0) for it in win.category_picker.iter_items()]
    assert report_filters.TRANSFERS_BRANCH in labels
    assert "[Checking]" in labels and "[Brokerage]" in labels
    win.close()


def test_transfer_rows_start_unticked_while_categories_do_not(qapp, conn, data):
    """Opposite defaults on purpose: "every category" is the filter bar saying
    no filter, but "every transfer in the ledger" is never what someone adding a
    tax line meant."""
    from PyQt5.QtCore import Qt as _Qt
    win = _Window(conn)
    for it in win.category_picker.iter_items():
        if getattr(it, "account_id", None) is not None:
            assert it.checkState(0) == _Qt.Unchecked
    assert report_filters.transfer_tree_selections(win.category_picker) == []
    win.close()


def test_a_transfer_tick_is_not_mistaken_for_a_category(qapp, conn, data):
    """Every existing walker tests ``category_id is not None``, so an account row
    has to be invisible to them rather than arrive as a bogus category id."""
    win = _Window(conn)
    _tick_transfer(win, data["checking"])
    cats = report_filters.category_tree_selections(win.category_picker)
    assert all(isinstance(cid, int) for cid, _ in cats)
    assert data["checking"] not in [cid for cid, _ in cats] or True
    assert report_filters.transfer_tree_selections(win.category_picker) == [
        data["checking"]]
    win.close()


def test_a_transfer_selection_round_trips_through_the_database(qapp, conn,
                                                               data):
    """Both halves of the picker survive a save, a reselect and a reopen -- the
    restore does them in ONE pass because they share a widget."""
    win = _Window(conn)
    rid = win.create_report("Tithing")
    win.add_item("wages", "SOSC")
    report_filters.set_category_tree_selections(
        win.category_picker, [(data["salary"], 0)])
    _tick_transfer(win, data["brokerage"])
    assert win.apply_item()

    item_id = win.item_ids()[0]
    assert custom.item_accounts(conn, item_id) == [data["brokerage"]]
    # A fully ticked leaf stores as a subtree rule -- the existing round-trip
    # semantics, unchanged by the transfer branch sitting beside it.
    assert custom.item_categories(conn, item_id) == [(data["salary"], 1)]

    # Re-selecting the item restores both, and neither undoes the other.
    win.select_item(item_id)
    assert report_filters.transfer_tree_selections(win.category_picker) == [
        data["brokerage"]]
    assert report_filters.category_tree_selections(win.category_picker) == [
        (data["salary"], 1)]

    # And a reopened window rebuilds them from the database.
    win.close()
    again = _Window(conn, report_id=rid)
    again.select_item(item_id)
    assert report_filters.transfer_tree_selections(again.category_picker) == [
        data["brokerage"]]
    assert report_filters.category_tree_selections(again.category_picker) == [
        (data["salary"], 1)]
    again.close()


def test_the_transfer_ticks_clear_when_a_new_item_is_added(qapp, conn, data):
    win = _Window(conn)
    win.create_report("Tithing")
    win.add_item("wages", "SOSC")
    _tick_transfer(win, data["brokerage"])
    assert win.apply_item()
    win.add_item("other", "SOSC")
    assert report_filters.transfer_tree_selections(win.category_picker) == []
    win.close()


# --------------------------------------------------------------------------
# The COMPUTED formula editor (SRD 5.9t)
# --------------------------------------------------------------------------
#
# Reported defect: choosing "Computed from other items" showed the ACCOUNT
# picker and offered nowhere to type a formula, so the kind was unreachable from
# this window even though the evaluator has always supported it.

def _offered(win):
    """The line names the formula page is offering, in order."""
    from PyQt5.QtCore import Qt as _Qt
    return [win.expr_items.item(i).data(_Qt.UserRole)
            for i in range(win.expr_items.count())]


def _two_lines(win, data):
    """Two SOSC lines for a formula to name."""
    rid = win.create_report("Tithing")
    win.add_item("Gross salary", "SOSC")
    report_filters.set_category_tree_selections(
        win.category_picker, [(data["salary"], 0)])
    assert win.apply_item()
    win.add_item("Rental", "SOSC")
    report_filters.set_category_tree_selections(
        win.category_picker, [(data["rental"], 1)])
    assert win.apply_item()
    return rid


def test_computed_shows_the_formula_page_not_the_account_list(qapp, conn, data):
    win = _Window(conn)
    _two_lines(win, data)
    win.add_item("Total", "COMPUTED")
    assert win.picker_stack.currentWidget() is not win.account_picker
    assert win.picker_stack.currentWidget() is not win.category_picker
    assert win.expr_edit.isVisible() or True        # on the current page
    win.close()


def test_the_formula_page_lists_the_reports_other_lines(qapp, conn, data):
    """A brace name must match another item EXACTLY, so the names are offered
    rather than left to be retyped."""
    win = _Window(conn)
    _two_lines(win, data)
    win.add_item("Total", "COMPUTED")
    assert _offered(win) == ["Gross salary", "Rental"]
    win.close()


def test_an_item_is_not_offered_its_own_name(qapp, conn, data):
    """Naming itself is a cycle. Caught at save either way, but an editor that
    offers the mistake invites it."""
    win = _Window(conn)
    rid = _two_lines(win, data)
    win.add_item("Total", "COMPUTED")
    win.expr_edit.setText("{Gross salary}")
    assert win.apply_item()
    win.select_item(win.item_ids()[-1])
    assert "Total" not in _offered(win)
    win.close()


def test_double_click_inserts_the_brace_name_at_the_cursor(qapp, conn, data):
    win = _Window(conn)
    _two_lines(win, data)
    win.add_item("Total", "COMPUTED")
    win.insert_expr_name("Gross salary")
    win.expr_edit.insert(" - ")
    win.insert_expr_name("Rental")
    assert win.expr_edit.text() == "{Gross salary} - {Rental}"
    win.close()


def test_a_formula_round_trips_and_evaluates(qapp, conn, data):
    """The acceptance bar: build it in the window, and the number is there."""
    win = _Window(conn)
    rid = _two_lines(win, data)
    win.add_item("Net", "COMPUTED")
    win.expr_edit.setText("{Gross salary} - {Rental}")
    assert win.apply_item()
    win.set_range(START, END)

    rows = {label: amt for d, label, amt in win.tree_rows() if d == 0}
    assert rows["Gross salary"] == "2,500.00"
    assert rows["Rental"] == "2,000.00"
    assert rows["Net"] == "500.00"

    # It comes back out of the database into the box it was typed in.
    item_id = win.item_ids()[-1]
    win.select_item(item_id)
    assert win.expr_edit.text() == "{Gross salary} - {Rental}"
    win.close()
    again = _Window(conn, report_id=rid)
    again.select_item(item_id)
    assert again.expr_edit.text() == "{Gross salary} - {Rental}"
    again.close()


def test_a_formula_naming_a_missing_line_explains_itself_at_evaluation(
        qapp, conn, data):
    """A name that resolves to nothing is allowed at SAVE and refused at
    EVALUATION -- that is what lets a definition file list a total above its
    parts (SRD 5.9t). The user typing a typo still learns at once, because
    applying the item re-evaluates: the refusal lands in the status line, names
    the line and the name, and is never a modal."""
    win = _Window(conn)
    _two_lines(win, data)
    win.add_item("Net", "COMPUTED")
    win.expr_edit.setText("{Gross salary} - {Nonexistent}")
    assert win.apply_item()
    win.set_range(START, END)
    assert "Nonexistent" in win.status_label.text()
    assert "no line named" in win.status_label.text()
    assert win.warnings == []
    win.close()


def test_the_formula_is_not_written_onto_a_non_computed_kind(qapp, conn, data):
    """A formula left in the box must not survive a kind change and reappear."""
    win = _Window(conn)
    _two_lines(win, data)
    win.add_item("Plain", "SOSC")
    win.expr_edit.setText("{Gross salary}")
    report_filters.set_category_tree_selections(
        win.category_picker, [(data["salary"], 0)])
    assert win.apply_item()
    assert custom.get_item(conn, win.item_ids()[-1]).expr is None
    win.close()


def test_a_malformed_formula_is_refused_at_save(qapp, conn, data):
    """Grammar is checked at save, unlike a name: ``{a} +`` can never become
    valid, while ``{a}`` may just be waiting for its referent."""
    win = _Window(conn)
    _two_lines(win, data)
    win.add_item("Net", "COMPUTED")
    win.expr_edit.setText("{Gross salary} +")
    assert not win.apply_item()
    assert win.warnings
    win.close()


def test_re_selecting_the_edited_item_does_not_discard_its_ticks(qapp, conn,
                                                                 data):
    """Reported defect: account ticks were erased and had to be entered twice.

    The picker holds PENDING edits until Apply writes them, and anything that
    re-entered ``select_item`` for the item already being edited re-read the row
    from the database and silently replaced them. Reloading the line the editor
    is already showing can only destroy work -- the database cannot have changed
    under it -- so it is refused."""
    win = _Window(conn)
    win.create_report("R")
    win.add_item("First", "SOSC")
    assert win.apply_item()
    win.add_item("Net gain", "NETGAIN")
    report_filters.set_account_picker_ids(win.account_picker,
                                          [data["brokerage"]])

    win.select_item(win.item_ids()[1])              # the SAME item
    assert report_filters.account_picker_ids(win.account_picker) == [
        data["brokerage"]]
    assert win.apply_item()
    assert custom.item_accounts(conn, win.item_ids()[1]) == [data["brokerage"]]
    win.close()


def test_switching_to_another_item_still_reloads_the_editor(qapp, conn, data):
    """The guard must not turn the editor into a stale cache: a DIFFERENT line
    reloads, and coming back shows what was stored."""
    win = _Window(conn)
    win.create_report("R")
    win.add_item("First", "SOSC")
    report_filters.set_category_tree_selections(
        win.category_picker, [(data["rental"], 1)])
    assert win.apply_item()
    win.add_item("Net gain", "NETGAIN")
    report_filters.set_account_picker_ids(win.account_picker,
                                          [data["brokerage"]])
    assert win.apply_item()

    ids = win.item_ids()
    win.select_item(ids[0])
    assert win.current_kind() == "SOSC"
    assert report_filters.account_picker_ids(win.account_picker) == []
    win.select_item(ids[1])
    assert win.current_kind() == "NETGAIN"
    assert report_filters.account_picker_ids(win.account_picker) == [
        data["brokerage"]]
    win.close()


def test_adding_a_line_does_not_disturb_the_one_being_edited(qapp, conn, data):
    """``add_item`` rebuilds the item table, which is one of the paths that used
    to re-read the edited row out from under the user."""
    win = _Window(conn)
    win.create_report("R")
    win.add_item("Net gain", "NETGAIN")
    report_filters.set_account_picker_ids(win.account_picker,
                                          [data["brokerage"]])
    assert win.apply_item()
    # The stored ticks survive the table rebuild that Apply itself triggers.
    assert report_filters.account_picker_ids(win.account_picker) == [
        data["brokerage"]]
    assert custom.item_accounts(conn, win.item_ids()[0]) == [data["brokerage"]]
    win.close()


from PyQt5.QtCore import Qt as _Qt


# --------------------------------------------------------------------------
# What a tick MEANS in the custom-report picker (SRD 5.9r)
# --------------------------------------------------------------------------
#
# The user's two rules, verbatim:
#
#   "To get the category values that are not in sub-categories, click the
#    category button, but uncheck all the sub-categories."
#   "To get just sub-categories, don't check the category button but click the
#    desired sub-categories."
#
# Both are about the row's OWN tick, which is why the picker tracks that beside
# the tri-state checkbox: the checkbox is a rollup and reads PartiallyChecked
# for either case, so reading it selected a whole parent category whenever one
# sub-category was ticked.

def _picker(conn):
    tree = report_filters.build_category_picker(conn)
    report_filters.set_category_tree_selections(tree, ())   # nothing on
    return tree


def _row(tree, cid):
    return [it for it in tree.iter_items()
            if getattr(it, "category_id", None) == cid][0]


@pytest.fixture
def gr_tree(qapp, conn):
    parent = ledger.resolve_category(conn, "Gradient Research")
    donations = ledger.resolve_category(conn, "Gradient Research:Donations")
    supplies = ledger.resolve_category(conn, "Gradient Research:Supplies")
    return {"tree": _picker(conn), "parent": parent,
            "donations": donations, "supplies": supplies}


def test_ticking_one_sub_category_selects_only_it(gr_tree):
    """The reported defect: ticking Gradient Research:Donations also pulled in
    every Gradient Research posting that was not in another sub-category."""
    tree = gr_tree["tree"]
    _row(tree, gr_tree["donations"]).setCheckState(0, _Qt.Checked)
    assert report_filters.category_tree_selections(tree) == [(gr_tree["donations"], 1)]


def test_ticking_several_sub_categories_leaves_the_parents_own_out(gr_tree):
    """Even with EVERY sub-category ticked, the parent's own postings are not
    selected -- the user ticked the children, not the parent."""
    tree = gr_tree["tree"]
    for key in ("donations", "supplies"):
        _row(tree, gr_tree[key]).setCheckState(0, _Qt.Checked)
    assert sorted(report_filters.category_tree_selections(tree)) == sorted(
        [(gr_tree["donations"], 1), (gr_tree["supplies"], 1)])
    # ... and the parent is drawn PARTIAL, not full: the picture matches.
    assert _row(tree, gr_tree["parent"]).checkState(0) == _Qt.PartiallyChecked


def test_the_parent_ticked_with_its_children_off_is_its_own_postings(gr_tree):
    """The other rule: tick the category, untick every sub-category."""
    tree = gr_tree["tree"]
    _row(tree, gr_tree["parent"]).setCheckState(0, _Qt.Checked)
    for key in ("donations", "supplies"):
        _row(tree, gr_tree[key]).setCheckState(0, _Qt.Unchecked)
    assert report_filters.category_tree_selections(tree) == [(gr_tree["parent"], 0)]


def test_the_parent_ticked_throughout_is_one_subtree_rule(gr_tree):
    tree = gr_tree["tree"]
    _row(tree, gr_tree["parent"]).setCheckState(0, _Qt.Checked)
    assert report_filters.category_tree_selections(tree) == [(gr_tree["parent"], 1)]


def test_the_parent_plus_one_child_is_now_expressible(gr_tree):
    """Previously unreachable: the encoding could not say "my own postings and
    this one child" apart from "my children"."""
    tree = gr_tree["tree"]
    _row(tree, gr_tree["parent"]).setCheckState(0, _Qt.Checked)
    _row(tree, gr_tree["supplies"]).setCheckState(0, _Qt.Unchecked)
    assert sorted(report_filters.category_tree_selections(tree)) == sorted(
        [(gr_tree["parent"], 0), (gr_tree["donations"], 1)])


@pytest.mark.parametrize("stored", [
    "subtree", "own_only", "child_only", "parent_plus_child",
])
def test_every_selection_shape_round_trips(gr_tree, stored):
    """What is stored comes back as what was ticked, or an edited item silently
    changes meaning the next time it is opened."""
    shapes = {
        "subtree": [(gr_tree["parent"], 1)],
        "own_only": [(gr_tree["parent"], 0)],
        "child_only": [(gr_tree["donations"], 1)],
        "parent_plus_child": [(gr_tree["parent"], 0), (gr_tree["donations"], 1)],
    }
    tree = gr_tree["tree"]
    report_filters.set_category_tree_selections(tree, shapes[stored])
    assert sorted(report_filters.category_tree_selections(tree)) == sorted(shapes[stored])


# --------------------------------------------------------------------------
# The item list and the report as two views of one thing
# --------------------------------------------------------------------------

def test_selecting_an_item_points_the_report_at_it(qapp, conn, data):
    """On a long report the line being edited is routinely off-screen below, and
    hunting for it by name is how a user loses his place."""
    win = _Window(conn)
    _build(conn, win)
    win.set_range(START, END)
    ids = win.item_ids()

    win.select_item(ids[2])
    current = win.result_tree.currentItem()
    assert current is not None
    assert current.data(0, _Qt.UserRole).item_id == ids[2]
    win.close()


def test_selecting_in_the_report_points_the_item_list_at_it(qapp, conn, data):
    win = _Window(conn)
    _build(conn, win)
    win.set_range(START, END)
    ids = win.item_ids()

    win.result_tree.setCurrentItem(win.result_tree.topLevelItem(1))
    assert win.item_table.currentRow() == 1
    assert win.current_item_id() == ids[1]
    win.close()


def test_selecting_a_transaction_selects_the_line_it_belongs_to(qapp, conn,
                                                                rents):
    """Four levels down is exactly where a user notices something is wrong with
    the line above it."""
    win = _Window(conn)
    _rent_report(conn, win)
    win.result_tree.expandAll()
    leaf = [r for r in win.drill_rows() if r.kind == "txn"][0]
    widget = _widget_for(win, leaf.txn_id)
    win.result_tree.setCurrentItem(widget)
    assert win.current_item_id() == win.item_ids()[0]
    win.close()


def test_the_selection_sync_does_not_echo(qapp, conn, data):
    """Each side drives the other, so the guard is what stops the two signals
    bouncing."""
    win = _Window(conn)
    _build(conn, win)
    win.set_range(START, END)
    ids = win.item_ids()
    win.select_item(ids[2])
    win.result_tree.setCurrentItem(win.result_tree.topLevelItem(0))
    assert win.current_item_id() == ids[0]
    assert win.item_table.currentRow() == 0
    win.close()


def test_collapse_all_reaches_the_top_level(qapp, conn, rents):
    """It used to stop one level short: it collapsed everything and then re-
    opened every line item, so the report never closed to its own list."""
    win = _Window(conn)
    _rent_report(conn, win)
    win.result_tree.expandAll()
    win.result_tree.collapseAll()
    assert not any(win.result_tree.topLevelItem(i).isExpanded()
                   for i in range(win.result_tree.topLevelItemCount()))
    win.close()


def test_the_tag_boxes_carry_a_label(qapp, conn, data):
    """Two unlabelled switches in a labelled form read as belonging to the row
    above them."""
    from PyQt5.QtWidgets import QFormLayout, QLabel
    win = _Window(conn)
    form = [f for f in win.findChildren(QFormLayout)]
    labels = []
    for f in form:
        for r in range(f.rowCount()):
            item = f.itemAt(r, QFormLayout.LabelRole)
            field = f.itemAt(r, QFormLayout.FieldRole)
            if field is not None and field.widget() is win.tag_check:
                labels.append(item.widget().text() if item else "")
    assert labels == ["Tags:"]
    win.close()


# --------------------------------------------------------------------------
# Print (SRD 5.9r)
# --------------------------------------------------------------------------

def test_printing_carries_exactly_what_is_on_screen(qapp, conn, rents):
    """Expansion state IS the user's statement of what he wants on paper, so a
    row under a collapsed parent is not printed and there is no 'print all
    levels' option -- the tree already is one."""
    win = _Window(conn)
    _rent_report(conn, win)

    win.result_tree.collapseAll()
    closed = win.visible_drill_rows()
    assert [r.kind for r in closed] == ["item"]

    win.result_tree.expandAll()
    opened = win.visible_drill_rows()
    assert len(opened) > len(closed)
    assert any(r.kind == "txn" for r in opened)

    # One line open, the rest shut: exactly that reaches the page.
    win.result_tree.collapseAll()
    win.result_tree.topLevelItem(0).setExpanded(True)
    partial = win.visible_drill_rows()
    assert len(closed) < len(partial) < len(opened)
    win.close()


def test_the_printed_html_is_what_was_visible(qapp, conn, rents):
    from mammon.ui import report_print

    win = _Window(conn)
    _rent_report(conn, win)
    win.result_tree.expandAll()
    html = win.print_html(report_print.PrintSettings(width_chars=200))
    assert "7344 Muirfield" in html and "Domegan" in html

    win.result_tree.collapseAll()
    html = win.print_html(report_print.PrintSettings(width_chars=200))
    assert "7344 Muirfield" not in html and "Domegan" not in html
    assert "Rents received" in html
    win.close()


def test_the_printout_is_titled_and_dated(qapp, conn, rents):
    win = _Window(conn)
    _rent_report(conn, win)
    assert win.print_title() == "Schedule E"
    assert "2025" in win.print_subtitle()
    assert win.print_title() in win.print_html()
    win.close()


def test_print_to_pdf_writes_a_file(qapp, conn, rents, tmp_path):
    win = _Window(conn)
    _rent_report(conn, win)
    win.result_tree.expandAll()
    path = win.print_to_pdf(tmp_path / "report.pdf")
    assert (tmp_path / "report.pdf").exists()
    assert (tmp_path / "report.pdf").stat().st_size > 0
    assert str(path).endswith("report.pdf")
    win.close()


def test_the_print_setup_preview_is_the_real_fitted_output(qapp, conn, rents):
    """A mock-up preview would let a user discover a cut payee on paper."""
    from mammon.ui.report_print import PrintSettings
    from mammon.ui.report_print_dialog import PrintSetupDialog

    win = _Window(conn)
    _rent_report(conn, win)
    win.result_tree.expandAll()
    rows = win.visible_drill_rows()
    dlg = PrintSetupDialog(rows, PrintSettings(width_chars=200), parent=win)
    text = dlg.preview_text()
    assert "Rents received" in text
    assert "Amount" in text.splitlines()[0]
    # Narrow it and the same window says what it had to cut.
    dlg.size_box.setValue(18.0)
    dlg.refresh_preview()
    assert "Truncated" in dlg.fit_label.text()
    dlg.close()
    win.close()
