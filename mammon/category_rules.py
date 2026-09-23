"""Hand-written ``keyword -> category_id`` rules (the Rules manager's table).

This table used to LEARN. ``learn_from_edit`` minted a rule from a single
correction, keyed on the first non-noise token of the raw description, matched
GLOBALLY against every merchant. Replayed over a real first year it fired on 67%
of rows and was wrong on 26% of those, and half those errors proposed a category
the payee had never carried -- a rule learned from one merchant firing on an
unrelated one. The town in the tail of every local card swipe ("ANYTOWN", from
Anytown UT) became a Utilities:Gas & Electric rule and then fired on Subway,
O'Reilly, Clegg Automotive and the youth theatre.

Auto-categorization now lives in :mod:`mammon.category_tree`, a discrimination
tree per PAYEE, where a candidate can only ever be a category that payee has
carried. The learner here is gone and migration 57 deleted every row it had
written, so what remains is exactly what somebody typed into the Rules manager.

That is the whole point of the purge: because nothing writes this table
automatically any more, a rule in it is a deliberate instruction rather than a
guess, and :func:`mammon.import_review.predict_fields` can honour it directly --
it is not the system offering an unrelated category at the user, which is the
thing the payee scoping exists to prevent.

Design notes / contracts:

* Rules are GLOBAL, not per-account, and that is now the user's explicit choice
  rather than an accident of learning. ``keyword`` is UNIQUE; re-teaching the
  same keyword overwrites its category (an edit is an update, not a duplicate).
* The tokenizer, keyword extraction and whole-token matching are shared with
  :mod:`mammon.transfer_rules` via :mod:`mammon.keywords`, so the two stay in
  lock-step. (``transfer_rules`` still learns; it maps statement text to an
  ACCOUNT, a far smaller and less ambiguous target than a category.)
* Consulted only after the payee tree declines; see ``predict_fields``.
* Transfer rows are handled entirely by :mod:`mammon.import_review`; callers must
  NOT apply category rules for a transfer row.
* Every mutator commits itself.
"""
from __future__ import annotations

from typing import Optional

from . import keywords

# The keyword machinery is identical for both engines -- reuse it so the two
# stay in lock-step (tokenizing, keyword choice, refinement, whole-token match).
extract_keyword = keywords.extract_keyword
refine_keyword = keywords.refine_keyword
match_rule = keywords.match_rule
_tokens = keywords._tokens

__all__ = [
    "extract_keyword",
    "refine_keyword",
    "match_rule",
    "load_rules",
    "apply_rules",
    "list_rules",
    "get_rule",
    "upsert_rule",
    "update_rule",
    "delete_rule",
]


def load_rules(conn) -> list[dict]:
    """All rules as dicts, most-specific first (longest keyword wins on ties).

    Includes the optional condition columns (``account_id``,
    ``amount_min_cents``, ``amount_max_cents``, ``memo_contains``) so
    :func:`match_rule` can honour them when given transaction context.
    """
    rows = conn.execute(
        "SELECT id, keyword, category_id, match_count, "
        "account_id, amount_min_cents, amount_max_cents, memo_contains "
        "FROM category_rules "
        "ORDER BY LENGTH(keyword) DESC, keyword ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def apply_rules(conn, desc: str, *, context=None) -> Optional[int]:
    """Learned ``category_id`` for ``desc`` (``None`` when no rule matches).

    Convenience wrapper over :func:`load_rules` + :func:`match_rule`; batch
    callers should load rules once and reuse :func:`match_rule`. ``context`` is
    optional transaction context (see :func:`mammon.keywords.match_rule`); when
    omitted, rule conditions are ignored and behavior is byte-identical to a
    plain keyword lookup.
    """
    rule = match_rule(desc, load_rules(conn), context=context)
    return int(rule["category_id"]) if rule else None


# ---------------------------------------------------------------------------
# management surface (list / edit / delete)
# ---------------------------------------------------------------------------
def list_rules(conn) -> list[dict]:
    """Rules for a management UI, alphabetically by keyword.

    Includes the optional condition columns so a Rules manager can display and
    edit them.
    """
    rows = conn.execute(
        "SELECT id, keyword, category_id, match_count, "
        "account_id, amount_min_cents, amount_max_cents, memo_contains, "
        "created_at, updated_at "
        "FROM category_rules ORDER BY keyword ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_rule(conn, rule_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT id, keyword, category_id, match_count, "
        "account_id, amount_min_cents, amount_max_cents, memo_contains, "
        "created_at, updated_at "
        "FROM category_rules WHERE id=?", (rule_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_rule(conn, keyword: str, category_id: Optional[int], *,
                compound: bool = False,
                account_id: Optional[int] = None,
                amount_min_cents: Optional[int] = None,
                amount_max_cents: Optional[int] = None,
                memo_contains: Optional[str] = None) -> Optional[int]:
    """Create or overwrite the rule for ``keyword`` -> ``category_id``; return id.

    ``keyword`` is normalized to a single UPPER-CASED token unless ``compound``
    is set (then its full space-joined form is kept, so a refinement like
    "AMAZON WEB" out-ranks the broad "AMAZON" rule). Returns ``None`` when the
    keyword is blank or ``category_id`` is ``None`` -- we never store an empty
    rule. Commits.

    The optional conditions (``account_id`` scope, inclusive signed-cent
    ``amount_min_cents``/``amount_max_cents`` range, case-insensitive
    ``memo_contains`` substring) narrow when the rule fires -- see
    :func:`mammon.keywords.match_rule`. They default to ``None`` (no condition),
    so an existing keyword-only caller stores exactly what it did before. On a
    fresh row all four are written (``None`` -> NULL); when overwriting an
    existing keyword, only the conditions explicitly supplied (non-``None``) are
    changed, so re-teaching a keyword never silently wipes conditions a Rules
    manager set. (Clearing a condition back to NULL is the editor's job, not
    upsert's.)
    """
    if compound:
        kw = " ".join(_tokens(keyword))
    else:
        kw = extract_keyword(keyword) or "".join(_tokens(keyword)[:1])
    kw = (kw or "").upper().strip()
    if not kw or category_id is None:
        return None
    cid = int(category_id)
    conds = keywords.normalize_conditions(
        account_id, amount_min_cents, amount_max_cents, memo_contains)
    existing = conn.execute(
        "SELECT id FROM category_rules WHERE keyword=?", (kw,)).fetchone()
    if existing is not None:
        sets = ["category_id=?"]
        vals: list = [cid]
        for col in keywords.CONDITION_COLUMNS:
            if conds[col] is not None:
                sets.append(f"{col}=?")
                vals.append(conds[col])
        sets.append("updated_at=datetime('now')")
        vals.append(int(existing[0]))
        conn.execute(
            f"UPDATE category_rules SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
        return int(existing[0])
    cur = conn.execute(
        "INSERT INTO category_rules "
        "(keyword, category_id, match_count, "
        "account_id, amount_min_cents, amount_max_cents, memo_contains, "
        "created_at, updated_at) "
        "VALUES (?,?,0,?,?,?,?, datetime('now'), datetime('now'))",
        (kw, cid, conds["account_id"], conds["amount_min_cents"],
         conds["amount_max_cents"], conds["memo_contains"]))
    conn.commit()
    return int(cur.lastrowid)


def update_rule(conn, rule_id: int, *, keyword: Optional[str] = None,
                category_id: Optional[int] = None) -> None:
    """Edit an existing rule's keyword and/or category (management UI). Commits."""
    sets: list[str] = []
    vals: list = []
    if keyword is not None:
        kw = (extract_keyword(keyword) or keyword).upper().strip()
        if kw:
            sets.append("keyword=?")
            vals.append(kw)
    if category_id is not None:
        sets.append("category_id=?")
        vals.append(int(category_id))
    if not sets:
        return
    sets.append("updated_at=datetime('now')")
    vals.append(rule_id)
    conn.execute(
        f"UPDATE category_rules SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()


def delete_rule(conn, rule_id: int) -> None:
    """Remove a rule (management UI). Commits."""
    conn.execute("DELETE FROM category_rules WHERE id=?", (rule_id,))
    conn.commit()


def note_applied(conn, rule_id: int) -> None:
    """Bump a rule's ``match_count`` after an auto-apply (best-effort). Commits."""
    conn.execute(
        "UPDATE category_rules SET match_count = match_count + 1 WHERE id=?",
        (rule_id,))
    conn.commit()
