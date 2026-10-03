"""mammon.categorize -- what a NEW transaction's Category cell starts as (SRD 5.5).

The learning lives in :mod:`mammon.category_tree`, the one learner for the
Category cell: a category or a transfer account, under the payee and under the
payee in each account, with vote counts. This module is the register's side of
it -- QuickFill, the auto-fill hooks, the scheduled-entry inheritance, and the
register edits that teach the tree.

It used to keep its own ``payee -> category`` table in ``import_mappings``
(``source='learned'`` from a history scan, ``'user'`` pinned by a register
edit). Migration 116 removed those rows: every learned behavior is a tree (the
user's ruling), and the tree's per-payee tally is the same evidence with counts
attached. ``import_mappings`` still holds the importer's ``account_link`` rows
(``importers/core.py``), which are not learning.

This module does NOT rename anything. Turning a bank's raw
``statementDescription`` into a payee name is the rename tree's job
(:mod:`mammon.rename_tree`); by the time a category is consulted the payee
already HAS its name.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from mammon import category_tree, ledger
from mammon.importers.record import normalize_payee


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def normalized_pattern(payee: Optional[str]) -> str:
    """The normalized form of a payee -- the key the category tree learns it
    under (:func:`mammon.category_tree.normalized_key`), so "SAFEWAY #123" and
    "Safeway  #456" are one payee."""
    return normalize_payee(payee or "")


# ---------------------------------------------------------------------------
# lookup / suggestion
# ---------------------------------------------------------------------------
def suggest_category(conn: sqlite3.Connection, payee: Optional[str], *,
                     account_id: Optional[int] = None) -> Optional[int]:
    """The category the tree is confident ``payee`` takes when nothing else is
    known about the row (no bank text), or ``None``: the payee's dominant
    category, when it is coherent and corroborated -- in ``account_id``'s own
    history once it has enough (:func:`category_tree.dominant`). A transfer
    answer is not a category; see :func:`quickfill` for the whole cell."""
    return category_tree.label_category(
        category_tree.dominant(conn, payee, account_id=account_id))


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
      becomes a transfer too;
    * otherwise the last row's own category -- the most recent intent, which
      is also what a user's latest edit changed;
    * with no row at all, the category the tree is confident the payee takes
      (:func:`suggest_category`).

    A pinned per-payee category used to outrank the last row; that pin lived in
    the ``import_mappings`` table migration 116 removed. The tree's knowledge
    of a payee's other categories is offered instead in the payee completer,
    one line per category with its count, so a different one is a pick away.

    A split is not copied: its lines belong to the split dialog (which offers
    "copy previous split" itself), so the category is left for the user.
    """
    last = ledger.last_transaction_for_payee(conn, payee, account_id)
    out: dict = {}
    if last is None:
        suggested = suggest_category(conn, payee, account_id=account_id)
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
    else:
        out["category"] = last["category_label"]
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
    * otherwise ``category_id`` / ``category_label`` -- the category the tree
      is confident the payee takes (:func:`suggest_category`, keyed on the
      NORMALIZED payee) or, with nothing confident, its most recent posted
      transaction's own category. Empty when the payee has no usable history.
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
# user overrides
# ---------------------------------------------------------------------------
def record_user_categorization(
    conn: sqlite3.Connection, payee: Optional[str], category_id: Optional[int],
    *, account_id: Optional[int] = None,
    transfer_account_id: Optional[int] = None,
) -> None:
    """Record that the USER set ``category_id`` -- or, given
    ``transfer_account_id``, a transfer to that account -- on a register row
    for ``payee`` in ``account_id``.

    The vote goes to :mod:`mammon.category_tree`, whose per-payee tally drives
    the import review's confidence gate, the ranked category picker and the
    payee completer's list of categories. A register edit carries no bank text,
    so this teaches the tally only and never the trie (see
    :func:`mammon.category_tree.learn`). A ``None`` category teaches nothing."""
    label = category_tree.label_for(category_id, transfer_account_id)
    if label is None or not normalize_payee(payee or ""):
        return
    category_tree.learn(conn, payee, "", label, account_id=account_id)


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
    category_id = suggest_category(conn, row["payee"],
                                   account_id=int(row["account_id"]))
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
