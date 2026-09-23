"""Shared keyword/tokenizer primitives for Mammon's learned-rule engines.

Raw bank ``statementDescription`` text is gobbledygook ("POS DEBIT 1234
AMAZON.COM SEATTLE WA"). The category- and transfer-learning engines
(:mod:`mammon.category_rules`, :mod:`mammon.transfer_rules`) each learn a
``keyword -> value`` rule keyed on a distinguishing UPPER-CASED token pulled
from that raw text, matched whole-token. This module is the tokenizer and
keyword-extraction machinery those engines share so they stay in lock-step.

Historically this code lived in ``mammon.payee_rules``; payee *renaming* has since
moved to the online discriminative tree in :mod:`mammon.rename_tree`, but the
keyword machinery is still the right primitive for the category/transfer rule
tables, so it lives here as a neutral, table-agnostic module.
"""
from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "extract_keyword",
    "refine_keyword",
    "match_rule",
    "normalize_conditions",
    "CONDITION_COLUMNS",
]

# Optional per-rule conditions shared by the category- and transfer-rule engines
# (roadmap item 10). A rule may carry any subset of these; a NULL/absent column
# means "no such condition". They are stored as real columns on both rule tables
# and evaluated by :func:`match_rule` only when the caller supplies transaction
# context -- so a rule with all-NULL conditions, or any match made without
# context, behaves byte-identically to a plain keyword rule.
CONDITION_COLUMNS = (
    "account_id",        # scope: the account a transaction must live in
    "amount_min_cents",  # inclusive lower bound, SIGNED cents (negative = out)
    "amount_max_cents",  # inclusive upper bound, SIGNED cents
    "memo_contains",     # case-insensitive substring of the memo
)

# Non-merchant filler that shows up in bank feeds. Kept UPPER-CASED; a token in
# this set is never chosen as a rule keyword. Deliberately conservative -- we'd
# rather keep an odd token than eat a real merchant name.
_NOISE = {
    "POS", "ACH", "ATM", "DEBIT", "CREDIT", "PURCHASE", "PAYMENT", "PMT",
    "WITHDRAWAL", "DEPOSIT", "AUTOMATIC", "CARD", "CHECKCARD", "CHECK", "VISA",
    "MASTERCARD",
    "TRANSACTION", "TRANS", "REF", "AUTH", "RECURRING", "ONLINE", "MOBILE",
    "BILLPAY", "BILL", "PAY", "WEB", "PPD", "CCD", "EFT", "DDA", "PIN", "SEC",
    "IAT", "TEL", "DES", "INDN", "THE", "AND", "FOR", "USA", "COM", "WWW", "LLC",
    "INC", "XXXX", "XXXXX", "XXXXXX", "NUM", "ID", "NO",
}

# Split a description into UPPER-CASED alphanumeric tokens.
_TOKEN_SPLIT = re.compile(r"[^A-Z0-9]+")

# Minimum length for a token to be considered a distinguishing keyword.
_MIN_KEYWORD_LEN = 3


def _tokens(desc: str) -> list[str]:
    """UPPER-CASED alphanumeric tokens of ``desc`` (empties dropped)."""
    return [t for t in _TOKEN_SPLIT.split((desc or "").upper()) if t]


def extract_keyword(desc: str) -> str:
    """Pull the single most distinguishing keyword from a raw description.

    Bank feeds lead with the merchant name and trail with card/date noise and
    city/state, so the FIRST meaningful token is the distinguishing one. Prefers
    the first purely-alphabetic token (>= 3 chars) that is not bank filler; falls
    back to the first non-noise token (allowing digits) when no clean word
    qualifies. Returns ``""`` when nothing usable remains -- callers then learn
    nothing. (Choosing "first" over "longest" avoids picking a trailing city
    like SEATTLE over a merchant like AMAZON.)
    """
    toks = _tokens(desc)
    if not toks:
        return ""
    for t in toks:
        if t.isalpha() and len(t) >= _MIN_KEYWORD_LEN and t not in _NOISE:
            return t
    # fallback: an alphanumeric merchant code (e.g. 7ELEVEN, H2O) -- but never a
    # pure number (check numbers / amounts make useless keywords).
    for t in toks:
        if (len(t) >= _MIN_KEYWORD_LEN and t not in _NOISE
                and any(c.isalpha() for c in t)):
            return t
    return ""


def refine_keyword(desc: str, base_keyword: str) -> Optional[str]:
    """A MORE specific keyword than ``base_keyword`` for ``desc``, or ``None``.

    Used to differentiate a one-off correction from a good broad rule: instead
    of overwriting ``base_keyword`` -> old value, we learn a compound keyword
    (``base_keyword`` plus the first distinguishing token of ``desc``) that
    out-ranks the broad rule via the longest-keyword-wins ordering. Returns
    ``None`` when ``desc`` has no distinguishing token to add -- the caller then
    falls back to overwriting the broad rule.
    """
    base = (base_keyword or "").upper().split()
    if not base:
        return None
    baseset = set(base)
    for t in _tokens(desc):
        if t in baseset:
            continue
        if len(t) < _MIN_KEYWORD_LEN or t in _NOISE or not any(c.isalpha() for c in t):
            continue
        return " ".join(base + [t])
    return None


def normalize_conditions(account_id=None, amount_min_cents=None,
                         amount_max_cents=None, memo_contains=None) -> dict:
    """Coerce optional rule-condition inputs to their stored forms (or ``None``).

    Integer columns are cast to ``int`` (so a stray Decimal/str cannot poison the
    row); ``memo_contains`` is stripped and a blank collapses to ``None`` -- an
    empty substring must not become an always-false condition. Returns a dict
    keyed by :data:`CONDITION_COLUMNS`, suitable for direct binding.
    """
    def _int_or_none(v):
        return None if v is None else int(v)

    memo = memo_contains
    if memo is not None:
        memo = str(memo).strip() or None
    return {
        "account_id": _int_or_none(account_id),
        "amount_min_cents": _int_or_none(amount_min_cents),
        "amount_max_cents": _int_or_none(amount_max_cents),
        "memo_contains": memo,
    }


def _ctx_get(context, key):
    """Read ``key`` from a mapping- or attribute-style context (``None`` if
    absent)."""
    try:
        return context.get(key)
    except AttributeError:
        return getattr(context, key, None)


def _conditions_hold(rule, context) -> bool:
    """True when every non-NULL condition on ``rule`` holds for ``context``.

    ``context`` carries the transaction being classified and may expose
    ``account_id``, ``amount_cents`` and ``memo`` (as a mapping or by attribute).
    A NULL/absent condition column is ignored; a *set* condition whose context
    field is missing/``None`` fails, so a scoped rule never fires on a row it
    cannot positively confirm belongs to it. Amounts compare as SIGNED integer
    cents (negative = money out) over an inclusive range.
    """
    acct = rule.get("account_id")
    if acct is not None:
        ctx_acct = _ctx_get(context, "account_id")
        if ctx_acct is None or int(ctx_acct) != int(acct):
            return False

    amin = rule.get("amount_min_cents")
    amax = rule.get("amount_max_cents")
    if amin is not None or amax is not None:
        amt = _ctx_get(context, "amount_cents")
        if amt is None:
            return False
        amt = int(amt)
        if amin is not None and amt < int(amin):
            return False
        if amax is not None and amt > int(amax):
            return False

    memo_sub = rule.get("memo_contains")
    if memo_sub:
        memo = _ctx_get(context, "memo")
        if not memo or str(memo_sub).lower() not in str(memo).lower():
            return False

    return True


def match_rule(desc: str, rules, *, context=None) -> Optional[dict]:
    """First rule whose keyword tokens are ALL whole tokens of ``desc`` (else
    ``None``).

    ``rules`` is an iterable of dicts with a ``keyword`` key; pass them ordered
    most-specific first (longest keyword first) so a longer / multi-word keyword
    wins over a shorter one.

    A keyword may be a single token ("AMAZON") or a space-joined refinement
    ("AMAZON WEB"); the rule fires only when *every* keyword token is present as
    a whole token of ``desc``. This stays whole-token (so "CAT" never matches
    "CATERPILLAR") and lets a refinement out-rank the broad rule it narrows.

    ``context`` is optional transaction context (a mapping/object exposing
    ``account_id``, ``amount_cents`` and/or ``memo``). When ``None`` -- the
    default, and what every pre-existing caller passes -- rule conditions are
    NOT consulted and matching is byte-identical to the keyword-only behavior.
    When supplied, a candidate rule additionally has to satisfy each of its
    non-NULL conditions (:func:`_conditions_hold`); a rule that fails a condition
    is skipped and matching continues, so a narrowly-conditioned keyword can fall
    through to a broader rule.
    """
    toks = set(_tokens(desc))
    if not toks:
        return None
    for r in rules:
        parts = (r.get("keyword") or "").upper().split()
        if not (parts and all(p in toks for p in parts)):
            continue
        if context is not None and not _conditions_hold(r, context):
            continue
        return r
    return None
