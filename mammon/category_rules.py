"""Category learning rules -- the category-side twin of :mod:`mammon.payee_rules`.

When a user sets or edits the category of a NEW import-review row, Mammon should
*learn* the association so the same bank ``statementDescription`` pre-fills that
category on every future import -- the same "type the first few of each kind"
gesture that payee renaming rules give for payees.

A rule is ``keyword -> category_id`` where ``keyword`` is a distinguishing,
UPPER-CASED token (or space-joined refinement) pulled from the raw description.
Matching, keyword extraction, and keyword-scoped refinement are shared verbatim
with :mod:`mammon.payee_rules` (same tokenizer, same "first significant token",
same "longer keyword wins" precedence, same whole-token guarantee), so the two
engines behave identically -- only the stored payload (a category id vs a payee
string) differs.

Design notes / contracts (mirroring payee_rules):

* Rules are GLOBAL, not per-account. ``keyword`` is UNIQUE; re-teaching the same
  keyword overwrites its category (an edit is an update, not a duplicate).
* A one-off correction of an auto-filled category does NOT clobber a good broad
  rule -- :func:`learn_from_edit` learns a more specific compound keyword instead
  (see :func:`mammon.keywords.refine_keyword`).
* Transfer rows are handled entirely by :mod:`mammon.import_review`; callers must
  NOT apply or learn category rules for a transfer row.
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
    "learn_from_edit",
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
    omitted, rule conditions are ignored and behaviour is byte-identical to a
    plain keyword lookup.
    """
    rule = match_rule(desc, load_rules(conn), context=context)
    return int(rule["category_id"]) if rule else None


# ---------------------------------------------------------------------------
# management surface (list / edit / delete) + learning
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

    ``keyword`` is normalised to a single UPPER-CASED token unless ``compound``
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


def learn_from_edit(conn, desc: str, chosen: Optional[int],
                    *, provisional: Optional[int] = None) -> Optional[int]:
    """Learn/refresh a rule from a user's category choice; return its id.

    ``desc`` is the raw ``statementDescription``, ``chosen`` the ``category_id``
    the user is committing, and ``provisional`` the category Mammon had predicted
    (possibly already rule-applied). Learns nothing (returns ``None``) when the
    chosen category is ``None`` (the user left it blank), equals the provisional
    (no correction happened), or no keyword can be extracted.

    Keyword-scoped refinement (using :func:`mammon.keywords.refine_keyword`): when
    the wrong ``provisional`` came from an existing broad rule, a more specific
    compound keyword is learned instead of overwriting that rule.
    """
    if chosen is None:
        return None
    picked = int(chosen)
    if provisional is not None and picked == int(provisional):
        return None
    if provisional is not None:
        matched = match_rule(desc, load_rules(conn))
        if (matched is not None
                and int(matched["category_id"]) == int(provisional)
                and int(provisional) != picked):
            refined = refine_keyword(desc, matched["keyword"])
            if refined:
                return upsert_rule(conn, refined, picked, compound=True)
            # No distinguishing token: fall through and overwrite the broad rule.
    keyword = extract_keyword(desc)
    if not keyword:
        return None
    return upsert_rule(conn, keyword, picked)


def note_applied(conn, rule_id: int) -> None:
    """Bump a rule's ``match_count`` after an auto-apply (best-effort). Commits."""
    conn.execute(
        "UPDATE category_rules SET match_count = match_count + 1 WHERE id=?",
        (rule_id,))
    conn.commit()
