"""Linking a crypto cash row to an ordinary account must stay a ONE-row affair.

Reported after the WITHDRAW sign fix: refreshing the transfers on a Coinbase
withdrawal grew the checking register by one 12/16/2020 transfer per refresh --
four of them -- against a single counterparty row in Coinbase.

Two defects, both on the crypto <-> ORDINARY-account cash path:

  (a) :func:`crypto.link_as_transfer` refused an already-linked row only when
      ``transfer_pair_id`` was set. A cash leg never sets it -- its other half
      lives in ``transactions``, so there is no crypto id to pair with -- so the
      guard never fired and ``_link_cash_leg`` minted another
      :func:`ledger.create_transfer` pair on every call while the crypto row
      stayed single. Hence four bank rows against one counterparty.

  (b) ``_unlink_cash_leg`` found the row to delete with ``AND amount=?`` using
      the SIGNED crypto amount. The sign fix made a WITHDRAW's bank leg POSITIVE
      while the crypto row stayed negative, so the lookup missed: nothing was
      deleted, the link was cleared anyway, and the bank row was orphaned.
      Before the sign fix the two signs agreed and the delete found its row,
      which is why this surfaced only afterwards -- that fix was correct, and
      exposed this one.

The two compound: the Transfer cell re-targets by unlinking and then linking
(``CryptoRegisterModel._write_transfer``), so each pass orphaned one row and
minted another.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import crypto, db, ledger
from mammon.tests import fresh_db

MOVE = 250000        # $2,500.00 -- the amount that crosses, in cents


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "crypto_transfer_relink.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    """A crypto EXCHANGE plus an ordinary checking account. Synthetic names."""
    ex = crypto.create_account(conn, "Coin Exchange",
                               kind=crypto.CRYPTO_KIND_EXCHANGE)
    bank = ledger.create_account(conn, "Everyday Checking", "checking")
    return ex, bank


def _rows(conn, account_id):
    """Ordinary ``transactions`` rows on an account, oldest id first."""
    return conn.execute(
        "SELECT * FROM transactions WHERE account_id=? ORDER BY id",
        (account_id,)).fetchall()


def _retarget(conn, txn_id, other):
    """What ``CryptoRegisterModel._write_transfer`` does for the Transfer cell:
    withdraw the old claim, then make the new one. This is the "refresh"."""
    row = crypto.get_event(conn, txn_id)
    if (row["transfer_pair_id"] is not None
            or row["transfer_account_id"] is not None):
        crypto.unlink_transfer(conn, txn_id)
    crypto.link_as_transfer(conn, txn_id, other)


# --------------------------------------------------------------------------
# (a) Re-linking a row that is already linked
# --------------------------------------------------------------------------

def test_relinking_a_linked_withdrawal_is_refused(conn, accounts):
    ex, bank = accounts
    wd = crypto.record_cash(conn, ex, "2020-12-16", -MOVE)
    crypto.link_as_transfer(conn, wd, bank)

    with pytest.raises(ValueError, match="already one leg of a transfer"):
        crypto.link_as_transfer(conn, wd, bank)
    assert len(_rows(conn, bank)) == 1


def test_relinking_a_linked_sellx_is_refused(conn, accounts):
    """A trade's cash link sets `transfer_account_id` alone too, so it was
    equally re-linkable."""
    ex, bank = accounts
    crypto.record_cash(conn, ex, "2020-01-01", 500000)
    crypto.record_buy(conn, ex, "2020-01-02", "ETH", 2, 400000)
    sell = crypto.record_sell(conn, ex, "2020-02-01", "ETH", 1, MOVE)
    crypto.link_as_transfer(conn, sell, bank)

    with pytest.raises(ValueError, match="already one leg of a transfer"):
        crypto.link_as_transfer(conn, sell, bank)
    assert len(_rows(conn, bank)) == 1


def test_refreshing_a_withdrawal_transfer_never_multiplies_it(conn, accounts):
    """The reported shape: four refreshes, four rows in checking, one counter-
    party in the exchange. One refresh, one row -- however many times it runs."""
    ex, bank = accounts
    crypto.record_cash(conn, ex, "2020-12-01", 500000)
    wd = crypto.record_cash(conn, ex, "2020-12-16", -MOVE)

    for attempt in range(1, 5):
        _retarget(conn, wd, bank)
        rows = _rows(conn, bank)
        assert len(rows) == 1, f"{len(rows)} checking rows after refresh {attempt}"
        # Money ARRIVING in checking: positive, the Deposit column.
        assert int(rows[0]["amount"]) == MOVE
        # ...and exactly one mirror leg on the exchange side.
        assert len(_rows(conn, ex)) == 1


# --------------------------------------------------------------------------
# (b) Unlinking must remove the row the link created
# --------------------------------------------------------------------------

def test_unlinking_a_withdrawal_removes_its_bank_row(conn, accounts):
    ex, bank = accounts
    wd = crypto.record_cash(conn, ex, "2020-12-16", -MOVE)
    crypto.link_as_transfer(conn, wd, bank)
    assert len(_rows(conn, bank)) == 1

    crypto.unlink_transfer(conn, wd)

    assert _rows(conn, bank) == []
    assert _rows(conn, ex) == []
    assert crypto.get_event(conn, wd)["transfer_account_id"] is None
    # The money is back in the exchange's own sleeve, where an unlinked
    # withdrawal leaves it.
    assert int(crypto.get_event(conn, wd)["amount"]) == -MOVE


def test_unlinking_a_deposit_removes_its_bank_row(conn, accounts):
    """The other half of the cash pair, whose signs are reversed again."""
    ex, bank = accounts
    dep = crypto.record_cash(conn, ex, "2020-12-16", MOVE)
    crypto.link_as_transfer(conn, dep, bank)
    assert int(_rows(conn, bank)[0]["amount"]) == -MOVE

    crypto.unlink_transfer(conn, dep)

    assert _rows(conn, bank) == []
    assert _rows(conn, ex) == []


def test_unlinking_a_sellx_still_removes_its_bank_row(conn, accounts):
    """The control: a trade's signs already agreed, and must keep working."""
    ex, bank = accounts
    crypto.record_cash(conn, ex, "2020-01-01", 500000)
    crypto.record_buy(conn, ex, "2020-01-02", "ETH", 2, 400000)
    sell = crypto.record_sell(conn, ex, "2020-02-01", "ETH", 1, MOVE)
    crypto.link_as_transfer(conn, sell, bank)

    crypto.unlink_transfer(conn, sell)

    assert _rows(conn, bank) == []
    assert _rows(conn, ex) == []
    assert crypto._norm(crypto.get_event(conn, sell)["action"]) == "SELL"


def test_editing_the_amount_of_a_linked_withdrawal_keeps_one_bank_row(
        conn, accounts):
    """``_write_posted``'s AMOUNT branch unlinks, rewrites, then re-links. That
    guard only works if the unlink really removes the old leg."""
    ex, bank = accounts
    crypto.record_cash(conn, ex, "2020-12-01", 500000)
    wd = crypto.record_cash(conn, ex, "2020-12-16", -MOVE)
    crypto.link_as_transfer(conn, wd, bank)

    other = crypto.get_event(conn, wd)["transfer_account_id"]
    crypto.unlink_transfer(conn, wd)
    crypto.update_event(conn, wd, amount=-300000)
    crypto.link_as_transfer(conn, wd, int(other))

    rows = _rows(conn, bank)
    assert len(rows) == 1
    assert int(rows[0]["amount"]) == 300000
    assert ledger.account_balance(conn, bank) == 300000
