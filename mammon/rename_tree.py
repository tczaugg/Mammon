"""Payee renaming and investment-action mapping as a DECISION TREE rebuilt from the
user's accepted corrections (the user's design; modelled on webSlinger's
``selector_tree`` with features replaced by tokens and branching fixed at one).

Two domains share the engine (see ``_DOMAINS``): ``payee`` -- a bank's statement
text (and/or the source's own payee field) -> the payee the user chose -- and
``action`` -- a broker's raw activity text -> the Quicken investment action the
user kept. Both are importer vocabulary guesses that only the user's own
corrections can make right.

Why the previous design was replaced
------------------------------------
The old engine was an ONLINE trie: every accept walked the description's tokens
(ranked by an entropy snapshot) and either bumped a count on an existing node or
split to a child on the next-ranked token. Measured on the user's fresh ledger it
had four defects, and each is structural rather than a threshold:

* **Splits reset the count.** A payee corrected three times could sit at a child
  node with count 1, because the examples learned BEFORE the split stayed on
  the parent. That is why the third correction still showed the bank's text.
* **The split token did not discriminate.** A differing payee descended on the
  next-ranked token of the NEW description, not on a token that told the two
  descriptions apart -- the Venmo tree split on ``AUTOMATIC``, present in both.
* **The deepest node hid its ancestors.** ``MOBILE DEPOSIT`` had been renamed
  to the same person twice, but the walk stopped on a one-off child, scored
  below the confidence floor, and the register offered nothing at all.
* **State could not follow the ledger.** A learned label lived on forever even
  when the transaction behind it was undone or its payee edited in the register;
  a title-cased copy of the bank text once learned as a "payee" is exactly the
  "name I never entered" the user saw.

The engine is now a pure function of the CURRENT accepted corrections. Nothing
accumulates online; a register edit or an undo changes the next answer.

Corpus: the rename example log
------------------------------
``rename_examples`` (schema v60) records one row per accepted review row -- the
source text, the source's own payee field (``extra``), the label the user chose,
and the id of the transaction the accept created. The label is read LIVE through
that id at query time (a payee edited in the register is the correction of a
correction), falling back to the stored label only when the transaction is gone.
Review retention (:func:`mammon.import_review.purge_old_batches`) never touches
this table, so a rename taught a year ago -- an annual bill -- survives the
review rows it came from. The table is seeded from the review rows that existed
at migration.

**Only corrections train the payee domain.** An example whose label is the
text it was shown with -- the description, its title-cased form, or the
supplied payee -- is a row the user (or a bulk accept) kept as-is, not a
rename, and is skipped when the corpus is loaded. The action domain keeps every
accept: an importer's guessed action that the user kept is a confirmation
worth counting, where a kept description is not.

Tokens
------
Upper-cased alphanumeric runs, minus pure numbers, tokens under three
characters, and mixed letter+digit tokens that fewer than two examples carry
(auth codes, ISINs, masked ids -- per-transaction noise that would otherwise be
the most "specific" feature of every row). ``extra`` -- the source's payee field
-- is tokenized into the same pool, so a feed that puts its description in the
payee column (the Costco card) trains and queries the same way as one that
sends a ``statementDescription``. Bank boilerplate (``keywords._NOISE``) is a
token like any other for the tree, but it is never *evidence* on its own.

Answering a query (:func:`suggest`)
----------------------------------
1. **Candidates.** The examples that could possibly be this row: those sharing
   a DISTINCTIVE token with it -- a non-boilerplate token that fewer than
   :data:`MAX_LABELS_PER_TOKEN` payees have carried -- plus any example whose
   whole token set equals the query's (how an all-boilerplate text like
   ``MOBILE DEPOSIT`` finds its own history). A town name shared by fifty
   merchants is not evidence of any one of them; ``WALMART`` is.
2. **The tree.** Over those candidates, a binary decision tree on token
   PRESENCE, grown greedily by information gain over the labels (ties: prefer a
   non-boilerplate token, then the one more examples carry). A node whose
   examples all share one label is a leaf; so is one no token can split, which
   only happens when the examples' token sets are identical -- the same text
   renamed two ways. Building it per query over the candidates is the same
   tree the whole corpus would grow on the branch this row takes, at a
   fraction of the cost: a corpus-wide tree over token presence is a decision
   LIST as deep as the number of payees.
3. **The leaf's pattern must fit the row.** A leaf is reached through ABSENT
   edges as readily as present ones, so landing on it proves nothing by
   itself. The leaf's examples are generalized the way webSlinger generalizes
   an array selector over its fields (the user's design): every example is a
   set of FEATURES -- each token, the token before it, the token after it, and
   the same over the source's payee field -- and the pattern keeps a feature
   only when every example has it: with its value when they all agree, as a
   bare "something is here" slot when they differ, and not at all when any
   example lacks it. The row fits when it has every feature the pattern kept,
   with the agreed value where there is one. So two rows renamed Tenant --
   ``June rent`` and ``July rent`` -- generalize to "a token, then RENT", which
   ``August rent`` fits and a bare ``rent`` does not; once the user names
   ``rent`` too, the slot before RENT is dropped and anything carrying RENT
   fits. A pattern learned from one shape stays exact, so ``VENMO PAYMENT``
   cannot inherit the ``VENMO CASHOUT`` leaf and a youth theater in Anytown
   cannot inherit Walmart's exact rows on the town's tokens alone.
   Generalization is earned by observed variation, never assumed. (A label
   whose examples share no merchant token at all -- Amazon's several formats
   -- is first split by shape, so a row is fitted against the format it
   resembles rather than against a pattern too loose to mean anything.)
4. **Fill or offer.** The leaf's leading label is filled when the row fits its
   pattern, at least :data:`MIN_FILL` of its examples back it ("corrected at
   least twice", by request), and it holds at least :data:`FILL_PURITY` of the
   leaf -- a contested leaf fills only once one payee outvotes the rest by
   that ratio, else the raw text stays and the payees go in the dropdown.
   Anything else is a dropdown: the leaf's labels first, then every other
   candidate label, each needing :data:`MIN_OFFER` sightings to be offered at
   all. A payee chosen once is neither filled nor offered.

Replayed cold over the user's fresh ledger (42 corrections) this fills 20 rows,
all correctly, with the first fill arriving on the third sighting of every
recurring payee; the online trie filled 15, one of them wrong, on the fourth.
Over the older ledger's 679 accepted rows it fills 369 at 98.6% -- the five
misses are two payees the user spelled two ways, one genuinely ambiguous
deposit, and two first sightings (a Comcast line that is a subset of an
Xfinity one, and a youth theater fitted to Walmart because Walmart's own
token varied across its rows and the town's slot had generalized) -- against
the trie's 255 at 96.9%; on the rows it leaves blank the dropdown holds the
right name 71 times in 138. (The earlier bag-of-tokens guard, which refused
any row carrying a token the leaf had never seen, reached 262 fills at 98.5%:
the pattern rule buys back the rows whose only novelty sits in a slot the
examples had already shown to vary.)

Management (``rename_stats``: applied / overridden per payee, and
:func:`forget_payee`) is unchanged in shape. :func:`bootstrap` and friends still
exist as an explicit seed-from-history action; nothing calls them on open (see
CLAUDE.md: hand-typed memos taught the old tree payees never chosen in review).
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from . import keywords

__all__ = [
    "normalize_tokens",
    "tidy_text",
    "confidence",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "HIGH_CONFIDENCE_MIN_COUNT",
    "MIN_FILL",
    "MIN_OFFER",
    "FILL_PURITY",
    "MAX_LABELS_PER_TOKEN",
    "learn",
    "suggest",
    "Suggestion",
    "Node",
    "Example",
    "make_example",
    "build_tree",
    "generalize",
    "fits",
    "ACTION_AUTO",
    "ACTION_DROPDOWN",
    "ACTION_LEAVE",
    "note_applied",
    "note_overridden",
    "rename_stats",
    "forget_payee",
    "forget_examples",
    "examples",
    "sources_for",
    "bootstrap",
    "bootstrap_actions",
    "ensure_bootstrapped",
    "clear",
]

# Split a description into UPPER-CASED alphanumeric tokens.
_TOKEN_SPLIT = re.compile(r"[^A-Z0-9]+")

# Tokens shorter than this are dropped as noise (single letters, "SQ", "WM").
MIN_TOKEN_LEN = 3

ACTION_AUTO = "auto"          # one label, matched, corroborated -> fill it in
ACTION_DROPDOWN = "dropdown"  # contested or unsure -> typeable dropdown, raw text stays
ACTION_LEAVE = "leave"        # nothing shares evidence with this row

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

# How many examples the matched leaf needs before its single label is written
# into the cell. TWO, by request: "show the raw payee ... until the user has
# corrected it at least twice". The old engine's 3-4 was compensating for
# counts that reset on every split; the leaf of a rebuilt tree holds every
# example that reaches it, so the count is the real number of sightings.
MIN_FILL = 2

# How many sightings a payee needs before it is OFFERED in the dropdown at all.
# One sighting is not evidence, and a name the user has picked once is noise
# in the list rather than help (by request).
MIN_OFFER = 2

# A contested leaf -- the same text renamed more than one way -- fills its
# leading payee only when that payee holds at least this share of the leaf's
# examples (the user's rule). Nine Citibank rows outvote one "Citi Bank" typo;
# a mobile deposit split five to one between two people stays a dropdown.
FILL_PURITY = 0.9

# A token that this many or fewer payees have carried is DISTINCTIVE -- sharing
# it with an example makes that example a candidate. Above this the token is a
# town, a bank's word for "purchase", or a marketplace's name, and sharing it
# says nothing about which payee this is. Five keeps ``COSTCO`` (Costco, Costco
# Gas) and a household's handful of Venmo counterparties; a card feed's town
# name passes it within the first month.
MAX_LABELS_PER_TOKEN = 5

# Back-compat alias: callers and tests that ask "how many corroborating examples
# before a rename is applied" get the fill floor.
HIGH_CONFIDENCE_MIN_COUNT = MIN_FILL

# The two learned domains. ``live`` names where the label is read at query time
# (the transaction the accept created, so a later register edit or undo is
# honoured); ``corrections_only`` is whether a kept default counts as an
# example.
_DOMAINS = {
    "payee": {
        "live_table": "transactions", "live_col": "payee",
        "meta_key": "bootstrapped", "corrections_only": True,
    },
    "action": {
        "live_table": "investment_transactions", "live_col": "action",
        "meta_key": "action_bootstrapped", "corrections_only": False,
    },
}

_NOISE = keywords._NOISE


# ---------------------------------------------------------------------------
# text normalization
# ---------------------------------------------------------------------------
def normalize_tokens(desc: str) -> list[str]:
    """UPPER-CASED tokens of ``desc`` with numeric/short noise dropped.

    Splits on non-alphanumerics, then discards pure-numeric tokens (auth codes,
    store/check numbers) and tokens shorter than :data:`MIN_TOKEN_LEN`, so a
    per-transaction number can never become a feature. Duplicates are
    collapsed, first occurrence order preserved.
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


def _is_mixed(tok: str) -> bool:
    """A token mixing letters and digits (ISIN, auth code, masked id)."""
    return any(ch.isdigit() for ch in tok) and any(ch.isalpha() for ch in tok)


_MULTISPACE = re.compile(r"\s+")

# Tokens that stay upper-cased when an all-caps feed line is title-cased.
_KEEP_UPPER = {"ACH", "POS", "ATM", "LLC", "US", "USA", "ID", "PPD", "CCD"}


def tidy_text(text: str) -> str:
    """The display default for a description with no rename: whitespace
    collapsed and, when the feed shouts in ALL CAPS, title-cased for
    readability. Mixed-case (human-formatted) text is left alone.

    Lives here, not in the review module, because the corpus loader has to
    recognise this exact string: a row whose payee IS its tidied description
    was kept as-is, and is not a correction (see the module docstring)."""
    s = _MULTISPACE.sub(" ", (text or "").strip())
    if not s:
        return ""
    letters = [c for c in s if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        s = " ".join(tok.upper() if tok.upper() in _KEEP_UPPER else tok.capitalize()
                     for tok in s.split(" "))
    return s


def _norm(s: Optional[str]) -> str:
    """Case- and whitespace-insensitive comparison key."""
    return _MULTISPACE.sub(" ", (s or "").strip()).lower()


def is_correction(label: Optional[str], text: Optional[str],
                  extra: Optional[str] = "") -> bool:
    """Whether ``label`` is a RENAME of the row it was accepted with, rather
    than the default the row was shown with (the description, its tidied form,
    or the supplied payee field)."""
    key = _norm(label)
    if not key:
        return False
    return key not in {_norm(text), _norm(tidy_text(text or "")), _norm(extra)}


def confidence(hit_count: int, depth: int) -> float:
    """``hits / (hits + depth)``: rises with corroborating examples, falls with
    the number of splits it took to isolate them. Kept as a descriptive score
    (and for :mod:`mammon.category_tree`); it gates nothing here."""
    hc = max(0, int(hit_count))
    if hc <= 0:
        return 0.0
    d = max(1, int(depth))
    return hc / (hc + d)


# ---------------------------------------------------------------------------
# the corpus: accepted examples with live labels
# ---------------------------------------------------------------------------
# The value a pattern slot takes when the examples disagree: "some token is
# here" (webSlinger's binarized feature).
ANY = "*"

# The feature a row carries when its source sent NO payee field. Absence is a
# value, not a gap: rows learned from a description-only feed must not fit a
# row whose source names a counterparty in its own column (the Venmo case),
# and rows learned from a payee-column feed must not fit a bare description.
_NO_FIELD = "f:none"


@dataclass(frozen=True)
class Example:
    """One accepted correction, tokenized.

    ``seq`` is the usable tokens of the text in order, ``fseq`` those of the
    supplied payee field; ``tokens`` is their union (the tree's features);
    ``features`` is the pattern vocabulary of step 3 (see :func:`features`).
    """
    id: int
    seq: tuple
    fseq: tuple
    label: str
    text: str = ""
    extra: str = ""

    @property
    def tokens(self) -> frozenset:
        return frozenset(self.seq) | frozenset(self.fseq)

    @property
    def features(self) -> dict:
        return features(self.seq, self.fseq)


def features(seq, fseq=()) -> dict:
    """The feature set of one row: for each token of the description, the token
    itself (``has:T``), the token before it (``prev:T``) and after it
    (``next:T``); the same over the payee field under an ``f:`` prefix; and
    :data:`_NO_FIELD` when there is no field. ``prev``/``next`` exist only where
    a neighbour exists, so a first or last token has none -- that absence is
    what lets a pattern learned from ``June rent`` reject a bare ``rent``."""
    out: dict = {}
    for prefix, toks in (("", tuple(seq)), ("f:", tuple(fseq))):
        for i, t in enumerate(toks):
            out[prefix + "has:" + t] = t
            if i > 0:
                out[prefix + "prev:" + t] = toks[i - 1]
            if i + 1 < len(toks):
                out[prefix + "next:" + t] = toks[i + 1]
    if not fseq:
        out[_NO_FIELD] = _NO_FIELD
    return out


def generalize(examples) -> dict:
    """The pattern shared by ``examples``: every feature all of them carry,
    with its value when they agree and :data:`ANY` when they differ. A feature
    any example lacks is dropped (the user's array-selector rule)."""
    if not examples:
        return {}
    feats = [e.features for e in examples]
    names = set(feats[0]).intersection(*(set(f) for f in feats[1:]))
    pattern: dict = {}
    for name in names:
        values = {f[name] for f in feats}
        pattern[name] = values.pop() if len(values) == 1 else ANY
    return pattern


def fits(pattern: dict, row_features: dict) -> bool:
    """Whether a row carries every feature of ``pattern`` with the agreed
    value where the pattern has one."""
    for name, value in pattern.items():
        got = row_features.get(name)
        if got is None or (value is not ANY and got != value):
            return False
    return True


def _raw_tokens(text: str, extra: str) -> set:
    toks = set(normalize_tokens(text))
    if extra:
        toks |= set(normalize_tokens(extra))
    return toks


def make_example(id_: int, text: str, label: str, extra: str = "") -> Example:
    """An :class:`Example` outside any corpus (tests, inspection): every token
    is kept, mixed letter+digit ones included."""
    return Example(id_, tuple(normalize_tokens(text)), tuple(normalize_tokens(extra)),
                   label, text, extra)


class _Corpus:
    """The examples of one domain plus the indexes :func:`suggest` reads."""

    def __init__(self, rows):
        # rows: (id, text, extra, label). Mixed letter+digit tokens count only
        # once at least two examples carry them (see the module docstring).
        freq: Counter = Counter()
        staged = []
        for rid, text, extra, label in rows:
            staged.append((rid, text, extra, label))
            freq.update(_raw_tokens(text, extra))
        self._freq = freq
        self.examples: list[Example] = []
        self.by_token: dict = defaultdict(set)
        self.labels_of: dict = defaultdict(set)
        self.by_set: dict = defaultdict(set)
        self.sightings: Counter = Counter()
        for rid, text, extra, label in staged:
            seq, fseq = self.sequences(text, extra)
            toks = frozenset(seq) | frozenset(fseq)
            if not toks:
                continue
            i = len(self.examples)
            self.examples.append(Example(rid, seq, fseq, label, text, extra))
            for t in toks:
                self.by_token[t].add(i)
                self.labels_of[t].add(label)
            self.by_set[toks].add(i)
            self.sightings[label] += 1

    def usable(self, tok: str) -> bool:
        return (not _is_mixed(tok)) or self._freq.get(tok, 0) >= 2

    def sequences(self, text: str, extra: str = "") -> tuple:
        """``(description tokens, payee-field tokens)`` in order, usable ones only."""
        return (tuple(t for t in normalize_tokens(text) if self.usable(t)),
                tuple(t for t in normalize_tokens(extra or "") if self.usable(t)))

    def tokens(self, text: str, extra: str = "") -> frozenset:
        seq, fseq = self.sequences(text, extra)
        return frozenset(seq) | frozenset(fseq)


_CACHE: dict = {}   # kind -> (fingerprint, _Corpus)


def _corpus_rows(conn, kind: str) -> list[tuple]:
    """``(id, text, extra, label)`` for every usable example of ``kind``, with
    the label read LIVE from the transaction the accept created."""
    dom = _DOMAINS[kind]
    rows = conn.execute(
        f"SELECT e.id AS id, e.text AS text, e.extra AS extra, e.label AS label, "
        f"       live.id AS live_id, live.{dom['live_col']} AS live_label "
        f"FROM rename_examples e "
        f"LEFT JOIN {dom['live_table']} live ON live.id = e.txn_id "
        f"WHERE e.kind=? ORDER BY e.id", (kind,)).fetchall()
    known_action = None
    if kind == "action":
        from . import investments
        known_action = investments.is_known_action
    out = []
    for r in rows:
        if r["live_id"] is not None:
            label = (r["live_label"] or "").strip()   # the row still exists: its
            if not label:                             # current value is the truth
                continue
        else:
            label = (r["label"] or "").strip()
        if not label:
            continue
        text = r["text"] or ""
        extra = r["extra"] or ""
        if known_action is not None and extra and known_action(extra):
            extra = ""      # a canonical action is the answer, not evidence
        if dom["corrections_only"] and not is_correction(label, text, extra):
            continue
        out.append((int(r["id"]), text, extra, label))
    return out


def _corpus(conn, kind: str) -> _Corpus:
    """The domain's corpus, rebuilt whenever the underlying rows change.

    The rows are cheap to read (one indexed SELECT over the example log); the
    tokenizing and indexing are what the fingerprint saves. Keyed by content,
    so two connections to the same file share, and a register edit that changes
    a live label invalidates it without any bookkeeping."""
    rows = _corpus_rows(conn, kind)
    fp = hash(tuple(rows))
    hit = _CACHE.get(kind)
    if hit is not None and hit[0] == fp:
        return hit[1]
    corpus = _Corpus(rows)
    _CACHE[kind] = (fp, corpus)
    return corpus


# ---------------------------------------------------------------------------
# the decision tree
# ---------------------------------------------------------------------------
@dataclass
class Node:
    """A tree node. ``token`` is the split feature (``None`` for a leaf);
    ``present`` / ``absent`` are the children for rows that carry / lack it."""
    examples: list
    token: Optional[str] = None
    present: Optional["Node"] = None
    absent: Optional["Node"] = None

    @property
    def is_leaf(self) -> bool:
        return self.token is None

    @property
    def labels(self) -> Counter:
        return Counter(e.label for e in self.examples)


def _entropy(counts: Counter, n: int) -> float:
    return -sum((c / n) * math.log2(c / n) for c in counts.values() if c)


def build_tree(examples: list) -> Node:
    """Grow the decision tree over ``examples`` (branching one: a single token
    per node, present vs. absent).

    At each node the split token is the one that minimises the label entropy of
    the two children (maximum information gain). A token every example carries
    splits nothing and is skipped; a node with one label, or one no token can
    split, is a leaf. Ties break toward a non-boilerplate token, then the token
    more of the node's examples carry, then alphabetically -- deterministic, so
    the same corpus always yields the same tree.
    """
    labels = Counter(e.label for e in examples)
    if len(labels) <= 1:
        return Node(examples)
    n = len(examples)
    tok_labels: dict = defaultdict(Counter)
    tok_n: Counter = Counter()
    for e in examples:
        for t in e.tokens:
            tok_labels[t][e.label] += 1
            tok_n[t] += 1
    best = None
    for t, present in tok_labels.items():
        n_p = tok_n[t]
        if n_p == n:
            continue                           # in every example: no split
        n_a = n - n_p
        absent = labels - present
        remaining = (n_p / n) * _entropy(present, n_p) + (n_a / n) * _entropy(absent, n_a)
        key = (remaining, t in _NOISE, -n_p, t)
        if best is None or key < best[0]:
            best = (key, t)
    if best is None:
        return Node(examples)                  # identical token sets: contested
    tok = best[1]
    return Node(examples, tok,
                build_tree([e for e in examples if tok in e.tokens]),
                build_tree([e for e in examples if tok not in e.tokens]))


def _walk(node: Node, toks: frozenset) -> tuple[Node, int]:
    depth = 0
    while not node.is_leaf:
        node = node.present if node.token in toks else node.absent
        depth += 1
    return node, depth


# ---------------------------------------------------------------------------
# suggesting
# ---------------------------------------------------------------------------
@dataclass
class Suggestion:
    """A rename decision for one row.

    ``action`` is :data:`ACTION_AUTO`, :data:`ACTION_DROPDOWN` or
    :data:`ACTION_LEAVE`; ``payee`` the label to fill (AUTO) or the dropdown's
    first entry; ``candidates`` is ``[(label, count), ...]`` -- the matched
    leaf's labels by leaf count, then other candidate labels by sightings, all
    at or above :data:`MIN_OFFER`. ``high_confidence`` is true exactly for AUTO.
    ``confidence`` is the fitted group's size against the depth walked.
    """

    action: str
    payee: str = ""
    candidates: list = field(default_factory=list)
    confidence: float = 0.0
    depth: int = 0
    high_confidence: bool = False

    @property
    def payees(self) -> list:
        """Just the candidate names, best first."""
        return [p for p, _ in self.candidates]

    @property
    def tier(self) -> str:
        return CONFIDENCE_HIGH if self.high_confidence else CONFIDENCE_LOW


def _candidates(corpus: _Corpus, toks: frozenset) -> list:
    """Indexes of the examples that could be this row (module docstring, step 1)."""
    found: set = set()
    for t in toks:
        if t in _NOISE:
            continue
        if len(corpus.labels_of.get(t, ())) > MAX_LABELS_PER_TOKEN:
            continue
        found |= corpus.by_token.get(t, set())
    found |= corpus.by_set.get(toks, set())
    return sorted(found)


def _anchored(pattern: dict) -> bool:
    """Whether a pattern pins at least one real (non-boilerplate) token."""
    return any(name.split("has:", 1)[1] not in _NOISE
               for name, value in pattern.items()
               if "has:" in name and value is not ANY)


def _shape_group(examples: list, toks: frozenset) -> list:
    """The examples of one label that share the row's SHAPE.

    A label learned from several unrelated formats (Amazon's marketplace,
    royalty and web-services lines) generalizes to a pattern that pins no
    merchant token at all, which would fit anything. While that is so, split
    the examples on the real token most of them carry and keep the side the
    row is on, so the row is fitted against the format it resembles. A label
    whose examples already share a token, or whose text is nothing but
    boilerplate (``MOBILE DEPOSIT``), is returned whole.
    """
    while len(examples) > 1 and not _anchored(generalize(examples)):
        n = len(examples)
        presence: Counter = Counter()
        for e in examples:
            for t in e.tokens:
                if t not in _NOISE:
                    presence[t] += 1
        splits = [(c, t) for t, c in presence.items() if c < n]
        if not splits:
            break
        _, tok = max(splits, key=lambda ct: (ct[0], ct[1]))
        examples = [e for e in examples if (tok in e.tokens) == (tok in toks)]
    return examples


def suggest(conn, desc: str, *, kind: str = "payee", extra: str = "") -> Suggestion:
    """Decide how to map ``desc`` (+ ``extra``, the source's own payee field)
    for the ``kind`` domain. See the module docstring for the four steps."""
    corpus = _corpus(conn, kind)
    seq, fseq = corpus.sequences(desc, extra)
    toks = frozenset(seq) | frozenset(fseq)
    if not toks or not corpus.examples:
        return Suggestion(ACTION_LEAVE)
    idx = _candidates(corpus, toks)
    if not idx:
        return Suggestion(ACTION_LEAVE)
    cands = [corpus.examples[i] for i in idx]
    leaf, depth = _walk(build_tree(cands), toks)
    counts = leaf.labels
    top, n_top = counts.most_common(1)[0]
    ratio = n_top / len(leaf.examples)
    # Step 3: the leading label's pattern must fit the row.
    group = _shape_group([e for e in leaf.examples if e.label == top], toks)
    fit = fits(generalize(group), features(seq, fseq))
    # Step 4: fill, or offer.
    leaf_labels = [(lab, c) for lab, c in counts.most_common()
                   if fit and corpus.sightings[lab] >= MIN_OFFER]
    shown = {lab for lab, _ in leaf_labels}
    others = Counter(e.label for e in cands if e.label not in shown)
    offered = leaf_labels + [(lab, corpus.sightings[lab]) for lab, _ in others.most_common()
                             if corpus.sightings[lab] >= MIN_OFFER]
    conf = confidence(len(group), depth)
    if fit and len(group) >= MIN_FILL and ratio >= FILL_PURITY:
        return Suggestion(ACTION_AUTO, payee=top, candidates=offered,
                          confidence=conf, depth=depth, high_confidence=True)
    if offered:
        return Suggestion(ACTION_DROPDOWN, payee=offered[0][0], candidates=offered,
                          confidence=conf, depth=depth)
    return Suggestion(ACTION_LEAVE, depth=depth)


# ---------------------------------------------------------------------------
# learning: the example log
# ---------------------------------------------------------------------------
def learn(conn, desc: str, payee: str, *, kind: str = "payee", extra: str = "",
          txn_id: Optional[int] = None, review_id: Optional[int] = None,
          commit: bool = True) -> bool:
    """Record one accepted example: ``desc`` (+ ``extra``) was labelled
    ``payee`` (the action, in the action domain).

    ``txn_id`` is the register row the accept created; when given, the label
    is read from that row at query time, so an edit or undo there is honoured.
    Returns ``False`` for a blank label or text with no usable tokens (nothing
    to learn). Whether a kept default counts is decided when the corpus is
    loaded, not here (``_DOMAINS[kind]['corrections_only']``).
    """
    label = (payee or "").strip()
    if not label:
        return False
    if not _raw_tokens(desc or "", extra or ""):
        return False
    conn.execute(
        "INSERT INTO rename_examples(kind, txn_id, review_id, text, extra, label) "
        "VALUES (?,?,?,?,?,?)",
        (kind, txn_id, review_id, (desc or "").strip(), (extra or "").strip(), label))
    if commit:
        conn.commit()
    return True


def forget_examples(conn, *, txn_id: int, kind: str = "payee",
                    commit: bool = True) -> int:
    """Drop the example(s) recorded for a transaction (an accept being undone).
    Returns the number removed."""
    cur = conn.execute("DELETE FROM rename_examples WHERE kind=? AND txn_id=?",
                       (kind, int(txn_id)))
    if commit:
        conn.commit()
    return int(cur.rowcount or 0)


def examples(conn, *, kind: str = "payee") -> list[dict]:
    """The live corpus as ``{id, text, extra, label, tokens}`` rows -- what the
    tree is built from, after the live-label and corrections-only filters.
    Inspection and tests."""
    corpus = _corpus(conn, kind)
    return [{"id": e.id, "text": e.text, "extra": e.extra, "label": e.label,
             "tokens": sorted(e.tokens)} for e in corpus.examples]


def pattern_for(conn, payee: str, *, kind: str = "payee") -> dict:
    """The generalized pattern of every live example labelled ``payee`` --
    what a row has to carry to be filled with it (inspection; the decision
    path fits against the leaf's shape group, which is this or a subset)."""
    corpus = _corpus(conn, kind)
    return generalize([e for e in corpus.examples if e.label == payee])


def sources_for(conn, payee: str, *, kind: str = "payee", limit: int = 40) -> list:
    """The raw source texts that resolve to ``payee``, most recent first --
    the bank's own words for it, which anything wanting them (the
    automatic-payment hint in :mod:`mammon.predictions`) asks here for."""
    corpus = _corpus(conn, kind)
    out: list = []
    seen: set = set()
    for e in reversed(corpus.examples):
        if e.label != payee:
            continue
        text = " ".join(s for s in (e.text, e.extra) if s)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
            if len(out) >= limit:
                break
    return out


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
    """One row per known payee for the management table: ``payee``,
    ``examples`` (live corrections that resolve to it), ``applied`` and
    ``overridden`` tallies. Sorted by payee name."""
    corpus = _corpus(conn, "payee")
    stats: dict = {}
    for r in conn.execute(
        "SELECT payee, applied_count, overridden_count FROM rename_stats"
    ):
        stats[r["payee"]] = (int(r["applied_count"] or 0), int(r["overridden_count"] or 0))
    out = []
    for p in sorted(set(corpus.sightings) | set(stats)):
        applied, overridden = stats.get(p, (0, 0))
        out.append({
            "payee": p,
            "examples": int(corpus.sightings.get(p, 0)),
            "applied": applied,
            "overridden": overridden,
        })
    return out


def forget_payee(conn, payee: str, *, kind: str = "payee", commit: bool = True) -> int:
    """Forget every example that resolves to ``payee`` -- by its live label or
    its stored one -- and drop its tally (management delete). Returns the
    number of examples removed."""
    p = (payee or "").strip()
    if not p:
        return 0
    dom = _DOMAINS[kind]
    cur = conn.execute(
        f"DELETE FROM rename_examples WHERE kind=? AND (label=? OR txn_id IN "
        f"(SELECT id FROM {dom['live_table']} WHERE {dom['live_col']}=?))",
        (kind, p, p))
    if kind == "payee":
        conn.execute("DELETE FROM rename_stats WHERE payee=?", (p,))
    if commit:
        conn.commit()
    return int(cur.rowcount or 0)


def clear(conn, *, kind: str = "payee", commit: bool = True) -> None:
    """Wipe one domain's examples (and, for payees, the tallies) and its
    bootstrap flag."""
    dom = _DOMAINS[kind]
    conn.execute("DELETE FROM rename_examples WHERE kind=?", (kind,))
    if kind == "payee":
        conn.execute("DELETE FROM rename_stats")
    conn.execute("DELETE FROM rename_meta WHERE key=?", (dom["meta_key"],))
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# bootstrap from register history (an EXPLICIT action; nothing calls it on open)
# ---------------------------------------------------------------------------
# The labeled history behind each domain: (row id, evidence text, label).
_CORPUS_SQL = {
    "payee": (
        "SELECT id, memo AS txt, payee AS label FROM transactions "
        "WHERE memo IS NOT NULL AND TRIM(memo) <> '' "
        "  AND payee IS NOT NULL AND TRIM(payee) <> '' "
        "  AND transfer_account_id IS NULL ORDER BY id ASC"
    ),
    "action": (
        "SELECT id, memo AS txt, action AS label FROM investment_transactions "
        "WHERE memo IS NOT NULL AND TRIM(memo) <> '' "
        "  AND action IS NOT NULL AND TRIM(action) <> '' ORDER BY id ASC"
    ),
}


def bootstrap(conn, *, force: bool = False) -> int:
    """Replay register history into the PAYEE example log (idempotent).

    Every posted non-transfer transaction pairs a ``memo`` with a ``payee``.
    That is an accepted rename only for DOWNLOADED rows -- for hand-entered and
    QIF-imported history the memo is a note the user typed -- which is why the
    app no longer calls this on open. It remains for an explicit seed. Guarded
    by a meta flag; returns the number of rows replayed.
    """
    return _bootstrap_kind(conn, "payee", force=force)


def bootstrap_actions(conn, *, force: bool = False) -> int:
    """Replay investment history (memo -> action the user kept) into the
    ACTION example log (idempotent, own meta flag)."""
    return _bootstrap_kind(conn, "action", force=force)


def _bootstrap_kind(conn, kind: str, *, force: bool = False) -> int:
    dom = _DOMAINS[kind]
    if not force and _meta_get(conn, dom["meta_key"]) == "1":
        return 0
    n = 0
    for r in conn.execute(_CORPUS_SQL[kind]).fetchall():
        if learn(conn, r["txt"], r["label"], kind=kind, txn_id=int(r["id"]),
                 commit=False):
            n += 1
    _meta_set(conn, dom["meta_key"], "1", commit=False)
    conn.commit()
    return n


def ensure_bootstrapped(conn) -> int:
    """Run both bootstraps once; a no-op after the first time. Callable API for
    an explicit seed-from-history action; app startup does not invoke it."""
    return bootstrap(conn, force=False) + bootstrap_actions(conn, force=False)


# ---------------------------------------------------------------------------
# meta: bootstrap flags
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
