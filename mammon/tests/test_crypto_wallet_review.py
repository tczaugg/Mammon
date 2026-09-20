"""A crypto WALLET (a single paper-wallet address) is coin-native end to end.

This is the regression for the reported defect: an on-chain by-address export
imported into a wallet came out as CASH. It reached the generic delimited
importer, which knows only date/payee/amount columns, so it inferred the block
number as the money column, reviewed the rows with a cash header set, and the
register showed Price, Amount and Cash Bal -- three columns that have no meaning
for an address where no dollar ever moves. What a wallet actually has is a coin
quantity in or out (the export's ``Value_IN`` / ``Value_OUT``), the counterparty
ADDRESS as the payee (``From`` on an increase, ``To`` on a decrease), and a
network fee paid IN THE COIN.

So the four surfaces are asserted together, because fixing any one of them alone
still leaves the wallet reading wrong:

  1. IMPORT routes to the coin-native parser, never the cash one -- no block
     number reaches an amount, and nothing is written until the row is accepted;
  2. the REVIEW pane renders coin columns (Coin In / Coin Out / Fee), not cash;
  3. ACCEPT posts through ``mammon.crypto``'s wallet writers into
     ``crypto_transactions`` with the coin, the counterparty and a COIN fee leg,
     and no fiat at all;
  4. the REGISTER drops Price / Amount / Cash Bal for a wallet and keeps them for
     an EXCHANGE, which really does hold a cash sleeve.

Offscreen Qt; synthetic ANON data only -- the fixture's addresses are obvious
0x1111.../0x2222... placeholders and no real wallet address, hash or amount
appears here.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal
from pathlib import Path

import pytest
from PyQt5.QtCore import Qt

from mammon import crypto, db, import_review
from mammon.ui import import_review_widget as irw
from mammon.ui.models import CryptoRegisterModel
from mammon.ui.widgets import (CryptoHoldingsDialog, CryptoRegisterWidget,
                               MainWindow, NetWorthByAssetDialog)
from mammon.tests import fresh_db

FIXTURE = Path(__file__).parent / "fixtures" / "etherscan_eth_2020.csv"
# The SAME export shape under Etherscan's current column names.
FIXTURE_NEW = Path(__file__).parent / "fixtures" / "etherscan_eth_2025.csv"

WALLET_ADDR = "0x1111111111111111111111111111111111111111"
COUNTERPARTY = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    # review_visibility() reads QSettings; keep it out of the real profile.
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "wallet.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    aid = crypto.create_account(conn, "Paper Wallet",
                                kind=crypto.CRYPTO_KIND_WALLET)
    # The address is what makes gas attribution and own-wallet detection possible.
    conn.execute("UPDATE accounts SET account_number=? WHERE id=?",
                 (WALLET_ADDR, aid))
    conn.commit()
    return aid


@pytest.fixture
def exchange(conn):
    return crypto.create_account(conn, "Exchange",
                                 kind=crypto.CRYPTO_KIND_EXCHANGE)


def _import(win, account_id, path=FIXTURE):
    """Drive the real per-account file-import chokepoint, reporting suppressed."""
    from mammon import ledger
    acct = ledger.get_account(win.conn, account_id)
    return win._ingest_file_via_review(account_id, acct, str(path), report=False)


# ---------------------------------------------------------------------------
# 0. the wallet's own address is asked for at creation
# ---------------------------------------------------------------------------
def test_new_account_dialog_captures_the_wallet_address(qapp, conn):
    """A wallet's on-chain address is its identity, and two behaviors need it: a
    move between two of the user's OWN accounts is recognised by matching the
    counterparty against a registered address, and gas is attributed to the user
    only on a row the user sent. Asking only in the after-the-fact properties
    dialog meant a freshly created wallet could never do either."""
    from mammon.ui.widgets import NewAccountDialog, create_account_from_values
    dlg = NewAccountDialog()
    try:
        dlg.name.setText("Paper Wallet")
        dlg.type.setCurrentText(crypto.CRYPTO_ACCOUNT_TYPE)
        # The crypto-only rows appear for a crypto type and hide for anything else.
        assert dlg.wallet_address.isVisibleTo(dlg)
        assert dlg.crypto_kind.isVisibleTo(dlg)
        dlg.type.setCurrentText("checking")
        assert not dlg.wallet_address.isVisibleTo(dlg)
        dlg.type.setCurrentText(crypto.CRYPTO_ACCOUNT_TYPE)
        i = dlg.crypto_kind.findData(crypto.CRYPTO_KIND_WALLET)
        dlg.crypto_kind.setCurrentIndex(i)
        dlg.wallet_address.setText(WALLET_ADDR)
        aid = create_account_from_values(conn, dlg.values())
    finally:
        dlg.deleteLater()
    acct = crypto.get_account(conn, aid)
    assert crypto.is_wallet_account(acct)
    # Stored in account_number -- the column the MCP authorizer already blanks, so
    # crypto adds no new place a private identifier can leak from.
    assert acct["account_number"] == WALLET_ADDR


def test_an_existing_crypto_account_can_be_switched_to_a_wallet(qapp, conn):
    """Every crypto account that predates the wallet/exchange split was backfilled
    to 'exchange' -- INCLUDING the ones that are really paper wallets. Without a
    way to say otherwise, such an account keeps the exchange register, the
    exchange review columns and the exchange import path, and the whole redesign
    never reaches the account it was built for. The properties dialog must be able
    to change the kind."""
    from mammon import ledger
    from mammon.ui.widgets import AccountDetailsDialog
    aid = crypto.create_account(conn, "Old Wallet")     # defaults to exchange
    assert crypto.is_exchange_account(crypto.get_account(conn, aid))

    dlg = AccountDetailsDialog(ledger.get_account(conn, aid), conn=conn)
    try:
        assert dlg.crypto_kind.isVisibleTo(dlg)
        i = dlg.crypto_kind.findData(crypto.CRYPTO_KIND_WALLET)
        assert i >= 0
        dlg.crypto_kind.setCurrentIndex(i)
        ledger.update_account(conn, aid, **dlg.values())
    finally:
        dlg.deleteLater()
    assert crypto.is_wallet_account(crypto.get_account(conn, aid))

    # ...and every kind-dependent surface follows immediately.
    from mammon.ui.models import CryptoRegisterModel
    m = CryptoRegisterModel(conn, aid)
    assert m.is_wallet
    assert m.column_index(CryptoRegisterModel.CASH_BAL) == -1
    reg = CryptoRegisterWidget(conn, aid)
    try:
        assert reg.review_panel.is_crypto_wallet
    finally:
        reg.deleteLater()


# ---------------------------------------------------------------------------
# 1. import routes coin-native and writes nothing yet
# ---------------------------------------------------------------------------
def test_wallet_import_produces_coin_native_review_rows(qapp, conn, wallet):
    win = MainWindow(conn)
    try:
        res = _import(win, wallet)
    finally:
        win.close()
    assert res["parsed"] == 3 and res["inserted"] == 3
    entries = import_review.load_pending(conn, wallet)
    assert len(entries) == 3
    assert all(e.mapped.is_crypto for e in entries)
    # NOTHING is in the ledger yet: review is still the gate.
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (wallet,)).fetchone()[0] == 0

    by_hash = {e.mapped.tx_hash[-1]: e.mapped for e in entries}
    # The two receives: quantity from Value_IN, payee from From, no fee (gas is
    # the sender's, and the sender is the counterparty on an inbound row).
    for tail in ("1", "2"):
        m = by_hash[tail]
        assert m.action == "RECEIVE"
        assert m.symbol == "ETH"
        assert Decimal(m.quantity) == Decimal("2")
        assert m.payee == COUNTERPARTY
        assert m.fee_quantity == ""
    # The send: quantity from Value_OUT, payee from To, gas as a COIN fee.
    out = by_hash["3"]
    assert out.action == "SEND"
    assert Decimal(out.quantity) == Decimal("1")
    assert out.payee == RECIPIENT
    assert out.fee_symbol == "ETH" and Decimal(out.fee_quantity) == Decimal("0.002")

    # No fiat anywhere -- and in particular the block number (1000001..3) never
    # became an amount, which is the defect this test exists for.
    amounts = [r[0] for r in conn.execute(
        "SELECT amount FROM review_items WHERE account_id=?", (wallet,))]
    assert amounts == [0, 0, 0]
    prices = [r[0] for r in conn.execute(
        "SELECT price FROM review_items WHERE account_id=?", (wallet,))]
    assert prices == [None, None, None]


def test_current_etherscan_column_names_are_read(qapp, conn, wallet):
    """ETHERSCAN RENAMES ITS COLUMNS. Exports through ~2023 head the hash column
    ``Txhash`` and the timestamp ``DateTime``; current ones say ``Transaction
    Hash`` and ``DateTime (UTC)``. Matching only the old spelling rejected a real
    export outright -- the parser found no header, the file read as EMPTY, and the
    import then reported it in the cash importer's words ("use Adjust mapping to
    point out the date and amount columns"), advice for a control the crypto path
    does not have. Both vocabularies must parse, and the header is the file's
    signature, so this is the one column that must never be matched narrowly."""
    from mammon.importers import crypto_core
    from mammon.importers.crypto_csv import looks_like_etherscan

    for path in (FIXTURE, FIXTURE_NEW):
        assert looks_like_etherscan(path.read_text(encoding="utf-8"))
    win = MainWindow(conn)
    try:
        res = _import(win, wallet, FIXTURE_NEW)
    finally:
        win.close()
    assert res["parsed"] == 2 and res["inserted"] == 2
    by_payee = {e.mapped.payee: e.mapped for e in import_review.load_pending(conn, wallet)}
    assert Decimal(by_payee[COUNTERPARTY].quantity) == Decimal("3")
    assert by_payee[COUNTERPARTY].action == "RECEIVE"
    assert by_payee[COUNTERPARTY].date == "2025-02-15"
    assert Decimal(by_payee[RECIPIENT].quantity) == Decimal("0.5")
    assert by_payee[RECIPIENT].action == "SEND"
    # The extra trailing Method column must not shift the fee off its own column.
    assert Decimal(by_payee[RECIPIENT].fee_quantity) == Decimal("0.0006")
    # The two spellings are the same file to the parser, field for field -- and
    # both route to the WALLET reader on the file's own signature.
    shape, old = crypto_core.parse_export_file(FIXTURE)
    assert shape == "wallet"
    assert [r.direction for r in old] == ["in", "in", "out"]


def test_an_unreadable_file_on_a_crypto_account_says_why(qapp, conn, wallet, tmp_path):
    """A file the coin parser cannot read must say so in crypto's terms. Falling
    through to the cash wording sent the user looking for an empty file and for an
    'Adjust mapping' button that a crypto register does not show."""
    bad = tmp_path / "not-an-export.csv"
    bad.write_text("Date,Description,Amount\n2025-01-01,Coffee,-4.50\n",
                   encoding="utf-8")
    win = MainWindow(conn)
    try:
        res = _import(win, wallet, bad)
        assert res["parsed"] == 0
        # The reason survives the batch path instead of being dropped.
        assert "Transaction Hash" in res["error"]
        title, msg = win._import_report(
            {"name": "Paper Wallet", "type": crypto.CRYPTO_ACCOUNT_TYPE},
            str(bad), 0, 0, {}, [], 0)
    finally:
        win.close()
    assert "Adjust mapping" not in msg
    assert "by-address" in msg.lower()
    assert "Transaction Hash" in msg


def test_reimporting_the_same_export_adds_nothing_and_says_so(qapp, conn, wallet):
    """``tx_hash`` is a globally unique on-chain key, so a re-import is a no-op --
    it must not stack a second pending copy of every row.

    And it must SAY that. ``parsed`` is what the file HELD; ``inserted`` is what
    was newly queued. Reporting the queued count as the read count made a
    re-import of an already-imported export announce that nothing could be read,
    which the zero-row message then explained as a layout problem -- for a file
    that had just been read perfectly. Every row was recognised and correctly
    refused re-entry, and that is a different sentence."""
    from mammon import ledger
    win = MainWindow(conn)
    try:
        acct = ledger.get_account(conn, wallet)
        _import(win, wallet)
        again = _import(win, wallet)
        # Still pending: named as pending, not as unreadable.
        assert again["parsed"] == 3 and again["inserted"] == 0
        assert again["prior"] == {"pending": 3}
        title, msg = win._import_report(
            acct, str(FIXTURE), again["parsed"], again["inserted"],
            again["prior"], [], 0)
        assert title == "Already imported"
        assert "already pending" in msg and "nothing was duplicated" in msg

        # Once ACCEPTED the rows are in the register, and re-importing still
        # reports them rather than claiming the file is unreadable.
        import_review.accept_all(conn, wallet)
        third = _import(win, wallet)
        assert third["parsed"] == 3 and third["inserted"] == 0
        assert third["prior"] == {"in the register": 3}
        title, msg = win._import_report(
            acct, str(FIXTURE), third["parsed"], third["inserted"],
            third["prior"], [], 0)
        assert title == "Already imported"
        assert "already in the register" in msg
    finally:
        win.close()
    assert len(import_review.load_pending(conn, wallet)) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (wallet,)).fetchone()[0] == 3


# ---------------------------------------------------------------------------
# 2. the review pane renders coin columns
# ---------------------------------------------------------------------------
def test_review_pane_uses_coin_columns_not_cash(qapp, conn, wallet):
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        panel = reg.review_panel
        assert panel.is_crypto_wallet
        headers = [panel.table.horizontalHeaderItem(c).text()
                   for c in range(panel.table.columnCount())]
        assert headers == irw._CRYPTO_HEADERS
        # The cash shape's money columns are gone; the coin ones are there.
        assert "Amount" not in headers and "Num" not in headers
        assert {"Coin", "Coin In", "Coin Out", "Fee"} <= set(headers)

        reg.show_review(import_review.load_pending(conn, wallet))
        rows = {}
        for r in range(panel.table.rowCount()):
            def cell(c):
                it = panel.table.item(r, c)
                return it.text() if it is not None else ""
            rows[cell(irw.C_PAYEE)] = (cell(irw.C_COIN), cell(irw.C_IN),
                                       cell(irw.C_OUT), cell(irw.C_FEE))
        # A receive fills Coin In and leaves Coin Out and Fee blank...
        assert rows[COUNTERPARTY] == ("ETH", "2", "", "")
        # ...and a send fills Coin Out and carries the gas, in ETH.
        assert rows[RECIPIENT] == ("ETH", "", "1", "0.002 ETH")
    finally:
        reg.deleteLater()


# ---------------------------------------------------------------------------
# 3. accept posts coin-native, through the crypto writers
# ---------------------------------------------------------------------------
def test_accepting_review_rows_posts_coin_native(qapp, conn, wallet):
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    assert import_review.accept_all(conn, wallet) == 3

    rows = list(conn.execute(
        "SELECT action, symbol, quantity, payee, amount, price, basis, "
        "       fee_symbol, fee_quantity, fee_amount, tx_hash "
        "FROM crypto_transactions WHERE account_id=? ORDER BY date", (wallet,)))
    assert len(rows) == 3
    for r in rows:
        # No fiat leg on ANY wallet row: no amount, no price, no basis, and the
        # fee is never a USD figure.
        assert r["amount"] is None and r["price"] is None and r["basis"] is None
        assert r["fee_amount"] is None
        assert r["symbol"] == "ETH"
        assert r["tx_hash"]
    assert [r["action"] for r in rows] == ["RECEIVE", "RECEIVE", "SEND"]
    assert [r["payee"] for r in rows] == [COUNTERPARTY, COUNTERPARTY, RECIPIENT]
    # The send is stored signed-negative with a coin fee leg.
    send = rows[-1]
    assert Decimal(send["quantity"]) == Decimal("-1")
    assert send["fee_symbol"] == "ETH"
    assert Decimal(send["fee_quantity"]) == Decimal("0.002")

    # Per-token holdings: 2 + 2 - 1 - 0.002 gas.
    held = {h["symbol"]: h["quantity"] for h in crypto.list_holdings(conn, wallet)}
    assert Decimal(held["ETH"]) == Decimal("2.998")
    # A wallet has no cash sleeve, and accepting coin rows must not invent one.
    assert crypto.crypto_cash(conn, wallet) == 0


def test_accepting_from_the_review_panel_posts_coin_native(qapp, conn, wallet):
    """The path the user actually clicks. The crypto register's grid is read-only,
    so its Accept hands the panel an EMPTY edit dict -- the row posts exactly as
    imported. That must still reach the coin writers and never the cash register:
    a wallet row committed as cash would book a $0.00 transaction and leave the
    coin unrecorded."""
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        reg.show_review(import_review.load_pending(conn, wallet))
        panel = reg.review_panel
        while panel.has_pending():
            entry = panel.current_entry()
            assert entry is not None
            reg._accept_new_direct(entry)
    finally:
        reg.deleteLater()
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (wallet,)).fetchone()[0] == 3
    # Nothing landed in the cash register, and no phantom category was minted.
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id=?",
        (wallet,)).fetchone()[0] == 0
    held = {h["symbol"]: h["quantity"] for h in crypto.list_holdings(conn, wallet)}
    assert Decimal(held["ETH"]) == Decimal("2.998")


# ---------------------------------------------------------------------------
# 4. the register's columns follow the account KIND
# ---------------------------------------------------------------------------
def test_wallet_register_drops_price_amount_and_cash_bal(qapp, conn, wallet):
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    import_review.accept_all(conn, wallet)

    m = CryptoRegisterModel(conn, wallet)
    assert m.is_wallet
    headers = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert "Price" not in headers
    assert "Amount" not in headers
    assert "Cash Bal" not in headers
    assert {"Coin In", "Coin Out", "Coin Bal", "Fee", "Payee",
            "Transfer"} <= set(headers)
    for key in (CryptoRegisterModel.PRICE, CryptoRegisterModel.AMOUNT,
                CryptoRegisterModel.CASH_BAL):
        assert m.column_index(key) == -1

    def cell(row, key):
        return m.data(m.index(row, m.column_index(key)))

    # Rows are in application order: receive, receive, send.
    assert cell(0, CryptoRegisterModel.COIN_IN) == "2"
    assert cell(0, CryptoRegisterModel.COIN_OUT) == ""
    assert cell(0, CryptoRegisterModel.PAYEE) == COUNTERPARTY
    assert cell(2, CryptoRegisterModel.COIN_OUT) == "1"
    assert cell(2, CryptoRegisterModel.COIN_IN) == ""
    assert cell(2, CryptoRegisterModel.PAYEE) == RECIPIENT
    assert cell(2, CryptoRegisterModel.FEE) == "0.002 ETH"
    # The running coin balance still nets the same-coin gas leg.
    assert Decimal(cell(2, CryptoRegisterModel.COIN_BAL)) == Decimal("2.998")


def test_exchange_rows_also_carry_the_counterparty_as_payee(qapp, conn, exchange):
    """The counterparty IS the payee on BOTH kinds. When the user moves coin from
    their own paper wallet to an exchange, the exchange side has to show that
    wallet's address -- otherwise the one row that says where the coin came from
    says nothing. The exchange's fiat-sleeve model is otherwise unchanged: it
    still books an FMV amount and a price, which is correct for a custodial
    account."""
    win = MainWindow(conn)
    try:
        res = _import(win, exchange)
    finally:
        win.close()
    # An exchange import goes through the SAME review queue a wallet's does --
    # its rows need MORE judgement than a wallet's, not less, because the
    # source's product names only approximate what happened.
    assert res["parsed"] == 3 and res["inserted"] == 3
    import_review.accept_all(conn, exchange)
    rows = list(conn.execute(
        "SELECT action, payee, amount, price FROM crypto_transactions "
        "WHERE account_id=? ORDER BY date", (exchange,)))
    assert [r["payee"] for r in rows] == [COUNTERPARTY, COUNTERPARTY, RECIPIENT]
    # ...and unlike a wallet, an exchange row really does carry USD.
    assert all(r["price"] is not None for r in rows)


def test_exchange_register_keeps_the_fiat_columns(qapp, conn, exchange):
    """An exchange account is custodial -- it holds a cash sleeve and trades coin
    for dollars -- so its rows really do carry a price, an amount and a running
    cash balance. Dropping them for a wallet must not drop them here."""
    m = CryptoRegisterModel(conn, exchange)
    assert not m.is_wallet
    headers = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert {"Price", "Amount", "Cash Bal", "Memo", "Transfer"} <= set(headers)
    # Columns are addressed by KEY, never by the bare constant: the two kinds
    # show different sets, so a position means different things in each.
    for key in (CryptoRegisterModel.AMOUNT, CryptoRegisterModel.PRICE,
                CryptoRegisterModel.CASH_BAL):
        assert m.column_index(key) >= 0


def test_wallet_surfaces_show_no_cash(qapp, conn, wallet):
    """A paper-wallet address has no fiat sleeve, so neither the register header
    nor the holdings window may report a Cash figure for it."""
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    import_review.accept_all(conn, wallet)

    reg = CryptoRegisterWidget(conn, wallet)
    try:
        assert "Cash" not in reg.valuation_label.text()
        assert "Coins" in reg.valuation_label.text()
    finally:
        reg.deleteLater()
    dlg = CryptoHoldingsDialog(conn, wallet)
    try:
        assert "Cash" not in dlg.total_label.text()
        symbols = [dlg.table.item(r, dlg.SYMBOL).text()
                   for r in range(dlg.table.rowCount())]
        assert symbols == ["ETH"]
    finally:
        dlg.deleteLater()


# ---------------------------------------------------------------------------
# 4b. selecting a review row opens a pending register line
# ---------------------------------------------------------------------------
def test_selecting_a_review_row_opens_a_pending_register_line(qapp, conn, wallet):
    """Clicking a review row must show the line that is about to be added, as the
    cash register does. Two things were wrong: the crypto register opened no
    pending line at all, and ``show_review`` populated the panel BEFORE revealing
    it -- so the auto-selected first row emitted while the panel was still hidden,
    the handler bailed, and clicking that row changed no selection, so Qt emitted
    nothing and it stayed blank."""
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        entries = import_review.load_pending(conn, wallet)
        reg.show_review(entries)
        m = reg.model
        # Open for the FIRST row without any further clicking -- the exact case
        # the hidden-panel emit used to lose.
        assert m.has_pending()
        row = m.pending_row()
        # The pending review line sits just before the trailing blank quick-entry
        # row (the cash register lays its pending and blank rows out the same way).
        assert m.is_pending_row(row)
        assert row == m.rowCount() - 2
        assert m.is_blank_row(m.rowCount() - 1)
        cols = [m.headerData(c, 1) for c in range(m.columnCount())]

        def cell(name):
            return m.data(m.index(row, cols.index(name)))

        assert cell("Payee") == COUNTERPARTY
        assert cell("Coin / Wallet") == "ETH"
        assert Decimal(cell("Coin In")) == Decimal("2")
        assert cell("Coin Out") == ""
        # An Accept button sits at the end of the line, where the commit belongs.
        assert reg.view.indexWidget(m.index(row, m.columnCount() - 1)) is not None

        # Only the three JUDGEMENT fields are editable. Date, coin, quantity and
        # fee are what the chain reported; editing them would invent history.
        editable = [cols[c] for c in range(m.columnCount())
                    if m.flags(m.index(row, c)) & Qt.ItemIsEditable]
        assert editable == ["Action", "Payee", "Transfer", "Memo"]

        # The action choices are scoped to the direction the chain already fixed:
        # a coin-in row is where the real judgement lives.
        assert "REWARD" in m.pending_actions() and "SEND" not in m.pending_actions()

        # Correcting the action changes what gets POSTED -- the whole point of
        # having a pending line rather than accepting as mapped.
        m.setData(m.index(row, cols.index("Action")), "REWARD")
        m.setData(m.index(row, cols.index("Memo")), "staking payout")
        reg._accept_pending()
    finally:
        reg.deleteLater()
    posted = conn.execute(
        "SELECT action, memo, payee FROM crypto_transactions "
        "WHERE account_id=?", (wallet,)).fetchone()
    assert posted["action"] == "REWARD"
    assert posted["memo"] == "staking payout"
    assert posted["payee"] == COUNTERPARTY


def test_coin_columns_are_wide_enough_and_nothing_collapses(qapp, conn, wallet):
    """A coin quantity is not a dollar amount: ETH carries 18 decimals, so a real
    row reads 25.566401739928923937 -- 21 characters against a fiat cell's 9. The
    fixed 96-110px columns elided those to '0....', which on a number is worse
    than useless. Sizing them to CONTENT then starved the stretched Payee (the
    42-character address that identifies the row) down to a 21px stub, so both
    ends are clamped and no section may collapse."""
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        reg.view.resize(1400, 400)
        m = reg.model
        cols = [m.headerData(c, 1) for c in range(m.columnCount())]
        width = {cols[c]: reg.view.columnWidth(c) for c in range(m.columnCount())}
        for name in ("Coin In", "Coin Out", "Coin Bal", "Fee"):
            assert width[name] >= 110, (name, width)
        # The fee carries a symbol too, so it is the widest of them.
        assert width["Fee"] > width["Coin In"]
        assert all(w >= 72 for w in width.values()), width
        # The review pane's coin columns follow the same rule.
        panel = reg.review_panel
        panel.table.resize(1300, 300)
        pw = {panel.table.horizontalHeaderItem(c).text(): panel.table.columnWidth(c)
              for c in range(panel.table.columnCount())}
        for name in ("Coin In", "Coin Out", "Fee"):
            assert pw[name] >= 110, (name, pw)
        assert all(w >= 72 for w in pw.values()), pw
        # And the full value is on the tooltip wherever it still elides.
        reg.show_review(import_review.load_pending(conn, wallet))
        item = panel.table.item(0, irw.C_IN)
        assert item is not None and item.toolTip() == item.text()
    finally:
        reg.deleteLater()


def test_leftover_cash_shaped_rows_cannot_be_added_to_a_wallet(
        qapp, conn, wallet, monkeypatch):
    """A ledger that imported a by-address export BEFORE on-chain routing existed
    still holds the cash-shaped review rows that import queued. They carry a
    fiat amount -- often a BLOCK NUMBER the generic importer mistook for money --
    and no coin at all.

    ``save_new`` dispatches on ``mapped.is_crypto``, so accepting one takes the
    CASH branch: it would post that amount as an ordinary transaction against an
    account that has no cash sleeve. The row must be named for what it is,
    refused, and left for the user to discard."""
    from mammon.ui import widgets as W
    rows = [{"transactionId": "0xlegacy1", "postedDate": "2024-01-05",
             "amount": "12345678.00", "isDebit": False,
             "statementDescription": "0x2222 -> 0x1111"}]
    entries = import_review.build_review(conn, wallet, rows)
    import_review.persist_entries(conn, wallet, entries)

    reg = CryptoRegisterWidget(conn, wallet)
    warned = []
    monkeypatch.setattr(W.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a[2]))
    try:
        reg.show_review(import_review.load_pending(conn, wallet))
        panel = reg.review_panel
        entry = panel.current_entry()
        assert not entry.mapped.is_crypto
        # Named, not drawn as four blank coin cells.
        assert panel.table.item(0, irw.C_COIN).text() == "(not on-chain)"
        # No pending line: there is nothing coin-native to show.
        assert not reg.model.has_pending()
        # And Accept refuses rather than posting a phantom amount.
        reg._accept_new_direct(entry)
        assert warned and "no coin" in warned[0]
    finally:
        reg.deleteLater()
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id=?",
        (wallet,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (wallet,)).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 5. net worth breaks out per coin/currency, and is REACHABLE
# ---------------------------------------------------------------------------
def test_net_worth_by_asset_is_reachable_and_breaks_out_the_coin(
        qapp, conn, wallet):
    """One column per coin/currency, a NATIVE row and a USD row, then the Total --
    and it must be reachable from the running app. The breakdown previously
    existed only on a widget nothing constructs, which is indistinguishable from
    not existing: the whole ask was to SEE what net worth is made of."""
    from mammon import fx, investments, ledger
    win = MainWindow(conn)
    try:
        _import(win, wallet)
        import_review.accept_all(conn, wallet)
        # A dollar cash account, so the breakdown has a fiat column to sit beside
        # the coin one.
        ledger.create_account(conn, "Checking", "checking",
                              opening_balance=250_000)
        # USD enters only here, at the net-worth layer: the coin is priced under
        # the shared {SYM}-USD pair, never on a wallet row.
        investments.record_price(conn, "ETH-USD", "2020-06-15", "1500")

        names = [a.text() for a in win.menuBar().actions()]
        assert "&Reports" in names
        reports = next(a.menu() for a in win.menuBar().actions()
                       if a.text() == "&Reports")
        labels = [a.text() for a in reports.actions()]
        assert "Net Worth by Asset…" in labels

        dlg = NetWorthByAssetDialog(conn, parent=win)
        try:
            m = dlg.model
            headers = [m.headerData(c, 1) for c in range(m.columnCount())]
            # A column for the coin, a column for the dollar bucket, then Total.
            assert headers[-1] == "Total"
            assert "ETH" in headers and "USD" in headers
            # Two rows: the native amount and its USD conversion.
            assert [m.headerData(r, 2) for r in range(m.rowCount())] == \
                ["Native", "USD"]
            eth = headers.index("ETH")
            assert Decimal(m.data(m.index(m.NATIVE, eth))) == Decimal("2.998")
            # The Total is the same number the account bar shows -- the coin is
            # counted exactly once.
            assert m.total_cents() == fx.total_in_currency(conn, fx.BASE_CURRENCY)
        finally:
            dlg.deleteLater()
    finally:
        win.close()
