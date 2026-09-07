"""mammon.categorize -- auto-categorization from history (SRD 5.5).

Mammon learns which category a recurring payee usually gets, stores that in the
``import_mappings`` table, and can then auto-fill the category on a new or
imported transaction whose payee it recognises.

This module does NOT rename anything. Turning a bank's raw
``statementDescription`` into a payee name is the rename tree's job
(:mod:`mammon.rename_tree`); by the time a mapping is consulted the payee
already HAS its name. The ``mapped_payee`` column is therefore left NULL by
this module: it only ever held the same text as ``payee_pattern`` re-cased
(every one of the real ledger's 30 rows did), which made the Rules manager
look like a second, competing rename list. The column stays in the schema
because ``importers/core.py`` uses ``import_mappings`` for a different
purpose -- ``source='account_link'`` rows, keyed ``@acct:...``, where
``mapped_payee`` is the linked account's display name and IS read back.

Design
------
* A mapping is keyed by ``payee_pattern`` -- the NORMALIZED payee (case,
  whitespace, and trailing store/reference numbers stripped, reusing
  :func:`mammon.importers.record.normalize_payee`, the same idea behind
  ``payees.normalized_name``). So "SAFEWAY #123" and "Safeway  #456" collapse to
  one pattern.
* ``source`` ranks the mapping: ``'user'`` (the user explicitly set a category on
  a transaction) OUTRANKS ``'learned'`` (inferred from history). There is at most
  one row per pattern (``payee_pattern`` is UNIQUE), so the source on that row is
  authoritative -- :func:`learn_from_history` never clobbers a ``'user'`` row.
* A ``'learned'`` mapping is only written when history is CONSISTENT: the dominant
  category must own at least :data:`LEARN_MIN_SHARE` of the payee's categorized
  transactions (so a payee split evenly across categories learns nothing and
  :func:`suggest_category` returns ``None`` -- the no-confident-match case). A
  ``'user'`` mapping is always authoritative.

This module is the only writer of ``import_mappings`` rows; category rows are
still resolved through :mod:`mammon.ledger` so the domain layer stays the single
writer of ``categories``.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from typing import Optional

from mammon import category_tree, ledger
from mammon.importers.record import normalize_payee

# A learned pattern must be this consistent to be trusted for auto-fill.
LEARN_MIN_SHARE = 0.6          # dominant category's share of the payee's txns
LEARN_MIN_COUNT = 1            # ...backed by at least this many categorized txns

_LEARNED = "learned"
_USER = "user"


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def normalized_pattern(payee: Optional[str]) -> str:
    """The mapping key for a payee (see module docstring)."""
    return normalize_payee(payee or "")


# ---------------------------------------------------------------------------
# lookup / suggestion
# ---------------------------------------------------------------------------
def mapping_for(conn: sqlite3.Connection, payee: Optional[str]) -> Optional[sqlite3.Row]:
    """The raw ``import_mappings`` row for a payee's pattern, or ``None``."""
    pattern = normalized_pattern(payee)
    if not pattern:
        return None
    return conn.execute(
        "SELECT * FROM import_mappings WHERE payee_pattern=?", (pattern,)
    ).fetchone()


def suggest_category(conn: sqlite3.Connection, payee: Optional[str]) -> Optional[int]:
    """Best known ``category_id`` for ``payee``, or ``None`` when no confident
    mapping exists. A ``'user'`` mapping outranks a ``'learned'`` one; because a
    learned row is only written when history is consistent, any present row with
    a category IS the confident answer.
    """
    row = mapping_for(conn, payee)
    if row is None:
        return None
    return row["mapped_category_id"]


def quickfill(conn: sqlite3.Connection, payee: Optional[str],
              account_id: Optional[int] = None) -> dict:
    """What to pre-enter for a NEW transaction to ``payee`` -- Quicken's
    QuickFill, without a separately maintained memorized-payee list.

    Returns ``{"category": label, "memo": str, "tag": str, "amount": cents}``
    with only the keys history can answer; ``{}`` for a payee never seen. The
    ledger is the memory: memo, tag and amount come from the payee's most
    recent posted transaction (:func:`ledger.last_transaction_for_payee`, this
    account's own row preferred). The category is chosen in this order:

    * the last row was a TRANSFER -> its ``[Account]`` label, so the new row
      becomes a transfer too (the most recent intent wins over an old mapping);
    * a learned or user-set mapping exists -> that category, since a user
      override there outranks whatever the last row happened to carry;
    * otherwise the last row's own category.

    A split is not copied: its lines belong to the split dialog (which offers
    "copy previous split" itself), so the category is left for the user.
    """
    last = ledger.last_transaction_for_payee(conn, payee, account_id)
    suggested = suggest_category(conn, payee)
    out: dict = {}
    if last is None:
        if suggested is not None:
            path = ledger.category_path(conn, suggested)
            if path:
                out["category"] = path
        return out
    out["amount"] = int(last["amount"])
    out["memo"] = last["memo"] or ""
    out["tag"] = last["tag"] or ""
    if last["is_split"]:
        out["category"] = ""
    elif last["transfer_account_id"] is not None:
        out["category"] = last["category_label"]
    else:
        path = ledger.category_path(conn, suggested) if suggested is not None else ""
        out["category"] = path or last["category_label"]
    return out


def inherited_entry_for_payee(conn: sqlite3.Connection,
                              payee: Optional[str]) -> dict:
    """The category or split a NEW scheduled definition for ``payee`` should
    INHERIT from that payee's history, so the finance calendar's Add-scheduled
    dialog can PREFILL and SHOW it (SRD 5.10c) instead of leaving a bare default
    the user cannot tell will actually become a split. Read-only.

    Returns ``{"category_id", "category_label", "splits"}``:

    * ``splits`` -- the payee's most recent SPLIT breakdown
      (:func:`ledger.previous_split_for_payee`, the ``ledger.get_splits`` shape).
      This is EXACTLY the set of lines the calendar learns onto the definition
      and reproduces on every pre-entry, so what the dialog shows is what will be
      entered -- no second lookup to drift out of step with the write path. When
      a split is present ``category_*`` are left empty (its lines carry the
      categories and a split overrides the single category at entry).
    * otherwise ``category_id`` / ``category_label`` -- the payee's learned or
      user-set category (:func:`suggest_category`, keyed on the NORMALIZED payee
      via ``import_mappings``, the same normalization the categorizer uses) or,
      with no confident mapping, its most recent posted transaction's own
      category. Empty when the payee has no usable history.
    """
    splits = ledger.previous_split_for_payee(conn, payee)
    if len(splits) >= 2:
        return {"category_id": None, "category_label": "", "splits": splits}
    cat = suggest_category(conn, payee)
    if cat is None:
        last = ledger.last_transaction_for_payee(conn, payee)
        cat = last["category_id"] if last is not None else None
    return {"category_id": cat,
            "category_label": ledger.category_path(conn, cat) if cat else "",
            "splits": []}


# ---------------------------------------------------------------------------
# learning from existing history
# ---------------------------------------------------------------------------
def learn_from_history(
    conn: sqlite3.Connection, account_ids: Optional[list[int]] = None
) -> int:
    """(Re)learn payee -> category mappings from the transactions already stored.

    One pass over every categorized, non-transfer transaction, grouped by
    normalized payee; each payee's dominant category is upserted as a
    ``'learned'`` mapping when it clears the consistency bar. ``'user'`` rows are
    left untouched. Returns the number of learned mappings written or refreshed.
    """
    stats = _scan(conn, account_ids)
    written = 0
    for pattern, (cats, payees) in stats.items():
        if _relearn_pattern(conn, pattern, cats, payees):
            written += 1
    conn.commit()
    return written


def relearn_payee(
    conn: sqlite3.Connection,
    payee: Optional[str],
    account_ids: Optional[list[int]] = None,
) -> Optional[int]:
    """Recompute just one payee's learned mapping from history (handy after
    inserting new transactions). Returns the learned ``category_id`` or ``None``.
    Never overrides a ``'user'`` mapping.
    """
    pattern = normalized_pattern(payee)
    if not pattern:
        return None
    stats = _scan(conn, account_ids, only_pattern=pattern)
    cats, payees = stats.get(pattern, (Counter(), Counter()))
    _relearn_pattern(conn, pattern, cats, payees)
    conn.commit()
    row = conn.execute(
        "SELECT mapped_category_id FROM import_mappings WHERE payee_pattern=?",
        (pattern,),
    ).fetchone()
    return row["mapped_category_id"] if row else None


def _relearn_pattern(
    conn: sqlite3.Connection, pattern: str, cats: Counter, payees: Counter
) -> bool:
    """Upsert (or clear) the LEARNED mapping for one pattern from its category
    votes. Returns True if a learned mapping is now in place. A ``'user'`` row is
    never modified. Does not commit (callers batch the commit)."""
    existing = conn.execute(
        "SELECT id, source FROM import_mappings WHERE payee_pattern=?", (pattern,)
    ).fetchone()
    if existing is not None and existing["source"] == _USER:
        return False  # user pin outranks anything history can say

    best = _dominant(cats)
    if best is None:
        # No confident category: drop a stale learned row so suggestions stop.
        if existing is not None:  # (only learned rows reach here)
            conn.execute("DELETE FROM import_mappings WHERE id=?", (existing["id"],))
        return False

    category_id, hit_count = best
    if existing is not None:
        conn.execute(
            "UPDATE import_mappings SET mapped_category_id=?, "
            "source=?, hit_count=? WHERE id=?",
            (category_id, _LEARNED, hit_count, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO import_mappings"
            "(payee_pattern, mapped_category_id, source, hit_count) "
            "VALUES (?,?,?,?)",
            (pattern, category_id, _LEARNED, hit_count),
        )
    return True


def _dominant(cats: Counter) -> Optional[tuple[int, int]]:
    """Return (category_id, count) for the consistently-dominant category, or
    None when the votes are too few or too split to trust."""
    total = sum(cats.values())
    if total == 0:
        return None
    category_id, count = cats.most_common(1)[0]
    if count < LEARN_MIN_COUNT:
        return None
    if count / total < LEARN_MIN_SHARE:
        return None
    return category_id, count


def _scan(
    conn: sqlite3.Connection,
    account_ids: Optional[list[int]],
    only_pattern: Optional[str] = None,
) -> dict[str, tuple[Counter, Counter]]:
    """Tally, per normalized payee, the category votes and the raw payee spellings
    across categorized, non-transfer transactions."""
    sql = (
        "SELECT payee, category_id FROM transactions "
        "WHERE category_id IS NOT NULL AND payee IS NOT NULL AND payee!='' "
        "AND transfer_account_id IS NULL"
    )
    params: list = []
    if account_ids:
        sql += " AND account_id IN (%s)" % ",".join("?" for _ in account_ids)
        params.extend(account_ids)
    stats: dict[str, tuple[Counter, Counter]] = defaultdict(
        lambda: (Counter(), Counter())
    )
    for row in conn.execute(sql, params):
        pattern = normalize_payee(row["payee"])
        if not pattern:
            continue
        if only_pattern is not None and pattern != only_pattern:
            continue
        cats, payees = stats[pattern]
        cats[row["category_id"]] += 1
        payees[row["payee"]] += 1
    return stats


# ---------------------------------------------------------------------------
# user overrides
# ---------------------------------------------------------------------------
def record_user_categorization(
    conn: sqlite3.Connection, payee: Optional[str], category_id: Optional[int]
) -> None:
    """Record that the USER set ``category_id`` on a transaction with this payee.

    The register/import UI calls this on a manual category edit. It upserts a
    ``source='user'`` mapping, which OUTRANKS any learned mapping and is never
    overwritten by :func:`learn_from_history`, so the next suggestion follows the
    override. Passing ``category_id=None`` records a user decision to leave this
    payee uncategorized (suppressing learned suggestions).

    It also records the vote with :mod:`mammon.category_tree`, whose per-payee
    tally drives the import review's confidence gate and the ranked category
    picker. A register edit carries no bank text, so this teaches the payee
    tally only and never the trie (see :func:`mammon.category_tree.learn`) --
    but it is the same user decision, and the picker must reflect it.
    """
    pattern = normalized_pattern(payee)
    if not pattern:
        return
    category_tree.learn(conn, payee, "", category_id)
    existing = conn.execute(
        "SELECT id, hit_count FROM import_mappings WHERE payee_pattern=?", (pattern,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE import_mappings SET mapped_category_id=?, "
            "source=?, hit_count=? WHERE id=?",
            (category_id, _USER, (existing["hit_count"] or 0) + 1, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO import_mappings"
            "(payee_pattern, mapped_category_id, source, hit_count) "
            "VALUES (?,?,?,1)",
            (pattern, category_id, _USER),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# management surface (list / forget) -- feeds the Rules manager UI so the GUI
# issues no SQL of its own; this module stays the sole writer of the table.
# ---------------------------------------------------------------------------
def list_mappings(conn: sqlite3.Connection) -> list[dict]:
    """Every learned payee mapping as a dict, alphabetically by pattern.

    Fields: ``id, payee_pattern, mapped_payee, mapped_category_id, source,
    hit_count``. The category is stored only as an id -- callers render a name
    via :func:`mammon.ledger.category_path`.
    """
    rows = conn.execute(
        "SELECT id, payee_pattern, mapped_payee, mapped_category_id, source, "
        "hit_count FROM import_mappings ORDER BY payee_pattern ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def forget_mapping(conn: sqlite3.Connection, mapping_id: int) -> None:
    """Delete a learned payee mapping by id (management UI). Commits.

    Unlike the internal relearn path (which only drops a stale ``'learned'``
    row), this forgets any mapping -- ``'user'`` or ``'learned'`` -- because the
    user is explicitly asking the Rules manager to drop it.
    """
    conn.execute("DELETE FROM import_mappings WHERE id=?", (mapping_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# auto-fill hook for new / imported transactions
# ---------------------------------------------------------------------------
def autofill_transaction(
    conn: sqlite3.Connection, txn_id: int, *, overwrite: bool = False
) -> Optional[int]:
    """Fill a transaction's category from history when a confident pattern
    exists. Skips transfers, and (unless ``overwrite``) transactions that already
    carry a category. Returns the ``category_id`` applied, or ``None``.

    This is the hook the ledger/import path calls after creating a row.
    """
    row = ledger.get_transaction(conn, txn_id)
    if row is None:
        return None
    if row["transfer_account_id"] is not None:
        return None  # transfers categorize as "[Other Account]", never learned
    if row["category_id"] is not None and not overwrite:
        return None
    category_id = suggest_category(conn, row["payee"])
    if category_id is None:
        return None
    ledger.update_transaction(conn, txn_id, category_id=category_id)
    return category_id


def autocategorize_import(
    conn: sqlite3.Connection, import_id: int, *, overwrite: bool = False
) -> int:
    """Auto-fill categories on every uncategorized row of a finished import.
    Returns the count filled. A convenient hook for the download/import path to
    call once a batch lands."""
    rows = conn.execute(
        "SELECT id FROM transactions WHERE import_id=? AND category_id IS NULL "
        "AND transfer_account_id IS NULL",
        (import_id,),
    ).fetchall()
    filled = 0
    for r in rows:
        if autofill_transaction(conn, r["id"], overwrite=overwrite) is not None:
            filled += 1
    return filled
