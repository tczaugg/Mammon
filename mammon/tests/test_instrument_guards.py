"""Option contracts are not spellings of their underlying -- the refusals.

Everything in ``mammon.securities`` and in ``security_aliases`` assumes the two
strings in front of it are two names for ONE continuous instrument. A contract
and its stock are two instruments, and the code could not tell:
``investments.ticker_of`` reads the first token of
"XYZ 260117C00150000 XYZ 17JAN26 150 C" as "XYZ", and a QIF option block states
the ROOT in its ``S`` field -- so a proposal to rename the contract to the stock
arrived marked as recorded fact, and ``apply_splits`` executes such a proposal by
DELETING the losing rows.

These tests pin the four guards that close that (see the module docstrings of
``mammon.securities`` and ``mammon.investments``), plus one equity case proving
nothing normal changed. The classifier they lean on,
``investments.looks_like_option``, is a conservative OSI-shape test standing in
for a real instrument classifier; when that lands these tests should keep
passing unchanged.

Synthetic symbols only -- XYZ is not a listed issuer and no real ledger row is
involved anywhere here.
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from mammon import db, investments, portfolio, securities  # noqa: E402

# The OSI symbol followed by its human rendering, exactly the shape a QIF
# security block's N field carries; the S field states the root.
OSI = "XYZ 260117C00150000 XYZ 17JAN26 150 C"
OSI_UNSPACED = "XYZ260117C00150000"
OSI_PADDED = "SPX   141122P01950000"
ROOT = "XYZ"


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "guards.db")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# The shape test itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [OSI, OSI_UNSPACED, OSI_PADDED])
def test_option_shapes_are_recognised(text):
    assert investments.looks_like_option(text) is True


@pytest.mark.parametrize("text", [
    "XYZ", "VGT VANGUARD INFO TECH ETF", "FID BALANCED K6",
    "TARGET 2030 FUND", "", None,
])
def test_plain_securities_are_not_option_shaped(text):
    assert investments.looks_like_option(text) is False


# ---------------------------------------------------------------------------
# (a) suggest never proposes an identity change for a contract, and is never
#     confident about one -- including when a source stated the root ticker.
# ---------------------------------------------------------------------------
def test_stated_root_ticker_does_not_rename_the_contract():
    split = securities.suggest(OSI, ROOT)
    assert split.symbol == OSI
    assert split.changes_key is False
    assert split.confident is False


def test_heuristic_route_does_not_rename_the_contract():
    # No stated ticker: ticker_of(OSI) is "XYZ", which used to become the
    # proposed identity.
    split = securities.suggest(OSI)
    assert split.symbol == OSI
    assert split.changes_key is False
    assert split.confident is False


def test_contract_proposal_writes_no_description_either():
    # name is None, so applying the batch touches nothing at all for it.
    assert securities.suggest(OSI, ROOT).name is None


def test_suggest_all_leaves_a_stored_contract_alone(conn):
    securities.record_master(conn, [(OSI, ROOT, "Option")])
    investments.record_price(conn, OSI, "2026-01-05", "3.10", "qif")
    proposed = {s.old: s for s in securities.suggest_all(conn)}
    assert proposed[OSI].changes_key is False
    assert proposed[OSI].confident is False


# ---------------------------------------------------------------------------
# (b) an option/underlying alias is refused, in either direction.
# ---------------------------------------------------------------------------
def test_alias_of_contract_onto_underlying_refused(conn):
    portfolio.set_security(conn, ROOT, name="XYZ Industries")
    portfolio.set_security(conn, OSI, name="XYZ Jan 2026 150 call")
    with pytest.raises(ValueError, match="option contract"):
        investments.add_alias(conn, OSI, ROOT)
    assert investments.list_aliases(conn) == []


def test_alias_of_underlying_onto_contract_refused(conn):
    portfolio.set_security(conn, ROOT, name="XYZ Industries")
    portfolio.set_security(conn, OSI, name="XYZ Jan 2026 150 call")
    with pytest.raises(ValueError, match="option contract"):
        investments.add_alias(conn, ROOT, OSI)
    assert investments.list_aliases(conn) == []


def test_option_guard_precedes_the_canonical_exists_guard(conn):
    # The reason for the ordering: the refusal must say WHY, not report the
    # contract as an unknown security and invite the user to create it.
    with pytest.raises(ValueError, match="never another spelling"):
        investments.add_alias(conn, OSI, "NOSUCH")


# ---------------------------------------------------------------------------
# (c) no stock price is ever filed against a contract.
# ---------------------------------------------------------------------------
def test_fetch_ticker_is_none_for_a_contract_whose_source_stated_the_root(conn):
    securities.record_master(conn, [(OSI, ROOT, "Option")])
    assert securities.recorded_ticker(conn, OSI) == ROOT   # the trap
    assert securities.fetch_ticker(conn, OSI) is None      # the guard


def test_fetch_ticker_is_none_for_a_bare_unspaced_contract(conn):
    securities.record_master(conn, [(OSI_UNSPACED, ROOT, "Option")])
    assert securities.fetch_ticker(conn, OSI_UNSPACED) is None


# ---------------------------------------------------------------------------
# (d) apply_splits refuses a colliding, differently-classified target instead
#     of deleting the contract's rows.
# ---------------------------------------------------------------------------
def test_apply_splits_refuses_to_merge_a_contract_into_its_underlying(conn):
    securities.record_master(conn, [(OSI, ROOT, "Option"),
                                    (ROOT, ROOT, "Stock")])
    investments.record_price(conn, OSI, "2026-01-05", "3.10", "qif")
    investments.record_price(conn, ROOT, "2026-01-05", "148.20", "qif")
    hand_made = securities.Split(OSI, ROOT, "XYZ 17JAN26 150 C", confident=True)

    with pytest.raises(ValueError, match="option contract"):
        securities.apply_splits(conn, [hand_made])

    # Nothing moved and nothing was deleted: both price series intact, both
    # securities rows still present.
    # (close_price is stored as normalized Decimal text, hence the Decimal
    # comparison rather than a string one.)
    prices = {sym: Decimal(px) for sym, px in conn.execute(
        "SELECT symbol, close_price FROM price_history WHERE date='2026-01-05'")}
    assert prices == {OSI: Decimal("3.10"), ROOT: Decimal("148.20")}
    stored = securities.stored_symbols(conn)
    assert OSI in stored and ROOT in stored


def test_apply_splits_refuses_the_whole_batch_before_touching_anything(conn):
    # A good split alongside a bad one must not be half-applied.
    securities.record_master(conn, [(OSI, ROOT, "Option"),
                                    (ROOT, ROOT, "Stock"),
                                    ("VGT VANGUARD INFO TECH ETF", "VGT", "ETF")])
    investments.record_price(conn, "VGT VANGUARD INFO TECH ETF",
                             "2026-01-05", "600.00", "qif")
    batch = [
        securities.Split("VGT VANGUARD INFO TECH ETF", "VGT",
                         "VANGUARD INFO TECH ETF", confident=True),
        securities.Split(OSI, ROOT, None, confident=True),
    ]
    with pytest.raises(ValueError):
        securities.apply_splits(conn, batch)
    assert "VGT VANGUARD INFO TECH ETF" in securities.stored_symbols(conn)


# ---------------------------------------------------------------------------
# (e) no regression: a plain equity rename still suggests, aliases and quotes.
# ---------------------------------------------------------------------------
def test_equity_split_still_suggested_confidently():
    split = securities.suggest("VGT VANGUARD INFO TECH ETF", "VGT")
    assert (split.symbol, split.name) == ("VGT", "VANGUARD INFO TECH ETF")
    assert split.changes_key is True
    assert split.confident is True


def test_equity_guess_still_needs_confirmation():
    split = securities.suggest("VGT VANGUARD INFO TECH ETF")
    assert split.symbol == "VGT"
    assert split.confident is False


def test_equity_alias_still_accepted(conn):
    portfolio.set_security(conn, "META", name="Meta Platforms")
    investments.add_alias(conn, "FB", "META")
    assert investments.list_aliases(conn) == [("FB", "META")]
    assert investments.resolve_symbol(conn, "FB") == "META"


def test_equity_ticker_still_fetched(conn):
    securities.record_master(conn, [("VGT VANGUARD INFO TECH ETF", "VGT", "ETF")])
    assert securities.fetch_ticker(conn, "VGT VANGUARD INFO TECH ETF") == "VGT"
    assert securities.fetch_ticker(conn, "FIPDX") == "FIPDX"


def test_equity_merge_still_applies(conn):
    securities.record_master(conn, [("VGT VANGUARD INFO TECH ETF", "VGT", "ETF")])
    investments.record_price(conn, "VGT VANGUARD INFO TECH ETF",
                             "2026-01-05", "600.00", "qif")
    investments.record_price(conn, "VGT", "2026-01-06", "601.00", "qif")
    report = securities.apply_splits(conn, [
        securities.Split("VGT VANGUARD INFO TECH ETF", "VGT",
                         "VANGUARD INFO TECH ETF", confident=True)])
    assert report["renamed"] >= 1
    assert report["merged"] == ["VGT"]
    left = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM price_history")}
    assert left == {"VGT"}
