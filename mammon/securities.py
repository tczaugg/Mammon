"""mammon.securities -- one identity per security, and a name to show for it.

A security has always had two facts about it and only one column to hold them.
``investment_transactions.symbol`` (and ``holdings``, ``price_history``,
``review_items``, ``holdings_checkpoints``, ``security_mixtures``,
``securities``) stores whatever the source happened to call the thing, so the
same ETF is "VGT VANGUARD INFO TECH ETF" from a 2021 QIF and "VGT" from a 2026
Interactive Brokers CSV -- two securities, two price series, one holding split
in half, and a chart that shows two points because the dividends went to one
name and the prices to the other.

The room was already there and empty: ``securities.name`` and ``holdings.name``
have existed since migration 37 and were never written to, while
``securities.symbol`` -- the primary key -- carried the display name. This
module makes the split real:

* **symbol is the IDENTITY**: the ticker where the security has one, so the key
  a quote provider understands and the key the holding is stored under are the
  same string. That is what stops a fetch filing a price under a name nothing
  uses.
* **name is the DESCRIPTION**: "VANGUARD INFO TECH ETF", shown wherever a person
  reads a security, never used to look anything up.

**A ticker is suggested and confirmed, never derived.** :func:`suggest` proposes
a split; nothing is applied until a caller passes it back. This is not caution
for its own sake -- ``investments.ticker_of`` returns ``FID`` for the plan fund
"FID BALANCED K6" and ``INTL`` for "INTL EQUITY INDEX", and INTL is a real
listed company whose price is already in the file. A blind migration would file
a stranger's prices against a retirement fund and there would be nothing in the
data to say it had happened. Same discipline as ``asset_values`` uses for a
property address and ``investments.ticker_of`` for a quote target.

**A security with no ticker keeps its name as its identity.** A plan's internal
fund ("DOMESTIC BOND INDEX", "TARGET 2030 FUND") has no public ticker and never
will; inventing one would be worse than the problem. Identity is therefore the
ticker when a ticker exists and the full name otherwise, which is heterogeneous
but honest -- and is what the surrogate-key design would replace if this file
ever outgrows it.

Renaming is GLOBAL here, unlike ``investments.apply_security_renames``, which is
scoped to one account because a user fixing a typo means it in the register they
are looking at. Merging two spellings of one security is the opposite: leaving
account 58 on the old spelling is precisely the bug being fixed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mammon import investments

# Every table that keys a security by its text symbol. Kept as data because the
# list has grown twice (security_mixtures in migration 46) and a rename that
# misses one leaves a security half-renamed -- which reads as data loss, since
# the rows that kept the old name simply stop being found.
SYMBOL_TABLES = (
    "investment_transactions",
    "holdings",
    "holdings_checkpoints",
    "price_history",
    "review_items",
    "security_mixtures",
)


@dataclass
class Split:
    """A proposed or confirmed identity for one security.

    ``old`` is what the file currently stores. ``symbol`` is the identity to key
    on, ``name`` the description to show. ``confident`` is False when the ticker
    was guessed from a name that may not contain one -- the caller must show
    those for confirmation rather than applying them."""
    old: str
    symbol: str
    name: Optional[str]
    confident: bool = True

    @property
    def changes_key(self) -> bool:
        return self.symbol != self.old


def _name_after_ticker(symbol: str, ticker: str) -> Optional[str]:
    """The descriptive remainder of ``symbol`` once ``ticker`` is taken off the
    front, or None when nothing is left (the source gave a bare ticker)."""
    rest = str(symbol or "").strip()[len(ticker):].strip()
    return rest or None


def record_master(conn, rows) -> int:
    """Store a source's security master: ``(name, ticker, sec_type)`` triples.

    This is the difference between a recorded fact and a guess. Quicken's
    ``!Type:Security`` block states the ticker outright and omits it for a fund
    that has none, so an import that keeps it removes both the derivation and
    the confirmation step for every security it covers -- 533 of 614 in the
    reference export. Keyed by the name the file currently stores rows under,
    because that is still the identity until a split is applied.

    Nothing is re-keyed here. An import must not silently restate a user's
    securities; it records what the source said, and
    :func:`suggest` reads it back."""
    n = 0
    for name, ticker, sec_type in rows or []:
        name = (name or "").strip()
        if not name:
            continue
        # "" is a RECORDED ABSENCE, not a missing value: Quicken writes a
        # security block with no S field for a fund that has no public ticker,
        # and that statement is the whole reason "INTL EQUITY INDEX" must not be
        # guessed at. NULL would be indistinguishable from never-imported and
        # would send it back to the heuristic that returns INTL.
        ticker = (ticker or "").strip()
        sec_type = (sec_type or "").strip() or None
        # The DESCRIPTION comes free with the pair: Quicken's name is the ticker
        # followed by the descriptive remainder ("VGT VANGUARD INFO TECH ETF"),
        # so the ticker it states is exactly the prefix to remove. Recording
        # only the ticker left `securities.name` NULL, which meant the register
        # and holdings tooltips had nothing to show until a rename was applied
        # -- the description was sitting in the file, derivable, unused.
        desc = None
        if ticker and name.upper().startswith(ticker.upper()):
            desc = _name_after_ticker(name, ticker)
        elif ticker and ticker.upper() != name.upper():
            # Ticker stated but not a prefix of the name: the whole name is the
            # description, since none of it is the ticker.
            desc = name
        # A stated ticker always wins; a recorded absence only fills where
        # nothing is known, so a source silent about the ticker cannot blank one
        # another source supplied.
        conn.execute(
            "INSERT INTO securities(symbol, ticker, name, sec_type) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "ticker=CASE WHEN excluded.ticker <> '' THEN excluded.ticker "
            "            ELSE COALESCE(securities.ticker, excluded.ticker) END, "
            "name=COALESCE(excluded.name, securities.name), "
            "sec_type=COALESCE(excluded.sec_type, securities.sec_type)",
            (name, ticker, desc, sec_type))
        n += 1
    conn.commit()
    return n


def recorded_ticker(conn, symbol: str) -> Optional[str]:
    """What a SOURCE stated about this security's ticker.

    Three-valued on purpose: the ticker, ``""`` when a source recorded the
    security and gave it no ticker, and None when no source has said anything.
    Collapsing the middle case into None is what would send "INTL EQUITY INDEX"
    back to the heuristic."""
    row = conn.execute("SELECT ticker FROM securities WHERE symbol=?",
                       (symbol,)).fetchone()
    return None if row is None else row[0]


def suggest(symbol: str, ticker: Optional[str] = None) -> Split:
    """Propose an identity/description split for one stored security name.

    ``ticker`` is the one a SOURCE stated (``securities.ticker``, from a QIF's
    ``S`` field). When given it is used verbatim and ``confident`` is True: it
    is recorded fact, not derivation, and it settles the cases the heuristic
    gets wrong. Quicken OMITS ``S`` for a fund with no public ticker, so an
    empty string passed deliberately means "this security has none" and the name
    stays the identity -- which is the right answer for "FID BALANCED K6" and
    "INTL EQUITY INDEX", the two the heuristic mangles.

    With no source ticker, the leading token is guessed and ``confident`` is
    False, so the caller must confirm -- see the module docstring. A bare token
    could equally be a ticker already ("FIPDX") or a plan fund's whole name."""
    raw = str(symbol or "").strip()
    if ticker is not None:
        stated = ticker.strip()
        if not stated:
            # The source spoke and said there is no ticker.
            return Split(raw, raw, raw or None, confident=True)
        rest = _name_after_ticker(raw, stated) if \
            raw.upper().startswith(stated.upper()) else raw
        return Split(raw, stated, rest, confident=True)
    ticker = investments.ticker_of(raw)
    if not ticker:
        # No ticker to be had: the name IS the identity, and it is also the name.
        return Split(raw, raw, raw or None, confident=True)
    rest = _name_after_ticker(raw, ticker)
    if rest is None:
        # Already a bare ticker; nothing to split, and no description to invent.
        return Split(raw, ticker, None, confident=True)
    # A GUESS: "VGT VANGUARD INFO TECH ETF" splits correctly and "FID BALANCED
    # K6" does not, and nothing in the string distinguishes them.
    return Split(raw, ticker, rest, confident=False)


def fetch_ticker(conn, symbol: str) -> Optional[str]:
    """The ticker it is SAFE to ask a quote provider about, or None.

    Three rules, in order, and the middle one is the whole subtlety:

    1. A ticker a source stated is used.
    2. A stored name that is a SINGLE ticker-shaped token is its own ticker.
       Nothing is being guessed -- the name is the token -- so "FIPDX" prices
       itself even though Quicken's block for it is bare ``NFIPDX``/``TStock``
       with no ``S`` line. Quicken omitting the symbol on a fund the user
       entered BY its symbol is an incomplete record, not a statement that no
       ticker exists.
    3. Anything else is None. This is where the damage lives: taking the
       leading word of a MULTI-token name gives INTL for "INTL EQUITY INDEX"
       and SP for "SP 500 INDEX PL CL D", both of which a provider will happily
       price -- with a stranger's numbers, against a retirement fund, leaving
       nothing in the data to show it happened.

    So a recorded absence suppresses only the guess, never the safe case.
    """
    raw = str(symbol or "").strip()
    if not raw:
        return None
    stated = recorded_ticker(conn, raw)
    if stated:
        return stated
    if len(raw.split()) == 1 and investments.ticker_of(raw) == raw.upper():
        return raw
    return None


def suggest_all(conn) -> list:
    """A :class:`Split` for every security the file knows, ordered by identity.

    Sourced from the union of the symbol-bearing tables rather than from
    ``securities`` alone, because ``securities`` is populated by the allocation
    feature and holds only what that feature has seen -- 18 rows against 24
    distinct holdings in a real file.

    A ticker RECORDED by an import wins over the derived one, so a security
    whose source stated its ticker needs no guess and no confirmation."""
    stated = {row[0]: row[1] for row in conn.execute(
        "SELECT symbol, ticker FROM securities WHERE ticker IS NOT NULL")}
    seen: dict = {}
    for table in SYMBOL_TABLES:
        for row in conn.execute(
                f"SELECT DISTINCT symbol FROM {table} "
                "WHERE symbol IS NOT NULL AND symbol<>''"):
            sym = row[0]
            if sym not in seen:
                seen[sym] = suggest(sym, stated.get(sym))
    for row in conn.execute(
            "SELECT symbol FROM securities WHERE symbol IS NOT NULL AND symbol<>''"):
        if row[0] not in seen:
            seen[row[0]] = suggest(row[0], stated.get(row[0]))
    return sorted(seen.values(), key=lambda s: (s.symbol.upper(), s.old.upper()))


def usage_counts(conn) -> dict:
    """``{stored symbol: row count}`` across every symbol-bearing table.

    Shown beside a proposed change so the size of what is about to move is
    visible before it moves: "12,988 transactions" and "3 review items" are very
    different confirmations, and a merge that reports two rows when the user
    expected two hundred has found the wrong security."""
    counts: dict = {}
    for table in SYMBOL_TABLES:
        for sym, n in conn.execute(
                f"SELECT symbol, COUNT(*) FROM {table} "
                "WHERE symbol IS NOT NULL AND symbol<>'' GROUP BY symbol"):
            counts[sym] = counts.get(sym, 0) + int(n)
    return counts


def collisions(splits: Iterable[Split]) -> dict:
    """``{identity: [old, ...]}`` for identities more than one security maps to.

    A collision is the WHOLE POINT when it is "VGT" and "VGT VANGUARD INFO TECH
    ETF" -- that is the merge being asked for. It is a disaster when it is two
    genuinely different securities that happen to share a leading token, so the
    caller shows them and the user decides. ``PWE PENN WEST ENERGY TRUST ORD
    SHR`` and ``PWE PENN WEST PETROLEUM LTD`` are one company across a rename
    and DO belong together; a ticker reused by an unrelated issuer does not."""
    by_key: dict = {}
    for s in splits:
        by_key.setdefault(s.symbol, []).append(s.old)
    return {k: sorted(v) for k, v in by_key.items() if len(v) > 1}


def stored_symbols(conn) -> set:
    """Every security identity the file currently stores, across all tables."""
    found = set()
    for table in SYMBOL_TABLES + ("securities",):
        for row in conn.execute(
                f"SELECT DISTINCT symbol FROM {table} "
                "WHERE symbol IS NOT NULL AND symbol<>''"):
            found.add(row[0])
    return found


def merge_preview(conn, splits: Iterable[Split]) -> dict:
    """``{identity: [stored symbol, ...]}`` for identities that will end up
    holding rows from more than one of today's securities.

    :func:`collisions` compares the proposals to each other, which is not
    enough: renaming "VGT VANGUARD INFO TECH ETF" to "VGT" merges it into the
    "VGT" rows ALREADY in the file, and that spelling needs no change of its own
    so it is never among the proposals. Confirming only proposal-vs-proposal
    collisions therefore described a two-way merge as a simple rename -- the one
    change here that re-running cannot undo, reported as the one that can."""
    existing = stored_symbols(conn)
    groups: dict = {}
    for s in splits:
        if not s.old:
            continue
        groups.setdefault(s.symbol, set()).add(s.old)
        if s.changes_key and s.symbol in existing:
            groups[s.symbol].add(s.symbol)
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def apply_splits(conn, splits: Iterable[Split]) -> dict:
    """Apply confirmed splits across every table, globally. Returns a report
    ``{"renamed": n, "named": n, "merged": [identity, ...]}``.

    Order matters and is the reason this is not a loop of UPDATEs:

    1. Rows are re-keyed first, table by table, so nothing is left half-renamed.
    2. ``price_history`` and ``holdings`` carry UNIQUE constraints that a merge
       violates by construction (both spellings may hold 2026-09-04), so the
       merge is done with INSERT OR IGNORE + DELETE rather than UPDATE. The
       SURVIVING row is the one already under the new identity, matching
       ``investments._migrate_price_history``.
    3. ``securities`` is upserted next, since it is keyed by the identity being
       written -- and BEFORE the rebuild, because ``rebuild_holdings`` reads the
       description back out of it to repopulate ``holdings.name``.
    4. Holdings are rebuilt LAST, for every account touched -- a merge changes
       the lot replay itself, so a patched checkpoint would carry a pre-merge
       cost basis forward from every year that already had one.
    """
    report = {"renamed": 0, "named": 0, "merged": []}
    todo = [s for s in splits if s.old]
    if not todo:
        return report
    accounts: set = set()
    for s in todo:
        if s.changes_key:
            for row in conn.execute(
                    "SELECT DISTINCT account_id FROM investment_transactions "
                    "WHERE symbol=?", (s.old,)):
                accounts.add(int(row[0]))

    for s in todo:
        if s.changes_key:
            report["renamed"] += _rekey(conn, s.old, s.symbol)
            report["merged"].append(s.symbol)
        if s.name:
            report["named"] += _set_name(conn, s.symbol, s.name)
    conn.commit()
    for account_id in sorted(accounts):
        investments.rebuild_holdings(conn, account_id)
    report["merged"] = sorted(set(report["merged"]))
    return report


def _rekey(conn, old: str, new: str) -> int:
    """Move every row keyed by ``old`` onto ``new``. Returns rows re-keyed."""
    moved = 0
    # UNIQUE(symbol, date) / UNIQUE(account_id, symbol): copy what does not
    # collide, then drop the old rows. A straight UPDATE raises on the first
    # date both spellings happen to share.
    conn.execute(
        "INSERT OR IGNORE INTO price_history(symbol, date, close_price, source) "
        "SELECT ?, date, close_price, source FROM price_history WHERE symbol=?",
        (new, old))
    moved += conn.execute("DELETE FROM price_history WHERE symbol=?",
                          (old,)).rowcount
    conn.execute(
        "INSERT OR IGNORE INTO holdings(account_id, symbol, name, quantity, cost_basis) "
        "SELECT account_id, ?, name, quantity, cost_basis FROM holdings WHERE symbol=?",
        (new, old))
    moved += conn.execute("DELETE FROM holdings WHERE symbol=?", (old,)).rowcount
    conn.execute(
        "INSERT OR IGNORE INTO security_mixtures(symbol, asset_class, pct, source, as_of) "
        "SELECT ?, asset_class, pct, source, as_of FROM security_mixtures WHERE symbol=?",
        (new, old))
    moved += conn.execute("DELETE FROM security_mixtures WHERE symbol=?",
                          (old,)).rowcount
    # `holdings_checkpoints` is keyed PRIMARY KEY(account_id, year, symbol) and
    # is DERIVED, so it is dropped rather than re-keyed. Renaming a snapshot
    # would collide on the first year both spellings held the security -- and
    # would be wrong even if it did not, because a merged position's year-end
    # state is not either half's state, it is the combined lot replay. The
    # rebuild at the end of apply_splits regenerates these from inception.
    conn.execute("DELETE FROM holdings_checkpoints WHERE symbol=?", (old,))
    # These two have no uniqueness on symbol, so a plain UPDATE is correct.
    for table in ("investment_transactions", "review_items"):
        moved += conn.execute(
            f"UPDATE {table} SET symbol=? WHERE symbol=?", (new, old)).rowcount
    # `securities` is keyed BY the symbol: carry the old row's classification
    # onto the new identity when the new one has none, then drop the old.
    conn.execute(
        "INSERT OR IGNORE INTO securities(symbol, name, sec_type, asset_class) "
        "SELECT ?, name, sec_type, asset_class FROM securities WHERE symbol=?",
        (new, old))
    conn.execute("DELETE FROM securities WHERE symbol=?", (old,))
    return moved


def _set_name(conn, symbol: str, name: str) -> int:
    """Record ``name`` as the description for ``symbol`` in both places that
    hold one. Returns how many rows now carry it."""
    conn.execute(
        "INSERT INTO securities(symbol, name) VALUES (?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name",
        (symbol, name))
    n = conn.execute("UPDATE holdings SET name=? WHERE symbol=?",
                     (name, symbol)).rowcount
    return n + 1


def name_of(conn, symbol: str) -> Optional[str]:
    """The description recorded for ``symbol``, or None."""
    row = conn.execute("SELECT name FROM securities WHERE symbol=?",
                       (symbol,)).fetchone()
    return row[0] if row is not None and row[0] else None


def display(conn, symbol: str) -> str:
    """What a person should read for ``symbol``: ``TICKER -- Description`` when a
    description is recorded, the bare identity otherwise.

    One chokepoint, so the register, the holdings dialog and the reports cannot
    drift into three answers -- the same reason ``ui.models.fmt_date`` exists."""
    sym = str(symbol or "").strip()
    if not sym:
        return ""
    name = name_of(conn, sym)
    return f"{sym} -- {name}" if name and name != sym else sym


def names_by_ticker(conn, symbols: Iterable[str]) -> dict:
    """``{ticker: [stored symbol, ...]}`` for a quote fetch.

    Once identities ARE tickers this is very nearly the identity mapping, which
    is the point: :func:`investments.fetch_quotes` needs to know which stored
    rows a quote should land on, and after the split there is exactly one."""
    out: dict = {}
    for sym in symbols:
        sym = str(sym or "").strip()
        if not sym:
            continue
        tick = investments.ticker_of(sym) or sym
        out.setdefault(tick.upper(), []).append(sym)
    return out
