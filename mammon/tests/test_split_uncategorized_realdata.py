"""Acceptance tests for the editable-split-total / uncategorized-remainder
feature, driven against a COPY of a real ``data/mammon_2026.db``.

The split editor's Total is editable: changing it is allowed and the signed
difference versus the split-line sum is absorbed into an UNCATEGORIZED line
(never blocking the save); an 'Adj' button snaps the Total to the current line
sum; and the register paints a yellow warning triangle before '--Split--' while
a nonzero uncategorized remainder exists. Transfer legs inside a split must be
left untouched -- including the mirror in the counter account.

Per the task, synthetic-only fixtures are NOT trusted as acceptance for this
behavior, so these exercise REAL split transactions through the actual
SplitDialog + RegisterModel code paths. The original file is never opened
writable: every test works on a tmp-dir copy and the suite asserts the original
is byte-for-byte unchanged.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.ui.models import RegisterModel
from mammon.ui.widgets import SplitDialog

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon

# mammon/tests/<this file> -> parents[2] == repo root
# Acceptance tests run against a real ledger, and ONLY when one is named
# explicitly via $MAMMON_ACCEPTANCE_DB. They deliberately do NOT fall back to
# probing for data/mammon.db: an unrelated database that merely happened to
# sit at that path made these run against the wrong ledger and fail with
# confusing AttributeErrors, and any test that opens a real ledger by
# accident is one migration away from modifying it.
REAL_DB = Path(os.environ.get("MAMMON_ACCEPTANCE_DB") or "__acceptance_db_not_configured__")

# Two real, historically stable split transactions:
PURE_CATEGORY_SPLIT = 82        # 1996 tithing split -- two category legs, no transfer
TRANSFER_LEG_SPLIT = 27476      # 2023 US Bank mortgage payment -- transfer leg to loan 81


pytestmark = pytest.mark.skipif(
    not REAL_DB.exists(),
    reason=f"real data file {REAL_DB} not present -- acceptance test needs it",
)


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _original_unmutated():
    """Guard: the real data file must never be touched. Hash it around each test."""
    before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    yield
    after = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    assert before == after, "the ORIGINAL data/mammon_2026.db was mutated"


@pytest.fixture
def real_conn(tmp_path):
    """A writable connection to a COPY of a real DB."""
    copy = tmp_path / "mammon_copy.db"
    shutil.copy2(REAL_DB, copy)
    c = db.connect(copy)
    yield c
    c.close()


def test_real_pure_category_split_edit_total_absorbs_into_uncategorized(
        qapp, real_conn):
    """Editing the Total on a real pure-category split routes the signed
    difference into an uncategorized line and lights the register triangle."""
    tid = PURE_CATEGORY_SPLIT
    t = ledger.get_transaction(real_conn, tid)
    legs0 = ledger.get_splits(real_conn, tid)
    assert t is not None and len(legs0) >= 2
    assert all(l["transfer_account_id"] is None for l in legs0)   # pure-category
    line_sum = sum(l["amount"] for l in legs0)
    assert t["amount"] == line_sum                                # starts balanced

    m = RegisterModel(real_conn, t["account_id"])
    idx = m.index(m.row_for_txn(tid), RegisterModel.CATEGORY)
    assert m.data(idx, Qt.DisplayRole) == "--Split--"
    assert m.data(idx, Qt.DecorationRole) is None                # balanced: no triangle

    dlg = SplitDialog(m, m.row_for_txn(tid))
    assert int(round(dlg.total_spin.value() * 100)) == t["amount"]
    new_total = t["amount"] - 50_00
    dlg.total_spin.setValue(new_total / 100.0)
    assert dlg.remainder_cents() == new_total - line_sum
    assert dlg.ok_btn.isEnabled()                                # never blocked
    assert dlg.apply_split() is True

    assert ledger.get_transaction(real_conn, tid)["amount"] == new_total
    assert ledger.uncategorized_split_amount(real_conn, tid) == new_total - line_sum
    legs1 = ledger.get_splits(real_conn, tid)
    # original category legs unchanged; a trailing uncategorized leg holds the diff.
    assert [(l["category_label"], l["amount"]) for l in legs1[:len(legs0)]] == \
           [(l["category_label"], l["amount"]) for l in legs0]
    assert legs1[-1]["category_label"] == ""
    assert legs1[-1]["amount"] == new_total - line_sum

    m.reload()
    idx = m.index(m.row_for_txn(tid), RegisterModel.CATEGORY)
    deco = m.data(idx, Qt.DecorationRole)
    assert isinstance(deco, QIcon) and not deco.isNull()         # warning triangle
    assert m.data(idx, Qt.DisplayRole) == "--Split--"


def test_real_split_adj_button_snaps_total_to_line_sum(qapp, real_conn):
    """'Adj' sets the Total to the current sum of the split lines (remainder 0)."""
    tid = PURE_CATEGORY_SPLIT
    t = ledger.get_transaction(real_conn, tid)
    m = RegisterModel(real_conn, t["account_id"])
    dlg = SplitDialog(m, m.row_for_txn(tid))

    # Zero out one line's amount to unbalance the split, then snap with 'Adj'.
    dlg._lines[0]["amount"].setValue(0.0)
    dlg._update_remainder()
    assert dlg.remainder_cents() != 0
    dlg._adjust_total_to_lines()
    assert dlg.remainder_cents() == 0
    assert int(round(dlg.total_spin.value() * 100)) == sum(dlg._line_amounts())
    assert dlg.apply_split() is True
    assert ledger.uncategorized_split_amount(real_conn, tid) == 0


def test_real_split_edit_total_leaves_transfer_leg_and_counter_balance(
        qapp, real_conn):
    """Editing the Total on a real split that contains a TRANSFER leg (a mortgage
    payment whose principal transfers into loan account 81) must leave the
    transfer leg's target+amount untouched and the counter account's balance
    unchanged; the difference lands in an uncategorized line."""
    tid = TRANSFER_LEG_SPLIT
    t = ledger.get_transaction(real_conn, tid)
    legs0 = ledger.get_splits(real_conn, tid)
    xfer0 = sorted((l["transfer_account_id"], l["amount"]) for l in legs0
                   if l["transfer_account_id"] is not None)
    assert xfer0, "expected at least one transfer leg on the chosen split"
    line_sum = sum(l["amount"] for l in legs0)
    targets = sorted({a for a, _amt in xfer0})
    bal_before = {a: ledger.account_balance(real_conn, a) for a in targets}

    m = RegisterModel(real_conn, t["account_id"])
    dlg = SplitDialog(m, m.row_for_txn(tid))
    new_total = t["amount"] - 100_00
    dlg.total_spin.setValue(new_total / 100.0)
    assert dlg.apply_split() is True

    assert ledger.get_transaction(real_conn, tid)["amount"] == new_total
    legs1 = ledger.get_splits(real_conn, tid)
    xfer1 = sorted((l["transfer_account_id"], l["amount"]) for l in legs1
                   if l["transfer_account_id"] is not None)
    assert xfer1 == xfer0                                        # transfer legs UNDISTURBED
    assert {a: ledger.account_balance(real_conn, a) for a in targets} == bal_before
    assert ledger.uncategorized_split_amount(real_conn, tid) == new_total - line_sum
