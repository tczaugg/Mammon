"""Per-payee discriminative tree for the Category cell (the user's design).

The Category cell holds one of two things -- a category, or a transfer account
written ``[Account]`` -- and this tree is the ONLY thing that learns what goes
there. Every learned behavior in Mammon is a decision tree (the user's ruling,
2026-10): payee renaming is :mod:`mammon.rename_tree`, and the category or
transfer account is this module. There is no keyword rules table.

Why there is no keyword table
-----------------------------
It replaced the flat, GLOBAL ``keyword -> category_id`` table that learned a rule
from a SINGLE correction, keyed on the first non-noise token of the raw text,
and matched it against every merchant. Replayed over a new user's first year
(625 accepted review rows, no history, predicting each row before learning it)
that table fired on 67% of rows, was wrong on 26% of those, and **half of the
errors proposed a category that payee had never carried** -- a rule learned from
one merchant firing on an unrelated one:

    SUBWAY 61276 ANYTOWN ST       kw=ANYTOWN  -> Utilities:Gas & Electric
    O'REILLY 2988 ANYTOWN ST      kw=ANYTOWN  -> Utilities:Gas & Electric
    GOOGLE *Chrome MOUNTAIN VIEW CA    kw=MOUNTAIN -> Medical:Doctor
    April 2026 Rent (Anyplace)        kw=APRIL    -> Gift Received

The transfer-account keyword table failed the same way later: two cards from one
issuer send byte-identical bank text ("ANYBANK AUTOPAY PAYMENT") and are paid
from different accounts; one accept learned a global rule, and the other card's
payment was filed to the wrong account. Migration 116 dropped both tables.

The fix is scope. Category is resolved AFTER the payee (:mod:`mammon.rename_tree`
runs first), and every decision here hangs below that payee, so a candidate can
only ever be a category or account that payee has actually carried. On the
cold-start corpus this proposes a never-seen category exactly zero times, by
construction rather than by tuning.

Labels
------
A vote is a signed integer LABEL: a positive label is a category id, a negative
label is a transfer account id negated (:func:`account_label`). In the tables
the two are separate nullable foreign keys, exactly one set, each ON DELETE
CASCADE, so deleting a category or an account drops its votes.

Two trees per payee: everywhere, and in the paying account
----------------------------------------------------------
The trie splits on a GLOBAL token ranking, which works when a word tells two
answers apart ("COSTCO GAS" vs "COSTCO WHSE") and cannot work when nothing in
the text does. Identical text paid from two accounts to two cards, or a business
account buying at the same warehouse as the household, differs only in the
account the row lives in. So every vote is recorded twice: once under the payee
(``payee_key``) and once under the payee IN THAT ACCOUNT (``payee_key`` + TAB +
account id). A row is answered by its account's tree once that tree holds
:data:`MIN_COUNT` votes, and by the payee's tree until then -- a new account
starts from everything the payee has taught elsewhere and specializes as soon as
it has evidence of its own.

Structure
---------
One trie per key (``category_nodes``, one root per ``payee_key``), grown exactly
the way the rename tree grows: walk the source text's tokens ranked by label
entropy; a node whose label matches increments, a node whose label differs
SPLITS and descends on the next token -- which is the lowest-entropy token that
tells this description apart from what already sits there. So "Costco" grows a
``GAS`` child under its root and nothing else needs to be said:

    COSTCO GAS #1234 ANYTOWN ST   -> Auto:Fuel   (confident, depth 1)
    COSTCO WHSE #1234 ANYTOWN ST  -> Groceries   (confident, depth 0)

Two gates, and both are load-bearing
------------------------------------
* **Node purity** (:data:`AUTO_PURITY`, :data:`MIN_COUNT`) asks *does this
  description discriminate?* -- the rename tree's gate, unchanged.
* **Payee coherence** (:data:`PAYEE_COHERENCE`) asks *is this merchant
  categorizable at all?* A node only accumulates the labels that STOPPED
  there; rows diverted into a child are not counted, so a marketplace payee
  whose every item description is unique looks perfectly pure at its root and
  auto-fills its most common category onto everything. Measured: 61 of the 67
  errors the node gate alone allowed were Amazon (51 categories in the real
  ledger -- it is not a merchant, it is a catalog). Requiring the payee's own
  dominant label to hold :data:`PAYEE_COHERENCE` of everything ever seen for
  it took precision from 77% to 96% (67 wrong -> 6) on the same 625 rows, and
  correctly demotes Amazon to dropdown-only forever.

The rows that fail either gate are not guesses to be improved -- they are the
cases the user must decide. They get a blank cell and a RANKED dropdown
(:func:`ranked_categories`): the matched node's labels first, then the rest of
that payee's, and the caller appends everything else alphabetically.

Source text
-----------
The discriminating text arrives in DIFFERENT fields depending on the importer.
Wells Fargo sends a ``statementDescription`` (review ``memo``, ``payee_supplied``
0); the Costco card sends no description at all and puts the same text in the
payee field (``payee_supplied`` 1, ``memo`` empty). :func:`source_text` is the
one place that picks, so both shapes train and query the same tree.

Investment rows never reach here. ``investment_transactions`` has no
``category_id`` column at all, so there is nothing to predict.

Forgetting
----------
:func:`forget_payee` drops a payee's trees, or one label from them, and records
it in ``category_forgotten`` up to the newest transaction at that moment, so an
explicit :func:`bootstrap` cannot quietly bring it back. New accepts after that
teach normally.

Persistence (schema v55/v56/v116): ``category_nodes`` (the trie),
``category_node_labels`` (votes at a node), ``category_payee_stats`` (votes for
the key overall -- the coherence gate and the dropdown ranking),
``category_token_freq`` + ``category_token_labels`` (the ranking snapshot and the
distribution its entropy is computed from), ``category_forgotten`` and
``category_meta``. Every mutator self-commits unless ``commit=False``.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

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
    "account_label",
    "label_account",
    "label_category",
    "label_for",
    "normalized_key",
    "source_text",
    "learn",
    "unlearn",
    "suggest",
    "ranked_categories",
    "known_categories",
    "coherence",
    "dominant",
    "bootstrap",
    "ensure_bootstrapped",
    "ensure_seeded",
    "snapshot_stats",
    "forget_payee",
    "merge_category",
    "payee_summary",
    "payee_labels",
    "labels_by_account",
    "list_payees",
    "clear",
]

# A node auto-fills when its top label holds at least this share of the
# node's votes, backed by at least MIN_COUNT of them. Same shape and the same
# values as the rename tree's payee domain: TWO -- a payee whose
# accepted rows have carried one category twice has told us what it is. Below
# two, the register's own QuickFill answers for a payee it already knows
# (``import_review.predict_fields``), so a single sighting of a payee with
# history fills too; the accepted label is still recorded as a vote here.
AUTO_PURITY = 0.9
MIN_COUNT = 2

# The payee's OWN dominant label must hold at least this share of everything
# ever seen for that payee before ANY node under it may auto-fill. See the
# module docstring: without this, a catalog payee (Amazon) reads as pure at
# its root and auto-fills one category onto every unrelated purchase. Swept over
# the real cold-start corpus: 0.5 -> 89% precision, 0.6 -> 96%, 0.8 -> 97% at
# steadily falling coverage. 0.6 is the knee.
PAYEE_COHERENCE = 0.6

# Below this confidence a matched node is too weak to trust (too deep, too few
# votes). ``confidence`` is hits/(hits+depth), so a lone vote at depth 2 scores
# 0.33 -- the canonical "too weak" case.
SUGGEST_FLOOR = 0.34

ACTION_AUTO = "auto"          # confident -> fill the cell in
ACTION_DROPDOWN = "dropdown"  # not confident -> blank, but rank the picker
ACTION_LEAVE = "leave"        # nothing known for this payee at all

_NOISE_FLOOR = 10 ** 9        # boilerplate always sorts last (see rename_tree)

# Separates a payee key from the account id in a per-account key. A payee key
# is normalized text and never holds a tab.
_ACCOUNT_SEP = "\t"

_SEED_FLAG = "seed_v116"


@dataclass
class Suggestion:
    """What the tree has to say about one (payee, description) pair.

    ``category_id`` is the confident LABEL (see :func:`label_category` /
    :func:`label_account`) and is ``None`` unless ``action == ACTION_AUTO``;
    ``candidates`` is the ranked ``[(label, votes)]`` for the picker and is
    populated whenever the payee has ANY history, so a non-confident row still
    gets a useful dropdown. ``scope`` says which tree answered: ``"account"``
    or ``"payee"``.
    """
    action: str = ACTION_LEAVE
    category_id: Optional[int] = None
    candidates: list = field(default_factory=list)
    confidence: float = 0.0
    depth: int = 0
    purity: float = 0.0
    coherence: float = 0.0
    scope: str = "payee"

    @property
    def label(self) -> Optional[int]:
        return self.category_id

    @property
    def category_ids(self) -> list:
        """Every candidate label, best first (categories AND accounts)."""
        return [cid for cid, _ in self.candidates]


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------
def account_label(account_id: int) -> int:
    """The label for a transfer to ``account_id``."""
    return -int(account_id)


def label_account(label: Optional[int]) -> Optional[int]:
    """The transfer account a label names, or ``None`` for a category."""
    return -int(label) if label is not None and int(label) < 0 else None


def label_category(label: Optional[int]) -> Optional[int]:
    """The category a label names, or ``None`` for a transfer account."""
    return int(label) if label is not None and int(label) > 0 else None


def label_for(category_id: Optional[int] = None,
              transfer_account_id: Optional[int] = None) -> Optional[int]:
    """The label for what a row carries: its transfer account if it is a
    transfer, else its category, else ``None`` (nothing to learn)."""
    if transfer_account_id is not None:
        return account_label(transfer_account_id)
    if category_id is not None:
        return int(category_id)
    return None


def _cols(label: int) -> tuple[Optional[int], Optional[int]]:
    """(category_id, account_id) columns for a label."""
    lab = int(label)
    return (lab, None) if lab > 0 else (None, -lab)


# The label as one value, for every reader.
_LABEL_SQL = "COALESCE(category_id, -account_id)"


# ---------------------------------------------------------------------------
# keys and source text
# ---------------------------------------------------------------------------
def normalized_key(payee: Optional[str]) -> str:
    """The tree key for a payee, so "SAFEWAY #123" and "Safeway  #456" share
    one tree."""
    return normalize_payee(payee or "")


def _account_key(key: str, account_id: int) -> str:
    return f"{key}{_ACCOUNT_SEP}{int(account_id)}"


def _keys(key: str, account_id: Optional[int]) -> list[str]:
    """The payee's own key, then its key in ``account_id`` when given."""
    return [key] if account_id is None else [key, _account_key(key, account_id)]


def source_text(memo: Optional[str], payee: Optional[str] = None,
                payee_supplied: bool = False) -> str:
    """The raw text to tokenize for one row.

    The bank's own description when there is one, else the payee field the
    source supplied verbatim (see the module docstring -- the Costco card sends
    its description THERE and leaves the memo empty, and a tree that only reads
    ``memo`` learns nothing from that account). ``payee_supplied`` is honored
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
# vote rows
# ---------------------------------------------------------------------------
def _bump(conn, table: str, keycol: str, keyval, label: int,
          count: int = 1) -> None:
    """Add ``count`` votes for ``label`` under ``keyval``, inserting the row
    when absent. (Not an ON CONFLICT upsert: the uniqueness is over an
    expression of two nullable columns.)"""
    cat, acct = _cols(label)
    cur = conn.execute(
        f"UPDATE {table} SET count = count + ? WHERE {keycol}=? "
        f"AND category_id IS ? AND account_id IS ?",
        (count, keyval, cat, acct))
    if cur.rowcount == 0:
        conn.execute(
            f"INSERT INTO {table}({keycol}, category_id, account_id, count) "
            f"VALUES(?,?,?,?)", (keyval, cat, acct, count))


def _drop(conn, table: str, keycol: str, keyval, label: int) -> None:
    """Withdraw one vote; a row reaching zero is deleted."""
    cat, acct = _cols(label)
    conn.execute(
        f"UPDATE {table} SET count = count - 1 WHERE {keycol}=? "
        f"AND category_id IS ? AND account_id IS ? AND count > 0",
        (keyval, cat, acct))
    conn.execute(
        f"DELETE FROM {table} WHERE {keycol}=? AND category_id IS ? "
        f"AND account_id IS ? AND count <= 0", (keyval, cat, acct))


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


def _note_token(conn, token: str, label: int) -> None:
    """Record one (token, label) observation and refresh the token's entropy.

    Entropy is recomputed here rather than only in :func:`snapshot_stats`
    because ranking decides which token a walk descends on, and rows are learned
    ONE AT A TIME during a review session. Left stale, a freshly seen token has
    no entropy, every token ties, the sort falls back to frequency, and the
    Costco tree splits on ``COSTCO`` -- a token both branches carry, which
    discriminates nothing and sends "COSTCO WHSE ..." down the fuel branch.
    """
    _bump(conn, "category_token_labels", "token", token, label)
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

    Two measured guards:

    * SHAPE FILTER -- a mixed letter+digit token not yet seen twice is dropped.
      Wells Fargo appends a unique auth code (``#1234567ABCDEFGHIJ``) to every
      description, and an unseen token looks maximally PURE to an entropy
      ranker, so without this every row roots its own unreachable path.
    * ENTROPY RANK -- purest first, support as tiebreak. Rarity is the wrong
      axis; a token that appears constantly but always under one label is
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
    """[(label, count)] at a node, most-voted first."""
    rows = conn.execute(
        f"SELECT {_LABEL_SQL} AS label, count FROM category_node_labels "
        "WHERE node_id=? ORDER BY count DESC, (label < 0), ABS(label)",
        (node_id,),
    ).fetchall()
    return [(int(r["label"]), int(r["count"])) for r in rows]


def _walk_path(conn, payee_key: str, toks: list[str]) -> list[int]:
    """Every node on the matched path, root first. At each node the walk takes
    the best-ranked unused token that has a child there: a split descends on the
    first token that DISTINGUISHES the row (see :func:`_grow`), which need not
    be the first-ranked one."""
    node_id = _root_id(conn, payee_key)
    if node_id is None:
        return []
    path = [node_id]
    used: set[str] = set()
    while True:
        nxt = None
        for tok in toks:
            if tok in used:
                continue
            nxt = _child_id(conn, node_id, tok, payee_key)
            if nxt is not None:
                used.add(tok)
                break
        if nxt is None:
            return path
        node_id = nxt
        path.append(node_id)


def _common(conn, node_id: int) -> Optional[set]:
    """The tokens every row that stopped at this node shared, or ``None`` for a
    node grown before schema v116 (unknown)."""
    row = conn.execute("SELECT common FROM category_nodes WHERE id=?",
                       (node_id,)).fetchone()
    if row is None or row["common"] is None:
        return None
    return set(row["common"].split())


def _note_common(conn, node_id: int, tokens: set, had_votes: bool) -> None:
    """Narrow the node's shared tokens to those this row also carries."""
    current = _common(conn, node_id)
    if current is None:
        if had_votes:
            return              # a pre-v116 node: what its rows shared is unknown
        current = set(tokens)
    else:
        current &= tokens
    conn.execute("UPDATE category_nodes SET common=? WHERE id=?",
                 (" ".join(sorted(current)), node_id))


def _walk(conn, payee_key: str, toks: list[str]) -> tuple[Optional[int], int]:
    """The node that ANSWERS for ``toks`` under this key, and its depth.

    The deepest matched node is not automatically the answer: a child too weak
    to stand on its own must not hide a strong parent. In one ledger a single
    Costco warehouse trip was categorized Dining, which minted a ``WHSE`` child
    holding a single vote -- and because WHSE ranks first, EVERY later
    "COSTCO WHSE ..." row then walked into that one-vote node instead of
    stopping at the root's strong Groceries majority.

    So the answer is the DEEPEST node whose top label clears
    :data:`MIN_COUNT`. With nothing on the path qualifying, the deepest node
    that has any labels at all is returned; it can still rank the picker, and
    the count gate in :func:`suggest` keeps it from auto-filling.
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
# key-level stats (the coherence gate and the dropdown ranking)
# ---------------------------------------------------------------------------
def _known(conn, key: str) -> list[tuple[int, int]]:
    if not key:
        return []
    rows = conn.execute(
        f"SELECT {_LABEL_SQL} AS label, count FROM category_payee_stats "
        "WHERE payee_key=? ORDER BY count DESC, (label < 0), ABS(label)",
        (key,),
    ).fetchall()
    return [(int(r["label"]), int(r["count"])) for r in rows]


def known_categories(conn, payee: Optional[str], *,
                     account_id: Optional[int] = None) -> list[tuple[int, int]]:
    """Every ``(label, count)`` this payee has ever carried, most first -- in
    ``account_id`` only, when given.

    This is the whole of what may be proposed for the payee: nothing outside
    this list is ever auto-filled or promoted.
    """
    key = normalized_key(payee)
    if not key:
        return []
    if account_id is not None:
        return _known(conn, _account_key(key, account_id))
    return _known(conn, key)


def coherence(conn, payee: Optional[str], *,
              account_id: Optional[int] = None) -> float:
    """Share of this payee's rows held by its single most common label.

    1.0 for a payee that has only ever been one thing, near 0 for a catalog.
    Below :data:`PAYEE_COHERENCE` the payee never auto-fills.
    """
    stats = known_categories(conn, payee, account_id=account_id)
    total = sum(n for _, n in stats)
    if not total:
        return 0.0
    return stats[0][1] / total


def _answering_key(conn, key: str, account_id: Optional[int]) -> tuple[str, str]:
    """The key whose tree answers for a row in ``account_id``: the payee's tree
    in that account once it holds :data:`MIN_COUNT` votes, else the payee's."""
    if account_id is not None:
        akey = _account_key(key, account_id)
        if sum(n for _, n in _known(conn, akey)) >= MIN_COUNT:
            return akey, "account"
    return key, "payee"


def dominant(conn, payee: Optional[str], *,
             account_id: Optional[int] = None) -> Optional[int]:
    """The label ``payee`` nearly always takes when nothing is known about the
    row but the payee -- the tree's ROOT answer, read from the tally: the most
    common label, backed by :data:`MIN_COUNT` votes and holding
    :data:`PAYEE_COHERENCE` of them, in ``account_id``'s own history once it
    has enough. ``None`` for a payee that is not consistently one thing.

    The tally is what a register edit teaches (it carries no bank text, so it
    never enters the trie), which is why a text-less question is answered here
    and not by walking the trie."""
    key = normalized_key(payee)
    if not key:
        return None
    answering, _scope = _answering_key(conn, key, account_id)
    known = _known(conn, answering)
    total = sum(n for _, n in known)
    if not total:
        return None
    top, top_n = known[0]
    if top_n >= MIN_COUNT and top_n / total >= PAYEE_COHERENCE:
        return top
    return None


def _ranked_for_key(conn, key: str, desc: str) -> list[tuple[int, int]]:
    overall = _known(conn, key)
    if not overall:
        return []
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    if desc:
        # The PICKER walks deeper than the ANSWER does. :func:`_walk` refuses to
        # let a thinly-supported child override a strong parent, which is right
        # for auto-filling -- but a label is not worth less as a SUGGESTION
        # for being rare, so rank from the deepest node that has any.
        path = _walk_path(conn, key, ranked_tokens(conn, desc))
        for node_id in reversed(path):
            labels = _labels(conn, node_id)
            if labels:
                for lab, n in labels:
                    if lab not in seen:
                        seen.add(lab)
                        out.append((lab, n))
                break
    for lab, n in overall:
        if lab not in seen:
            seen.add(lab)
            out.append((lab, n))
    return out


def _merge_ranked(first: Iterable, then: Iterable) -> list[tuple[int, int]]:
    out, seen = [], set()
    for lab, n in list(first) + list(then):
        if lab not in seen:
            seen.add(lab)
            out.append((lab, n))
    return out


def ranked_categories(conn, payee: Optional[str], desc: str = "", *,
                      account_id: Optional[int] = None) -> list[tuple[int, int]]:
    """Labels to PROMOTE in the picker for this payee, best first.

    The answering tree's labels lead (the account's own, when it has enough
    history), conditioned on this actual description -- for "COSTCO GAS ..."
    that is Auto:Fuel even though Groceries dominates the payee -- then the
    payee's remaining labels by frequency. Never includes a label the payee has
    not carried; the caller appends everything else alphabetically underneath.
    """
    ensure_seeded(conn)
    key = normalized_key(payee)
    if not key:
        return []
    answering, scope = _answering_key(conn, key, account_id)
    ranked = _ranked_for_key(conn, answering, desc)
    if scope == "account":
        ranked = _merge_ranked(ranked, _ranked_for_key(conn, key, desc))
    return ranked


# ---------------------------------------------------------------------------
# suggestion
# ---------------------------------------------------------------------------
def suggest(conn, payee: Optional[str], desc: str, *,
            account_id: Optional[int] = None,
            floor: Optional[float] = None) -> Suggestion:
    """What to do with one (payee, description) pair in ``account_id``.

    ``ACTION_AUTO`` with a label in ``category_id`` when BOTH gates pass -- the
    matched node is pure and corroborated, and the payee itself is coherent.
    ``ACTION_DROPDOWN`` when the payee has history but this row is not
    confident: the cell is left blank and ``candidates`` ranks the picker.
    ``ACTION_LEAVE`` when the payee is unknown -- a first sighting proposes
    nothing at all, which is the whole point during the learning period.
    """
    ensure_seeded(conn)
    key = normalized_key(payee)
    if not key:
        return Suggestion(ACTION_LEAVE)
    if not _known(conn, key):
        return Suggestion(ACTION_LEAVE)
    answering, scope = _answering_key(conn, key, account_id)
    overall = _known(conn, answering)

    fl = SUGGEST_FLOOR if floor is None else float(floor)
    total_payee = sum(n for _, n in overall)
    coh = overall[0][1] / total_payee if total_payee else 0.0
    cands = ranked_categories(conn, payee, desc, account_id=account_id)

    node_id, depth = _walk(conn, answering, ranked_tokens(conn, desc))
    labels = _labels(conn, node_id) if node_id is not None else []
    if not labels:
        return Suggestion(ACTION_DROPDOWN, candidates=cands, coherence=coh,
                          depth=depth, scope=scope)
    top, top_n = labels[0]
    total = sum(n for _, n in labels)
    purity = top_n / total if total else 0.0
    conf = confidence(top_n, depth if depth else 1)

    # The coherence gate applies AT THE ROOT ONLY, and the depth is what makes
    # that the right rule. A root answer is the key's bare prior -- no token
    # distinguished this row from any other, so the odds of being right ARE the
    # dominant label's share, which is what coherence measures. That is the
    # Amazon case, and it is correctly refused. A node BELOW the root was reached
    # by matching a token that has meant one label every time it appeared;
    # that is direct evidence about this description, and it must not be thrown
    # away because the payee happens to be bimodal overall.
    ok = (top_n >= MIN_COUNT and purity >= AUTO_PURITY and conf >= fl
          and (depth > 0 or coh >= PAYEE_COHERENCE))
    if ok:
        # The value that was filled IN leads its own picker.
        cands = [(top, top_n)] + [(c, n) for c, n in cands if c != top]
        return Suggestion(ACTION_AUTO, category_id=top, candidates=cands,
                          confidence=conf, depth=depth, purity=purity,
                          coherence=coh, scope=scope)
    return Suggestion(ACTION_DROPDOWN, candidates=cands, confidence=conf,
                      depth=depth, purity=purity, coherence=coh, scope=scope)


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------
def _grow(conn, key: str, desc: str, label: int) -> None:
    """Grow ``key``'s trie by one vote: at each node, a label already present
    increments and stops; a DIFFERENT label splits and descends on the
    best-ranked token that tells this row apart from the rows already there --
    one it carries and they did not all share. With no such token the labels
    share the node, and its purity falls, which is what stops it auto-filling.

    The split used to take the next-ranked token whatever it was. For two cards
    whose bank text is byte-identical that is a token both carry, so the second
    card's vote went into a child every later row walked into, and the tree
    confidently named the second card for both."""
    tokens = set(normalize_tokens(desc))
    node_id = _root_id(conn, key, create=True)
    ranked = ranked_tokens(conn, desc)
    used: set[str] = set()
    while True:
        labels = dict(_labels(conn, node_id))
        if not labels or label in labels:
            break
        common = _common(conn, node_id)
        nxt = next((t for t in ranked if t not in used
                    and (common is None or t not in common)), None)
        if nxt is None:
            break                       # nothing distinguishes it: share the node
        used.add(nxt)
        node_id = _child_id(conn, node_id, nxt, key, create=True)
    had_votes = bool(_labels(conn, node_id))
    _bump(conn, "category_node_labels", "node_id", node_id, label)
    _note_common(conn, node_id, tokens, had_votes)


def _learn(conn, key: str, desc: str, label: int, account_id: Optional[int],
           *, payee_level: bool = True, note_tokens: bool = True) -> None:
    toks_raw = normalize_tokens(desc)
    if note_tokens:
        for tok in toks_raw:
            _note_token(conn, tok, label)
    keys = _keys(key, account_id)
    if not payee_level:
        keys = keys[1:]
    for k in keys:
        _bump(conn, "category_payee_stats", "payee_key", k, label)
        # Text-less evidence updates the TALLY ONLY and never enters the trie
        # (see :func:`learn`).
        if toks_raw:
            _grow(conn, k, desc, label)


def learn(conn, payee: Optional[str], desc: str, category_id: Optional[int],
          *, account_id: Optional[int] = None, commit: bool = True) -> None:
    """Record that the user put ``category_id`` -- a LABEL: a category id, or a
    transfer account from :func:`account_label` -- on a row for ``payee`` in
    ``account_id``.

    The vote goes to the payee's tree and, given ``account_id``, to the payee's
    tree in that account (see the module docstring). A ``None`` label teaches
    nothing (the user left the row blank); so does a blank payee.

    Text-less evidence updates the TALLY ONLY and never enters the trie. A
    register edit (and 30 years of imported Quicken rows) carries a payee and a
    label but no bank text; with no tokens to walk, every such vote would land
    on the root, and the root is where mismatched labels accumulate. Costco's
    root would end up Groceries 249 / Auto:Fuel 45 / Household 8 -- purity 0.82,
    below :data:`AUTO_PURITY` -- and the one case the tree gets *right* with no
    effort ("COSTCO WHSE ..." -> Groceries) would stop auto-filling.
    """
    key = normalized_key(payee)
    if not key or category_id is None or int(category_id) == 0:
        return
    _learn(conn, key, desc, int(category_id), account_id)
    if commit:
        conn.commit()


def unlearn(conn, payee: Optional[str], desc: str,
            category_id: Optional[int], *, account_id: Optional[int] = None,
            commit: bool = True) -> None:
    """Withdraw one vote recorded by :func:`learn` (an accept that was undone).

    Decrements the tally and the answering node holding that label in each tree
    the vote went to; rows that reach zero are deleted so a withdrawn correction
    cannot keep a stale label in the picker.
    """
    key = normalized_key(payee)
    if not key or category_id is None or int(category_id) == 0:
        return
    lab = int(category_id)
    toks = ranked_tokens(conn, desc)
    for k in _keys(key, account_id):
        node_id, _depth = _walk(conn, k, toks)
        if node_id is not None:
            _drop(conn, "category_node_labels", "node_id", node_id, lab)
        _drop(conn, "category_payee_stats", "payee_key", k, lab)
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# forgetting
# ---------------------------------------------------------------------------
def _key_filter(key: str) -> tuple[str, tuple]:
    """SQL matching the payee's own key and every per-account key under it."""
    prefix = key + _ACCOUNT_SEP
    return ("(payee_key = ? OR substr(payee_key, 1, ?) = ?)",
            (key, len(prefix), prefix))


def forget_payee(conn, payee: Optional[str], *, label: Optional[int] = None,
                 commit: bool = True) -> None:
    """Drop what was learned for one payee -- everything, or just ``label`` (a
    category or transfer account it carried) -- in every account. Recorded in
    ``category_forgotten`` so :func:`bootstrap` does not bring it back. Token
    ranking statistics are global and are left alone."""
    key = normalized_key(payee)
    if not key:
        return
    where, args = _key_filter(key)
    if label is None:
        conn.execute(
            "DELETE FROM category_node_labels WHERE node_id IN "
            f"(SELECT id FROM category_nodes WHERE {where})", args)
        conn.execute(f"DELETE FROM category_nodes WHERE {where}", args)
        conn.execute(f"DELETE FROM category_payee_stats WHERE {where}", args)
    else:
        cat, acct = _cols(label)
        conn.execute(
            "DELETE FROM category_node_labels WHERE category_id IS ? "
            "AND account_id IS ? AND node_id IN "
            f"(SELECT id FROM category_nodes WHERE {where})", (cat, acct) + args)
        conn.execute(
            "DELETE FROM category_payee_stats WHERE category_id IS ? "
            f"AND account_id IS ? AND {where}", (cat, acct) + args)
    newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]
    conn.execute(
        "INSERT INTO category_forgotten(payee_key, label, through_txn_id) "
        "VALUES(?,?,?) ON CONFLICT(payee_key, label) "
        "DO UPDATE SET through_txn_id=excluded.through_txn_id",
        (key, 0 if label is None else int(label), int(newest)))
    if commit:
        conn.commit()


def _forgotten(conn) -> dict:
    """{(payee_key, label): through_txn_id}; label 0 is the whole payee."""
    return {(r["payee_key"], int(r["label"])): int(r["through_txn_id"])
            for r in conn.execute("SELECT * FROM category_forgotten")}


def _is_forgotten(forgotten: dict, key: str, label: int, txn_id: int) -> bool:
    for lab in (0, label):
        through = forgotten.get((key, lab))
        if through is not None and txn_id <= through:
            return True
    return False


# ---------------------------------------------------------------------------
# category merge
# ---------------------------------------------------------------------------
def merge_category(conn, from_id: int, to_id: int) -> None:
    """Move every vote for category ``from_id`` onto ``to_id`` (a category
    merge), summing where both exist. Without it the delete that ends a merge
    cascades the votes away and the survivor starts its tree over. Does not
    commit: :func:`mammon.ledger.merge_category` commits the whole merge."""
    for table, keycol in (("category_node_labels", "node_id"),
                          ("category_payee_stats", "payee_key"),
                          ("category_token_labels", "token")):
        rows = conn.execute(
            f"SELECT {keycol} AS k, count FROM {table} WHERE category_id=?",
            (int(from_id),)).fetchall()
        for r in rows:
            _bump(conn, table, keycol, r["k"], int(to_id), int(r["count"]))
        conn.execute(f"DELETE FROM {table} WHERE category_id=?", (int(from_id),))


# ---------------------------------------------------------------------------
# bootstrap and the one-time v116 seed
# ---------------------------------------------------------------------------
def _review_rows(conn) -> list[dict]:
    """Every accepted review row joined to the transaction it created, in the
    order it was accepted: the only place the raw bank text survives beside
    the category or transfer account the user chose. Investment rows are
    excluded (``investment_transactions`` has no category)."""
    rows = conn.execute(
        "SELECT t.id AS txn_id, t.payee AS payee, ri.memo AS memo, "
        "       ri.payee AS src_payee, ri.payee_supplied AS supplied, "
        "       ri.account_id AS account_id, t.category_id AS category_id, "
        "       t.transfer_account_id AS transfer_account_id "
        "FROM review_items ri JOIN transactions t ON t.id = ri.accepted_txn_id "
        "WHERE ri.state='accepted' AND ri.is_investment=0 "
        "  AND (t.category_id IS NOT NULL OR t.transfer_account_id IS NOT NULL) "
        "  AND COALESCE(t.payee,'') != '' "
        "ORDER BY ri.date, ri.id"
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "txn_id": int(r["txn_id"]),
            "payee": r["payee"],
            "text": source_text(r["memo"], r["src_payee"], bool(r["supplied"] or 0)),
            "account_id": int(r["account_id"]),
            "label": label_for(r["category_id"], r["transfer_account_id"]),
            "transfer": r["transfer_account_id"] is not None,
        })
    return out


def _register_rows(conn) -> list[dict]:
    """Every categorized or transfer register row (no text: tally only)."""
    rows = conn.execute(
        "SELECT id, payee, account_id, category_id, transfer_account_id "
        "FROM transactions "
        "WHERE (category_id IS NOT NULL OR transfer_account_id IS NOT NULL) "
        "  AND scheduled=0 AND COALESCE(payee,'') != ''"
    ).fetchall()
    return [{"txn_id": int(r["id"]), "payee": r["payee"],
             "account_id": int(r["account_id"]),
             "label": label_for(r["category_id"], r["transfer_account_id"])}
            for r in rows]


def snapshot_stats(conn, *, commit: bool = True) -> int:
    """Rebuild ``category_token_freq`` (frequency + label entropy) from the
    accepted review rows. Ranking must be computed BEFORE the trees are grown,
    because a trie built under one ranking is unreachable under another.
    Returns the token count."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for r in _review_rows(conn):
        for tok in normalize_tokens(r["text"]):
            counts[tok][r["label"]] += 1
    conn.execute("DELETE FROM category_token_freq")
    conn.execute("DELETE FROM category_token_labels")
    for tok, labs in counts.items():
        total = sum(labs.values())
        ent = -sum((v / total) * math.log2(v / total) for v in labs.values())
        conn.execute(
            "INSERT INTO category_token_freq(token, freq, entropy) VALUES(?,?,?)",
            (tok, total, ent))
        for lab, n in labs.items():
            _bump(conn, "category_token_labels", "token", tok, int(lab), int(n))
    if commit:
        conn.commit()
    return len(counts)


def bootstrap(conn, *, commit: bool = True) -> int:
    """Build every tree from scratch out of what the user already accepted.

    Three passes, in this order, and the order matters:

    1. the tallies, from the WHOLE register -- so the coherence gate and the
       picker ranking have every categorization and transfer the user ever
       made, including the decades of imported history that carry no text;
    2. the token ranking snapshot, from the review list (the only rows with
       real bank text);
    3. the tries themselves, replaying those review rows in the order they
       were accepted.

    Anything the user told Mammon to forget is skipped. Not run on open (by
    request); an explicit seed-from-history action. Returns the number of
    review rows replayed. Idempotent: clears first.
    """
    clear(conn, commit=False)
    forgotten = _forgotten(conn)
    for r in _register_rows(conn):
        key = normalized_key(r["payee"])
        if not key or _is_forgotten(forgotten, key, r["label"], r["txn_id"]):
            continue
        for k in _keys(key, r["account_id"]):
            _bump(conn, "category_payee_stats", "payee_key", k, r["label"])
    snapshot_stats(conn, commit=False)
    rows = _review_rows(conn)
    for r in rows:
        key = normalized_key(r["payee"])
        if not key or _is_forgotten(forgotten, key, r["label"], r["txn_id"]):
            continue
        # The tallies already counted this row via the register pass; grow
        # only the tries here so a review row is not voted twice.
        if normalize_tokens(r["text"]):
            for k in _keys(key, r["account_id"]):
                _grow(conn, k, r["text"], r["label"])
    _meta_set(conn, _SEED_FLAG, "done")
    if commit:
        conn.commit()
    return len(rows)


def ensure_bootstrapped(conn) -> None:
    """Build the trees once, on first use, if the tables are empty."""
    row = conn.execute("SELECT COUNT(*) AS c FROM category_payee_stats").fetchone()
    if int(row["c"]) == 0:
        bootstrap(conn)


def ensure_seeded(conn) -> int:
    """Migration 116's one-time step, a no-op afterwards and in a ledger that
    had nothing accepted when it upgraded.

    The trees held category votes only, payee-wide only; transfer accounts
    were learned by the keyword table the migration dropped. This replays the
    ACCEPTED REVIEW ROWS -- the same evidence the trees were grown from, and
    the only evidence: the app never seeds from register history
    -- adding what the old shape could not hold: every transfer's vote, in the
    payee's tree and its account's, and every category vote in the account's
    tree (the payee's tree already has those). Returns the rows replayed."""
    try:
        if _meta_get(conn, _SEED_FLAG) != "pending":
            return 0
    except Exception:
        return 0                        # a read-only or pre-v116 connection
    n = 0
    for r in _review_rows(conn):
        key = normalized_key(r["payee"])
        if not key:
            continue
        _learn(conn, key, r["text"], r["label"], r["account_id"],
               payee_level=r["transfer"], note_tokens=r["transfer"])
        n += 1
    _meta_set(conn, _SEED_FLAG, "done")
    conn.commit()
    return n


def _meta_get(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM category_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO category_meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------------------
# management surface
# ---------------------------------------------------------------------------
def list_payees(conn) -> list[dict]:
    """One row per payee the tree knows: key, votes, label count, coherence.
    Per-account trees are folded into their payee (see :func:`labels_by_account`).
    Feeds the learned-payees view so the GUI issues no SQL of its own."""
    rows = conn.execute(
        "SELECT payee_key, SUM(count) AS votes, COUNT(*) AS cats, "
        "       MAX(count) AS top "
        "FROM category_payee_stats WHERE instr(payee_key, char(9)) = 0 "
        "GROUP BY payee_key ORDER BY payee_key"
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


def payee_labels(conn, payee: Optional[str], *,
                 account_id: Optional[int] = None) -> list[tuple[int, int]]:
    """``[(label, count)]`` for the payee, most used first: in ``account_id``
    when it has any history there, else across every account. What the payee
    completer lists beside a payee and the learned-payees view shows."""
    if account_id is not None:
        here = known_categories(conn, payee, account_id=account_id)
        if here:
            return here
    return known_categories(conn, payee)


def labels_by_account(conn, payee: Optional[str]) -> dict[int, list[tuple[int, int]]]:
    """``{account_id: [(label, count)]}`` -- the payee's per-account trees."""
    key = normalized_key(payee)
    if not key:
        return {}
    prefix = key + _ACCOUNT_SEP
    rows = conn.execute(
        f"SELECT payee_key, {_LABEL_SQL} AS label, count FROM category_payee_stats "
        "WHERE substr(payee_key, 1, ?) = ? ORDER BY count DESC, (label < 0), ABS(label)",
        (len(prefix), prefix)).fetchall()
    out: dict[int, list] = defaultdict(list)
    for r in rows:
        out[int(r["payee_key"][len(prefix):])].append((int(r["label"]), int(r["count"])))
    return dict(out)


def payee_summary(conn, payee: Optional[str]) -> dict:
    """``{payee_key, coherence, auto_ok, categories: [(label, count)]}``."""
    key = normalized_key(payee)
    cats = known_categories(conn, payee)
    coh = coherence(conn, payee)
    return {"payee_key": key, "coherence": coh,
            "auto_ok": coh >= PAYEE_COHERENCE, "categories": cats}


def clear(conn, *, commit: bool = True) -> None:
    """Wipe every tree (a rebuild starts here). What was forgotten stays
    forgotten."""
    for table in ("category_node_labels", "category_nodes",
                  "category_payee_stats", "category_token_freq",
                  "category_token_labels"):
        conn.execute(f"DELETE FROM {table}")
    if commit:
        conn.commit()
