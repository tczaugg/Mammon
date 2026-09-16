"""A target covers accounts the user picks, of ONE kind of money, and shows what
moved inside each class (SRD 5.8f).

User-reported, 2026-09-15: "This mixes kinds of money. 401K + IRA shouldn't be
mixed with ROTH which shouldn't be mixed with non-tax special holdings."; "There
is no customization for accounts. I wouldn't want to include the [529] accounts
here as those are for my kids and not something I consider part of my assets.";
"wouldn't it also make sense to show which assets within the asset class have
changed the most"; "The difference between investments and cash and investments
is unclear, as both have cash, yet the advice changes."

Synthetic data only.
"""
from __future__ import annotations

import os
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import crypto, db, investments, ledger, portfolio, rebalance

AS_OF = "2026-06-30"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "targets.db")
    yield c
    c.close()


def _fund(conn, acct, symbol, qty, price, date="2024-01-02"):
    investments.record_investment(conn, acct, date, "Buy", symbol=symbol,
                                  quantity=qty, price=price,
                                  amount=-int(Decimal(qty) * Decimal(price) * 100))


@pytest.fixture
def world(conn):
    """A 401k, a Roth, and a 529 held for a child -- three kinds of money."""
    # Opened with the cash the purchases below spend, so each account's cash
    # sleeve ends at zero and the mix is its securities.
    plan = ledger.create_account(conn, "ANON 401k", "investment",
                                 opening_balance=15_000_00)
    roth = ledger.create_account(conn, "ANON Roth", "investment",
                                 opening_balance=5_000_00)
    kid = ledger.create_account(conn, "ANON 529", "investment", opening_balance=5_000_00)
    rebalance.set_account_treatment(conn, plan, "deferred")
    rebalance.set_account_treatment(conn, roth, "roth")
    rebalance.set_account_treatment(conn, kid, "special")
    _fund(conn, plan, "ANONSTK", "100", "100")
    _fund(conn, plan, "ANONBND", "100", "50")
    _fund(conn, roth, "ANONSTK", "50", "100")
    for sym, price in (("ANONSTK", "150"), ("ANONBND", "50")):
        investments.record_price(conn, sym, AS_OF, price)
    # What each was worth at the start of the span the change is measured over.
    investments.record_price(conn, "ANONSTK", "2024-12-31", "100")
    investments.record_price(conn, "ANONBND", "2024-12-31", "50")
    for acct in (plan, roth, kid):
        investments.rebuild_holdings(conn, acct)
    portfolio.set_security(conn, "ANONSTK", asset_class="domestic_stock")
    portfolio.set_security(conn, "ANONBND", asset_class="bond")
    return {"plan": plan, "roth": roth, "kid": kid}


def test_a_target_covers_one_kind_of_money(conn, world):
    tid = rebalance.create_target(conn, "Plan", lines={"domestic_stock": 60, "bond": 40},
                                  active=True)
    rebalance.set_target_accounts(conn, tid, [world["plan"]])
    assert rebalance.target_accounts(conn, tid) == [world["plan"]]
    with pytest.raises(ValueError) as exc:
        rebalance.set_target_accounts(conn, tid, [world["plan"], world["roth"]])
    assert "one kind of money" in str(exc.value)
    assert rebalance.target_accounts(conn, tid) == [world["plan"]]   # unchanged


def test_the_drift_measures_only_the_chosen_accounts(conn, world):
    tid = rebalance.create_target(conn, "Plan", lines={"domestic_stock": 60, "bond": 40},
                                  active=True)
    rebalance.set_target_accounts(conn, tid, [world["plan"]])
    r = rebalance.drift(conn, tid, as_of=AS_OF)
    assert r.sleeve_total == 150 * 100_00 + 100 * 50_00      # the 401k alone
    assert r.account_ids == [world["plan"]] and r.tax_treatment == "deferred"
    assert "ANON 529" not in r.sleeve_accounts and "ANON Roth" not in r.sleeve_accounts


def test_an_account_with_no_treatment_can_still_be_chosen(conn, world):
    plain = ledger.create_account(conn, "ANON Brokerage", "investment", opening_balance=0)
    tid = rebalance.create_target(conn, "Mixed-in", lines={"domestic_stock": 100})
    rebalance.set_target_accounts(conn, tid, [world["plan"], plain])
    assert rebalance.target_accounts(conn, tid) == sorted([world["plan"], plain])


def test_each_class_lists_its_holdings_and_what_they_did(conn, world):
    tid = rebalance.create_target(conn, "Plan", lines={"domestic_stock": 60, "bond": 40},
                                  active=True)
    rebalance.set_target_accounts(conn, tid, [world["plan"]])
    rebalance.set_rebalanced(conn, tid, "2025-01-01")
    r = rebalance.drift(conn, tid, as_of=AS_OF)
    assert r.since == "2025-01-01" and r.since_is_rebalance
    stock = next(row for row in r.rows if row.asset_class == "domestic_stock")
    [held] = stock.holdings
    assert (held.symbol, held.account, held.value_cents) == (
        "ANONSTK", "ANON 401k", 150 * 100_00)
    assert held.pct_of_class == Decimal("100")
    assert held.change_cents == 50 * 100_00          # 100 -> 150 a share since the date


def test_unclassified_is_not_traded_and_not_out_of_band(conn, world):
    ledger.set_opening_balance(conn, world["plan"], 16_000_00)
    _fund(conn, world["plan"], "ANONNEW", "10", "100")
    investments.record_price(conn, "ANONNEW", AS_OF, "100")
    investments.rebuild_holdings(conn, world["plan"])
    tid = rebalance.create_target(conn, "Plan", lines={"domestic_stock": 60, "bond": 40},
                                  active=True)
    rebalance.set_target_accounts(conn, tid, [world["plan"]])
    r = rebalance.drift(conn, tid, as_of=AS_OF)
    row = r.unclassified_row
    assert row is not None and row.current_cents == 1_000_00
    assert row.action == "classify" and not row.out_of_band
    assert [h.symbol for h in row.holdings] == ["ANONNEW"]
    # ...and it is not counted as money a rebalance would move.
    assert r.to_move_cents == sum(x.move_cents for x in r.rows
                                  if x.move_cents > 0 and not x.is_unclassified)


def test_a_wallets_coins_are_valued_as_crypto_not_as_a_brokerage(conn, world):
    """The allocation used to value a crypto account with the brokerage rule,
    which saw only its bank transfer legs: a wallet whose own balance is zero
    came out as tens of thousands of NEGATIVE cash."""
    wallet = crypto.create_account(conn, "ANON Wallet", kind=crypto.CRYPTO_KIND_EXCHANGE)
    crypto.record_wallet_credit(conn, wallet, "2025-02-02", "ANC", "10")
    crypto.rebuild_holdings(conn, wallet)
    investments.record_prices(conn, [("ANC-USD", AS_OF, "100", "yfinance")])
    bank = ledger.create_account(conn, "ANON Checking", "checking", opening_balance=0)
    ledger.create_transfer(conn, wallet, bank, "2025-03-03", 1_000_00)
    alloc = portfolio.allocation(conn, account_ids=[wallet], as_of=AS_OF)
    assert alloc.total == 10 * 100_00 == crypto.display_balance(conn, wallet, AS_OF)


def test_the_report_names_holdings_it_could_not_price(conn, world):
    ledger.set_opening_balance(conn, world["plan"], 15_050_00)
    _fund(conn, world["plan"], "ANONDARK", "5", "10")
    investments.rebuild_holdings(conn, world["plan"])
    tid = rebalance.create_target(conn, "Plan", lines={"domestic_stock": 100}, active=True)
    rebalance.set_target_accounts(conn, tid, [world["plan"]])
    assert "ANONDARK" in rebalance.drift(conn, tid, as_of=AS_OF).unpriced


def test_a_target_percentage_is_stored_without_an_exponent(conn, world):
    tid = rebalance.create_target(conn, "Plan", lines={"domestic_stock": 70, "bond": 30})
    stored = {r["asset_class"]: r["pct"] for r in conn.execute(
        "SELECT asset_class, pct FROM allocation_target_lines WHERE target_id=?", (tid,))}
    assert stored["domestic_stock"] == "70"           # not "7E+1"
    rebalance.update_target(conn, tid, band_abs_pct=10)
    assert rebalance.get_target(conn, tid)["band_abs_pct"] == "10"


def test_the_dialog_picks_accounts_and_shows_the_kind_of_money(qapp, conn, world):
    from mammon.ui.rebalance_dialog import RebalanceDialog
    tid = rebalance.create_target(conn, "Plan", lines={"domestic_stock": 60, "bond": 40},
                                  active=True)
    dlg = RebalanceDialog(conn, as_of=AS_OF)
    warned = []
    dlg._warn = lambda title, text: warned.append(text)
    dlg.choose_accounts([world["plan"], world["roth"]])
    assert warned and "one kind of money" in warned[0]
    dlg.choose_accounts([world["plan"]])
    assert rebalance.target_accounts(conn, tid) == [world["plan"]]
    assert "Tax-deferred" in dlg.fixed_label.text()
    stock = next(dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())
                 if dlg.tree.topLevelItem(i).text(dlg.CLASS) == "Domestic stock")
    assert stock.childCount() == 1                      # its holdings hang under it
    assert "ANONSTK" in stock.child(0).text(dlg.CLASS)
    dlg.mark_rebalanced()
    assert rebalance.get_target(conn, tid)["rebalanced_on"]
    assert "rebalanced on" in dlg.status.text()
    dlg.deleteLater()


def test_clicking_the_buttons_does_not_pass_qt_the_answer(qapp, conn, world):
    """A clicked signal hands a `checked` bool to any slot that can take one.
    Connected to choose_accounts directly, the click read False as "these are
    the accounts" and emptied the target."""
    from mammon.ui.rebalance_dialog import RebalanceDialog
    tid = rebalance.create_target(conn, "Plan", lines={"domestic_stock": 100}, active=True)
    rebalance.set_target_accounts(conn, tid, [world["plan"]])
    dlg = RebalanceDialog(conn, as_of=AS_OF)
    dlg._ask_accounts = lambda chosen: None            # the user cancels the picker
    dlg.accounts_btn.click()
    assert rebalance.target_accounts(conn, tid) == [world["plan"]]   # not emptied
    dlg.rebalanced_btn.click()
    assert rebalance.get_target(conn, tid)["rebalanced_on"]
    dlg.deleteLater()
