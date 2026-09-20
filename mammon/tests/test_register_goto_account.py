"""'Go to [account]' parity across all three registers.

The cash/loan register has long offered, on a right-clicked transfer leg, a
context-menu entry naming the counterpart account and jumping to the mirror row.
The crypto and investment registers did not, and the user reported it as a gap:
a transfer is one movement seen twice, so whichever side you are looking at, the
register has to let you walk to the other one. The three widgets now share
``TransferGotoMixin``; what differs per register is only "what does this row
transfer to", which each one answers from its own domain module.

These tests drive the real context-menu handlers offscreen. ``QMenu`` is
monkeypatched with a recorder so the menu never has to be shown (a real
``exec_()`` would block forever under the offscreen platform, per CLAUDE.md's
headless-modal hazard) and so the chosen action can be picked by label.

Covered, per register:

  * crypto <-> crypto -- both legs live in ``crypto_transactions``, cross-linked
    by ``transfer_pair_id``;
  * crypto <-> ordinary account -- the cash leg lives in ``transactions`` and the
    crypto row keeps only ``transfer_account_id``, so the mirror is relocated by
    shape (and by MAGNITUDE: a cash leg carries the opposite sign);
  * investment XIn/XOut -- ``investment_transactions`` has no pair-id column at
    all, so the counterpart is matched on (date, counter-account, |amount|);
  * a backfilled cash leg shown in the investment register -- an ordinary
    ``transactions`` row, paired the cash register's way;
  * and, in both registers, an ordinary row, which must offer nothing.

And the direction the user reported broken afterwards: CASH -> crypto/investment.
Walking out of a cash register is not symmetric with walking into it, because
``transactions.transfer_pair_id`` names a ``transactions`` row and neither of the
other two registers is built from that table. Going to a crypto account it named
the shadow leg ``ledger.create_transfer`` writes on the exchange purely to carry
the mirror invariant -- a row the crypto grid never shows -- so ``select_txn``
found nothing and the register opened unselected, dumping the user at the bottom.
The investment side has the same hazard whenever an XIn/XOut already represents
the movement, because the register then hides the mirror cash leg. Both are now
translated into the target register's id space by the domain modules
(``crypto.crypto_txn_for_cash_leg`` / ``investments.investment_txn_for_cash_leg``).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import crypto, db, investments, ledger
from mammon.tests import fresh_db


@pytest.fixture
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    """Keep the window's remembered state out of the developer's real settings."""
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "goto.db")
    yield c
    c.close()


class _Act:
    """Stand-in for a QAction: the registers call ``setEnabled`` on some of the
    actions they build, so a bare sentinel object will not do."""

    def __init__(self, text):
        self.text = text

    def setEnabled(self, _on):
        pass


class _MenuRecorder:
    """Records every label a context menu offers and returns the action whose
    label is currently ``picked``, without ever showing a menu."""

    def __init__(self):
        self.labels = []
        self.picked = None
        self._acts = {}

    def install(self, monkeypatch):
        from mammon.ui import widgets
        recorder = self

        class _FakeMenu:
            def __init__(self, *a, **k):
                pass

            def addAction(self, text):
                act = _Act(text)
                recorder._acts[text] = act
                recorder.labels.append(text)
                return act

            def addSeparator(self):
                pass

            def addMenu(self, *a, **k):
                return _FakeMenu()

            def exec_(self, *a, **k):
                return recorder._acts.get(recorder.picked)

        monkeypatch.setattr(widgets, "QMenu", _FakeMenu)

    def reset(self, picked=None):
        self.labels = []
        self._acts = {}
        self.picked = picked

    def goto_labels(self):
        return [t for t in self.labels if t.startswith("Go to")]


def _point_at(monkeypatch, reg, row):
    """Make the next right-click land on ``row`` of ``reg``'s view."""
    monkeypatch.setattr(reg.view, "indexAt", lambda pos: reg.model.index(row, 0))
    return reg.view.rect().center()


def _selected_id(reg):
    """The id of the transaction the register is sitting on, whichever of the
    three register widgets it is."""
    row = reg.view.currentIndex().row()
    txn = reg.model.txn_at(row)
    return None if txn is None else int(txn["id"])


# ---------------------------------------------------------------------------
# crypto register
# ---------------------------------------------------------------------------
def test_crypto_register_offers_go_to_for_both_transfer_shapes(
        qapp, conn, monkeypatch):
    """A crypto row linked to an ordinary account, and one linked to another
    crypto account, each offer 'Go to [counterpart]' and land on the mirror row;
    a plain deposit offers nothing."""
    from mammon.ui.widgets import MainWindow

    ex = crypto.create_account(conn, "Coin Exchange",
                               kind=crypto.CRYPTO_KIND_EXCHANGE)
    cold = crypto.create_account(conn, "Cold Wallet",
                                 kind=crypto.CRYPTO_KIND_WALLET)
    bank = ledger.create_account(conn, "Everyday Checking", "checking")

    fund = crypto.record_cash(conn, ex, "2024-03-01", 1_000_000)   # not a transfer
    crypto.record_buy(conn, ex, "2024-03-02", "ETH", "2", 400_000)
    out_id, in_id = crypto.record_wallet_transfer(
        conn, ex, cold, "2024-03-05", "ETH", "1")
    wd = crypto.record_cash(conn, ex, "2024-03-10", -250_000)
    crypto.link_as_transfer(conn, wd, bank)

    win = MainWindow(conn)
    reg = win.open_register(ex)
    menu = _MenuRecorder()
    menu.install(monkeypatch)

    # 1. An ordinary deposit is not a transfer leg -- no entry at all.
    row = reg.model.row_for_txn(fund)
    assert row >= 0
    menu.reset()
    reg._context_menu(_point_at(monkeypatch, reg, row))
    assert menu.goto_labels() == []

    # 2. The crypto->crypto leg names the other wallet and lands on its mirror.
    row = reg.model.row_for_txn(out_id)
    assert row >= 0
    menu.reset(picked="Go to [Cold Wallet]")
    reg._context_menu(_point_at(monkeypatch, reg, row))
    assert menu.goto_labels() == ["Go to [Cold Wallet]"]
    assert win._current_account == cold
    cold_reg = win._registers[cold]
    assert win.stack.currentWidget() is cold_reg
    assert _selected_id(cold_reg) == in_id

    # 3. The cash leg lives in an ordinary account; the jump crosses register
    #    kinds, from the crypto register to the cash one.
    row = reg.model.row_for_txn(wd)
    assert row >= 0
    menu.reset(picked="Go to [Everyday Checking]")
    reg._context_menu(_point_at(monkeypatch, reg, row))
    assert menu.goto_labels() == ["Go to [Everyday Checking]"]
    assert win._current_account == bank
    bank_reg = win._registers[bank]
    assert win.stack.currentWidget() is bank_reg
    landed = bank_reg.model.txn_at(bank_reg._selected_row())
    assert landed is not None
    assert int(landed["transfer_account_id"] or 0) == ex
    assert abs(int(landed["amount"])) == 250_000

    win.close()


# ---------------------------------------------------------------------------
# investment register
# ---------------------------------------------------------------------------
def test_investment_register_offers_go_to_for_xin_and_cash_leg(
        qapp, conn, monkeypatch):
    """An XIn carrying a transfer account, and a backfilled cash leg, each offer
    'Go to [counterpart]'; a Buy offers nothing."""
    from mammon.ui.widgets import MainWindow

    brok = ledger.create_account(conn, "Brokerage", "investment",
                                 opening_balance=0)
    chk = ledger.create_account(conn, "Everyday Checking", "checking",
                                opening_balance=0)

    # (a) An XIn that IS represented on both sides: the investment row records
    #     the move, and the ordinary pair sits in the two cash accounts. There
    #     is no pair-id column on investment_transactions, so the counterpart is
    #     found by (date, counter-account, |amount|).
    xin = investments.record_investment(
        conn, brok, "2026-01-02", "XIn", amount=500_00,
        transfer_account_id=chk)
    cash_from, _cash_to = ledger.create_transfer(
        conn, chk, brok, "2026-01-02", 500_00, payee="Fund brokerage")

    # (b) A transfer with no investment row of its own: it surfaces in the
    #     register as a backfilled cash leg, paired the cash register's way.
    leg_from, leg_to = ledger.create_transfer(
        conn, chk, brok, "2026-02-01", 200_00, payee="Top up")

    # (c) A plain purchase -- not a transfer.
    buy = investments.record_investment(
        conn, brok, "2026-03-01", "Buy", symbol="Y", quantity="1",
        price="10.00", amount=-10_00)
    investments.rebuild_holdings(conn, brok)

    win = MainWindow(conn)
    reg = win.open_register(brok)
    menu = _MenuRecorder()
    menu.install(monkeypatch)

    # A Buy is not a transfer leg.
    row = reg.model.row_for_txn(buy)
    assert row >= 0
    menu.reset()
    reg._on_view_context_menu(_point_at(monkeypatch, reg, row))
    assert menu.goto_labels() == []

    # The backfilled cash leg jumps to the mirror row in checking.
    row = reg.model.row_for_txn(leg_to)
    assert row >= 0
    menu.reset(picked="Go to [Everyday Checking]")
    reg._on_view_context_menu(_point_at(monkeypatch, reg, row))
    assert menu.goto_labels() == ["Go to [Everyday Checking]"]
    assert win._current_account == chk
    chk_reg = win._registers[chk]
    assert win.stack.currentWidget() is chk_reg
    landed = chk_reg.model.txn_at(chk_reg._selected_row())
    assert landed is not None and int(landed["id"]) == leg_from

    # The XIn names the same account and lands on its own counterpart.
    row = reg.model.row_for_txn(xin)
    assert row >= 0
    menu.reset(picked="Go to [Everyday Checking]")
    reg._on_view_context_menu(_point_at(monkeypatch, reg, row))
    assert menu.goto_labels() == ["Go to [Everyday Checking]"]
    assert win._current_account == chk
    landed = chk_reg.model.txn_at(chk_reg._selected_row())
    assert landed is not None and int(landed["id"]) == cash_from

    win.close()


# ---------------------------------------------------------------------------
# cash register -> the other two (the reported defect)
# ---------------------------------------------------------------------------
def test_cash_register_goto_crypto_selects_the_crypto_row(
        qapp, conn, monkeypatch):
    """The reported bug: from a CASH register, 'Go to [exchange]' opened the
    crypto register with nothing selected.

    The cash leg's ``transfer_pair_id`` is a ``transactions`` id -- the shadow
    row ``ledger.create_transfer`` puts on the crypto account -- and the crypto
    grid is built from ``crypto_transactions``, so the id matched no row. The
    counterpart must be relocated by shape, exactly as the working reverse
    direction does."""
    from mammon.ui.widgets import MainWindow

    ex = crypto.create_account(conn, "Coin Exchange",
                               kind=crypto.CRYPTO_KIND_EXCHANGE)
    bank = ledger.create_account(conn, "Everyday Checking", "checking",
                                 opening_balance=0)

    # A withdrawal wired to the bank, and a deposit funded from it: opposite
    # directions, so both sign conventions in `_link_cash_leg` are exercised.
    wd = crypto.record_cash(conn, ex, "2024-03-10", -250_000)
    crypto.link_as_transfer(conn, wd, bank)
    dep = crypto.record_cash(conn, ex, "2024-04-01", 100_000)
    crypto.link_as_transfer(conn, dep, bank)

    win = MainWindow(conn)
    bank_reg = win.open_register(bank)
    menu = _MenuRecorder()
    menu.install(monkeypatch)

    for crypto_id, cents in ((wd, 250_000), (dep, 100_000)):
        # Find this link's cash leg in the checking register.
        cash_id = None
        for r in range(bank_reg.model.rowCount()):
            t = bank_reg.model.txn_at(r)
            if t and int(t.get("transfer_account_id") or 0) == ex \
                    and abs(int(t["amount"])) == cents:
                cash_id = int(t["id"])
                break
        assert cash_id is not None
        row = bank_reg.model.row_for_txn(cash_id)
        assert row >= 0

        # The pair id the row carries is deliberately NOT the id we must land on:
        # that is the whole defect.
        pairs = bank_reg._transfer_pairs(row)
        assert pairs == [(ex, crypto_id)]

        menu.reset(picked="Go to [Coin Exchange]")
        bank_reg._context_menu(_point_at(monkeypatch, bank_reg, row))
        assert menu.goto_labels() == ["Go to [Coin Exchange]"]
        assert win._current_account == ex
        ex_reg = win._registers[ex]
        assert win.stack.currentWidget() is ex_reg
        assert ex_reg.select_txn(crypto_id) is True
        assert _selected_id(ex_reg) == crypto_id

    win.close()


def test_cash_register_goto_investment_selects_the_represented_row(
        qapp, conn, monkeypatch):
    """Same gap on the investment side: when an XIn already represents the
    movement, the register HIDES the mirror cash leg, so forwarding the pair id
    selects nothing. The XIn must be selected instead -- while a transfer with no
    investment row of its own still lands on its backfilled cash leg."""
    from mammon.ui.widgets import MainWindow

    brok = ledger.create_account(conn, "Brokerage", "investment",
                                 opening_balance=0)
    chk = ledger.create_account(conn, "Everyday Checking", "checking",
                                opening_balance=0)

    xin = investments.record_investment(
        conn, brok, "2026-01-02", "XIn", amount=500_00,
        transfer_account_id=chk)
    cash_from, cash_to = ledger.create_transfer(
        conn, chk, brok, "2026-01-02", 500_00, payee="Fund brokerage")
    # A transfer the investment register shows as a backfilled cash leg.
    leg_from, leg_to = ledger.create_transfer(
        conn, chk, brok, "2026-02-01", 200_00, payee="Top up")
    investments.rebuild_holdings(conn, brok)

    win = MainWindow(conn)
    chk_reg = win.open_register(chk)
    inv_reg = win.open_register(brok)
    win.open_register(chk)
    menu = _MenuRecorder()
    menu.install(monkeypatch)

    # The register really does hide the represented mirror leg -- otherwise this
    # test would pass for the wrong reason.
    assert inv_reg.model.row_for_txn(xin) >= 0
    assert inv_reg.model.row_for_txn(cash_to) < 0

    # 1. The leg whose movement an XIn represents -> select the XIn.
    row = chk_reg.model.row_for_txn(cash_from)
    assert row >= 0
    assert chk_reg._transfer_pairs(row) == [(brok, xin)]
    menu.reset(picked="Go to [Brokerage]")
    chk_reg._context_menu(_point_at(monkeypatch, chk_reg, row))
    assert menu.goto_labels() == ["Go to [Brokerage]"]
    assert win._current_account == brok
    assert _selected_id(inv_reg) == xin

    # 2. The unrepresented leg -> still the backfilled cash leg, by pair id.
    win.open_register(chk)
    row = chk_reg.model.row_for_txn(leg_from)
    assert row >= 0
    assert chk_reg._transfer_pairs(row) == [(brok, leg_to)]
    menu.reset(picked="Go to [Brokerage]")
    chk_reg._context_menu(_point_at(monkeypatch, chk_reg, row))
    assert win._current_account == brok
    assert _selected_id(inv_reg) == leg_to

    win.close()


def test_goto_target_is_skipped_when_counterpart_account_is_gone(
        qapp, conn, monkeypatch):
    """Naming the account is the whole point of the entry, so a leg whose
    counterpart account no longer exists offers no jump rather than an unnamed
    one -- the same rule for every register."""
    from mammon.ui.widgets import MainWindow

    ex = crypto.create_account(conn, "Coin Exchange",
                               kind=crypto.CRYPTO_KIND_EXCHANGE)
    win = MainWindow(conn)
    reg = win.open_register(ex)
    # A row pointing at an account id that was never created.
    monkeypatch.setattr(reg, "_transfer_pairs", lambda row: [(9999, None)])
    assert reg._transfer_targets(0) == []
    win.close()
