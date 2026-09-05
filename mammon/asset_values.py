"""mammon.asset_values -- what a non-investment asset is WORTH (SRD 5.8e).

An asset account's ledger balance is its **cost basis**: what was paid, plus the
improvements posted to it. That is exactly the number a capital gain needs and
exactly the wrong number for allocation, leverage or net worth. Until migration
42 it was the only number the file had, so a house bought in 2010 was carried at
its 2010 price against a mortgage that had been amortizing for fifteen years --
and the leverage read far worse than it actually was.

Market value therefore gets its own series (``asset_values``), shaped like
``price_history`` is for securities. Cost basis stays in the ledger, market
value lives here, and nothing conflates them. That separation is the whole point:
Quicken's "Update Account Balance" writes an adjusting transaction instead, which
makes an appreciation indistinguishable from money spent on a new roof and
destroys the basis you need when you sell.

**Fetching is source-injected**, exactly like :func:`mammon.investments.fetch_quotes`:
:func:`fetch_values` takes any object with ``get_values(requests) -> list[AssetValue]``,
so the network lives entirely inside the source and tests hand it a fake.
:class:`ZillowValueSource` is the real one -- a recorded webSlinger script driving
the user's own Chrome profile. Zillow retired its public API in 2021 and the
partner API does not expose a Zestimate, so a recorded script is the only route;
it is also, unlike a datacenter scraper, simply the user's own browser looking at
their own house.

Three rules are load-bearing, and each is a bug that was actually hit:

* **An address is confirmed by a human and stored, never derived.** A name is not
  an address ("Condo (Asset)"); a name that looks like one may be incomplete
  ("240 Birch" needs its city); and a neighbouring account's address is not
  this account's. Inferring "118 Cedar Ln" from a liability of that name
  returned a real, cleanly-extracted $400,000 -- for a house the user sold years
  ago. Same discipline as ``investments.ticker_of``: suggest, confirm, store.
* **A closed account is never valued.** A property account outlives ownership,
  because the history stays after the sale. Walking every asset account would
  have refetched that sold house every quarter, adding a stranger's equity to net
  worth, silently, forever.
* **A failed fetch leaves the last good value standing.** A valuation that comes
  back wrong is far worse than one that errors, so a source that raises, returns
  nothing, or returns an unparseable value writes NOTHING. The previous value
  remains the answer and the failure is reported to the caller.

:func:`exposure` then answers the question a pie cannot: a $400k house against a
$250k mortgage is not a $400k position, it is $150k of equity carrying 2.7x
exposure to the underlying, and a 27% fall in the house takes ~72% of the equity.
The debt comes from ``accounts.secured_by_account_id`` (migration 43), set by the
user on the LOAN -- many loans to one property, since a first and second mortgage
or a loan and its refinance all sit against one house. It is never inferred from
name similarity, for the same reason an address never is.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from mammon import ledger

# The account types that can carry a market value of their own. Investment
# accounts are excluded because they are already valued at market from their
# holdings (`investments.account_valuation`); giving one a second, manual value
# would create two disagreeing answers with no rule for which wins.
VALUABLE_TYPES = ("asset",)


@dataclass
class AssetValue:
    """One dated market value for one account."""
    account_id: int
    date: str
    value_cents: int
    source: Optional[str] = None
    note: Optional[str] = None


@dataclass
class ValueRequest:
    """What a source is asked to value: an account and the address to look it
    up by. The address is always supplied by the caller from stored, confirmed
    data -- a source never sees an account name and never guesses."""
    account_id: int
    address: str


class ValueSourceUnavailable(RuntimeError):
    """No usable valuation backend (no webSlinger client, none given)."""


def _today() -> str:
    return _dt.date.today().isoformat()


def _validate_date(date: str) -> str:
    try:
        _dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError(f"date must be ISO YYYY-MM-DD, got {date!r}")
    return date


def parse_value(raw) -> Optional[int]:
    """A source's raw value -> signed integer cents, or None when it is not a
    number at all.

    Scrapes return strings in whatever shape the page had: ``"871900"``,
    ``"$871,900"``, ``"871,900.00"``. None of those may become a wrong number
    silently, so anything unparseable comes back as None and the caller declines
    to write rather than guessing a zero."""
    if raw is None:
        return None
    if isinstance(raw, bool):            # bool is an int subclass; not a value
        return None
    if isinstance(raw, int):
        return raw * 100
    if isinstance(raw, float):
        return int(round(raw * 100))
    text = str(raw).strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        from decimal import ROUND_HALF_UP, Decimal
        return int((Decimal(cleaned) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# the address on an account
# ---------------------------------------------------------------------------
def get_address(conn, account_id: int) -> Optional[str]:
    """The stored, user-confirmed address, or None. NEVER falls back to the
    account name -- see the module docstring."""
    acct = ledger.get_account(conn, int(account_id))
    if acct is None or "property_address" not in acct.keys():
        return None
    return (acct["property_address"] or None)


def set_address(conn, account_id: int, address) -> None:
    """Record (or clear) the address a valuation source looks this account up
    by. Writes through :mod:`mammon.ledger`, which owns the accounts table."""
    text = (str(address).strip() if address else "") or None
    ledger.update_account(conn, int(account_id), property_address=text)


def suggest_address(name: str) -> str:
    """A STARTING POINT for the address field, from the account name -- offered
    to the user to correct, never used to fetch.

    An account named for its street ("120 Cedar Ln") gives a usable stem; a
    descriptive one ("Condo (Asset)", "House (Asset)") gives nothing worth
    keeping. Either way the user completes it, because neither case can be told
    from the other without knowing what the user meant."""
    text = (name or "").strip()
    # Drop a parenthetical qualifier: "House (Asset)" -> "House".
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    if not re.search(r"\d", text):      # no street number: not an address stem
        return ""
    return text


# ---------------------------------------------------------------------------
# reading and writing the series
# ---------------------------------------------------------------------------
def set_value(conn, account_id: int, date: str, value_cents: int,
              source: Optional[str] = None, note: Optional[str] = None) -> int:
    """Record one dated market value, replacing any value already on that date.
    Returns the row id."""
    _validate_date(date)
    cur = conn.execute(
        "INSERT INTO asset_values(account_id, date, value_cents, source, note) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(account_id, date) DO UPDATE SET "
        "value_cents=excluded.value_cents, source=excluded.source, note=excluded.note",
        (int(account_id), date, int(value_cents), source, note))
    conn.commit()
    row = conn.execute("SELECT id FROM asset_values WHERE account_id=? AND date=?",
                       (int(account_id), date)).fetchone()
    return int(row["id"]) if row is not None else int(cur.lastrowid)


def value_at(conn, account_id: int, as_of: Optional[str] = None) -> Optional[AssetValue]:
    """The newest recorded value on or before ``as_of`` (the newest of all when
    omitted), or None when the account has never been valued.

    On-or-BEFORE, never the nearest: a value recorded today is not evidence of
    what the house was worth in 2012, and letting a later valuation leak
    backwards would rewrite every historical net-worth figure the moment the
    user first clicked Get Value."""
    if as_of is not None:
        _validate_date(as_of)
        row = conn.execute(
            "SELECT * FROM asset_values WHERE account_id=? AND date<=? "
            "ORDER BY date DESC, id DESC LIMIT 1", (int(account_id), as_of)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM asset_values WHERE account_id=? "
            "ORDER BY date DESC, id DESC LIMIT 1", (int(account_id),)).fetchone()
    if row is None:
        return None
    return AssetValue(int(row["account_id"]), row["date"], int(row["value_cents"]),
                      row["source"], row["note"])


def value_history(conn, account_id: int) -> list:
    """Every recorded value for the account, oldest first."""
    return [AssetValue(int(r["account_id"]), r["date"], int(r["value_cents"]),
                       r["source"], r["note"])
            for r in conn.execute(
                "SELECT * FROM asset_values WHERE account_id=? ORDER BY date, id",
                (int(account_id),)).fetchall()]


def delete_value(conn, account_id: int, date: str) -> bool:
    """Remove one dated value. True when a row was removed."""
    cur = conn.execute("DELETE FROM asset_values WHERE account_id=? AND date=?",
                       (int(account_id), date))
    conn.commit()
    return cur.rowcount > 0


def market_value(conn, account_id: int, as_of: Optional[str] = None) -> Optional[int]:
    """The account's market value in cents at ``as_of``, or None when it has no
    recorded value by then. ``None`` means "no opinion" -- callers fall back to
    the ledger balance rather than treating it as zero."""
    v = value_at(conn, account_id, as_of)
    return None if v is None else v.value_cents


# ---------------------------------------------------------------------------
# which accounts can be fetched
# ---------------------------------------------------------------------------
def valuable_accounts(conn, include_unaddressed: bool = False) -> list:
    """The accounts a valuation applies to: OPEN asset accounts.

    Closed accounts are excluded and that exclusion is the point -- a sold house
    keeps its account for the history, and refetching it would add a property
    the user no longer owns to their net worth every quarter. Hidden accounts
    ARE included: hiding says "incomplete records", which is a reason to leave an
    account out of totals (``investments.net_worth``), not a statement that the
    thing was sold.

    Without ``include_unaddressed`` only accounts carrying a confirmed address
    come back, since those are the ones a source can actually look up."""
    out = []
    for a in ledger.list_accounts(conn, include_closed=False, include_hidden=True):
        if (a["type"] or "") not in VALUABLE_TYPES:
            continue
        if not include_unaddressed and not (
                "property_address" in a.keys() and (a["property_address"] or "").strip()):
            continue
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
class ZillowValueSource:
    """Market values from a recorded webSlinger script (default ``GetZEstimate``).

    Zillow retired its public API on 2021-09-30 and the partner API does not
    expose a Zestimate, so a recorded script is the only route to the number.
    It is not a scraper in the usual sense: webSlinger drives the user's own
    Chrome profile and session, so this is the user looking up their own house.

    One script run per address. A run that raises, reports no ``output_data``,
    or hands back an unparseable value yields NO AssetValue for that account --
    the caller then leaves the previous value standing.
    """

    source_name = "zillow"

    def __init__(self, client, script: str = "GetZEstimate",
                 address_field: str = "houseAddress", value_field: str = "zestimate"):
        self.client = client
        self.script = script
        self.address_field = address_field
        self.value_field = value_field

    def _extract(self, result) -> Optional[int]:
        raw = getattr(result, "raw", None) or {}
        out = raw.get("output_data") if isinstance(raw.get("output_data"), dict) else {}
        if self.value_field in out:
            return parse_value(out.get(self.value_field))
        # A script whose output goal was never declared may still surface exactly
        # one extracted field; accept that, but only when it is unambiguous.
        if len(out) == 1:
            return parse_value(next(iter(out.values())))
        return None

    def get_values(self, requests: Iterable[ValueRequest]) -> list:
        found: list = []
        for req in requests:
            try:
                result = self.client.run_script(self.script,
                                                {self.address_field: req.address})
            except Exception:
                continue                      # last good value stands
            cents = self._extract(result)
            if cents is None or cents <= 0:
                continue                      # never write a zero or a guess
            found.append(AssetValue(req.account_id, _today(), cents,
                                    self.source_name, req.address))
        return found


def default_value_source(client=None):
    """The best available valuation backend, or raise if none is configured."""
    if client is None:
        from mammon import webslinger as webslinger_mod
        client = webslinger_mod.default_client()
    if client is None or not client.available():
        raise ValueSourceUnavailable(
            "no webSlinger client is configured; set $MAMMON_WEBSLINGER_MCP_CMD, "
            "record a GetZEstimate script, or enter the value by hand.")
    return ZillowValueSource(client)


@dataclass
class FetchReport:
    """What :func:`fetch_values` did: the values written, and the accounts that
    produced none (so the UI can say WHICH house failed rather than reporting a
    silent partial success)."""
    written: list
    missing: list                 # [(account_id, name, reason)]

    @property
    def ok(self) -> bool:
        return not self.missing


def fetch_values(conn, account_ids=None, source=None, client=None) -> FetchReport:
    """Fetch and record current market values for the given accounts (every
    addressed, open asset account when omitted).

    ``source`` is any object with ``get_values(requests) -> list[AssetValue]``;
    omit it for :func:`default_value_source`. The network lives entirely inside
    the source, so tests inject a fake and never touch it.

    Nothing is written for an account the source declined to value: the previous
    value remains the answer and the account is listed in the report's
    ``missing``. A wrong valuation is worse than a stale one."""
    accounts = valuable_accounts(conn, include_unaddressed=True)
    by_id = {int(a["id"]): a for a in accounts}
    if account_ids is None:
        wanted = [int(a["id"]) for a in accounts]
    else:
        wanted = [int(a) for a in account_ids]

    requests: list = []
    missing: list = []
    for aid in wanted:
        acct = by_id.get(aid)
        if acct is None:
            # Not an open asset account: closed (sold), wrong type, or gone.
            row = ledger.get_account(conn, aid)
            name = row["name"] if row is not None else str(aid)
            reason = ("account is closed -- a sold property is not valued"
                      if row is not None and row["closed_flag"]
                      else "not an open asset account")
            missing.append((aid, name, reason))
            continue
        address = (acct["property_address"] or "").strip() if \
            "property_address" in acct.keys() else ""
        if not address:
            missing.append((aid, acct["name"],
                            "no confirmed address -- set one in Account Details"))
            continue
        requests.append(ValueRequest(aid, address))

    if not requests:
        return FetchReport([], missing)

    if source is None:
        source = default_value_source(client)
    values = list(source.get_values(requests))
    got = {int(v.account_id) for v in values}
    written: list = []
    for v in values:
        set_value(conn, v.account_id, v.date or _today(), v.value_cents,
                  v.source or getattr(source, "source_name", None), v.note)
        written.append(v)
    for req in requests:
        if req.account_id not in got:
            missing.append((req.account_id, by_id[req.account_id]["name"],
                            "the source returned no usable value -- previous "
                            "value left in place"))
    return FetchReport(written, missing)


# ---------------------------------------------------------------------------
# liens: which loan is secured by which asset
# ---------------------------------------------------------------------------
# Account types that can be secured by an asset. A credit card is unsecured by
# definition in this ledger, so offering it the choice would be noise.
SECURABLE_TYPES = ("liability",)


def set_lien(conn, loan_account_id: int, asset_account_id) -> None:
    """Record that ``loan_account_id`` is secured by ``asset_account_id``
    (``None`` clears it).

    Validated rather than trusted: the loan must be a securable account and the
    security must be a valuable (asset) one. A loan secured by a chequing account
    or by itself is not a lien, it is a typo, and it would silently corrupt every
    leverage figure downstream."""
    loan = ledger.get_account(conn, int(loan_account_id))
    if loan is None:
        raise KeyError(f"no account {loan_account_id}")
    if (loan["type"] or "") not in SECURABLE_TYPES:
        raise ValueError(
            f"{loan['name']!r} is a {loan['type']!r} account; only "
            f"{'/'.join(SECURABLE_TYPES)} accounts are secured by an asset")
    if asset_account_id is None:
        ledger.update_account(conn, int(loan_account_id), secured_by_account_id=None)
        return
    asset = ledger.get_account(conn, int(asset_account_id))
    if asset is None:
        raise KeyError(f"no account {asset_account_id}")
    if (asset["type"] or "") not in VALUABLE_TYPES:
        raise ValueError(
            f"{asset['name']!r} is a {asset['type']!r} account; a loan is secured "
            f"by an asset account")
    ledger.update_account(conn, int(loan_account_id),
                          secured_by_account_id=int(asset_account_id))


def lien_of(conn, loan_account_id: int) -> Optional[int]:
    """The asset account id this loan is secured by, or None."""
    loan = ledger.get_account(conn, int(loan_account_id))
    if loan is None or "secured_by_account_id" not in loan.keys():
        return None
    val = loan["secured_by_account_id"]
    return None if val is None else int(val)


def loans_against(conn, asset_account_id: int) -> list:
    """Every liability account secured by this asset, closed ones included.

    A paid-off-and-closed loan contributes a zero balance, so including it costs
    nothing and leaving it out would hide a lien the user can still see."""
    return list(conn.execute(
        "SELECT * FROM accounts WHERE secured_by_account_id=? ORDER BY name",
        (int(asset_account_id),)).fetchall())


def debt_against(conn, asset_account_id: int, as_of: Optional[str] = None) -> int:
    """Total owed against this asset, as POSITIVE cents.

    Liability balances are stored negative (money owed), so the magnitude is what
    a leverage figure wants. A liability that has swung positive -- an overpaid
    loan -- contributes nothing rather than a negative debt, which would
    otherwise report leverage below 1.0 and read as though the house were worth
    more than it is."""
    total = 0
    for loan in loans_against(conn, asset_account_id):
        balance = ledger.account_balance(conn, int(loan["id"]), as_of)
        if balance < 0:
            total += -balance
    return total


# ---------------------------------------------------------------------------
# exposure: what a thing is worth, what is owed against it
# ---------------------------------------------------------------------------
@dataclass
class Exposure:
    """One asset's gross value, the debt secured against it, and the leverage
    that implies."""
    account_id: int
    name: str
    gross: int
    debt: int                     # positive cents owed
    basis: int                    # the ledger balance (cost)

    @property
    def net(self) -> int:
        return self.gross - self.debt

    @property
    def leverage(self) -> Optional[float]:
        """Gross exposure per dollar of equity. ``None`` when equity is zero or
        negative -- a ratio there is not merely large, it is meaningless."""
        return (self.gross / self.net) if self.net > 0 else None


def exposure(conn, account_id: int, as_of: Optional[str] = None,
             debt_cents: Optional[int] = None) -> Exposure:
    """Gross value, debt, net equity and leverage for one asset account.

    Gross is the recorded market value when there is one and the ledger balance
    (cost basis) otherwise -- so an unvalued house still appears, at the only
    number the file has, rather than vanishing from the picture.

    Debt is the sum of the loans SECURED BY this asset
    (:func:`debt_against`), which the user links on each loan. Pass
    ``debt_cents`` to override that with a figure of your own; the links are used
    whenever it is omitted."""
    acct = ledger.get_account(conn, int(account_id))
    if acct is None:
        raise KeyError(f"no account {account_id}")
    basis = ledger.account_balance(conn, int(account_id), as_of)
    mv = market_value(conn, int(account_id), as_of)
    debt = (debt_against(conn, int(account_id), as_of) if debt_cents is None
            else abs(int(debt_cents)))
    return Exposure(int(account_id), acct["name"],
                    basis if mv is None else mv, debt, basis)


def exposure_report(conn, as_of: Optional[str] = None,
                    include_closed: bool = False) -> list:
    """:class:`Exposure` for every asset account, largest gross first.

    Closed accounts are excluded by default for the same reason they are never
    valued: a sold property is not part of what you own."""
    out = []
    for a in ledger.list_accounts(conn, include_closed=include_closed,
                                  include_hidden=True):
        if (a["type"] or "") in VALUABLE_TYPES:
            out.append(exposure(conn, int(a["id"]), as_of))
    return sorted(out, key=lambda e: -e.gross)
