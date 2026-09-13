"""mammon.ledger -- accounts, transactions, transfers, and running balances.

This is the domain layer the GUI sits on top of. It is deliberately UI-free so
it can be unit-tested headless.

Money is signed integer cents everywhere (negative = money out).

Transfers follow Quicken's mirror model: a transfer is TWO linked transactions,
one in each account, equal and opposite. They are cross-linked by
`transfer_pair_id`, and each carries `transfer_account_id` pointing at the other
account. Editing amount/date on one side syncs the other; deleting one deletes
both. The "category" of a transfer is virtual -- it renders as "[Other Account]"
(see ``category_display``), so no real category row is consumed.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from typing import Any, Optional

# Fields a caller may set on a plain transaction via add/update. ``tag`` is
# accepted here for backward compatibility, but it does NOT write the column
# directly: add/update_transaction intercept it and route it through the tag
# junction (see the Tags section below), so the relational store and the
# ``transactions.tag`` cache never diverge.
_TXN_FIELDS = {
    "date", "num", "payee", "category_id", "memo", "tag",
    "amount", "cleared", "reconciled", "fitid", "import_id", "scheduled",
}

# Distinguishes "tag not supplied" from "tag supplied as None/empty (clear it)"
# when a caller passes it through **fields.
_UNSET = object()

# Quicken's marker in the Category field for a split transaction.
SPLIT_LABEL = "--Split--"

# Fields whose change on an ALREADY-reconciled transaction is worth auditing
# (migration 63, table ``reconciled_change_log``). ``amount`` and ``date`` are
# the ones that actually move a reconcile balance; the rest identify the row.
# ``cleared``/``reconciled`` are here because flipping them on a reconciled row
# (un-reconciling, un-clearing) is precisely the kind of quiet change this log
# exists to catch. Internal ids (``fitid``, ``import_id``, ``scheduled``,
# transfer links) are deliberately excluded -- they never affect a reconcile.
_AUDIT_FIELDS = (
    "date", "num", "payee", "category_id", "memo", "amount",
    "cleared", "reconciled",
)
# On a DELETE the whole row goes, so its surviving VALUE fields are what a later
# reconcile lost. The status flags are omitted: a 'delete' entry logged at all
# already means the row was reconciled, so recording reconciled=1 -> None is noise.
_DELETE_AUDIT_FIELDS = ("date", "num", "payee", "category_id", "memo", "amount")


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------
# The account types valued at MARKET rather than at their ledger cash balance,
# and grouped together for net worth, the sidebar and the allocation pie. This
# is the single source of truth: every "is this an investment?" classification
# (valuation, grouping, summing) tests membership here rather than comparing to
# the "investment" literal, so adding a market-valued type is a one-line change
# and nothing silently values it as flat cash. 'crypto' is a DISTINCT type value
# (a coin wallet is not an equity brokerage) but is investment-LIKE for these
# purposes; see mammon/crypto.py.
INVESTMENT_LIKE_TYPES = ("investment", "crypto")


def create_account(
    conn: sqlite3.Connection,
    name: str,
    type: str,
    opening_balance: int = 0,
    opening_date: Optional[str] = None,
    institution: Optional[str] = None,
    note: Optional[str] = None,
    currency: str = "USD",
) -> int:
    """Create an account. ``currency`` is its native ISO 4217 code, chosen at
    creation (the New Account dialog offers it) and treated as an immutable
    account property thereafter -- it is not edited on the account-details dialog.
    A blank/None currency normalises to the base ``'USD'`` (the schema default),
    so a caller passing an empty field never writes an invalid code. This is the
    ONLY insert path for accounts, so it is where currency is set."""
    ccy = (currency or "USD").strip().upper() or "USD"
    cur = conn.execute(
        "INSERT INTO accounts(name, type, opening_balance, opening_date, institution, note, currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, type, opening_balance, opening_date, institution, note, ccy),
    )
    conn.commit()
    return cur.lastrowid


def get_account(conn: sqlite3.Connection, account_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def get_account_by_name(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    """Look up an account by its (unique) name -- used by the download layer to
    find the account an import just created/updated so it can reconcile it."""
    return conn.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()


def list_accounts(conn: sqlite3.Connection, include_closed: bool = False,
                  include_hidden: bool = False) -> list[sqlite3.Row]:
    """Accounts for the left bar (default) or, with the include_* flags, the full
    roster for the Accounts list. ``hidden`` accounts are filtered out by default
    exactly like ``closed`` ones."""
    sql = "SELECT * FROM accounts"
    conds = []
    if not include_closed:
        conds.append("closed_flag=0")
    if not include_hidden:
        conds.append("hidden=0")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY sort_order, name"
    return conn.execute(sql).fetchall()


# --------------------------------------------------------------------------
# Plain transactions
# --------------------------------------------------------------------------
def add_transaction(conn: sqlite3.Connection, account_id: int, date: str, amount: int, **fields: Any) -> int:
    """Insert one ordinary (non-transfer) transaction. Returns its id.

    A ``tag`` keyword is accepted as a single comma-separated string and stored
    through the tag junction (never written to the column directly), so a tag
    set on creation is immediately filterable/reportable like any other."""
    _validate_date(date)
    tag_text = fields.pop("tag", _UNSET)  # -> the junction, not the raw column
    cols = {"account_id": account_id, "date": date, "amount": int(amount)}
    for k, v in fields.items():
        if k not in _TXN_FIELDS:
            raise ValueError(f"unknown transaction field: {k}")
        cols[k] = v
    placeholders = ",".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO transactions({','.join(cols)}) VALUES ({placeholders})",
        tuple(cols.values()),
    )
    txn_id = cur.lastrowid
    if tag_text is not _UNSET:
        _apply_tags(conn, txn_id, parse_tags(tag_text))
    conn.commit()
    _touch_checkpoints(conn, (account_id, int(date[:4])))
    return txn_id


def get_transaction(conn: sqlite3.Connection, txn_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()


def update_transaction(conn: sqlite3.Connection, txn_id: int, **fields: Any) -> None:
    """Update fields on a transaction. If it is one side of a transfer, the
    linked side is kept in sync: `date` mirrors, `amount` mirrors negated,
    `payee` and `memo` mirror verbatim (Quicken keeps the same payee/memo on
    both legs). ``memo`` mirrors even when CLEARED to ``None`` -- clearing a
    transfer's memo on one leg must clear it on the mirror too, never leave the
    old value behind."""
    row = get_transaction(conn, txn_id)
    if row is None:
        raise KeyError(f"no transaction {txn_id}")
    # Tags are stored relationally, not in the column, and are PER-LEG: they are
    # never mirrored onto a transfer's other side. Pull ``tag`` out before the
    # column update so it takes the junction path below.
    tag_text = fields.pop("tag", _UNSET)
    updates = {}
    for k, v in fields.items():
        if k not in _TXN_FIELDS:
            raise ValueError(f"unknown transaction field: {k}")
        if k == "date":
            _validate_date(v)
        if k == "amount":
            v = int(v)
        updates[k] = v
    if not updates and tag_text is _UNSET:
        return
    pair_id = row["transfer_pair_id"]
    pair_row = get_transaction(conn, pair_id) if pair_id is not None else None
    if updates:
        _apply_update(conn, txn_id, updates)

        if pair_id is not None:
            mirror = {}
            if "date" in updates:
                mirror["date"] = updates["date"]
            if "amount" in updates:
                mirror["amount"] = -updates["amount"]
            if "payee" in updates:
                mirror["payee"] = updates["payee"]
            # Mirror the memo verbatim -- INCLUDING an explicit clear (memo=None).
            # Testing "memo in updates" (not truthiness) is what makes clearing the
            # memo propagate to the mirror instead of restoring its old value.
            if "memo" in updates:
                mirror["memo"] = updates["memo"]
            if mirror:
                _apply_update(conn, pair_id, mirror)
    if tag_text is not _UNSET:
        _apply_tags(conn, txn_id, parse_tags(tag_text))
    conn.commit()

    if not updates:
        # A tag-only edit moves no money, so no balance checkpoint is stale.
        return
    # Cascade checkpoints from the earliest year touched on each affected
    # account: the old date, plus the new date when a back-dated edit moved it.
    edits = [(row["account_id"], int(row["date"][:4]))]
    if "date" in updates:
        edits.append((row["account_id"], int(updates["date"][:4])))
    if pair_row is not None:
        edits.append((pair_row["account_id"], int(pair_row["date"][:4])))
        if "date" in updates:
            edits.append((pair_row["account_id"], int(updates["date"][:4])))
    _touch_checkpoints(conn, *edits)


VOID_PREFIX = "**VOID**"


def is_voided(txn) -> bool:
    """True when the row carries Quicken's void mark on its payee."""
    return str((txn["payee"] if txn is not None else "") or "").startswith(VOID_PREFIX)


def void_transaction(conn: sqlite3.Connection, txn_id: int) -> bool:
    """Quicken's Void: keep the row as a record but take it out of the money.

    The amount goes to zero (both legs of a transfer, through the mirror sync),
    the payee gains the ``**VOID**`` prefix the register shows, and the original
    amount is noted in the memo so nothing is lost. Split lines are dropped
    first -- they can no longer sum to anything. Cleared/reconciled flags are
    left alone: a voided check that already cleared stays cleared. Returns
    False (and changes nothing) when the row is already void, so the action is
    idempotent."""
    row = get_transaction(conn, txn_id)
    if row is None:
        raise KeyError(f"no transaction {txn_id}")
    if is_voided(row):
        return False
    if _has_splits(conn, txn_id):
        clear_splits(conn, txn_id)
    was = abs(int(row["amount"]))
    note = f"voided; was {was // 100:,}.{was % 100:02d}"
    memo = (row["memo"] or "").strip()
    payee = (row["payee"] or "").strip()
    update_transaction(conn, txn_id, amount=0,
                       payee=f"{VOID_PREFIX} {payee}".strip(),
                       memo=f"{memo} ({note})" if memo else f"({note})")
    return True


REPLACEABLE_FIELDS = ("payee", "memo", "tag", "num", "category")


def replace_field(conn: sqlite3.Connection, txn_ids, field: str, value) -> tuple[int, int]:
    """Find-and-replace: set one ``field`` to ``value`` on every transaction in
    ``txn_ids``. Text fields take the value verbatim (``""`` clears them);
    ``category`` takes a ``Parent:Child`` path, created if new, and SKIPS
    transfers and splits, whose category is not a free field. Returns
    ``(changed, skipped)``. Transfer legs mirror payee/memo as they always do."""
    if field not in REPLACEABLE_FIELDS:
        raise ValueError(f"cannot replace {field!r}")
    text = (str(value) if value is not None else "").strip()
    changed = skipped = 0
    if field == "category":
        cid = resolve_category(conn, text)
        for tid in txn_ids:
            row = get_transaction(conn, tid)
            if row is None:
                continue
            if row["transfer_account_id"] is not None or _has_splits(conn, tid):
                skipped += 1
                continue
            update_transaction(conn, tid, category_id=cid)
            changed += 1
        return changed, skipped
    for tid in txn_ids:
        if get_transaction(conn, tid) is None:
            continue
        update_transaction(conn, tid, **{field: text or None})
        changed += 1
    return changed, skipped


# --------------------------------------------------------------------------
# Tags (first-class, many-per-transaction)
# --------------------------------------------------------------------------
# A transaction may carry any number of tags. The RELATIONAL store (the ``tags``
# table + the ``transaction_tags`` junction) is authoritative; ``transactions.tag``
# is a normalized, comma-joined CACHE of a row's tag names, so every legacy reader
# -- the register cell, the global Find, the report line loader (reports/_lines.py),
# find-and-replace -- keeps working unchanged while exact per-tag filtering and
# reporting come from the junction.
#
# This module is the SOLE writer of both, and parse/join lives HERE, never in Qt:
# the register edits one comma-separated slot and the domain owns the splitting.
# Tags are per-leg -- a transfer's mirror is never tagged from its other side,
# matching how the legacy single ``tag`` column always behaved.

TAG_SEPARATOR = ", "


def parse_tags(text) -> list[str]:
    """Split the single comma-separated slot the user types into clean names.

    Splits on commas ONLY -- a tag may contain spaces, so ``"Ski Trip"`` is one
    tag, not two. Each name is trimmed; empties are dropped; case-insensitive
    duplicates collapse to the first spelling. ``None``/``""`` -> ``[]``."""
    if text is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in str(text).split(","):
        name = part.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def format_tags(names) -> str:
    """Join tag names back into the one comma-separated slot the register shows."""
    return TAG_SEPARATOR.join(names)


def _tag_id(conn: sqlite3.Connection, name: str) -> int:
    """Resolve a tag name to its id, creating the tag row if new. ``tags.name``
    collates NOCASE, so an existing ``Home`` is reused for ``home``."""
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    return int(conn.execute("INSERT INTO tags(name) VALUES (?)", (name,)).lastrowid)


def tag_id(conn: sqlite3.Connection, name: str) -> Optional[int]:
    """Public get-or-create for a tag name -- the id a SPLIT LEG stores.

    Row-level tags go through :func:`set_tags`, which owns the junction and the
    cache together. A split leg holds a single ``tag_id`` of its own instead, so
    the importer needs this one resolver; a blank name yields ``None`` rather
    than creating an empty tag."""
    name = (name or "").strip()
    return _tag_id(conn, name) if name else None


def get_tags(conn: sqlite3.Connection, txn_id: int) -> list[str]:
    """A transaction's tag names, in the order they were entered."""
    rows = conn.execute(
        "SELECT g.name AS name FROM transaction_tags j "
        "JOIN tags g ON g.id = j.tag_id "
        "WHERE j.transaction_id = ? ORDER BY j.rowid",
        (txn_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def tags_text(conn: sqlite3.Connection, txn_id: int) -> str:
    """A transaction's tags as the single comma-separated string the register
    edits (identical to the cached ``transactions.tag``)."""
    return format_tags(get_tags(conn, txn_id))


def _apply_tags(conn: sqlite3.Connection, txn_id: int, names: list[str]) -> None:
    """Rebuild a transaction's tag links from already-parsed ``names``, then
    recompute the ``transactions.tag`` cache from the stored links so the cache
    always equals the canonical, comma-joined tag names. Junction first, cache
    second: the cache is a projection, never a second source of truth. Does NOT
    commit -- the caller owns the transaction boundary."""
    conn.execute("DELETE FROM transaction_tags WHERE transaction_id = ?", (txn_id,))
    for name in names:
        conn.execute(
            "INSERT OR IGNORE INTO transaction_tags(transaction_id, tag_id) VALUES (?, ?)",
            (txn_id, _tag_id(conn, name)),
        )
    cache = format_tags(get_tags(conn, txn_id)) or None
    conn.execute("UPDATE transactions SET tag = ? WHERE id = ?", (cache, txn_id))


def set_tags(conn: sqlite3.Connection, txn_id: int, names) -> None:
    """Replace a transaction's tags. ``names`` may be an iterable of tag strings,
    the single comma-separated string the register slot holds, or ``None`` to
    clear. Normalizes, writes the junction and the cache, commits. Sole writer of
    tag data; tags are per-leg (a transfer's mirror is untouched)."""
    if get_transaction(conn, txn_id) is None:
        raise KeyError(f"no transaction {txn_id}")
    if names is None:
        clean: list[str] = []
    elif isinstance(names, str):
        clean = parse_tags(names)
    else:
        clean = parse_tags(",".join("" if n is None else str(n) for n in names))
    _apply_tags(conn, txn_id, clean)
    conn.commit()


def set_tags_text(conn: sqlite3.Connection, txn_id: int, text) -> None:
    """Set a transaction's tags from the one comma-separated slot the user typed.
    The register's single tag cell writes through here (thin projection: Qt hands
    over the raw string, the domain parses it)."""
    set_tags(conn, txn_id, text)


def all_tags(conn: sqlite3.Connection) -> list[str]:
    """Every tag in use (attached to at least one transaction), sorted NOCASE."""
    rows = conn.execute(
        "SELECT DISTINCT g.name AS name FROM tags g "
        "JOIN transaction_tags j ON j.tag_id = g.id "
        "ORDER BY g.name COLLATE NOCASE"
    ).fetchall()
    return [r["name"] for r in rows]


def transactions_with_tag(
    conn: sqlite3.Connection, tag: str, account_id: Optional[int] = None
) -> list[dict]:
    """Every transaction carrying ``tag`` (case-insensitive EXACT match), ordered
    by date. Optionally scoped to one account. Each row is the same dict shape as
    a register row. This is the precise per-tag filter the substring Find cannot
    give -- Find also matches payees and memos that happen to contain the word."""
    name = (tag or "").strip()
    if not name:
        return []
    sql = (
        "SELECT t.* FROM transactions t "
        "JOIN transaction_tags j ON j.transaction_id = t.id "
        "JOIN tags g ON g.id = j.tag_id "
        "WHERE g.name = ?"
    )
    params: list[Any] = [name]
    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    sql += " ORDER BY t.date, t.id"
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


# --------------------------------------------------------------------------
# Tag colors and tag management (the Tag Manager's domain verbs)
# --------------------------------------------------------------------------
# A tag carries an optional display color -- a '#RRGGBB' hex string, NULL when
# unchosen (the ``tags.color`` column, added in migration 54). Color is per-tag
# IDENTITY: the register cell, the split dialog and the By Tag report all read it
# through the accessors here, so a tag keeps ONE color everywhere instead of
# color following a chart slice's rank and changing as the ranking moves. This
# module stays the SOLE writer of the tags table -- its ``name``, its ``color``
# and the ``transactions.tag`` cache -- so the Tag Manager holds no SQL and can
# never let the relational store and the cache diverge.

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def normalize_tag_color(color) -> Optional[str]:
    """Validate a tag color to canonical lowercase ``#rrggbb``, or ``None`` to
    clear it. ``None``/``""`` -> ``None``; anything that is not a 6-digit
    ``#RRGGBB`` hex string raises ``ValueError`` (money and colors are both
    validated at the domain boundary, never trusted from the UI)."""
    if color is None:
        return None
    c = str(color).strip()
    if not c:
        return None
    if not _HEX_COLOR_RE.match(c):
        raise ValueError(f"tag color must be '#RRGGBB' hex, got {color!r}")
    return c.lower()


def _refresh_tag_cache(conn: sqlite3.Connection, txn_id: int) -> None:
    """Recompute the ``transactions.tag`` comma-joined cache from the junction
    after a rename/delete changed a name or dropped a link. The junction is
    authoritative; the cache is only its projection (see :func:`_apply_tags`)."""
    cache = format_tags(get_tags(conn, txn_id)) or None
    conn.execute("UPDATE transactions SET tag = ? WHERE id = ?", (cache, txn_id))


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    """Every tag, whether or not it is currently attached, as dicts with ``id``,
    ``name``, ``color`` and a ``usage`` count (transactions carrying it plus
    split legs tagged with it). Sorted by name NOCASE. Powers the Tag Manager,
    which -- unlike :func:`all_tags` -- must show even an unused tag so its color
    can be set before it is applied."""
    rows = conn.execute(
        "SELECT g.id AS id, g.name AS name, g.color AS color, "
        "(SELECT COUNT(*) FROM transaction_tags j WHERE j.tag_id = g.id) "
        "+ (SELECT COUNT(*) FROM splits s WHERE s.tag_id = g.id) AS usage "
        "FROM tags g ORDER BY g.name COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def get_tag(conn: sqlite3.Connection, tag_id: int) -> Optional[dict]:
    """One tag as ``{'id', 'name', 'color'}``, or ``None`` when it is gone."""
    row = conn.execute(
        "SELECT id, name, color FROM tags WHERE id = ?", (tag_id,)).fetchone()
    return dict(row) if row else None


def tag_colors(conn: sqlite3.Connection) -> dict[str, str]:
    """Map CASEFOLDED tag name -> its ``#rrggbb`` color, for the tags that have
    one. Keyed casefolded because ``tags.name`` collates NOCASE, so a renderer
    resolves a parsed tag name straight through ``.get(name.casefold())``. This is
    the single accessor the register cell, split dialog and By Tag report read to
    color a tag, keeping money/colour logic out of ``ui/``."""
    rows = conn.execute(
        "SELECT name, color FROM tags WHERE color IS NOT NULL AND color <> ''"
    ).fetchall()
    return {r["name"].casefold(): r["color"] for r in rows}


def set_tag_color(conn: sqlite3.Connection, tag_id: int, color) -> None:
    """Set (or clear, with ``color=None``) a tag's display color; validated and
    committed. Sole writer of ``tags.color``."""
    if get_tag(conn, tag_id) is None:
        raise KeyError(f"no tag {tag_id}")
    conn.execute("UPDATE tags SET color = ? WHERE id = ?",
                 (normalize_tag_color(color), tag_id))
    conn.commit()


def rename_tag(conn: sqlite3.Connection, tag_id: int, new_name: str) -> None:
    """Rename a tag IN PLACE -- keeping its id, so every junction link, split-leg
    reference and its color survive. Refuses a blank name, a comma (the tag
    separator would split it into two) and a case-insensitive collision with a
    DIFFERENT existing tag. Rebuilds the ``transactions.tag`` cache of every
    transaction carrying it so the register cell shows the new spelling."""
    if get_tag(conn, tag_id) is None:
        raise KeyError(f"no tag {tag_id}")
    name = (new_name or "").strip()
    if not name:
        raise ValueError("a tag needs a name")
    if "," in name:
        raise ValueError("a tag name cannot contain a comma")
    clash = conn.execute(
        "SELECT id FROM tags WHERE name = ? AND id <> ?", (name, tag_id)).fetchone()
    if clash is not None:
        raise ValueError(f"a tag named {name!r} already exists")
    affected = [r["transaction_id"] for r in conn.execute(
        "SELECT transaction_id FROM transaction_tags WHERE tag_id = ?", (tag_id,))]
    conn.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
    for tid in affected:
        _refresh_tag_cache(conn, tid)
    conn.commit()


def delete_tag(conn: sqlite3.Connection, tag_id: int) -> None:
    """Delete a tag. Its junction links cascade away and any split legs pointing
    at it are set NULL (``foreign_keys`` is ON -- see :func:`db.connect`); the
    ``transactions.tag`` cache of every affected transaction is then rebuilt so
    the register cell drops the name."""
    if get_tag(conn, tag_id) is None:
        raise KeyError(f"no tag {tag_id}")
    affected = [r["transaction_id"] for r in conn.execute(
        "SELECT transaction_id FROM transaction_tags WHERE tag_id = ?", (tag_id,))]
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    for tid in affected:
        _refresh_tag_cache(conn, tid)
    conn.commit()


def split_leg_tags_by_txn(conn: sqlite3.Connection,
                          account_id: int) -> dict[int, list[str]]:
    """For one account, the tag names carried by SPLIT LEGS, grouped by their
    parent transaction id: ``{txn_id: [name, ...]}`` in leg order. The register
    cell unions these into the parent row's own tags so a split's per-leg tags
    surface on the collapsed row (matching :func:`reports._lines._line_tags`)
    without a per-row query and without double-counting. Only legs that actually
    carry a tag appear."""
    rows = conn.execute(
        "SELECT s.transaction_id AS tid, g.name AS name "
        "FROM splits s JOIN tags g ON g.id = s.tag_id "
        "JOIN transactions t ON t.id = s.transaction_id "
        "WHERE t.account_id = ? AND s.tag_id IS NOT NULL "
        "ORDER BY s.transaction_id, s.id",
        (account_id,),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["tid"], []).append(r["name"])
    return out


def set_scheduled(conn: sqlite3.Connection, txn_id: int, scheduled: bool) -> None:
    """Flip a transaction between PENDING (``scheduled=1``: a pre-entry the
    reminder generator placed, not yet real) and POSTED, together with every
    row that stands or falls with it: its transfer mirror, and the mirror of
    each transfer split leg. This is the one way a pre-entry becomes real by
    hand -- the register's Enter Pending Payment, marking it cleared, Enter on
    a loan -- so the two sides of a transfer can never disagree about whether
    the money has moved."""
    flag = 1 if scheduled else 0
    row = get_transaction(conn, txn_id)
    if row is None:
        raise KeyError(f"no transaction {txn_id}")
    conn.execute("UPDATE transactions SET scheduled=? WHERE id=?", (flag, txn_id))
    if row["transfer_pair_id"] is not None:
        conn.execute("UPDATE transactions SET scheduled=? WHERE id=?",
                     (flag, row["transfer_pair_id"]))
    conn.execute(
        "UPDATE transactions SET scheduled=? WHERE id IN "
        "(SELECT transfer_pair_id FROM splits WHERE transaction_id=? "
        " AND transfer_pair_id IS NOT NULL)", (flag, txn_id))
    conn.commit()


def delete_transaction(conn: sqlite3.Connection, txn_id: int) -> None:
    """Delete a transaction. If it is one side of a transfer, delete both sides."""
    row = get_transaction(conn, txn_id)
    if row is None:
        return
    # Remove any counter-account mirrors this transaction's transfer split legs
    # created BEFORE deleting it -- the split rows (which remember the mirror
    # ids) cascade away with the parent, so read them first.
    _delete_split_mirrors(conn, txn_id)
    pair_id = row["transfer_pair_id"]
    pair_row = get_transaction(conn, pair_id) if pair_id is not None else None
    # Audit the deletion of a reconciled row (migration 63) BEFORE it is gone --
    # both legs, each scoped to its own account, since deleting one transfer leg
    # deletes the other and either may have been reconciled.
    _log_reconciled_delete(conn, row)
    if pair_row is not None:
        _log_reconciled_delete(conn, pair_row)
    # Deleting this row NULLs the pair's transfer_pair_id via ON DELETE SET NULL,
    # so the second delete is a clean, unlinked delete.
    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    if pair_id is not None:
        conn.execute("DELETE FROM transactions WHERE id=?", (pair_id,))
    conn.commit()

    edits = [(row["account_id"], int(row["date"][:4]))]
    if pair_row is not None:
        edits.append((pair_row["account_id"], int(pair_row["date"][:4])))
    _touch_checkpoints(conn, *edits)


def reconciled_change_log(conn: sqlite3.Connection,
                          account_id: int) -> list[sqlite3.Row]:
    """Read-only: the audit trail of edits and deletions applied to RECONCILED
    transactions in ``account_id``, oldest first (migration 63).

    Each row has ``transaction_id``, ``changed_at`` (ISO), ``operation``
    ('edit'/'delete'), ``field``, ``old_value`` and ``new_value`` (both TEXT;
    amounts are signed cents rendered verbatim -- no money logic here). Written
    only by the edit/delete paths above; nothing else touches the table."""
    return conn.execute(
        "SELECT * FROM reconciled_change_log WHERE account_id=? ORDER BY id",
        (account_id,),
    ).fetchall()


# --------------------------------------------------------------------------
# Transfers (Quicken mirror model)
# --------------------------------------------------------------------------
def create_transfer(
    conn: sqlite3.Connection,
    from_account_id: int,
    to_account_id: int,
    date: str,
    amount: int,
    memo: Optional[str] = None,
    num: Optional[str] = None,
    cleared: int = 0,
    payee: Optional[str] = None,
    reconciled: int = 0,
) -> tuple[int, int]:
    """Move `amount` cents (must be > 0) from one account to another, creating
    both linked mirror transactions atomically. Returns (from_id, to_id).

    Per Quicken convention a transfer keeps a normal payee on BOTH legs (e.g.
    paying a credit card shows payee 'Discover' on both the paying account and
    the card account), so `payee` is written identically to each leg.

    Both legs are stamped with a DEFINITE cleared/reconciled status (never NULL;
    the columns are NOT NULL and both default to 0 = uncleared here). Callers
    that know the real status -- e.g. the QIF importer carrying the Quicken `C`
    flag -- pass it through so an imported transfer's Clr column renders (an
    'R'/'c' glyph) instead of a bare blank. The two legs reconcile INDEPENDENTLY
    against their own accounts' statements, so a caller may afterward adjust one
    leg's status without touching the other."""
    _validate_date(date)
    amount = int(amount)
    if amount <= 0:
        raise ValueError("transfer amount must be a positive number of cents")
    if from_account_id == to_account_id:
        raise ValueError("cannot transfer to the same account")
    # Both accounts must exist (fail loudly rather than orphaning a half-transfer).
    for aid in (from_account_id, to_account_id):
        if get_account(conn, aid) is None:
            raise KeyError(f"no account {aid}")

    payee = payee or None
    from_id = conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, memo, num, cleared, reconciled, transfer_account_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (from_account_id, date, -amount, payee, memo, num, cleared, reconciled, to_account_id),
    ).lastrowid
    to_id = conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, memo, num, cleared, reconciled, transfer_account_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (to_account_id, date, amount, payee, memo, num, cleared, reconciled, from_account_id),
    ).lastrowid
    conn.execute("UPDATE transactions SET transfer_pair_id=? WHERE id=?", (to_id, from_id))
    conn.execute("UPDATE transactions SET transfer_pair_id=? WHERE id=?", (from_id, to_id))
    conn.commit()
    return from_id, to_id


def convert_to_transfer(
    conn: sqlite3.Connection, txn_id: int, target_account_id: int
) -> int:
    """Turn an existing PLAIN transaction into one side of a transfer to
    ``target_account_id``, creating the linked mirror in that account. This is
    what happens when a user sets a transaction's Category to an account
    (Quicken's ``[Account]`` convention).

    The original row keeps its id, date, amount, num, memo and cleared flag --
    so register position, reconciliation and any import linkage survive -- while
    its ``category_id`` is cleared and ``transfer_account_id``/``transfer_pair_id``
    are set. The new mirror carries the opposite (negated) amount and points back.
    Once linked, ordinary :func:`update_transaction`/:func:`delete_transaction`
    keep the two sides in sync. Returns the mirror transaction id.

    Raises if the row is already a transfer, is split, or the target is missing
    or is the transaction's own account."""
    row = get_transaction(conn, txn_id)
    if row is None:
        raise KeyError(f"no transaction {txn_id}")
    if row["transfer_account_id"] is not None:
        raise ValueError("transaction is already a transfer")
    if _has_splits(conn, txn_id):
        raise ValueError("a split transaction cannot be made a plain transfer")
    if get_account(conn, target_account_id) is None:
        raise KeyError(f"no account {target_account_id}")
    if target_account_id == row["account_id"]:
        raise ValueError("cannot transfer to the same account")
    amount = int(row["amount"])
    mirror_id = conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, num, memo, "
        "cleared, transfer_account_id) VALUES (?,?,?,?,?,?,?,?)",
        (target_account_id, row["date"], -amount, row["payee"], row["num"],
         row["memo"], row["cleared"], row["account_id"]),
    ).lastrowid
    conn.execute(
        "UPDATE transactions SET category_id=NULL, transfer_account_id=?, "
        "transfer_pair_id=? WHERE id=?",
        (target_account_id, mirror_id, txn_id),
    )
    conn.execute(
        "UPDATE transactions SET transfer_pair_id=? WHERE id=?", (txn_id, mirror_id)
    )
    conn.commit()
    _touch_checkpoints(conn, (row["account_id"], int(row["date"][:4])),
                       (target_account_id, int(row["date"][:4])))
    return mirror_id


def retarget_transfer(
    conn: sqlite3.Connection, txn_id: int, new_target_account_id: int
) -> int:
    """Re-point an existing transfer at a DIFFERENT account, moving the mirror
    leg atomically -- the fix for "I picked the wrong transfer account and can't
    change it".

    ``txn_id`` is the leg the user is editing; its OWN account is unchanged and
    its ``transfer_account_id`` currently names the account to move away from.
    The counter-leg living in that old account is deleted and an equivalent leg
    is created in ``new_target_account_id`` -- same date, negated amount, payee,
    num, memo and cleared status -- and both legs are re-cross-linked. No leg is
    orphaned and none is duplicated. Returns the new counter-leg's id.

    The editing leg keeps its own id (so register position, reconciliation and
    any import linkage survive); only the mirror moves. If ``date``/``amount``/
    ``payee``/``memo`` were also edited, call :func:`update_transaction` FIRST --
    it syncs both current legs -- then this rebuilds the mirror from the (now
    updated) editing leg, so the new leg carries the edits too.

    Raises ``ValueError`` if the row is not a transfer, is split, is a one-sided
    mirror leg (no counter-leg to move -- e.g. a loan/import mirror whose
    ``transfer_pair_id`` is NULL), or the new target is the transaction's own
    account; ``KeyError`` if the target account does not exist. Re-pointing to
    the SAME account it already targets is a no-op that returns the existing
    counter-leg id."""
    row = get_transaction(conn, txn_id)
    if row is None:
        raise KeyError(f"no transaction {txn_id}")
    if row["transfer_account_id"] is None:
        raise ValueError("transaction is not a transfer")
    if _has_splits(conn, txn_id):
        raise ValueError(
            "a split transfer's account is changed per split line, not as a whole")
    old_pair_id = row["transfer_pair_id"]
    if old_pair_id is None:
        # A one-sided leg (loan/import mirror) has no reciprocal row to move;
        # fabricating one here would corrupt the amortization/mirror model.
        raise ValueError("this transfer leg has no counter-leg to move")
    new_target = int(new_target_account_id)
    if new_target == row["account_id"]:
        raise ValueError("cannot transfer to the same account")
    if get_account(conn, new_target) is None:
        raise KeyError(f"no account {new_target}")
    if new_target == row["transfer_account_id"]:
        return old_pair_id  # already points there -- nothing to move

    old_pair = get_transaction(conn, old_pair_id)
    # Carry the counter-leg's own Clr state and check number onto the moved leg;
    # amount/date/payee/memo come from the EDITING leg so the pair is guaranteed
    # consistent even if the old mirror had drifted. ``reconciled`` resets to 0:
    # the moved leg is new to the target account and has not been reconciled
    # against ITS statement.
    cleared = old_pair["cleared"] if old_pair is not None else row["cleared"]
    num = old_pair["num"] if old_pair is not None else row["num"]
    new_pair_id = conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, num, memo, "
        "cleared, reconciled, transfer_account_id, transfer_pair_id) "
        "VALUES (?,?,?,?,?,?,?,0,?,?)",
        (new_target, row["date"], -int(row["amount"]), row["payee"], num,
         row["memo"], cleared, row["account_id"], txn_id),
    ).lastrowid
    # Re-point the editing leg at the new account + new mirror BEFORE deleting
    # the old mirror, so the editing leg is never momentarily orphaned (and the
    # ON DELETE SET NULL on transfer_pair_id can't null our just-set link -- it
    # now references new_pair_id, a different row than the one being deleted).
    conn.execute(
        "UPDATE transactions SET transfer_account_id=?, transfer_pair_id=? WHERE id=?",
        (new_target, new_pair_id, txn_id),
    )
    _delete_split_mirrors(conn, old_pair_id)  # normally none; belt-and-braces
    conn.execute("DELETE FROM transactions WHERE id=?", (old_pair_id,))
    conn.commit()

    edits = [(row["account_id"], int(row["date"][:4])),
             (new_target, int(row["date"][:4]))]
    if old_pair is not None:
        edits.append((old_pair["account_id"], int(old_pair["date"][:4])))
    _touch_checkpoints(conn, *edits)
    return new_pair_id


# --------------------------------------------------------------------------
# Balances and register rows
# --------------------------------------------------------------------------
def account_balance(conn: sqlite3.Connection, account_id: int, as_of: Optional[str] = None) -> int:
    """End-of-day balance in cents: opening_balance + sum of amounts on/before
    `as_of` (default: all transactions).

    Reads via the year-end ``balance_checkpoints`` cache (previous year's
    snapshot + current-year delta) rather than summing from inception. When no
    checkpoints exist the checkpoint reader degrades to the same full sum, so the
    result is identical either way (see :func:`_account_balance_full`, the
    from-inception oracle the tests compare against)."""
    acct = get_account(conn, account_id)
    if acct is None:
        raise KeyError(f"no account {account_id}")
    if as_of is not None:
        _validate_date(as_of)
    # A sentinel far past any real date makes the checkpoint reader return the
    # grand total (latest snapshot + everything after it) for the as_of=None case.
    return balance_via_checkpoint(conn, account_id, as_of if as_of is not None else "9999-12-31")


def _account_balance_full(conn: sqlite3.Connection, account_id: int, as_of: Optional[str] = None) -> int:
    """From-inception balance: opening_balance + SUM(amount) [through ``as_of``].
    The oracle the checkpoint path must match; kept as the source of truth for
    :func:`rebuild_checkpoints` verification."""
    acct = get_account(conn, account_id)
    if acct is None:
        raise KeyError(f"no account {account_id}")
    if as_of is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE account_id=?",
            (account_id,),
        ).fetchone()
    else:
        _validate_date(as_of)
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE account_id=? AND date<=?",
            (account_id, as_of),
        ).fetchone()
    return acct["opening_balance"] + row[0]


def net_worth(conn: sqlite3.Connection, as_of: Optional[str] = None, *,
              account_ids=None, include_hidden: bool = False) -> int:
    """Total net worth in cents. Investment accounts are valued at market (cash +
    securities), every other account at its ledger balance. The valuation lives in
    :mod:`mammon.investments`; the import is deferred to avoid an import cycle
    (investments imports ledger).

    Hidden accounts are EXCLUDED -- hiding is how the user marks an account whose
    records are incomplete (see :func:`investments.net_worth`). Pass
    ``include_hidden=True`` for the fuller historical picture, which is what the
    report bar's checkbox does. ``account_ids`` restricts to a chosen subset.

    Multi-currency: when accounts span more than one native currency the total is
    each currency's subtotal converted into the base currency and summed, with a
    non-zero foreign balance that has no FX rate left OUT rather than folded in at
    1:1 (see :func:`fx.net_worth_currencies`). A single-currency ledger -- the
    common case -- takes a fast path that delegates straight to
    :func:`investments.net_worth`, so an all-USD file is byte-for-byte unchanged
    and never pays for the currency machinery."""
    from mammon import fx, investments
    if len(fx.account_currencies(conn, include_hidden=include_hidden,
                                 account_ids=account_ids)) > 1:
        return fx.net_worth_currencies(
            conn, as_of, include_hidden=include_hidden,
            account_ids=account_ids).total_cents
    return investments.net_worth(conn, as_of, account_ids=account_ids,
                                 include_hidden=include_hidden)


def transaction_date_bounds(conn: sqlite3.Connection) -> tuple[Optional[str], Optional[str]]:
    """(earliest, latest) transaction date across all accounts as ISO strings,
    or (None, None) when there are no transactions. Lets the reporting UI pick a
    sensible default range without issuing its own SQL."""
    row = conn.execute("SELECT MIN(date), MAX(date) FROM transactions").fetchone()
    return (row[0], row[1]) if row else (None, None)


def latest_activity_date(conn: sqlite3.Connection) -> Optional[str]:
    """The most recent date across ordinary AND investment transactions (ISO), or
    None. Used as the default 'as of' for valuing an investment holding for
    DISPLAY: hold-at-latest-price would value a 1997 snapshot at a 2014 quote, so
    the account bar values investments as of the last activity the ledger knows
    -- which for a live, up-to-date ledger is effectively 'today / current
    price'."""
    row = conn.execute(
        "SELECT MAX(d) FROM (SELECT MAX(date) d FROM transactions "
        "UNION ALL SELECT MAX(date) d FROM investment_transactions)"
    ).fetchone()
    return row[0] if row else None


def set_opening_balance(conn: sqlite3.Connection, account_id: int, cents: int,
                        opening_date: Optional[str] = None) -> None:
    """Set an account's opening balance (and, when given, its opening date)
    and recompute every running balance, since all of them start from it.
    What a QIF "Opening Balance" row means on import."""
    if get_account(conn, account_id) is None:
        raise KeyError(f"no account {account_id}")
    conn.execute("UPDATE accounts SET opening_balance=?, "
                 "opening_date=COALESCE(?, opening_date) WHERE id=?",
                 (int(cents), opening_date, account_id))
    conn.commit()
    rebuild_checkpoints(conn, account_id)


def update_account(conn: sqlite3.Connection, account_id: int, **fields: Any) -> None:
    """Update editable account details (name, type, native currency, institution,
    note, closed_flag, the online-banking url/account_number, the hidden flag, and
    the webSlinger download_script + download_config). Used by the account-details
    dialog and the accounts list. ``mammon.fx.set_account_currency`` routes through
    here so the accounts table keeps a single writer."""
    allowed = {"name", "type", "currency", "institution", "note", "closed_flag",
               "sort_order", "url", "account_number", "hidden", "download_script",
               "lot_method", "download_config", "cutover_date", "asset_class",
               "property_address", "secured_by_account_id", "crypto_kind"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown account field(s): {sorted(bad)}")
    if not updates:
        return
    assignments = ",".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE accounts SET {assignments} WHERE id=?",
                 (*updates.values(), account_id))
    conn.commit()


def set_account_hidden(conn: sqlite3.Connection, account_id: int, hidden: bool) -> None:
    """Hide (or unhide) an account. A hidden account keeps all its data and stays
    reachable from the Accounts list, but drops off the left account bar."""
    update_account(conn, account_id, hidden=1 if hidden else 0)


def account_cutover_date(conn: sqlite3.Connection, account_id: int) -> Optional[str]:
    """The account's migration cutover (last-migrated) watermark, or None.

    Migrated (QIF) rows carry no fitid, so the first live OFX/QFX pull cannot
    dedup the overlap by fitid. This ISO date is the newest migrated transaction
    date; import rows dated on/before it are already in migrated history and are
    skipped, preventing duplicates across the migration seam (gap G4)."""
    row = conn.execute(
        "SELECT cutover_date FROM accounts WHERE id=?", (account_id,)
    ).fetchone()
    return row["cutover_date"] if row and row["cutover_date"] else None


def set_account_cutover_date(conn: sqlite3.Connection, account_id: int,
                             cutover_date: str) -> None:
    """Record/advance the migration cutover watermark for an account. Monotonic:
    an existing watermark is never moved earlier, so re-running (or partially
    re-running) a migration cannot shrink the guarded window."""
    if not cutover_date:
        return
    existing = account_cutover_date(conn, account_id)
    if existing and cutover_date <= existing:
        return
    update_account(conn, account_id, cutover_date=cutover_date)


def account_max_dates_for_import(conn: sqlite3.Connection,
                                 import_id: int) -> dict[int, str]:
    """Newest transaction date per account among rows inserted by one import,
    across both cash and investment ledgers. Used to compute a migration's
    per-account cutover watermark from exactly the rows it wrote."""
    rows = conn.execute(
        "SELECT account_id, MAX(date) AS d FROM ("
        "  SELECT account_id, date FROM transactions WHERE import_id=?"
        "  UNION ALL"
        "  SELECT account_id, date FROM investment_transactions WHERE import_id=?"
        ") GROUP BY account_id",
        (import_id, import_id),
    ).fetchall()
    return {r["account_id"]: r["d"] for r in rows if r["d"]}


def register_rows(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    """Return the account's transactions in ledger order (date, then insertion
    order) with a running `balance` on each, starting from opening_balance."""
    acct = get_account(conn, account_id)
    if acct is None:
        raise KeyError(f"no account {account_id}")
    rows = conn.execute(
        "SELECT * FROM transactions WHERE account_id=? ORDER BY date, id",
        (account_id,),
    ).fetchall()
    running = acct["opening_balance"]
    out = []
    for r in rows:
        running += r["amount"]
        d = dict(r)
        d["balance"] = running
        d["is_split"] = _has_splits(conn, r["id"])
        d["category_label"] = category_display(conn, r)
        # Signed cents still parked in an uncategorized split line (0 when the
        # transaction is not split or is fully categorized). The register paints a
        # warning triangle before '--Split--' when this is nonzero.
        d["uncat_split"] = (
            uncategorized_split_amount(conn, r["id"]) if d["is_split"] else 0)
        out.append(d)
    return out


def category_display(conn: sqlite3.Connection, txn: sqlite3.Row) -> str:
    """classic category label. A split transaction shows as '--Split--',
    a plain transfer shows as '[Other Account]', otherwise the category path.

    Splits are checked FIRST: a transaction that is simultaneously a transfer
    (``transfer_account_id`` set) AND carries split lines -- e.g. an imported
    mortgage payment whose QIF split has a ``[House]`` principal leg -- is really
    a multi-leg split, and must read as ``--Split--`` so both legs surface, not
    as the lone bracketed transfer account (which hid the split and broke
    reconciliation against the counter-account's mirror)."""
    if _has_splits(conn, txn["id"]):
        return SPLIT_LABEL
    if txn["transfer_account_id"] is not None:
        other = get_account(conn, txn["transfer_account_id"])
        return f"[{other['name']}]" if other else "[Transfer]"
    if txn["category_id"] is not None:
        return _category_path(conn, txn["category_id"])
    return ""


# --------------------------------------------------------------------------
# Splits (divide one transaction across multiple categories)
# --------------------------------------------------------------------------
def _has_splits(conn: sqlite3.Connection, txn_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM splits WHERE transaction_id=? LIMIT 1", (txn_id,)
    ).fetchone() is not None


def has_splits(conn: sqlite3.Connection, txn_id: int) -> bool:
    """True when the transaction is divided across multiple category lines."""
    return _has_splits(conn, txn_id)


def get_splits(conn: sqlite3.Connection, txn_id: int) -> list[dict]:
    """The transaction's split lines in insertion order. Each carries its
    ``category_id`` (or ``transfer_account_id`` when the leg is a transfer to
    another account), signed ``amount`` cents, ``memo``, and a display
    ``category_label`` -- the ``Parent:Child`` category path, or ``[Account]``
    for a transfer leg -- plus the leg's own ``tag`` name (``""`` when
    untagged). Empty list when the transaction is not split."""
    rows = conn.execute(
        "SELECT s.id, s.category_id, s.transfer_account_id, s.transfer_pair_id, "
        "s.amount, s.memo, s.tag_id, g.name AS tag "
        "FROM splits s LEFT JOIN tags g ON g.id = s.tag_id "
        "WHERE s.transaction_id=? ORDER BY s.id", (txn_id,),
    ).fetchall()
    out = []
    for r in rows:
        if r["transfer_account_id"] is not None:
            other = get_account(conn, r["transfer_account_id"])
            label = f"[{other['name']}]" if other else "[Transfer]"
        elif r["category_id"] is not None:
            label = _category_path(conn, r["category_id"])
        else:
            label = ""
        out.append({
            "id": r["id"],
            "category_id": r["category_id"],
            "transfer_account_id": r["transfer_account_id"],
            "amount": r["amount"],
            "memo": r["memo"] or "",
            "category_label": label,
            "tag_id": r["tag_id"],
            "tag": r["tag"] or "",
        })
    return out


def previous_split_for_payee(conn: sqlite3.Connection, payee,
                             exclude_txn_id=None) -> list[dict]:
    """Split lines (same shape as :func:`get_splits`) of the most recent OTHER
    transaction sharing this exact ``payee`` that is itself split, or ``[]``
    when there is none. Powers the split dialog's "Copy from previous <payee>
    split" button, which re-enters a recurring paycheck/bill's many-row
    breakdown in one click. An empty/None payee never matches."""
    if not payee:
        return []
    row = conn.execute(
        "SELECT t.id FROM transactions t "
        "WHERE t.payee = ? AND t.id != COALESCE(?, -1) "
        "AND EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id) "
        "ORDER BY t.date DESC, t.id DESC LIMIT 1",
        (payee, exclude_txn_id),
    ).fetchone()
    return get_splits(conn, row["id"]) if row else []


def set_splits(conn: sqlite3.Connection, txn_id: int, lines) -> None:
    """Replace the transaction's split lines atomically.

    ``lines`` is an iterable of ``(category_id, amount_cents, memo)`` tuples --
    or dicts with keys ``category_id``/``amount``/``memo``. A leg may instead
    carry ``transfer_account_id`` (dict form) to post that leg as a TRANSFER to
    another account (Quicken's ``[Account]`` split leg -- a mortgage principal
    leg to the house account, a paycheck 401(k) deferral to the retirement
    account): a mirror transaction is created in that account and linked back
    from the split row. The split amounts (signed cents) must sum to the
    transaction's own ``amount`` -- Quicken's invariant that keeps the register
    total and the by-category spending report in agreement. Rather than REJECT a
    mismatch (which trapped the user when the real total was wrong), any signed
    difference is absorbed into an uncategorized split line so the split always
    reconciles; the register surfaces a leftover uncategorized amount with a
    warning triangle.
    A plain whole-transaction transfer CAN be split: the transfer stops being a
    property of the ROW and becomes one LINE of the split. Selling crypto for
    859.70 and receiving 843.09 in checking is one transaction whose money moved
    two ways -- 843.09 to ``[Checking]`` and 16.61 to a fee category -- and the
    old hard block ("a transfer cannot be split") left the user no way to record
    it. Splitting a transfer therefore ADOPTS the existing mirror onto the
    matching leg: the parent's own ``transfer_account_id``/``transfer_pair_id``
    are cleared, the counter-account row is kept (same id, date, payee, register
    position, cleared/reconciled status) and re-amounted to the negated LINE, and
    the split row remembers it via ``splits.transfer_pair_id`` like any other
    transfer leg. The invariant becomes: the mirror equals the transfer LINE, not
    the row total. The split must therefore contain a line still targeting the
    old transfer account -- otherwise it would orphan the counter-leg, and that
    is rejected. :func:`clear_splits` with ``restore_transfer_to`` is the exact
    inverse (what undo uses).
    A transfer that ALREADY carries split lines is re-split the same way: an
    imported mortgage payment is legitimately BOTH a transfer to ``[House]`` AND
    a principal+interest split, and editing its legs must round-trip while the
    parent's ``transfer_account_id`` link is preserved. A split needs at least
    two lines (one line is just a plain category). Applying a split NULLs the
    transaction's own ``category_id`` so its Category field displays
    ``--Split--``.
    """
    txn = get_transaction(conn, txn_id)
    if txn is None:
        raise KeyError(f"no transaction {txn_id}")
    parent_taid = txn["transfer_account_id"]
    # Only a PLAIN transfer (no splits yet) moves its transfer off the row and
    # onto a line. A transfer that already carries splits keeps the parent link
    # exactly as before -- that is the imported-mortgage shape.
    split_a_transfer = parent_taid is not None and not _has_splits(conn, txn_id)
    # Each normalized leg is (category_id, transfer_account_id, amount, memo,
    # tag_id). A leg may carry its own single tag (Quicken tags a split leg to
    # attribute part of a payment to a project) as ``tag_id`` (already resolved)
    # or ``tag`` (a name, get-or-created here). Threading it through set_splits is
    # what lets an edited/re-saved split KEEP a per-leg tag an import wrote,
    # instead of silently dropping it on the delete+recreate rebuild below.
    norm = []
    for ln in lines:
        cat, taid, amt, memo = _split_line(ln)
        tg = None
        if isinstance(ln, dict):
            if ln.get("tag_id") not in (None, ""):
                tg = int(ln["tag_id"])
            elif ln.get("tag"):
                tg = tag_id(conn, ln["tag"])
        norm.append((cat, taid, amt, memo, tg))
    total = sum(amt for _cat, _taid, amt, _memo, _tg in norm)
    target = int(txn["amount"])
    diff = target - total
    if diff != 0:
        # Balance the split instead of blocking the save: absorb the signed
        # difference between the lines and the transaction total into an
        # UNCATEGORIZED line (no category, no transfer target) so the split
        # always reconciles. the user can finish categorizing later; the register
        # flags the leftover with a warning triangle (uncategorized_split_amount).
        # A transfer leg (transfer_account_id set) is NEVER touched -- only a
        # true uncategorized line absorbs the difference. Fold into an existing
        # uncategorized line if there is one; otherwise append a fresh one.
        idx = next((i for i, (c, t, _a, _m, _tg) in enumerate(norm)
                    if c is None and t is None), None)
        if idx is None:
            norm.append((None, None, diff, None, None))
        else:
            c, t, a, m, tg = norm[idx]
            norm[idx] = (c, t, a + diff, m, tg)
    if len(norm) < 2:
        raise ValueError("a split needs at least two lines")
    # Snapshot each existing transfer-split mirror's own per-account
    # cleared/reconciled BEFORE the delete+recreate rebuild, then restore it onto
    # the matching new mirror below. Without this, editing ANY line of a split
    # that contains a transfer leg (e.g. a mortgage principal leg to [House], a
    # paycheck 401(k) deferral to retirement) silently drops that leg back to
    # uncleared -- even when the leg had been reconciled ('R') against the
    # counter-account's statement. Each leg reconciles independently, so its
    # status must survive a rebuild triggered by an unrelated edit. Legs are
    # matched by target account, exact leg amount preferred, so an unchanged leg
    # AND an edited-amount leg (e.g. a loan principal paydown) both keep status.
    # Splitting a plain transfer: which leg inherits the existing mirror? The
    # first leg still pointing at the old transfer account. Without one the
    # counter-account row would be orphaned (money that arrived in checking with
    # nothing on this side claiming it), so refuse rather than silently delete
    # the other half of the user's transfer.
    adopt_idx = None
    adopt_pair = None
    if split_a_transfer:
        adopt_idx = next((i for i, (_c, t, _a, _m, _tg) in enumerate(norm)
                          if t == parent_taid), None)
        if adopt_idx is None:
            acct = get_account(conn, parent_taid)
            name = acct["name"] if acct else str(parent_taid)
            raise ValueError(
                f"splitting this transfer needs one line that still transfers "
                f"to [{name}] -- the other side of the transfer lives there")
        adopt_pair = txn["transfer_pair_id"]
    preserved = _capture_split_mirror_flags(conn, txn_id)
    _delete_split_mirrors(conn, txn_id)
    conn.execute("DELETE FROM splits WHERE transaction_id=?", (txn_id,))
    if split_a_transfer:
        # The transfer is a property of the LINE now, not the row.
        conn.execute(
            "UPDATE transactions SET transfer_account_id=NULL, "
            "transfer_pair_id=NULL WHERE id=?", (txn_id,))
    for i, (cat, taid, amt, memo, tg) in enumerate(norm):
        pair_id = None
        if i == adopt_idx:
            # Adopt, do not recreate: keeping the counter row's id preserves its
            # reconcile status, its own edits and anything referencing it. A
            # one-sided parent (transfer_pair_id NULL -- itself the mirror of
            # someone else's leg) yields a one-sided leg: never fabricate a
            # second counter row. The mirror drops its back-link because a split
            # leg's mirror is one-sided by construction (_create_split_mirror).
            pair_id = adopt_pair
            if pair_id is not None:
                conn.execute(
                    "UPDATE transactions SET amount=?, transfer_pair_id=NULL "
                    "WHERE id=?", (-int(amt), pair_id))
        elif taid is not None:
            pair_id = _create_split_mirror(conn, txn, taid, amt)
            flag = _take_preserved_flag(preserved, taid, amt)
            if flag and (flag[0] or flag[1]):
                conn.execute(
                    "UPDATE transactions SET cleared=?, reconciled=? WHERE id=?",
                    (flag[0], flag[1], pair_id),
                )
        conn.execute(
            "INSERT INTO splits(transaction_id, category_id, transfer_account_id, "
            "transfer_pair_id, amount, memo, tag_id) VALUES (?,?,?,?,?,?,?)",
            (txn_id, cat, taid, pair_id, amt, memo, tg),
        )
    conn.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (txn_id,))
    conn.commit()


def rebalance_splits(conn: sqlite3.Connection, txn_id: int) -> None:
    """Re-assert the split invariant after the transaction total changed: rewrite
    the existing split lines so they sum to the transaction's CURRENT amount,
    absorbing the signed difference into an uncategorized line (transfer legs
    untouched -- see :func:`set_splits`). No-op when the transaction is not
    split. Used when the user edits a split transaction's total on the register row or
    in the edit dialog: the total moves, the categorized/transfer lines stay put,
    and the leftover lands in 'uncategorized' for him to finish later."""
    if not _has_splits(conn, txn_id):
        return
    lines = [
        {"category_id": s["category_id"],
         "transfer_account_id": s["transfer_account_id"],
         "amount": s["amount"], "memo": s["memo"], "tag_id": s["tag_id"]}
        for s in get_splits(conn, txn_id)
    ]
    set_splits(conn, txn_id, lines)


def set_transaction_amount(conn: sqlite3.Connection, txn_id: int,
                           new_amount: int) -> None:
    """Set a transaction's total (signed cents). For a SPLIT transaction the
    signed difference between the new total and the current split sum is absorbed
    into an uncategorized split line so the split still reconciles (transfer legs
    untouched). A plain or transfer transaction just has its amount updated -- a
    transfer mirrors the negated amount to its pair as usual. This replaces the
    old hard block that stopped the user from correcting a split transaction's total."""
    update_transaction(conn, txn_id, amount=int(new_amount))
    rebalance_splits(conn, txn_id)


def uncategorized_split_amount(conn: sqlite3.Connection, txn_id: int) -> int:
    """Signed cents sitting in the transaction's UNCATEGORIZED split lines (no
    category and no transfer target). Nonzero means the split still holds money
    the user has not assigned to a category -- the register paints a warning triangle
    in front of '--Split--' when this is nonzero. Returns 0 when the transaction
    is not split or is fully categorized."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS amt FROM splits "
        "WHERE transaction_id=? AND category_id IS NULL "
        "AND transfer_account_id IS NULL",
        (txn_id,),
    ).fetchone()
    return int(row["amt"]) if row else 0


def clear_splits(conn: sqlite3.Connection, txn_id: int,
                 restore_transfer_to: int | None = None) -> None:
    """Remove all split lines, reverting the transaction to a plain one. Its
    ``category_id`` is left NULL for the caller to reassign. Any mirror
    transactions the transfer legs created are removed too.

    ``restore_transfer_to`` is the exact inverse of :func:`set_splits`'s adoption
    of a transfer into a split line: given the account the row used to transfer
    to, the leg pointing there is handed BACK to the row -- its mirror survives
    (same id, same reconcile status), gets re-amounted to the negated ROW total
    and is cross-linked as a normal two-sided transfer again. Undo uses this to
    put a split-a-transfer edit back; every other caller (loan payments, import
    review, the split dialog's Remove) leaves it None and gets the old behavior,
    because a loan payment's principal leg to ``[Mortgage]`` must NOT be promoted
    into a whole-row transfer."""
    keep = None
    txn = get_transaction(conn, txn_id)
    if (restore_transfer_to is not None and txn is not None
            and txn["transfer_account_id"] is None):
        restore_transfer_to = int(restore_transfer_to)
        row = conn.execute(
            "SELECT transfer_pair_id FROM splits WHERE transaction_id=? "
            "AND transfer_account_id=? AND transfer_pair_id IS NOT NULL "
            "ORDER BY id LIMIT 1", (txn_id, restore_transfer_to)).fetchone()
        if row is not None:
            keep = int(row["transfer_pair_id"])
    else:
        restore_transfer_to = None
    _delete_split_mirrors(conn, txn_id, skip_id=keep)
    conn.execute("DELETE FROM splits WHERE transaction_id=?", (txn_id,))
    if restore_transfer_to is not None:
        conn.execute(
            "UPDATE transactions SET transfer_account_id=?, transfer_pair_id=? "
            "WHERE id=?", (restore_transfer_to, keep, txn_id))
        if keep is not None:
            conn.execute(
                "UPDATE transactions SET amount=?, transfer_account_id=?, "
                "transfer_pair_id=? WHERE id=?",
                (-int(txn["amount"]), txn["account_id"], txn_id, keep))
    conn.commit()


def _split_line(ln):
    """Normalize a split line to ``(category_id, transfer_account_id, amount,
    memo)``. ``amount`` is required; ``category_id`` may be None (Uncategorized).
    A dict may carry ``transfer_account_id`` to make the leg a transfer -- which
    is mutually exclusive with a category (the transfer wins). Tuples keep the
    legacy ``(category_id, amount, memo)`` shape (never a transfer). ``memo`` is
    trimmed to None when blank."""
    if isinstance(ln, dict):
        cat = ln.get("category_id")
        taid = ln.get("transfer_account_id")
        amt = ln.get("amount")
        memo = ln.get("memo")
    else:
        cat, amt, memo = (list(ln) + [None, None, None])[:3]
        taid = None
    if amt is None:
        raise ValueError("a split line needs an amount")
    cat = None if cat in (None, "") else int(cat)
    taid = None if taid in (None, "") else int(taid)
    if taid is not None:
        cat = None                          # a transfer leg carries no category
    memo = str(memo).strip() or None if memo not in (None, "") else None
    return cat, taid, int(amt), memo


def _row_get(row, key, default=None):
    """Read ``key`` from a sqlite3.Row or dict, tolerating its absence."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _create_split_mirror(conn: sqlite3.Connection, txn: sqlite3.Row,
                         target_account_id: int, leg_amount: int) -> int:
    """Create the counter-account mirror for a transfer split leg and return its
    id. The mirror is a one-sided transfer leg (its own ``transfer_pair_id``
    stays NULL, exactly like an asymmetric imported leg, so update/delete of the
    mirror never tries to sync back through the split parent); the split row is
    what remembers the mirror, via ``splits.transfer_pair_id``."""
    if get_account(conn, target_account_id) is None:
        raise KeyError(f"no account {target_account_id}")
    if target_account_id == txn["account_id"]:
        raise ValueError("a split leg cannot transfer to its own account")
    # The mirror belongs to whatever created the parent -- an import, or the
    # register. Carrying the parent's import_id keeps it inside that run for
    # anything that asks what a run wrote (the opening-balance adoption reads
    # "does this account hold anything from BEFORE this import").
    return conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, num, memo, "
        "transfer_account_id, import_id) VALUES (?,?,?,?,?,?,?,?)",
        (target_account_id, txn["date"], -int(leg_amount), txn["payee"],
         txn["num"], txn["memo"], txn["account_id"], _row_get(txn, "import_id")),
    ).lastrowid


def _delete_split_mirrors(conn: sqlite3.Connection, txn_id: int,
                          skip_id: int | None = None) -> None:
    """Delete the counter-account mirror transactions created by ``txn_id``'s
    transfer split legs (called before replacing or clearing its splits, and
    when the parent transaction is deleted). ``skip_id`` spares one mirror the
    caller is about to re-use rather than recreate -- see
    :func:`clear_splits`'s ``restore_transfer_to``."""
    for r in conn.execute(
        "SELECT transfer_pair_id FROM splits "
        "WHERE transaction_id=? AND transfer_pair_id IS NOT NULL", (txn_id,),
    ).fetchall():
        if skip_id is not None and int(r["transfer_pair_id"]) == int(skip_id):
            continue
        conn.execute("DELETE FROM transactions WHERE id=?", (r["transfer_pair_id"],))


def _capture_split_mirror_flags(conn: sqlite3.Connection, txn_id: int) -> dict:
    """Snapshot the per-account cleared/reconciled of every transfer-split mirror
    of ``txn_id``, grouped by the leg's ``transfer_account_id``. :func:`set_splits`
    uses this to RESTORE each leg's own reconcile status across its delete+recreate
    rebuild, so editing one line of a split never silently drops a DIFFERENT
    transfer leg that had been reconciled ('R') in its counter-account. Each bucket
    holds ``(mirror_amount, cleared, reconciled)`` tuples; the mirror stores the
    NEGATED leg amount, matched back in :func:`_take_preserved_flag`."""
    out: dict = {}
    for r in conn.execute(
        "SELECT s.transfer_account_id AS taid, t.amount AS amount, "
        "t.cleared AS cleared, t.reconciled AS reconciled "
        "FROM splits s JOIN transactions t ON t.id = s.transfer_pair_id "
        "WHERE s.transaction_id=? AND s.transfer_pair_id IS NOT NULL",
        (txn_id,),
    ).fetchall():
        out.setdefault(r["taid"], []).append(
            (int(r["amount"]), int(r["cleared"]), int(r["reconciled"])))
    return out


def _take_preserved_flag(preserved: dict, target_account_id: int,
                         leg_amount: int):
    """Pop the captured ``(cleared, reconciled)`` for a freshly recreated split
    mirror to ``target_account_id`` with leg amount ``leg_amount``, preferring an
    exact amount match (the mirror carries the NEGATED leg amount) so an unchanged
    leg keeps its status; otherwise the first remaining leg to that account, which
    covers an edited amount (e.g. a loan principal paydown). Returns ``None`` when
    that account has no remaining captured leg."""
    bucket = preserved.get(target_account_id)
    if not bucket:
        return None
    want = -int(leg_amount)
    idx = next((i for i, (a, _c, _r) in enumerate(bucket) if a == want), None)
    if idx is None:
        idx = 0
    _a, cleared, reconciled = bucket.pop(idx)
    return cleared, reconciled


def backfill_split_transfer_mirrors(conn: sqlite3.Connection,
                                    target_account_id=None) -> dict:
    """Repair split legs that carry a ``transfer_account_id`` but no
    ``transfer_pair_id`` -- i.e. a transfer leg with no counter-account mirror.

    The UI/``set_splits`` path always mirrors such a leg; the *importer* does
    NOT (``importers.core._insert_split`` writes the leg one-sided, expecting the
    counter-account's own imported file to supply the reciprocal leg -- see the
    QIF split-transfer tests). When that counter file is never imported (a 401(k),
    loan or asset account only ever referenced from a paycheck/mortgage split),
    the target account is left with no legs at all. This backfills them.

    Idempotent: only orphaned legs (``transfer_pair_id IS NULL``) are considered,
    so a re-run is a no-op. Before fabricating a mirror it ADOPTS an existing,
    unclaimed matching leg already in the target account (same date, opposite
    amount, pointing back at the parent's account) -- so it never duplicates a
    reciprocal leg that a separate import already provided.

    ``target_account_id`` limits the repair to legs transferring INTO that one
    account; ``None`` repairs every account. Returns ``{"created", "adopted",
    "skipped"}`` counts.
    """
    where = "s.transfer_account_id IS NOT NULL AND s.transfer_pair_id IS NULL"
    params: list = []
    if target_account_id is not None:
        where += " AND s.transfer_account_id = ?"
        params.append(int(target_account_id))
    orphans = conn.execute(
        f"SELECT s.id AS split_id, s.transaction_id, s.transfer_account_id, "
        f"s.amount FROM splits s WHERE {where} ORDER BY s.transaction_id, s.id",
        params,
    ).fetchall()
    summary = {"created": 0, "adopted": 0, "skipped": 0}
    for o in orphans:
        txn = get_transaction(conn, o["transaction_id"])
        target = o["transfer_account_id"]
        # A leg whose parent is gone, that targets its own account, or names a
        # missing account cannot form a valid mirror -- leave it untouched.
        if (txn is None or target == txn["account_id"]
                or get_account(conn, target) is None):
            summary["skipped"] += 1
            continue
        existing = conn.execute(
            "SELECT id FROM transactions "
            "WHERE account_id=? AND date=? AND amount=? AND transfer_account_id=? "
            "AND transfer_pair_id IS NULL "
            "AND id NOT IN (SELECT transfer_pair_id FROM splits "
            "               WHERE transfer_pair_id IS NOT NULL) "
            "ORDER BY id LIMIT 1",
            (target, txn["date"], -int(o["amount"]), txn["account_id"]),
        ).fetchone()
        if existing is not None:
            pair_id = existing["id"]
            summary["adopted"] += 1
        else:
            pair_id = _create_split_mirror(conn, txn, target, o["amount"])
            summary["created"] += 1
        conn.execute("UPDATE splits SET transfer_pair_id=? WHERE id=?",
                     (pair_id, o["split_id"]))
    conn.commit()
    return summary


# --------------------------------------------------------------------------
# Reconciliation (reconcile an account against a bank statement)
# --------------------------------------------------------------------------
def reconcile_summary(
    conn: sqlite3.Connection,
    account_id: int,
    statement_balance: int,
    through_date: Optional[str] = None,
    beginning_balance: Optional[int] = None,
) -> dict:
    """Reconcile math for an account against a target statement ending balance.

    Quicken's model: the BEGINNING balance is everything already reconciled
    (opening balance + the sum of reconciled transactions). You then check off
    the transactions that appear on this statement (marking them ``cleared``);
    the CLEARED balance is the beginning balance plus those cleared-but-not-yet-
    reconciled amounts. When the cleared balance equals the statement's ending
    balance the DIFFERENCE is zero and the reconciliation can be finished.

    ``through_date`` bounds the cleared sum to rows dated ON OR BEFORE it, which
    is what a statement actually covers -- a cleared row dated after the closing
    date belongs to the NEXT statement, not this one. It also keeps this math in
    step with the reconcile dialog, which shows only rows through that date: an
    unbounded sum counted cleared rows the user could neither see nor unmark, so
    the difference would not close and 'Clear All' could not rescue it. Left
    None the sum is unbounded (every cleared row in the account).

    ``beginning_balance`` REPLACES the opening+reconciled figure. A credit-card
    statement is self-contained -- its own numbers imply what was owed at the
    start -- and that implied figure is the one to reconcile against. Deriving
    the beginning from the account's R rows instead couples the reconcile to
    however that history happens to be flagged: a card whose Quicken history
    arrived already carrying R showed thousands of dollars of it as the starting
    point, and no amount of checking or unchecking items in the window could move
    it. Quicken does not consult the register for this, and neither do we.

    Returns a dict of integer cents:
      beginning_balance  opening + already-reconciled, or the override
      cleared_total      sum of cleared-not-reconciled transactions in range --
                         i.e. exactly the items checked off in the reconcile
                         window, which is what the dialog labels "Cleared
                         balance". Nothing else is folded in.
      cleared_balance    beginning_balance + cleared_total; the projected balance
                         the statement's ending figure is compared against
      cleared_after      sum of cleared rows EXCLUDED by through_date (0 if none)
      statement_balance  the target (echoed back as int)
      difference         statement_balance - cleared_balance (0 => balanced)
    """
    acct = get_account(conn, account_id)
    if acct is None:
        raise KeyError(f"no account {account_id}")
    reconciled = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions "
        "WHERE account_id=? AND reconciled=1", (account_id,),
    ).fetchone()[0]
    cutoff = (through_date or "").strip()
    where = "account_id=? AND cleared=1 AND reconciled=0"
    args: list = [account_id]
    if cutoff:
        where += " AND date<=?"
        args.append(cutoff)
    cleared = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE {where}",
        tuple(args),
    ).fetchone()[0]
    after = 0
    if cutoff:
        after = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions "
            "WHERE account_id=? AND cleared=1 AND reconciled=0 AND date>?",
            (account_id, cutoff),
        ).fetchone()[0]
    beginning = (acct["opening_balance"] + reconciled
                 if beginning_balance is None else int(beginning_balance))
    cleared_balance = beginning + cleared
    stmt = int(statement_balance)
    return {
        "beginning_balance": beginning,
        "cleared_total": cleared,
        "cleared_balance": cleared_balance,
        "cleared_after": after,
        "statement_balance": stmt,
        "difference": stmt - cleared_balance,
    }


def implied_card_beginning(charges: int, payments: int, credits: int,
                          finance: int, ending: int) -> int:
    """The balance a CREDIT-CARD statement implies was owed when it opened, in
    register sign (owed => negative). Inputs are the figures as they read on the
    paper statement, all POSITIVE.

    A card statement is closed arithmetic::

        previous + charges + finance - payments - credits = ending

    so the previous balance is recoverable from the other five. That is the whole
    point: the reconcile needs no beginning balance typed by the user and none
    read out of the register. Quicken does not ask for one on a card, and it does
    not check the implied figure against the account's reconciled history -- if
    the two disagree the statement wins, because the statement is the document
    being reconciled. Asking the register instead is what made a card carrying
    imported Quicken R flags open its reconcile thousands of dollars adrift.

    The finance charge is subtracted alongside charges: it is a charge, listed on
    its own statement line rather than inside the charges total.
    """
    owed_at_start = (int(ending) - int(charges) - int(finance)
                     + int(payments) + int(credits))
    return -owed_at_start


def unreconciled_rows(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    """Register rows not yet reconciled -- the candidates a reconcile pass checks
    off -- in ledger order, each carrying its ``cleared`` flag so the dialog can
    show and toggle it. Already-reconciled rows are locked and excluded."""
    return [r for r in register_rows(conn, account_id) if not r["reconciled"]]


def finish_reconciliation(
    conn: sqlite3.Connection,
    account_id: int,
    statement_date: str,
    statement_balance: int,
    note: Optional[str] = None,
    beginning_balance: Optional[int] = None,
) -> int:
    """Lock in a reconciliation: every cleared-but-not-reconciled transaction
    dated ON OR BEFORE the statement date is marked ``reconciled`` and a
    ``reconciliations`` row is recorded. Refuses unless the cleared balance
    already equals the statement balance (the difference must be zero). Any
    in-progress draft for the account is dropped. Returns the reconciliation id.

    The statement date bounds BOTH the guard and the update, using the same
    cutoff, so the set that made the difference zero is exactly the set that gets
    locked. A cleared row dated after the closing date stays cleared and unlocked
    for the next statement -- reconciling January must not silently reconcile a
    March payment that happens to be checked off.

    ``beginning_balance`` is passed through to the guard, so a credit card is
    judged against the beginning its own statement implies rather than against
    the account's R rows (see :func:`reconcile_summary`)."""
    _validate_date(statement_date)
    summary = reconcile_summary(conn, account_id, statement_balance,
                                statement_date, beginning_balance)
    if summary["difference"] != 0:
        raise ValueError(
            f"cannot finish reconciliation: off by {summary['difference']} cents "
            f"(clear items until the difference is zero)")
    conn.execute(
        "UPDATE transactions SET reconciled=1 "
        "WHERE account_id=? AND cleared=1 AND reconciled=0 AND date<=?",
        (account_id, statement_date),
    )
    cur = conn.execute(
        "INSERT INTO reconciliations(account_id, statement_date, statement_balance, note) "
        "VALUES (?,?,?,?)",
        (account_id, statement_date, int(statement_balance), note),
    )
    conn.execute("DELETE FROM reconcile_drafts WHERE account_id=?", (account_id,))
    conn.commit()
    return cur.lastrowid


def last_reconciliation(conn: sqlite3.Connection, account_id: int) -> Optional[sqlite3.Row]:
    """The most recent reconciliations row for an account, or None -- lets the
    reconcile dialog default the statement date and show prior progress."""
    return conn.execute(
        "SELECT * FROM reconciliations WHERE account_id=? "
        "ORDER BY statement_date DESC, id DESC LIMIT 1", (account_id,),
    ).fetchone()


# ---- in-progress reconcile (the statement inputs, kept across a close) -------
_DRAFT_FIELDS = (
    "statement_date", "beginning_cents", "ending_cents", "charges_cents",
    "payments_cents", "credits_cents", "finance_cents", "finance_category",
    "finance_txn_id",
)


def get_reconcile_draft(conn: sqlite3.Connection, account_id: int) -> Optional[dict]:
    """The saved statement inputs of an UNFINISHED reconcile for this account, or
    None. Reopening a reconcile restores these instead of re-prompting, so
    stepping out to the register to check something does not cost the user the
    whole statement retyped."""
    row = conn.execute(
        "SELECT * FROM reconcile_drafts WHERE account_id=?", (account_id,),
    ).fetchone()
    return {k: row[k] for k in _DRAFT_FIELDS} if row is not None else None


def save_reconcile_draft(conn: sqlite3.Connection, account_id: int, **fields) -> None:
    """Create or update the in-progress statement inputs for an account. Only the
    named fields are written; the rest keep their stored values. Unknown keys
    raise rather than being dropped on the floor."""
    bad = set(fields) - set(_DRAFT_FIELDS)
    if bad:
        raise ValueError(f"unknown reconcile draft field(s): {sorted(bad)}")
    conn.execute(
        "INSERT OR IGNORE INTO reconcile_drafts(account_id) VALUES (?)", (account_id,))
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE reconcile_drafts SET {sets}, updated_at=datetime('now') "
            "WHERE account_id=?",
            (*fields.values(), account_id),
        )
    conn.commit()


def clear_reconcile_draft(conn: sqlite3.Connection, account_id: int) -> None:
    """Discard an account's in-progress reconcile inputs. Called when a
    reconciliation is finished, and when the user abandons one outright."""
    conn.execute("DELETE FROM reconcile_drafts WHERE account_id=?", (account_id,))
    conn.commit()


# --------------------------------------------------------------------------
# Search (within an account and globally)
# --------------------------------------------------------------------------
def search_transactions(
    conn: sqlite3.Connection,
    query: str,
    account_id: Optional[int] = None,
) -> list[dict]:
    """Find transactions whose text fields or amount contain ``query``
    (case-insensitive substring). Scoped to one account when ``account_id`` is
    given, else across EVERY account (the global find). Results are ordered
    newest-first and each carries ``account_id``, ``account_name`` and
    ``category_label`` so the Find UI can render account-labelled rows.

    For cash transactions matching spans payee, memo, tag, num, the category
    label, the account name, the date (both ISO and US MM/DD/YYYY), and
    the amount (plain and comma-grouped, e.g. ``1,234.56``). Investment
    transactions are searched too: their security symbol and name (the name from
    the account's ``holdings`` row when present), the action, memo, account name,
    date and amount. Keeping the rules here means the search is unit-tested
    headless and the GUI holds no SQL."""
    q = (query or "").strip().lower()
    if not q:
        return []
    names: dict[int, str] = {}

    def _name(aid: int) -> str:
        if aid not in names:
            a = get_account(conn, aid)
            names[aid] = a["name"] if a else ""
        return names[aid]

    out: list[dict] = []

    # -- cash transactions -------------------------------------------------
    if account_id is not None:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE account_id=? ORDER BY date DESC, id DESC",
            (account_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY date DESC, id DESC"
        ).fetchall()
    for r in rows:
        aid = r["account_id"]
        cat = category_display(conn, r)
        if _txn_matches(r, cat, _name(aid), q):
            out.append({
                "id": r["id"],
                "account_id": aid,
                "account_name": _name(aid),
                "date": r["date"],
                "num": r["num"] or "",
                "payee": r["payee"] or "",
                "category_label": cat,
                "tag": r["tag"] or "",
                "memo": r["memo"] or "",
                "amount": r["amount"],
                "symbol": "",
                "security_name": "",
                "is_investment": False,
            })

    # -- investment transactions (security symbol/name, amount, memo) ------
    # LEFT JOIN holdings so a symbol that is still held also matches on its
    # human-readable name; symbols long fully sold (no holdings row) still match
    # by ticker. holdings.name is NULL unless an importer set it, so name search
    # is best-effort while symbol/amount/memo always work.
    isql = (
        "SELECT it.*, h.name AS security_name "
        "FROM investment_transactions it "
        "LEFT JOIN holdings h "
        "  ON h.account_id = it.account_id AND h.symbol = it.symbol "
    )
    if account_id is not None:
        irows = conn.execute(
            isql + "WHERE it.account_id=? ORDER BY it.date DESC, it.id DESC",
            (account_id,),
        ).fetchall()
    else:
        irows = conn.execute(
            isql + "ORDER BY it.date DESC, it.id DESC"
        ).fetchall()
    for r in irows:
        if not _inv_txn_matches(r, _name(r["account_id"]), q):
            continue
        aid = r["account_id"]
        sym = r["symbol"] or ""
        sec_name = r["security_name"] or ""
        action = r["action"] or ""
        if sym and sec_name:
            label = f"{sym} — {sec_name}"
        else:
            label = sym or sec_name or action
        out.append({
            "id": r["id"],
            "account_id": aid,
            "account_name": _name(aid),
            "date": r["date"],
            "num": action,
            "payee": label,
            "category_label": action,
            "tag": "",
            "memo": r["memo"] or "",
            "amount": int(r["amount"] or 0),
            "symbol": sym,
            "security_name": sec_name,
            "is_investment": True,
        })

    # Newest-first across BOTH tables. A stable sort keeps same-date cash rows in
    # their id-desc order (and ahead of same-date investment rows).
    out.sort(key=lambda d: d["date"] or "", reverse=True)
    return out


def _txn_matches(r: sqlite3.Row, category_label: str, account_name: str, q: str) -> bool:
    cents = int(r["amount"] or 0)
    whole, frac = divmod(abs(cents), 100)
    iso = r["date"] or ""
    parts = iso.split("-")
    us = f"{parts[1]}/{parts[2]}/{parts[0]}" if len(parts) == 3 else iso
    haystack = " ".join(str(x) for x in (
        r["payee"] or "", r["memo"] or "", r["tag"] or "", r["num"] or "",
        category_label, account_name, iso, us,
        f"{whole}.{frac:02d}", f"{whole:,}.{frac:02d}",
    )).lower()
    return q in haystack


def _inv_txn_matches(r: sqlite3.Row, account_name: str, q: str) -> bool:
    """Substring match for an investment transaction row (which carries a joined
    ``security_name``). Mirrors :func:`_txn_matches`: covers the security symbol
    and name, the action, memo, account name, date and amount (cents rendered as
    dollars, plain and comma-grouped)."""
    cents = int(r["amount"] or 0)
    whole, frac = divmod(abs(cents), 100)
    iso = r["date"] or ""
    parts = iso.split("-")
    us = f"{parts[1]}/{parts[2]}/{parts[0]}" if len(parts) == 3 else iso
    haystack = " ".join(str(x) for x in (
        r["symbol"] or "", r["security_name"] or "", r["action"] or "",
        r["memo"] or "", account_name, iso, us,
        f"{whole}.{frac:02d}", f"{whole:,}.{frac:02d}",
    )).lower()
    return q in haystack


# --------------------------------------------------------------------------
# Category / payee helpers (read + get-or-create) for the UI and importers
# --------------------------------------------------------------------------
def list_categories(conn: sqlite3.Connection, include_hidden: bool = False) -> list[dict]:
    """Every category as {'id', 'path'} with its hierarchical 'Parent:Child'
    path, sorted case-insensitively by path. Feeds the register's category
    picker so the GUI never issues its own SQL."""
    sql = "SELECT id FROM categories"
    if not include_hidden:
        sql += " WHERE hidden=0"
    rows = conn.execute(sql).fetchall()
    out = [{"id": r["id"], "path": _category_path(conn, r["id"])} for r in rows]
    out.sort(key=lambda d: d["path"].lower())
    return out


def category_children(conn: sqlite3.Connection, parent_id: Optional[int],
                      include_hidden: bool = False) -> list[dict]:
    """Direct child categories of ``parent_id`` (``None`` = the top level) as
    ``{'id', 'name'}``, sorted case-insensitively by name. Feeds the report
    category-tree picker's drill-down so the GUI issues no SQL of its own."""
    if parent_id is None:
        sql = "SELECT id, name FROM categories WHERE parent_id IS NULL"
        params: tuple = ()
    else:
        sql = "SELECT id, name FROM categories WHERE parent_id=?"
        params = (int(parent_id),)
    if not include_hidden:
        sql += " AND hidden=0"
    rows = conn.execute(sql, params).fetchall()
    out = [{"id": int(r["id"]), "name": r["name"]} for r in rows]
    out.sort(key=lambda d: d["name"].lower())
    return out


def category_transactions(conn: sqlite3.Connection, category_id: int) -> list[dict]:
    """Every transaction posting to ``category_id`` -- plain rows
    (``transactions.category_id``) and split lines (``splits.category_id``,
    whose parent transaction carries a NULL category) -- as
    ``{'date', 'payee', 'amount', 'account'}`` newest first. Feeds the report
    category-tree picker's leaf drill-down, so the GUI issues no SQL itself."""
    cid = int(category_id)
    out: list[dict] = []
    for r in conn.execute(
        "SELECT t.date AS date, t.payee AS payee, t.amount AS amount, "
        "a.name AS account FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id WHERE t.category_id=?", (cid,),
    ).fetchall():
        out.append({"date": r["date"], "payee": r["payee"],
                    "amount": int(r["amount"]), "account": r["account"]})
    for r in conn.execute(
        "SELECT t.date AS date, t.payee AS payee, s.amount AS amount, "
        "a.name AS account FROM splits s "
        "JOIN transactions t ON t.id = s.transaction_id "
        "JOIN accounts a ON a.id = t.account_id WHERE s.category_id=?", (cid,),
    ).fetchall():
        out.append({"date": r["date"], "payee": r["payee"],
                    "amount": int(r["amount"]), "account": r["account"]})
    out.sort(key=lambda d: (d["date"] or ""), reverse=True)
    return out


def resolve_category(conn: sqlite3.Connection, path: Optional[str]) -> Optional[int]:
    """Get-or-create a category id from a 'Parent:Child' path, creating each
    missing level. Returns None for a blank path. This is the single writer of
    category rows, shared by the importers and the register UI."""
    path = (path or "").strip()
    if not path:
        return None
    parent = None
    for part in (p.strip() for p in path.split(":") if p.strip()):
        # Match existing levels CASE-INSENSITIVELY so typing a capitalization
        # variant ("business" when "Business" exists) reuses the existing
        # category instead of forking a near-duplicate (feature parity).
        if parent is None:
            row = conn.execute(
                "SELECT id FROM categories WHERE name=? COLLATE NOCASE "
                "AND parent_id IS NULL", (part,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM categories WHERE name=? COLLATE NOCASE "
                "AND parent_id=?", (part, parent)
            ).fetchone()
        if row is not None:
            parent = row["id"]
        else:
            parent = conn.execute(
                "INSERT INTO categories(name, parent_id) VALUES (?,?)", (part, parent)
            ).lastrowid
    conn.commit()
    return parent


def create_category(conn: sqlite3.Connection, path: Optional[str]) -> Optional[int]:
    """Create (get-or-create) a category from a 'Parent:Child' path for the
    management UI's Add action. A thin, intention-revealing alias for
    :func:`resolve_category` -- adding a category that already exists is a
    no-op that returns the existing id (the UNIQUE(name, parent_id) invariant
    guarantees we never fork a duplicate). Returns None for a blank path."""
    return resolve_category(conn, path)


def count_category_usage(conn: sqlite3.Connection, category_id: int) -> int:
    """How many ledger rows still point at ``category_id`` -- plain transactions
    plus split lines. The management UI checks this before a delete so it can
    demand a replacement category rather than orphan those rows (ON DELETE SET
    NULL would silently uncategorize them otherwise)."""
    txns = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id=?", (category_id,)
    ).fetchone()[0]
    splits = conn.execute(
        "SELECT COUNT(*) FROM splits WHERE category_id=?", (category_id,)
    ).fetchone()[0]
    return int(txns) + int(splits)


def reassign_category(conn: sqlite3.Connection, from_id: int, to_id: int) -> int:
    """Move every transaction and split line from ``from_id`` onto ``to_id``.
    Returns the number of ledger rows repointed. Used by the delete-with-reassign
    path so no transaction is orphaned onto a category that is about to vanish.
    Commits."""
    if int(from_id) == int(to_id):
        return 0
    n = conn.execute(
        "UPDATE transactions SET category_id=? WHERE category_id=?",
        (to_id, from_id),
    ).rowcount
    n += conn.execute(
        "UPDATE splits SET category_id=? WHERE category_id=?",
        (to_id, from_id),
    ).rowcount
    conn.commit()
    return int(n)


def delete_category(conn: sqlite3.Connection, category_id: int, *,
                    replacement_id: Optional[int] = None) -> int:
    """Delete ``category_id``. If ``replacement_id`` is given, every transaction
    and split line on the doomed category is first reassigned to it (returns the
    count reassigned); otherwise the category is deleted outright and any stray
    references fall back to NULL via ON DELETE SET NULL. Child categories are
    reparented to top level by the same FK rule, and learned category_rules
    referencing it cascade away. Commits."""
    reassigned = 0
    if replacement_id is not None:
        reassigned = reassign_category(conn, category_id, replacement_id)
    conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    conn.commit()
    return reassigned


def rename_category(conn: sqlite3.Connection, category_id: int, new_name: str) -> None:
    """Rename a category IN PLACE, keeping its id -- and therefore every learned
    rule, budget line, import mapping and scheduled payment already pointing at
    it. (Delete-and-recreate would forfeit the id and silently break those
    links, which is exactly why the management UI must never take that route.)

    ``new_name`` is a single level, never a path: a ':' is rejected because that
    is the ``Parent:Child`` separator, so a rename cannot smuggle in a reparent.
    Raises ``ValueError`` on a blank name, a ':' in the name, or a
    case-insensitive collision with an existing sibling under the same parent
    (the ``UNIQUE(name, parent_id)`` invariant, matched CASE-INSENSITIVELY the
    same way :func:`resolve_category` does). Commits."""
    name = (new_name or "").strip()
    if not name:
        raise ValueError("A category name is required.")
    if ":" in name:
        raise ValueError("A category name cannot contain ':' (the path separator).")
    row = conn.execute(
        "SELECT parent_id FROM categories WHERE id=?", (int(category_id),)
    ).fetchone()
    if row is None:
        raise KeyError(f"no category {category_id}")
    dup = conn.execute(
        "SELECT id FROM categories WHERE name=? COLLATE NOCASE "
        "AND parent_id IS ? AND id<>?", (name, row["parent_id"], int(category_id))
    ).fetchone()
    if dup is not None:
        raise ValueError(f"A category named '{name}' already exists here.")
    conn.execute("UPDATE categories SET name=? WHERE id=?", (name, int(category_id)))
    conn.commit()


def _is_ancestor(conn: sqlite3.Connection, ancestor_id: int,
                 node_id: Optional[int]) -> bool:
    """True if ``ancestor_id`` is ``node_id`` or lies on its parent chain. Walks
    parent_id upward with a ``seen`` guard so a corrupt cycle can't hang."""
    seen: set = set()
    cur = node_id
    while cur is not None and int(cur) not in seen:
        seen.add(int(cur))
        if int(cur) == int(ancestor_id):
            return True
        row = conn.execute(
            "SELECT parent_id FROM categories WHERE id=?", (int(cur),)
        ).fetchone()
        cur = row["parent_id"] if row is not None else None
    return False


def reparent_category(conn: sqlite3.Connection, category_id: int,
                      new_parent_id: Optional[int]) -> None:
    """Move a category under a different parent (``None`` = the top level),
    keeping its id and its whole subtree intact. Refuses to create a cycle (a
    category cannot become its own parent or a child of one of its descendants)
    and refuses a move that would collide with an existing same-named sibling
    under the new parent (``UNIQUE(name, parent_id)``). Raises ``ValueError`` in
    those cases. A category id never indexes into balance history, so no balance
    checkpoint is affected. Commits."""
    cid = int(category_id)
    pid = None if new_parent_id is None else int(new_parent_id)
    if pid is not None:
        if pid == cid:
            raise ValueError("A category cannot be its own parent.")
        if _is_ancestor(conn, cid, pid):
            raise ValueError("Cannot move a category under its own descendant.")
    row = conn.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise KeyError(f"no category {category_id}")
    dup = conn.execute(
        "SELECT id FROM categories WHERE name=? COLLATE NOCASE "
        "AND parent_id IS ? AND id<>?", (row["name"], pid, cid)
    ).fetchone()
    if dup is not None:
        raise ValueError(
            f"A category named '{row['name']}' already exists under the new parent.")
    conn.execute("UPDATE categories SET parent_id=? WHERE id=?", (pid, cid))
    conn.commit()


def _merge_budget_lines(conn: sqlite3.Connection, from_id: int, to_id: int) -> None:
    """Repoint a merged category's budget target lines onto the survivor. Where
    both already have a line for the same ``(budget, period)`` -- which the
    ``UNIQUE(budget_id, category_id, period)`` index forbids from coexisting --
    the two targets are SUMMED onto the surviving line and the doomed line
    dropped, so a merge neither crashes on the constraint nor silently loses a
    budgeted amount (a plain repoint would do one or the other)."""
    for line in conn.execute(
        "SELECT id, budget_id, period, amount_cents FROM budget_lines "
        "WHERE category_id=?", (int(from_id),)
    ).fetchall():
        twin = conn.execute(
            "SELECT id, amount_cents FROM budget_lines "
            "WHERE budget_id=? AND category_id=? AND period=?",
            (line["budget_id"], int(to_id), line["period"])
        ).fetchone()
        if twin is not None:
            conn.execute(
                "UPDATE budget_lines SET amount_cents=? WHERE id=?",
                (int(twin["amount_cents"]) + int(line["amount_cents"]), twin["id"]))
            conn.execute("DELETE FROM budget_lines WHERE id=?", (line["id"],))
        else:
            conn.execute(
                "UPDATE budget_lines SET category_id=? WHERE id=?",
                (int(to_id), line["id"]))


def merge_category(conn: sqlite3.Connection, from_id: int, to_id: int) -> int:
    """Merge ``from_id`` INTO ``to_id`` and delete ``from_id``. Every reference is
    REPOINTED, never dropped: transactions and split lines (so no posting is
    orphaned onto NULL), learned ``category_rules`` and payee ``import_mappings``,
    ``scheduled_payments`` definitions, and ``budget_lines`` (summing any
    colliding target via :func:`_merge_budget_lines`). Child categories of
    ``from_id`` are moved under ``to_id``; a child whose name already exists
    there is merged recursively rather than colliding. Returns the number of
    ledger rows (transactions + splits) repointed.

    This is the whole reason a merge is a domain operation and not a UI delete:
    a plain ``delete_category`` would let ON DELETE CASCADE silently destroy the
    doomed category's budget lines and keyword rules and ON DELETE SET NULL
    uncategorize its transactions. A merge preserves all of it on the survivor.
    Category ids never index into balance history, so no balance checkpoint is
    touched. Raises ``ValueError`` when asked to merge a category into itself or
    into one of its own descendants. Commits."""
    src = int(from_id)
    dst = int(to_id)
    if src == dst:
        raise ValueError("Cannot merge a category into itself.")
    if _is_ancestor(conn, src, dst):
        raise ValueError("Cannot merge a category into its own descendant.")
    if conn.execute("SELECT id FROM categories WHERE id=?", (src,)).fetchone() is None:
        raise KeyError(f"no category {from_id}")
    if conn.execute("SELECT id FROM categories WHERE id=?", (dst,)).fetchone() is None:
        raise KeyError(f"no category {to_id}")

    # Move children under the survivor first, merging a same-named child
    # recursively so the reparent never trips UNIQUE(name, parent_id).
    for child in category_children(conn, src, include_hidden=True):
        twin = conn.execute(
            "SELECT id FROM categories WHERE parent_id=? AND name=? COLLATE NOCASE",
            (dst, child["name"])
        ).fetchone()
        if twin is not None:
            merge_category(conn, child["id"], twin["id"])
        else:
            reparent_category(conn, child["id"], dst)

    n = conn.execute(
        "UPDATE transactions SET category_id=? WHERE category_id=?", (dst, src)
    ).rowcount
    n += conn.execute(
        "UPDATE splits SET category_id=? WHERE category_id=?", (dst, src)
    ).rowcount
    # keyword rules are UNIQUE on keyword, not category, so a repoint can't
    # collide; import_mappings/scheduled_payments key on payee/schedule, likewise.
    conn.execute(
        "UPDATE import_mappings SET mapped_category_id=? WHERE mapped_category_id=?",
        (dst, src))
    conn.execute(
        "UPDATE category_rules SET category_id=? WHERE category_id=?", (dst, src))
    conn.execute(
        "UPDATE scheduled_payments SET category_id=? WHERE category_id=?", (dst, src))
    _merge_budget_lines(conn, src, dst)
    conn.execute("DELETE FROM categories WHERE id=?", (src,))
    conn.commit()
    return int(n)


def list_payees(conn: sqlite3.Connection) -> list[str]:
    """Distinct payee names the register has actually used, most recently used
    first -- the choices behind QuickFill's payee completer.

    Derived from ``transactions`` rather than the ``payees`` table: nothing
    writes that table, so reading it offered an empty completer. Recency order
    puts the payees being typed this month at the top of a prefix match instead
    of the one used once decades ago."""
    rows = conn.execute(
        "SELECT payee FROM transactions "
        "WHERE payee IS NOT NULL AND TRIM(payee) <> '' "
        "GROUP BY payee ORDER BY MAX(date) DESC, MAX(id) DESC, payee"
    ).fetchall()
    return [r["payee"] for r in rows]


def last_transaction_for_payee(
    conn: sqlite3.Connection, payee: Optional[str], account_id: Optional[int] = None,
    *, exclude_txn_id: Optional[int] = None,
) -> Optional[dict]:
    """The most recent POSTED transaction carrying ``payee`` -- QuickFill's
    memory -- or ``None``.

    Matching is case- and edge-whitespace-insensitive, so ``safeway`` recalls
    ``Safeway``. A row in ``account_id``'s own register wins over a newer one
    elsewhere: the same payee is a different amount on the card than from
    checking. Scheduled placeholders (``scheduled = 1``) are skipped -- a
    pre-entered bill is Mammon's guess, not something the user did. The dict is
    a transaction row plus ``is_split`` and ``category_label`` (the register's
    ``Parent:Child`` / ``[Account]`` / ``--Split--`` text), so a caller can copy
    the row's SHAPE, not only its category id."""
    key = (payee or "").strip().lower()
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM transactions "
        "WHERE payee IS NOT NULL AND scheduled = 0 "
        "AND LOWER(TRIM(payee)) = ? AND id != COALESCE(?, -1) "
        "ORDER BY (account_id = ?) DESC, date DESC, id DESC LIMIT 1",
        (key, exclude_txn_id, -1 if account_id is None else account_id),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["is_split"] = _has_splits(conn, row["id"])
    d["category_label"] = category_display(conn, row)
    return d


def category_path(conn: sqlite3.Connection, category_id: Optional[int]) -> str:
    """The ``Parent:Child`` path of a category id; ``""`` for None or unknown."""
    if category_id is None:
        return ""
    return _category_path(conn, category_id)


# --------------------------------------------------------------------------
# Balance checkpoints (performance: cache end-of-year running balances)
# --------------------------------------------------------------------------
def rebuild_checkpoints(conn: sqlite3.Connection, account_id: int) -> None:
    """Recompute end-of-year running balances into balance_checkpoints. A
    checkpoint for year Y holds the balance through Dec 31 of year Y."""
    acct = get_account(conn, account_id)
    if acct is None:
        raise KeyError(f"no account {account_id}")
    conn.execute("DELETE FROM balance_checkpoints WHERE account_id=?", (account_id,))
    rows = conn.execute(
        "SELECT substr(date,1,4) AS yr, SUM(amount) AS total "
        "FROM transactions WHERE account_id=? GROUP BY yr ORDER BY yr",
        (account_id,),
    ).fetchall()
    running = acct["opening_balance"]
    for r in rows:
        running += r["total"]
        conn.execute(
            "INSERT INTO balance_checkpoints(account_id, year, balance) VALUES (?,?,?)",
            (account_id, int(r["yr"]), running),
        )
    conn.commit()


def balance_via_checkpoint(conn: sqlite3.Connection, account_id: int, as_of: str) -> int:
    """Fast end-of-day balance using the nearest prior checkpoint plus the
    partial current year. Must equal account_balance() (verified in tests)."""
    _validate_date(as_of)
    year = int(as_of[:4])
    cp = conn.execute(
        "SELECT year, balance FROM balance_checkpoints "
        "WHERE account_id=? AND year<? ORDER BY year DESC LIMIT 1",
        (account_id, year),
    ).fetchone()
    if cp is None:
        base = get_account(conn, account_id)["opening_balance"]
        lower_bound = None
    else:
        base = cp["balance"]
        lower_bound = f"{cp['year']}-12-31"
    if lower_bound is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE account_id=? AND date<=?",
            (account_id, as_of),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions "
            "WHERE account_id=? AND date>? AND date<=?",
            (account_id, lower_bound, as_of),
        ).fetchone()
    return base + row[0]


def _has_checkpoints(conn: sqlite3.Connection, account_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM balance_checkpoints WHERE account_id=? LIMIT 1", (account_id,)
    ).fetchone() is not None


def recompute_checkpoints_from_year(conn: sqlite3.Connection, account_id: int, from_year: int) -> None:
    """Cascade a change dated in ``from_year``: drop the year-end balance
    snapshots for that year and all later ones, reseed from the ``from_year - 1``
    snapshot (or opening balance), and recompute each affected year forward.
    Earlier snapshots are untouched."""
    acct = get_account(conn, account_id)
    if acct is None:
        raise KeyError(f"no account {account_id}")
    conn.execute(
        "DELETE FROM balance_checkpoints WHERE account_id=? AND year>=?",
        (account_id, from_year),
    )
    prev = conn.execute(
        "SELECT balance FROM balance_checkpoints "
        "WHERE account_id=? AND year<? ORDER BY year DESC LIMIT 1",
        (account_id, from_year),
    ).fetchone()
    running = prev["balance"] if prev else acct["opening_balance"]
    rows = conn.execute(
        "SELECT substr(date,1,4) AS yr, SUM(amount) AS total FROM transactions "
        "WHERE account_id=? AND substr(date,1,4)>=? GROUP BY yr ORDER BY yr",
        (account_id, f"{from_year:04d}"),
    ).fetchall()
    for r in rows:
        running += r["total"]
        conn.execute(
            "INSERT INTO balance_checkpoints(account_id, year, balance) VALUES (?,?,?)",
            (account_id, int(r["yr"]), running),
        )
    conn.commit()


def _touch_checkpoints(conn: sqlite3.Connection, *account_years) -> None:
    """Cascade-recompute checkpoints for the given ``(account_id, year)`` edits,
    but only for accounts that already have a checkpoint cache -- a fresh bulk
    import (no checkpoints yet) stays O(n); the cache is built once at batch end
    (or by the first :func:`account_balance`-backing rebuild)."""
    by_account: dict[int, int] = {}
    for account_id, year in account_years:
        if account_id is None or year is None:
            continue
        by_account[account_id] = min(by_account.get(account_id, year), year)
    for account_id, year in by_account.items():
        if _has_checkpoints(conn, account_id):
            recompute_checkpoints_from_year(conn, account_id, year)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------
def _apply_update(conn: sqlite3.Connection, txn_id: int, updates: dict) -> None:
    # Snapshot BEFORE the write so a change to a reconciled row can be audited
    # (migration 63). This is the single choke point every column edit passes
    # through -- primary and mirror legs, void, replace_field -- so auditing here
    # catches them all in one place. The read is skipped-free of side effects and
    # only matters when the row turns out to have been reconciled.
    before = get_transaction(conn, txn_id)
    assignments = ",".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE transactions SET {assignments} WHERE id=?",
        (*updates.values(), txn_id),
    )
    if before is not None and before["reconciled"]:
        for field, new_value in updates.items():
            if field in _AUDIT_FIELDS and before[field] != new_value:
                _log_reconciled_change(
                    conn, before["account_id"], txn_id, "edit",
                    field, before[field], new_value,
                )


def _audit_value(v: Any) -> Optional[str]:
    """Render an old/new field value for the audit log as TEXT, preserving NULL.

    Amounts are signed cents (e.g. ``'-5000'``); the log stays money-logic-free
    and any presentation layer formats them, so the value is stored verbatim."""
    return None if v is None else str(v)


def _log_reconciled_change(conn: sqlite3.Connection, account_id: int,
                           txn_id: Optional[int], operation: str,
                           field: Optional[str], old: Any, new: Any) -> None:
    """Append one row to ``reconciled_change_log`` (migration 63). Called only
    from the edit/delete paths and only for transactions that were reconciled at
    the time -- the caller has already checked that."""
    conn.execute(
        "INSERT INTO reconciled_change_log"
        " (account_id, transaction_id, operation, field, old_value, new_value)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, txn_id, operation, field,
         _audit_value(old), _audit_value(new)),
    )


def _log_reconciled_delete(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Audit the deletion of a RECONCILED transaction: one 'delete' entry per
    surviving value field (migration 63). No-op for an unreconciled row."""
    if not row["reconciled"]:
        return
    for field in _DELETE_AUDIT_FIELDS:
        value = row[field]
        if value is not None:
            _log_reconciled_change(
                conn, row["account_id"], row["id"], "delete",
                field, value, None,
            )


def _category_path(conn: sqlite3.Connection, category_id: int) -> str:
    parts = []
    cid = category_id
    seen = set()
    while cid is not None and cid not in seen:
        seen.add(cid)
        r = conn.execute("SELECT name, parent_id FROM categories WHERE id=?", (cid,)).fetchone()
        if r is None:
            break
        parts.append(r["name"])
        cid = r["parent_id"]
    return ":".join(reversed(parts))


def _validate_date(date: str) -> None:
    try:
        _dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError(f"date must be ISO 'YYYY-MM-DD', got {date!r}")
