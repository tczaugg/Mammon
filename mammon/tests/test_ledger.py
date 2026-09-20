"""Tests for mammon.ledger: ordinary ledger ops, balances, and -- the first
priority -- classic mirror transfers."""
from __future__ import annotations

import pytest

from mammon import db, ledger


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    checking = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    savings = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return checking, savings


# ---- ordinary ledger --------------------------------------------------------
def test_add_and_balance(conn, accounts):
    checking, _ = accounts
    ledger.add_transaction(conn, checking, "2026-01-05", -25_00, payee="Safeway")
    ledger.add_transaction(conn, checking, "2026-01-10", 200_00, payee="Paycheck")
    assert ledger.account_balance(conn, checking) == 100_00 - 25_00 + 200_00


def test_balance_as_of(conn, accounts):
    checking, _ = accounts
    ledger.add_transaction(conn, checking, "2026-01-05", -25_00)
    ledger.add_transaction(conn, checking, "2026-02-01", -10_00)
    assert ledger.account_balance(conn, checking, as_of="2026-01-31") == 100_00 - 25_00


def test_register_running_balance(conn, accounts):
    checking, _ = accounts
    ledger.add_transaction(conn, checking, "2026-01-10", 50_00)
    ledger.add_transaction(conn, checking, "2026-01-05", -20_00)  # earlier date, added later
    rows = ledger.register_rows(conn, checking)
    # ordered by date: -20 first, then +50
    assert [r["balance"] for r in rows] == [80_00, 130_00]


# ---- same-day order: cash balance high to low (SRD 5.1b) ---------------------
def test_same_day_rows_show_money_arriving_before_it_is_spent(conn, accounts):
    """Entered in the worst order: two payments, then the transfer that funds
    them. The register shows the deposit first and the larger payment last, so
    the running balance never dips below what the day ends at."""
    checking, savings = accounts
    ledger.add_transaction(conn, checking, "2026-02-02", -80_00, payee="small")
    ledger.add_transaction(conn, checking, "2026-02-02", -300_00, payee="large")
    ledger.create_transfer(conn, savings, checking, "2026-02-02", 500_00)
    ledger.add_transaction(conn, checking, "2026-02-01", -10_00, payee="day before")
    rows = ledger.register_rows(conn, checking)
    assert [r["amount"] for r in rows] == [-10_00, 500_00, -80_00, -300_00]
    assert [r["balance"] for r in rows] == [90_00, 590_00, 510_00, 210_00]


def test_equal_same_day_amounts_keep_entry_order(conn, accounts):
    checking, _ = accounts
    first = ledger.add_transaction(conn, checking, "2026-02-02", -4_99, payee="first")
    second = ledger.add_transaction(conn, checking, "2026-02-02", -4_99, payee="second")
    assert [r["id"] for r in ledger.register_rows(conn, checking)] == [first, second]


# ---- opening balance counts from its date -----------------------------------
# Quicken's balances before an account's opening date do not include its opening
# balance. Adding it for every date made a mortgage opened in 2002 appear, whole,
# in a 2000 balance -- and in every net-worth figure before it existed.
@pytest.fixture
def loan(conn):
    return ledger.create_account(conn, "Mortgage", "liability",
                                 opening_balance=-150_000_00, opening_date="2002-08-14")


def test_an_opening_balance_is_not_there_before_its_date(conn, loan):
    assert ledger.account_balance(conn, loan, as_of="2000-12-31") == 0
    assert ledger.account_balance(conn, loan, as_of="2002-08-13") == 0
    assert ledger.account_balance(conn, loan, as_of="2002-08-14") == -150_000_00
    assert ledger.account_balance(conn, loan) == -150_000_00


def test_an_account_with_no_opening_date_keeps_it_from_the_start(conn, accounts):
    checking, _ = accounts
    assert ledger.account_balance(conn, checking, as_of="1990-01-01") == 100_00


def test_year_end_snapshots_agree_with_the_full_sum_around_an_opening_date(conn, loan):
    """Transactions before the opening date, the opening date in a year with no
    transactions of its own, then later years: every snapshot-backed balance must
    equal the from-inception sum."""
    ledger.add_transaction(conn, loan, "2001-03-01", -500_00)
    ledger.add_transaction(conn, loan, "2003-01-08", 1_000_00)
    ledger.add_transaction(conn, loan, "2004-05-01", 2_000_00)
    ledger.rebuild_checkpoints(conn, loan)
    for as_of in ("2001-12-31", "2002-08-13", "2002-08-14", "2002-12-31", "2003-06-30",
                  "2004-12-31", "2030-01-01"):
        assert ledger.account_balance(conn, loan, as_of) == \
            ledger._account_balance_full(conn, loan, as_of), as_of
    assert ledger.account_balance(conn, loan, "2002-12-31") == -150_000_00 - 500_00

    ledger.add_transaction(conn, loan, "2001-06-01", -100_00)      # a back-dated edit
    ledger.recompute_checkpoints_from_year(conn, loan, 2001)
    for as_of in ("2001-12-31", "2002-12-31", "2004-12-31"):
        assert ledger.account_balance(conn, loan, as_of) == \
            ledger._account_balance_full(conn, loan, as_of), as_of


def test_the_register_running_balance_picks_up_the_opening_balance_on_its_date(conn, loan):
    ledger.add_transaction(conn, loan, "2001-03-01", -500_00)
    ledger.add_transaction(conn, loan, "2003-01-08", 1_000_00)
    rows = ledger.register_rows(conn, loan)
    assert [r["balance"] for r in rows] == [-500_00, -500_00 - 150_000_00 + 1_000_00]


def test_update_and_delete(conn, accounts):
    checking, _ = accounts
    t = ledger.add_transaction(conn, checking, "2026-01-05", -25_00)
    ledger.update_transaction(conn, t, amount=-30_00, memo="fixed")
    assert ledger.account_balance(conn, checking) == 100_00 - 30_00
    ledger.delete_transaction(conn, t)
    assert ledger.account_balance(conn, checking) == 100_00


# ---- transfers (FIRST PRIORITY) --------------------------------------------
def test_transfer_creates_both_sides(conn, accounts):
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    f = ledger.get_transaction(conn, from_id)
    t = ledger.get_transaction(conn, to_id)
    assert f["amount"] == -40_00 and t["amount"] == 40_00
    assert f["transfer_pair_id"] == to_id and t["transfer_pair_id"] == from_id
    assert f["transfer_account_id"] == savings and t["transfer_account_id"] == checking
    assert ledger.account_balance(conn, checking) == 60_00
    assert ledger.account_balance(conn, savings) == 40_00


def test_transfer_category_renders_as_other_account(conn, accounts):
    checking, savings = accounts
    from_id, _ = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    rows = {r["id"]: r for r in ledger.register_rows(conn, checking)}
    assert rows[from_id]["category_label"] == "[Savings]"


def test_transfer_edit_amount_syncs_both(conn, accounts):
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    ledger.update_transaction(conn, from_id, amount=-75_00)
    assert ledger.get_transaction(conn, to_id)["amount"] == 75_00
    assert ledger.account_balance(conn, checking) == 100_00 - 75_00
    assert ledger.account_balance(conn, savings) == 75_00


def test_transfer_edit_date_syncs_both(conn, accounts):
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    ledger.update_transaction(conn, from_id, date="2026-04-15")
    assert ledger.get_transaction(conn, to_id)["date"] == "2026-04-15"


def test_transfer_payee_written_to_both_legs(conn, accounts):
    # Quicken keeps a normal payee on both legs (e.g. 'Discover' on the paying
    # account AND on the card account). create_transfer must persist it on each.
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(
        conn, checking, savings, "2026-03-01", 40_00, payee="Discover")
    assert ledger.get_transaction(conn, from_id)["payee"] == "Discover"
    assert ledger.get_transaction(conn, to_id)["payee"] == "Discover"


def test_transfer_edit_payee_syncs_both(conn, accounts):
    # Editing the payee on one leg mirrors to the linked leg (Quicken keeps the
    # payee identical across a transfer).
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(
        conn, checking, savings, "2026-03-01", 40_00, payee="Discover")
    ledger.update_transaction(conn, from_id, payee="Discover Card")
    assert ledger.get_transaction(conn, from_id)["payee"] == "Discover Card"
    assert ledger.get_transaction(conn, to_id)["payee"] == "Discover Card"


def test_transfer_edit_memo_syncs_both(conn, accounts):
    # Editing the memo on one leg mirrors to the linked leg, like the payee.
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(
        conn, checking, savings, "2026-03-01", 40_00, memo="rent")
    ledger.update_transaction(conn, from_id, memo="March rent")
    assert ledger.get_transaction(conn, from_id)["memo"] == "March rent"
    assert ledger.get_transaction(conn, to_id)["memo"] == "March rent"


def test_transfer_clear_memo_clears_mirror_not_restores_old(conn, accounts):
    # the user's fix: clearing a transfer's memo on one leg must CLEAR the mirror too,
    # never leave/restore the old value on the linked entry.
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(
        conn, checking, savings, "2026-03-01", 40_00, memo="rent")
    ledger.update_transaction(conn, from_id, memo=None)   # user cleared it
    assert ledger.get_transaction(conn, from_id)["memo"] is None
    assert ledger.get_transaction(conn, to_id)["memo"] is None


def test_transfer_delete_removes_both(conn, accounts):
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    ledger.delete_transaction(conn, from_id)
    assert ledger.get_transaction(conn, from_id) is None
    assert ledger.get_transaction(conn, to_id) is None
    assert ledger.account_balance(conn, checking) == 100_00
    assert ledger.account_balance(conn, savings) == 0


def test_retarget_transfer_moves_mirror(conn, accounts):
    """Re-pointing a transfer moves the counter-leg to the new account: the old
    mirror is deleted, exactly one new mirror is created, both legs cross-link,
    and no orphan/duplicate is left (the user's wrong-transfer-account bug)."""
    checking, savings = accounts
    third = ledger.create_account(conn, "Brokerage", "checking", opening_balance=0)
    from_id, to_id = ledger.create_transfer(
        conn, checking, savings, "2026-03-01", 40_00, memo="rent", payee="Move",
        num="101", cleared=1)

    new_pair = ledger.retarget_transfer(conn, from_id, third)

    # editing leg stayed put but now targets Brokerage and links the new mirror
    src = ledger.get_transaction(conn, from_id)
    assert src["account_id"] == checking
    assert src["transfer_account_id"] == third
    assert src["transfer_pair_id"] == new_pair
    # old mirror in savings is GONE; new mirror is the negated leg in Brokerage
    assert ledger.get_transaction(conn, to_id) is None
    mirror = ledger.get_transaction(conn, new_pair)
    assert mirror["account_id"] == third
    assert mirror["amount"] == -src["amount"] == 40_00
    assert mirror["transfer_account_id"] == checking
    assert mirror["transfer_pair_id"] == from_id
    # memo/payee carried; cleared carried from the old mirror; reconciled reset
    assert mirror["memo"] == "rent"
    assert mirror["payee"] == "Move"
    assert mirror["num"] == "101"
    assert mirror["cleared"] == 1
    assert mirror["reconciled"] == 0
    # exactly one row in each affected account; balances consistent
    assert ledger.register_rows(conn, savings) == []
    assert len(ledger.register_rows(conn, third)) == 1
    assert ledger.account_balance(conn, checking) == 100_00 - 40_00
    assert ledger.account_balance(conn, savings) == 0
    assert ledger.account_balance(conn, third) == 40_00
    assert ledger.net_worth(conn) == 100_00


def test_retarget_transfer_from_the_mirror_side(conn, accounts):
    """Editing the OTHER leg (the deposit side) retargets its own counter-leg:
    the leg being edited stays, the one in the source account moves."""
    checking, savings = accounts
    third = ledger.create_account(conn, "Brokerage", "checking", opening_balance=0)
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)

    # Re-point the SAVINGS leg (to_id) away from checking, over to Brokerage.
    new_pair = ledger.retarget_transfer(conn, to_id, third)

    kept = ledger.get_transaction(conn, to_id)
    assert kept["account_id"] == savings and kept["transfer_account_id"] == third
    assert ledger.get_transaction(conn, from_id) is None  # old checking leg moved
    mirror = ledger.get_transaction(conn, new_pair)
    assert mirror["account_id"] == third
    assert mirror["amount"] == -kept["amount"] == -40_00
    assert ledger.account_balance(conn, checking) == 100_00
    assert ledger.account_balance(conn, savings) == 40_00
    assert ledger.account_balance(conn, third) == -40_00


def test_retarget_transfer_to_same_account_rejected(conn, accounts):
    checking, savings = accounts
    from_id, _ = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    with pytest.raises(ValueError):
        ledger.retarget_transfer(conn, from_id, checking)  # its own account


def test_retarget_transfer_unchanged_target_is_noop(conn, accounts):
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    assert ledger.retarget_transfer(conn, from_id, savings) == to_id  # no-op
    # nothing moved or duplicated
    assert ledger.get_transaction(conn, to_id) is not None
    assert len(ledger.register_rows(conn, savings)) == 1


def test_retarget_transfer_missing_account_rejected(conn, accounts):
    checking, savings = accounts
    from_id, _ = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    with pytest.raises(KeyError):
        ledger.retarget_transfer(conn, from_id, 9999)


def test_retarget_non_transfer_rejected(conn, accounts):
    checking, savings = accounts
    t = ledger.add_transaction(conn, checking, "2026-03-01", -40_00)
    with pytest.raises(ValueError):
        ledger.retarget_transfer(conn, t, savings)


def test_retarget_one_sided_mirror_leg_rejected(conn, accounts):
    """A one-sided mirror leg (transfer_account_id set but transfer_pair_id NULL,
    like a loan/import mirror) has no counter-leg to move -- retarget refuses
    rather than fabricate one and corrupt the model."""
    checking, savings = accounts
    third = ledger.create_account(conn, "Brokerage", "checking", opening_balance=0)
    # A lone leg pointing at savings with no reciprocal row.
    leg = conn.execute(
        "INSERT INTO transactions(account_id, date, amount, transfer_account_id) "
        "VALUES (?,?,?,?)", (checking, "2026-03-01", -40_00, savings)).lastrowid
    conn.commit()
    with pytest.raises(ValueError):
        ledger.retarget_transfer(conn, leg, third)


def test_retarget_split_transfer_rejected(conn, accounts):
    """A transfer that also carries split lines is a multi-leg split; its whole
    account isn't re-pointed as a unit (each split leg has its own target)."""
    checking, savings = accounts
    third = ledger.create_account(conn, "Brokerage", "checking", opening_balance=0)
    from_id, _ = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    # Give the transfer leg split lines directly (as an imported mortgage would).
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount) "
                 "VALUES (?,?,?)", (from_id, None, -40_00))
    conn.commit()
    with pytest.raises(ValueError):
        ledger.retarget_transfer(conn, from_id, third)


def test_transfer_nets_to_zero_in_net_worth(conn, accounts):
    checking, savings = accounts
    before = ledger.net_worth(conn)
    ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)
    assert ledger.net_worth(conn) == before  # moving money doesn't change net worth


def test_new_transfer_legs_start_unreconciled_and_reconcile_independently(conn, accounts):
    """A newly created transfer must leave BOTH legs unreconciled (each starts at
    the plain non-transfer default), both are reconcile candidates in their OWN
    account, and reconciling one leg must NOT touch the other."""
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-03-01", 40_00)

    # Neither leg is auto-reconciled (nor auto-cleared).
    assert ledger.get_transaction(conn, from_id)["reconciled"] == 0
    assert ledger.get_transaction(conn, to_id)["reconciled"] == 0
    assert ledger.get_transaction(conn, from_id)["cleared"] == 0
    assert ledger.get_transaction(conn, to_id)["cleared"] == 0

    # Each leg is a reconcile candidate in its own account's reconcile dialog.
    assert any(r["id"] == from_id for r in ledger.unreconciled_rows(conn, checking))
    assert any(r["id"] == to_id for r in ledger.unreconciled_rows(conn, savings))

    # Reconcile ONLY the checking account: clear its leg, then finish.
    ledger.update_transaction(conn, from_id, cleared=1)
    stmt = ledger.account_balance(conn, checking)  # only txn is cleared -> == cleared balance
    ledger.finish_reconciliation(conn, checking, "2026-03-31", stmt)

    # Checking's leg is now R; savings' leg is UNTOUCHED and still reconcilable.
    assert ledger.get_transaction(conn, from_id)["reconciled"] == 1
    assert ledger.get_transaction(conn, to_id)["reconciled"] == 0
    assert ledger.get_transaction(conn, to_id)["cleared"] == 0
    assert any(r["id"] == to_id for r in ledger.unreconciled_rows(conn, savings))
    assert not any(r["id"] == from_id for r in ledger.unreconciled_rows(conn, checking))


def test_transfer_validation(conn, accounts):
    checking, savings = accounts
    with pytest.raises(ValueError):
        ledger.create_transfer(conn, checking, checking, "2026-03-01", 40_00)
    with pytest.raises(ValueError):
        ledger.create_transfer(conn, checking, savings, "2026-03-01", 0)
    with pytest.raises(KeyError):
        ledger.create_transfer(conn, checking, 999, "2026-03-01", 40_00)


# ---- converting a plain transaction into a transfer -------------------------
def test_convert_to_transfer_creates_linked_mirror(conn, accounts):
    checking, savings = accounts
    cat = ledger.resolve_category(conn, "Misc")
    t = ledger.add_transaction(conn, checking, "2026-03-01", -40_00,
                               payee="Move money", num="101", category_id=cat)
    mirror = ledger.convert_to_transfer(conn, t, savings)

    src = ledger.get_transaction(conn, t)
    dst = ledger.get_transaction(conn, mirror)
    # original row kept its identity/amount, lost its category, gained the link
    assert src["id"] == t and src["amount"] == -40_00
    assert src["category_id"] is None
    assert src["transfer_account_id"] == savings and src["transfer_pair_id"] == mirror
    # mirror is the opposite side in the target account, pointing back
    assert dst["account_id"] == savings and dst["amount"] == 40_00
    assert dst["transfer_account_id"] == checking and dst["transfer_pair_id"] == t
    assert dst["payee"] == "Move money" and dst["num"] == "101"
    # balances moved; net worth unchanged
    assert ledger.account_balance(conn, checking) == 100_00 - 40_00
    assert ledger.account_balance(conn, savings) == 40_00
    assert ledger.net_worth(conn) == 100_00
    # register shows the transfer label on the source side
    rows = {r["id"]: r for r in ledger.register_rows(conn, checking)}
    assert rows[t]["category_label"] == "[Savings]"


def test_converted_transfer_syncs_on_edit_and_delete(conn, accounts):
    checking, savings = accounts
    t = ledger.add_transaction(conn, checking, "2026-03-01", -40_00, payee="X")
    mirror = ledger.convert_to_transfer(conn, t, savings)
    # editing the source amount/date mirrors to the linked side
    ledger.update_transaction(conn, t, amount=-75_00, date="2026-04-15")
    assert ledger.get_transaction(conn, mirror)["amount"] == 75_00
    assert ledger.get_transaction(conn, mirror)["date"] == "2026-04-15"
    assert ledger.account_balance(conn, savings) == 75_00
    # deleting one side removes both
    ledger.delete_transaction(conn, mirror)
    assert ledger.get_transaction(conn, t) is None
    assert ledger.get_transaction(conn, mirror) is None
    assert ledger.account_balance(conn, checking) == 100_00
    assert ledger.account_balance(conn, savings) == 0


def test_convert_to_transfer_validation(conn, accounts):
    checking, savings = accounts
    t = ledger.add_transaction(conn, checking, "2026-03-01", -40_00)
    with pytest.raises(ValueError):                       # same account
        ledger.convert_to_transfer(conn, t, checking)
    with pytest.raises(KeyError):                         # no such account
        ledger.convert_to_transfer(conn, t, 999)
    ledger.convert_to_transfer(conn, t, savings)
    with pytest.raises(ValueError):                       # already a transfer
        ledger.convert_to_transfer(conn, t, savings)
    # a split transaction cannot be collapsed into a plain transfer
    s = ledger.add_transaction(conn, checking, "2026-05-01", -30_00)
    groc = ledger.resolve_category(conn, "Groceries")
    din = ledger.resolve_category(conn, "Dining")
    ledger.set_splits(conn, s, [
        {"category_id": groc, "amount": -20_00, "memo": None},
        {"category_id": din, "amount": -10_00, "memo": None}])
    with pytest.raises(ValueError):
        ledger.convert_to_transfer(conn, s, savings)


# ---- checkpoints ------------------------------------------------------------
def test_checkpoint_balance_matches_direct(conn, accounts):
    checking, _ = accounts
    ledger.add_transaction(conn, checking, "2024-06-01", 500_00)
    ledger.add_transaction(conn, checking, "2025-03-01", -200_00)
    ledger.add_transaction(conn, checking, "2026-02-01", 75_00)
    ledger.rebuild_checkpoints(conn, checking)
    for as_of in ("2024-12-31", "2025-06-01", "2026-02-01", "2026-12-31"):
        assert ledger.balance_via_checkpoint(conn, checking, as_of) == \
            ledger.account_balance(conn, checking, as_of)


def test_bad_date_rejected(conn, accounts):
    checking, _ = accounts
    with pytest.raises(ValueError):
        ledger.add_transaction(conn, checking, "03/01/2026", -10_00)


# ---- splits (divide one transaction across categories) ---------------------
def test_set_get_splits_and_display(conn, accounts):
    checking, _ = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    t = ledger.add_transaction(conn, checking, "2026-01-05", -100_00,
                               payee="Costco", category_id=groc)
    ledger.set_splits(conn, t, [
        {"category_id": groc, "amount": -60_00, "memo": "food"},
        {"category_id": dining, "amount": -40_00},
    ])
    assert ledger.has_splits(conn, t)
    lines = ledger.get_splits(conn, t)
    assert [(l["category_label"], l["amount"], l["memo"]) for l in lines] == [
        ("Groceries", -60_00, "food"),
        ("Dining", -40_00, ""),
    ]
    # the transaction's own category is cleared; the field shows --Split--.
    row = ledger.get_transaction(conn, t)
    assert row["category_id"] is None
    assert ledger.category_display(conn, row) == ledger.SPLIT_LABEL
    # register_rows carries both the label and the is_split flag.
    reg = {r["id"]: r for r in ledger.register_rows(conn, checking)}[t]
    assert reg["category_label"] == "--Split--" and reg["is_split"] is True
    # the total (and account balance) is unchanged by splitting.
    assert ledger.account_balance(conn, checking) == 100_00 - 100_00


def test_set_splits_absorbs_difference_into_uncategorized(conn, accounts):
    """A split whose lines do not sum to the transaction total is NOT rejected:
    the signed difference is folded into an uncategorized line so the split always
    reconciles (the user finishes categorizing later; the register flags the leftover
    with a warning triangle). This replaces the old hard 'must sum to total' block."""
    checking, _ = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    t = ledger.add_transaction(conn, checking, "2026-01-05", -100_00)
    ledger.set_splits(conn, t, [
        {"category_id": groc, "amount": -60_00},
        {"category_id": dining, "amount": -30_00},   # sums to -90, total is -100
    ])
    assert ledger.has_splits(conn, t)
    lines = ledger.get_splits(conn, t)
    assert [(l["category_label"], l["amount"]) for l in lines] == [
        ("Groceries", -60_00), ("Dining", -30_00), ("", -10_00)]
    # the -10.00 leftover is reported (drives the register warning triangle).
    assert ledger.uncategorized_split_amount(conn, t) == -10_00
    # the transaction total and the account balance are unchanged by the split.
    assert ledger.get_transaction(conn, t)["amount"] == -100_00
    assert ledger.account_balance(conn, checking) == 100_00 - 100_00
    # a fully-covered split reports no leftover.
    ledger.set_splits(conn, t, [
        {"category_id": groc, "amount": -60_00},
        {"category_id": dining, "amount": -40_00}])
    assert ledger.uncategorized_split_amount(conn, t) == 0


def test_set_splits_needs_two_lines(conn, accounts):
    checking, _ = accounts
    t = ledger.add_transaction(conn, checking, "2026-01-05", -100_00)
    with pytest.raises(ValueError):
        ledger.set_splits(conn, t, [(None, -100_00, None)])


def test_splitting_a_transfer_needs_a_leg_to_the_counter_account(conn, accounts):
    """A transfer IS splittable now, but only into a split that still sends one
    LINE to the counter-account: the row's mirror lives there, and a split with
    no leg pointing at it would orphan the other half of the user's transfer."""
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-01-05", 50_00)
    with pytest.raises(ValueError):
        ledger.set_splits(conn, from_id, [(None, -25_00, None), (None, -25_00, None)])
    # nothing was touched: the transfer is intact on both sides.
    row = ledger.get_transaction(conn, from_id)
    assert row["transfer_account_id"] == savings
    assert row["transfer_pair_id"] == to_id
    assert not ledger.has_splits(conn, from_id)


def test_already_split_transfer_can_be_resplit(conn, accounts):
    """USER BUG (2026-08-16): a txn that is BOTH a transfer (transfer_account_id
    set) AND already split -- an imported Crossland Mortgage payment to [House]
    with a principal+interest split -- must let set_splits re-split it (edit the
    legs) while preserving the parent transfer link. A PLAIN transfer instead
    moves its transfer onto one split LINE (see test_transfer_split.py)."""
    checking, _ = accounts
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, checking, "2000-12-08", -829_56,
                               payee="Crossland Mortgage Corp")
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -120_46, "memo": "principal"},
        {"category_id": int_exp, "amount": -709_10, "memo": "interest"},
    ])
    # legacy import state: the parent row ALSO carries the [House] transfer link.
    conn.execute("UPDATE transactions SET transfer_account_id=? WHERE id=?",
                 (house, t))
    conn.commit()
    # re-split (edit the legs) succeeds despite the parent transfer_account_id.
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -200_00, "memo": "principal"},
        {"category_id": int_exp, "amount": -629_56, "memo": "interest"},
    ])
    row = ledger.get_transaction(conn, t)
    assert row["transfer_account_id"] == house               # transfer link preserved
    assert ledger.category_display(conn, row) == ledger.SPLIT_LABEL
    labels = [(l["category_label"], l["amount"]) for l in ledger.get_splits(conn, t)]
    assert labels == [("[House]", -200_00), ("Int Exp", -629_56)]


def test_transfer_linked_txn_with_splits_displays_as_split(conn, accounts):
    """ROOT CAUSE regression: a transaction that is simultaneously a transfer
    (transfer_account_id set) AND carries split lines -- e.g. an imported
    mortgage payment whose QIF split has a [House] principal leg -- must read as
    --Split-- so both legs surface, NOT as the lone bracketed transfer account
    (which hid the split and broke reconciliation)."""
    checking, savings = accounts
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, checking, "2000-12-08", -829_56,
                               payee="Crossland Mortgage Corp")
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -120_46, "memo": "principal"},
        {"category_id": int_exp, "amount": -709_10, "memo": "interest"},
    ])
    # Simulate the legacy import state where the payment ALSO carried a
    # transfer_account_id on the parent row (Quicken echoed [House] in L).
    conn.execute("UPDATE transactions SET transfer_account_id=? WHERE id=?",
                 (house, t))
    conn.commit()
    row = ledger.get_transaction(conn, t)
    assert row["transfer_account_id"] is not None            # it IS a transfer...
    assert ledger.has_splits(conn, t)                        # ...and it IS split
    # splits win: the field shows --Split--, not a lone [House].
    assert ledger.category_display(conn, row) == ledger.SPLIT_LABEL
    reg = {r["id"]: r for r in ledger.register_rows(conn, checking)}[t]
    assert reg["category_label"] == ledger.SPLIT_LABEL and reg["is_split"] is True
    # both legs are visible, one of them the [House] principal transfer leg.
    labels = [(l["category_label"], l["amount"]) for l in ledger.get_splits(conn, t)]
    assert labels == [("[House]", -120_46), ("Int Exp", -709_10)]


def test_split_leg_transfer_creates_reciprocal_mirror(conn, accounts):
    """A split leg may be a transfer to another account: it displays as
    [Account], and a reciprocal mirror is created in that account so the two
    sides reconcile. Clearing/deleting the split removes the mirror."""
    checking, _ = accounts
    k401 = ledger.create_account(conn, "401K", "investment")
    salary = ledger.resolve_category(conn, "Salary")
    fed = ledger.resolve_category(conn, "Tax:Fed")
    # a $2,000 paycheck: $2,600 salary, -$400 fed tax, -$200 to [401K].
    chk0 = ledger.account_balance(conn, checking)
    t = ledger.add_transaction(conn, checking, "2026-02-01", 2000_00, payee="ACME Payroll")
    ledger.set_splits(conn, t, [
        {"category_id": salary, "amount": 2600_00},
        {"category_id": fed, "amount": -400_00},
        {"transfer_account_id": k401, "amount": -200_00, "memo": "401k deferral"},
    ])
    # the parent reads as a split, and the 401K leg shows [401K].
    assert ledger.category_display(conn, ledger.get_transaction(conn, t)) == ledger.SPLIT_LABEL
    legs = {l["category_label"]: l for l in ledger.get_splits(conn, t)}
    assert legs["[401K]"]["amount"] == -200_00
    assert legs["[401K]"]["transfer_account_id"] == k401
    # a reciprocal +$200 mirror now sits in the 401K account, labelled [Checking].
    assert ledger.account_balance(conn, k401) == 200_00
    krows = ledger.register_rows(conn, k401)
    assert len(krows) == 1
    assert krows[0]["amount"] == 200_00
    assert krows[0]["category_label"] == "[Checking]"
    # checking still nets the full paycheck (the split sums to +2000).
    assert ledger.account_balance(conn, checking) == chk0 + 2000_00
    # clearing the split removes the mirror (no orphan in 401K).
    ledger.clear_splits(conn, t)
    assert ledger.account_balance(conn, k401) == 0
    assert ledger.register_rows(conn, k401) == []


def test_deleting_split_parent_removes_transfer_mirror(conn, accounts):
    checking, _ = accounts
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, checking, "2000-12-08", -829_56)
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -120_46},
        {"category_id": int_exp, "amount": -709_10},
    ])
    assert ledger.account_balance(conn, house) == 120_46   # +mirror in House
    ledger.delete_transaction(conn, t)
    assert ledger.register_rows(conn, house) == []      # mirror deleted too
    assert ledger.account_balance(conn, house) == 0


def test_split_transfer_leg_mirror_carries_parent_payee(conn, accounts):
    """The reciprocal mirror keeps the parent transaction's payee on BOTH legs,
    exactly like a plain whole-transaction transfer."""
    checking, _ = accounts
    k401 = ledger.create_account(conn, "401K", "investment")
    salary = ledger.resolve_category(conn, "Salary")
    t = ledger.add_transaction(conn, checking, "2026-02-01", 2000_00,
                               payee="ACME Payroll")
    ledger.set_splits(conn, t, [
        {"category_id": salary, "amount": 2200_00},
        {"transfer_account_id": k401, "amount": -200_00, "memo": "401k deferral"},
    ])
    krows = ledger.register_rows(conn, k401)
    assert len(krows) == 1
    assert krows[0]["payee"] == "ACME Payroll"
    assert krows[0]["amount"] == 200_00


def test_editing_split_transfer_leg_updates_mirror(conn, accounts):
    """Re-applying the split (an edit) resyncs the mirror: the amount follows,
    a retargeted leg moves the mirror to the new account, and no orphan mirror
    is left behind in the old one."""
    checking, _ = accounts
    k401 = ledger.create_account(conn, "401K", "investment")
    ira = ledger.create_account(conn, "IRA", "investment")
    salary = ledger.resolve_category(conn, "Salary")
    t = ledger.add_transaction(conn, checking, "2026-02-01", 2000_00,
                               payee="ACME Payroll")
    ledger.set_splits(conn, t, [
        {"category_id": salary, "amount": 2200_00},
        {"transfer_account_id": k401, "amount": -200_00},
    ])
    assert ledger.account_balance(conn, k401) == 200_00
    # Edit the leg amount: the single mirror follows, not accumulates.
    ledger.set_splits(conn, t, [
        {"category_id": salary, "amount": 2300_00},
        {"transfer_account_id": k401, "amount": -300_00},
    ])
    krows = ledger.register_rows(conn, k401)
    assert len(krows) == 1                       # replaced, not duplicated
    assert krows[0]["amount"] == 300_00
    # Retarget the leg to a different account: old mirror gone, new one appears.
    ledger.set_splits(conn, t, [
        {"category_id": salary, "amount": 2300_00},
        {"transfer_account_id": ira, "amount": -300_00},
    ])
    assert ledger.register_rows(conn, k401) == []   # no orphan in old target
    assert ledger.account_balance(conn, k401) == 0
    irarows = ledger.register_rows(conn, ira)
    assert len(irarows) == 1
    assert irarows[0]["amount"] == 300_00
    assert irarows[0]["category_label"] == "[Checking]"


def test_multiple_split_transfer_legs_each_mirror(conn, accounts):
    """A split with more than one transfer leg mirrors EACH leg into its own
    target account with the correct sign, amount and payee."""
    checking, _ = accounts
    k401 = ledger.create_account(conn, "401K", "investment")
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, checking, "2000-12-08", -1000_00,
                               payee="First Bank")
    ledger.set_splits(conn, t, [
        {"transfer_account_id": k401, "amount": -200_00},
        {"transfer_account_id": house, "amount": -300_00},
        {"category_id": int_exp, "amount": -500_00},
    ])
    krows = ledger.register_rows(conn, k401)
    hrows = ledger.register_rows(conn, house)
    assert len(krows) == 1 and len(hrows) == 1
    assert krows[0]["amount"] == 200_00
    assert krows[0]["payee"] == "First Bank"
    assert krows[0]["category_label"] == "[Checking]"
    assert hrows[0]["amount"] == 300_00
    assert hrows[0]["payee"] == "First Bank"
    assert hrows[0]["category_label"] == "[Checking]"
    assert ledger.account_balance(conn, k401) == 200_00
    assert ledger.account_balance(conn, house) == 300_00
    # Deleting the parent cleans up both mirrors.
    ledger.delete_transaction(conn, t)
    assert ledger.register_rows(conn, k401) == []
    assert ledger.register_rows(conn, house) == []


def _pair_id(conn, txn_id, target):
    return conn.execute(
        "SELECT transfer_pair_id FROM splits WHERE transaction_id=? "
        "AND transfer_account_id=?", (txn_id, target)).fetchone()[0]


# ---- per-leg Clr=R survives every transfer write-path ------------------------
# Regression guard for the whole-db loss of Clr=R on transfers: each of these
# write-paths rebuilds/rewrites a transfer leg, and NONE may silently drop the
# leg's own per-account cleared/reconciled status. Each leg reconciles
# independently against its own account, so a rewrite triggered in one account
# must never disturb the other leg -- and a rewrite of a leg (e.g. an unrelated
# split edit) must never drop THAT leg's own reconciled flag.
def _reconcile_leg(conn, account_id, txn_id):
    """Clear a single leg and finish its account's reconcile so it reaches 'R'."""
    ledger.update_transaction(conn, txn_id, cleared=1)
    ledger.finish_reconciliation(
        conn, account_id, "2026-12-31", ledger.account_balance(conn, account_id))


def test_editing_split_preserves_reconciled_transfer_leg(conn, accounts):
    """Editing ANY line of a split that contains a transfer leg must NOT drop the
    transfer leg's own per-account reconciled ('R') status. The mortgage-principal
    leg to [House] is reconciled in House; then an UNRELATED interest-line edit --
    and, separately, a change to the transfer leg's OWN amount (a principal
    paydown) -- both rebuild the split, and the House leg must keep its 'R'. This
    is the exact silent-drop that wiped Clr=R across transfers (set_splits used to
    recreate the mirror with cleared/reconciled defaulting to 0)."""
    checking, _ = accounts
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, checking, "2000-12-08", -829_56)
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -120_46},
        {"category_id": int_exp, "amount": -709_10},
    ])
    mirror_id = _pair_id(conn, t, house)
    _reconcile_leg(conn, house, mirror_id)
    assert ledger.get_transaction(conn, mirror_id)["reconciled"] == 1

    # (a) Edit an UNRELATED line (interest memo only; amounts/sum unchanged).
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -120_46},
        {"category_id": int_exp, "amount": -709_10, "memo": "reclassified"},
    ])
    mirror_id = _pair_id(conn, t, house)          # rebuilt -> new row id
    m = ledger.get_transaction(conn, mirror_id)
    assert m["reconciled"] == 1 and m["cleared"] == 1

    # (b) Change the transfer leg's OWN amount (principal paydown), interest
    # absorbs the offset so the split still sums. Matched by account (amount
    # differs) -> status still preserved.
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -130_46},
        {"category_id": int_exp, "amount": -699_10},
    ])
    mirror_id = _pair_id(conn, t, house)
    m = ledger.get_transaction(conn, mirror_id)
    assert m["reconciled"] == 1 and m["cleared"] == 1
    assert m["amount"] == 130_46                  # amount followed the edit


def test_editing_split_preserves_each_of_multiple_reconciled_legs(conn, accounts):
    """A split with two transfer legs to DIFFERENT accounts, each reconciled in
    its own account, keeps BOTH 'R's across a rebuild -- the preservation is
    per-target-account, not a single blanket flag."""
    checking, _ = accounts
    k401 = ledger.create_account(conn, "401K", "investment")
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, checking, "2000-12-08", -1000_00,
                               payee="First Bank")
    ledger.set_splits(conn, t, [
        {"transfer_account_id": k401, "amount": -200_00},
        {"transfer_account_id": house, "amount": -300_00},
        {"category_id": int_exp, "amount": -500_00},
    ])
    _reconcile_leg(conn, k401, _pair_id(conn, t, k401))
    _reconcile_leg(conn, house, _pair_id(conn, t, house))

    # Rebuild via an unrelated edit (interest memo).
    ledger.set_splits(conn, t, [
        {"transfer_account_id": k401, "amount": -200_00},
        {"transfer_account_id": house, "amount": -300_00},
        {"category_id": int_exp, "amount": -500_00, "memo": "edited"},
    ])
    assert ledger.get_transaction(conn, _pair_id(conn, t, k401))["reconciled"] == 1
    assert ledger.get_transaction(conn, _pair_id(conn, t, house))["reconciled"] == 1


def test_reconcile_finish_stamps_R_on_split_transfer_mirror_only(conn, accounts):
    """RECONCILE flow on a transfer leg: checking the split's [House] mirror leg
    sets the temporary 'c', and Finish promotes it to 'R' -- writing R ONLY to
    House's own leg. The split parent in checking (the counter-leg) is never
    touched by Finish; it reconciles independently in its own account."""
    checking, _ = accounts
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, checking, "2000-12-08", -829_56)
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -120_46},
        {"category_id": int_exp, "amount": -709_10},
    ])
    mirror_id = _pair_id(conn, t, house)

    # Check the item during reconcile -> temporary 'c' (not yet reconciled).
    ledger.update_transaction(conn, mirror_id, cleared=1)
    assert ledger.get_transaction(conn, mirror_id)["cleared"] == 1
    assert ledger.get_transaction(conn, mirror_id)["reconciled"] == 0

    # Finish House's reconcile: 'c' -> 'R' on the mirror leg only.
    ledger.finish_reconciliation(conn, house, "2000-12-31",
                                 ledger.account_balance(conn, house))
    assert ledger.get_transaction(conn, mirror_id)["reconciled"] == 1
    # Counter-leg (the split parent in checking) is UNTOUCHED.
    parent = ledger.get_transaction(conn, t)
    assert parent["reconciled"] == 0 and parent["cleared"] == 0


def test_retarget_transfer_preserves_reconciled_on_stationary_leg(conn, accounts):
    """Re-pointing a transfer to a new account must not disturb the STATIONARY
    (edited) leg's own reconciled ('R'): only the moved counter-leg is recreated
    (and, per design, restarts unreconciled in its brand-new account)."""
    checking, savings = accounts
    third = ledger.create_account(conn, "Brokerage", "checking", opening_balance=0)
    from_id, _to = ledger.create_transfer(
        conn, checking, savings, "2026-03-01", 40_00, cleared=1)
    _reconcile_leg(conn, checking, from_id)
    assert ledger.get_transaction(conn, from_id)["reconciled"] == 1

    new_pair = ledger.retarget_transfer(conn, from_id, third)

    # Stationary leg keeps its 'R'; the moved mirror is a fresh unreconciled leg.
    assert ledger.get_transaction(conn, from_id)["reconciled"] == 1
    assert ledger.get_transaction(conn, from_id)["cleared"] == 1
    assert ledger.get_transaction(conn, new_pair)["reconciled"] == 0


def test_update_transaction_never_mirrors_clr_to_the_other_leg(conn, accounts):
    """The generic update path keeps each leg's flag independent: reconciling one
    leg via update_transaction must NOT propagate cleared/reconciled to its pair
    (unlike date/amount/payee/memo, which do mirror)."""
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-03-01",
                                            40_00)
    ledger.update_transaction(conn, from_id, cleared=1, reconciled=1)
    # from_id got R; to_id (the counter-leg) is left entirely alone.
    assert ledger.get_transaction(conn, from_id)["reconciled"] == 1
    assert ledger.get_transaction(conn, to_id)["cleared"] == 0
    assert ledger.get_transaction(conn, to_id)["reconciled"] == 0


def test_backfill_creates_missing_split_transfer_mirror(conn, accounts):
    """A split leg imported one-sided (transfer_account_id set, transfer_pair_id
    NULL, no mirror -- exactly what importers.core._insert_split writes) leaves
    the target account with no leg at all. backfill_split_transfer_mirrors
    fabricates the reciprocal leg and links it back. Re-running is a no-op."""
    checking, _ = accounts
    k401 = ledger.create_account(conn, "401K", "investment")
    salary = ledger.resolve_category(conn, "Salary")
    t = ledger.add_transaction(conn, checking, "2024-01-05", 2000_00, payee="ACME")
    # Emulate the importer: write the split rows directly, NO mirror created.
    conn.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (t,))
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount, memo) "
                 "VALUES (?,?,?,?)", (t, salary, 2200_00, None))
    conn.execute("INSERT INTO splits(transaction_id, transfer_account_id, amount, "
                 "memo) VALUES (?,?,?,?)", (t, k401, -200_00, "deferral"))
    conn.commit()
    assert ledger.register_rows(conn, k401) == []          # nothing there yet
    assert _pair_id(conn, t, k401) is None

    summary = ledger.backfill_split_transfer_mirrors(conn)
    assert summary == {"created": 1, "adopted": 0, "skipped": 0}
    krows = ledger.register_rows(conn, k401)
    assert len(krows) == 1
    assert krows[0]["amount"] == 200_00                    # opposite sign
    assert krows[0]["payee"] == "ACME"                     # parent payee kept
    assert krows[0]["category_label"] == "[Checking]"
    assert _pair_id(conn, t, k401) == krows[0]["id"]       # leg now linked
    assert ledger.account_balance(conn, k401) == 200_00

    # Idempotent: a second run touches nothing and creates no duplicate.
    assert ledger.backfill_split_transfer_mirrors(conn) == {
        "created": 0, "adopted": 0, "skipped": 0}
    assert len(ledger.register_rows(conn, k401)) == 1


def test_backfill_adopts_existing_counter_leg_without_duplicating(conn, accounts):
    """When the target account already holds its own matching leg (a separate
    import supplied the reciprocal), backfill LINKS to it rather than fabricating
    a second one -- no duplicate."""
    checking, _ = accounts
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, checking, "2000-12-08", -829_56,
                               payee="Crossland")
    conn.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (t,))
    conn.execute("INSERT INTO splits(transaction_id, transfer_account_id, amount, "
                 "memo) VALUES (?,?,?,?)", (t, house, -120_46, "principal"))
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount, memo) "
                 "VALUES (?,?,?,?)", (t, int_exp, -709_10, "interest"))
    # The counter-account already has the reciprocal leg from its own import.
    existing = conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, "
        "transfer_account_id) VALUES (?,?,?,?,?)",
        (house, "2000-12-08", 120_46, "Crossland", checking)).lastrowid
    conn.commit()

    summary = ledger.backfill_split_transfer_mirrors(conn)
    assert summary == {"created": 0, "adopted": 1, "skipped": 0}
    hrows = ledger.register_rows(conn, house)
    assert len(hrows) == 1                                 # adopted, not doubled
    assert _pair_id(conn, t, house) == existing


def test_backfill_can_target_a_single_account(conn, accounts):
    """Passing an account id restricts the repair to legs transferring INTO it;
    orphans aimed at other accounts are left alone."""
    checking, _ = accounts
    k401 = ledger.create_account(conn, "401K", "investment")
    house = ledger.create_account(conn, "House", "asset")
    salary = ledger.resolve_category(conn, "Salary")
    t = ledger.add_transaction(conn, checking, "2024-01-05", 2000_00, payee="ACME")
    conn.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (t,))
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount, memo) "
                 "VALUES (?,?,?,?)", (t, salary, 2500_00, None))
    conn.execute("INSERT INTO splits(transaction_id, transfer_account_id, amount, "
                 "memo) VALUES (?,?,?,?)", (t, k401, -200_00, None))
    conn.execute("INSERT INTO splits(transaction_id, transfer_account_id, amount, "
                 "memo) VALUES (?,?,?,?)", (t, house, -300_00, None))
    conn.commit()

    summary = ledger.backfill_split_transfer_mirrors(conn, target_account_id=k401)
    assert summary == {"created": 1, "adopted": 0, "skipped": 0}
    assert len(ledger.register_rows(conn, k401)) == 1
    assert ledger.register_rows(conn, house) == []         # untouched
    assert _pair_id(conn, t, house) is None


def test_clear_splits_reverts(conn, accounts):
    checking, _ = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    t = ledger.add_transaction(conn, checking, "2026-01-05", -100_00)
    ledger.set_splits(conn, t, [(groc, -60_00, None), (dining, -40_00, None)])
    ledger.clear_splits(conn, t)
    assert not ledger.has_splits(conn, t)
    assert ledger.get_splits(conn, t) == []
    assert ledger.category_display(conn, ledger.get_transaction(conn, t)) == ""


def test_split_lines_drive_spending_report(conn, accounts):
    from mammon.reports import spending_by_category
    checking, _ = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    t = ledger.add_transaction(conn, checking, "2026-01-05", -100_00,
                               category_id=groc)
    ledger.set_splits(conn, t, [(groc, -60_00, None), (dining, -40_00, None)])
    rep = spending_by_category(conn, "2026-01-01", "2026-01-31")
    by_path = {r.path: r.total_cents for r in rep.flat()}
    assert by_path.get("Groceries") == 60_00
    assert by_path.get("Dining") == 40_00
    assert rep.total_cents == 100_00           # no double counting


# ---- reconciliation (reconcile an account against a statement) --------------
def test_reconcile_summary_and_finish(conn, accounts):
    checking, _ = accounts                      # opening 100_00
    a = ledger.add_transaction(conn, checking, "2026-01-05", -25_00, payee="Store")
    b = ledger.add_transaction(conn, checking, "2026-01-10", 200_00, payee="Pay")
    ledger.add_transaction(conn, checking, "2026-01-31", -10_00, payee="Later")
    # Nothing reconciled yet: beginning is the opening balance.
    s0 = ledger.reconcile_summary(conn, checking, 275_00)
    assert s0["beginning_balance"] == 100_00
    assert s0["cleared_total"] == 0
    assert s0["difference"] == 175_00
    # Clear the two statement items; the cleared balance now hits the statement.
    ledger.update_transaction(conn, a, cleared=1)
    ledger.update_transaction(conn, b, cleared=1)
    s1 = ledger.reconcile_summary(conn, checking, 275_00)
    assert s1["cleared_total"] == -25_00 + 200_00
    assert s1["cleared_balance"] == 275_00
    assert s1["difference"] == 0
    rid = ledger.finish_reconciliation(conn, checking, "2026-01-15", 275_00)
    assert rid > 0
    assert ledger.get_transaction(conn, a)["reconciled"] == 1
    assert ledger.get_transaction(conn, b)["reconciled"] == 1
    last = ledger.last_reconciliation(conn, checking)
    assert last["statement_balance"] == 275_00 and last["statement_date"] == "2026-01-15"


def test_reconcile_beginning_includes_prior_reconciled(conn, accounts):
    checking, _ = accounts                      # opening 100_00
    a = ledger.add_transaction(conn, checking, "2026-01-05", -25_00)
    b = ledger.add_transaction(conn, checking, "2026-01-10", 200_00)
    c = ledger.add_transaction(conn, checking, "2026-02-01", -10_00)
    ledger.update_transaction(conn, a, cleared=1)
    ledger.update_transaction(conn, b, cleared=1)
    ledger.finish_reconciliation(conn, checking, "2026-01-31", 275_00)
    # Second statement: beginning now folds in the reconciled 175_00.
    ledger.update_transaction(conn, c, cleared=1)
    s = ledger.reconcile_summary(conn, checking, 265_00)
    assert s["beginning_balance"] == 275_00     # opening + prior reconciled
    assert s["cleared_total"] == -10_00
    assert s["difference"] == 0


def test_finish_reconciliation_requires_zero_difference(conn, accounts):
    checking, _ = accounts
    a = ledger.add_transaction(conn, checking, "2026-01-05", -25_00)
    ledger.update_transaction(conn, a, cleared=1)
    with pytest.raises(ValueError):
        ledger.finish_reconciliation(conn, checking, "2026-01-15", 999_00)
    # Nothing was locked in on the failed finish.
    assert ledger.get_transaction(conn, a)["reconciled"] == 0
    assert ledger.last_reconciliation(conn, checking) is None


def test_unreconciled_rows_excludes_reconciled(conn, accounts):
    checking, _ = accounts
    a = ledger.add_transaction(conn, checking, "2026-01-05", -25_00)
    ledger.add_transaction(conn, checking, "2026-01-10", 200_00)
    ledger.update_transaction(conn, a, cleared=1)
    ledger.finish_reconciliation(conn, checking, "2026-01-15",
                                 100_00 - 25_00)   # only `a` cleared
    rows = ledger.unreconciled_rows(conn, checking)
    assert all(not r["reconciled"] for r in rows)
    assert a not in [r["id"] for r in rows]        # the reconciled one is gone


def test_reconcile_summary_bounds_cleared_by_statement_date(conn, accounts):
    """A cleared row dated AFTER the statement date belongs to the NEXT statement.
    Counting it made the reconcile dialog unbalanceable: the row is not shown in
    either pane, so no click and no 'Clear All' could reach it, yet it moved the
    cleared total."""
    checking, _ = accounts                      # opening 100_00
    inside = ledger.add_transaction(conn, checking, "2026-01-10", -25_00)
    later = ledger.add_transaction(conn, checking, "2026-03-20", 500_00)
    ledger.update_transaction(conn, inside, cleared=1)
    ledger.update_transaction(conn, later, cleared=1)

    bounded = ledger.reconcile_summary(conn, checking, 75_00, "2026-01-31")
    assert bounded["cleared_total"] == -25_00
    assert bounded["cleared_after"] == 500_00    # named, not silently folded in
    assert bounded["difference"] == 0            # so January can actually close

    # Unbounded (no statement date) keeps the old whole-account behaviour.
    unbounded = ledger.reconcile_summary(conn, checking, 75_00)
    assert unbounded["cleared_total"] == -25_00 + 500_00
    assert unbounded["cleared_after"] == 0


def test_finish_reconciliation_leaves_later_cleared_rows_for_next_statement(conn, accounts):
    """Finishing January must not lock in a March payment that happens to be
    checked off -- the guard and the UPDATE share the statement-date cutoff."""
    checking, _ = accounts                      # opening 100_00
    inside = ledger.add_transaction(conn, checking, "2026-01-10", -25_00)
    later = ledger.add_transaction(conn, checking, "2026-03-20", 500_00)
    ledger.update_transaction(conn, inside, cleared=1)
    ledger.update_transaction(conn, later, cleared=1)

    ledger.finish_reconciliation(conn, checking, "2026-01-31", 75_00)
    assert ledger.get_transaction(conn, inside)["reconciled"] == 1
    after = ledger.get_transaction(conn, later)
    assert (after["cleared"], after["reconciled"]) == (1, 0)   # still pending
    # And it is the whole beginning balance of the next statement's summary.
    nxt = ledger.reconcile_summary(conn, checking, 575_00, "2026-03-31")
    assert nxt["beginning_balance"] == 75_00
    assert nxt["cleared_total"] == 500_00
    assert nxt["difference"] == 0


# ---- in-progress reconcile drafts -------------------------------------------
def test_reconcile_draft_round_trips_and_survives_partial_save(conn, accounts):
    checking, _ = accounts
    assert ledger.get_reconcile_draft(conn, checking) is None
    ledger.save_reconcile_draft(conn, checking, statement_date="2026-05-31",
                                ending_cents=-1_234_56, charges_cents=99_00)
    d = ledger.get_reconcile_draft(conn, checking)
    assert d["statement_date"] == "2026-05-31"
    assert d["ending_cents"] == -1_234_56
    assert d["charges_cents"] == 99_00
    assert d["credits_cents"] == 0              # untouched fields default
    # A later partial save updates only what it names.
    ledger.save_reconcile_draft(conn, checking, credits_cents=5_00)
    d = ledger.get_reconcile_draft(conn, checking)
    assert (d["credits_cents"], d["statement_date"]) == (5_00, "2026-05-31")
    # A typo'd field is refused rather than silently discarded.
    with pytest.raises(ValueError):
        ledger.save_reconcile_draft(conn, checking, ending=1)
    ledger.clear_reconcile_draft(conn, checking)
    assert ledger.get_reconcile_draft(conn, checking) is None


def test_finishing_a_reconciliation_drops_its_draft(conn, accounts):
    checking, _ = accounts
    a = ledger.add_transaction(conn, checking, "2026-01-05", -25_00)
    ledger.update_transaction(conn, a, cleared=1)
    ledger.save_reconcile_draft(conn, checking, statement_date="2026-01-31",
                                ending_cents=75_00)
    ledger.finish_reconciliation(conn, checking, "2026-01-31", 75_00)
    assert ledger.get_reconcile_draft(conn, checking) is None


def test_implied_card_beginning_recovers_the_previous_balance(conn):
    """A card statement is closed arithmetic, so the previous balance falls out of
    the printed totals -- no beginning balance need be typed or looked up."""
    # Owed 500 before; 120 of charges and a 5 finance charge; 30 paid, 7 credited.
    # 500 + 120 + 5 - 30 - 7 = 588 ending.
    assert ledger.implied_card_beginning(
        charges=120_00, payments=30_00, credits=7_00,
        finance=5_00, ending=588_00) == -500_00
    # A card paid off to zero implies nothing owed at the start.
    assert ledger.implied_card_beginning(
        charges=50_00, payments=50_00, credits=0, finance=0, ending=0) == 0


def test_reconcile_summary_beginning_override_ignores_the_r_rows(conn, accounts):
    """The override is what keeps a card reconcile off the register's reconciled
    history: whatever the R rows sum to, the statement's implied beginning wins."""
    checking, _ = accounts                      # opening 100_00
    hist = ledger.add_transaction(conn, checking, "2025-11-24", -3_142_00)
    ledger.update_transaction(conn, hist, cleared=1, reconciled=1)
    item = ledger.add_transaction(conn, checking, "2026-01-10", -25_00)
    ledger.update_transaction(conn, item, cleared=1)

    # Derived from the register, the beginning carries the imported history.
    derived = ledger.reconcile_summary(conn, checking, 0, "2026-01-31")
    assert derived["beginning_balance"] == 100_00 - 3_142_00

    # Overridden, it does not -- and the difference closes on the checked item.
    forced = ledger.reconcile_summary(conn, checking, -525_00, "2026-01-31",
                                      beginning_balance=-500_00)
    assert forced["beginning_balance"] == -500_00
    assert forced["cleared_total"] == -25_00
    assert forced["difference"] == 0
    # finish_reconciliation applies the same override, so its guard agrees.
    ledger.finish_reconciliation(conn, checking, "2026-01-31", -525_00,
                                 beginning_balance=-500_00)
    assert ledger.get_transaction(conn, item)["reconciled"] == 1


# --------------------------------------------------------------------------
# The report exclusion toggle (SRD 5.9u)
# --------------------------------------------------------------------------
#
# WHERE an exclusion tag may live is a fact about tag STORAGE, which is why the
# rule is here and not in the report window: a transaction holds many tags (the
# junction), a split leg holds exactly one (``splits.tag_id``, a single column),
# and a parent's tags reach every leg. Both refusals below exist to stop a
# precise-looking gesture from doing something blunt.

def _toggle_fixture(conn):
    acct = ledger.create_account(conn, "Checking", "checking")
    cat = ledger.resolve_category(conn, "Rental:Rent")
    return acct, cat


def test_exclusion_joins_a_transactions_comma_list(conn):
    acct, cat = _toggle_fixture(conn)
    txn = ledger.add_transaction(conn, acct, "2025-03-01", 1000_00,
                                 category_id=cat)
    ledger.set_tags(conn, txn, ["7344 Muirfield"])

    assert ledger.toggle_report_exclusion(conn, "Rents received", txn) is True
    assert ledger.get_tags(conn, txn) == ["7344 Muirfield", "!Rents received"]
    # The cache the legacy readers use stays in step.
    assert conn.execute("SELECT tag FROM transactions WHERE id = ?",
                        (txn,)).fetchone()[0] == \
        "7344 Muirfield, !Rents received"

    assert ledger.toggle_report_exclusion(conn, "Rents received", txn) is False
    assert ledger.get_tags(conn, txn) == ["7344 Muirfield"]


def test_exclusion_matching_is_case_insensitive(conn):
    """``tags.name`` collates NOCASE, so one spelling must toggle the other or a
    line could collect two exclusions that look like one."""
    acct, cat = _toggle_fixture(conn)
    txn = ledger.add_transaction(conn, acct, "2025-03-01", 100_00,
                                 category_id=cat)
    ledger.set_tags(conn, txn, ["!rents received"])
    assert ledger.toggle_report_exclusion(conn, "Rents received", txn) is False
    assert ledger.get_tags(conn, txn) == []


def test_an_untagged_split_leg_takes_the_exclusion_on_its_own(conn):
    acct, cat = _toggle_fixture(conn)
    txn = ledger.add_transaction(conn, acct, "2025-03-01", 1000_00)
    ledger.set_splits(conn, txn, [
        {"category_id": cat, "amount": 600_00},
        {"category_id": cat, "amount": 400_00},
    ])
    legs = ledger.get_splits(conn, txn)
    assert ledger.toggle_report_exclusion(
        conn, "Rents received", txn, legs[0]["id"]) is True
    assert ledger.split_leg_tag(conn, legs[0]["id"]) == "!Rents received"
    # Only that leg, and never the parent.
    assert ledger.split_leg_tag(conn, legs[1]["id"]) is None
    assert ledger.get_tags(conn, txn) == []

    assert ledger.toggle_report_exclusion(
        conn, "Rents received", txn, legs[0]["id"]) is False
    assert ledger.split_leg_tag(conn, legs[0]["id"]) is None


def test_a_tagged_split_leg_refuses_and_names_the_tag_in_the_way(conn):
    """A leg holds ONE tag. Overwriting the property tag would destroy the very
    attribution that put the line in the report."""
    acct, cat = _toggle_fixture(conn)
    txn = ledger.add_transaction(conn, acct, "2025-03-01", 1000_00)
    ledger.set_splits(conn, txn, [
        {"category_id": cat, "amount": 600_00, "tag": "7344 Muirfield"},
        {"category_id": cat, "amount": 400_00},
    ])
    legs = ledger.get_splits(conn, txn)
    with pytest.raises(ValueError) as err:
        ledger.toggle_report_exclusion(conn, "Rents received", txn,
                                       legs[0]["id"])
    assert "7344 Muirfield" in str(err.value)
    assert "only one tag" in str(err.value)
    assert ledger.split_leg_tag(conn, legs[0]["id"]) == "7344 Muirfield"


def test_a_parent_with_tagged_legs_refuses_the_whole_transaction(conn):
    """The parent's tags are the union applied to every leg, so an exclusion
    there would drop legs a per-leg tag deliberately pulled in."""
    acct, cat = _toggle_fixture(conn)
    txn = ledger.add_transaction(conn, acct, "2025-03-01", 1000_00)
    ledger.set_splits(conn, txn, [
        {"category_id": cat, "amount": 600_00, "tag": "7344 Muirfield"},
        {"category_id": cat, "amount": 400_00},
    ])
    with pytest.raises(ValueError) as err:
        ledger.toggle_report_exclusion(conn, "Rents received", txn)
    assert "7344 Muirfield" in str(err.value)
    assert "split line instead" in str(err.value)
    assert ledger.get_tags(conn, txn) == []


def test_a_split_with_no_tagged_legs_can_be_excluded_whole(conn):
    acct, cat = _toggle_fixture(conn)
    txn = ledger.add_transaction(conn, acct, "2025-03-01", 1000_00)
    ledger.set_splits(conn, txn, [
        {"category_id": cat, "amount": 600_00},
        {"category_id": cat, "amount": 400_00},
    ])
    assert ledger.toggle_report_exclusion(conn, "Rents received", txn) is True
    assert ledger.get_tags(conn, txn) == ["!Rents received"]


def test_set_split_tag_moves_one_column_and_leaves_the_siblings(conn):
    acct, cat = _toggle_fixture(conn)
    txn = ledger.add_transaction(conn, acct, "2025-03-01", 1000_00)
    ledger.set_splits(conn, txn, [
        {"category_id": cat, "amount": 600_00, "tag": "7344 Muirfield"},
        {"category_id": cat, "amount": 400_00, "tag": "6054 Mapleview"},
    ])
    legs = ledger.get_splits(conn, txn)
    ledger.set_split_tag(conn, legs[0]["id"], "Renamed")
    assert ledger.split_leg_tag(conn, legs[0]["id"]) == "Renamed"
    assert ledger.split_leg_tag(conn, legs[1]["id"]) == "6054 Mapleview"
    assert [s["amount"] for s in ledger.get_splits(conn, txn)] == \
        [600_00, 400_00]
    ledger.set_split_tag(conn, legs[0]["id"], None)
    assert ledger.split_leg_tag(conn, legs[0]["id"]) is None


def test_the_toggle_refuses_a_row_that_is_not_there(conn):
    with pytest.raises(KeyError):
        ledger.toggle_report_exclusion(conn, "Rents received", 9999)
    with pytest.raises(KeyError):
        ledger.set_split_tag(conn, 9999, "x")


def test_a_comma_in_the_item_name_refuses_the_exclusion(conn):
    """``transactions.tag`` is comma-joined, so ``!Div inc., non-taxable`` would
    be read back as TWO tags -- an exclusion matching nothing, plus a junk tag in
    the user's vocabulary. Fourteen lines of the packaged Quicken tax definition
    have a comma in the name, so this is the common case, not a corner."""
    acct, cat = _toggle_fixture(conn)
    txn = ledger.add_transaction(conn, acct, "2025-03-01", 100_00,
                                 category_id=cat)
    with pytest.raises(ValueError) as err:
        ledger.toggle_report_exclusion(conn, "Div inc., non-taxable", txn)
    assert "comma" in str(err.value)
    # Nothing was written -- not the exclusion, and not a stray tag either.
    assert ledger.get_tags(conn, txn) == []
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
