"""The classification audit: what the securities table says, and what it hides.

Two properties are being held down here. The first is that the report READS --
it is the safe half of a backfill onto a 40-year ledger, so running it leaves
the file byte for byte as it was. The second is that it separates the three
shapes an old destructive merge leaves behind instead of raising one alarm: a
ticker that differs from the symbol is the ordinary rename and benign; a
CONTRACT whose ticker is its underlying's is one Apply away from being absorbed
into the stock; and a row whose identity is not a contract while its description
is one is already two instruments fused into one row.

A fused row is deliberately proposed NO kind change. Relabelling it would pick
one of the two instruments and lose the other quietly, and un-fusing is a
separate operation that does not exist yet.

All data here is synthetic.
"""
from __future__ import annotations

import hashlib

import pytest

from mammon import db, instruments
from mammon.reports import security_audit
from mammon.tests import fresh_db


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "audit.db"


@pytest.fixture
def conn(db_path):
    c = fresh_db(db_path)
    yield c
    c.close()


def _sec(conn, symbol, name=None, sec_type=None, ticker=None, kind=None):
    """A securities row exactly as a pre-migration-67 import left it: kind NULL
    unless the test is about a row that is already classified."""
    conn.execute(
        "INSERT INTO securities(symbol, name, sec_type, ticker, kind) "
        "VALUES (?,?,?,?,?)", (symbol, name, sec_type, ticker, kind))
    conn.commit()
    return symbol


OSI = "XYZ 260117C00150000 XYZ 17JAN26 150 C"


# ---------------------------------------------------------------------------
# what each row is proposed to be
# ---------------------------------------------------------------------------
def test_a_plain_ticker_is_proposed_as_an_equity(conn):
    _sec(conn, "XYZ", "XYZ INC", "Stock")
    row = security_audit.audit(conn).by_symbol("XYZ")
    assert row.proposal.kind == instruments.Kind.EQUITY.value
    assert row.proposal.kind_source == "derived"     # proposed, not decided
    assert not row.proposal.is_option


def test_funds_and_cash_are_not_all_called_equity(conn):
    _sec(conn, "VGT VANGUARD INFO TECH ETF", "VANGUARD INFO TECH ETF", "ETF")
    _sec(conn, "DOMESTIC BOND INDEX", "DOMESTIC BOND INDEX", "Mutual Fund")
    _sec(conn, "MMKT CASH RESERVES", "CASH RESERVES", "Money Market")
    audit = security_audit.audit(conn)
    assert audit.by_symbol("VGT VANGUARD INFO TECH ETF").proposal.kind == "etf"
    assert audit.by_symbol("DOMESTIC BOND INDEX").proposal.kind == "mutual_fund"
    assert audit.by_symbol("MMKT CASH RESERVES").proposal.kind == "money_market"


def test_an_option_symbol_carries_its_terms_out(conn):
    _sec(conn, OSI, None, "Option")
    p = security_audit.audit(conn).by_symbol(OSI).proposal
    assert p.kind == instruments.Kind.OPTION.value
    assert (p.underlying, p.expiration, p.strike, p.option_right) == \
        ("XYZ", "2026-01-17", "150", "C")
    assert p.multiplier == "100"
    assert p.unknown == ()


def test_a_pre_2010_symbol_leaves_the_unstated_terms_null_and_named(conn):
    """The year and the strike are simply not in an OPRA symbol. Manufacturing
    them is indistinguishable from reading them, so they stay NULL."""
    _sec(conn, "IBMAF", None, "Option")
    p = security_audit.audit(conn).by_symbol("IBMAF").proposal
    assert p.kind == instruments.Kind.OPTION.value
    assert (p.underlying, p.option_right) == ("IBM", "C")
    assert p.strike is None and p.expiration is None and p.multiplier is None
    assert "strike" in p.unknown and "multiplier" in p.unknown


def test_the_legacy_decoder_is_not_let_loose_on_ordinary_tickers(conn):
    """`parse_legacy_option` is not a detector -- it reads "IBM" happily. It is
    only asked about rows something already calls an option."""
    _sec(conn, "IBM", "INTERNATIONAL BUSINESS MACHINES", "Stock")
    p = security_audit.audit(conn).by_symbol("IBM").proposal
    assert p.kind == instruments.Kind.EQUITY.value
    assert not p.has_terms


def test_null_kind_is_unclassified_and_never_equity(conn):
    _sec(conn, "XYZ", "XYZ INC", "Stock")
    audit = security_audit.audit(conn)
    row = audit.by_symbol("XYZ")
    assert row.kind is None and not row.classified
    assert row.changes                       # the proposal would be a change
    assert audit.counts["unclassified"] == 1
    assert audit.counts["classified"] == 0


def test_a_row_already_classified_proposes_no_change(conn):
    _sec(conn, "XYZ", "XYZ INC", "Stock", kind="equity")
    row = security_audit.audit(conn).by_symbol("XYZ")
    assert row.classified and not row.changes
    assert security_audit.audit(conn).counts["changes"] == 0


# ---------------------------------------------------------------------------
# fusion and tickers
# ---------------------------------------------------------------------------
def test_a_rename_ticker_is_a_mismatch_but_not_suspect(conn):
    _sec(conn, "VGT VANGUARD INFO TECH ETF", "VANGUARD INFO TECH ETF", "ETF",
         ticker="VGT")
    row = security_audit.audit(conn).by_symbol("VGT VANGUARD INFO TECH ETF")
    assert row.ticker_mismatch
    assert not row.ticker_points_elsewhere and not row.fused
    assert not row.suspect


def test_a_contract_filed_under_its_underlying_is_flagged(conn):
    """The ticker is what a quote fetch and a merge proposal both key on, so a
    contract carrying its stock's ticker is one Apply from being absorbed."""
    _sec(conn, OSI, None, "Option", ticker="XYZ")
    row = security_audit.audit(conn).by_symbol(OSI)
    assert row.ticker_points_elsewhere and row.suspect
    assert "XYZ" in row.note
    assert security_audit.audit(conn).counts["ticker_points_elsewhere"] == 1


def test_a_contract_whose_ticker_is_the_same_contract_is_not_flagged(conn):
    _sec(conn, OSI, None, "Option", ticker="XYZ260117C00150000")
    row = security_audit.audit(conn).by_symbol(OSI)
    assert row.ticker_mismatch            # the two spellings do differ
    assert not row.ticker_points_elsewhere


def test_a_stock_row_describing_a_contract_is_fused(conn):
    """What a completed merge of a contract into its stock looks like from the
    securities table alone: one row, two instruments."""
    _sec(conn, "XYZ", OSI, "Stock")
    audit = security_audit.audit(conn)
    row = audit.by_symbol("XYZ")
    assert row.fused and row.suspect
    assert audit.counts["fused"] == 1
    assert [r.symbol for r in audit.suspect_rows] == ["XYZ"]


def test_a_fused_row_is_never_proposed_as_a_reclassification(conn):
    """The bug this ordering prevents: proposing OPTION for a row whose identity
    is a stock would let a confirmation screen quietly restate the stock."""
    _sec(conn, "XYZ", OSI, "Stock", kind="equity")
    row = security_audit.audit(conn).by_symbol("XYZ")
    assert row.fused
    assert row.proposal.kind == "equity"
    assert not row.changes


def test_a_stock_and_its_contracts_are_reported_as_one_mixed_group(conn):
    _sec(conn, "XYZ", "XYZ INC", "Stock")
    _sec(conn, OSI, None, "Option")
    _sec(conn, "XYZ 260117P00120000 XYZ 17JAN26 120 P", None, "Option")
    audit = security_audit.audit(conn)
    [group] = audit.groups
    assert group.ticker == "XYZ"
    assert len(group.symbols) == 3
    assert group.mixed                    # a stock and two contracts
    assert audit.counts["mixed_kind_groups"] == 1
    assert audit.counts["shared_ticker_symbols"] == 3


def test_unrelated_securities_do_not_form_a_group(conn):
    _sec(conn, "XYZ", "XYZ INC", "Stock")
    _sec(conn, "QRS", "QRS CORP", "Stock")
    audit = security_audit.audit(conn)
    assert audit.groups == []
    assert audit.counts["shared_ticker_groups"] == 0


# ---------------------------------------------------------------------------
# the shape of the report itself
# ---------------------------------------------------------------------------
def test_counts_add_up_over_a_mixed_file(conn):
    _sec(conn, "XYZ", "XYZ INC", "Stock")
    _sec(conn, OSI, None, "Option", ticker="XYZ")
    _sec(conn, "IBMAF", None, "Option")
    _sec(conn, "QRS", OSI, "Stock", kind="equity")
    counts = security_audit.audit(conn).counts
    assert counts["total"] == 4
    assert counts["classified"] == 1 and counts["unclassified"] == 3
    assert counts["proposed_options"] == 2
    assert counts["incomplete_terms"] == 1       # the pre-2010 symbol
    assert counts["fused"] == 1
    assert counts["changes"] == 3                # the fused row proposes none


def test_the_report_can_be_narrowed_to_named_symbols(conn):
    _sec(conn, "XYZ", "XYZ INC", "Stock")
    _sec(conn, "QRS", "QRS CORP", "Stock")
    audit = security_audit.audit(conn, symbols=["QRS"])
    assert [r.symbol for r in audit.rows] == ["QRS"]
    assert audit.counts["total"] == 1


def test_an_empty_file_reports_nothing_rather_than_failing(conn):
    audit = security_audit.audit(conn)
    assert audit.rows == [] and audit.groups == []
    assert audit.counts["total"] == 0


def _fingerprint(path):
    """Every byte of the database, journal included -- a write in WAL mode lands
    beside the file, not in it."""
    out = {}
    for p in (path, path.with_suffix(".db-wal"), path.with_suffix(".db-shm")):
        if p.exists():
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_running_the_report_leaves_the_database_byte_identical(conn, db_path):
    """The property the whole report-before-action split exists for: this runs
    on a real 40-year ledger, so it must not be able to change one."""
    _sec(conn, "XYZ", "XYZ INC", "Stock")
    _sec(conn, OSI, None, "Option", ticker="XYZ")
    _sec(conn, "IBMAF", None, "Option")
    _sec(conn, "QRS", OSI, "Stock")
    before_rows = conn.execute(
        "SELECT * FROM securities ORDER BY symbol").fetchall()
    before = _fingerprint(db_path)

    audit = security_audit.audit(conn)
    assert audit.counts["total"] == 4            # it really did read the file

    assert _fingerprint(db_path) == before
    after_rows = conn.execute(
        "SELECT * FROM securities ORDER BY symbol").fetchall()
    assert [tuple(r) for r in after_rows] == [tuple(r) for r in before_rows]


def test_the_report_module_never_writes(conn):
    """Belt and braces on the layering rule: a read-only connection is enough
    to run the whole report."""
    conn.execute("PRAGMA query_only = ON")
    try:
        _ = security_audit.audit(conn)
    finally:
        conn.execute("PRAGMA query_only = OFF")
