"""A crypto EXCHANGE imports a custodial transaction history, coin AND cash.

The reported defect: creating a Coinbase account and importing its CSV failed
outright. Every crypto account was routed to the block-explorer (by-address)
reader, which is a different document entirely -- it looks for Value_IN /
Value_OUT / Transaction Hash columns that a Coinbase history does not have, so
the file "could not be read" and the message blamed the file.

Four things this asserts, because an exchange is not a wallet with dollars bolted
on -- it is a different account:

  1. the FILE picks the reader (a Coinbase history and an Etherscan export are
     both CSVs imported into crypto accounts, and only their content tells them
     apart), while the ACCOUNT decides only whether USD rides onto the row;
  2. the SIGN on ``Quantity Transacted`` fixes direction -- the same
     ``Withdrawal`` type is coin leaving on one row and dollars leaving on the
     next, and ``Exchange Withdrawal`` is money ARRIVING;
  3. a pure FIAT row moves the cash sleeve and opens no position, while a coin
     move that is not a trade moves NO cash (its USD figure is a valuation);
  4. an unknown Transaction Type is not an error: Coinbase keeps adding product
     names, and the row falls back to the direction the sign already fixed.

Offscreen Qt; synthetic ANON data only -- the fixture's user, bank and addresses
are placeholders, and no real name, account number or transaction id appears.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal
from pathlib import Path

import pytest

from mammon import crypto, db, import_review, ledger
from mammon.importers import crypto_core
from mammon.importers.coinbase_csv import looks_like_coinbase, parse_coinbase
from mammon.ui import import_review_widget as irw
from mammon.ui.models import CryptoRegisterModel
from mammon.ui.widgets import CryptoRegisterWidget, MainWindow

FIXTURE = Path(__file__).parent / "fixtures" / "coinbase_history.csv"
ETHERSCAN = Path(__file__).parent / "fixtures" / "etherscan_eth_2020.csv"
SENDER = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "exchange.db")
    yield c
    c.close()


@pytest.fixture
def exchange(conn):
    return crypto.create_account(conn, "Coinbase",
                                 kind=crypto.CRYPTO_KIND_EXCHANGE)


def _import(win, account_id, path=FIXTURE):
    acct = ledger.get_account(win.conn, account_id)
    return win._ingest_file_via_review(account_id, acct, str(path), report=False)


def _by_id(entries):
    return {e.mapped.tx_hash[-2:]: e.mapped for e in entries}


# ---------------------------------------------------------------------------
# 1. the parser
# ---------------------------------------------------------------------------
def test_header_is_found_below_the_preamble():
    """Coinbase puts a blank line, a 'Transactions' title and a USER line (the
    account holder's real name) above the header. A reader that assumes row 0 is
    the header sees a one-column file and finds nothing."""
    text = FIXTURE.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ""            # the preamble really is there
    assert looks_like_coinbase(text)
    assert len(parse_coinbase(text)) == 11


def test_direction_comes_from_the_sign_not_the_type_name():
    """The same 'Withdrawal' is coin leaving on one row and dollars leaving on
    the next, and 'Exchange Withdrawal' is money ARRIVING (withdrawn from the
    exchange venue INTO this account). Reading direction off the word inverts
    them; the sign is the only trustworthy statement."""
    rows = {r.txn_id[-2:]: r for r in parse_coinbase(
        FIXTURE.read_text(encoding="utf-8"))}
    # ...ARRIVING, despite the word "Withdrawal".
    assert rows["a7"].raw_type == "Exchange Withdrawal"
    assert rows["a7"].action == "DEPOSIT" and rows["a7"].amount_cents == 91025
    # ...dollars leaving.
    assert rows["a8"].action == "WITHDRAW" and rows["a8"].amount_cents == -150000
    # ...and a COIN "Withdrawal" is a SALE plus a cash-out, not a coin send:
    # Coinbase converts and wires the dollars. The row carries a Subtotal, a
    # Total and a fee, and names a bank in its note; a genuine coin move off the
    # platform is type `Send` and carries a recipient address instead.
    assert rows["a9"].action == "SELL" and rows["a9"].symbol == "ETH"
    assert rows["a9"].amount_cents == 225000


def test_fiat_rows_move_cash_and_coin_rows_do_not():
    """A row whose Asset IS the pricing currency moves dollars and opens no
    position. A coin move that is not a trade moves NO cash: Coinbase prints a
    USD figure on those rows for tax purposes, and booking it would invent money
    the account never saw."""
    rows = {r.txn_id[-2:]: r for r in parse_coinbase(
        FIXTURE.read_text(encoding="utf-8"))}
    assert rows["a1"].is_cash and rows["a1"].symbol == "" and rows["a1"].amount_cents == 200000
    # A trade DOES move the sleeve, at the total the account was charged.
    assert rows["a2"].action == "BUY" and rows["a2"].amount_cents == -152000
    assert rows["a4"].action == "SELL" and rows["a4"].amount_cents == 99000
    # A receive, a staking reward and a venue transfer move no cash at all.
    for key in ("a3", "a5", "a6", "b1"):
        assert rows[key].amount_cents == 0, key


def test_venue_transfers_are_transfers_not_disposals():
    """Coin moved between the user's own Coinbase venues never left their
    control. Booking it as SEND/RECEIVE would realize a gain on it."""
    rows = {r.txn_id[-2:]: r for r in parse_coinbase(
        FIXTURE.read_text(encoding="utf-8"))}
    assert rows["a5"].action == "TRANSFER_OUT"
    assert rows["a6"].action == "TRANSFER_IN"


def test_an_unknown_transaction_type_is_not_an_error():
    """Coinbase keeps adding product names. An unrecognised one falls back to the
    direction the sign already fixed, and the raw type is kept so the review row
    can show what the file actually said."""
    rows = {r.txn_id[-2:]: r for r in parse_coinbase(
        FIXTURE.read_text(encoding="utf-8"))}
    row = rows["b2"]
    assert row.raw_type == "Some Future Product"
    assert row.action == "RECEIVE"           # positive quantity -> coin in
    assert row.symbol == "ETH"


# ---------------------------------------------------------------------------
# 2. routing: the FILE picks the reader
# ---------------------------------------------------------------------------
def test_the_file_picks_the_reader_not_the_account():
    """Both documents are CSVs imported into crypto accounts. Only their content
    tells them apart, so routing by account kind hands one of them a reader that
    cannot read it -- which is exactly what failed."""
    assert crypto_core.parse_export_file(FIXTURE)[0] == "exchange"
    assert crypto_core.parse_export_file(ETHERSCAN)[0] == "wallet"


def test_a_file_that_is_neither_names_both_shapes(tmp_path):
    bad = tmp_path / "plain.csv"
    bad.write_text("Date,Description,Amount\n2025-01-01,Coffee,-4.50\n",
                   encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        crypto_core.parse_export_file(bad)
    assert "Transaction Hash" in str(exc.value) and "Coinbase" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. import -> review -> accept, through the real app
# ---------------------------------------------------------------------------
def test_exchange_import_reviews_then_posts_coin_and_cash(qapp, conn, exchange):
    win = MainWindow(conn)
    try:
        res = _import(win, exchange)
    finally:
        win.close()
    assert res["parsed"] == 11 and res["inserted"] == 11
    # Nothing is written until a row is accepted -- an exchange's rows need MORE
    # judgement than a wallet's, not less.
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (exchange,)).fetchone()[0] == 0

    mapped = _by_id(import_review.load_pending(conn, exchange))
    assert mapped["a1"].symbol == ""          # a fiat row carries no coin...
    assert mapped["a1"].quantity == ""        # ...and its dollars are the Amount
    assert mapped["a1"].amount_cents == 200000
    assert mapped["a2"].action == "BUY" and mapped["a2"].amount_cents == -152000
    assert mapped["a3"].payee == SENDER       # the counterparty IS the payee
    assert mapped["a9"].payee == RECIPIENT

    assert import_review.accept_all(conn, exchange) == 11

    # Cash sleeve: +2000 deposit, -1520 buy, +990 sell, +910.25 in, -1500 out.
    # +2000 deposit, -1520 buy, +990 sell, +910.25 in, -1500 out, and the 2021
    # coin withdrawal is a SALE whose $2250 proceeds land in the sleeve.
    assert crypto.crypto_cash(conn, exchange) == (
        200000 - 152000 + 99000 + 91025 - 150000 + 225000)
    # Coin: +1 buy, +2 receive, -0.5 sell, -0.25 out, +0.25 in, -0.75 sold-and-
    #       withdrawn, +0.01 staking, +0.5 unknown-type.
    held = {h["symbol"]: h["quantity"] for h in crypto.list_holdings(conn, exchange)}
    assert Decimal(held["ETH"]) == Decimal("2.26")
    # A venue transfer books no realized gain; a sale and a send do.
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM crypto_transactions WHERE account_id=? ORDER BY date",
        (exchange,))]
    assert actions.count("TRANSFER_OUT") == 1 and actions.count("TRANSFER_IN") == 1
    assert "DEPOSIT" in actions and "WITHDRAW" in actions


def test_reimport_is_a_no_op_and_says_so(qapp, conn, exchange):
    """The exchange's own row id is as exact a key as an on-chain hash."""
    win = MainWindow(conn)
    try:
        _import(win, exchange)
        import_review.accept_all(conn, exchange)
        again = _import(win, exchange)
    finally:
        win.close()
    assert again["parsed"] == 11 and again["inserted"] == 0
    assert again["prior"] == {"in the register": 11}


# ---------------------------------------------------------------------------
# 4. the UI surfaces
# ---------------------------------------------------------------------------
def test_review_pane_shows_coin_and_fiat(qapp, conn, exchange):
    """The cash layout hides the coin (the reported symptom: a dollar figure and
    no asset, for rows whose whole content is '0.25 ETH moved'); the wallet
    layout hides what a Buy cost. An exchange needs both."""
    win = MainWindow(conn)
    try:
        _import(win, exchange)
    finally:
        win.close()
    reg = CryptoRegisterWidget(conn, exchange)
    try:
        panel = reg.review_panel
        assert panel.is_crypto_exchange and not panel.is_crypto_wallet
        headers = [panel.table.horizontalHeaderItem(c).text()
                   for c in range(panel.table.columnCount())]
        assert headers == irw._EXCHANGE_HEADERS
        assert {"Coin", "Quantity", "Price", "Amount", "Action"} <= set(headers)

        reg.show_review(import_review.load_pending(conn, exchange))
        rows = {}
        for r in range(panel.table.rowCount()):
            def cell(c):
                it = panel.table.item(r, c)
                return it.text() if it is not None else ""
            rows[cell(irw.X_MEMO)] = (cell(irw.X_ACTION), cell(irw.X_COIN),
                                      cell(irw.X_QTY), cell(irw.X_AMOUNT))
        buy = rows["Bought 1 ETH for 1520.00 USD"]
        assert buy == ("BUY", "ETH", "1", "-1,520.00")
        # A coin receive shows the coin and NO amount: no money moved.
        recv = rows["Received 2 ETH from an external account"]
        assert recv[:3] == ("RECEIVE", "ETH", "2") and recv[3] == ""
    finally:
        reg.deleteLater()


def test_pending_line_offers_the_actions_of_the_same_direction(qapp, conn, exchange):
    """The action is the least certain field on this source -- product names only
    approximate what happened -- so it is correctable. But a coin-out row must
    not be turnable into a deposit by a mis-click: the choices are the actions of
    the direction the sign already fixed."""
    win = MainWindow(conn)
    try:
        _import(win, exchange)
    finally:
        win.close()
    reg = CryptoRegisterWidget(conn, exchange)
    try:
        entries = {e.mapped.tx_hash[-2:]: e
                   for e in import_review.load_pending(conn, exchange)}
        reg.show_review(list(entries.values()))
        m = reg.model
        assert not m.is_wallet
        # A cash row offers only cash actions.
        reg._show_pending(entries["a1"])
        assert m.pending_actions() == sorted(crypto.CASH_ACTIONS)
        # A coin-out row offers only removals -- never DEPOSIT.
        reg._show_pending(entries["a9"])
        assert set(m.pending_actions()) == set(crypto.REMOVE_ACTIONS)
        # An exchange's fiat leg is correctable; a wallet has none to correct.
        cols = [m.headerData(c, 1) for c in range(m.columnCount())]
        row = m.pending_row()
        from PyQt5.QtCore import Qt
        editable = [cols[c] for c in range(m.columnCount())
                    if m.flags(m.index(row, c)) & Qt.ItemIsEditable]
        assert editable == ["Action", "Payee", "Transfer", "Amount", "Memo"]

        # Correcting the unknown-type row's action is what it exists for.
        reg._show_pending(entries["b2"])
        r2 = m.pending_row()
        m.setData(m.index(r2, cols.index("Action")), "REWARD")
        reg._accept_pending()
    finally:
        reg.deleteLater()
    assert conn.execute(
        "SELECT action FROM crypto_transactions WHERE account_id=? AND tx_hash LIKE '%b2'",
        (exchange,)).fetchone()[0] == "REWARD"


def test_exchange_register_shows_the_memo(qapp, conn, exchange):
    """An exchange's source says things its columns cannot ('Sold 2 ETH for
    1222.01 USD'), and that sentence is often the only record of what a row was."""
    win = MainWindow(conn)
    try:
        _import(win, exchange)
    finally:
        win.close()
    import_review.accept_all(conn, exchange)
    m = CryptoRegisterModel(conn, exchange)
    headers = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert "Memo" in headers
    memos = [m.data(m.index(r, headers.index("Memo"))) for r in range(m.rowCount())]
    assert "Bought 1 ETH for 1520.00 USD" in memos


# ---------------------------------------------------------------------------
# 5. correcting a posted row, and the transfer gesture
# ---------------------------------------------------------------------------
def test_a_posted_crypto_row_is_correctable(qapp, conn, exchange):
    """The crypto register was once fully READ-ONLY, on the reasoning that crypto
    events are entered by import. But an import is not an oracle -- an address is
    mistyped, a coin symbol comes through wrong, a source dates a row a day off,
    and a "Receive from an external account" is really a transfer from your own
    wallet -- so EVERY field the row legitimately has is now correctable, both
    inline and through the Edit dialog. The only cells that stay read-only are the
    DERIVED running balances (Coin Bal, Cash Bal): they are computed, not stored,
    so there is nothing there to edit."""
    from PyQt5.QtCore import Qt
    from mammon.ui.widgets import CryptoRegisterWidget
    txn = crypto.record_income(conn, exchange, "2021-03-01", "RECEIVE", "ETH", 2,
                               320000, payee="an external account")
    crypto.rebuild_holdings(conn, exchange)
    reg = CryptoRegisterWidget(conn, exchange)
    try:
        m = reg.model
        cols = [m.headerData(c, 1) for c in range(m.columnCount())]
        editable = [cols[c] for c in range(m.columnCount())
                    if m.flags(m.index(0, c)) & Qt.ItemIsEditable]
        # Everything but the two derived running balances (Coin Bal, Cash Bal).
        assert editable == ["Date", "Action", "Coin / Wallet", "Payee",
                            "Transfer", "Quantity", "Price", "Amount", "Fee",
                            "Memo"]
        assert m.setData(m.index(0, cols.index("Memo")), "from cold storage",
                         Qt.EditRole)
        m.reload()
    finally:
        reg.deleteLater()
    assert conn.execute("SELECT memo FROM crypto_transactions WHERE id=?",
                        (txn,)).fetchone()[0] == "from cold storage"


def test_naming_your_own_account_makes_a_real_transfer(qapp, conn, exchange):
    """The TRANSFER field names one of the user's own accounts, exactly as the
    cash register's category/transfer cell does -- a payee stays free text and
    does not resolve to an account. Coin moving between two accounts the user
    controls is not a disposal: it realizes no gain and its basis rides along, so
    this field performs the link rather than storing a string.

    And it must ADOPT the leg that already exists. A wallet-to-exchange move is
    normally recorded twice, once from each side's own export, so minting a
    mirror would leave the wallet short by the whole amount."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    wallet = crypto.create_account(conn, "Paper1 ETH",
                                   kind=crypto.CRYPTO_KIND_WALLET)
    crypto.record_wallet_debit(conn, wallet, "2021-03-01", "ETH", 2,
                               payee="0xdepositaddr")
    txn = crypto.record_income(conn, exchange, "2021-03-01", "RECEIVE", "ETH", 2,
                               320000, payee="an external account")
    for a in (wallet, exchange):
        crypto.rebuild_holdings(conn, a)

    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    # Brackets are accepted but not needed: this column holds nothing BUT
    # accounts, so there is no category to tell it apart from.
    # Bracketed, exactly as the cash register lists a transfer target.
    assert "[Paper1 ETH]" in m.transfer_choices()
    assert m.setData(m.index(0, cols.index("Transfer")), "Paper1 ETH", Qt.EditRole)
    m.reload()

    rows = list(conn.execute(
        "SELECT id, account_id, action, transfer_account_id, transfer_pair_id, "
        "amount FROM crypto_transactions ORDER BY id"))
    # TWO rows, not three: the wallet's existing send was adopted, not duplicated.
    assert len(rows) == 2
    out, inn = rows
    assert (out["action"], inn["action"]) == ("TRANSFER_OUT", "TRANSFER_IN")
    assert out["transfer_pair_id"] == inn["id"]
    assert inn["transfer_pair_id"] == out["id"]
    assert out["transfer_account_id"] == exchange
    assert inn["transfer_account_id"] == wallet
    # A transfer books no proceeds: the source's valuation was not money moving.
    assert out["amount"] is None and inn["amount"] is None

    # The link lives in its OWN column, and the COIN column goes back to naming
    # the coin -- it used to do both, which is why a transfer row could not state
    # its own symbol. The payee is untouched: it is not a link.
    assert m.data(m.index(0, cols.index("Transfer"))) == "[Paper1 ETH]"
    assert m.data(m.index(0, cols.index("Coin / Wallet"))) == "ETH"
    assert m.data(m.index(0, cols.index("Payee"))) == "an external account"
    wm = CryptoRegisterModel(conn, wallet)
    assert wm.data(wm.index(0, wm.column_index(CryptoRegisterModel.TRANSFER))) == "[Coinbase]"
    assert wm.data(wm.index(0, wm.column_index(CryptoRegisterModel.COIN))) == "ETH"


def test_an_unknown_account_name_is_refused_not_stored(qapp, conn, exchange):
    """A bracketed name that resolves to nothing must not be stored as dead text:
    it would read as a transfer that does not exist."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    txn = crypto.record_income(conn, exchange, "2021-03-01", "RECEIVE", "ETH", 2,
                               320000, payee="an external account")
    crypto.rebuild_holdings(conn, exchange)
    m = CryptoRegisterModel(conn, exchange)
    errors = []
    m.error.connect(errors.append)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert not m.setData(m.index(0, cols.index("Transfer")), "Nope", Qt.EditRole)
    assert errors and "No account named" in errors[0]
    assert conn.execute("SELECT transfer_pair_id FROM crypto_transactions "
                        "WHERE id=?", (txn,)).fetchone()[0] is None


def test_clearing_the_transfer_field_unlinks_but_keeps_both_rows(qapp, conn, exchange):
    """Withdrawing the CLAIM that two rows are one movement is not the same as
    saying the movements did not happen. Both rows stay."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    wallet = crypto.create_account(conn, "Paper1 ETH",
                                   kind=crypto.CRYPTO_KIND_WALLET)
    crypto.record_wallet_debit(conn, wallet, "2021-03-01", "ETH", 2)
    crypto.record_income(conn, exchange, "2021-03-01", "RECEIVE", "ETH", 2,
                         320000, payee="an external account")
    for a in (wallet, exchange):
        crypto.rebuild_holdings(conn, a)
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    m.setData(m.index(0, cols.index("Transfer")), "Paper1 ETH", Qt.EditRole)
    m.reload()
    m.setData(m.index(0, cols.index("Transfer")), "", Qt.EditRole)
    m.reload()
    rows = list(conn.execute(
        "SELECT action, transfer_pair_id, payee FROM crypto_transactions ORDER BY id"))
    assert len(rows) == 2                      # both movements survive
    assert all(r["transfer_pair_id"] is None for r in rows)
    assert [r["action"] for r in rows] == ["SEND", "RECEIVE"]
    # The payee was never the link, so clearing the link leaves it alone.
    assert rows[1]["payee"] == "an external account"


# ---------------------------------------------------------------------------
# 6. the GENERIC reader: infer the columns and the structure
# ---------------------------------------------------------------------------
PRO = Path(__file__).parent / "fixtures" / "exchange_pro_statement.csv"


def test_generic_reader_infers_columns_it_has_never_seen():
    """A venue-per-parser approach does not scale past the first three formats.
    The generic reader names each column by ROLE from header vocabulary and
    content -- here from a Coinbase Pro account statement, whose headers
    ('amount/balance unit', 'trade id', 'transfer id') appear in no other
    export."""
    from mammon.importers.crypto_tabular import read_records
    layout, recs = read_records(PRO.read_text(encoding="utf-8"))
    assert layout.roles["date"] == "time"
    assert layout.roles["asset"] == "amount/balance unit"
    assert layout.roles["quantity"] == "amount"
    assert layout.roles["group"] == "trade id"
    assert recs


def test_generic_reader_folds_legs_into_one_trade():
    """A leg-structured export puts one ASSET SIDE per row: a sale is three rows
    -- the coin leg, the cash leg and the fee -- sharing a trade id. Read as
    three events that is a disposal, an unrelated deposit and a mystery expense:
    three wrong entries from one right trade.

    The tell is structural, needing no vendor knowledge: rows sharing a group key
    carry DIFFERENT assets. A group key whose rows all name one asset is just an
    identifier, which is why the 2022-style file (a trade-id column but no
    trades) still reads as events."""
    from mammon.importers.crypto_tabular import read_records
    layout, recs = read_records(PRO.read_text(encoding="utf-8"))
    assert layout.structure == "legs"
    sells = [r for r in recs if r.action == "SELL"]
    assert len(sells) == 2                      # two trades, not six rows
    # Cash is NET of the fee. On this shape the fee is its own ROW, so the trade's
    # cash leg is the GROSS and the fee comes off it; an export that reports one
    # Total column has already netted it, which is why only this path subtracts.
    assert sorted(r.amount_cents for r in sells) == [497500, 796000]
    assert sorted(r.fee_cents for r in sells) == [2500, 4000]
    assert sum(Decimal(r.quantity) for r in sells) == Decimal("-10")


def test_generic_reader_reads_a_venue_transfer_as_a_transfer():
    """A type that NAMES A VENUE moves value between the user's own places, so it
    is a transfer whatever valuation the export prints beside it. Deciding on the
    presence of a dollar figure alone read 31 of the retail export's venue
    transfers as sales -- Coinbase prints a USD valuation on every row."""
    from mammon.importers.crypto_tabular import read_records
    _, recs = read_records(PRO.read_text(encoding="utf-8"))
    moves = [r for r in recs if r.action in ("TRANSFER_IN", "TRANSFER_OUT")]
    assert [r.symbol for r in moves] == ["ETH"]
    assert Decimal(moves[0].quantity) == Decimal("10")
    # ...while the FIAT withdrawals are cash leaving the sleeve, not transfers.
    cash_out = [r for r in recs if r.action == "WITHDRAW"]
    assert len(cash_out) == 2 and all(r.symbol == "" for r in cash_out)


def test_generic_reader_agrees_with_the_purpose_built_ones():
    """The known formats keep their proven parsers -- an inference never beats a
    parser that has been exercised. But where the generic reader CAN read them it
    must agree, or one of the two is wrong."""
    from mammon.importers.crypto_tabular import read_records
    from mammon.importers.crypto_csv import parse_etherscan
    text = ETHERSCAN.read_text(encoding="utf-8")
    _, generic = read_records(text)
    native = parse_etherscan(text)
    assert len(generic) == len(native)
    # The by-address export names its coin only in the HEADER (`Value_IN(ETH)`) --
    # there is no asset column to sniff -- so the reader takes it from there.
    assert {r.symbol for r in generic} == {"ETH"}
    net = sum(Decimal(r.quantity) for r in generic)
    assert net == sum((Decimal(r.quantity) if r.direction == "in"
                       else -Decimal(r.quantity)) for r in native)


def test_an_empty_year_is_an_empty_statement_not_an_error(tmp_path):
    """A year with no activity exports as a header and nothing else. Raising on
    it killed a nine-file import at the first quiet year."""
    from mammon.importers.crypto_tabular import read_records
    _, recs = read_records(
        "portfolio,type,time,amount,balance,amount/balance unit\n")
    assert recs == []


def test_the_pro_statement_imports_through_the_app(qapp, conn, exchange):
    """End to end: infer, review, accept -- and the books balance. Every dollar
    of the sales was withdrawn, and every coin deposited was sold."""
    win = MainWindow(conn)
    try:
        res = _import(win, exchange, PRO)
    finally:
        win.close()
    assert res["parsed"] == 5          # 2 trades + 1 deposit + 2 withdrawals
    import_review.accept_all(conn, exchange)
    assert crypto.crypto_cash(conn, exchange) == 0
    assert not crypto.list_holdings(conn, exchange)


# ---------------------------------------------------------------------------
# 7. the X-twins: a trade whose cash went somewhere else
# ---------------------------------------------------------------------------
def test_sellx_is_offered_and_means_the_cash_left(qapp, conn, exchange):
    """Mammon already speaks Quicken's ``SellX``/``BuyX`` on the investment side
    (``investments._CASH_ZERO_ACTIONS``): a trade whose cash arrives or leaves by
    TRANSFER instead of sitting in the account. The crypto register offered no
    such action, so a sale whose proceeds were wired to a bank could only be said
    in two half-statements.

    Setting the Transfer promotes the action on its own, and clearing it demotes
    -- one concept, never two states meaning the same thing."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    bank = ledger.create_account(conn, "America 1st Ck", "checking")
    crypto.record_buy(conn, exchange, "2017-01-01", "ETH", 1, 20000)
    sell = crypto.record_sell(conn, exchange, "2018-03-05", "ETH", 1, 84309)
    crypto.rebuild_holdings(conn, exchange)

    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    row = next(r for r in range(m.rowCount()) if m.txn_at(r)["id"] == sell)
    assert "SELLX" in m.actions_for_row(row)

    m.setData(m.index(row, cols.index("Transfer")), "[America 1st Ck]",
              Qt.EditRole)
    m.reload()
    assert m.data(m.index(row, cols.index("Action"))) == "SELLX"
    assert ledger.account_balance(conn, bank) == 84309
    # Still a real disposal: the gain is booked against the relieved basis.
    assert "SELLX" in crypto._DISPOSAL_ACTIONS

    m.setData(m.index(row, cols.index("Transfer")), "", Qt.EditRole)
    m.reload()
    assert m.data(m.index(row, cols.index("Action"))) == "SELL"
    assert ledger.account_balance(conn, bank) == 0


def test_a_coin_row_can_be_corrected_into_a_sale_either_way_round(qapp, conn,
                                                                  exchange):
    """A trade-with-a-transfer is ONE concept spread over two cells, and a
    register is edited one cell at a time.

    Enforcing completeness per cell deadlocked it: the X action was refused for
    want of a transfer, and the transfer refused for want of an amount, so an
    imported coin-only row could never be corrected at all. An incomplete state
    between two keystrokes is now allowed, and the writer fills in what it can --
    a row turning into a trade is seeded from the price the import recorded,
    rather than left at nothing for the user to be blocked on."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    bank = ledger.create_account(conn, "America 1st Ck", "checking")
    # Exactly the shape an older import produced: coin out, no proceeds.
    txn = crypto.record_event(conn, exchange, "2018-03-05", "SEND", symbol="ETH",
                              quantity=-1, price="859.70")
    crypto.rebuild_holdings(conn, exchange)
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]

    # Action first: the X action is ACCEPTED without a destination yet.
    assert m.setData(m.index(0, cols.index("Action")), "SELLX", Qt.EditRole)
    m.reload()
    assert m.data(m.index(0, cols.index("Action"))) == "SELLX"
    # ...and naming the destination completes it.
    assert m.setData(m.index(0, cols.index("Transfer")), "[America 1st Ck]",
                     Qt.EditRole)
    m.reload()
    assert ledger.account_balance(conn, bank) == 85970      # seeded from price

    # Correcting the amount MOVES the cash leg rather than adding a second one.
    assert m.setData(m.index(0, cols.index("Amount")), "843.09", Qt.EditRole)
    m.reload()
    assert m.data(m.index(0, cols.index("Amount"))) == "843.09"
    assert ledger.account_balance(conn, bank) == 84309
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_the_amount_column_shows_the_trade_not_the_sleeve_effect(qapp, conn,
                                                                 exchange):
    """An X row's proceeds went straight out, so its effect on THIS account's
    cash is zero -- but the sale still happened for a sum. Rendering the sleeve
    effect made a SELLX look like it sold for nothing."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    ledger.create_account(conn, "America 1st Ck", "checking")
    crypto.record_sell(conn, exchange, "2018-03-05", "ETH", 1, 84309)
    crypto.rebuild_holdings(conn, exchange)
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    m.setData(m.index(0, cols.index("Transfer")), "[America 1st Ck]", Qt.EditRole)
    m.reload()
    assert m.data(m.index(0, cols.index("Action"))) == "SELLX"
    assert m.data(m.index(0, cols.index("Amount"))) == "843.09"


# ---------------------------------------------------------------------------
# 8. two crypto accounts move BOTH coin and cash between them
# ---------------------------------------------------------------------------
def test_cash_transfers_between_two_crypto_accounts(qapp, conn, exchange):
    """An exchange venue and its retail front are BOTH crypto accounts, and both
    have cash sleeves, so dollars move between them as readily as coin does.

    Linking only knew two cases -- a coin mirror between crypto accounts, or a
    fiat leg out to a CASH account -- so a dollar row between two crypto accounts
    hit neither and was refused as "only a row that moves coin can become a
    transfer". Left unpaired it reads as a withdrawal to nowhere and a deposit
    from nowhere."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    # Each export records its own half, exactly as the real files do.
    crypto.record_cash(conn, pro, "2021-10-21", -91025, memo="withdrawal")
    crypto.record_cash(conn, exchange, "2021-10-21", 91025,
                       memo="Exchange Withdrawal")
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert m.setData(m.index(0, cols.index("Transfer")), "[Coinbase Pro]",
                     Qt.EditRole)
    m.reload()
    assert m.data(m.index(0, cols.index("Transfer"))) == "[Coinbase Pro]"
    # The other side's own row was ADOPTED, not duplicated.
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions").fetchone()[0] == 2
    # A cash leg keeps DEPOSIT/WITHDRAW -- there is no position to open, so the
    # coin transfer actions would be wrong here -- and the pair nets to zero.
    assert [r[0] for r in conn.execute(
        "SELECT action FROM crypto_transactions ORDER BY account_id")] == \
        ["DEPOSIT", "WITHDRAW"]
    assert (crypto.crypto_cash(conn, exchange)
            + crypto.crypto_cash(conn, pro)) == 0


def test_coin_transfers_between_two_crypto_accounts(qapp, conn, exchange):
    """The coin direction between the same two accounts, for symmetry: the
    counter-leg the other export already recorded is adopted, so the coin is not
    counted twice."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    crypto.record_event(conn, exchange, "2021-12-31", "TRANSFER_OUT",
                        symbol="ETH", quantity="-0.09920726")
    crypto.record_event(conn, pro, "2021-12-31", "TRANSFER_IN",
                        symbol="ETH", quantity="0.09920726")
    for a in (exchange, pro):
        crypto.rebuild_holdings(conn, a)
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert m.setData(m.index(0, cols.index("Transfer")), "[Coinbase Pro]",
                     Qt.EditRole)
    m.reload()
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions").fetchone()[0] == 2
    pm = CryptoRegisterModel(conn, pro)
    assert pm.data(pm.index(0, pm.column_index(CryptoRegisterModel.TRANSFER))) \
        == "[Coinbase]"


def test_unlinking_a_cash_pair_keeps_its_cash_actions(qapp, conn, exchange):
    """Withdrawing the claim that two rows are one movement must not rewrite what
    each row IS. A coin transfer becomes a plain send/receive; a cash leg keeps
    DEPOSIT/WITHDRAW, because money still moved in or out of the sleeve."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    crypto.record_cash(conn, pro, "2021-10-21", -91025)
    crypto.record_cash(conn, exchange, "2021-10-21", 91025)
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    m.setData(m.index(0, cols.index("Transfer")), "[Coinbase Pro]", Qt.EditRole)
    m.reload()
    m.setData(m.index(0, cols.index("Transfer")), "", Qt.EditRole)
    m.reload()
    assert sorted(r[0] for r in conn.execute(
        "SELECT action FROM crypto_transactions")) == ["DEPOSIT", "WITHDRAW"]
    assert all(r[0] is None for r in conn.execute(
        "SELECT transfer_pair_id FROM crypto_transactions"))


def test_a_transfer_leg_matches_within_this_register(qapp, conn, exchange):
    """Matching is against THIS ACCOUNT'S REGISTER, never another account's.

    That is the cash model and it is not incidental: ``_find_match`` queries
    ``WHERE account_id=?``. The counterpart is expected to be here ALREADY,
    because accepting the other side with a transfer target created it --
    ``save_new`` routes such a row through ``ledger.create_transfer``, which
    writes both legs, and ``crypto.link_as_transfer`` does the same for coin.

    So this is rule 2 of ``_find_match`` in coin: a transfer-shaped row matches an
    existing leg of a double-entry transfer in this register, and accepting
    CLEARS that leg instead of inserting a second one-sided row."""
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    # The other side was accepted WITH a transfer target, which wrote Pro's legs.
    cash = crypto.record_cash(conn, exchange, "2021-10-21", 91025)
    coin = crypto.record_event(conn, exchange, "2021-12-31", "TRANSFER_OUT",
                               symbol="ETH", quantity="-0.09920726")
    crypto.link_as_transfer(conn, cash, pro)
    crypto.link_as_transfer(conn, coin, pro)
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (pro,)).fetchone()[0] == 2                 # the mirrors are HERE now

    entries = import_review.build_exchange_review(conn, pro, [
        _rec("p1", "2021-10-21", "WITHDRAW", "", "-910.25", amount=-91025),
        _rec("p2", "2021-12-31", "TRANSFER_IN", "ETH", "0.09920726"),
        _rec("p3", "2021-06-01", "SELL", "ETH", "-1", amount=200000),
    ])
    assert {e.mapped.tx_hash: e.label for e in entries} == {
        "p1": import_review.LABEL_MATCHING,
        "p2": import_review.LABEL_MATCHING,
        "p3": import_review.LABEL_NEW}

    import_review.persist_entries(conn, pro, entries)
    import_review.accept_all(conn, pro)
    # Accepting a MATCH adds NOTHING -- the leg was already here. Only the SELL
    # created a row. Inserting one would be the double-entry this prevents.
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (pro,)).fetchone()[0] == 3
    # ...and the source id is stamped onto the leg it matched.
    assert conn.execute(
        "SELECT tx_hash FROM crypto_transactions WHERE account_id=? AND "
        "symbol IS NULL", (pro,)).fetchone()[0] == "p1"


def test_no_counterpart_here_means_NEW(qapp, conn, exchange):
    """With nothing in this register to match, every row is NEW -- even when
    another account holds the other half. Reaching into other accounts is not
    what the cash review does, and a crypto register must not invent its own
    rule."""
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    # Present in the OTHER account, but never linked, so no leg exists here.
    crypto.record_cash(conn, exchange, "2021-10-21", 91025)
    entries = import_review.build_exchange_review(conn, pro, [
        _rec("p1", "2021-10-21", "WITHDRAW", "", "-910.25", amount=-91025)])
    assert entries[0].label == import_review.LABEL_NEW


def _rec(txn_id, date, action, symbol, quantity, amount=0):
    from mammon.importers.coinbase_csv import ExchangeRecord
    return ExchangeRecord(txn_id=txn_id, date=date, action=action, symbol=symbol,
                          quantity=quantity, amount_cents=amount)


# ---------------------------------------------------------------------------
# 8. two crypto accounts move BOTH coin and cash between them
# ---------------------------------------------------------------------------
def test_cash_transfers_between_two_crypto_accounts(qapp, conn, exchange):
    """An exchange venue and its retail front are BOTH crypto accounts, and both
    have cash sleeves, so dollars move between them as readily as coin does.

    Linking only knew two cases -- a coin mirror between crypto accounts, or a
    fiat leg out to a CASH account -- so a dollar row between two crypto accounts
    hit neither and was refused as "only a row that moves coin can become a
    transfer". Left unpaired it reads as a withdrawal to nowhere and a deposit
    from nowhere."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    # Each export records its own half, exactly as the real files do.
    crypto.record_cash(conn, pro, "2021-10-21", -91025, memo="withdrawal")
    crypto.record_cash(conn, exchange, "2021-10-21", 91025,
                       memo="Exchange Withdrawal")
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert m.setData(m.index(0, cols.index("Transfer")), "[Coinbase Pro]",
                     Qt.EditRole)
    m.reload()
    assert m.data(m.index(0, cols.index("Transfer"))) == "[Coinbase Pro]"
    # The other side's own row was ADOPTED, not duplicated.
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions").fetchone()[0] == 2
    # A cash leg keeps DEPOSIT/WITHDRAW -- there is no position to open, so the
    # coin transfer actions would be wrong here -- and the pair nets to zero.
    assert [r[0] for r in conn.execute(
        "SELECT action FROM crypto_transactions ORDER BY account_id")] == \
        ["DEPOSIT", "WITHDRAW"]
    assert (crypto.crypto_cash(conn, exchange)
            + crypto.crypto_cash(conn, pro)) == 0


def test_coin_transfers_between_two_crypto_accounts(qapp, conn, exchange):
    """The coin direction between the same two accounts, for symmetry: the
    counter-leg the other export already recorded is adopted, so the coin is not
    counted twice."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    crypto.record_event(conn, exchange, "2021-12-31", "TRANSFER_OUT",
                        symbol="ETH", quantity="-0.09920726")
    crypto.record_event(conn, pro, "2021-12-31", "TRANSFER_IN",
                        symbol="ETH", quantity="0.09920726")
    for a in (exchange, pro):
        crypto.rebuild_holdings(conn, a)
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert m.setData(m.index(0, cols.index("Transfer")), "[Coinbase Pro]",
                     Qt.EditRole)
    m.reload()
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions").fetchone()[0] == 2
    pm = CryptoRegisterModel(conn, pro)
    assert pm.data(pm.index(0, pm.column_index(CryptoRegisterModel.TRANSFER))) \
        == "[Coinbase]"


def test_unlinking_a_cash_pair_keeps_its_cash_actions(qapp, conn, exchange):
    """Withdrawing the claim that two rows are one movement must not rewrite what
    each row IS. A coin transfer becomes a plain send/receive; a cash leg keeps
    DEPOSIT/WITHDRAW, because money still moved in or out of the sleeve."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    crypto.record_cash(conn, pro, "2021-10-21", -91025)
    crypto.record_cash(conn, exchange, "2021-10-21", 91025)
    m = CryptoRegisterModel(conn, exchange)
    cols = [m.headerData(c, 1) for c in range(m.columnCount())]
    m.setData(m.index(0, cols.index("Transfer")), "[Coinbase Pro]", Qt.EditRole)
    m.reload()
    m.setData(m.index(0, cols.index("Transfer")), "", Qt.EditRole)
    m.reload()
    assert sorted(r[0] for r in conn.execute(
        "SELECT action FROM crypto_transactions")) == ["DEPOSIT", "WITHDRAW"]
    assert all(r[0] is None for r in conn.execute(
        "SELECT transfer_pair_id FROM crypto_transactions"))


def test_a_transfer_leg_matches_within_this_register(qapp, conn, exchange):
    """Matching is against THIS ACCOUNT'S REGISTER, never another account's.

    That is the cash model and it is not incidental: ``_find_match`` queries
    ``WHERE account_id=?``. The counterpart is expected to be here ALREADY,
    because accepting the other side with a transfer target created it --
    ``save_new`` routes such a row through ``ledger.create_transfer``, which
    writes both legs, and ``crypto.link_as_transfer`` does the same for coin.

    So this is rule 2 of ``_find_match`` in coin: a transfer-shaped row matches an
    existing leg of a double-entry transfer in this register, and accepting
    CLEARS that leg instead of inserting a second one-sided row."""
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    # The other side was accepted WITH a transfer target, which wrote Pro's legs.
    cash = crypto.record_cash(conn, exchange, "2021-10-21", 91025)
    coin = crypto.record_event(conn, exchange, "2021-12-31", "TRANSFER_OUT",
                               symbol="ETH", quantity="-0.09920726")
    crypto.link_as_transfer(conn, cash, pro)
    crypto.link_as_transfer(conn, coin, pro)
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (pro,)).fetchone()[0] == 2                 # the mirrors are HERE now

    entries = import_review.build_exchange_review(conn, pro, [
        _rec("p1", "2021-10-21", "WITHDRAW", "", "-910.25", amount=-91025),
        _rec("p2", "2021-12-31", "TRANSFER_IN", "ETH", "0.09920726"),
        _rec("p3", "2021-06-01", "SELL", "ETH", "-1", amount=200000),
    ])
    assert {e.mapped.tx_hash: e.label for e in entries} == {
        "p1": import_review.LABEL_MATCHING,
        "p2": import_review.LABEL_MATCHING,
        "p3": import_review.LABEL_NEW}

    import_review.persist_entries(conn, pro, entries)
    import_review.accept_all(conn, pro)
    # Accepting a MATCH adds NOTHING -- the leg was already here. Only the SELL
    # created a row. Inserting one would be the double-entry this prevents.
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (pro,)).fetchone()[0] == 3
    # ...and the source id is stamped onto the leg it matched.
    assert conn.execute(
        "SELECT tx_hash FROM crypto_transactions WHERE account_id=? AND "
        "symbol IS NULL", (pro,)).fetchone()[0] == "p1"


def test_no_counterpart_here_means_NEW(qapp, conn, exchange):
    """With nothing in this register to match, every row is NEW -- even when
    another account holds the other half. Reaching into other accounts is not
    what the cash review does, and a crypto register must not invent its own
    rule."""
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    # Present in the OTHER account, but never linked, so no leg exists here.
    crypto.record_cash(conn, exchange, "2021-10-21", 91025)
    entries = import_review.build_exchange_review(conn, pro, [
        _rec("p1", "2021-10-21", "WITHDRAW", "", "-910.25", amount=-91025)])
    assert entries[0].label == import_review.LABEL_NEW


def _rec(txn_id, date, action, symbol, quantity, amount=0):
    from mammon.importers.coinbase_csv import ExchangeRecord
    return ExchangeRecord(txn_id=txn_id, date=date, action=action, symbol=symbol,
                          quantity=quantity, amount_cents=amount)


def test_a_transfer_candidate_must_be_unambiguous(qapp, conn, exchange):
    """Only when exactly ONE other account holds a matching leg. Two candidates
    is not a near-miss to be broken by a tiebreak -- welding two unrelated
    movements together is worse than offering nothing and letting the user say."""
    pro = crypto.create_account(conn, "Coinbase Pro",
                                kind=crypto.CRYPTO_KIND_EXCHANGE)
    other = crypto.create_account(conn, "Kraken",
                                  kind=crypto.CRYPTO_KIND_EXCHANGE)
    for acct in (exchange, other):
        crypto.record_cash(conn, acct, "2021-10-21", 91025)
    assert crypto.find_transfer_candidate(
        conn, pro, "2021-10-21", amount=-91025) is None
    # ...and unambiguous again once one of them is spoken for.
    conn.execute("UPDATE crypto_transactions SET transfer_account_id=? "
                 "WHERE account_id=?", (pro, other))
    conn.commit()
    hit = crypto.find_transfer_candidate(conn, pro, "2021-10-21", amount=-91025)
    assert hit is not None and hit[1] == "Coinbase"


def test_a_trade_never_matches_a_transfer(qapp, conn, exchange):
    """A sale has no counterpart; only a transfer-shaped row is a candidate.
    Matching one to a same-sized movement would weld two unrelated events
    together."""
    crypto.record_cash(conn, exchange, "2021-06-01", 200000)
    entries = import_review.build_exchange_review(conn, exchange, [
        _rec("p3", "2021-06-01", "SELL", "ETH", "-1", amount=200000)])
    assert entries[0].label == import_review.LABEL_NEW
