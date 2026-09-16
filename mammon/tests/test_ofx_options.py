"""OFX/QFX option handling: contracts survive the import instead of being
dropped or flattened.

Three things are pinned here, and each one was a real way to lose money silently:

1. IDENTITY. A ``<SECLIST>``'s ``<OPTINFO>`` states the contract's terms, so every
   broker spelling of one contract -- ``XYZ   260117C00150000``, ``.XYZ260117C150``,
   a bare CUSIP -- must resolve to the SAME 21-char OSI symbol, and two contracts
   that differ only in strike or expiry must NOT. The underlying's ticker is never
   the answer: collapsing an option onto ``XYZ`` fuses the option position into the
   stock position.
2. THE TRADES THEMSELVES. ``<BUYOPT>``/``<SELLOPT>`` used to be unmapped, i.e.
   reported by the audit and then dropped, which left an options account's
   holdings permanently wrong. Their OPTBUYTYPE/OPTSELLTYPE open/close flag is
   load-bearing: SELLTOOPEN WRITES a contract (a short), and importing it as a
   plain Sell would relieve units the account never held.
3. THE ENDING. ``<CLOSUREOPT>`` used to become a cash-neutral RemoveShares for all
   three outcomes, which threw away the only realized result an option position
   ever has. An expiry realizes the premium; an assignment covers a short; an
   exercise is deliberately the one cash-neutral case, because its cost rolls into
   the share leg named by RELFITID.

Fixtures are SYNTHETIC: invented symbols (XYZ/ACME), invented CUSIPs and FITIDs.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, importers, instruments, investments
from mammon.importers.ofx import (
    _parse_seclist,
    option_leg,
    parse_ofx,
    unmapped_investment_actions,
)


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


# --------------------------------------------------------------------------
# fixtures: minimal synthetic statements
# --------------------------------------------------------------------------
def _stmt(txns: str = "", seclist: str = "") -> str:
    """A minimal single-account investment statement, optionally with a SECLIST."""
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


# The underlying, so the OPTINFO's trailing <SECID> has something to resolve to.
_XYZ_STOCK = """<STOCKINFO><SECINFO>
<SECID><UNIQUEID>XYZBASE001</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>
<SECNAME>Acme Widgets Inc</SECNAME><TICKER>XYZ</TICKER>
</SECINFO></STOCKINFO>"""


def _optinfo(uid: str, ticker: str, right: str, strike: str, expire: str,
             shperctrct: str = "100", name: str = "XYZ option") -> str:
    """One <OPTINFO>: its own <SECINFO> (the contract) then the UNDERLYING's
    <SECID>. The two SECIDs are told apart by position, so the order matters."""
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

# The canonical identity, built from the terms rather than typed out, so this test
# cannot drift from instruments.OptionTerms.osi().
_XYZ_CALL_OSI = instruments.OptionTerms(
    underlying="XYZ", root="XYZ", expiration="2026-01-17",
    strike=Decimal("150.00"), right="C",
).osi()


def _buyopt(fitid: str, uid: str, flag: str, units: str, price: str, total: str) -> str:
    return (
        "<BUYOPT>\n<INVBUY>\n"
        f"<INVTRAN><FITID>{fitid}</FITID><DTTRADE>20260105</DTTRADE></INVTRAN>\n"
        f"<SECID><UNIQUEID>{uid}</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>\n"
        f"<UNITS>{units}</UNITS><UNITPRICE>{price}</UNITPRICE><TOTAL>{total}</TOTAL>\n"
        f"</INVBUY>\n<OPTBUYTYPE>{flag}</OPTBUYTYPE><SHPERCTRCT>100</SHPERCTRCT>\n"
        "</BUYOPT>"
    )


def _sellopt(fitid: str, uid: str, flag: str, units: str, price: str, total: str,
             relfitid: str = "") -> str:
    rel = f"<RELFITID>{relfitid}</RELFITID>\n" if relfitid else ""
    return (
        "<SELLOPT>\n<INVSELL>\n"
        f"<INVTRAN><FITID>{fitid}</FITID><DTTRADE>20260110</DTTRADE></INVTRAN>\n"
        f"<SECID><UNIQUEID>{uid}</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>\n"
        f"<UNITS>{units}</UNITS><UNITPRICE>{price}</UNITPRICE><TOTAL>{total}</TOTAL>\n"
        f"</INVSELL>\n<OPTSELLTYPE>{flag}</OPTSELLTYPE>\n{rel}"
        "</SELLOPT>"
    )


def _closureopt(fitid: str, uid: str, action: str, units: str, relfitid: str = "",
                date: str = "20260117") -> str:
    rel = f"<RELFITID>{relfitid}</RELFITID>\n" if relfitid else ""
    return (
        "<CLOSUREOPT>\n"
        f"<INVTRAN><FITID>{fitid}</FITID><DTTRADE>{date}</DTTRADE></INVTRAN>\n"
        f"<SECID><UNIQUEID>{uid}</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>\n"
        f"<OPTACTION>{action}</OPTACTION><UNITS>{units}</UNITS><SHPERCTRCT>100</SHPERCTRCT>\n"
        f"{rel}</CLOSUREOPT>"
    )


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _rows(txns: str, seclist: str = "") -> dict:
    return {t.fitid: t for t in parse_ofx(_stmt(txns, seclist))}


# --------------------------------------------------------------------------
# (a) the SECLIST: OPTINFO -> option terms, canonical OSI symbol, multiplier
# --------------------------------------------------------------------------
def test_optinfo_becomes_an_option_security_with_osi_symbol():
    sec = _parse_seclist(_stmt(seclist=_XYZ_STOCK + "\n" + _XYZ_CALL))
    entry = sec["OPTXYZC150"]
    assert entry.is_option
    assert entry.symbol == _XYZ_CALL_OSI == "XYZ   260117C00150000"
    terms = entry.option
    assert terms.right == "C"
    assert terms.expiration == "2026-01-17"          # ISO, never the OFX compact form
    assert terms.strike == Decimal("150.00")
    assert terms.underlying == "XYZ"
    # The plain stock entry beside it is untouched and is NOT an option.
    assert sec["XYZBASE001"].symbol == "XYZ" and not sec["XYZBASE001"].is_option


def test_optinfo_multiplier_comes_from_shperctrct():
    """SHPERCTRCT is the deliverable, and it is not always 100: an ADJUSTED
    contract (post-merger, post-special-dividend) delivers an odd share count, and
    assuming 100 misstates every amount derived from it by an order of magnitude."""
    adj = _optinfo("OPTXYZADJ", "XYZ1  260117C00150000", "CALL", "150.00",
                   "20260117", shperctrct="87")
    sec = _parse_seclist(_stmt(seclist=_XYZ_STOCK + "\n" + adj))
    entry = sec["OPTXYZADJ"]
    assert entry.multiplier == "87"
    assert entry.option.multiplier == 87
    assert entry.option.standard is False        # the adjusted root survives
    assert entry.option.root == "XYZ1"
    assert entry.symbol != _XYZ_CALL_OSI         # NOT the same contract as the plain one
    # ...and the ordinary contract still reports the ordinary deliverable.
    plain = _parse_seclist(_stmt(seclist=_XYZ_STOCK + "\n" + _XYZ_CALL))["OPTXYZC150"]
    assert plain.multiplier == "100"


def test_optinfo_links_the_underlying_without_becoming_it():
    """The underlying SECID is recorded, but the SYMBOL stays the contract's.
    Collapsing an option onto its underlying's ticker merges two unrelated
    positions into one -- the destructive path this parser must never take."""
    entry = _parse_seclist(_stmt(seclist=_XYZ_STOCK + "\n" + _XYZ_CALL))["OPTXYZC150"]
    assert entry.underlying_symbol == "XYZ"
    assert entry.underlying_uniqueid == "XYZBASE001"
    assert entry.symbol != "XYZ"


def test_two_contracts_on_one_underlying_stay_distinct():
    put = _optinfo("OPTXYZP120", "XYZ   260117P00120000", "PUT", "120.00", "20260117")
    far = _optinfo("OPTXYZC150F", "XYZ   270115C00150000", "CALL", "150.00", "20270115")
    sec = _parse_seclist(_stmt(seclist="\n".join([_XYZ_STOCK, _XYZ_CALL, put, far])))
    symbols = {sec[u].symbol for u in ("OPTXYZC150", "OPTXYZP120", "OPTXYZC150F")}
    assert len(symbols) == 3            # right and expiry both change the identity


def test_divergent_broker_spellings_canonicalize_to_one_symbol():
    """The same contract spelled three ways is ONE security. Without this, a
    position opened under one spelling can never be closed under another."""
    spellings = [
        _optinfo("OPTA", "XYZ   260117C00150000", "CALL", "150.00", "20260117"),
        _optinfo("OPTB", ".XYZ260117C150", "CALL", "150.00", "20260117"),
        _optinfo("OPTC", "-XYZ260117C150", "call", "150.00", "20260117"),
    ]
    sec = _parse_seclist(_stmt(seclist="\n".join([_XYZ_STOCK] + spellings)))
    assert {sec[u].symbol for u in ("OPTA", "OPTB", "OPTC")} == {_XYZ_CALL_OSI}


def test_optinfo_without_recoverable_terms_keeps_the_brokers_spelling():
    """A feed that states no strike/expiry leaves the contract only partly known.
    The unknown parts stay UNKNOWN and the broker's own text stays the symbol: an
    invented contract is worse than an unparsed one."""
    vague = (
        "<OPTINFO><SECINFO>\n"
        "<SECID><UNIQUEID>OPTLEGACY1</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>\n"
        "<SECNAME>Acme legacy contract</SECNAME><TICKER>XYZAF</TICKER>\n"
        "</SECINFO>\n<OPTTYPE>CALL</OPTTYPE>\n</OPTINFO>"
    )
    entry = _parse_seclist(_stmt(seclist=_XYZ_STOCK + "\n" + vague))["OPTLEGACY1"]
    assert entry.symbol == "XYZAF"          # not guessed into a fake OSI string
    assert entry.is_option                  # still known to BE an option
    assert entry.option is None and entry.partial_option is not None


# --------------------------------------------------------------------------
# (b) BUYOPT / SELLOPT are imported, not dropped
# --------------------------------------------------------------------------
def test_buyopt_and_sellopt_are_no_longer_unmapped():
    txns = (_buyopt("OB1", "OPTXYZC150", "BUYTOOPEN", "2", "3.50", "-700.00")
            + "\n" + _sellopt("OS1", "OPTXYZC150", "SELLTOCLOSE", "-2", "4.25", "850.00"))
    ofx = _stmt(txns, _XYZ_STOCK + "\n" + _XYZ_CALL)
    assert unmapped_investment_actions(ofx) == []     # the audit has nothing to report
    assert set(_rows(txns, _XYZ_STOCK + "\n" + _XYZ_CALL)) == {"OB1", "OS1"}


def test_buytoopen_is_a_buy_of_the_canonical_contract():
    rows = _rows(_buyopt("OB1", "OPTXYZC150", "BUYTOOPEN", "2", "3.50", "-700.00"),
                 _XYZ_STOCK + "\n" + _XYZ_CALL)
    t = rows["OB1"]
    assert t.action == "Buy"
    assert t.symbol == _XYZ_CALL_OSI
    assert t.quantity == "2" and t.price == "3.50"
    assert t.amount_cents == -700_00          # the OFX <TOTAL>, verbatim, in cents
    leg = option_leg(t)
    assert leg.open_close == "open"
    assert leg.right == "C" and leg.strike == "150.00" and leg.expiration == "2026-01-17"
    assert leg.multiplier == "100" and leg.underlying == "XYZ"


def test_selltoopen_writes_a_short_rather_than_selling_units_we_never_held():
    """Writing a contract opens a SHORT. Imported as a plain Sell it would relieve
    units the account never owned and invent a realized gain out of the premium."""
    rows = _rows(_sellopt("OS1", "OPTXYZC150", "SELLTOOPEN", "-3", "2.00", "600.00"),
                 _XYZ_STOCK + "\n" + _XYZ_CALL)
    t = rows["OS1"]
    assert t.action == "ShtSell"
    assert t.quantity == "3"                  # magnitude: direction lives in the action
    assert t.amount_cents == 600_00           # premium received
    assert option_leg(t).open_close == "open"


def test_buytoclose_covers_the_short():
    rows = _rows(_buyopt("OB2", "OPTXYZC150", "BUYTOCLOSE", "3", "0.75", "-225.00"),
                 _XYZ_STOCK + "\n" + _XYZ_CALL)
    t = rows["OB2"]
    assert t.action == "CvrShrt"
    assert option_leg(t).open_close == "close"


def test_selltoclose_is_a_plain_sale():
    rows = _rows(_sellopt("OS2", "OPTXYZC150", "SELLTOCLOSE", "-2", "4.25", "850.00"),
                 _XYZ_STOCK + "\n" + _XYZ_CALL)
    t = rows["OS2"]
    assert t.action == "Sell" and t.quantity == "2"
    assert option_leg(t).open_close == "close"


def test_option_trade_without_a_seclist_still_canonicalizes_its_ticker():
    """No <SECLIST> at all: the row's own TICKER is the only statement of the
    contract, and it still has to land on the one canonical symbol."""
    txn = (
        "<BUYOPT>\n<INVBUY>\n"
        "<INVTRAN><FITID>OB3</FITID><DTTRADE>20260105</DTTRADE></INVTRAN>\n"
        "<SECID><TICKER>.XYZ260117C150</TICKER></SECID>\n"
        "<UNITS>1</UNITS><UNITPRICE>3.50</UNITPRICE><TOTAL>-350.00</TOTAL>\n"
        "</INVBUY>\n<OPTBUYTYPE>BUYTOOPEN</OPTBUYTYPE>\n</BUYOPT>"
    )
    assert _rows(txn)["OB3"].symbol == _XYZ_CALL_OSI


def test_option_trades_land_in_the_database_as_holdings(conn, tmp_path):
    """End to end through importers.import_file: two contracts bought, one sold,
    one contract still held under the canonical symbol."""
    txns = (_buyopt("OB1", "OPTXYZC150", "BUYTOOPEN", "2", "3.50", "-700.00")
            + "\n" + _sellopt("OS1", "OPTXYZC150", "SELLTOCLOSE", "-1", "4.25", "425.00"))
    path = _write(tmp_path, "opt.qfx", _stmt(txns, _XYZ_STOCK + "\n" + _XYZ_CALL))
    importers.import_file(conn, path, account="Broker")
    aid = conn.execute("SELECT id FROM accounts WHERE name='Broker'").fetchone()["id"]
    holds = {h["symbol"]: h for h in investments.list_holdings(conn, aid)}
    assert list(holds) == [_XYZ_CALL_OSI]
    assert holds[_XYZ_CALL_OSI]["quantity"] == "1"
    assert investments.investment_cash(conn, aid) == -275_00   # -700 + 425


# --------------------------------------------------------------------------
# (c) CLOSUREOPT: exercise / assign / expire, with the share leg linked
# --------------------------------------------------------------------------
def test_expire_realizes_the_premium_instead_of_removing_shares():
    """The old behavior turned EVERY closure into a cash-neutral RemoveShares,
    which relieves the position while hiding the gain or loss. An expiry is a
    disposal at a price of ZERO: the whole premium is realized."""
    rows = _rows(_closureopt("C1", "OPTXYZC150", "EXPIRE", "2"),
                 _XYZ_STOCK + "\n" + _XYZ_CALL)
    t = rows["C1"]
    assert t.action != "RemoveShares"
    assert t.action == "Sell"
    assert t.price == "0"
    assert t.quantity == "2" and t.amount_cents == 0
    assert t.symbol == _XYZ_CALL_OSI
    assert option_leg(t).opt_action == "EXPIRE"


def test_expire_of_a_written_contract_covers_the_short():
    """A writer's contract expiring is the GOOD outcome: the short is covered at
    zero and the premium is kept. Negative UNITS is the feed saying 'short'."""
    t = _rows(_closureopt("C2", "OPTXYZC150", "EXPIRE", "-2"),
              _XYZ_STOCK + "\n" + _XYZ_CALL)["C2"]
    assert t.action == "CvrShrt" and t.price == "0" and t.quantity == "2"


def test_assign_covers_the_short_and_links_the_share_leg():
    """Only a writer can be ASSIGNed, so the position is short no matter how the
    units are signed; RemoveShares on a short drives it further negative."""
    t = _rows(_closureopt("C3", "OPTXYZC150", "ASSIGN", "1", relfitid="SH9"),
              _XYZ_STOCK + "\n" + _XYZ_CALL)["C3"]
    assert t.action == "CvrShrt"
    assert t.action != "RemoveShares"
    leg = option_leg(t)
    assert leg.opt_action == "ASSIGN"
    assert leg.related_fitid == "SH9"       # the share leg the assignment produced
    assert leg.open_close == "close"


def test_exercise_is_cash_neutral_because_its_cost_rolls_into_the_share_leg():
    """The one case that stays a cash-neutral relief, deliberately: exercising a
    long produces no result of its own -- the premium becomes part of the basis of
    the shares that arrive on the RELFITID leg."""
    txns = (_closureopt("C4", "OPTXYZC150", "EXERCISE", "1", relfitid="SH1") + "\n"
            + "<BUYSTOCK>\n<INVBUY>\n"
              "<INVTRAN><FITID>SH1</FITID><DTTRADE>20260117</DTTRADE></INVTRAN>\n"
              "<SECID><UNIQUEID>XYZBASE001</UNIQUEID></SECID>\n"
              "<UNITS>100</UNITS><UNITPRICE>150.00</UNITPRICE><TOTAL>-15000.00</TOTAL>\n"
              "</INVBUY>\n</BUYSTOCK>")
    rows = _rows(txns, _XYZ_STOCK + "\n" + _XYZ_CALL)
    opt, shares = rows["C4"], rows["SH1"]
    assert opt.action == "RemoveShares" and opt.amount_cents == 0
    assert option_leg(opt).opt_action == "EXERCISE"
    assert option_leg(opt).related_fitid == shares.fitid
    # The share leg is an ordinary stock row under the UNDERLYING's ticker, and it
    # carries the strike money. The option row is not a second security named XYZ.
    assert shares.symbol == "XYZ" and shares.amount_cents == -15000_00
    assert option_leg(shares) is None
    assert opt.symbol != shares.symbol


def test_closureopt_of_an_unlisted_contract_keeps_its_own_ticker():
    """No SECLIST entry for the contract: the row's TICKER is canonicalized where
    it can be, and never truncated toward the underlying."""
    txn = (
        "<CLOSUREOPT>\n"
        "<INVTRAN><FITID>C5</FITID><DTTRADE>20260117</DTTRADE></INVTRAN>\n"
        "<SECID><TICKER>.XYZ260117C150</TICKER></SECID>\n"
        "<OPTACTION>EXPIRE</OPTACTION><UNITS>1</UNITS>\n"
        "</CLOSUREOPT>"
    )
    assert _rows(txn)["C5"].symbol == _XYZ_CALL_OSI


def test_written_contract_expiring_closes_the_position_in_the_database(conn, tmp_path):
    """Full round trip: write 2 contracts for a 600.00 premium, let them expire,
    and the position is flat with the premium kept as cash."""
    txns = (_sellopt("OS1", "OPTXYZC150", "SELLTOOPEN", "-2", "3.00", "600.00") + "\n"
            + _closureopt("C1", "OPTXYZC150", "EXPIRE", "-2"))
    path = _write(tmp_path, "exp.qfx", _stmt(txns, _XYZ_STOCK + "\n" + _XYZ_CALL))
    importers.import_file(conn, path, account="Broker")
    aid = conn.execute("SELECT id FROM accounts WHERE name='Broker'").fetchone()["id"]
    assert investments.list_holdings(conn, aid) == []
    assert investments.investment_cash(conn, aid) == 600_00
    actions = [r["action"] for r in conn.execute(
        "SELECT action FROM investment_transactions WHERE account_id=? ORDER BY date", (aid,))]
    assert actions == ["ShtSell", "CvrShrt"]
    assert "RemoveShares" not in actions
