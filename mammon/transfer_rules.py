"""Transfer learning rules -- the transfer-side twin of :mod:`mammon.payee_rules`
and :mod:`mammon.category_rules`.

When a user points a NEW import-review *transfer* row at a real account, Mammon
should *learn* the association so the same bank ``statementDescription``
pre-fills that transfer account on every future import -- the same "type the
first few of each kind" gesture the payee/category engines already give (the user's
request: transfer learning must work "as it does for dividends").

A rule is ``keyword -> transfer_account_id`` where ``keyword`` is a
distinguishing, UPPER-CASED token (or space-joined refinement) pulled from the
raw description. Matching, keyword extraction, and keyword-scoped refinement are
shared verbatim with :mod:`mammon.payee_rules` (same tokenizer, same precedence),
so all three engines behave identically -- only the stored payload differs (a
payee string, a category id, or here an account id).

Design notes / contracts (mirroring category_rules):

* Rules are GLOBAL, not per-account. ``keyword`` is UNIQUE; re-teaching the same
  keyword overwrites its account (an edit is an update, not a duplicate).
* A one-off correction does NOT clobber a good broad rule --
  :func:`learn_from_edit` learns a more specific compound keyword instead.
* Every mutator commits itself.
"""
from __future__ import annotations

from typing import Optional

from . import keywords

# The keyword machinery is identical for all three engines -- reuse it so they
# stay in lock-step (tokenizing, keyword choice, refinement, whole-token match).
extract_keyword = keywords.extract_keyword
refine_keyword = keywords.refine_keyword
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
    "delete_rule",
    "learn_from_edit",
]


def match_rule(desc: str, rules, *, context=None) -> Optional[dict]:
    """Most-specific rule whose keyword matches ``desc`` (or ``None``).

    Delegates to :func:`keywords.match_rule` -- the payload column differs but
    the keyword-matching logic (and optional condition handling via ``context``)
    is identical."""
    return keywords.match_rule(desc, rules, context=context)


def load_rules(conn) -> list[dict]:
    """All rules as dicts, most-specific first (longest keyword wins on ties).

    Includes the optional condition columns (``account_id``,
    ``amount_min_cents``, ``amount_max_cents``, ``memo_contains``) so
    :func:`match_rule` can honour them when given transaction context. Note
    ``account_id`` is the SCOPE condition (which account the row lives in),
    distinct from the ``transfer_account_id`` payload (the transfer's far side).
    """
    rows = conn.execute(
        "SELECT id, keyword, transfer_account_id, match_count, "
        "account_id, amount_min_cents, amount_max_cents, memo_contains "
        "FROM transfer_rules "
        "ORDER BY LENGTH(keyword) DESC, keyword ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def apply_rules(conn, desc: str, *, context=None) -> Optional[int]:
    """Learned ``transfer_account_id`` for ``desc`` (``None`` when none match).

    ``context`` is optional transaction context (see
    :func:`mammon.keywords.match_rule`); when omitted, rule conditions are
    ignored and behavior is byte-identical to a plain keyword lookup.
    """
    rule = match_rule(desc, load_rules(conn), context=context)
    return int(rule["transfer_account_id"]) if rule else None


# ---------------------------------------------------------------------------
# management surface (list / fetch / delete) -- mirrors category_rules so a
# Rules manager can drive both engines identically, issuing no SQL of its own.
# ---------------------------------------------------------------------------
def list_rules(conn) -> list[dict]:
    """Rules for a management UI, alphabetically by keyword.

    Includes the optional condition columns so a Rules manager can display and
    edit them. ``account_id`` is the SCOPE condition (which account the row
    lives in), distinct from the ``transfer_account_id`` payload.
    """
    rows = conn.execute(
        "SELECT id, keyword, transfer_account_id, match_count, "
        "account_id, amount_min_cents, amount_max_cents, memo_contains, "
        "created_at, updated_at "
        "FROM transfer_rules ORDER BY keyword ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_rule(conn, rule_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT id, keyword, transfer_account_id, match_count, "
        "account_id, amount_min_cents, amount_max_cents, memo_contains, "
        "created_at, updated_at "
        "FROM transfer_rules WHERE id=?", (rule_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def delete_rule(conn, rule_id: int) -> None:
    """Remove a rule (management UI). Commits."""
    conn.execute("DELETE FROM transfer_rules WHERE id=?", (rule_id,))
    conn.commit()


def upsert_rule(conn, keyword: str, transfer_account_id: Optional[int], *,
                compound: bool = False,
                account_id: Optional[int] = None,
                amount_min_cents: Optional[int] = None,
                amount_max_cents: Optional[int] = None,
                memo_contains: Optional[str] = None) -> Optional[int]:
    """Create or overwrite the rule for ``keyword`` -> account id; return id.

    ``keyword`` is normalized to a single UPPER-CASED token unless ``compound``
    is set (then its full space-joined form is kept, so a refinement out-ranks
    the broad rule). Returns ``None`` when the keyword is blank or the account is
    ``None`` -- we never store an empty rule. Commits.

    The optional conditions (``account_id`` scope, inclusive signed-cent
    ``amount_min_cents``/``amount_max_cents`` range, case-insensitive
    ``memo_contains`` substring) narrow when the rule fires -- see
    :func:`mammon.keywords.match_rule`. They default to ``None`` (no condition),
    keeping existing keyword-only callers byte-identical. On a fresh row all four
    are written (``None`` -> NULL); when overwriting an existing keyword, only
    conditions explicitly supplied (non-``None``) change, so re-teaching a
    keyword never silently wipes conditions a Rules manager set. ``account_id``
    is the SCOPE (which account the transaction lives in), never confused with
    ``transfer_account_id`` (the payload, the transfer's other side)."""
    if compound:
        kw = " ".join(_tokens(keyword))
    else:
        kw = extract_keyword(keyword) or "".join(_tokens(keyword)[:1])
    kw = (kw or "").upper().strip()
    if not kw or transfer_account_id is None:
        return None
    aid = int(transfer_account_id)
    conds = keywords.normalize_conditions(
        account_id, amount_min_cents, amount_max_cents, memo_contains)
    existing = conn.execute(
        "SELECT id FROM transfer_rules WHERE keyword=?", (kw,)).fetchone()
    if existing is not None:
        sets = ["transfer_account_id=?"]
        vals: list = [aid]
        for col in keywords.CONDITION_COLUMNS:
            if conds[col] is not None:
                sets.append(f"{col}=?")
                vals.append(conds[col])
        sets.append("updated_at=datetime('now')")
        vals.append(int(existing[0]))
        conn.execute(
            f"UPDATE transfer_rules SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
        return int(existing[0])
    cur = conn.execute(
        "INSERT INTO transfer_rules "
        "(keyword, transfer_account_id, match_count, "
        "account_id, amount_min_cents, amount_max_cents, memo_contains, "
        "created_at, updated_at) "
        "VALUES (?,?,0,?,?,?,?, datetime('now'), datetime('now'))",
        (kw, aid, conds["account_id"], conds["amount_min_cents"],
         conds["amount_max_cents"], conds["memo_contains"]))
    conn.commit()
    return int(cur.lastrowid)


def learn_from_edit(conn, desc: str, chosen: Optional[int],
                    *, provisional: Optional[int] = None) -> Optional[int]:
    """Learn/refresh a rule from a user's transfer-account choice; return its id.

    ``desc`` is the raw ``statementDescription``, ``chosen`` the account id the
    user is committing, and ``provisional`` the account Mammon had predicted.
    Learns nothing (returns ``None``) when the chosen account is ``None``, equals
    the provisional (no correction happened), or no keyword can be extracted.

    Keyword-scoped refinement mirrors :func:`mammon.category_rules.learn_from_edit`:
    when the wrong ``provisional`` came from an existing broad rule, a more
    specific compound keyword is learned instead of overwriting that rule."""
    if chosen is None:
        return None
    picked = int(chosen)
    if provisional is not None and picked == int(provisional):
        return None
    if provisional is not None:
        matched = match_rule(desc, load_rules(conn))
        if (matched is not None
                and int(matched["transfer_account_id"]) == int(provisional)
                and int(provisional) != picked):
            refined = refine_keyword(desc, matched["keyword"])
            if refined:
                return upsert_rule(conn, refined, picked, compound=True)
            # No distinguishing token: fall through and overwrite the broad rule.
    keyword = extract_keyword(desc)
    if not keyword:
        return None
    return upsert_rule(conn, keyword, picked)
