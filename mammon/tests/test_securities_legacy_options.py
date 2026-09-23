"""An option contract in a broker's PRE-2010 shorthand is not a spelling of its
underlying, and the identity review must never offer to collapse it onto one.

The modern OSI symbol was already guarded (`investments.looks_like_option`), but
a long history carries the older form: a root, then one month/right letter and
one strike letter, with the root written in a fixed-width three-character field
-- so "MS DJ" (two-character root, space-padded) and "LOWFX" (three-character
root, closed up) are the same spelling. Nothing recognized it, so the generic
heuristic read "MS" as the ticker and proposed a merge that `apply_splits`
performs by DELETING the contract's rows. User-reported, over a real ledger:
"a bunch of securities like 'MS DJ' and 'MS FH' that it wants to merge but I
have no idea what they are".

Two things are pinned here and they pull against each other. Every four- or
five-letter ticker "decodes" as the compact form (AAPL -> AA/PL), so the
refusal must NOT fire on ordinary funds -- that would stamp a false "option"
label on half the file and make the 100-row dialog worse, not better. The rule
that separates them: consult the decoder only for a row that would otherwise
change its key. Tickers keep their own key; contracts are the ones being
renamed away.

Synthetic throughout: MS and LOW are generic public tickers, the ledger around
them is invented.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from mammon import db, instruments, investments, ledger, securities
from mammon.ui.securities_dialog import (
    SecuritiesDialog, INCLUDE, STATUS, STORED,
)
from mammon.tests import fresh_db

# The stored spellings under test. Both roots are real listed tickers, which is
# the entire danger: the underlying is genuinely in the file next door.
SPACED = ("MS DJ", "MS FH", "LOW FX")
COMPACT = ("MSDJ", "LOWFX")


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "legacyopts.db")
    yield c
    c.close()


def _buy(conn, account, symbol, qty=5, price="4.25", date="2008-04-11"):
    investments.record_investment(
        conn, account, date, "Buy", symbol=symbol, quantity=str(qty),
        price=str(price), amount=-int(Decimal(qty) * Decimal(price) * 100))
    investments.rebuild_holdings(conn, account)


# ---- the decoder ----------------------------------------------------------
def test_the_spaced_broker_form_decodes_like_the_compact_one():
    """"MS DJ" and "MSDJ" are one symbol written two ways: a root padded into a
    three-character field, then April ('D') and the strike code."""
    for text in ("MS DJ", "MSDJ"):
        terms = instruments.parse_legacy_option(text)
        assert terms is not None, text
        assert terms.underlying == "MS"
        assert terms.month == 4 and terms.right == "C"
        assert terms.strike_code == "J"
    wide = instruments.parse_legacy_option("LOW FX")
    assert wide is not None
    assert (wide.underlying, wide.month, wide.right) == ("LOW", 6, "C")
    assert instruments.parse_legacy_option("LOWFX") == wide


def test_the_strike_day_and_year_stay_unknown():
    """The shorthand states the month and the right and genuinely does not state
    the rest; UNKNOWN is the sentinel, not None and not a guess."""
    terms = instruments.parse_legacy_option("MS FH")
    assert terms.strike is instruments.UNKNOWN
    assert terms.day is instruments.UNKNOWN
    assert terms.year is instruments.UNKNOWN
    assert terms.expiration is instruments.UNKNOWN


def test_letters_that_are_not_a_month_right_code_are_not_decoded():
    """Y and Z encode no month in either direction, so "MS YZ" is some other
    two-word name and gets no option treatment -- the table is the test, and a
    symbol outside it returns None rather than a guess."""
    for text in ("MS YZ", "MS ZQ", "MSYZ", "LOW ZX"):
        assert instruments.parse_legacy_option(text) is None, text


def test_a_one_letter_root_is_still_refused():
    """The four-letter minimum survives the space: "A BC" would otherwise decode
    as root A, and a one-letter root is the collision this was written against."""
    assert instruments.parse_legacy_option("A BC") is None
    assert instruments.parse_legacy_option("IBM") is None


# ---- suggest() ------------------------------------------------------------
def _refused(split, old):
    assert split.old == old
    assert split.symbol == old, f"{old!r} was proposed as {split.symbol!r}"
    assert split.changes_key is False
    assert split.name is None
    assert split.confident is False
    assert split.reason and "option" in split.reason.lower()


def test_a_spaced_contract_is_never_proposed_for_its_underlying():
    for stored in SPACED:
        _refused(securities.suggest(stored), stored)


def test_a_stated_root_does_not_turn_a_contract_into_its_stock():
    """The worst case, and the one a QIF/IB import actually produces: the source
    states the ROOT in its symbol field, which suggest() normally treats as
    recorded fact -- so the merge would be applied confidently."""
    for stored, root in (("MSDJ", "MS"), ("LOWFX", "LOW"), ("MS FH", "MS")):
        _refused(securities.suggest(stored, root), stored)


def test_the_refusal_says_what_the_symbol_means():
    """The user could not identify these rows at all. The reason decodes them."""
    reason = securities.suggest("MS DJ").reason
    assert "MS" in reason and "Apr" in reason and "call" in reason
    assert "never merged" in reason


def test_a_bare_compact_symbol_is_left_alone_even_without_the_decoder():
    """"MSDJ" with nothing stated is already its own identity -- one token, no
    remainder, no key change. Pinned because it is why the decoder does not need
    to fire here, and firing it would mislabel every four-letter fund."""
    split = securities.suggest("MSDJ")
    assert split.symbol == "MSDJ" and split.changes_key is False


def test_ordinary_names_still_get_their_normal_proposal():
    """The guard must not eat the feature. "FID BALANCED K6" still proposes its
    leading token (that guess is the dialog's whole reason to exist), and an
    ordinary ticker is untouched and unlabelled."""
    split = securities.suggest("FID BALANCED K6")
    assert split.symbol == "FID"
    assert split.name == "BALANCED K6"
    assert split.reason is None
    for ticker in ("AAPL", "VFIAX", "FIPDX", "GOOG"):
        plain = securities.suggest(ticker)
        assert plain.symbol == ticker and plain.reason is None, ticker
        stated = securities.suggest(ticker, ticker)
        assert stated.reason is None and stated.changes_key is False, ticker


def test_a_two_word_name_whose_tail_is_not_a_code_is_proposed_as_before():
    """The decoder is the validity test, not the space: "MS YZ" is no contract,
    so it goes back to the ordinary ticker heuristic."""
    split = securities.suggest("MS YZ")
    assert split.symbol == "MS"
    assert split.reason is None


def test_the_modern_osi_symbol_is_guarded_exactly_as_before():
    stored = "XYZ 260117C00150000 XYZ 17JAN26 150 C"
    split = securities.suggest(stored, "XYZ")
    _refused(split, stored)
    assert split.reason == securities.OPTION_REASON


# ---- nothing can act on a refused row -------------------------------------
def test_applying_every_suggestion_leaves_the_contracts_untouched(conn):
    """The structural half of the guarantee: a refused split changes no key and
    carries no description, so apply_splits -- which re-keys and DELETES -- has
    nothing it could execute, even when the caller applies the whole list."""
    acct = ledger.create_account(conn, "Brokerage", "investment")
    _buy(conn, acct, "MS DJ")
    _buy(conn, acct, "MSDJ")
    _buy(conn, acct, "MS", qty=100, price="41.00")
    securities.record_master(conn, [("MSDJ", "MS", None)])
    securities.apply_splits(conn, securities.suggest_all(conn))
    left = sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions"))
    assert left == ["MS", "MS DJ", "MSDJ"]


# ---- the dialog -----------------------------------------------------------
@pytest.fixture
def world(conn):
    """One stock, two contracts on it, and two ordinary securities so the
    proposals the dialog exists for are still present."""
    acct = ledger.create_account(conn, "Brokerage", "investment")
    _buy(conn, acct, "MS", qty=100, price="41.00")
    _buy(conn, acct, "MS DJ")
    _buy(conn, acct, "MS FH")
    _buy(conn, acct, "ZBX ZEBRA WIDGET CORP", qty=20, price="13.50")
    _buy(conn, acct, "HOUSE BOND INDEX", qty=30, price="10.00")
    return acct


def _row_for(dlg, stored):
    for r, s in enumerate(dlg._splits):
        if s.old == stored:
            return r
    raise AssertionError(f"{stored!r} not listed")


def test_option_rows_start_unticked_and_cannot_be_ticked(conn, world):
    dlg = SecuritiesDialog(conn)
    for stored in ("MS DJ", "MS FH"):
        row = _row_for(dlg, stored)
        tick = dlg.table.item(row, INCLUDE)
        assert tick.checkState() == Qt.Unchecked, stored
        assert not (tick.flags() & Qt.ItemIsUserCheckable), stored
    dlg._set_all(True)          # "Select all changes" must not reach them
    for stored in ("MS DJ", "MS FH"):
        assert dlg.table.item(_row_for(dlg, stored),
                              INCLUDE).checkState() == Qt.Unchecked, stored


def test_an_option_row_says_what_it_is(conn, world):
    dlg = SecuritiesDialog(conn)
    text = dlg.table.item(_row_for(dlg, "MS DJ"), STATUS).text()
    assert "option contract" in text
    assert "never merged" in text
    assert "merges with" not in text


def test_chosen_never_returns_an_option_row(conn, world):
    dlg = SecuritiesDialog(conn)
    assert [s for s in dlg.chosen() if s.old in ("MS DJ", "MS FH")] == []
    # ...while the row the dialog exists for is still offered.
    assert any(s.old == "ZBX ZEBRA WIDGET CORP" and s.symbol == "ZBX"
               for s in dlg.chosen())


def test_option_rows_sort_together_at_the_foot(conn, world):
    """Interleaved among real proposals they read as changes to understand
    before ticking, and on a real ledger there are dozens."""
    dlg = SecuritiesDialog(conn)
    order = [s.old for s in dlg._splits]
    assert order[-2:] == ["MS DJ", "MS FH"]
    assert dlg.table.item(dlg.table.rowCount() - 1, STORED).text() == "MS FH"


def test_the_count_line_says_how_few_rows_want_a_decision(conn, world):
    dlg = SecuritiesDialog(conn)
    text = dlg.tally.text()
    assert "5 securities" in text
    # The two multi-word names; MS is already itself and the contracts are
    # refused outright -- which is the point: 5 rows, 2 decisions.
    assert "2 proposed changes" in text
    assert "3 left alone" in text
    assert "2 option contracts" in text
    # The moving summary is a different question and still answers it.
    assert "selected" in dlg.summary.text()
