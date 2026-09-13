"""A crypto WITHDRAW linked to a bank account must land there as a DEPOSIT.

The reported defect: withdrawals from a Coinbase exchange to a checking account
showed correctly negative on the Coinbase side, but the mirrored leg in the
checking register was ALSO negative -- it rendered in the Payment column when
the money was arriving and belonged in Deposit. SellX rows mirrored correctly,
which is the clue: both shapes go through ``crypto._link_cash_leg``, and it read
the sign of ``crypto_transactions.amount`` the same way for both.

Root cause: a trade and a cash move mean OPPOSITE things by that sign.

  * A TRADE's cash is CREATED by the trade. ``amount`` is what the disposal
    realized (+) or what the purchase cost (-). A +ve SELL means the exchange
    now holds proceeds, and linking says they were wired OUT to the bank.
  * A DEPOSIT/WITHDRAW's cash is MOVED, not created. ``amount`` is already this
    side's sleeve delta -- a WITHDRAW is -ve because money LEFT the exchange --
    and the linked account is simply the other end of the same move. So the
    direction is the mirror of the trade rule.

Reading it the trade way sent a -ve WITHDRAW down the "purchase funded from the
bank" branch, making checking the FROM leg and so negative on both sides.
``crypto._link_cash_mirror`` (crypto <-> crypto) already negated correctly; the
fix is that same rule for an ordinary cash account, applied at the mirror-
creation boundary in the domain layer -- the register's columns are a faithful
projection of the sign and were never wrong.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import crypto, db, ledger

MOVE = 250000        # $2,500.00 -- the amount that crosses, in cents


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "crypto_transfer_sign.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    """A crypto EXCHANGE plus an ordinary checking account. Synthetic names."""
    ex = crypto.create_account(conn, "Coin Exchange",
                               kind=crypto.CRYPTO_KIND_EXCHANGE)
    bank = ledger.create_account(conn, "Everyday Checking", "checking")
    return ex, bank


def _ordinary_rows(conn, account_id):
    """The ordinary ``transactions`` rows on an account, oldest id first."""
    return conn.execute(
        "SELECT * FROM transactions WHERE account_id=? ORDER BY id",
        (account_id,)).fetchall()


def _sole_amount(conn, account_id) -> int:
    rows = _ordinary_rows(conn, account_id)
    assert len(rows) == 1, f"expected one leg on {account_id}, got {len(rows)}"
    return int(rows[0]["amount"])


# --------------------------------------------------------------------------
# The defect: a withdrawal OUT of the exchange, INTO checking.
# --------------------------------------------------------------------------

def test_withdrawal_to_checking_is_a_deposit_there(conn, accounts):
    ex, bank = accounts
    crypto.record_cash(conn, ex, "2024-03-01", 500000)          # fund the sleeve
    wd = crypto.record_cash(conn, ex, "2024-03-10", -MOVE)      # money leaves
    assert crypto._norm(crypto.get_event(conn, wd)["action"]) == "WITHDRAW"

    crypto.link_as_transfer(conn, wd, bank)

    # The checking leg is POSITIVE: incoming cash, the Deposit column.
    assert _sole_amount(conn, bank) == MOVE
    assert ledger.account_balance(conn, bank) == MOVE

    # ...and the crypto side stays negative, both on its crypto row and on the
    # ordinary mirror leg the transfer writes there.
    assert int(crypto.get_event(conn, wd)["amount"]) == -MOVE
    assert _sole_amount(conn, ex) == -MOVE


def test_withdrawal_legs_are_equal_and_opposite_and_paired(conn, accounts):
    """The CLAUDE.md transfer invariant, on this path: two rows, equal and
    opposite, cross-linked, each naming the other account."""
    ex, bank = accounts
    wd = crypto.record_cash(conn, ex, "2024-03-10", -MOVE)
    crypto.link_as_transfer(conn, wd, bank)

    crypto_leg = _ordinary_rows(conn, ex)[0]
    bank_leg = _ordinary_rows(conn, bank)[0]
    assert crypto_leg["amount"] == -bank_leg["amount"]
    assert crypto_leg["transfer_pair_id"] == bank_leg["id"]
    assert bank_leg["transfer_pair_id"] == crypto_leg["id"]
    assert int(crypto_leg["transfer_account_id"]) == bank
    assert int(bank_leg["transfer_account_id"]) == ex
    # The crypto row itself points at the cash account it was linked to.
    assert int(crypto.get_event(conn, wd)["transfer_account_id"]) == bank


def test_deposit_from_checking_is_a_payment_there(conn, accounts):
    """The withdrawal's mirror image: money going INTO the exchange must leave
    checking, so the checking leg is negative (the Payment column)."""
    ex, bank = accounts
    dep = crypto.record_cash(conn, ex, "2024-03-10", MOVE)
    assert crypto._norm(crypto.get_event(conn, dep)["action"]) == "DEPOSIT"

    crypto.link_as_transfer(conn, dep, bank)

    assert _sole_amount(conn, bank) == -MOVE
    assert _sole_amount(conn, ex) == MOVE
    assert int(crypto.get_event(conn, dep)["amount"]) == MOVE


def test_withdrawal_still_leaves_the_exchange_cash_sleeve(conn, accounts):
    """Linking must not change what the exchange holds: the sleeve is the
    register's Cash Bal, and the withdrawal really did remove the money."""
    ex, bank = accounts
    crypto.record_cash(conn, ex, "2024-03-01", 500000)
    wd = crypto.record_cash(conn, ex, "2024-03-10", -MOVE)
    crypto.link_as_transfer(conn, wd, bank)

    assert crypto.crypto_cash(conn, ex) == 500000 - MOVE
    assert crypto.register_rows(conn, ex)[-1]["cash_bal"] == 500000 - MOVE
    # ...and the valuation agrees with the register (it ignores the ordinary
    # mirror leg the transfer wrote on the crypto account).
    assert crypto.account_valuation(conn, ex, prices={}).cash == 500000 - MOVE


# --------------------------------------------------------------------------
# No regression: the SellX / BuyX shapes that were already correct.
# --------------------------------------------------------------------------

def test_sellx_proceeds_still_deposit_into_checking(conn, accounts):
    ex, bank = accounts
    crypto.record_cash(conn, ex, "2024-01-01", 500000)
    crypto.record_buy(conn, ex, "2024-01-02", "ETH", 2, 400000)
    sell = crypto.record_sell(conn, ex, "2024-02-01", "ETH", 1, MOVE)
    crypto.link_as_transfer(conn, sell, bank)

    # Proceeds were wired to the bank: positive there, negative on the mirror.
    assert _sole_amount(conn, bank) == MOVE
    assert ledger.account_balance(conn, bank) == MOVE
    assert _sole_amount(conn, ex) == -MOVE
    assert crypto._norm(crypto.get_event(conn, sell)["action"]) == "SELLX"
    # The proceeds never touched the exchange's sleeve.
    assert crypto.crypto_cash(conn, ex) == 500000 - 400000


def test_buyx_still_pays_out_of_checking(conn, accounts):
    ex, bank = accounts
    buy = crypto.record_buy(conn, ex, "2024-01-02", "ETH", 1, MOVE)
    crypto.link_as_transfer(conn, buy, bank)

    # The purchase was funded from the bank: negative there, positive mirror.
    assert _sole_amount(conn, bank) == -MOVE
    assert _sole_amount(conn, ex) == MOVE
    assert crypto._norm(crypto.get_event(conn, buy)["action"]) == "BUYX"
    assert crypto.crypto_cash(conn, ex) == 0
