"""Tests for share reconciliation (SRD 5.11b) -- the share equivalent of the
cash register's reconcile, in mammon.investments.

The share balance is to a security what the cash balance is to a bank account,
and an untickered 401(k) fund is the case that forces it: no quote exists, so
the statement's share count is the only truth. What is covered here:

  * the full life cycle over two statement periods (clear, finish, carry the
    reconciled balance into the next period);
  * a leftover uncleared row leaves a non-zero difference and finish refuses;
  * a period SPANNING a stock split reconciles correctly -- the split RESCALES
    the running balance, so the number a naive buys-minus-sells sum would get is
    explicitly asserted NOT to be the answer;
  * a security renamed mid-history with a security_aliases row sums as ONE
    identity, so no bogus adjustment is offered (Quicken's huge share
    adjustments were usually exactly this);
  * an adjustment closes a real gap, is identifiable, and can be deleted later
    without the reconciliation silently un-reconciling itself;
  * finishing the same period twice is idempotent.

All data is synthetic: ANON fund names, no real balances, no PII.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, investments, ledger, portfolio


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "sharerecon.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "ANON 401(k)", "investment",
                                 opening_balance=0)


FUND = "ANONBAL"          # an internal 401(k) fund: no ticker, no quotes


def _buy(conn, a, date, qty, sym=FUND, price="10.00"):
    amount = -int(Decimal(qty) * Decimal(price) * 100)
    return investments.record_investment(conn, a, date, "Buy", symbol=sym,
                                         quantity=qty, price=price,
                                         amount=amount)


def _sell(conn, a, date, qty, sym=FUND, price="10.00"):
    amount = int(Decimal(qty) * Decimal(price) * 100)
    return investments.record_investment(conn, a, date, "Sell", symbol=sym,
                                         quantity=qty, price=price,
                                         amount=amount)


def _split(conn, a, date, ratio, sym=FUND):
    r = investments.parse_split_ratio(ratio)
    return investments.record_investment(
        conn, a, date, "StkSplit", symbol=sym,
        quantity=investments.split_stored(r),
        split_num=r.numerator, split_den=r.denominator)


def _clear_all(conn, summary):
    for row in summary["uncleared_rows"]:
        if not row["split"]:
            investments.set_investment_cleared(conn, row["id"])


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def test_migration_65_creates_share_reconcile_shapes(conn):
    """_V65 is applied by a plain init_db, and the schema version tracks it."""
    names = db.table_names(conn)
    assert "share_reconciliations" in names
    assert "share_reconcile_drafts" in names
    cols = {r[1] for r in conn.execute("PRAGMA table_info(investment_transactions)")}
    assert {"cleared", "reconciled"} <= cols
    assert db.SCHEMA_VERSION == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_only_quantity_changing_actions_are_reconciled(conn, acct):
    """A Div moves cash, not shares, so it never appears as a line to clear --
    "we clear the transactions for each security that changes the number of
    shares"."""
    _buy(conn, acct, "2026-01-05", "100")
    investments.record_investment(conn, acct, "2026-01-20", "Div", symbol=FUND,
                                  amount=25_00)
    rows = investments.share_reconcile_rows(conn, acct, FUND)
    assert [r["action"] for r in rows] == ["Buy"]
    assert investments.is_quantity_action("Div") is False
    assert investments.is_quantity_action("IntInc") is False
    assert investments.is_quantity_action("StkSplit") is True
    assert investments.is_quantity_action("shrs in") is True    # normalized


# ---------------------------------------------------------------------------
# (a) the life cycle: two statement periods
# ---------------------------------------------------------------------------
def test_two_periods_reconcile_to_stated_ending_counts(conn, acct):
    _buy(conn, acct, "2026-01-15", "100")
    _buy(conn, acct, "2026-02-10", "50.5")

    s1 = investments.share_reconcile_summary(conn, acct, FUND, "2026-02-28",
                                             "150.5")
    assert s1["prior_qty"] == Decimal(0)
    assert len(s1["uncleared_rows"]) == 2
    assert s1["computed_ending_qty"] == Decimal(0)       # nothing cleared yet
    assert s1["difference"] == Decimal("150.5")

    _clear_all(conn, s1)
    s1 = investments.share_reconcile_summary(conn, acct, FUND, "2026-02-28",
                                             "150.5")
    assert s1["computed_ending_qty"] == Decimal("150.5")
    assert s1["difference"] == 0
    rec1 = investments.finish_share_reconciliation(conn, acct, FUND,
                                                   "2026-02-28", "150.5")

    row = conn.execute("SELECT * FROM share_reconciliations WHERE id=?",
                       (rec1,)).fetchone()
    assert (row["symbol"], row["starting_qty"], row["ending_qty"]) == \
        (FUND, "0", "150.5")
    assert row["adjustment_txn_id"] is None

    # Second period: the reconciled balance carries in as prior_qty.
    _sell(conn, acct, "2026-03-10", "25.5")
    _buy(conn, acct, "2026-04-05", "10")
    s2 = investments.share_reconcile_summary(conn, acct, FUND, "2026-04-30",
                                             "135")
    assert s2["prior_qty"] == Decimal("150.5")
    assert s2["prior_statement_date"] == "2026-02-28"
    assert s2["prior_statement_qty"] == Decimal("150.5")
    assert len(s2["reconciled_rows"]) == 2
    assert len(s2["uncleared_rows"]) == 2

    _clear_all(conn, s2)
    s2 = investments.share_reconcile_summary(conn, acct, FUND, "2026-04-30",
                                             "135")
    assert s2["cleared_qty_change"] == Decimal("-15.5")
    assert s2["computed_ending_qty"] == Decimal("135")
    assert s2["difference"] == 0
    rec2 = investments.finish_share_reconciliation(conn, acct, FUND,
                                                   "2026-04-30", "135")
    assert rec2 != rec1
    assert [r["ending_qty"] for r in
            investments.list_share_reconciliations(conn, acct)] == ["150.5", "135"]
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions WHERE reconciled=1"
    ).fetchone()[0] == 4
    # The shares the reconciliation agreed on are the shares Holdings reports.
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, FUND)["quantity"] == "135"


def test_typed_starting_count_replaces_the_derived_one(conn, acct):
    """Like the cash dialog's beginning balance: when the user types the
    statement's starting share count, unreconciled history is not re-counted."""
    _buy(conn, acct, "2026-01-15", "40")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31",
                                            "1040", starting_qty="1000")
    assert s["prior_qty"] == Decimal("1000")
    assert s["starting_qty_given"] == Decimal("1000")
    _clear_all(conn, s)
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31",
                                            "1040", starting_qty="1000")
    assert s["computed_ending_qty"] == Decimal("1040")
    assert s["difference"] == 0


# ---------------------------------------------------------------------------
# (b) a leftover uncleared row
# ---------------------------------------------------------------------------
def test_uncleared_row_leaves_a_difference_and_finish_refuses(conn, acct):
    _buy(conn, acct, "2026-01-15", "100")
    _buy(conn, acct, "2026-02-10", "7")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-02-28", "107")
    investments.set_investment_cleared(conn, s["uncleared_rows"][0]["id"])

    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-02-28", "107")
    assert len(s["cleared_rows"]) == 1
    assert len(s["uncleared_rows"]) == 1
    assert s["computed_ending_qty"] == Decimal("100")
    assert s["difference"] == Decimal("7")

    with pytest.raises(ValueError) as exc:
        investments.finish_share_reconciliation(conn, acct, FUND, "2026-02-28",
                                                "107")
    assert "off by 7 shares" in str(exc.value)
    # nothing was stamped and nothing was recorded
    assert investments.list_share_reconciliations(conn, acct) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions WHERE reconciled=1"
    ).fetchone()[0] == 0


def test_uncleared_row_cannot_be_hidden_by_a_later_date(conn, acct):
    """Rows after the statement date are simply out of the period."""
    _buy(conn, acct, "2026-01-15", "100")
    _buy(conn, acct, "2026-03-02", "9")          # next period's activity
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-02-28", "100")
    assert [r["date"] for r in s["uncleared_rows"]] == ["2026-01-15"]
    _clear_all(conn, s)
    investments.finish_share_reconciliation(conn, acct, FUND, "2026-02-28", "100")
    later = conn.execute(
        "SELECT reconciled FROM investment_transactions WHERE date='2026-03-02'"
    ).fetchone()["reconciled"]
    assert later == 0


# ---------------------------------------------------------------------------
# (c) a period spanning a stock split
# ---------------------------------------------------------------------------
def test_period_spanning_a_split_reconciles_and_beats_the_naive_sum(conn, acct):
    """The running balance must be MULTIPLIED by M/N on the split date and then
    continued. Summing buys minus sells across a split is the bug."""
    _buy(conn, acct, "2026-01-05", "100")
    s0 = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "100")
    _clear_all(conn, s0)
    investments.finish_share_reconciliation(conn, acct, FUND, "2026-01-31", "100")

    _split(conn, acct, "2026-02-17", "2:1")
    _buy(conn, acct, "2026-03-01", "20")

    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-03-31", "220")
    assert [r["split_display"] for r in s["split_rows"]] == ["2:1"]
    assert s["prior_qty"] == Decimal("200")        # the reconciled 100, doubled
    # the split is not a line the user clears; the buy is
    assert [r["date"] for r in s["uncleared_rows"]] == ["2026-03-01"]
    _clear_all(conn, s)

    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-03-31", "220")
    naive = Decimal("100") + Decimal("20")         # what summing quantities gets
    assert s["computed_ending_qty"] == Decimal("220")
    assert s["computed_ending_qty"] != naive
    assert s["difference"] == 0

    rec = investments.finish_share_reconciliation(conn, acct, FUND, "2026-03-31",
                                                  "220")
    assert conn.execute("SELECT ending_qty FROM share_reconciliations WHERE id=?",
                        (rec,)).fetchone()["ending_qty"] == "220"
    # the split row is stamped along with the cleared rows: the statement's
    # share count already reflects it
    split_row = conn.execute(
        "SELECT cleared, reconciled FROM investment_transactions "
        "WHERE action='StkSplit'").fetchone()
    assert (split_row["cleared"], split_row["reconciled"]) == (1, 1)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, FUND)["quantity"] == "220"


def test_reverse_split_inside_one_period_is_exact(conn, acct):
    """300 shares 1:3 then a 10-share buy = 110, exactly (multiply before
    divide), and every row of the period reconciles in one pass."""
    _buy(conn, acct, "2026-01-05", "300")
    _split(conn, acct, "2026-02-17", "1:3")
    _buy(conn, acct, "2026-03-01", "10")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-03-31", "110")
    _clear_all(conn, s)
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-03-31", "110")
    assert s["computed_ending_qty"] == Decimal("110")
    assert s["difference"] == 0
    investments.finish_share_reconciliation(conn, acct, FUND, "2026-03-31", "110")


# ---------------------------------------------------------------------------
# (d) a renamed security is one identity
# ---------------------------------------------------------------------------
def test_renamed_security_sums_as_one_identity_with_no_adjustment(conn, acct):
    """Quicken's giant share adjustments were usually an unrenamed security.
    The alias table means the old spelling's shares are summed in FIRST, so no
    adjustment is offered at all."""
    old, new = "ANONOLD", "ANONNEW"
    portfolio.set_security(conn, old, name="ANON Balanced Fund (old)")
    portfolio.set_security(conn, new, name="ANON Balanced Fund")
    _buy(conn, acct, "2026-01-15", "60", sym=old)
    _buy(conn, acct, "2026-02-10", "40", sym=new)
    investments.add_alias(conn, old, new)

    s = investments.share_reconcile_summary(conn, acct, new, "2026-02-28", "100")
    assert s["symbol"] == new
    assert set(s["identity_symbols"]) == {old, new}
    assert {r["symbol"] for r in s["uncleared_rows"]} == {old, new}
    _clear_all(conn, s)

    s = investments.share_reconcile_summary(conn, acct, new, "2026-02-28", "100")
    assert s["computed_ending_qty"] == Decimal("100")   # NOT just the 40 new-name
    assert s["difference"] == 0
    assert s["adjustment_qty"] == 0

    # reconciling by the OLD spelling resolves to the same canonical period
    s_old = investments.share_reconcile_summary(conn, acct, old, "2026-02-28",
                                                "100")
    assert s_old["symbol"] == new
    assert s_old["difference"] == 0

    rec = investments.finish_share_reconciliation(conn, acct, old, "2026-02-28",
                                                   "100")
    assert conn.execute("SELECT symbol FROM share_reconciliations WHERE id=?",
                        (rec,)).fetchone()["symbol"] == new
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions WHERE reconciled=1"
    ).fetchone()[0] == 2
    assert investments.list_share_adjustments(conn, acct) == []


def test_alias_identity_spans_a_split_too(conn, acct):
    old, new = "ANONOLD2", "ANONNEW2"
    portfolio.set_security(conn, old, name="ANON Growth (old)")
    portfolio.set_security(conn, new, name="ANON Growth")
    _buy(conn, acct, "2026-01-15", "50", sym=old)
    _split(conn, acct, "2026-02-01", "2:1", sym=old)
    _buy(conn, acct, "2026-03-01", "5", sym=new)
    investments.add_alias(conn, old, new)

    s = investments.share_reconcile_summary(conn, acct, new, "2026-03-31", "105")
    _clear_all(conn, s)
    s = investments.share_reconcile_summary(conn, acct, new, "2026-03-31", "105")
    assert s["computed_ending_qty"] == Decimal("105")
    assert s["difference"] == 0


# ---------------------------------------------------------------------------
# (e) an adjustment closes a real gap -- and can be deleted
# ---------------------------------------------------------------------------
def test_adjustment_closes_a_gap_and_stays_deletable(conn, acct):
    _buy(conn, acct, "2026-01-15", "100")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "112")
    _clear_all(conn, s)
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "112")
    assert s["difference"] == Decimal("12")
    assert s["adjustment_qty"] == Decimal("12")
    # the domain hands the UI the sentence the user has to be told
    assert "reconciled again by hand" in s["adjustment_warning"]

    rec = investments.finish_share_reconciliation(conn, acct, FUND, "2026-01-31",
                                                  "112", adjust=True,
                                                  adjust_memo="ANON statement")
    row = conn.execute("SELECT * FROM share_reconciliations WHERE id=?",
                       (rec,)).fetchone()
    assert row["ending_qty"] == "112"
    adj_id = row["adjustment_txn_id"]
    assert adj_id is not None

    adj = investments.get_investment_txn(conn, adj_id)
    assert adj["action"] == "ShrsIn"          # an EXISTING action, not a new one
    assert adj["quantity"] == "12"
    assert adj["symbol"] == FUND
    assert investments.is_share_adjustment(adj)
    assert adj["reconciled"] == 1
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, FUND)["quantity"] == "112"

    listed = investments.list_share_adjustments(conn, acct, FUND)
    assert [a["id"] for a in listed] == [adj_id]
    assert listed[0]["reconciliation_ids"] == [rec]
    assert "delete it later" in listed[0]["warning"]

    # ... and later the missing shares turn up, so the user deletes it.
    out = investments.delete_share_adjustment(conn, adj_id)
    assert out["deleted"] is True
    assert out["reconciliation_ids"] == [rec]
    assert "reconciled again by hand" in out["warning"]
    assert investments.get_investment_txn(conn, adj_id) is None

    after = conn.execute("SELECT * FROM share_reconciliations WHERE id=?",
                         (rec,)).fetchone()
    assert after is not None                  # the reconciliation is NOT undone
    assert after["adjustment_txn_id"] is None
    assert "by hand" in after["note"]
    # and the gap is honestly open again: restoring it is manual
    again = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31",
                                                "112")
    assert again["difference"] == Decimal("12")


def test_adjustment_can_remove_shares_and_zero_is_refused(conn, acct):
    _buy(conn, acct, "2026-01-15", "100")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "95")
    _clear_all(conn, s)
    rec = investments.finish_share_reconciliation(conn, acct, FUND, "2026-01-31",
                                                  "95", adjust=True)
    adj_id = conn.execute(
        "SELECT adjustment_txn_id FROM share_reconciliations WHERE id=?",
        (rec,)).fetchone()["adjustment_txn_id"]
    adj = investments.get_investment_txn(conn, adj_id)
    assert (adj["action"], adj["quantity"]) == ("ShrsOut", "5")

    with pytest.raises(ValueError):
        investments.record_share_adjustment(conn, acct, FUND, "2026-01-31", "0")


def test_adjust_is_a_no_op_when_the_difference_is_already_zero(conn, acct):
    _buy(conn, acct, "2026-01-15", "100")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "100")
    _clear_all(conn, s)
    investments.finish_share_reconciliation(conn, acct, FUND, "2026-01-31", "100",
                                            adjust=True)
    assert investments.list_share_adjustments(conn, acct) == []


# ---------------------------------------------------------------------------
# (f) idempotence
# ---------------------------------------------------------------------------
def test_finishing_the_same_period_twice_is_idempotent(conn, acct):
    _buy(conn, acct, "2026-01-15", "100")
    _split(conn, acct, "2026-01-20", "3:2")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "150")
    _clear_all(conn, s)
    first = investments.finish_share_reconciliation(conn, acct, FUND,
                                                    "2026-01-31", "150")
    before = conn.execute(
        "SELECT id, cleared, reconciled FROM investment_transactions "
        "ORDER BY id").fetchall()

    again = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31",
                                                "150")
    assert again["prior_qty"] == Decimal("150")
    assert again["computed_ending_qty"] == Decimal("150")
    assert again["difference"] == 0
    assert again["cleared_rows"] == []

    second = investments.finish_share_reconciliation(conn, acct, FUND,
                                                     "2026-01-31", "150")
    assert second == first
    assert len(investments.list_share_reconciliations(conn, acct)) == 1
    assert [tuple(r) for r in conn.execute(
        "SELECT id, cleared, reconciled FROM investment_transactions "
        "ORDER BY id")] == [tuple(r) for r in before]


def test_reconciled_row_cannot_be_uncleared(conn, acct):
    _buy(conn, acct, "2026-01-15", "100")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "100")
    txn_id = s["uncleared_rows"][0]["id"]
    _clear_all(conn, s)
    investments.finish_share_reconciliation(conn, acct, FUND, "2026-01-31", "100")
    assert investments.set_investment_cleared(conn, txn_id, False) is False
    assert conn.execute("SELECT cleared FROM investment_transactions WHERE id=?",
                        (txn_id,)).fetchone()["cleared"] == 1


# ---------------------------------------------------------------------------
# drafts and input validation
# ---------------------------------------------------------------------------
def test_draft_round_trips_and_finish_clears_it(conn, acct):
    assert investments.get_share_reconcile_draft(conn, acct, FUND) is None
    investments.save_share_reconcile_draft(
        conn, acct, FUND, statement_date="2026-01-31", starting_qty="0",
        ending_qty="100", starting_price="10.00", ending_price="11.00")
    draft = investments.get_share_reconcile_draft(conn, acct, FUND)
    assert draft == {"statement_date": "2026-01-31", "starting_qty": "0",
                     "ending_qty": "100", "starting_price": "10.00",
                     "ending_price": "11.00"}

    _buy(conn, acct, "2026-01-15", "100")
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "100")
    _clear_all(conn, s)
    investments.finish_share_reconciliation(conn, acct, FUND, "2026-01-31", "100")
    assert investments.get_share_reconcile_draft(conn, acct, FUND) is None


def test_draft_is_per_security_and_keyed_by_the_canonical_symbol(conn, acct):
    old, new = "ANONOLD3", "ANONNEW3"
    portfolio.set_security(conn, old, name="ANON (old)")
    portfolio.set_security(conn, new, name="ANON")
    investments.add_alias(conn, old, new)
    investments.save_share_reconcile_draft(conn, acct, old, ending_qty="7")
    assert investments.get_share_reconcile_draft(conn, acct, new)["ending_qty"] == "7"

    investments.save_share_reconcile_draft(conn, acct, FUND, ending_qty="3")
    assert investments.get_share_reconcile_draft(conn, acct, FUND)["ending_qty"] == "3"
    investments.clear_share_reconcile_draft(conn, acct, FUND)
    assert investments.get_share_reconcile_draft(conn, acct, FUND) is None
    assert investments.get_share_reconcile_draft(conn, acct, new) is not None

    investments.clear_share_reconcile_draft(conn, acct)
    assert investments.get_share_reconcile_draft(conn, acct, new) is None

    with pytest.raises(ValueError):
        investments.save_share_reconcile_draft(conn, acct, FUND, shares="7")


def test_summary_validates_symbol_and_date(conn, acct):
    with pytest.raises(ValueError):
        investments.share_reconcile_summary(conn, acct, "", "2026-01-31", "0")
    with pytest.raises(ValueError):
        investments.share_reconcile_summary(conn, acct, FUND, "01/31/2026", "0")


def test_voided_row_is_out_of_the_share_math(conn, acct):
    _buy(conn, acct, "2026-01-15", "100")
    bad = _buy(conn, acct, "2026-01-20", "40")
    investments.void_investment(conn, bad)
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "100")
    assert [r["date"] for r in s["uncleared_rows"]] == ["2026-01-15"]
    _clear_all(conn, s)
    s = investments.share_reconcile_summary(conn, acct, FUND, "2026-01-31", "100")
    assert s["difference"] == 0
