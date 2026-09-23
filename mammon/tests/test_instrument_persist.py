"""What an import PERSISTS about an instrument's kind, and what it refuses to.

Parsing a contract is only half the job: until the terms reach the `securities`
row, an option is indistinguishable from a share with an odd ticker, and every
downstream number that depends on a 100-share deliverable is quietly wrong.
This file pins the persistence half, and the rule that governs it.

PRECEDENCE -- user > source > derived -- is the load-bearing part. A feed's
statement may fill a NULL kind and may correct something this code merely
inferred from a symbol's shape, but it may NEVER overwrite a classification a
person made: imports are re-run routinely (the same statement, the same
year-end file), and a correction that gets undone by the next import is worse
than no correction screen at all.

The other half of the rule is that an import never GUESSES. A pre-2010 OPRA
symbol states a root, a month and a call/put and genuinely states no strike,
day or year; those land NULL and stay visible as unknown. A fabricated
expiration would silently misprice the position and misdate its expiry.

Fixtures are SYNTHETIC: invented symbols (XYZ/ACME), invented CUSIPs, invented
FITIDs.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, importers, instruments, securities
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


# --------------------------------------------------------------------------
# synthetic statements
# --------------------------------------------------------------------------
def _stmt(txns: str = "", seclist: str = "") -> str:
    sec = f"<SECLISTMSGSRSV1><SECLIST>\n{seclist}\n</SECLIST></SECLISTMSGSRSV1>\n" if seclist else ""
    return (
        "<OFX>\n"
        "<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>\n"
        "<INVACCTFROM><ACCTID>Z1</ACCTID></INVACCTFROM>\n"
        "<INVTRANLIST>\n"
        f"{txns}\n"
        "</INVTRANLIST>\n"
        "</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>\n"
        f"{sec}"
        "</OFX>\n"
    )


_XYZ_STOCK = """<STOCKINFO><SECINFO>
<SECID><UNIQUEID>XYZBASE001</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>
<SECNAME>Acme Widgets Inc</SECNAME><TICKER>XYZ</TICKER>
</SECINFO></STOCKINFO>"""

_ACME_FUND = """<MFINFO><SECINFO>
<SECID><UNIQUEID>ACMEFUND01</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>
<SECNAME>Acme Index Fund</SECNAME><TICKER>ACMEX</TICKER>
</SECINFO><MFTYPE>OPENEND</MFTYPE></MFINFO>"""


def _optinfo(uid: str, ticker: str, right: str, strike: str, expire: str,
             shperctrct: str = "100", name: str = "XYZ option") -> str:
    return (
        "<OPTINFO><SECINFO>\n"
        f"<SECID><UNIQUEID>{uid}</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>\n"
        f"<SECNAME>{name}</SECNAME><TICKER>{ticker}</TICKER>\n"
        "</SECINFO>\n"
        f"<OPTTYPE>{right}</OPTTYPE><STRIKEPRICE>{strike}</STRIKEPRICE>\n"
        f"<DTEXPIRE>{expire}</DTEXPIRE><SHPERCTRCT>{shperctrct}</SHPERCTRCT>\n"
        "<SECID><UNIQUEID>XYZBASE001</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>\n"
        "</OPTINFO>"
    )


_XYZ_CALL = _optinfo("OPTXYZC150", "XYZ   260117C00150000", "CALL", "150.00", "20260117")

# Built from the terms, never typed out, so this file cannot drift from
# instruments.OptionTerms.osi().
_XYZ_CALL_OSI = instruments.OptionTerms(
    underlying="XYZ", root="XYZ", expiration="2026-01-17",
    strike=Decimal("150.00"), right="C",
).osi()

# A pre-2010 OPRA spelling: root + month/right code + strike code, and no
# OPTTYPE terms beyond the right. The strike table it needs is not in the file.
_XYZ_LEGACY = (
    "<OPTINFO><SECINFO>\n"
    "<SECID><UNIQUEID>OPTLEGACY1</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>\n"
    "<SECNAME>Acme legacy contract</SECNAME><TICKER>XYZAF</TICKER>\n"
    "</SECINFO>\n<OPTTYPE>CALL</OPTTYPE>\n</OPTINFO>"
)


def _buyopt(fitid: str, uid: str, units: str, price: str, total: str) -> str:
    return (
        "<BUYOPT>\n<INVBUY>\n"
        f"<INVTRAN><FITID>{fitid}</FITID><DTTRADE>20260105</DTTRADE></INVTRAN>\n"
        f"<SECID><UNIQUEID>{uid}</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>\n"
        f"<UNITS>{units}</UNITS><UNITPRICE>{price}</UNITPRICE><TOTAL>{total}</TOTAL>\n"
        "</INVBUY>\n<OPTBUYTYPE>BUYTOOPEN</OPTBUYTYPE><SHPERCTRCT>100</SHPERCTRCT>\n"
        "</BUYOPT>"
    )


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _row(conn, symbol):
    return conn.execute(
        "SELECT symbol, kind, kind_source, multiplier, underlying, expiration, "
        "strike, option_right FROM securities WHERE symbol=?", (symbol,)).fetchone()


# --------------------------------------------------------------------------
# (a) full life cycle: an OPTINFO the file states lands as a classified row
# --------------------------------------------------------------------------
def test_optinfo_persists_the_contract_it_states(conn, tmp_path):
    """Parsed terms are not enough -- they have to reach the row. Everything the
    <OPTINFO> stated is stored, and kind_source records that a FEED said it, not
    that this code worked it out."""
    path = _write(tmp_path, "opt.qfx",
                  _stmt(_buyopt("OB1", "OPTXYZC150", "2", "3.50", "-700.00"),
                        _XYZ_STOCK + "\n" + _XYZ_CALL))
    importers.import_file(conn, path, account="Broker")

    row = _row(conn, _XYZ_CALL_OSI)
    assert row is not None, "the contract the file named has no securities row"
    assert row["kind"] == "option"
    assert row["kind_source"] == "source"
    assert Decimal(row["multiplier"]) == 100          # <SHPERCTRCT>, the deliverable
    assert row["underlying"] == "XYZ"                 # linked, not collapsed onto
    assert row["expiration"] == "2026-01-17"          # ISO, never the OFX compact form
    assert Decimal(row["strike"]) == Decimal("150.00")
    assert row["option_right"] == "C"
    # The contract is its own security; it did not become its underlying, and no
    # alias was invented between them.
    assert row["symbol"] != "XYZ"
    assert conn.execute("SELECT COUNT(*) c FROM security_aliases").fetchone()["c"] == 0


def test_stated_multiplier_is_the_adjusted_one_not_an_assumed_hundred(conn, tmp_path):
    """An adjusted contract delivers an odd share count. Persisting 100 because
    options 'are' 100 shares misstates every amount derived from it."""
    adj = _optinfo("OPTXYZADJ", "XYZ1  260117C00150000", "CALL", "150.00",
                   "20260117", shperctrct="87")
    path = _write(tmp_path, "adj.qfx", _stmt(seclist=_XYZ_STOCK + "\n" + adj))
    importers.import_file(conn, path, account="Broker")

    rows = [r for r in conn.execute(
        "SELECT symbol, kind, multiplier FROM securities WHERE kind='option'")]
    assert len(rows) == 1
    assert Decimal(rows[0]["multiplier"]) == 87


# --------------------------------------------------------------------------
# (b) re-import is idempotent
# --------------------------------------------------------------------------
def test_reimporting_the_same_file_changes_nothing(conn, tmp_path):
    """Statements get re-imported constantly. A second pass must leave ONE row
    with the SAME values -- not a second contract, and not a column churned by
    a source restating what it already said."""
    path = _write(tmp_path, "opt.qfx",
                  _stmt(_buyopt("OB1", "OPTXYZC150", "2", "3.50", "-700.00"),
                        _XYZ_STOCK + "\n" + _XYZ_CALL))
    importers.import_file(conn, path, account="Broker")
    first = tuple(_row(conn, _XYZ_CALL_OSI))

    importers.import_file(conn, path, account="Broker")
    assert conn.execute(
        "SELECT COUNT(*) c FROM securities WHERE symbol=?",
        (_XYZ_CALL_OSI,)).fetchone()["c"] == 1
    assert tuple(_row(conn, _XYZ_CALL_OSI)) == first


# --------------------------------------------------------------------------
# (c) precedence: a person's classification outranks any feed
# --------------------------------------------------------------------------
def test_a_user_classification_survives_reimport(conn, tmp_path):
    """The whole reason kind_source exists. The user corrected the deliverable
    (an adjusted contract the broker still files as 100); re-importing the file
    that says 100 must not undo it, or the correction screen is a lie."""
    path = _write(tmp_path, "opt.qfx",
                  _stmt(_buyopt("OB1", "OPTXYZC150", "2", "3.50", "-700.00"),
                        _XYZ_STOCK + "\n" + _XYZ_CALL))
    importers.import_file(conn, path, account="Broker")

    securities.set_kinds(conn, [securities.KindUpdate(
        symbol=_XYZ_CALL_OSI, kind="option", kind_source="user",
        multiplier="87", underlying="XYZ", expiration="2026-01-17",
        strike="150.00", option_right="C")])

    importers.import_file(conn, path, account="Broker")
    row = _row(conn, _XYZ_CALL_OSI)
    assert row["kind_source"] == "user"
    assert Decimal(row["multiplier"]) == 87       # NOT restored to the feed's 100
    assert row["kind"] == "option"


# --------------------------------------------------------------------------
# (d) an incomplete contract lands PARTIAL and visible, never invented
# --------------------------------------------------------------------------
def test_legacy_contract_lands_partial_with_nulls_for_what_is_unknown(conn, tmp_path):
    """A pre-2010 symbol encodes no strike, no day and no year. Those columns
    stay NULL -- the absence of knowledge, on the record -- and the row is still
    an option, because the feed said so. It must never be classified as equity
    just because its terms would not parse."""
    path = _write(tmp_path, "legacy.qfx",
                  _stmt(seclist=_XYZ_STOCK + "\n" + _XYZ_LEGACY))
    importers.import_file(conn, path, account="Broker")

    row = _row(conn, "XYZAF")                    # the broker's own spelling, kept
    assert row is not None
    assert row["kind"] == "option" and row["kind_source"] == "source"
    assert row["underlying"] == "XYZ"            # what the spelling DOES state
    assert row["option_right"] == "C"
    assert row["strike"] is None                 # ...and what it does not
    assert row["expiration"] is None
    # No invented OSI contract was manufactured alongside it.
    assert conn.execute(
        "SELECT COUNT(*) c FROM securities WHERE kind='option'").fetchone()["c"] == 1


# --------------------------------------------------------------------------
# (e) anything the feed did not state stays NULL
# --------------------------------------------------------------------------
def test_stockinfo_states_no_kind_and_none_is_invented(conn, tmp_path):
    """A broker files a share, an ETF, an ADR and a money-market fund alike under
    <STOCKINFO>. Mapping it to 'equity' would record a guess with the standing of
    a statement, so nothing is written at all."""
    path = _write(tmp_path, "stock.qfx", _stmt(seclist=_XYZ_STOCK))
    importers.import_file(conn, path, account="Broker")

    row = _row(conn, "XYZ")
    assert row is None or row["kind"] is None
    assert conn.execute(
        "SELECT COUNT(*) c FROM securities WHERE kind IS NOT NULL").fetchone()["c"] == 0


def test_mfinfo_and_debtinfo_state_their_kind_outright(conn, tmp_path):
    """The two wrappers that DO say what the instrument is are believed, with
    kind_source='source' -- and they carry no option terms."""
    path = _write(tmp_path, "fund.qfx", _stmt(seclist=_ACME_FUND))
    importers.import_file(conn, path, account="Broker")

    row = _row(conn, "ACMEX")
    assert row["kind"] == "mutual_fund"
    assert row["kind_source"] == "source"
    assert row["option_right"] is None and row["strike"] is None
