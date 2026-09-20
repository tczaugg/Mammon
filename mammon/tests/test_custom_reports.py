"""Tests for mammon.reports.custom: user-defined report definitions (SRD 5.9).

Phase 1 is the domain layer only -- the tables, CRUD over definitions and their
items, and ``evaluate`` for the three kinds that exist so far (SOSC, EDAB,
SDAB). The life-cycle test below is the acceptance bar, and it exists to pin the
two properties the feature is FOR:

* a selection is keyed by category ID, so renaming a category cannot silently
  change a report's numbers (the failure this design exists to prevent); and
* ``include_subtree`` is a flag expanded at evaluation, so a child category
  created later is picked up with no edit to the item.

Plus the arithmetic identity that makes the balance kinds trustworthy: a
starting balance taken as of the day BEFORE the range, plus every flow inside
the range, equals the ending balance exactly.

Phase 2 adds the TAG OVERRIDES, and its own life-cycle test below walks the
whole precedence order on one report: exclusion beats inclusion beats the
category selection, on unsplit rows, on split legs and on transfer legs, with
the coverage panel as the record of what the arithmetic could not settle.

All data here is synthetic.
"""
from __future__ import annotations

import json

import pytest

from mammon import db, ledger
from mammon.reports import custom, report_defs
from mammon.reports._lines import signed_lines

START = "2025-01-01"
END = "2025-12-31"


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "custom.db")
    yield c
    c.close()


@pytest.fixture
def ledger_data(conn):
    """Two accounts, three categories, and rental money on both range edges."""
    checking = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=1000_00,
                                     opening_date="2024-01-01")
    brokerage = ledger.create_account(conn, "Brokerage", "investment")

    salary = ledger.resolve_category(conn, "Salary")
    rental = ledger.resolve_category(conn, "Rental")
    repairs = ledger.resolve_category(conn, "Rental:Repairs")

    # Rental money on both sides of the boundary, and ON both boundary dates.
    ledger.add_transaction(conn, checking, "2024-12-31", 500_00, category_id=rental)
    ledger.add_transaction(conn, checking, START, 1200_00, category_id=rental)
    ledger.add_transaction(conn, checking, "2025-06-15", -300_00, category_id=repairs)
    ledger.add_transaction(conn, checking, END, 1100_00, category_id=rental)
    ledger.add_transaction(conn, checking, "2026-01-02", 900_00, category_id=rental)
    # Money that is in the range but not in the rental item.
    ledger.add_transaction(conn, checking, "2025-03-10", 2500_00, category_id=salary)
    ledger.add_transaction(conn, checking, "2025-07-04", -45_00)      # uncategorized

    return {
        "checking": checking, "brokerage": brokerage,
        "salary": salary, "rental": rental, "repairs": repairs,
    }


@pytest.fixture
def report(conn, ledger_data):
    """A fixed-2025 report: rental net (subtree), and Checking's end/start
    balances."""
    d = ledger_data
    rid = custom.create_report(conn, "Schedule E 2025", kind="tax",
                               range_kind="fixed", range_start=START,
                               range_end=END)
    rental_item = custom.add_item(conn, rid, "rental_net", "SOSC",
                                  label="Rental net")
    custom.set_item_categories(conn, rental_item, [(d["rental"], 1)])

    end_item = custom.add_item(conn, rid, "checking_end", "EDAB")
    custom.set_item_accounts(conn, end_item, [d["checking"]])

    start_item = custom.add_item(conn, rid, "checking_start", "SDAB")
    custom.set_item_accounts(conn, start_item, [d["checking"]])

    return {"id": rid, "rental_item": rental_item, "end_item": end_item,
            "start_item": start_item, **d}


# --------------------------------------------------------------------------
# The life-cycle test: the acceptance bar for Phase 1
# --------------------------------------------------------------------------

def test_report_life_cycle(conn, report):
    rid = report["id"]

    # -- evaluate: exact cents, boundary dates included, outside dates not ----
    ev = custom.evaluate(conn, rid)
    assert (ev.start, ev.end) == (START, END)
    assert [r.name for r in ev.rows] == ["rental_net", "checking_end",
                                         "checking_start"]
    # 1200.00 (on start) - 300.00 + 1100.00 (on end); the 2024 and 2026 rental
    # rows are outside the range.
    assert ev.amount("rental_net") == 2000_00
    # Opening balance is as of the day BEFORE start: 1000.00 + the 2024-12-31 row.
    assert ev.amount("checking_start") == 1500_00
    # ... and the ending balance is that plus every 2025 flow.
    assert ev.amount("checking_end") == 1500_00 + 2000_00 + 2500_00 - 45_00

    # -- a NEW child category is picked up with no edit to the item ----------
    insurance = ledger.resolve_category(conn, "Rental:Insurance")
    ledger.add_transaction(conn, report["checking"], "2025-09-01", -220_00,
                           category_id=insurance)
    assert custom.item_categories(conn, report["rental_item"]) == \
        [(report["rental"], 1)]                      # untouched
    ev2 = custom.evaluate(conn, rid)
    assert ev2.amount("rental_net") == 2000_00 - 220_00

    # -- renaming a category changes NOTHING: the selection is id-keyed -------
    ledger.rename_category(conn, report["rental"], "Rentals")
    ev3 = custom.evaluate(conn, rid)
    assert [(r.name, r.amount) for r in ev3.rows] == \
           [(r.name, r.amount) for r in ev2.rows]

    # -- deleting an account cascades its selection row and survives ----------
    brokerage_item = custom.add_item(conn, rid, "brokerage_end", "EDAB")
    custom.set_item_accounts(conn, brokerage_item, [report["brokerage"]])
    assert custom.item_accounts(conn, brokerage_item) == [report["brokerage"]]

    conn.execute("DELETE FROM accounts WHERE id = ?", (report["brokerage"],))
    conn.commit()

    assert custom.item_accounts(conn, brokerage_item) == []
    ev4 = custom.evaluate(conn, rid)
    assert ev4.amount("brokerage_end") == 0          # no accounts left to sum
    assert ev4.amount("rental_net") == ev3.amount("rental_net")
    assert ev4.amount("checking_end") == ev3.amount("checking_end")


# --------------------------------------------------------------------------
# The balance identity
# --------------------------------------------------------------------------

def test_sdab_plus_flows_equals_edab(conn, report):
    """SDAB is the OPENING balance -- as of the day before the range -- so it
    plus every flow inside the range is the ending balance to the cent. Taken
    as of ``start`` itself it would double-count the first day."""
    # A transfer as well, so the identity covers money that is neither income
    # nor expense (hence transfers='all' below).
    ledger.create_transfer(conn, report["checking"], report["brokerage"],
                           "2025-05-05", 400_00)

    ev = custom.evaluate(conn, report["id"])
    flows = sum(line.amount for line in signed_lines(
        conn, START, END, [report["checking"]], transfers="all"))
    assert ev.amount("checking_start") + flows == ev.amount("checking_end")


def test_sdab_is_the_day_before_start(conn, report):
    """The boundary itself: a transaction dated exactly on ``start`` belongs to
    the range, never to the opening balance."""
    ev = custom.evaluate(conn, report["id"])
    before = custom.evaluate(conn, report["id"], start="2024-12-31", end=END)
    # Moving the range one day earlier moves the 2024-12-31 rental row out of
    # the opening balance and into the range.
    assert before.amount("checking_start") == ev.amount("checking_start") - 500_00
    assert before.amount("rental_net") == ev.amount("rental_net") + 500_00
    assert before.amount("checking_end") == ev.amount("checking_end")


# --------------------------------------------------------------------------
# Selection semantics
# --------------------------------------------------------------------------

def test_partially_checked_parent_is_not_dropped(conn, report):
    """A parent stored WITHOUT the subtree flag is the picker's partially
    checked state: its own postings count, its unticked children do not, and it
    must never be filtered out for having children (SRD 5.9c)."""
    ledger.resolve_category(conn, "Rental:Insurance")
    insurance = ledger.resolve_category(conn, "Rental:Insurance")
    ledger.add_transaction(conn, report["checking"], "2025-09-01", -220_00,
                           category_id=insurance)

    item = custom.add_item(conn, report["id"], "rental_partial", "SOSC")
    custom.set_item_categories(conn, item,
                               [(report["rental"], 0), (report["repairs"], 0)])
    ev = custom.evaluate(conn, report["id"])
    # The parent's own rows plus Repairs, but NOT the unticked Insurance child.
    assert ev.amount("rental_partial") == 2000_00
    # ... while the subtree item next to it does pick Insurance up.
    assert ev.amount("rental_net") == 2000_00 - 220_00


def test_sign_flips_presentation_only(conn, report):
    item = custom.add_item(conn, report["id"], "repairs_expense", "SOSC", sign=-1)
    custom.set_item_categories(conn, item, [report["repairs"]])
    row = custom.evaluate(conn, report["id"]).by_name("repairs_expense")
    assert row.raw_amount == -300_00      # what was summed
    assert row.amount == 300_00           # what the report prints


def test_splits_land_on_their_own_legs(conn, report):
    """A split's legs are the lines, so the rental leg of a mixed payment
    counts and the rest of it does not."""
    txn = ledger.add_transaction(conn, report["checking"], "2025-04-01", -500_00,
                                 payee="ANON Hardware")
    ledger.set_splits(conn, txn, [
        {"category_id": report["repairs"], "amount": -200_00},
        {"category_id": report["salary"], "amount": -300_00},
    ])
    ev = custom.evaluate(conn, report["id"])
    assert ev.amount("rental_net") == 2000_00 - 200_00


def test_transfers_never_enter_an_sosc_item(conn, report):
    """Transfer legs carry no category, so moving money between the user's own
    accounts cannot show up as rental income."""
    ledger.create_transfer(conn, report["brokerage"], report["checking"],
                           "2025-08-08", 750_00)
    assert custom.evaluate(conn, report["id"]).amount("rental_net") == 2000_00


def test_scheduled_pre_entries_are_excluded(conn, report):
    ledger.add_transaction(conn, report["checking"], "2025-10-01", 999_00,
                           category_id=report["rental"], scheduled=1)
    assert custom.evaluate(conn, report["id"]).amount("rental_net") == 2000_00


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------

def test_coverage_flags_a_line_claimed_twice(conn, report):
    """Two items claiming one line is allowed -- a category can feed several tax
    lines -- but it is why the items do not sum to a total, so it is reported."""
    ev = custom.evaluate(conn, report["id"])
    assert ev.coverage.clean

    item = custom.add_item(conn, report["id"], "repairs_only", "SOSC")
    custom.set_item_categories(conn, item, [report["repairs"]])
    cov = custom.evaluate(conn, report["id"]).coverage
    assert not cov.clean
    assert cov.unclaimed == ()
    assert len(cov.multi_claimed) == 1
    claim = cov.multi_claimed[0]
    assert claim.amount == -300_00
    assert set(claim.item_names) == {"rental_net", "repairs_only"}


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------

def test_definition_crud(conn):
    rid = custom.create_report(conn, "Giving 2025", range_kind="calendar_year",
                               range_year=2025)
    assert custom.resolve_range(conn, rid) == ("2025-01-01", "2025-12-31")
    assert [r.name for r in custom.list_reports(conn)] == ["Giving 2025"]
    assert custom.find_report(conn, "Giving 2025").id == rid
    assert custom.find_report(conn, "nothing") is None

    custom.update_report(conn, rid, name="Giving", notes="synthetic")
    rd = custom.get_report(conn, rid)
    assert (rd.name, rd.notes, rd.kind) == ("Giving", "synthetic", "custom")

    with pytest.raises(ValueError):
        custom.create_report(conn, "Giving")            # name collision
    with pytest.raises(ValueError):
        custom.create_report(conn, "Bad", range_kind="whenever")
    with pytest.raises(ValueError):
        custom.create_report(conn, "Backwards", range_start=END, range_end=START)
    with pytest.raises(ValueError):
        custom.update_report(conn, rid, nonsense=1)

    custom.delete_report(conn, rid)
    assert custom.list_reports(conn) == []
    with pytest.raises(KeyError):
        custom.get_report(conn, rid)


def test_item_crud_and_cascade(conn, ledger_data):
    rid = custom.create_report(conn, "Custom", range_start=START, range_end=END)
    a = custom.add_item(conn, rid, "one", "SOSC", options={"note": "synthetic"})
    b = custom.add_item(conn, rid, "two", "EDAB", group_label="Balances")
    assert [i.seq for i in custom.list_items(conn, rid)] == [0, 1]
    assert custom.get_item(conn, a).options_dict() == {"note": "synthetic"}
    assert custom.get_item(conn, a).display_label == "one"

    custom.update_item(conn, a, label="One", sign=-1)
    assert custom.get_item(conn, a).display_label == "One"
    custom.reorder_items(conn, rid, [b, a])
    assert [i.name for i in custom.list_items(conn, rid)] == ["two", "one"]

    with pytest.raises(ValueError):
        custom.add_item(conn, rid, "one", "SOSC")       # duplicate name
    with pytest.raises(ValueError):
        custom.add_item(conn, rid, "three", "NOPE")
    with pytest.raises(ValueError):
        custom.add_item(conn, rid, "three", "SOSC", sign=0)

    custom.set_item_categories(conn, a, [ledger_data["rental"]])
    custom.set_item_accounts(conn, b, [ledger_data["checking"]])
    custom.delete_item(conn, a)
    assert conn.execute("SELECT COUNT(*) FROM report_item_categories").fetchone()[0] == 0

    custom.delete_report(conn, rid)
    assert conn.execute("SELECT COUNT(*) FROM report_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM report_item_accounts").fetchone()[0] == 0


def test_deleting_a_category_cascades_and_evaluation_survives(conn, report):
    assert custom.items_using_category(conn, report["rental"]) == \
        [("Schedule E 2025", "rental_net")]
    ledger.delete_category(conn, report["repairs"])
    assert custom.evaluate(conn, report["id"]).amount("rental_net") == 2000_00 + 300_00


def test_an_unknown_kind_is_refused_not_silently_zero(conn, report):
    """Every kind in ``ALL_KINDS`` evaluates as of Phase 5, so the only way to
    reach the fallback is a hand-written row -- and it must still raise rather
    than report a confident zero for a line nobody can compute."""
    item = custom.add_item(conn, report["id"], "holdings", "HOLDVAL")
    custom.evaluate(conn, report["id"])                 # implemented, no accounts
    conn.execute("UPDATE report_items SET kind = 'WAT' WHERE id = ?", (item,))
    conn.commit()
    with pytest.raises(ValueError, match="unknown kind"):
        custom.evaluate(conn, report["id"])


def test_explicit_range_overrides_the_stored_one(conn, report):
    ev = custom.evaluate(conn, report["id"], start="2025-01-02", end="2025-12-30")
    assert ev.amount("rental_net") == -300_00        # both edge rows excluded
    with pytest.raises(ValueError):
        custom.evaluate(conn, report["id"], start=END, end=START)
    with pytest.raises(ValueError):
        custom.evaluate(conn, report["id"], start="01/01/2025", end=END)


# --------------------------------------------------------------------------
# Phase 2: the tag overrides
# --------------------------------------------------------------------------

LINE20 = "TX-1040:line 20"       # a colon is legal in a tag name
LINE17 = "TX-SCHE:line 17"


@pytest.fixture
def tag_data(conn):
    """Two cash accounts and four categories, with the money left UNTAGGED --
    the life-cycle test applies the tags itself, one step at a time."""
    checking = ledger.create_account(conn, "Everyday", "checking",
                                     opening_balance=5000_00,
                                     opening_date="2024-12-01")
    savings = ledger.create_account(conn, "Rainy Day", "savings")

    utilities = ledger.resolve_category(conn, "Utilities")
    charity = ledger.resolve_category(conn, "Charity")
    groceries = ledger.resolve_category(conn, "Groceries")
    misc = ledger.resolve_category(conn, "Misc")

    util1 = ledger.add_transaction(conn, checking, "2025-02-02", -150_00,
                                   payee="ANON POWER CO", category_id=utilities)
    charity1 = ledger.add_transaction(conn, checking, "2025-03-03", -60_00,
                                      payee="ANON FUND", category_id=charity)
    util2 = ledger.add_transaction(conn, checking, "2025-04-04", -80_00,
                                   payee="ANON WATER", category_id=utilities)
    charity2 = ledger.add_transaction(conn, checking, "2025-05-05", -30_00,
                                      payee="ANON FUND", category_id=charity)
    # A three-leg split in categories NO item selects, so only a tag can reach it.
    split = ledger.add_transaction(conn, checking, "2025-06-06", -600_00,
                                   payee="ANON MARKET")
    ledger.set_splits(conn, split, [
        {"category_id": charity, "amount": -100_00},
        {"category_id": groceries, "amount": -200_00},
        {"category_id": misc, "amount": -300_00},
    ])

    return {"checking": checking, "savings": savings, "utilities": utilities,
            "charity": charity, "groceries": groceries, "misc": misc,
            "util1": util1, "util2": util2, "charity1": charity1,
            "charity2": charity2, "split": split}


@pytest.fixture
def tag_report(conn, tag_data):
    """One report, two tag-enabled SOSC items, BOTH selecting Utilities."""
    rid = custom.create_report(conn, "Taxes 2025", kind="tax",
                               range_kind="fixed", range_start=START,
                               range_end=END)
    item20 = custom.add_item(conn, rid, LINE20, "SOSC", tag_enabled=1)
    custom.set_item_categories(conn, item20, [tag_data["utilities"]])
    item17 = custom.add_item(conn, rid, LINE17, "SOSC", tag_enabled=1)
    custom.set_item_categories(conn, item17, [tag_data["utilities"]])
    return {"id": rid, "item20": item20, "item17": item17, **tag_data}


def test_tag_override_life_cycle(conn, tag_report):
    """The Phase 2 acceptance bar: the whole precedence order, in order."""
    rid = tag_report["id"]

    # -- two items may claim the same category, and Coverage says so ---------
    ev = custom.evaluate(conn, rid)
    assert ev.amount(LINE20) == -230_00                # both utility rows
    assert ev.amount(LINE17) == -230_00
    assert ev.by_name(LINE20).line_count == 2
    overlap = {c.txn_id: set(c.item_names) for c in ev.coverage.multi_claimed}
    assert overlap == {tag_report["util1"]: {LINE20, LINE17},
                       tag_report["util2"]: {LINE20, LINE17}}
    assert ev.coverage.unclaimed == ()

    # -- rule 2: an inclusion tag pulls in an UNSELECTED category ------------
    ledger.set_tags(conn, tag_report["charity1"], [LINE20])
    ev = custom.evaluate(conn, rid)
    assert ev.amount(LINE20) == -290_00                # -230.00 - 60.00
    assert ev.by_name(LINE20).line_count == 3
    assert ev.amount(LINE17) == -230_00                # item 1 only

    # -- rule 1: an exclusion tag beats the category selection ---------------
    ledger.set_tags(conn, tag_report["util2"], ["!" + LINE20])
    ev = custom.evaluate(conn, rid)
    assert ev.amount(LINE20) == -210_00                # -290.00 + 80.00
    assert ev.amount(LINE17) == -230_00                # untouched by item 1's tag
    # ... and the line is no longer an overlap, because only item 2 holds it.
    assert [c.txn_id for c in ev.coverage.multi_claimed] == [tag_report["util1"]]

    # -- rule 1 again: exclusion beats INCLUSION on the same line ------------
    ledger.set_tags(conn, tag_report["charity2"], [LINE20, "!" + LINE20])
    ev = custom.evaluate(conn, rid)
    assert ev.amount(LINE20) == -210_00                # no way back in

    # -- splits: a tag on the parent pulls in every leg, at its own amount ---
    ledger.set_tags(conn, tag_report["split"], [LINE20])
    ev = custom.evaluate(conn, rid)
    assert ev.amount(LINE20) == -810_00                # -210.00 - 600.00
    assert ev.by_name(LINE20).line_count == 5          # 2 unsplit + 3 legs

    # ... and !N on ONE leg carves out that leg only; siblings stay in.
    ledger.set_splits(conn, tag_report["split"], [
        {"category_id": tag_report["charity"], "amount": -100_00},
        {"category_id": tag_report["groceries"], "amount": -200_00,
         "tag": "!" + LINE20},
        {"category_id": tag_report["misc"], "amount": -300_00},
    ])
    ev = custom.evaluate(conn, rid)
    assert ev.amount(LINE20) == -610_00                # -810.00 + 200.00
    assert ev.by_name(LINE20).line_count == 4

    # -- transfers: the TAGGED LEG ONLY, at that leg's sign ------------------
    from_id, to_id = ledger.create_transfer(conn, tag_report["checking"],
                                            tag_report["savings"],
                                            "2025-07-07", 400_00)
    ledger.set_tags(conn, to_id, [LINE20])             # the +400.00 leg
    ev = custom.evaluate(conn, rid)
    assert ev.amount(LINE20) == -210_00                # -610.00 + 400.00, once
    assert ev.by_name(LINE20).line_count == 5
    assert ev.coverage.transfer_double_counted == ()

    # ... tag BOTH legs and the pair is reported, never silently de-duplicated.
    ledger.set_tags(conn, from_id, [LINE20])
    ev = custom.evaluate(conn, rid)
    assert ev.amount(LINE20) == -610_00                # the two legs cancel
    assert ev.by_name(LINE20).line_count == 6
    (warn,) = ev.coverage.transfer_double_counted
    assert warn.item_names == (LINE20,)
    assert warn.date == "2025-07-07"
    assert {warn.txn_id, warn.pair_txn_id} == {from_id, to_id}
    assert not ev.coverage.clean

    # -- a tag-enabled item name must be usable as a tag name ----------------
    with pytest.raises(ValueError, match="comma"):
        custom.add_item(conn, rid, "a,b", "SOSC", tag_enabled=1)


def test_a_tag_on_a_balance_item_is_ignored_and_flagged(conn, tag_report):
    """"Include this transaction in an end-of-year balance" is incoherent, so
    the tag is ignored -- but the user is TOLD, not left guessing."""
    rid = tag_report["id"]
    end_item = custom.add_item(conn, rid, "Ending cash", "EDAB", tag_enabled=1)
    custom.set_item_accounts(conn, end_item, [tag_report["checking"]])
    start_item = custom.add_item(conn, rid, "Opening cash", "SDAB", tag_enabled=1)
    custom.set_item_accounts(conn, start_item, [tag_report["checking"]])

    before = custom.evaluate(conn, rid)
    # A transaction tagged with a balance item's own name changes nothing ...
    ledger.set_tags(conn, tag_report["charity1"], ["Ending cash", "!Opening cash"])
    after = custom.evaluate(conn, rid)
    assert after.amount("Ending cash") == before.amount("Ending cash")
    assert after.amount("Opening cash") == before.amount("Opening cash")

    # ... and both items are surfaced as having ignored it.
    flagged = {(f.name, f.kind) for f in after.coverage.ignored_tags}
    assert flagged == {("Ending cash", "EDAB"), ("Opening cash", "SDAB")}
    assert not after.coverage.clean


def test_coverage_reports_money_excluded_by_every_item(conn, tag_report):
    """A line in a SELECTED category that every item excluded is the first thing
    to read on a tax report: the user aimed the report at it and it fell out
    again."""
    ledger.set_tags(conn, tag_report["util2"], ["!" + LINE20, "!" + LINE17])
    ev = custom.evaluate(conn, tag_report["id"])
    assert ev.amount(LINE20) == ev.amount(LINE17) == -150_00
    (gap,) = ev.coverage.unclaimed
    assert (gap.txn_id, gap.amount) == (tag_report["util2"], -80_00)
    assert gap.item_names == ()


def test_tag_enabled_item_names_are_validated(conn, tag_report):
    rid = tag_report["id"]
    with pytest.raises(ValueError, match="comma"):
        custom.add_item(conn, rid, "a,b", "SOSC", tag_enabled=1)
    with pytest.raises(ValueError, match="exclusion"):
        custom.add_item(conn, rid, "!" + LINE20, "SOSC", tag_enabled=1)

    # NOCASE uniqueness is LEDGER-wide, not per report: a second report's item
    # sharing the name would fight over the same tag row.
    other = custom.create_report(conn, "Taxes 2026", kind="tax",
                                 range_kind="calendar_year", range_year=2026)
    with pytest.raises(ValueError, match="collides"):
        custom.add_item(conn, other, LINE20.upper(), "SOSC", tag_enabled=1)
    # The same name is fine while the item is NOT tag-enabled -- nothing is
    # looked up in `tags`, so there is nothing to collide with ...
    plain = custom.add_item(conn, other, LINE20, "SOSC")
    # ... until it is promoted, which re-validates.
    with pytest.raises(ValueError, match="collides"):
        custom.update_item(conn, plain, tag_enabled=1)
    assert custom.get_item(conn, plain).tag_enabled == 0

    # A colon is legal, and re-saving an item against ITSELF is not a collision.
    custom.update_item(conn, tag_report["item20"], name=LINE20 + ":a")
    assert custom.get_item(conn, tag_report["item20"]).name == LINE20 + ":a"


# --------------------------------------------------------------------------
# Phase 5: COMPUTED expressions, the remaining kinds, and compare
# --------------------------------------------------------------------------

INCREASE = "TI:increase"
TITHE = "TI:tithe due"


@pytest.fixture
def tithing(conn):
    """The synthetic tithing report of the design: four measured lines, a
    COMPUTED sum of them and a COMPUTED tenth of that.

    The 2025 figures are chosen so the tenth lands exactly on a half cent --
    151586.5 -- which is the only interesting rounding case there is."""
    checking = ledger.create_account(conn, "Everyday", "checking",
                                     opening_balance=0,
                                     opening_date="2022-12-01")
    wages = ledger.resolve_category(conn, "Wages")
    dividends = ledger.resolve_category(conn, "Dividends")
    gifts = ledger.resolve_category(conn, "Gifts received")
    loss = ledger.resolve_category(conn, "Business loss")

    # 2023: dividends only -- so wages and gifts are absent from that column.
    ledger.add_transaction(conn, checking, "2023-05-05", 500_00,
                           category_id=dividends)
    # 2024: wages and dividends.
    ledger.add_transaction(conn, checking, "2024-05-05", 10000_00,
                           category_id=wages)
    ledger.add_transaction(conn, checking, "2024-06-06", 1000_00,
                           category_id=dividends)
    # 2025: all four, adding to 15158.65.
    ledger.add_transaction(conn, checking, "2025-04-04", 12480_00,
                           category_id=wages)
    ledger.add_transaction(conn, checking, "2025-07-07", 1864_50,
                           category_id=dividends)
    ledger.add_transaction(conn, checking, "2025-09-09", 942_15,
                           category_id=gifts)
    ledger.add_transaction(conn, checking, "2025-11-11", -128_00,
                           category_id=loss)

    rid = custom.create_report(conn, "Tithing", range_kind="calendar_year",
                               range_year=2025)
    ids = {}
    for name, category in (("TI:wages", wages), ("TI:dividends", dividends),
                           ("TI:gifts", gifts), ("TI:loss", loss)):
        ids[name] = custom.add_item(conn, rid, name, "SOSC")
        custom.set_item_categories(conn, ids[name], [category])
    ids[INCREASE] = custom.add_item(
        conn, rid, INCREASE, "COMPUTED", label="Total increase",
        expr="{TI:wages} + {TI:dividends} + {TI:gifts} + {TI:loss}")
    ids[TITHE] = custom.add_item(conn, rid, TITHE, "COMPUTED",
                                 expr="{" + INCREASE + "} / 10")
    return {"id": rid, "checking": checking, "wages": wages,
            "dividends": dividends, "gifts": gifts, "loss": loss, "ids": ids}


def test_computed_life_cycle(conn, tithing):
    """The Phase 5 acceptance bar for expressions: the whole report end to end,
    to the cent, with the dependency edges as the durable record of what
    references what."""
    rid = tithing["id"]
    ids = tithing["ids"]

    ev = custom.evaluate(conn, rid)
    assert ev.amount("TI:wages") == 12480_00
    assert ev.amount("TI:dividends") == 1864_50
    assert ev.amount("TI:gifts") == 942_15
    assert ev.amount("TI:loss") == -128_00

    # -- the sum is the sum of its referents, to the cent --------------------
    parts = sum(ev.amount(n) for n in
                ("TI:wages", "TI:dividends", "TI:gifts", "TI:loss"))
    assert parts == 15158_65
    assert ev.amount(INCREASE) == parts
    assert ev.by_name(INCREASE).line_count == 4        # four referents
    assert ev.by_name(INCREASE).label == "Total increase"

    # -- and the tenth of it rounds HALF UP, not to even ---------------------
    assert ev.amount(TITHE) == 1515_87                 # 1515.865 -> 1515.87

    # -- names were resolved at SAVE time, into real edges -------------------
    assert custom.item_refs(conn, ids[INCREASE]) == sorted(
        ids[n] for n in ("TI:wages", "TI:dividends", "TI:gifts", "TI:loss"))
    assert custom.item_refs(conn, ids[TITHE]) == [ids[INCREASE]]
    assert custom.item_refs(conn, ids["TI:wages"]) == []
    graph = custom.report_refs(conn, rid)
    assert set(graph) == set(ids.values())             # every item is present

    # -- a RENAME re-points the expressions that used the old name -----------
    custom.update_item(conn, ids["TI:wages"], name="TI:salary")
    custom.update_item(conn, ids[INCREASE],
                       expr="{TI:salary} + {TI:dividends} + {TI:gifts} + {TI:loss}")
    assert custom.item_refs(conn, ids[INCREASE]) == sorted(
        ids[n] for n in ("TI:wages", "TI:dividends", "TI:gifts", "TI:loss"))
    assert custom.evaluate(conn, rid).amount(INCREASE) == 15158_65


def test_an_expression_naming_a_missing_line_is_refused(conn, tithing):
    item = custom.add_item(conn, tithing["id"], "TI:bad", "COMPUTED",
                           expr="{TI:nothing} + 1")
    assert custom.item_refs(conn, item) == []          # nothing to point at
    with pytest.raises(ValueError, match="no line named"):
        custom.evaluate(conn, tithing["id"])


def test_a_malformed_expression_is_refused_at_save(conn, tithing):
    rid = tithing["id"]
    for bad in ("1 +", "(1 + 2", "1 2", '__import__("os")',
                "{}", "{a} & {b}", "1 * / 2", "()"):
        with pytest.raises(ValueError):
            custom.add_item(conn, rid, f"TI:x{bad!r}", "COMPUTED", expr=bad)

    # An EMPTY expression is the one exception, and only at save: the editor
    # adds the row before the formula is typed, so a half-built report must
    # still be saveable. Evaluation is where it comes due.
    blank = custom.add_item(conn, rid, "TI:blank", "COMPUTED", expr="   ")
    assert custom.item_refs(conn, blank) == []
    with pytest.raises(ValueError, match="expression"):
        custom.evaluate(conn, rid)
    custom.delete_item(conn, blank)

    # A leading sign is a UNARY operator, not a typo: "-{loss}" is how a user
    # negates ONE term of a sum without flipping the whole line's sign.
    unary = custom.add_item(conn, rid, "TI:unary", "COMPUTED",
                            expr="-{TI:loss} + 0")
    assert custom.evaluate(conn, rid).amount("TI:unary") == 128_00
    # A division that is only zero for some data is NOT a save-time error.
    ok = custom.add_item(conn, rid, "TI:ratio", "COMPUTED",
                         expr="{TI:wages} / ({TI:gifts} - {TI:loss})")
    assert custom.get_item(conn, ok).expr.startswith("{TI:wages}")


def test_a_cycle_is_refused_at_save_and_names_the_loop(conn, tithing):
    rid = tithing["id"]
    a = custom.add_item(conn, rid, "TI:A", "COMPUTED", expr="{TI:wages} + 0")
    custom.add_item(conn, rid, "TI:B", "COMPUTED", expr="{TI:A} + 0")

    with pytest.raises(ValueError, match="cycle") as err:
        custom.update_item(conn, a, expr="{TI:B} + 0")
    assert "TI:A" in str(err.value) and "TI:B" in str(err.value)

    # The refused edit did not land, and the report still evaluates.
    assert custom.get_item(conn, a).expr == "{TI:wages} + 0"
    assert custom.evaluate(conn, rid).amount("TI:B") == 12480_00

    # Self-reference is the degenerate case and is caught the same way.
    with pytest.raises(ValueError, match="cycle"):
        custom.add_item(conn, rid, "TI:self", "COMPUTED", expr="{TI:self} * 2")


def test_evaluate_rechecks_cycles_in_the_stored_edges(conn, tithing):
    """The save-time check is not enough: one hand-written UPDATE -- or a
    definition file loaded by an older build -- can leave a loop in the table,
    and the evaluator must refuse it rather than recurse until the stack goes."""
    ids = tithing["ids"]
    conn.execute(
        "INSERT OR IGNORE INTO report_item_refs (item_id, ref_item_id) "
        "VALUES (?,?)", (ids["TI:wages"], ids[INCREASE]))
    conn.commit()
    with pytest.raises(ValueError, match="cycle"):
        custom.evaluate(conn, tithing["id"])


def test_computed_reads_the_presented_amount_not_the_raw_total(conn, tithing):
    """``sign`` is applied before an expression sees a referent, so a loss line
    shown as a positive expense subtracts as one."""
    rid = tithing["id"]
    custom.update_item(conn, tithing["ids"]["TI:loss"], sign=-1)
    ev = custom.evaluate(conn, rid)
    assert ev.by_name("TI:loss").raw_amount == -128_00
    assert ev.amount("TI:loss") == 128_00
    assert ev.amount(INCREASE) == 15158_65 + 2 * 128_00


def test_deleting_a_referent_leaves_a_refusal_not_a_wrong_number(conn, tithing):
    custom.delete_item(conn, tithing["ids"]["TI:gifts"])
    assert custom.item_refs(conn, tithing["ids"][INCREASE]) == sorted(
        tithing["ids"][n] for n in ("TI:wages", "TI:dividends", "TI:loss"))
    with pytest.raises(ValueError, match="no line named"):
        custom.evaluate(conn, tithing["id"])


# --------------------------------------------------------------------------
# Phase 5: compare
# --------------------------------------------------------------------------

YEARS = [("2023-01-01", "2023-12-31"),
         ("2024-01-01", "2024-12-31"),
         ("2025-01-01", "2025-12-31")]


def test_compare_aligns_three_years(conn, tithing):
    """One definition re-pointed at three calendar years: three columns, rows
    aligned by item, and a category with nothing in the earliest year reading an
    explicit zero that is FLAGGED as "no data" rather than passed off as a
    fact."""
    rid = tithing["id"]
    res = custom.compare(conn, rid, YEARS)

    assert [c.label for c in res.columns] == ["2023", "2024", "2025"]
    assert [(c.start, c.end) for c in res.columns] == YEARS
    items = custom.list_items(conn, rid)
    assert [r.name for r in res.rows] == [i.name for i in items]   # seq order
    assert all(len(r.cells) == 3 for r in res.rows)

    assert res.by_name("TI:dividends").amounts == (500_00, 1000_00, 1864_50)
    assert res.by_name("TI:wages").amounts == (0, 10000_00, 12480_00)

    gifts = res.by_name("TI:gifts")
    assert gifts.amounts == (0, 0, 942_15)
    assert gifts.cells[0].no_data and gifts.cells[1].no_data
    assert not gifts.cells[2].no_data

    # The COMPUTED lines are re-evaluated per column, not carried across.
    assert res.by_name(INCREASE).amounts == (500_00, 11000_00, 15158_65)
    assert res.by_name(TITHE).amounts == (50_00, 1100_00, 1515_87)

    # An explicit label wins over the derived one, and a non-year range keeps
    # its dates.
    labelled = custom.compare(conn, rid, [
        ("2025-01-01", "2025-06-30", "H1"), ("2025-07-01", "2025-12-31")])
    assert [c.label for c in labelled.columns] == ["H1", "2025-07-01 to 2025-12-31"]

    with pytest.raises(ValueError):
        custom.compare(conn, rid, [])


def test_compare_coverage_is_per_column(conn, tithing):
    """A line claimed twice in 2025 and absent in 2023 must show up on the 2025
    column only -- merging the coverages would lose the year, which is the one
    thing a comparison is for."""
    rid = tithing["id"]
    audit = custom.add_item(conn, rid, "TI:wages audit", "SOSC")
    custom.set_item_categories(conn, audit, [tithing["wages"]])

    res = custom.compare(conn, rid, YEARS)
    y2023, y2024, y2025 = res.columns
    assert y2023.coverage.multi_claimed == ()          # no wages that year
    assert len(y2024.coverage.multi_claimed) == 1
    assert len(y2025.coverage.multi_claimed) == 1
    assert y2025.coverage.multi_claimed[0].amount == 12480_00
    assert set(y2025.coverage.multi_claimed[0].item_names) == \
        {"TI:wages", "TI:wages audit"}
    assert y2023.coverage.clean and not y2025.coverage.clean


# --------------------------------------------------------------------------
# Phase 5: HOLDVAL, RGAIN and NETGAIN
# --------------------------------------------------------------------------

@pytest.fixture
def invest_data(conn):
    """A funded brokerage holding two synthetic symbols, one of which has NO
    price -- which is what separates "worth nothing" from "not known"."""
    from mammon import investments, portfolio

    cash = ledger.create_account(conn, "Cash", "checking",
                                 opening_balance=50000_00,
                                 opening_date="2024-12-01")
    broker = ledger.create_account(conn, "Brokerage", "investment",
                                   opening_balance=0)
    portfolio.set_security(conn, "ZZAA", name="ANON Industries", sec_type="stock")
    portfolio.set_security(conn, "ZZBB", name="ANON Holdings", sec_type="stock")

    ledger.create_transfer(conn, cash, broker, "2025-01-02", 20000_00,
                           payee="Fund brokerage")
    investments.record_investment(conn, broker, "2025-01-03", "Buy",
                                  symbol="ZZAA", quantity="100",
                                  price="100.00", amount=-10000_00)
    investments.record_investment(conn, broker, "2025-01-04", "Buy",
                                  symbol="ZZBB", quantity="50",
                                  price="20.00", amount=-1000_00)
    investments.record_investment(conn, broker, "2025-06-10", "Sell",
                                  symbol="ZZAA", quantity="40",
                                  price="130.00", amount=5200_00)
    investments.rebuild_holdings(conn, broker)
    investments.record_price(conn, "ZZAA", "2025-12-31", "150.00")
    # ZZBB is deliberately left unpriced.

    rid = custom.create_report(conn, "Portfolio", range_kind="fixed",
                               range_start=START, range_end=END)
    return {"id": rid, "cash": cash, "broker": broker}


def test_holdval_values_what_it_can_and_says_what_it_could_not(conn, invest_data):
    rid = invest_data["id"]
    item = custom.add_item(conn, rid, "holdings", "HOLDVAL")
    custom.set_item_accounts(conn, item, [invest_data["broker"]])

    row = custom.evaluate(conn, rid).by_name("holdings")
    assert row.amount == 9000_00           # 60 ZZAA at 150.00; ZZBB unknown
    assert row.line_count == 2             # both positions were counted
    assert row.unpriced == ("ZZBB",)
    assert row.incomplete
    assert not row.no_data

    # An explicit selection narrows it, and dropping the unpriced symbol makes
    # the row complete again.
    custom.set_item_securities(conn, item, ["ZZAA"])
    row = custom.evaluate(conn, rid).by_name("holdings")
    assert (row.amount, row.line_count, row.unpriced) == (9000_00, 1, ())
    assert not row.incomplete


def test_rgain_splits_by_term(conn, invest_data):
    rid = invest_data["id"]
    short = custom.add_item(conn, rid, "short gains", "RGAIN",
                            options={"term": "short"})
    custom.set_item_accounts(conn, short, [invest_data["broker"]])
    long = custom.add_item(conn, rid, "long gains", "RGAIN",
                           options={"term": "long"})
    custom.set_item_accounts(conn, long, [invest_data["broker"]])
    every = custom.add_item(conn, rid, "all gains", "RGAIN")
    custom.set_item_accounts(conn, every, [invest_data["broker"]])

    ev = custom.evaluate(conn, rid)
    # 40 shares bought at 100.00 in January, sold at 130.00 in June.
    assert ev.amount("short gains") == 1200_00
    assert ev.amount("long gains") == 0
    assert ev.amount("all gains") == 1200_00

    # A range that does not contain the sale realizes nothing.
    ev = custom.evaluate(conn, rid, start="2025-07-01", end=END)
    assert ev.amount("all gains") == 0

    bad = custom.add_item(conn, rid, "nonsense gains", "RGAIN",
                          options={"term": "medium"})
    custom.set_item_accounts(conn, bad, [invest_data["broker"]])
    with pytest.raises(ValueError, match="term"):
        custom.evaluate(conn, rid)


def test_netgain_removes_deposits(conn, invest_data):
    """Money the user put IN is not a gain. That is the whole content of the
    kind, so the test pins the subtraction and not just the end balance."""
    rid = invest_data["id"]
    for name, kind in (("ending", "EDAB"), ("opening", "SDAB"),
                       ("gain", "NETGAIN")):
        item = custom.add_item(conn, rid, name, kind)
        custom.set_item_accounts(conn, item, [invest_data["broker"]])

    ev = custom.evaluate(conn, rid)
    assert ev.amount("opening") == 0                   # the account is new
    assert ev.amount("gain") == ev.amount("ending") - ev.amount("opening") \
        - 20000_00                                     # the funding transfer
    assert ev.amount("gain") == 3200_00
    # The unpriced holding taints the gain too: it is a floor, not the answer.
    assert ev.by_name("gain").unpriced == ("ZZBB",)
    assert ev.by_name("gain").incomplete


def test_incompleteness_is_contagious_through_an_expression(conn, invest_data):
    rid = invest_data["id"]
    item = custom.add_item(conn, rid, "holdings", "HOLDVAL")
    custom.set_item_accounts(conn, item, [invest_data["broker"]])
    custom.add_item(conn, rid, "half", "COMPUTED", expr="{holdings} / 2")

    row = custom.evaluate(conn, rid).by_name("half")
    assert row.amount == 4500_00
    assert row.unpriced == ("ZZBB",)
    assert row.incomplete


# --------------------------------------------------------------------------
# Phase 4: breaking one item down by TAG (Schedule E, one copy per property)
# --------------------------------------------------------------------------

MAPLE = "Maple Street"
OAK = "Oak Avenue"


@pytest.fixture
def properties(conn):
    """Two rental properties tagged by name, plus money carrying no tag at all.

    This is the shape the breakdown exists for: one set of categories -- property
    tax, maintenance, insurance -- whose money has to land on a SEPARATE copy of
    the same tax line per property, with whatever was never tagged still counted
    somewhere."""
    checking = ledger.create_account(conn, "Rent Checking", "checking",
                                     opening_balance=0,
                                     opening_date="2024-12-01")
    tax = ledger.resolve_category(conn, "Rental:Property tax")
    upkeep = ledger.resolve_category(conn, "Rental:Maintenance")
    insurance = ledger.resolve_category(conn, "Rental:Insurance")
    rows = [
        ("2025-02-01", -1200_00, tax, [MAPLE]),
        ("2025-02-02", -800_00, tax, [OAK]),
        ("2025-02-03", -150_00, tax, []),
        ("2025-05-01", -300_00, upkeep, [MAPLE]),
        ("2025-05-02", -250_00, upkeep, [OAK]),
        ("2025-05-03", -75_00, upkeep, []),
        ("2025-07-01", -400_00, insurance, [MAPLE]),
    ]
    for date, amount, category, tags in rows:
        txn = ledger.add_transaction(conn, checking, date, amount,
                                     category_id=category)
        if tags:
            ledger.set_tags(conn, txn, tags)
    rid = custom.create_report(conn, "Schedule E", kind="tax",
                               range_kind="fixed", range_start=START,
                               range_end=END)
    return {"id": rid, "checking": checking, "tax": tax, "upkeep": upkeep,
            "insurance": insurance}


def _sosc(conn, report_id, name, category, **extra):
    item = custom.add_item(conn, report_id, name, "SOSC", **extra)
    custom.set_item_categories(conn, item, [category])
    return item


def test_break_by_tag_splits_an_item_without_moving_its_total(conn, properties):
    """The acceptance bar: the same categories reported once per property AND
    once in total, with the total still the number the item reported before the
    breakdown existed."""
    rid = properties["id"]
    # "Plain" now means OPTED OUT: the breakdown is the default, so the way to
    # get one bare total is to say so.
    _sosc(conn, rid, "tax_plain", properties["tax"],
          options={"no_tag_breakdown": True})
    _sosc(conn, rid, "tax_by_property", properties["tax"],
          options={"break_by_tag": True})

    ev = custom.evaluate(conn, rid)
    subs = ev.sub_rows("tax_by_property")
    assert [r.label for r in subs] == [MAPLE, OAK, custom.UNTAGGED_LABEL]
    assert [r.amount for r in subs] == [-1200_00, -800_00, -150_00]
    assert [r.tag_value for r in subs] == [MAPLE, OAK, None]
    assert all(r.is_breakdown for r in subs)
    assert subs[-1].is_untagged and not subs[0].is_untagged
    # Every cent of the item lands in exactly one sub-row.
    assert sum(r.amount for r in subs) == -2150_00

    # The total is untouched: identical to the same item WITHOUT the breakdown.
    assert ev.amount("tax_by_property") == ev.amount("tax_plain") == -2150_00
    assert ev.by_name("tax_by_property").is_breakdown is False
    assert ev.sub_rows("tax_plain") == ()

    # Sub-rows come BEFORE the total, which is what keeps compare()'s
    # last-row-wins (and every other pre-breakdown reader) on the total.
    order = [(r.name, r.is_breakdown) for r in ev.rows]
    assert order.index(("tax_by_property", False)) > \
        order.index(("tax_by_property", True))
    assert order.count(("tax_by_property", True)) == 3


def test_a_line_carrying_two_property_tags_counts_under_each(conn, properties):
    """A bill covering both properties is counted under EACH tag; the total
    stays un-duplicated, so the sub-rows deliberately over-sum it."""
    txn = ledger.add_transaction(conn, properties["checking"], "2025-08-01",
                                 -100_00, category_id=properties["upkeep"])
    ledger.set_tags(conn, txn, [MAPLE, OAK])
    rid = properties["id"]
    _sosc(conn, rid, "upkeep", properties["upkeep"],
          options={"break_by_tag": True})

    ev = custom.evaluate(conn, rid)
    subs = {r.label: r.amount for r in ev.sub_rows("upkeep")}
    assert subs == {MAPLE: -400_00, OAK: -350_00, custom.UNTAGGED_LABEL: -75_00}
    assert ev.amount("upkeep") == -725_00          # the shared bill counts ONCE
    assert sum(subs.values()) == -825_00           # ... but twice across tags


def test_an_explicit_tag_list_restricts_and_orders_the_copies(conn, properties):
    """A listed property keeps its copy number even in a year it had no expense,
    and anything unlisted falls into the remainder rather than vanishing."""
    rid = properties["id"]
    _sosc(conn, rid, "prop_tax", properties["tax"],
          options={"break_by_tag": [OAK, MAPLE, "Birch Lane"]})
    _sosc(conn, rid, "maple_only", properties["tax"],
          options={"break_by_tag": [MAPLE]})

    ev = custom.evaluate(conn, rid)
    listed = ev.sub_rows("prop_tax")
    assert [r.label for r in listed] == [OAK, MAPLE, "Birch Lane",
                                         custom.UNTAGGED_LABEL]
    assert [r.txf_copy for r in listed] == [1, 2, 3, 4]
    assert [r.amount for r in listed] == [-800_00, -1200_00, 0, -150_00]
    assert listed[2].no_data                       # a property with no bills
    assert ev.amount("prop_tax") == -2150_00

    restricted = ev.sub_rows("maple_only")
    assert [(r.label, r.amount) for r in restricted] == [
        (MAPLE, -1200_00), (custom.UNTAGGED_LABEL, -800_00 - 150_00)]
    assert sum(r.amount for r in restricted) == ev.amount("maple_only")


def test_a_computed_line_sums_its_referents_per_tag(conn, properties):
    """The case this was built for: several expense lines added up into one
    Schedule E figure, per property as well as in total."""
    rid = properties["id"]
    _sosc(conn, rid, "prop_tax", properties["tax"],
          options={"break_by_tag": True})
    _sosc(conn, rid, "upkeep", properties["upkeep"],
          options={"break_by_tag": True})
    custom.add_item(conn, rid, "expenses", "COMPUTED",
                    expr="{prop_tax} + {upkeep}")

    ev = custom.evaluate(conn, rid)
    subs = {r.label: r.amount for r in ev.sub_rows("expenses")}
    assert subs == {MAPLE: -1500_00, OAK: -1050_00,
                    custom.UNTAGGED_LABEL: -225_00}
    # The total is what the expression always said, and the parts add to it.
    assert ev.amount("expenses") == ev.amount("prop_tax") + ev.amount("upkeep")
    assert ev.amount("expenses") == -2775_00
    assert sum(subs.values()) == ev.amount("expenses")


def test_a_plain_referent_reaches_a_computed_total_only(conn, properties):
    """An item nobody broke down has no per-property number to offer, so it
    reaches the total and no tag row -- visible as sub-rows that no longer add
    up to the total."""
    rid = properties["id"]
    _sosc(conn, rid, "prop_tax", properties["tax"],
          options={"break_by_tag": True})
    _sosc(conn, rid, "insurance", properties["insurance"],
          options={"no_tag_breakdown": True})               # not broken down
    custom.add_item(conn, rid, "expenses", "COMPUTED",
                    expr="{prop_tax} + {insurance}")

    ev = custom.evaluate(conn, rid)
    assert ev.sub_rows("insurance") == ()
    subs = {r.label: r.amount for r in ev.sub_rows("expenses")}
    assert subs == {MAPLE: -1200_00, OAK: -800_00,
                    custom.UNTAGGED_LABEL: -150_00}
    assert ev.amount("expenses") == -2150_00 - 400_00
    assert sum(subs.values()) == -2150_00             # the insurance is missing

    # A COMPUTED reference by NAME still resolves to the referent's total.
    custom.add_item(conn, rid, "double_tax", "COMPUTED", expr="{prop_tax} * 2")
    assert custom.evaluate(conn, rid).amount("double_tax") == -4300_00


def test_a_definition_file_round_trips_the_breakdown(conn, tmp_path):
    """The setting survives the file it was written into."""
    path = tmp_path / "example-sched-e-2025.json"
    path.write_text(json.dumps({
        "id": "example-sched-e-2025",
        "title": "Example Schedule E 2025",
        "kind": "tax",
        "year": 2025,
        "items": [
            {"name": "SE:property tax", "kind": "SOSC",
             "break_by_tag": [MAPLE, OAK],
             "export": {"txf_refnum": 9100, "txf_copy": 1}},
            {"name": "SE:maintenance", "kind": "SOSC", "break_by_tag": True,
             "options": {"term": "all"}},
            {"name": "SE:insurance", "kind": "SOSC"},
            {"name": "SE:one property", "kind": "SOSC", "break_by_tag": MAPLE},
            {"name": "SE:off", "kind": "SOSC", "break_by_tag": False},
        ],
    }), encoding="utf-8")

    defn = report_defs.load_definition(path)
    assert [i.break_by_tag for i in defn.items] == [
        (MAPLE, OAK), (), None, (MAPLE,), None]

    rid = report_defs.create_from_definition(conn, defn, name="Sched E 2025")
    items = {i.name: i for i in custom.list_items(conn, rid)}
    assert items["SE:property tax"].break_by_tag == (MAPLE, OAK)
    assert items["SE:maintenance"].break_by_tag == ()
    assert items["SE:one property"].break_by_tag == (MAPLE,)
    # Silence reads as the DEFAULT -- discover the tags -- and still invents no
    # options. An explicit false in the FILE lands the same way: the loader
    # stores nothing for it (there is nothing to store), and nothing now means
    # on. That is the price of making the breakdown the default, and it costs
    # the reader an extra row he can turn off, never a wrong total.
    assert items["SE:insurance"].break_by_tag == ()
    assert items["SE:off"].break_by_tag == ()
    assert items["SE:insurance"].options is None
    # An options blob the file already carried is kept, not replaced.
    assert items["SE:maintenance"].options_dict()["term"] == "all"


def test_a_definition_written_before_the_breakdown_loads_unchanged(conn, tmp_path):
    path = tmp_path / "example-old-2024.json"
    path.write_text(json.dumps({
        "id": "example-old-2024",
        "kind": "tax",
        "year": 2024,
        "items": [{"name": "TX:wages", "kind": "SOSC",
                   "export": {"txf_refnum": 9200}}],
    }), encoding="utf-8")

    defn = report_defs.load_definition(path)
    assert defn.items[0].break_by_tag is None
    rid = report_defs.create_from_definition(conn, defn, name="Old 2024")
    item = custom.list_items(conn, rid)[0]
    assert item.options is None
    # It gains the default -- discovery -- without gaining an options blob, and
    # discovery over lines that carry no tags is still no sub-rows at all, so
    # the old definition renders exactly as it always did.
    assert item.break_by_tag == ()
    assert custom.evaluate(conn, rid).sub_rows("TX:wages") == ()


def test_an_unreadable_break_by_tag_is_an_error_not_a_guess(tmp_path):
    path = tmp_path / "example-bad-2025.json"
    path.write_text(json.dumps({
        "id": "example-bad-2025", "kind": "tax", "year": 2025,
        "items": [{"name": "TX:wages", "kind": "SOSC", "break_by_tag": 3}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="break_by_tag"):
        report_defs.load_definition(path)


def _rent_lines(conn, properties):
    """Rent income tagged per property, plus one untagged receipt."""
    rent = ledger.resolve_category(conn, "Rental:Rents received")
    for date, amount, tags in (("2025-03-01", 2400_00, [MAPLE]),
                               ("2025-03-02", 1800_00, [OAK]),
                               ("2025-03-03", 500_00, [])):
        txn = ledger.add_transaction(conn, properties["checking"], date,
                                     amount, category_id=rent)
        if tags:
            ledger.set_tags(conn, txn, tags)
    return rent


def test_a_listed_breakdown_tag_survives_an_item_of_the_same_name(
        conn, properties):
    """Reported defect: a report that ALSO carries a tag-enabled item per
    property emptied every named sub-row into ``(untagged)``.

    The item names are report machinery, so the breakdown suppressed the tags
    of the same name -- including the ones the user had typed into the item's
    own tag list. An explicitly listed tag is the user naming a property; it is
    never suppressed."""
    rid = properties["id"]
    rent = _rent_lines(conn, properties)
    # The collision: one tag-enabled item NAMED after each property.
    custom.add_item(conn, rid, MAPLE, "SOSC", tag_enabled=1)
    custom.add_item(conn, rid, OAK, "SOSC", tag_enabled=1)
    _sosc(conn, rid, "rents", rent, options={"break_by_tag": [MAPLE, OAK]})

    subs = custom.evaluate(conn, rid).sub_rows("rents")
    assert [r.label for r in subs] == [MAPLE, OAK, custom.UNTAGGED_LABEL]
    # Each named property carries its own rent, and only the receipt that
    # really has no tag lands in the remainder.
    assert [r.amount for r in subs] == [2400_00, 1800_00, 500_00]
    assert [r.line_count for r in subs] == [1, 1, 1]


def test_a_tag_on_a_split_leg_gets_its_own_breakdown_sub_row(conn, properties):
    """A tag attached to ONE LEG, with nothing on the parent, still buckets:
    ``Line.tag`` is the merged parent+leg set, so the breakdown sees it."""
    rid = properties["id"]
    upkeep = properties["upkeep"]
    txn = ledger.add_transaction(conn, properties["checking"], "2025-06-01",
                                 -500_00, category_id=upkeep)
    ledger.set_splits(conn, txn, [
        {"category_id": upkeep, "amount": -200_00, "tag": MAPLE},
        {"category_id": upkeep, "amount": -300_00, "tag": OAK},
    ])
    _sosc(conn, rid, "upkeep_by_property", upkeep,
          options={"break_by_tag": True})

    ev = custom.evaluate(conn, rid)
    subs = ev.sub_rows("upkeep_by_property")
    assert [r.label for r in subs] == [MAPLE, OAK, custom.UNTAGGED_LABEL]
    assert [r.amount for r in subs] == [-500_00, -550_00, -75_00]
    assert ev.amount("upkeep_by_property") == -1125_00


def test_the_breakdown_never_moves_the_total_even_with_a_name_collision(
        conn, properties):
    """The invariant of record (SRD 5.9u): turning the breakdown on may not
    change a cent of the TOTAL row."""
    rid = properties["id"]
    rent = _rent_lines(conn, properties)
    custom.add_item(conn, rid, MAPLE, "SOSC", tag_enabled=1)
    _sosc(conn, rid, "rents_plain", rent)
    plain = custom.evaluate(conn, rid).amount("rents_plain")

    _sosc(conn, rid, "rents_listed", rent,
          options={"break_by_tag": [MAPLE, OAK]})
    _sosc(conn, rid, "rents_every", rent, options={"break_by_tag": True})
    ev = custom.evaluate(conn, rid)
    assert ev.amount("rents_listed") == ev.amount("rents_every") == plain
    assert plain == 4700_00
    # And the listed sub-rows still account for every cent of it.
    assert sum(r.amount for r in ev.sub_rows("rents_listed")) == plain


def test_the_breakdown_needs_no_tag_list_at_all(conn, properties):
    """The shape the user actually has, configured with NOTHING.

    A rent item straight out of the editor -- no options, no typed tags -- in a
    report that also carries a tag-enabled item named after each property. That
    collision is what used to empty every property row into ``(untagged)``; the
    tags are now discovered from the lines and only the item's OWN inclusion tag
    is ever held back."""
    rid = properties["id"]
    rent = _rent_lines(conn, properties)
    custom.add_item(conn, rid, MAPLE, "SOSC", tag_enabled=1)
    custom.add_item(conn, rid, OAK, "SOSC", tag_enabled=1)
    _sosc(conn, rid, "rents", rent)             # left exactly as created

    ev = custom.evaluate(conn, rid)
    subs = ev.sub_rows("rents")
    assert [r.label for r in subs] == [MAPLE, OAK, custom.UNTAGGED_LABEL]
    # Real money per property -- the defect showed 0.00 on both of these.
    assert [r.amount for r in subs] == [2400_00, 1800_00, 500_00]
    assert [r.line_count for r in subs] == [1, 1, 1]
    assert [r.txf_copy for r in subs] == [1, 2, 3]
    assert subs[-1].is_untagged and all(r.is_breakdown for r in subs)
    assert sum(r.amount for r in subs) == ev.amount("rents") == 4700_00

    # The tag-enabled items are not themselves broken down by the tag that
    # admitted their lines: that sub-row would only restate the total.
    assert ev.sub_rows(MAPLE) == ()
    assert ev.amount(MAPLE) == 2400_00 - 1200_00 - 300_00 - 400_00


def test_the_opt_out_leaves_one_bare_total(conn, properties):
    """The checkbox: same money, no sub-rows."""
    rid = properties["id"]
    rent = _rent_lines(conn, properties)
    _sosc(conn, rid, "rents_default", rent)
    _sosc(conn, rid, "rents_off", rent, options={"no_tag_breakdown": True})

    ev = custom.evaluate(conn, rid)
    assert len(ev.sub_rows("rents_default")) == 3
    assert ev.sub_rows("rents_off") == ()
    # The TOTAL is the invariant: the opt-out changes rows, never cents.
    assert ev.amount("rents_off") == ev.amount("rents_default") == 4700_00
    assert ev.by_name("rents_off").line_count == \
        ev.by_name("rents_default").line_count == 3


# --------------------------------------------------------------------------
# The drill-down (SRD 5.9u): item -> tag -> category -> transaction
# --------------------------------------------------------------------------
#
# The bar these tests hold is a single invariant: the tree EXPLAINS the report,
# it does not recompute it. Every figure in it must be one `evaluate` produced,
# because a drill-down that disagreed with the total it sits under would be
# worse than no drill-down at all -- the user would have two numbers and no way
# to tell which one the tax form gets.

def _nodes(node, kind):
    """Every descendant of ``node`` of one kind, depth first."""
    out = []
    for child in node.children:
        if child.kind == kind:
            out.append(child)
        out.extend(_nodes(child, kind))
    return out


def test_detail_does_not_move_a_single_number(conn, properties):
    """``detail=True`` is a flag on the ONE evaluation, not a second walk: it
    must be invisible to every row."""
    rid = properties["id"]
    _sosc(conn, rid, "tax", properties["tax"])
    plain = custom.evaluate(conn, rid)
    kept = custom.evaluate(conn, rid, detail=True)
    assert [(r.item_id, r.amount, r.raw_amount, r.line_count, r.tag_value)
            for r in plain.rows] == \
           [(r.item_id, r.amount, r.raw_amount, r.line_count, r.tag_value)
            for r in kept.rows]
    assert plain.details == {}
    assert kept.details[custom.list_items(conn, rid)[0].id].admitted


def test_the_tree_is_four_levels_and_reaches_the_transactions(conn, properties):
    rid = properties["id"]
    _sosc(conn, rid, "tax", properties["tax"])
    tree = custom.drill_down(conn, rid)
    item = tree.items[0]
    assert item.kind == "item" and item.item_name == "tax"
    assert [t.label for t in item.children] == [MAPLE, OAK,
                                                custom.UNTAGGED_LABEL]
    assert [c.kind for c in item.children[0].children] == ["category"]
    assert [t.kind for t in item.children[0].children[0].children] == ["txn"]
    line = item.children[0].children[0].children[0].line
    assert (line.date, line.amount) == ("2025-02-01", -1200_00)


def test_an_item_node_is_the_evaluators_row_verbatim(conn, properties):
    """Never a sum taken here: the item node carries the number that is filed."""
    rid = properties["id"]
    _sosc(conn, rid, "tax", properties["tax"])
    ev = custom.evaluate(conn, rid)
    tree = custom.drill_down(conn, rid)
    assert tree.items[0].amount == ev.amount("tax") == -2150_00


def test_the_tag_level_agrees_with_the_breakdown_sub_rows(conn, properties):
    """The tree and the TXF/CSV sub-rows must bucket identically -- they are two
    readings of the same breakdown, and a user comparing them would notice."""
    rid = properties["id"]
    _sosc(conn, rid, "tax", properties["tax"])
    ev = custom.evaluate(conn, rid)
    tree = custom.drill_down(conn, rid)
    assert [(t.label, t.amount) for t in tree.items[0].children] == \
           [(r.label, r.amount) for r in ev.sub_rows("tax")]


def test_a_sign_flip_reaches_every_level(conn, properties):
    """A deduction reads positive throughout, the way the report presents it --
    the leaf must not disagree with the line above it."""
    rid = properties["id"]
    _sosc(conn, rid, "tax", properties["tax"], sign=-1)
    tree = custom.drill_down(conn, rid)
    item = tree.items[0]
    assert item.amount == 2150_00
    assert all(t.amount >= 0 for t in item.children)
    assert all(n.line.amount > 0 for n in _nodes(item, "txn"))


def test_an_excluded_line_is_kept_for_display_and_counts_nothing(
        conn, properties):
    rid = properties["id"]
    item_id = _sosc(conn, rid, "tax", properties["tax"], tag_enabled=1)
    victim = conn.execute(
        "SELECT id FROM transactions WHERE amount = -120000").fetchone()[0]
    ledger.toggle_report_exclusion(conn, "tax", victim)

    ev = custom.evaluate(conn, rid, detail=True)
    assert ev.amount("tax") == -950_00                  # the 1200 is gone
    detail = ev.details[item_id]
    assert [ln.txn_id for ln in detail.excluded] == [victim]

    tree = custom.drill_down(conn, rid)
    leaves = _nodes(tree.items[0], "txn")
    gone = [n for n in leaves if n.line.txn_id == victim]
    assert len(gone) == 1                                # still on screen
    assert gone[0].line.excluded and gone[0].line.amount == -1200_00
    # ... and its bucket reports nothing.
    maple = [t for t in tree.items[0].children if t.label == MAPLE][0]
    assert maple.amount == 0


def test_a_line_that_was_never_the_items_business_is_not_shown_as_excluded(
        conn, properties):
    """An exclusion tag on a line in a category this item never selected is not
    a line the item LOST -- showing it struck through would invent a decision
    nobody made."""
    rid = properties["id"]
    item_id = _sosc(conn, rid, "tax", properties["tax"], tag_enabled=1)
    other = conn.execute(
        "SELECT id FROM transactions WHERE amount = -30000").fetchone()[0]
    ledger.toggle_report_exclusion(conn, "tax", other)   # a MAINTENANCE line
    ev = custom.evaluate(conn, rid, detail=True)
    assert [ln.txn_id for ln in ev.details[item_id].excluded] == []


def test_an_explicit_list_keeps_an_empty_bucket_discovery_would_drop(
        conn, properties):
    rid = properties["id"]
    _sosc(conn, rid, "tax", properties["tax"],
          options={"break_by_tag": [MAPLE, "Nowhere Lane"]})
    tree = custom.drill_down(conn, rid)
    tags = {t.label: t for t in tree.items[0].children}
    assert tags["Nowhere Lane"].amount == 0
    assert tags["Nowhere Lane"].children == []
    # OAK is not listed, so its money falls to the remainder, not to a bucket.
    assert OAK not in tags
    assert tags[custom.UNTAGGED_LABEL].amount == -800_00 - 150_00


def test_discovery_with_no_tags_has_no_tag_level_at_all(conn, ledger_data):
    """What keeps the default-on breakdown quiet: the categories come straight
    up under the item."""
    rid = custom.create_report(conn, "Plain", range_kind="fixed",
                               range_start=START, range_end=END)
    _sosc(conn, rid, "rental", ledger_data["rental"])
    tree = custom.drill_down(conn, rid)
    assert [c.kind for c in tree.items[0].children] == ["category"]


def test_a_kind_with_no_lines_is_a_childless_leaf(conn, report):
    """A balance has no transactions to open; it must not pretend otherwise."""
    tree = custom.drill_down(conn, report["id"])
    balances = [n for n in tree.items if n.item_kind in ("EDAB", "SDAB")]
    assert balances and all(n.children == [] for n in balances)


def test_a_transfer_leg_is_shown_under_its_counterparty_account(
        conn, ledger_data):
    rid = custom.create_report(conn, "Transfers", range_kind="fixed",
                               range_start=START, range_end=END)
    custom.add_item(conn, rid, "moved", "SOSC", tag_enabled=1)
    txn, _mirror = ledger.create_transfer(
        conn, ledger_data["checking"], ledger_data["brokerage"],
        "2025-04-01", 500_00)
    ledger.set_tags(conn, txn, ["moved"])
    tree = custom.drill_down(conn, rid)
    labels = [n.label for n in _nodes(tree.items[0], "category")]
    assert "[Brokerage]" in labels


def test_a_shared_category_marks_both_items_with_the_reason(conn, properties):
    rid = properties["id"]
    _sosc(conn, rid, "tax_a", properties["tax"])
    _sosc(conn, rid, "tax_b", properties["tax"])
    tree = custom.drill_down(conn, rid)
    marks = {n.item_name: n.marks for n in tree.items}
    assert [m.code for m in marks["tax_a"]] == ["shared_lines"]
    assert "tax_b" in marks["tax_a"][0].text
    assert "tax_a" in marks["tax_b"][0].text


def test_both_legs_of_a_tagged_transfer_are_flagged(conn, ledger_data):
    rid = custom.create_report(conn, "Doubled", range_kind="fixed",
                               range_start=START, range_end=END)
    custom.add_item(conn, rid, "moved", "SOSC", tag_enabled=1)
    txn, mirror = ledger.create_transfer(
        conn, ledger_data["checking"], ledger_data["brokerage"],
        "2025-04-01", 500_00)
    ledger.set_tags(conn, txn, ["moved"])
    ledger.set_tags(conn, mirror, ["moved"])
    tree = custom.drill_down(conn, rid)
    marks = {m.code: m for m in tree.items[0].marks}
    assert "transfer_both_legs" in marks
    assert "counted twice" in marks["transfer_both_legs"].text


def test_two_listed_tags_on_one_line_are_flagged_but_discovery_is_not(
        conn, properties):
    """With a LIST the tag rows are meant to partition, so two hits on one line
    is one payment booked to two properties. In discovery they are a re-cut and
    an incidental tag is normal -- a mark that fires on something normal teaches
    the user to ignore marks."""
    rid = properties["id"]
    both = conn.execute(
        "SELECT id FROM transactions WHERE amount = -15000").fetchone()[0]
    ledger.set_tags(conn, both, [MAPLE, OAK])
    _sosc(conn, rid, "listed", properties["tax"],
          options={"break_by_tag": [MAPLE, OAK]})
    _sosc(conn, rid, "found", properties["tax"])

    tree = custom.drill_down(conn, rid)
    by_name = {n.item_name: n for n in tree.items}
    assert "double_tagged" in [m.code for m in by_name["listed"].marks]
    assert "double_tagged" not in [m.code for m in by_name["found"].marks]
    # The money is still right: the item total never double counts.
    assert by_name["listed"].amount == -2150_00
    assert sum(t.amount for t in by_name["listed"].children) == \
        -2150_00 - 150_00                      # the double-tagged line, twice


def test_an_exclusion_works_on_an_item_that_is_not_tag_enabled(conn, properties):
    """Reported defect: the report's right-click wrote ``!<item name>`` and the
    evaluator ignored it, because the negation was computed only for
    ``tag_enabled`` items -- 67 of 68 lines on a real tax report. The tag landed
    in the ledger, the number did not move, and nothing said why.

    ``tag_enabled`` gates INCLUSION, which is a standing promise about a whole
    vocabulary. Pushing one line out is not that promise reversed."""
    rid = properties["id"]
    item_id = _sosc(conn, rid, "tax", properties["tax"])
    assert custom.get_item(conn, item_id).tag_enabled == 0
    before = custom.evaluate(conn, rid).amount("tax")

    victim = conn.execute(
        "SELECT id FROM transactions WHERE amount = -120000").fetchone()[0]
    ledger.toggle_report_exclusion(conn, "tax", victim)

    assert custom.evaluate(conn, rid).amount("tax") == before + 1200_00
    tree = custom.drill_down(conn, rid)
    gone = [n for n in _nodes(tree.items[0], "txn")
            if n.line.txn_id == victim]
    assert gone and gone[0].line.excluded


def test_an_inclusion_tag_still_needs_tag_enabled(conn, properties):
    """The other half must NOT have moved: a line tagged with an item's name
    joins that item only when the item opted in."""
    rid = properties["id"]
    _sosc(conn, rid, "tax", properties["tax"])
    stray = ledger.add_transaction(conn, properties["checking"], "2025-08-01",
                                   -99_00,
                                   category_id=properties["insurance"])
    ledger.set_tags(conn, stray, ["tax"])
    assert custom.evaluate(conn, rid).amount("tax") == -2150_00   # not -2249


# --------------------------------------------------------------------------
# Transfers as an SOSC selection (SRD 5.9r, rule 4)
# --------------------------------------------------------------------------
#
# The case: a tax line is often stated NET of money that merely moved. W-2 box 1
# wages are gross pay less the 401(k) deferral, and that deferral is a transfer
# leg of the paycheck carrying no category at all -- so an item summing
# categories could not reach it, and the figure the form asks for could not be
# expressed. The same arithmetic is what a tithing report wants.

@pytest.fixture
def paycheck(conn):
    """A real paycheck's shape: gross salary in, tax legs out, an employer match,
    and a 401(k) deferral transferring to the retirement account."""
    checking = ledger.create_account(conn, "Checking", "checking")
    retirement = ledger.create_account(conn, "Artemis 401K", "investment")
    salary = ledger.resolve_category(conn, "Salary")
    fed = ledger.resolve_category(conn, "Tax:Fed")
    match = ledger.resolve_category(conn, "401K Match")
    txn = ledger.add_transaction(conn, checking, "2025-02-01", 3_344_54,
                                 payee="Artemis Inc.")
    ledger.set_splits(conn, txn, [
        {"category_id": salary, "amount": 4_315_39},
        {"category_id": fed, "amount": -160_06},
        {"category_id": match, "amount": 151_03},
        {"transfer_account_id": retirement, "amount": -961_82},
    ])
    rid = custom.create_report(conn, "Tithing", range_kind="fixed",
                               range_start=START, range_end=END)
    return {"id": rid, "checking": checking, "retirement": retirement,
            "salary": salary, "fed": fed, "match": match, "txn": txn}


def test_a_transfer_leg_has_no_category_to_select(conn, paycheck):
    """Why rule 4 has to exist: the deferral is invisible to every category."""
    item = _sosc(conn, paycheck["id"], "wages", paycheck["salary"])
    assert custom.evaluate(conn, paycheck["id"]).amount("wages") == 4_315_39
    assert custom._selected_transfer_ids(conn, item) == frozenset()


def test_selecting_the_far_account_nets_the_transfer_off_the_line(conn,
                                                                  paycheck):
    """The acceptance bar: salary LESS the 401(k) deferral, in one item."""
    item = _sosc(conn, paycheck["id"], "wages", paycheck["salary"])
    custom.set_item_accounts(conn, item, [paycheck["retirement"]])
    ev = custom.evaluate(conn, paycheck["id"])
    assert ev.amount("wages") == 4_315_39 - 961_82
    # The leg enters at its OWN sign, which is what makes it subtract.
    assert ev.by_name("wages").line_count == 2


def test_the_mirror_leg_is_not_also_pulled_in(conn, paycheck):
    """Single-sided by construction: a transfer contributes two rows and only the
    one OUTSIDE the selected account points at it. Were both matched the deferral
    would cancel itself and the line would read as plain salary."""
    item = _sosc(conn, paycheck["id"], "wages", paycheck["salary"])
    custom.set_item_accounts(conn, item, [paycheck["retirement"]])
    tree = custom.drill_down(conn, paycheck["id"])
    legs = [n for n in _nodes(tree.items[0], "txn")
            if n.line.amount < 0]
    assert [n.line.amount for n in legs] == [-961_82]
    assert tree.items[0].amount == 4_315_39 - 961_82


def test_the_transfer_shows_under_its_bracketed_account(conn, paycheck):
    """It drills down like anything else, under the label the register uses."""
    item = _sosc(conn, paycheck["id"], "wages", paycheck["salary"])
    custom.set_item_accounts(conn, item, [paycheck["retirement"]])
    tree = custom.drill_down(conn, paycheck["id"])
    labels = [n.label for n in _nodes(tree.items[0], "category")]
    assert "[Artemis 401K]" in labels and "Salary" in labels


def test_selecting_both_accounts_of_one_transfer_is_flagged(conn, paycheck):
    """Both legs pulled in IS a double count -- the same mistake as tagging both
    sides, and reported the same way rather than silently halved."""
    rid = custom.create_report(conn, "Both sides", range_kind="fixed",
                               range_start=START, range_end=END)
    plain = ledger.create_account(conn, "Savings", "checking")
    a, b = ledger.create_transfer(conn, paycheck["checking"], plain,
                                  "2025-03-01", 500_00)
    item = custom.add_item(conn, rid, "moved", "SOSC")
    custom.set_item_accounts(conn, item, [paycheck["checking"], plain])
    tree = custom.drill_down(conn, rid)
    assert "transfer_both_legs" in [m.code for m in tree.items[0].marks]


def test_an_exclusion_still_beats_a_transfer_selection(conn, paycheck):
    """Rule 1 outranks rule 4 like every other selection, so one deferral can be
    carved out of the line without un-selecting the account."""
    item = _sosc(conn, paycheck["id"], "wages", paycheck["salary"])
    custom.set_item_accounts(conn, item, [paycheck["retirement"]])
    leg = conn.execute(
        "SELECT id FROM splits WHERE transfer_account_id = ?",
        (paycheck["retirement"],)).fetchone()[0]
    ledger.toggle_report_exclusion(conn, "wages", paycheck["txn"], leg)
    ev = custom.evaluate(conn, paycheck["id"], detail=True)
    assert ev.amount("wages") == 4_315_39
    assert [ln.split_id for ln in ev.details[item].excluded] == [leg]


def test_a_transfer_selection_does_not_disturb_the_balance_kinds(conn,
                                                                 paycheck):
    """``report_item_accounts`` means two things, one per kind, and an item has
    exactly one kind -- so the reuse cannot collide."""
    rid = paycheck["id"]
    sosc = _sosc(conn, rid, "wages", paycheck["salary"])
    custom.set_item_accounts(conn, sosc, [paycheck["retirement"]])
    bal = custom.add_item(conn, rid, "retirement_end", "EDAB")
    custom.set_item_accounts(conn, bal, [paycheck["retirement"]])
    ev = custom.evaluate(conn, rid)
    assert ev.amount("wages") == 4_315_39 - 961_82
    assert ev.amount("retirement_end") == 961_82        # the balance, not a sum


# --------------------------------------------------------------------------
# Crypto wallets in the valued kinds (SRD 5.9r)
# --------------------------------------------------------------------------

@pytest.fixture
def wallet(conn):
    """A crypto wallet holding one coin, priced on ONE day."""
    from mammon import crypto, investments
    acct = ledger.create_account(conn, "Coinbase", "crypto")
    crypto.record_buy(conn, acct, "2025-02-01", "ETH", "10", 20_000_00)
    crypto.rebuild_holdings(conn, acct)
    # Priced on ONE day, which is what makes the incomplete case reachable: a
    # coin's price series is often a handful of downloaded closes, not a history.
    investments.record_price(conn, crypto.pair_symbol("ETH"), "2025-06-01",
                             "3000")
    rid = custom.create_report(conn, "Crypto", range_kind="fixed",
                               range_start=START, range_end=END)
    return {"id": rid, "account": acct}


def test_holdval_values_a_crypto_wallet(conn, wallet):
    """Reported defect: HOLDVAL over a crypto account read 0.00. Its coins live
    in the crypto_* tables, so the securities helpers saw no positions at all --
    and zero positions cannot be unpriced, so nothing flagged it either."""
    item = custom.add_item(conn, wallet["id"], "coins", "HOLDVAL")
    custom.set_item_accounts(conn, item, [wallet["account"]])
    row = custom.evaluate(conn, wallet["id"]).by_name("coins")
    assert row.amount == 30_000_00
    assert row.line_count == 1                      # the position was counted
    assert not row.incomplete


def test_an_unpriced_coin_marks_the_row_incomplete(conn, wallet):
    """The valued kinds DO value a wallet through ``display_balance``, so a coin
    with no price on the as-of date drops out of a number that would otherwise
    read as a confident zero."""
    item = custom.add_item(conn, wallet["id"], "coins", "HOLDVAL")
    custom.set_item_accounts(conn, item, [wallet["account"]])
    ev = custom.evaluate(conn, wallet["id"], START, "2025-03-01")
    row = ev.by_name("coins")
    assert row.amount == 0
    assert row.unpriced == ("ETH",) and row.incomplete


def test_a_balance_over_a_wallet_says_when_it_could_not_price_it(conn, wallet):
    for kind in ("EDAB", "NETGAIN"):
        item = custom.add_item(conn, wallet["id"], f"bal_{kind}", kind)
        custom.set_item_accounts(conn, item, [wallet["account"]])
    ev = custom.evaluate(conn, wallet["id"], START, "2025-03-01")
    for kind in ("EDAB", "NETGAIN"):
        assert ev.by_name(f"bal_{kind}").unpriced == ("ETH",)


def test_a_securities_account_is_unchanged_by_the_crypto_branch(conn,
                                                                ledger_data):
    """The securities path is untouched: only a wallet takes the new branch."""
    item = custom.add_item(conn, 
                           custom.create_report(conn, "Sec", range_kind="fixed",
                                                range_start=START,
                                                range_end=END),
                           "held", "HOLDVAL")
    custom.set_item_accounts(conn, item, [ledger_data["brokerage"]])
    row = custom.evaluate(conn, custom.get_item(conn, item).report_id
                          ).by_name("held")
    assert row.amount == 0 and row.line_count == 0
