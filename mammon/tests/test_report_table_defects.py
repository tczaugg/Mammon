"""Regression tests for six report-TABLE defects (§5.9b).

These cover the tabular report windows (not the charts): the Itemize-by-Category
drill-down's click-to-sort ordering (including the "transfers stay alphabetical
unless Amount is the key" rule), the Cash-Flow and Account-Balance first-column
header renames, the Account-Balance rows following the account-bar grouping, the
By-Payee Payee column fitting its contents, and the shared Period dropdown being
wide enough for "Earliest to date". Synthetic data only -- no PII.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon.reports.balances import AccountBalance, BalanceReport
from mammon.reports.itemized import ItemizedTree, TreeNode, TxnLine
from mammon.ui.models import fmt_cents
from mammon.ui.report_window import (
    ACCOUNT_BALANCES_SPEC,
    BY_PAYEE_SPEC,
    BY_TAG_SPEC,
    CASH_FLOW_SPEC,
    INCOME_EXPENSE_SPEC,
    _SIDEBAR_TYPE_ORDER,
    account_balances_rows,
    itemize_tree_rows,
)


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# --------------------------------------------------------------------------
# Defect 1: Itemize drill-down -- click a column to sort within a subcategory
# --------------------------------------------------------------------------

def _txn(date, payee, cents):
    return TreeNode(kind="txn", label=date, net_cents=cents,
                    line=TxnLine(date=date, account="Checking", num="",
                                 description=payee, memo="", cleared="",
                                 amount_cents=cents))


def _one_category_tree():
    """A single leaf category holding four transactions chosen so that Date,
    Payee and Amount sorts each yield a DISTINCT order and each exercises its
    secondary key (a tie on the primary is broken by the 2nd key)."""
    t_zeta = _txn("2024-02-01", "Zeta", -500)
    t_alpha = _txn("2024-02-01", "Alpha", -100)   # ties DATE with Zeta
    t_mid = _txn("2024-01-15", "Mid", -500)       # ties AMOUNT with Zeta
    t_late = _txn("2024-03-10", "Alpha", -300)    # ties PAYEE with Alpha
    leaf = TreeNode(kind="category", label="Groceries", net_cents=-1400,
                    category_id=1, children=[t_zeta, t_alpha, t_mid, t_late])
    section = TreeNode(kind="section", label="EXPENSES", type="expense",
                       net_cents=-1400, children=[leaf])
    return ItemizedTree(start="2024-01-01", end="2024-12-31", account_ids=None,
                        sections=[section], total_cents=-1400)


def _leaf_ids(rows):
    """(payee, amount-text) uniquely identifies each of the four leaf txns."""
    return [(r.cells[2], r.cells[3]) for r in rows if r.kind == "txn"]


def test_itemize_leaves_sort_by_date_then_payee():
    rows = itemize_tree_rows(_one_category_tree(), sort_key="date")
    assert _leaf_ids(rows) == [
        ("Mid", fmt_cents(-500)),     # 2024-01-15
        ("Alpha", fmt_cents(-100)),   # 2024-02-01, payee Alpha < Zeta
        ("Zeta", fmt_cents(-500)),    # 2024-02-01
        ("Alpha", fmt_cents(-300)),   # 2024-03-10
    ]


def test_itemize_leaves_sort_by_payee_then_date():
    rows = itemize_tree_rows(_one_category_tree(), sort_key="payee")
    assert _leaf_ids(rows) == [
        ("Alpha", fmt_cents(-100)),   # Alpha, 2024-02-01 (earlier)
        ("Alpha", fmt_cents(-300)),   # Alpha, 2024-03-10
        ("Mid", fmt_cents(-500)),
        ("Zeta", fmt_cents(-500)),
    ]


def test_itemize_leaves_sort_by_amount_then_date():
    rows = itemize_tree_rows(_one_category_tree(), sort_key="amount")
    assert _leaf_ids(rows) == [
        ("Mid", fmt_cents(-500)),     # -500, 2024-01-15 (earlier)
        ("Zeta", fmt_cents(-500)),    # -500, 2024-02-01
        ("Alpha", fmt_cents(-300)),   # -300
        ("Alpha", fmt_cents(-100)),   # -100
    ]


def test_itemize_amount_sort_descending_keeps_secondary_ascending():
    # A second click toggles descending; the secondary key (date) stays ascending
    # so ties do not scramble.
    rows = itemize_tree_rows(_one_category_tree(), sort_key="amount", sort_desc=True)
    assert _leaf_ids(rows) == [
        ("Alpha", fmt_cents(-100)),   # -100 leads when descending
        ("Alpha", fmt_cents(-300)),   # -300
        ("Mid", fmt_cents(-500)),     # -500 tie: date 2024-01-15 first
        ("Zeta", fmt_cents(-500)),    # -500 tie: date 2024-02-01
    ]


def test_itemize_no_sort_key_keeps_tree_order():
    # Default (sort_key=None): the tree's own child order is preserved verbatim.
    rows = itemize_tree_rows(_one_category_tree())
    assert _leaf_ids(rows) == [
        ("Zeta", fmt_cents(-500)),
        ("Alpha", fmt_cents(-100)),
        ("Mid", fmt_cents(-500)),
        ("Alpha", fmt_cents(-300)),
    ]


# ---- the transfers-stay-alphabetical rule ---------------------------------

def _transfers_tree():
    """A TRANSFERS section with three counterparty nodes, supplied in the
    alphabetical order reports.itemize_tree emits them in."""
    def counterparty(label, net):
        return TreeNode(kind="transfer", label=label, net_cents=net,
                        type="transfer", account_id=1,
                        children=[_txn("2024-01-01", "x", net)])

    nodes = [counterparty("[Alpha Bank]", -300),
             counterparty("[Mid Bank]", -500),
             counterparty("[Zeta Bank]", -100)]
    section = TreeNode(kind="section", label="TRANSFERS", type="transfer",
                       net_cents=-900, children=nodes)
    return ItemizedTree(start="2024-01-01", end="2024-12-31", account_ids=None,
                        sections=[section], total_cents=-900)


def _transfer_labels(rows):
    return [r.cells[0] for r in rows if r.kind == "transfer"]


@pytest.mark.parametrize("key", ["date", "payee", None])
def test_transfers_stay_alphabetical_unless_amount(key):
    rows = itemize_tree_rows(_transfers_tree(), sort_key=key)
    assert _transfer_labels(rows) == ["[Alpha Bank]", "[Mid Bank]", "[Zeta Bank]"]


def test_transfers_sort_by_amount_when_amount_is_the_key():
    rows = itemize_tree_rows(_transfers_tree(), sort_key="amount")
    # nets -500, -300, -100 ascending -> Mid, Alpha, Zeta.
    assert _transfer_labels(rows) == ["[Mid Bank]", "[Alpha Bank]", "[Zeta Bank]"]


# --------------------------------------------------------------------------
# Defects 2 & 4: first-column header renames
# --------------------------------------------------------------------------

def test_cash_flow_first_column_header_is_direction():
    assert CASH_FLOW_SPEC.columns == ["Direction", "Category / Account", "Amount"]


def test_account_balances_first_column_header_is_account_type():
    assert ACCOUNT_BALANCES_SPEC.columns == [
        "Account Type", "Category / Account", "Amount"]


def test_income_expense_keeps_shared_section_header():
    # Only Cash Flow's first column changed (it alone folds transfers into Net);
    # Income vs Expense keeps the shared "Section" header.
    assert INCOME_EXPENSE_SPEC.columns[0] == "Section"


def test_report_window_renders_renamed_headers(qapp, tmp_path):
    from mammon import db
    from mammon.app import sample_data
    from mammon.ui.report_window import ReportWindow

    conn = db.init_db(tmp_path / "hdr.db")
    sample_data(conn)
    try:
        for spec, first in [(CASH_FLOW_SPEC, "Direction"),
                            (ACCOUNT_BALANCES_SPEC, "Account Type")]:
            win = ReportWindow(conn, spec=spec)
            try:
                assert win.table.horizontalHeaderItem(0).text() == first
            finally:
                win.close()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Defect 4: Account-Balance rows follow the sidebar's account grouping
# --------------------------------------------------------------------------

def _balance(name, type_, cents):
    return AccountBalance(account_id=0, name=name, type=type_,
                          hidden=False, closed=False, cents=cents)


def test_account_balances_rows_follow_sidebar_group_order():
    # Rows arrive in a scrambled type order; the projector regroups them
    # Banking -> Credit Card -> Investing -> Property & Debt, preserving the
    # within-group input order (a stable sort).
    report = BalanceReport(as_of=None, total=0, rows=[
        _balance("House", "asset", 30000000),
        _balance("Visa", "credit", -50000),
        _balance("Brokerage", "investment", 8000000),
        _balance("Checking A", "checking", 100000),
        _balance("Mortgage", "liability", -20000000),
        _balance("Checking B", "checking", 200000),
        _balance("Savings", "savings", 500000),
    ])
    rows = account_balances_rows(report)
    names = [r.label for r in rows[:-1]]  # drop the trailing Net Worth total
    assert names == [
        "Checking A", "Checking B", "Savings",   # Banking, input order kept
        "Visa",                                   # Credit Card
        "Brokerage",                              # Investing
        "House", "Mortgage",                      # Property & Debt
    ]
    assert rows[-1].label == "Net Worth"


def test_unknown_account_type_sorts_last():
    report = BalanceReport(as_of=None, total=0, rows=[
        _balance("Mystery", "loyalty_points", 999),
        _balance("Checking", "checking", 100000),
    ])
    names = [r.label for r in account_balances_rows(report)[:-1]]
    assert names == ["Checking", "Mystery"]


def test_sidebar_type_order_mirrors_the_account_bar():
    # Drift guard: report_window._SIDEBAR_TYPE_ORDER must equal the flattened type
    # sequence of the account bar's own grouping (widgets._BAR_GROUPS), or the
    # printed Account-Balance report and the on-screen bar disagree on ordering.
    from mammon.ui.widgets import _BAR_GROUPS
    flattened = [t for _, types in _BAR_GROUPS for t in types]
    assert _SIDEBAR_TYPE_ORDER == flattened


# --------------------------------------------------------------------------
# Defect 5: By-Payee Payee column starts wide enough to show every name
# --------------------------------------------------------------------------

def test_by_payee_fits_first_column():
    assert BY_PAYEE_SPEC.fit_first_column is True
    # By Tag shares the payee projector but is not flagged, so it is unaffected.
    assert BY_TAG_SPEC.fit_first_column is False


# --------------------------------------------------------------------------
# Defect 6: shared Period dropdown is wide enough for "Earliest to date"
# --------------------------------------------------------------------------

def test_period_combo_is_wide_enough_for_earliest_to_date(qapp):
    from mammon.ui.report_filters import PERIOD_PRESETS, make_period_combo

    combo = make_period_combo()
    assert combo.count() == len(PERIOD_PRESETS)
    fm = combo.fontMetrics()
    measure = getattr(fm, "horizontalAdvance", fm.width)
    # The widest preset label must fit inside the pinned minimum width.
    widest = max(measure(label) for label, _ in PERIOD_PRESETS)
    assert combo.minimumWidth() >= widest
    assert combo.minimumWidth() >= measure("Earliest to date")
