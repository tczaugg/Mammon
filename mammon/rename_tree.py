"""Online-learning discriminative tree for payee renaming (the user's design).

Replaces the old flat ``keyword -> payee`` rules table with a discriminative
*trie* that learns from the user's renames and grows only where it must to tell
two merchants apart.

The same machinery runs TWO domains (see ``_DOMAINS``): ``payee`` -- statement
description -> payee rename -- and ``action`` -- a source's raw activity text
("Credit Interest", "RECORDKEEPING FEE") -> Quicken investment action. Both are
the identical problem: an importer's vocabulary guess that only the user's own
corrections can make right.

Pipeline for a raw statement description (+ optional ``extra`` evidence -- the
source's own Payee field, which is EVIDENCE, not a gate):

1. NORMALIZE -- uppercase, strip punctuation into tokens, then DROP pure-numeric
   tokens and very-short noise (:func:`normalize_tokens`), plus mixed
   letter+digit tokens the snapshot has not seen twice (ISINs, auth codes,
   masked ids -- 87% of them occur exactly once in the reference corpus and
   would otherwise root unreachable paths).
2. RANK the surviving tokens by LABEL ENTROPY -- purest first, support as
   tiebreak (:func:`ranked_tokens`). Frequency was the wrong axis: "IAT"
   appears 373 times mapping to 3 payees (highly discriminating) while "VENMO"
   appears 282 times mapping to 73 (discriminates nothing). Measured online
   over the ledger's 8,096 renames, entropy ranking + the dominance gate cut
   wrong auto-renames from 4.2% to 1.8% at identical coverage and shrank
   silence from 38% to 23%. The stats snapshot is built by :func:`bootstrap` /
   :func:`snapshot_stats`; ``learn`` deliberately does NOT mutate it, so the
   ranking a description gets is stable session-to-session (drift is tolerated
   because queries walk by token *presence*, not exact rank -- and a RANKING
   ALGORITHM change rebuilds the trees outright, see ``RANKING_VERSION``).

Learning from a user rename (:func:`learn`): walk the ranked tokens from the
root; for each token --
  * node ABSENT   -> create it, store the payee with count 1 (done);
  * node PRESENT and payee MATCHES one already there -> increment its count (done);
  * node PRESENT and payee DIFFERS -> SPLIT: descend into it and try the next
    (lower-frequency) token, which is precisely the lowest-frequency token that
    differentiates this description from the payees already sitting at the node.
Tokens exhausted -> add the payee to the node's list (several payees now share it).

Suggesting for a NEW description (:func:`suggest`): traverse the same way to the
deepest matching node and read its payees. The matched node's payee CARDINALITY
decides -- a single payee auto-renames, several payees offer a typeable dropdown
(most-frequent first) -- while CONFIDENCE only gates whether we act at all: a node
too deep / too few hits (below :data:`_SUGGEST_FLOOR`) is left unchanged whatever
its cardinality, as is a description that matched nothing. CONFIDENCE is
``f(hit count, depth)`` -- shallow + many hits is high, deep + few is low
(:func:`confidence`).

The tree is BOOTSTRAPPED from register history (:func:`bootstrap`): every posted
non-transfer transaction pairs a raw ``memo`` with a chosen ``payee``, which is
exactly one accepted rename to replay. Per-payee ``applied`` / ``overridden``
tallies (``rename_stats``) drive the management table.

Persistence (schema v15): ``rename_nodes`` (self-referencing token trie),
``rename_node_payees`` (payee + hit count per node), ``rename_token_freq`` (the
frequency snapshot), ``rename_stats`` (applied/overridden per payee), and
``rename_meta`` (bootstrap flag). Every mutator self-commits
unless ``commit=False`` (used to batch a bootstrap into one transaction).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import keywords

__all__ = [
    "normalize_tokens",
    "ranked_tokens",
    "confidence",
    "confidence_tier",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "HIGH_CONFIDENCE_MIN_COUNT",
    "learn",
    "suggest",
    "Suggestion",
    "ACTION_AUTO",
    "ACTION_DROPDOWN",
    "ACTION_LEAVE",
    "note_applied",
    "note_overridden",
    "rename_stats",
    "forget_payee",
    "bootstrap",
    "bootstrap_actions",
    "ensure_bootstrapped",
    "snapshot_frequencies",
    "snapshot_stats",
    "AUTO_PURITY",
    "set_frequencies",
    "children",
    "node_payees",
    "list_nodes",
    "clear",
]

# Split a description into UPPER-CASED alphanumeric tokens.
_TOKEN_SPLIT = re.compile(r"[^A-Z0-9]+")

# Tokens shorter than this are dropped as noise (single letters, "SQ", etc.).
MIN_TOKEN_LEN = 3

ACTION_AUTO = "auto"          # node dominated by one payee -> auto-rename
ACTION_DROPDOWN = "dropdown"  # contested node -> typeable, most-frequent-first dropdown
ACTION_LEAVE = "leave"        # nothing / too weak to trust -> leave the row unchanged

# A node AUTO-renames when its top label holds at least this share of the node's
# hits. Cardinality alone was the old rule (exactly one payee -> auto), which a
# single stray learn at a busy node could permanently demote to a dropdown; a
# dominance threshold recovers once the stray is outvoted.
AUTO_PURITY = 0.9

# The two learned domains. Everything about the tree is identical between them --
# same walk, same SQL shape (the label column is named `payee` in both) -- only
# the tables, the training corpus, and the auto-apply floor differ. `min_count`
# is how many corroborating examples the top label needs before a suggestion is
# HIGH confidence (auto-applied): replayed over the real ledger, payees at 4
# halved wrong auto-renames (4.2% -> 1.8%) at identical coverage, while actions
# -- a finite label set, and a lower-stakes prefill sitting in an editable
# pending row -- are reliable at 2.
_DOMAINS = {
    "payee": {
        "nodes": "rename_nodes", "labels": "rename_node_payees",
        "freq": "rename_token_freq", "meta_key": "bootstrapped",
        "min_count": 4,
    },
    "action": {
        "nodes": "action_nodes", "labels": "action_node_labels",
        "freq": "action_token_freq", "meta_key": "action_bootstrapped",
        "min_count": 2,
    },
}

# Below this confidence a matched node is too weak to trust (too deep / too few
# hits) -> leave the row unchanged, whatever its payee cardinality. A lone hit at
# depth 2 (1/3 = 0.33) is the canonical "too weak" case.
_SUGGEST_FLOOR = 0.34

# Confidence TIERS (distinct from the raw confidence float). A payee is only
# 'high' confidence once at least this many renames corroborate it at the matched
# node -- a single example is never high, so callers show the raw statement
# description rather than silently renaming on one prior sighting (by request).
# Raised from 2 to 4 on measurement: replaying the ledger's 8,096 renames online,
# 4 cut wrong auto-renames from 5.7% to 1.8% while entropy ranking kept auto
# coverage identical (the extra examples come from generalizing better, not from
# acting less). The ACTION domain stays at 2 (see _DOMAINS). 'high' additionally
# requires clearing the suggest floor and AUTO_PURITY.
CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"
HIGH_CONFIDENCE_MIN_COUNT = 4

# Known bank boilerplate (POS, DEBIT, ACH, ...) is the least discriminating text
# there is -- it appears on nearly every statement. Inverse-frequency ranking
# demotes it once a snapshot exists, but at COLD START the snapshot is empty and
# boilerplate would otherwise lead (it prints first). Treat these tokens as if
# maximally frequent so they always sort last, so a real merchant token wins.
_NOISE_FLOOR = 10 ** 9


# ---------------------------------------------------------------------------
# normalization + ranking
# ---------------------------------------------------------------------------
def normalize_tokens(desc: str) -> list[str]:
    """UPPER-CASED tokens of ``desc`` with numeric/short noise dropped.

    Splits on non-alphanumerics, then discards pure-numeric tokens (auth codes,
    store/check numbers) and tokens shorter than :data:`MIN_TOKEN_LEN`, so a
    per-transaction number can never overfit into its own tree node. Duplicates
    are collapsed, first occurrence order preserved.
    """
    out: list[str] = []
    seen: set[str] = set()
    for t in _TOKEN_SPLIT.split((desc or "").upper()):
        if not t or t in seen:
            continue
        if t.isdigit():          # pure-numeric noise
            continue
        if len(t) < MIN_TOKEN_LEN:  # very-short noise
            continue
        seen.add(t)
        out.append(t)
    return out


def _stats_map(conn, toks: list[str], kind: str) -> dict:
    """{token: (freq, entropy)} from the domain's snapshot; absent -> missing."""
    if not toks:
        return {}
    dom = _DOMAINS[kind]
    placeholders = ",".join("?" * len(toks))
    rows = conn.execute(
        "SELECT token, freq, entropy FROM %s WHERE token IN (%s)"
        % (dom["freq"], placeholders),
        toks,
    ).fetchall()
    return {r["token"]: (int(r["freq"]),
                         None if r["entropy"] is None else float(r["entropy"]))
            for r in rows}


def _is_mixed(tok: str) -> bool:
    """A token mixing letters and digits (ISIN, auth code, masked id)."""
    return any(ch.isdigit() for ch in tok) and any(ch.isalpha() for ch in tok)


def ranked_tokens(conn, desc: str, *, kind: str = "payee", extra: str = "") -> list[str]:
    """Evidence tokens of ``desc`` (and ``extra``) ordered most-discriminating
    first.

    ``extra`` is additional evidence text -- typically the source's own Payee
    field -- tokenized into the same pool. It is evidence, not a gate: the
    earlier design used a supplied payee verbatim and skipped the tree entirely,
    which stopped renaming even when the field carried extractable junk.

    Two measured corrections to the old inverse-global-frequency ranking:

    * SHAPE FILTER -- mixed letter+digit tokens not seen at least twice in the
      snapshot are dropped. In the real corpus 87% of them occur exactly once
      (ISINs, auth codes, masked ids like ``x*****99``); being rare, the old
      ranking put them FIRST, so learned paths were rooted in tokens that never
      recur and were unreachable for every later row.
    * ENTROPY RANK -- tokens sort by conditional label entropy (purest first,
      support as tiebreak), not by rarity. Frequency is the wrong axis: ``IAT``
      appears 373 times mapping to 3 payees (keep it early), ``VENMO`` 282
      times mapping to 73 payees (it discriminates nothing). An unseen token
      ranks with the pure ones but after supported ones -- it may be a brand-new
      merchant name. Known boilerplate still sorts last.
    """
    toks = normalize_tokens(desc)
    if extra:
        seen = set(toks)
        for tok in normalize_tokens(extra):
            if tok not in seen:
                seen.add(tok)
                toks.append(tok)
    if not toks:
        return []
    stats = _stats_map(conn, toks, kind)
    toks = [tk for tk in toks
            if not (_is_mixed(tk) and stats.get(tk, (0, None))[0] < 2)]
    order = {tk: i for i, tk in enumerate(toks)}

    def rank(tk: str):
        freq, ent = stats.get(tk, (0, None))
        h = 0.0 if ent is None else ent
        if tk in keywords._NOISE:
            h = max(h, float(_NOISE_FLOOR))   # boilerplate always sorts last
        return (h, -min(freq, 50), order[tk])

    return sorted(toks, key=rank)


def confidence(hit_count: int, depth: int) -> float:
    """Confidence of a payee suggestion from its hit count and node depth.

    ``f(hits, depth) = hits / (hits + depth)``: rises with corroborating hits,
    falls with depth, so a shallow node backed by many renames scores high while
    a deep node reached through many splits with a single hit scores low.
    """
    hc = max(0, int(hit_count))
    if hc <= 0:
        return 0.0
    d = max(1, int(depth))
    return hc / (hc + d)


def confidence_tier(hit_count: int, depth: int, *, floor: Optional[float] = None) -> str:
    """Classify a suggestion as :data:`CONFIDENCE_HIGH` or :data:`CONFIDENCE_LOW`.

    A suggestion is only 'high' when it is corroborated by at least
    :data:`HIGH_CONFIDENCE_MIN_COUNT` renames at the matched node AND its
    :func:`confidence` clears ``floor`` (default :data:`_SUGGEST_FLOOR`). A lone
    example (count 1) is therefore never high, however shallow the node -- so a
    single prior rename does not auto-apply; the raw statement description is
    shown instead."""
    fl = _SUGGEST_FLOOR if floor is None else float(floor)
    if int(hit_count) >= HIGH_CONFIDENCE_MIN_COUNT and confidence(hit_count, depth) >= fl:
        return CONFIDENCE_HIGH
    return CONFIDENCE_LOW


# ---------------------------------------------------------------------------
# node helpers
# ---------------------------------------------------------------------------
def _find_child(conn, parent_id: Optional[int], token: str, kind: str = "payee"):
    return conn.execute(
        "SELECT id, depth FROM %s "
        "WHERE token=? AND IFNULL(parent_id,-1)=IFNULL(?,-1)" % _DOMAINS[kind]["nodes"],
        (token, parent_id),
    ).fetchone()


def children(conn, parent_id: Optional[int], *, kind: str = "payee") -> list[dict]:
    """Child nodes of ``parent_id`` (pass ``None`` for the root level)."""
    rows = conn.execute(
        "SELECT id, parent_id, token, depth FROM %s "
        "WHERE IFNULL(parent_id,-1)=IFNULL(?,-1) ORDER BY token"
        % _DOMAINS[kind]["nodes"],
        (parent_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def node_payees(conn, node_id: int, *, kind: str = "payee") -> list[dict]:
    """Labels stored at ``node_id`` with their hit counts, most-hit first."""
    rows = conn.execute(
        "SELECT payee, count FROM %s WHERE node_id=? "
        "ORDER BY count DESC, payee ASC" % _DOMAINS[kind]["labels"],
        (node_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_nodes(conn, *, kind: str = "payee") -> list[dict]:
    """Every node (shallowest first) -- inspection/debugging helper."""
    rows = conn.execute(
        "SELECT id, parent_id, token, depth FROM %s ORDER BY depth, id"
        % _DOMAINS[kind]["nodes"]
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------
def learn(conn, desc: str, payee: str, *, kind: str = "payee",
          extra: str = "", commit: bool = True) -> bool:
    """Teach the ``kind`` tree that ``desc`` (+ ``extra`` evidence) maps to
    ``payee`` -- one accepted correction. For the action domain the label is the
    Quicken ACTION; the parameter keeps its name so both domains share one shape.

    Returns ``True`` when something was recorded, ``False`` when the label is
    blank or the evidence yields no usable tokens (nothing to learn). See the
    module docstring for the absent / matches / differs walk.
    """
    dom = _DOMAINS[kind]
    picked = (payee or "").strip()
    if not picked:
        return False
    toks = ranked_tokens(conn, desc, kind=kind, extra=extra)
    if not toks:
        return False
    parent_id: Optional[int] = None
    for depth, tok in enumerate(toks, start=1):
        node = _find_child(conn, parent_id, tok, kind)
        if node is None:
            # ABSENT: create the node, store this label with count 1.
            cur = conn.execute(
                "INSERT INTO %s(parent_id, token, depth) VALUES (?,?,?)"
                % dom["nodes"],
                (parent_id, tok, depth),
            )
            conn.execute(
                "INSERT INTO %s(node_id, payee, count) VALUES (?,?,1)"
                % dom["labels"],
                (int(cur.lastrowid), picked),
            )
            if commit:
                conn.commit()
            return True
        node_id = int(node["id"])
        hit = conn.execute(
            "SELECT id FROM %s WHERE node_id=? AND payee=?" % dom["labels"],
            (node_id, picked),
        ).fetchone()
        if hit is not None:
            # PRESENT and label MATCHES: reinforce.
            conn.execute(
                "UPDATE %s SET count=count+1 WHERE id=?" % dom["labels"],
                (int(hit["id"]),),
            )
            if commit:
                conn.commit()
            return True
        # PRESENT but label DIFFERS: split on the next-ranked token.
        parent_id = node_id
    # Tokens exhausted with no match -> this node now serves several labels.
    conn.execute(
        "INSERT INTO %s(node_id, payee, count) VALUES (?,?,1) "
        "ON CONFLICT(node_id, payee) DO UPDATE SET count=count+1" % dom["labels"],
        (parent_id, picked),
    )
    if commit:
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# suggesting
# ---------------------------------------------------------------------------
@dataclass
class Suggestion:
    """A rename decision for one description.

    ``action`` is one of :data:`ACTION_AUTO`, :data:`ACTION_DROPDOWN`,
    :data:`ACTION_LEAVE`. ``payee`` is the top candidate (the auto payee, or the
    default pre-fill for a dropdown). ``candidates`` is ``[(payee, count), ...]``
    most-hit first (empty for LEAVE). ``confidence`` and ``depth`` describe the
    matched node.
    """

    action: str
    payee: str = ""
    candidates: list = field(default_factory=list)
    confidence: float = 0.0
    depth: int = 0
    high_confidence: bool = False   # >=2 corroborating hits (see confidence_tier)

    @property
    def payees(self) -> list:
        """Just the candidate payee names, most-hit first."""
        return [p for p, _ in self.candidates]

    @property
    def tier(self) -> str:
        """:data:`CONFIDENCE_HIGH` or :data:`CONFIDENCE_LOW` for this suggestion."""
        return CONFIDENCE_HIGH if self.high_confidence else CONFIDENCE_LOW


def _walk(conn, toks: list[str], kind: str = "payee") -> tuple[Optional[int], int]:
    """Deepest node reached by following ``toks`` from the root by presence."""
    parent_id: Optional[int] = None
    depth = 0
    reached: Optional[int] = None
    for tok in toks:
        node = _find_child(conn, parent_id, tok, kind)
        if node is None:
            break
        parent_id = int(node["id"])
        depth += 1
        reached = parent_id
    return reached, depth


def suggest(conn, desc: str, *, kind: str = "payee", extra: str = "",
            floor: Optional[float] = None) -> Suggestion:
    """Decide how to map ``desc`` (+ ``extra`` evidence) by NODE DOMINANCE.

    Walk the trie to the deepest matching node and read its labels:

      * top label holds >= :data:`AUTO_PURITY` of the node's hits -> ACTION_AUTO.
        (A lone label is purity 1.0, so the old cardinality rule is a special
        case -- but a busy node with one stray learn can now recover once the
        stray is outvoted, instead of being demoted to a dropdown forever.)
      * contested node -> ACTION_DROPDOWN (typeable, most-frequent first).

    Confidence gates whether we act at all: a node too weak to trust -- too deep
    and/or too few hits, confidence below ``floor`` (default
    :data:`_SUGGEST_FLOOR`) -- is left unchanged, as is a description that
    matched no node. ``high_confidence`` additionally demands the domain's
    ``min_count`` corroborating examples AND dominance; auto-appliers gate on it.
    """
    dom = _DOMAINS[kind]
    fl = _SUGGEST_FLOOR if floor is None else float(floor)
    toks = ranked_tokens(conn, desc, kind=kind, extra=extra)
    node_id, depth = _walk(conn, toks, kind)
    if node_id is None:
        return Suggestion(ACTION_LEAVE)
    cands = [(r["payee"], int(r["count"]))
             for r in node_payees(conn, node_id, kind=kind)]
    if not cands:
        return Suggestion(ACTION_LEAVE, depth=depth)
    top_payee, top_count = cands[0]
    total = sum(n for _, n in cands)
    purity = top_count / total if total else 0.0
    conf = confidence(top_count, depth)
    high = (top_count >= dom["min_count"] and purity >= AUTO_PURITY
            and conf >= fl)
    # Too weak to act on at all -> leave unchanged (deep node / too few hits).
    if conf < fl:
        return Suggestion(ACTION_LEAVE, payee=top_payee, candidates=cands,
                          confidence=conf, depth=depth, high_confidence=high)
    if purity >= AUTO_PURITY:
        return Suggestion(ACTION_AUTO, payee=top_payee, candidates=cands,
                          confidence=conf, depth=depth, high_confidence=high)
    return Suggestion(ACTION_DROPDOWN, payee=top_payee, candidates=cands,
                      confidence=conf, depth=depth, high_confidence=high)


# ---------------------------------------------------------------------------
# per-payee applied / overridden tallies (management table)
# ---------------------------------------------------------------------------
def note_applied(conn, payee: str, *, commit: bool = True) -> None:
    """Record that a suggested rename to ``payee`` was accepted (auto or picked)."""
    p = (payee or "").strip()
    if not p:
        return
    conn.execute(
        "INSERT INTO rename_stats(payee, applied_count, overridden_count) VALUES(?,1,0) "
        "ON CONFLICT(payee) DO UPDATE SET applied_count=applied_count+1",
        (p,),
    )
    if commit:
        conn.commit()


def note_overridden(conn, payee: str, *, commit: bool = True) -> None:
    """Record that a suggested rename to ``payee`` was rejected for another name."""
    p = (payee or "").strip()
    if not p:
        return
    conn.execute(
        "INSERT INTO rename_stats(payee, applied_count, overridden_count) VALUES(?,0,1) "
        "ON CONFLICT(payee) DO UPDATE SET overridden_count=overridden_count+1",
        (p,),
    )
    if commit:
        conn.commit()


def rename_stats(conn) -> list[dict]:
    """One row per known payee for the management table.

    Each row: ``payee``, ``examples`` (total learned hits across the tree),
    ``nodes`` (how many tree nodes it appears at), ``applied`` and ``overridden``
    tallies. Sorted by payee name.
    """
    learned: dict = {}
    for r in conn.execute(
        "SELECT payee, SUM(count) AS c, COUNT(*) AS n "
        "FROM rename_node_payees GROUP BY payee"
    ):
        learned[r["payee"]] = (int(r["c"] or 0), int(r["n"] or 0))
    stats: dict = {}
    for r in conn.execute(
        "SELECT payee, applied_count, overridden_count FROM rename_stats"
    ):
        stats[r["payee"]] = (int(r["applied_count"] or 0), int(r["overridden_count"] or 0))
    out = []
    for p in sorted(set(learned) | set(stats)):
        examples, nodes = learned.get(p, (0, 0))
        applied, overridden = stats.get(p, (0, 0))
        out.append({
            "payee": p,
            "examples": examples,
            "nodes": nodes,
            "applied": applied,
            "overridden": overridden,
        })
    return out


def forget_payee(conn, payee: str, *, commit: bool = True) -> int:
    """Delete a payee from the whole tree and drop its stats (management delete).

    Removes the payee from every node, prunes nodes left empty, and clears its
    applied/overridden tally. Returns the number of node entries removed.
    """
    p = (payee or "").strip()
    if not p:
        return 0
    removed = conn.execute(
        "DELETE FROM rename_node_payees WHERE payee=?", (p,)
    ).rowcount
    _prune_empty(conn)
    conn.execute("DELETE FROM rename_stats WHERE payee=?", (p,))
    if commit:
        conn.commit()
    return int(removed or 0)


def _prune_empty(conn) -> None:
    """Delete leaf nodes that have no payees and no children, repeatedly."""
    while True:
        cur = conn.execute(
            "DELETE FROM rename_nodes WHERE id IN ("
            "  SELECT n.id FROM rename_nodes n "
            "  LEFT JOIN rename_node_payees p ON p.node_id=n.id "
            "  LEFT JOIN rename_nodes c ON c.parent_id=n.id "
            "  WHERE p.id IS NULL AND c.id IS NULL)"
        )
        if not cur.rowcount:
            break


def sources_for(conn, payee: str, *, kind: str = "payee", limit: int = 40) -> list:
    """The token paths that resolve to ``payee`` -- the raw statement
    vocabulary it was learned from, each as a space-joined string.

    The tree is the only place that knows what a payee was renamed FROM, so
    anything wanting the bank's own words for a payee (the automatic-payment
    hint in :mod:`mammon.predictions`, say) asks here rather than guessing
    from the payee's own name.
    """
    d = _DOMAINS[kind]
    rows = conn.execute(
        f"SELECT l.node_id FROM {d['labels']} l WHERE l.payee=? "
        f"ORDER BY l.count DESC LIMIT ?", (payee, int(limit))).fetchall()
    out: list = []
    for r in rows:
        tokens: list = []
        node_id = r["node_id"]
        guard = 0
        while node_id is not None and guard < 32:
            n = conn.execute(f"SELECT token, parent_id FROM {d['nodes']} WHERE id=?",
                             (node_id,)).fetchone()
            if n is None:
                break
            tokens.append(n["token"])
            node_id = n["parent_id"]
            guard += 1
        if tokens:
            out.append(" ".join(reversed(tokens)))
    return out


def clear(conn, *, kind: str = "payee", commit: bool = True) -> None:
    """Wipe one learned tree, its stats, and its snapshot (reset)."""
    dom = _DOMAINS[kind]
    conn.execute("DELETE FROM %s" % dom["labels"])
    conn.execute("DELETE FROM %s" % dom["nodes"])
    if kind == "payee":
        conn.execute("DELETE FROM rename_stats")
    conn.execute("DELETE FROM %s" % dom["freq"])
    conn.execute("DELETE FROM rename_meta WHERE key=?", (dom["meta_key"],))
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# frequency snapshot
# ---------------------------------------------------------------------------
def set_frequencies(conn, mapping, *, kind: str = "payee",
                    commit: bool = True) -> None:
    """Overwrite the token snapshot from ``{token: freq}`` (explicit; tests).

    Entropy is left NULL -- unknown -- so ranking treats every token as pure and
    falls back to support + first-position, which keeps hand-built fixtures
    deterministic."""
    dom = _DOMAINS[kind]
    conn.execute("DELETE FROM %s" % dom["freq"])
    conn.executemany(
        "INSERT INTO %s(token, freq) VALUES(?,?)" % dom["freq"],
        [(str(t).upper(), int(f)) for t, f in dict(mapping).items()],
    )
    if commit:
        conn.commit()


# The labeled corpus behind each domain's snapshot: (evidence text, label) pairs.
_CORPUS_SQL = {
    "payee": (
        "SELECT memo AS txt, payee AS label FROM transactions "
        "WHERE memo IS NOT NULL AND TRIM(memo) <> '' "
        "  AND payee IS NOT NULL AND TRIM(payee) <> '' "
        "  AND transfer_account_id IS NULL ORDER BY id ASC"
    ),
    "action": (
        "SELECT memo AS txt, action AS label FROM investment_transactions "
        "WHERE memo IS NOT NULL AND TRIM(memo) <> '' "
        "  AND action IS NOT NULL AND TRIM(action) <> '' ORDER BY id ASC"
    ),
}


def snapshot_stats(conn, *, kind: str = "payee", commit: bool = True) -> int:
    """Rebuild the token snapshot -- document frequency AND label entropy -- from
    the domain's labeled corpus. Returns the number of distinct tokens.

    Entropy is H(label | token) over the corpus: 0 for a token that always
    co-occurs with one label (maximally discriminating -- ranks first however
    frequent it is), rising as the token spreads across labels. This is the
    partition the ranking sorts by; the trie itself stays an online learner, so
    like the old frequency snapshot this is refreshed at bootstrap, not on every
    learn -- queries tolerate drift because the walk is by token presence."""
    import math

    dom = _DOMAINS[kind]
    freq: dict = {}
    labels: dict = {}
    for r in conn.execute(_CORPUS_SQL[kind]):
        for tok in set(normalize_tokens(r["txt"])):
            freq[tok] = freq.get(tok, 0) + 1
            d = labels.setdefault(tok, {})
            d[r["label"]] = d.get(r["label"], 0) + 1
    import math as _m
    rows = []
    for tok, f in freq.items():
        d = labels[tok]
        n = sum(d.values())
        h = -sum((v / n) * _m.log2(v / n) for v in d.values()) if n else 0.0
        rows.append((tok, f, h))
    conn.execute("DELETE FROM %s" % dom["freq"])
    conn.executemany(
        "INSERT INTO %s(token, freq, entropy) VALUES(?,?,?)" % dom["freq"], rows
    )
    if commit:
        conn.commit()
    return len(rows)


def snapshot_frequencies(conn, *, commit: bool = True) -> int:
    """Back-compat alias: rebuild the PAYEE snapshot (now with entropy)."""
    return snapshot_stats(conn, kind="payee", commit=commit)


# ---------------------------------------------------------------------------
# bootstrap from register history
# ---------------------------------------------------------------------------
def bootstrap(conn, *, force: bool = False) -> int:
    """Replay accepted renames from register history into the tree (idempotent).

    A posted, non-transfer transaction pairs a raw ``memo`` with a chosen
    ``payee`` -- one accepted rename to replay. Builds the frequency snapshot
    first (so ranking reflects the real corpus), then learns each pair oldest
    first. Guarded by a meta flag: a second call is a no-op unless ``force``.
    Returns the number of renames replayed.
    """
    return _bootstrap_kind(conn, "payee", force=force)


def _bootstrap_kind(conn, kind: str, *, force: bool = False) -> int:
    dom = _DOMAINS[kind]
    if not force and _meta_get(conn, dom["meta_key"]) == "1":
        return 0
    snapshot_stats(conn, kind=kind, commit=False)
    rows = conn.execute(_CORPUS_SQL[kind]).fetchall()
    n = 0
    for r in rows:
        if learn(conn, r["txt"], r["label"], kind=kind, commit=False):
            n += 1
    _meta_set(conn, dom["meta_key"], "1", commit=False)
    conn.commit()
    return n


def bootstrap_actions(conn, *, force: bool = False) -> int:
    """Replay register history into the ACTION tree (idempotent) -- every posted
    investment row pairs a source description with the action the user kept,
    which is one accepted mapping to replay. 5,619 such rows in the reference
    ledger reach 88% correct auto-mapping before the first manual correction."""
    return _bootstrap_kind(conn, "action", force=force)


# Bumped whenever the RANKING algorithm changes shape. A trie's paths are laid
# down in ranking order and walked in ranking order, so a tree built under one
# ranking is silently unreachable under another -- after the entropy rewrite, a
# ledger's existing payee tree answered LEAVE for descriptions it had learned
# hundreds of times. The version marker forces a one-time rebuild from register
# history, which loses nothing: history IS the training set.
RANKING_VERSION = "2"


def ensure_bootstrapped(conn) -> int:
    """Run both bootstraps once; a no-op after the first time. Cheap to call on
    every app launch (two meta lookups once the flags are set). A ranking
    algorithm change rebuilds both trees from history, once."""
    if _meta_get(conn, "ranking_version") != RANKING_VERSION:
        clear(conn, kind="payee", commit=False)
        clear(conn, kind="action", commit=False)
        n = bootstrap(conn, force=True) + bootstrap_actions(conn, force=True)
        _meta_set(conn, "ranking_version", RANKING_VERSION)
        return n
    return bootstrap(conn, force=False) + bootstrap_actions(conn, force=False)


# ---------------------------------------------------------------------------
# meta: bootstrap flag
# ---------------------------------------------------------------------------
def _meta_get(conn, key: str, default=None):
    row = conn.execute(
        "SELECT value FROM rename_meta WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def _meta_set(conn, key: str, value, *, commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO rename_meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    if commit:
        conn.commit()


