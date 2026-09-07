"""Per-payee discriminative tree for auto-categorization (the user's design).

Replaces the flat, GLOBAL ``keyword -> category_id`` table in
:mod:`mammon.category_rules` as the auto-fill source. That table learned a rule
from a SINGLE correction, keyed on the first non-noise token of the raw text,
and matched it against every merchant. Replayed over a new user's first year
(625 accepted review rows, no history, predicting each row before learning it)
it fired on 67% of rows, was wrong on 26% of those, and **half of the errors
proposed a category that payee had never carried** -- a rule learned from one
merchant firing on an unrelated one:

    SUBWAY 61276 ANYTOWN UT       kw=ANYTOWN  -> Utilities:Gas & Electric
    O'REILLY 2988 ANYTOWN UT      kw=ANYTOWN  -> Utilities:Gas & Electric
    GOOGLE *Chrome MOUNTAIN VIEW CA    kw=MOUNTAIN -> Medical:Doctor
    April 2026 Rent (Anyplace)        kw=APRIL    -> Gift Received

The town in the tail of every local card swipe had become a Utilities rule.

The fix is scope. Category is resolved AFTER the payee (:mod:`mammon.rename_tree`
runs first), and every decision here hangs below that payee, so a candidate can
only ever be a category that payee has actually carried. On the same corpus this
proposes a never-seen category exactly zero times, by construction rather than by
tuning -- :func:`suggest` reads vote counts that are themselves keyed by payee.

The keyword table is not gone, but it no longer learns: migration 57 purged every
row the old learner had written, so what is left there is what the user typed
into the Rules manager. :func:`mammon.import_review.predict_fields` consults it
only after this tree declines, and honours it without the payee filter -- an
instruction the user wrote down is not the system guessing at them.

Structure
---------
One trie per normalized payee (``category_nodes``, one root per ``payee_key``),
grown exactly the way the rename tree grows: walk the source text's tokens
ranked by label entropy; a node whose category matches increments, a node whose
category differs SPLITS and descends on the next token -- which is the lowest-
entropy token that tells this description apart from what already sits there.
So "Costco" grows a ``GAS`` child under its root and nothing else needs to be
said:

    COSTCO GAS #1234 ANYTOWN UT   -> Auto:Fuel   (confident, depth 1)
    COSTCO WHSE #1234 ANYTOWN UT  -> Groceries   (confident, depth 0)

Two gates, and both are load-bearing
------------------------------------
* **Node purity** (:data:`AUTO_PURITY`, :data:`MIN_COUNT`) asks *does this
  description discriminate?* -- the rename tree's gate, unchanged.
* **Payee coherence** (:data:`PAYEE_COHERENCE`) asks *is this merchant
  categorizable at all?* A node only accumulates the categories that STOPPED
  there; rows diverted into a child are not counted, so a marketplace payee
  whose every item description is unique looks perfectly pure at its root and
  auto-fills its most common category onto everything. Measured: 61 of the 67
  errors the node gate alone allowed were Amazon (51 categories in the real
  ledger -- it is not a merchant, it is a catalogue). Requiring the payee's own
  dominant category to hold :data:`PAYEE_COHERENCE` of everything ever seen for
  it took precision from 77% to 96% (67 wrong -> 6) on the same 625 rows, and
  correctly demotes Amazon to dropdown-only forever.

The rows that fail either gate are not guesses to be improved -- they are the
cases the user must decide. They get a blank category and a RANKED dropdown
(:func:`ranked_categories`): the matched node's categories first, then the rest
of that payee's, and the caller appends everything else alphabetically. Replayed
cold over the real ledger's first year (625 accepted rows, predicting each before
learning it) this auto-fills 32% at 95% precision with ZERO never-seen
proposals -- against the keyword table's 67% fire rate at 74% precision, half of
whose errors were a category from an unrelated merchant -- and on the rows left
blank the promoted first entry was the right answer 69% of the time.

Source text
-----------
The discriminating text arrives in DIFFERENT fields depending on the importer.
Wells Fargo sends a ``statementDescription`` (review ``memo``, ``payee_supplied``
0); the Costco card sends no description at all and puts the same text in the
payee field (``payee_supplied`` 1, ``memo`` empty). A tree reading only one of
them is blind on the other -- and on the Costco card all three of the old
category mechanisms were structurally dead, because each begins by tokenizing an
empty string. :func:`source_text` is the one place that picks, so both shapes
train and query the same tree.

Investment rows never reach here. ``investment_transactions`` has no
``category_id`` column at all, so there is no category to predict and none the
user could correct to train on; callers filter them out (see
:func:`bootstrap`).

Persistence (schema v55/v56): ``category_nodes`` (the trie),
``category_node_labels`` (votes at a node), ``category_payee_stats`` (votes for
the payee overall -- the coherence gate and the dropdown ranking),
``category_token_freq`` + ``category_token_labels`` (the ranking snapshot and
the distribution its entropy is computed from). Every mutator self-commits unless ``commit=False`` (used to batch a
bootstrap into one transaction).
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from . import keywords
from .importers.record import normalize_payee
from .rename_tree import _is_mixed, confidence, normalize_tokens

__all__ = [
    "AUTO_PURITY",
    "MIN_COUNT",
    "PAYEE_COHERENCE",
    "SUGGEST_FLOOR",
    "Suggestion",
    "ACTION_AUTO",
    "ACTION_DROPDOWN",
    "ACTION_LEAVE",
    "normalized_key",
    "source_text",
    "learn",
    "suggest",
    "ranked_categories",
    "known_categories",
    "bootstrap",
    "ensure_bootstrapped",
    "snapshot_stats",
    "forget_payee",
    "payee_summary",
    "list_payees",
    "clear",
]

# A node auto-fills when its top category holds at least this share of the
# node's votes, backed by at least MIN_COUNT of them. Same shape and the same
# values as the rename tree's payee domain: TWO, by request -- a payee whose
# accepted rows have carried one category twice has told us what it is, and
# the four the old rename trie needed was compensating for counts that reset
# on every split. Below two, the register's own QuickFill answers for a payee
# it already knows (``import_review.predict_fields``), so a single sighting of
# a payee with history fills too; the accepted category is still recorded as
# a vote here either way.
AUTO_PURITY = 0.9
MIN_COUNT = 2

# The payee's OWN dominant category must hold at least this share of everything
# ever seen for that payee before ANY node under it may auto-fill. See the
# module docstring: without this, a catalogue payee (Amazon) reads as pure at
# its root and auto-fills one category onto every unrelated purchase. Swept over
# the real cold-start corpus: 0.5 -> 89% precision, 0.6 -> 96%, 0.8 -> 97% at
# steadily falling coverage. 0.6 is the knee.
PAYEE_COHERENCE = 0.6

# Below this confidence a matched node is too weak to trust (too deep, too few
# votes). ``confidence`` is hits/(hits+depth), so a lone vote at depth 2 scores
# 0.33 -- the canonical "too weak" case.
SUGGEST_FLOOR = 0.34

ACTION_AUTO = "auto"          # confident -> fill the category in
ACTION_DROPDOWN = "dropdown"  # not confident -> blank, but rank the picker
ACTION_LEAVE = "leave"        # nothing known for this payee at all

_NOISE_FLOOR = 10 ** 9        # boilerplate always sorts last (see rename_tree)


@dataclass
class Suggestion:
    """What the tree has to say about one (payee, description) pair.

    ``category_id`` is the confident answer and is ``None`` unless
    ``action == ACTION_AUTO``; ``candidates`` is the ranked
    ``[(category_id, votes)]`` for the picker and is populated whenever the payee
    has ANY history, so a non-confident row still gets a useful dropdown.
    """
    action: str = ACTION_LEAVE
    category_id: Optional[int] = None
    candidates: list = field(default_factory=list)
    confidence: float = 0.0
    depth: int = 0
    purity: float = 0.0
    coherence: float = 0.0

    @property
    def category_ids(self) -> list:
        return [cid for cid, _ in self.candidates]


# ---------------------------------------------------------------------------
# keys and source text
# ---------------------------------------------------------------------------
def normalized_key(payee: Optional[str]) -> str:
    """The tree key for a payee -- the same normalization
    :mod:`mammon.categorize` keys ``import_mappings`` on, so "SAFEWAY #123" and
    "Safeway  #456" share one tree."""
    return normalize_payee(payee or "")


def source_text(memo: Optional[str], payee: Optional[str] = None,
                payee_supplied: bool = False) -> str:
    """The raw text to tokenize for one row.

    The bank's own description when there is one, else the payee field the
    source supplied verbatim (see the module docstring -- the Costco card sends
    its description THERE and leaves the memo empty, and a tree that only reads
    ``memo`` learns nothing from that account). ``payee_supplied`` is honoured
    when given but not required: an empty memo with a payee is the same
    situation whether or not the flag survived a reload.
    """
    text = (memo or "").strip()
    if text:
        return text
    if payee_supplied or payee:
        return (payee or "").strip()
    return ""


# ---------------------------------------------------------------------------
# token ranking
# ---------------------------------------------------------------------------
def _stats_map(conn, toks: list[str]) -> dict:
    """{token: (freq, entropy)} from the snapshot; absent tokens are missing."""
    if not toks:
        return {}
    rows = conn.execute(
        "SELECT token, freq, entropy FROM category_token_freq WHERE token IN (%s)"
        % ",".join("?" * len(toks)),
        toks,
    ).fetchall()
    return {r["token"]: (int(r["freq"]), r["entropy"]) for r in rows}


def _note_token(conn, token: str, category_id: int) -> None:
    """Record one (token, category) observation and refresh the token's entropy.

    Entropy is recomputed here rather than only in :func:`snapshot_stats`
    because ranking decides which token a walk descends on, and rows are learned
    ONE AT A TIME during a review session. Left stale, a freshly seen token has
    no entropy, every token ties, the sort falls back to frequency, and the
    Costco tree splits on ``COSTCO`` -- a token both branches carry, which
    discriminates nothing and sends "COSTCO WHSE ..." down the fuel branch.
    """
    conn.execute(
        "INSERT INTO category_token_labels(token, category_id, count) VALUES(?,?,1) "
        "ON CONFLICT(token, category_id) DO UPDATE SET count = count + 1",
        (token, int(category_id)))
    rows = conn.execute(
        "SELECT count FROM category_token_labels WHERE token=?", (token,)
    ).fetchall()
    counts = [int(r["count"]) for r in rows]
    total = sum(counts)
    ent = -sum((c / total) * math.log2(c / total) for c in counts) if total else 0.0
    conn.execute(
        "INSERT INTO category_token_freq(token, freq, entropy) VALUES(?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET freq=excluded.freq, entropy=excluded.entropy",
        (token, total, ent))


def ranked_tokens(conn, desc: str) -> list[str]:
    """Evidence tokens of ``desc``, most-discriminating first.

    The entropy ranking the rename tree used before it became a rebuilt
    decision tree (:mod:`mammon.rename_tree`), over CATEGORY labels, and with
    no ``extra`` channel -- :func:`source_text` has already chosen the one text
    that matters. The two measured guards are kept because the failure they
    prevent is the same one:

    * SHAPE FILTER -- a mixed letter+digit token not yet seen twice is dropped.
      Wells Fargo appends a unique auth code (``#2413746KS5SN96LYT``) to every
      description, and an unseen token looks maximally PURE to an entropy
      ranker, so without this every row roots its own unreachable path.
    * ENTROPY RANK -- purest first, support as tiebreak. Rarity is the wrong
      axis; a token that appears constantly but always under one category is
      exactly what we want to split on.
    """
    toks = normalize_tokens(desc)
    if not toks:
        return []
    stats = _stats_map(conn, toks)
    toks = [t for t in toks
            if not (_is_mixed(t) and stats.get(t, (0, None))[0] < 2)]
    if not toks:
        return []
    order = {t: i for i, t in enumerate(toks)}

    def rank(tok: str):
        freq, ent = stats.get(tok, (0, None))
        h = 0.0 if ent is None else float(ent)
        if tok in keywords._NOISE:
            h = max(h, float(_NOISE_FLOOR))
        return (h, -min(freq, 50), order[tok])

    return sorted(toks, key=rank)


# ---------------------------------------------------------------------------
# trie walking
# ---------------------------------------------------------------------------
def _root_id(conn, payee_key: str, create: bool = False) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM category_nodes WHERE payee_key=? AND parent_id IS NULL",
        (payee_key,),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    if not create:
        return None
    cur = conn.execute(
        "INSERT INTO category_nodes(parent_id, payee_key, token) VALUES(NULL,?,NULL)",
        (payee_key,),
    )
    return int(cur.lastrowid)


def _child_id(conn, node_id: int, token: str, payee_key: str,
              create: bool = False) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM category_nodes WHERE parent_id=? AND token=?",
        (node_id, token),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    if not create:
        return None
    cur = conn.execute(
        "INSERT INTO category_nodes(parent_id, payee_key, token) VALUES(?,?,?)",
        (node_id, payee_key, token),
    )
    return int(cur.lastrowid)


def _labels(conn, node_id: int) -> list[tuple[int, int]]:
    """[(category_id, count)] at a node, most-voted first."""
    rows = conn.execute(
        "SELECT category_id, count FROM category_node_labels "
        "WHERE node_id=? ORDER BY count DESC, category_id ASC",
        (node_id,),
    ).fetchall()
    return [(int(r["category_id"]), int(r["count"])) for r in rows]


def _walk_path(conn, payee_key: str, toks: list[str]) -> list[int]:
    """Every node on the matched path, root first."""
    node_id = _root_id(conn, payee_key)
    if node_id is None:
        return []
    path = [node_id]
    for tok in toks:
        nxt = _child_id(conn, node_id, tok, payee_key)
        if nxt is None:
            break
        node_id = nxt
        path.append(node_id)
    return path


def _walk(conn, payee_key: str, toks: list[str]) -> tuple[Optional[int], int]:
    """The node that ANSWERS for ``toks`` under this payee, and its depth.

    The deepest matched node is not automatically the answer: a child too weak
    to stand on its own must not hide a strong parent. On the real ledger one
    Costco warehouse trip was categorized Dining, which minted a ``WHSE`` child
    holding a single vote -- and because WHSE ranks first, EVERY later
    "COSTCO WHSE ..." row then walked into that one-vote node instead of
    stopping at the root's 41 Groceries. A stray correction captured the whole
    mainstream branch, and the picker led with Dining.

    So the answer is the DEEPEST node whose top label clears
    :data:`MIN_COUNT` -- deep enough to have earned its answer, supported enough
    to be worth more than its parent. With nothing on the path qualifying, the
    deepest node that has any labels at all is returned; it can still rank the
    picker, and the count gate in :func:`suggest` keeps it from auto-filling.
    """
    path = _walk_path(conn, payee_key, toks)
    if not path:
        return None, 0
    best = None
    for depth, node_id in enumerate(path):
        labels = _labels(conn, node_id)
        if labels and labels[0][1] >= MIN_COUNT:
            best = (node_id, depth)
    if best is not None:
        return best
    for depth in range(len(path) - 1, -1, -1):
        if _labels(conn, path[depth]):
            return path[depth], depth
    return path[0], 0


# ---------------------------------------------------------------------------
# payee-level stats (the coherence gate and the dropdown ranking)
# ---------------------------------------------------------------------------
def known_categories(conn, payee: Optional[str]) -> list[tuple[int, int]]:
    """Every ``(category_id, count)`` this payee has ever carried, most first.

    This is the whole of what may be proposed for the payee: nothing outside
    this list is ever auto-filled or promoted (the invariant the flat rule table
    could not express).
    """
    key = normalized_key(payee)
    if not key:
        return []
    rows = conn.execute(
        "SELECT category_id, count FROM category_payee_stats "
        "WHERE payee_key=? ORDER BY count DESC, category_id ASC",
        (key,),
    ).fetchall()
    return [(int(r["category_id"]), int(r["count"])) for r in rows]


def coherence(conn, payee: Optional[str]) -> float:
    """Share of this payee's rows held by its single most common category.

    1.0 for a payee that has only ever been one thing, near 0 for a catalogue.
    Below :data:`PAYEE_COHERENCE` the payee never auto-fills.
    """
    stats = known_categories(conn, payee)
    total = sum(n for _, n in stats)
    if not total:
        return 0.0
    return stats[0][1] / total


def ranked_categories(conn, payee: Optional[str],
                      desc: str = "") -> list[tuple[int, int]]:
    """Categories to PROMOTE in the picker for this payee, best first.

    The matched node's categories lead (they are conditioned on this actual
    description -- for "COSTCO GAS ..." that is Auto:Fuel even though Groceries
    dominates the payee), then the payee's remaining categories by frequency.
    Never includes a category the payee has not carried; the caller appends
    everything else alphabetically underneath.
    """
    key = normalized_key(payee)
    if not key:
        return []
    overall = known_categories(conn, payee)
    if not overall:
        return []
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    if desc:
        # The PICKER walks deeper than the ANSWER does. :func:`_walk` refuses to
        # let a thinly-supported child override a strong parent, which is right
        # for auto-filling -- but a category is not worth less as a SUGGESTION
        # for being rare. When the row is left blank, the two or three sightings
        # this exact description shape has had are the most useful thing to put
        # at the top of the list, so rank from the deepest node that has any.
        path = _walk_path(conn, key, ranked_tokens(conn, desc))
        for node_id in reversed(path):
            labels = _labels(conn, node_id)
            if labels:
                for cid, n in labels:
                    if cid not in seen:
                        seen.add(cid)
                        out.append((cid, n))
                break
    for cid, n in overall:
        if cid not in seen:
            seen.add(cid)
            out.append((cid, n))
    return out


# ---------------------------------------------------------------------------
# suggestion
# ---------------------------------------------------------------------------
def suggest(conn, payee: Optional[str], desc: str, *,
            floor: Optional[float] = None) -> Suggestion:
    """What to do with one (payee, description) pair.

    ``ACTION_AUTO`` with a ``category_id`` when BOTH gates pass -- the matched
    node is pure and corroborated, and the payee itself is coherent.
    ``ACTION_DROPDOWN`` when the payee has history but this row is not
    confident: the category is left blank and ``candidates`` ranks the picker.
    ``ACTION_LEAVE`` when the payee is unknown -- a first sighting proposes
    nothing at all, which is the whole point during the learning period.
    """
    key = normalized_key(payee)
    if not key:
        return Suggestion(ACTION_LEAVE)
    overall = known_categories(conn, payee)
    if not overall:
        return Suggestion(ACTION_LEAVE)

    fl = SUGGEST_FLOOR if floor is None else float(floor)
    total_payee = sum(n for _, n in overall)
    coh = overall[0][1] / total_payee if total_payee else 0.0
    cands = ranked_categories(conn, payee, desc)

    node_id, depth = _walk(conn, key, ranked_tokens(conn, desc))
    labels = _labels(conn, node_id) if node_id is not None else []
    if not labels:
        return Suggestion(ACTION_DROPDOWN, candidates=cands, coherence=coh,
                          depth=depth)
    top_cid, top_n = labels[0]
    total = sum(n for _, n in labels)
    purity = top_n / total if total else 0.0
    conf = confidence(top_n, depth if depth else 1)

    # The coherence gate applies AT THE ROOT ONLY, and the depth is what makes
    # that the right rule. A root answer is the payee's bare prior -- no token
    # distinguished this row from any other, so the odds of being right ARE the
    # dominant category's share, which is what coherence measures. That is the
    # Amazon case, and it is correctly refused. A node BELOW the root was reached
    # by matching a token that has meant one category every time it appeared;
    # that is direct evidence about this description, and it must not be thrown
    # away because the payee happens to be bimodal overall. Costco splits its
    # rows almost evenly between groceries and fuel -- gating its GAS branch on
    # the payee's overall mix would refuse the one answer the tree is surest of.
    ok = (top_n >= MIN_COUNT and purity >= AUTO_PURITY and conf >= fl
          and (depth > 0 or coh >= PAYEE_COHERENCE))
    if ok:
        # The value that was filled IN leads its own picker. The candidate list
        # is ranked from the deepest node with any history, which can be a
        # thinly-supported one the answer deliberately walked past -- leaving the
        # dropdown headed by a category the cell does not show.
        cands = ([(top_cid, top_n)]
                 + [(c, n) for c, n in cands if c != top_cid])
        return Suggestion(ACTION_AUTO, category_id=top_cid, candidates=cands,
                          confidence=conf, depth=depth, purity=purity,
                          coherence=coh)
    return Suggestion(ACTION_DROPDOWN, candidates=cands, confidence=conf,
                      depth=depth, purity=purity, coherence=coh)


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------
def _bump(conn, table: str, cols: tuple[str, str], keys: tuple,
          count: int = 1) -> None:
    """Increment a (key, category) vote row, inserting it when absent."""
    conn.execute(
        f"INSERT INTO {table}({cols[0]}, {cols[1]}, count) VALUES(?,?,?) "
        f"ON CONFLICT({cols[0]}, {cols[1]}) DO UPDATE SET count = count + ?",
        (keys[0], keys[1], count, count),
    )


def learn(conn, payee: Optional[str], desc: str, category_id: Optional[int],
          *, commit: bool = True) -> None:
    """Record that the user put ``category_id`` on a row for ``payee``.

    Grows the payee's tree exactly the way the rename tree grows: at each node,
    a category already present increments and stops; a DIFFERENT category splits
    and descends on the next-ranked token, which is the lowest-entropy token
    that distinguishes this description from what is already there. Tokens
    exhausted -> the categories share the node, and its purity falls, which is
    what stops it auto-filling.

    A ``None`` category teaches nothing (the user left the row blank); so does a
    blank payee. Always updates the payee-level tally, which is what the
    coherence gate and the picker ranking read.

    Text-less evidence updates the TALLY ONLY and never enters the trie. A
    register edit (and 30 years of imported Quicken rows) carries a payee and a
    category but no bank text; with no tokens to walk, every such vote would land
    on the root, and the root is where mismatched categories accumulate. Costco's
    root would end up Groceries 249 / Auto:Fuel 45 / Household 8 -- purity 0.82,
    below :data:`AUTO_PURITY` -- and the one case the tree gets *right* with no
    effort ("COSTCO WHSE ..." -> Groceries) would stop auto-filling. The tally
    wants that evidence; the trie does not.
    """
    key = normalized_key(payee)
    if not key or category_id is None:
        return
    cid = int(category_id)
    _bump(conn, "category_payee_stats", ("payee_key", "category_id"), (key, cid))
    toks_raw = normalize_tokens(desc)
    if not toks_raw:
        if commit:
            conn.commit()
        return
    for tok in toks_raw:
        _note_token(conn, tok, cid)

    toks = ranked_tokens(conn, desc)
    node_id = _root_id(conn, key, create=True)
    for tok in toks:
        labels = dict(_labels(conn, node_id))
        if not labels or cid in labels:
            _bump(conn, "category_node_labels", ("node_id", "category_id"),
                  (node_id, cid))
            if commit:
                conn.commit()
            return
        node_id = _child_id(conn, node_id, tok, key, create=True)
    # Tokens exhausted: this description cannot be told apart from what already
    # sits here, so the categories share the node and neither can auto-fill.
    _bump(conn, "category_node_labels", ("node_id", "category_id"),
          (node_id, cid))
    if commit:
        conn.commit()


def unlearn(conn, payee: Optional[str], desc: str,
            category_id: Optional[int], *, commit: bool = True) -> None:
    """Withdraw one vote recorded by :func:`learn` (an accept that was undone).

    Decrements the payee tally and the deepest node holding that category;
    rows that reach zero are deleted so a withdrawn correction cannot keep a
    stale category in the picker.
    """
    key = normalized_key(payee)
    if not key or category_id is None:
        return
    cid = int(category_id)
    node_id, _depth = _walk(conn, key, ranked_tokens(conn, desc))
    for table, col, kval in (("category_node_labels", "node_id", node_id),
                             ("category_payee_stats", "payee_key", key)):
        if kval is None:
            continue
        conn.execute(
            f"UPDATE {table} SET count = count - 1 "
            f"WHERE {col}=? AND category_id=? AND count > 0", (kval, cid))
        conn.execute(
            f"DELETE FROM {table} WHERE {col}=? AND category_id=? AND count <= 0",
            (kval, cid))
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# bootstrap from what the user has already accepted
# ---------------------------------------------------------------------------
def _training_rows(conn) -> list[tuple[str, str, int]]:
    """(payee, source_text, category_id) for every row worth learning from.

    Two sources, and the split is deliberate:

    * the REVIEW LIST -- accepted rows joined to the transaction they created.
      This is the only place the raw bank text survives alongside the category
      the user chose, and it is what the payee trees are built from.
    * the REGISTER -- every categorized non-transfer transaction. These feed the
      payee-level tally only (see :func:`bootstrap`): 30 years of Quicken
      history carries a renamed payee and NO description (checked against the
      QIF exports -- ``PCostco`` / ``LGroceries``, no memo), so there is nothing
      to split on, but the payee's category distribution is exactly what the
      coherence gate and the picker ranking need.

    Investment rows are excluded: ``investment_transactions`` has no category.
    """
    rows = conn.execute(
        "SELECT t.payee AS payee, ri.memo AS memo, ri.payee AS src_payee, "
        "       ri.payee_supplied AS supplied, t.category_id AS category_id "
        "FROM review_items ri JOIN transactions t ON t.id = ri.accepted_txn_id "
        "WHERE ri.state='accepted' AND ri.is_investment=0 "
        "  AND t.category_id IS NOT NULL AND t.transfer_account_id IS NULL "
        "  AND COALESCE(t.payee,'') != '' "
        "ORDER BY ri.date, ri.id"
    ).fetchall()
    out = []
    for r in rows:
        text = source_text(r["memo"], r["src_payee"],
                           bool(r["supplied"] or 0))
        out.append((r["payee"], text, int(r["category_id"])))
    return out


def _register_rows(conn) -> list[tuple[str, int]]:
    """(payee, category_id) for every categorized non-transfer register row."""
    rows = conn.execute(
        "SELECT payee, category_id FROM transactions "
        "WHERE category_id IS NOT NULL AND transfer_account_id IS NULL "
        "  AND scheduled=0 AND COALESCE(payee,'') != ''"
    ).fetchall()
    return [(r["payee"], int(r["category_id"])) for r in rows]


def snapshot_stats(conn, *, commit: bool = True) -> int:
    """Rebuild ``category_token_freq`` (frequency + label entropy) from history.

    Ranking must be computed BEFORE the trees are grown, because a trie built
    under one ranking is unreachable under another (the defect that eventually
    retired the rename trie in favour of a tree rebuilt per query). Returns the
    token count.
    """
    counts: dict[str, Counter] = defaultdict(Counter)
    for _payee, text, cid in _training_rows(conn):
        for tok in normalize_tokens(text):
            counts[tok][cid] += 1
    conn.execute("DELETE FROM category_token_freq")
    conn.execute("DELETE FROM category_token_labels")
    for tok, cats in counts.items():
        total = sum(cats.values())
        ent = -sum((v / total) * math.log2(v / total) for v in cats.values())
        conn.execute(
            "INSERT INTO category_token_freq(token, freq, entropy) VALUES(?,?,?)",
            (tok, total, ent))
        # Keep the distribution too, so a row learned online after this snapshot
        # updates the same entropy this rebuild computed instead of resetting it.
        for cid, n in cats.items():
            conn.execute(
                "INSERT INTO category_token_labels(token, category_id, count) "
                "VALUES(?,?,?)", (tok, int(cid), int(n)))
    if commit:
        conn.commit()
    return len(counts)


def bootstrap(conn, *, commit: bool = True) -> int:
    """Build every payee tree from scratch out of what the user already accepted.

    Three passes, in this order, and the order matters:

    1. the payee-level tally, from the WHOLE register -- so the coherence gate
       and the picker ranking have every categorization the user ever made,
       including the decades of imported history that carry no description;
    2. the token ranking snapshot, from the review list (the only rows with real
       bank text);
    3. the trees themselves, replaying those review rows in date order -- which
       is exactly the sequence of accepts that produced them.

    Returns the number of review rows replayed. Idempotent: clears first.
    """
    clear(conn, commit=False)
    for payee, cid in _register_rows(conn):
        key = normalized_key(payee)
        if key:
            _bump(conn, "category_payee_stats", ("payee_key", "category_id"),
                  (key, cid))
    snapshot_stats(conn, commit=False)
    rows = _training_rows(conn)
    for payee, text, cid in rows:
        # The payee tally already counted this row via the register pass; grow
        # only the tree here so a review row is not voted twice.
        _learn_tree_only(conn, payee, text, cid)
    if commit:
        conn.commit()
    return len(rows)


def _learn_tree_only(conn, payee: Optional[str], desc: str,
                     category_id: int) -> None:
    """The tree half of :func:`learn`, without touching the payee tally."""
    key = normalized_key(payee)
    if not key:
        return
    cid = int(category_id)
    node_id = _root_id(conn, key, create=True)
    for tok in ranked_tokens(conn, desc):
        labels = dict(_labels(conn, node_id))
        if not labels or cid in labels:
            _bump(conn, "category_node_labels", ("node_id", "category_id"),
                  (node_id, cid))
            return
        node_id = _child_id(conn, node_id, tok, key, create=True)
    _bump(conn, "category_node_labels", ("node_id", "category_id"),
          (node_id, cid))


def ensure_bootstrapped(conn) -> None:
    """Build the trees once, on first use, if the tables are empty.

    Cheap to call: one COUNT. A user upgrading into v55 gets their existing
    history immediately rather than starting blind.
    """
    row = conn.execute("SELECT COUNT(*) AS c FROM category_payee_stats").fetchone()
    if int(row["c"]) == 0:
        bootstrap(conn)


# ---------------------------------------------------------------------------
# management surface
# ---------------------------------------------------------------------------
def list_payees(conn) -> list[dict]:
    """One row per payee the tree knows: key, votes, category count, coherence.

    Feeds a Rules-manager tab so the GUI issues no SQL of its own.
    """
    rows = conn.execute(
        "SELECT payee_key, SUM(count) AS votes, COUNT(*) AS cats, "
        "       MAX(count) AS top "
        "FROM category_payee_stats GROUP BY payee_key ORDER BY payee_key"
    ).fetchall()
    out = []
    for r in rows:
        votes = int(r["votes"] or 0)
        out.append({
            "payee_key": r["payee_key"],
            "votes": votes,
            "categories": int(r["cats"] or 0),
            "coherence": (int(r["top"] or 0) / votes) if votes else 0.0,
        })
    return out


def payee_summary(conn, payee: Optional[str]) -> dict:
    """``{payee_key, coherence, auto_ok, categories: [(id, count)]}``."""
    key = normalized_key(payee)
    cats = known_categories(conn, payee)
    coh = coherence(conn, payee)
    return {"payee_key": key, "coherence": coh,
            "auto_ok": coh >= PAYEE_COHERENCE, "categories": cats}


def forget_payee(conn, payee: Optional[str], *, commit: bool = True) -> None:
    """Drop everything learned for one payee (management UI). Commits."""
    key = normalized_key(payee)
    if not key:
        return
    conn.execute(
        "DELETE FROM category_node_labels WHERE node_id IN "
        "(SELECT id FROM category_nodes WHERE payee_key=?)", (key,))
    conn.execute("DELETE FROM category_nodes WHERE payee_key=?", (key,))
    conn.execute("DELETE FROM category_payee_stats WHERE payee_key=?", (key,))
    if commit:
        conn.commit()


def clear(conn, *, commit: bool = True) -> None:
    """Wipe every tree (a rebuild starts here)."""
    for table in ("category_node_labels", "category_nodes",
                  "category_payee_stats", "category_token_freq",
                  "category_token_labels"):
        conn.execute(f"DELETE FROM {table}")
    if commit:
        conn.commit()
