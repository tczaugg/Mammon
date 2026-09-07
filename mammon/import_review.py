"""classic import review: field-mapping + NEW/MATCHING classification.

This is the *data layer* the import-review UI will call. It takes freshly
downloaded rows (webSlinger's flat-dict download form) and:

  1. maps each row to a provisional Mammon transaction -- signed amount from
     ``amount`` + ``isDebit``, ISO date from ``postedDate`` (parser already
     handles ISO offsets), a best-effort provisional payee derived from
     ``statementDescription`` (with transfer detection), and the *raw*
     ``statementDescription`` preserved in ``memo``; and

  2. classifies each mapped row against the existing register as either
     ``NEW`` or ``MATCHING`` -- deduping by ``transactionId`` (stored as a
     transaction's ``fitid``) first, then matching by date + signed amount
     within a small day window.

Nothing in the classification path writes to the register. The online-learning
rename tree (:mod:`mammon.rename_tree`) plugs in here: :func:`build_review`
auto-fills a NEW/non-transfer row's provisional payee when the tree is confident
(and otherwise records dropdown candidates), and :func:`save_new` learns from the
saved payee -- reinforcing a kept suggestion, tallying an overridden one, and
teaching a genuine correction. Transfer rows keep their ``TRANSFER FROM/TO``
payee -- the tree never applies to or learns from a transfer.

Incoming row form (one webSlinger download record)::

    {transactionId, SubAccountId, postedDate, amount, isDebit,
     checkNumber, statementDescription}

Field names are matched loosely (a few common aliases each) so the layer is
resilient to minor script-to-script naming drift.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from typing import Optional

from . import categorize
from . import category_rules
from . import category_tree
from . import ledger
from . import rename_tree
from . import transfer_rules
from .importers import record

__all__ = [
    "LABEL_NEW",
    "LABEL_MATCHING",
    "DEFAULT_WINDOW_DAYS",
    "MANUAL_WINDOW_DAYS",
    "MappedRow",
    "ReviewEntry",
    "map_row",
    "mapped_from_record",
    "classify_row",
    "build_review",
    "build_review_from_records",
    "import_records_via_review",
    "resolve_or_create_transfer_account",
    "save_new",
    "accept_match",
    "revert_match",
    "delete_saved",
    "predict_action",
    "predict_fields",
    "category_id_for_name",
    "resolve_or_create_category",
    "category_name_for_id",
    "persist_entries",
    "load_pending",
    "count_pending",
    "accept_all",
    "discard_all",
    "unmatch_one",
    "release_txn_matches",
    "undo_all_matches",
    "manual_match_candidates",
    "set_manual_match",
]

LABEL_NEW = "NEW"
LABEL_MATCHING = "MATCHING"

# Half-window (in days) for the date+amount fuzzy match. Bank "posted" dates
# routinely drift a day or two from when a transaction was first entered by
# hand, so we look a few days either side. Kept small so unrelated same-amount
# transactions in the same account don't collide.
DEFAULT_WINDOW_DAYS = 3

# Manual-match browsing window (half-window, in days). A hand-picked match is
# deliberately wider than the auto window -- the user is eyeballing candidates --
# but never wider than a fortnight either side: past that, a "candidate" is
# guesswork about a different month's payment. (By request; was 30.)
MANUAL_WINDOW_DAYS = 15

# The ``num`` an app-generated pre-entry carries. It is the marker that survives
# a QIF round trip -- on a reloaded ledger 523 rows carry it while ``scheduled``
# is 0 for every one of them -- so the matcher keys on the NUM, not the flag.
SCHED_NUM = "Sched"

# How far a pre-entry's amount may miss and still be the same payment. A
# scheduled row's amount is a FORECAST -- the finance calendar enters the median
# of the last six months -- so it is wrong by construction whenever escrow or a
# rate moves, and the real debit then never met its placeholder. Same figure the
# FILE import path already allows for exactly this case
# (``importers/core._funding_pending_by_payee``: "the payment CHANGED (escrow /
# rate) after it was scheduled"), so the two ingestion paths agree.
#
# core.py can afford 50% because it also demands the payee match. A downloaded
# review row has no payee yet -- ``map_row`` deliberately leaves it empty, the
# rename tree only runs later at predict time -- so the safety property here is
# different: a tolerant match is taken ONLY when it is UNAMBIGUOUS. One
# in-window scheduled candidate is the payment; two are a guess, and a guess
# that silently reconciles the wrong row is worse than leaving it NEW for the
# user to match by hand.
SCHED_AMOUNT_TOLERANCE = 0.5

# How many times a payee must have been chosen before it is even SHOWN as a
# rename candidate. Distinct from ``rename_tree.MIN_FILL``, which governs
# whether a rename is applied: this governs whether it is offered at all. One
# sighting says nothing, and a name the user has never accepted is noise in the
# dropdown, not help (by request). The tree applies it itself; the alias is
# kept so the requirement has one name here.
RENAME_MIN_SIGHTINGS = rename_tree.MIN_OFFER

# How much the prior register rows sharing a statement text must AGREE, and how
# many of them there must be, before their CATEGORY is back-filled (see
# :func:`_prior_txn_for`). Identical bank text is strong evidence of identity,
# but not when the rows carrying it disagree. Deliberately stricter than the
# rename tree's fill floor: these rows may never have been reviewed at all.
PRIOR_TXN_PURITY = 0.8
PRIOR_TXN_MIN_ROWS = 3


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
_MULTISPACE = re.compile(r"\s+")

# "SHARE TRANSFER FROM SHARE ACCOUNT: 0123", "Transfer to Checking", etc.
# Captures the direction and the counterparty account label (up to a ':' or
# end-of-string).
_TRANSFER_RE = re.compile(r"\bTRANSFER\s+(FROM|TO)\b\s*(.*?)\s*(?::|$)", re.IGNORECASE)

def _g(row: dict, *names: str, default: str = "") -> str:
    """First non-empty value among ``names`` (as a stripped string)."""
    for n in names:
        if n in row:
            v = row[n]
            if v is not None and v != "":
                return str(v).strip()
    return default


def _as_bool(value) -> Optional[bool]:
    """Interpret a debit/credit flag. Returns ``None`` when unknown/absent."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "y", "debit", "dr", "d"):
        return True
    if s in ("false", "0", "no", "n", "credit", "cr", "c"):
        return False
    return None


def _signed_cents(row: dict) -> int:
    """Signed integer cents from ``amount`` + ``isDebit``.

    ``amount`` is treated as a magnitude in dollars; ``isDebit`` decides the
    sign (debit -> money out -> negative). If ``isDebit`` is absent/ambiguous
    we fall back to whatever sign the amount itself carried.
    """
    cents = record.dollars_to_cents(_g(row, "amount", "transactionAmount", "amount_usd"))
    is_debit = _as_bool(row.get("isDebit")) if "isDebit" in row else None
    if is_debit is None:
        return cents
    return -abs(cents) if is_debit else abs(cents)


# The display default for a description with no rename: whitespace collapsed
# and an ALL-CAPS feed line title-cased. It is the rename tree's function
# because the tree's corpus loader has to recognise the exact string -- a row
# whose payee IS this text was kept as-is and is not a correction.
_clean_payee = rename_tree.tidy_text


def _derive_payee(desc: str) -> tuple[str, bool, str]:
    """Return ``(payee, is_transfer, transfer_account)`` for a description."""
    raw = (desc or "").strip()
    if not raw:
        return "", False, ""
    m = _TRANSFER_RE.search(raw)
    if m:
        direction = m.group(1).lower()
        account = _clean_payee(m.group(2))
        prep = "from" if direction == "from" else "to"
        payee = ("Transfer %s %s" % (prep, account)).strip()
        return payee, True, account
    return _clean_payee(raw), False, ""


def _map_date(row: dict) -> str:
    value = _g(row, "postedDate", "posted", "postDate", "date",
               "transactionDate", "effectiveDate")
    if not value:
        return ""
    return record.parse_date(value)


# ---------------------------------------------------------------------------
# public data shapes
# ---------------------------------------------------------------------------
@dataclass
class MappedRow:
    """A downloaded row mapped to provisional Mammon-transaction fields."""

    transaction_id: str = ""       # source unique id -> stored as fitid on accept
    account_ref: str = ""          # SubAccountId (source sub-account handle)
    date: str = ""                 # ISO YYYY-MM-DD
    amount_cents: int = 0          # signed; negative = money out
    payee: str = ""                # GROUND TRUTH: only what the SOURCE supplied
    memo: str = ""                 # raw statementDescription, preserved verbatim
    check_number: str = ""
    # Quicken 'C' cleared status for THIS row/leg (per-account, independent):
    # cleared=1 -> '*'/'c', reconciled=1 -> 'X'/'R'. Carried through so an
    # imported (single-account) transfer/plain row keeps its own Clr glyph
    # instead of importing blank. reconciled=1 implies cleared=1.
    cleared: int = 0
    reconciled: int = 0
    is_transfer: bool = False      # detected 'TRANSFER FROM/TO ...' line
    transfer_account: str = ""     # counterparty account label when is_transfer
    # Transient PREDICTIONS. Kept apart from the source fields above so the
    # review row a user looks at is never overwritten by a rename: the review
    # panel renders ground truth, the register renders the prediction.
    predicted_payee: str = ""      # transient: rename-tree / history suggestion
    category_id: Optional[int] = None  # transient: predicted/provisional category
    transfer_account_id: Optional[int] = None  # transient: predicted transfer account
    # investment extension: a brokerage/401k file row that posts into
    # investment_transactions (its own table) rather than the cash register.
    is_investment: bool = False
    action: str = ""              # Buy|Sell|Div|ReinvDiv|BuyX|SellX|...
    symbol: str = ""              # ticker or ticker-less fund NAME (holding key)
    quantity: str = ""            # Decimal text
    price: str = ""               # Decimal text (per share/unit)
    commission_cents: int = 0
    raw: dict = field(default_factory=dict)
    # True when ``payee`` was carried in VERBATIM from the source record -- an OFX
    # <NAME>/<PAYEE>, a QIF payee, or a tabular importer's explicit payee column
    # (e.g. a Venmo From/To). Such a payee is authoritative: the description-driven
    # rename rules are SKIPPED for it (user: a record that already has a Payee keeps
    # it as-is), while category auto-assignment still runs. Left False for a
    # download/scrape row whose payee was DERIVED from the statement description.
    payee_supplied: bool = False
    # Transient rename-tree hint, refreshed by :func:`predict_fields` when the
    # register opens the row (never persisted). Only the candidate LIST survives:
    # the payee delegate reads it to offer a typeable dropdown. There is
    # deliberately no "was it auto-filled" flag -- the accept path
    # (:func:`_learn_payee_rename`) re-queries the tree and compares its
    # suggestion against the payee actually committed, which stays accurate even
    # after the user edits the row. A stored flag would only go stale.
    payee_candidates: list = field(default_factory=list)  # dropdown options
    # The same idea for the CATEGORY picker: the payee-scoped category ids
    # :func:`predict_fields` found, most likely first, so the register can
    # promote them above the full alphabetical list. Transient; a row whose
    # category was not confident enough to fill in still carries these, which is
    # the whole point -- blank category, ranked choices.
    category_candidates: list = field(default_factory=list)


@dataclass
class ReviewEntry:
    """One row in the import-review list the UI renders."""

    mapped: MappedRow
    label: str = LABEL_NEW                 # LABEL_NEW | LABEL_MATCHING
    matched_txn_id: Optional[int] = None   # existing txn id when MATCHING
    match_method: str = ""                 # "transactionId" | "transfer" | "date+amount" | "manual" | ""
    review_id: Optional[int] = None        # persisted review_items row id (when saved)
    state: str = "pending"                 # pending | accepted | discarded
    accepted_txn_id: Optional[int] = None  # register row an accepted NEW created
    batch_id: Optional[int] = None         # the import operation it arrived in
    # TRANSIENT, never persisted: the Amazon per-item split allocation this row
    # carries (an ``amazon_items.ChargeAllocation``) when the entry came from
    # :func:`build_amazon_review`. Amazon invoice data is NEVER stored (SRD A6):
    # the review is rebuilt from the Downloads file on demand, so this lives only
    # in memory for the duration of the session. ``persist_entries`` neither reads
    # nor writes it; its presence is what routes accept through the Amazon
    # split-writing path instead of the single-category one.
    amazon_alloc: Optional[object] = None

    @property
    def is_actioned(self) -> bool:
        """Already accepted or discarded -- shown greyed, not actionable."""
        return self.state in ("accepted", "discarded")

    @property
    def is_new(self) -> bool:
        return self.label == LABEL_NEW

    @property
    def is_matching(self) -> bool:
        return self.label == LABEL_MATCHING


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------
def map_row(row: dict) -> MappedRow:
    """Map one downloaded row (flat dict) to a :class:`MappedRow`.

    Raises ``ValueError`` only when a *present* date value is unparseable; a
    missing date maps to ``""`` (still reviewable, just unmatchable by date).
    """
    # ``transactionDescription`` is not a synonym invented for tidiness: the SAME
    # webSlinger script emitted ``statementDescription`` on one run and
    # ``transactionDescription`` on the next, for byte-identical Wells Fargo rows.
    # Scripts are generated, so their field NAMES drift between runs. Missing the
    # variant is silent and expensive -- 214 review rows came through with no
    # memo at all, which also leaves the categorizer nothing to learn from and
    # ``_prior_txn_for`` nothing to match on.
    desc = _g(row, "statementDescription", "transactionDescription",
              "description", "memo", "name")
    payee, is_transfer, transfer_account = _derive_payee(desc)
    # A downloaded row carries only a statement description. Cleaning that text up
    # and calling it a Payee MANUFACTURES data the source never sent, and the
    # review list is supposed to be ground truth -- so a non-transfer row's payee
    # stays EMPTY here and the description shows (verbatim) in Memo. The register
    # fills a payee from the rename tree at selection time; that is a prediction
    # and belongs on the register, not in the stored review row. A transfer keeps
    # its "Transfer to/from X" payee: that is not a rename but the same parse that
    # produced is_transfer / transfer_account.
    if not is_transfer:
        payee = ""
    return MappedRow(
        transaction_id=_g(row, "transactionId", "id", "fitid", "referenceNumber"),
        account_ref=_g(row, "SubAccountId", "subAccountId", "accountId", "account"),
        date=_map_date(row),
        amount_cents=_signed_cents(row),
        payee=payee,
        memo=desc,
        check_number=_g(row, "checkNumber", "check_number", "check", "num"),
        is_transfer=is_transfer,
        transfer_account=transfer_account,
        raw=dict(row),
    )


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
def _find_match(conn, account_id: int, mapped: MappedRow, window_days: int,
                claimed_txn=frozenset(), claimed_inv=frozenset(), *,
                allow_scheduled: bool = True):
    """Return ``(txn_id, method)`` for the best existing match, else ``(None, "")``.

    Order: (1) exact ``transactionId`` == stored ``fitid`` (true dedupe), then
    (2) when the row reads as a transfer, an existing *transfer leg* of the same
    signed amount within the date window (so a manually-entered transfer's
    downloaded side reconciles instead of duplicating), then (3) any same signed
    amount within +/- ``window_days`` of the posted date, preferring the closest
    date.

    Count-aware: an existing id already in ``claimed_txn`` (``claimed_inv`` for
    investment rows) -- consumed by an earlier row of the SAME batch -- is skipped,
    so N genuinely-identical incoming rows match N DISTINCT existing rows and the
    surplus (when the register holds fewer) stays NEW instead of all matching one
    row. This is the multiset half of :func:`mammon.importers.record.identity_key`.
    """
    if mapped.is_investment:
        return _find_investment_match(conn, account_id, mapped, claimed_inv,
                                      window_days=window_days)
    tid = mapped.transaction_id
    if tid:
        rows = conn.execute(
            "SELECT id FROM transactions "
            "WHERE account_id=? AND fitid IS NOT NULL AND fitid<>'' AND fitid=?",
            (account_id, tid),
        ).fetchall()
        for row in rows:
            if int(row[0]) not in claimed_txn:
                return int(row[0]), "transactionId"

    if mapped.date:
        lo = record.iso_shift(mapped.date, -window_days)
        hi = record.iso_shift(mapped.date, window_days)
        # (2) The download reads as a transfer (statementDescription like
        # "SHARE TRANSFER FROM SHARE ACCOUNT: ..."). Prefer matching the
        # existing register leg of a manually-entered double-entry transfer
        # (``transfer_account_id`` set) over a coincidental same-amount plain
        # txn -- accepting then clears that downloaded leg rather than inserting
        # a second, one-sided transaction.
        if mapped.is_transfer:
            rows = conn.execute(
                "SELECT id FROM transactions "
                "WHERE account_id=? AND amount=? AND date BETWEEN ? AND ? "
                "AND transfer_account_id IS NOT NULL "
                "ORDER BY ABS(julianday(date) - julianday(?)), id",
                (account_id, mapped.amount_cents, lo, hi, mapped.date),
            ).fetchall()
            for row in rows:
                if int(row[0]) not in claimed_txn:
                    return int(row[0]), "transfer"

        # (3) General same-amount / near-date match. Also catches a transfer
        # leg by coincidence when the description did not read as a transfer.
        rows = conn.execute(
            "SELECT id FROM transactions "
            "WHERE account_id=? AND amount=? AND date BETWEEN ? AND ? "
            "ORDER BY ABS(julianday(date) - julianday(?)), id",
            (account_id, mapped.amount_cents, lo, hi, mapped.date),
        ).fetchall()
        for row in rows:
            if int(row[0]) not in claimed_txn:
                return int(row[0]), "date+amount"

        # (4) A SCHEDULED pre-entry, on amount TOLERANCE. Runs last, so an exact
        # match always wins and this only ever rescues a row that would have
        # been NEW. See SCHED_AMOUNT_TOLERANCE: the placeholder's amount is a
        # forecast, so an escrow or rate change makes it miss by construction --
        # the user's mortgage stopped matching for exactly this reason.
        # Unambiguous-only: with two in-window scheduled candidates we cannot
        # tell which payment this is, and reconciling the wrong one silently is
        # worse than leaving the row NEW to be matched by hand.
        if allow_scheduled:
            sched = _scheduled_candidates(conn, account_id, mapped, lo, hi,
                                          claimed_txn)
            if len(sched) == 1:
                return sched[0], "scheduled"

    return None, ""


def _scheduled_candidates(conn, account_id: int, mapped: MappedRow,
                          lo: str, hi: str, claimed_txn=frozenset()) -> list[int]:
    """Unclaimed ``num='Sched'`` rows in the window whose amount is within
    :data:`SCHED_AMOUNT_TOLERANCE` of the downloaded row's, SAME SIGN, nearest
    amount first.

    Same sign is not negotiable: a pre-entered payment can never be answered by
    a deposit, however close the value. The tolerance is a share of the
    PLACEHOLDER's own amount, so a $2,000 mortgage tolerates a bigger escrow
    move than a $40 subscription does.
    """
    want = int(mapped.amount_cents)
    if not want:
        return []
    rows = conn.execute(
        "SELECT id, amount FROM transactions "
        "WHERE account_id=? AND num=? AND date BETWEEN ? AND ? "
        "  AND ((amount < 0 AND ? < 0) OR (amount > 0 AND ? > 0)) "
        "ORDER BY ABS(amount - ?), ABS(julianday(date) - julianday(?)), id",
        (account_id, SCHED_NUM, lo, hi, want, want, want, mapped.date),
    ).fetchall()
    out = []
    for row in rows:
        if int(row["id"]) in claimed_txn:
            continue
        expected = abs(int(row["amount"]))
        if expected and abs(abs(want) - expected) <= expected * SCHED_AMOUNT_TOLERANCE:
            out.append(int(row["id"]))
    return out


def _ticker_of(symbol) -> str:
    """The leading token of a security string -- its ticker, or the whole head
    token when that is not ticker-shaped.

    Different exports of the SAME broker spell the same security differently: a
    Flex/QIF history stores "ALTY GLOBAL X SUPERDIVIDEND ALTER" while the
    activity CSV sends the bare "ALTY". The first token is the stable part.

    Unlike :func:`investments.ticker_of` this keeps a NON-ticker head token
    (a plan fund's "TARGET"), because here the job is comparing two spellings of
    the same holding, not asking a quote provider about it -- two rows both
    naming "TARGET 2030 FUND" should still match each other."""
    return str(symbol or "").strip().upper().split(" ")[0] if symbol else ""


def _find_investment_match(conn, account_id: int, mapped: MappedRow,
                           claimed_inv=frozenset(), *,
                           window_days: int = DEFAULT_WINDOW_DAYS):
    """Dedup an investment review row against ``investment_transactions``.

    An investment row lives in its own table, not ``transactions``, so it must
    NOT match a cash line of the same amount. Three tiers, strongest first:

    1. the source id (``fitid``);
    2. identical ``(date, symbol, quantity, amount)`` -- a re-import of the
       very same statement;
    3. NEAR-identity: date within ``window_days``, a compatible security -- same
       leading TICKER token, or both rows carrying no security at all -- and the
       row's IDENTIFYING QUANTITY equal.

    **Shares are to an investment row what amount is to a cash row.** A share
    move is identified by its share count; only a row that moves no shares
    (a dividend, interest, a cash fee) falls back to matching on amount. Keying
    every tier on amount instead was why a re-download of a plan's fee history
    matched almost nothing: the stored rows came from a QIF that carried only a
    share count, so their amount is 0, while the CSV states the dollar value --
    and 0 never equals -304. The shares agree exactly, which is the whole point.
    Matching on shares also lets the accept FILL IN the dollar value the old row
    never had, which is where these funds' only price history comes from.

    Tier 3 is what a re-download in a DIFFERENT export format needs: a year of
    dividends stored from a QIF as "ALTY GLOBAL X SUPERDIVIDEND ALTER" classified
    NEW against a CSV's "ALTY", because exact identity can never bridge two
    spellings, and the whole year would have re-entered as duplicates. A row with
    a security never matches a row without one -- a fee must not swallow a
    same-amount dividend.

    Returns ``(invtxn_id, method)`` or ``(None, "")`` -- the id refers to an
    ``investment_transactions`` row, handled specially on accept. Count-aware:
    ids already in ``claimed_inv`` (matched by an earlier batch row) are skipped
    so identical lots dedup by count."""
    tid = mapped.transaction_id
    if tid:
        rows = conn.execute(
            "SELECT id FROM investment_transactions "
            "WHERE account_id=? AND fitid IS NOT NULL AND fitid<>'' AND fitid=?",
            (account_id, tid),
        ).fetchall()
        for row in rows:
            if int(row[0]) not in claimed_inv:
                return int(row[0]), "transactionId"
    if not mapped.date:
        return None, ""
    rows = conn.execute(
        "SELECT id FROM investment_transactions "
        "WHERE account_id=? AND date=? AND amount=? "
        "AND IFNULL(symbol,'')=? AND IFNULL(quantity,'')=?",
        (account_id, mapped.date, mapped.amount_cents,
         mapped.symbol or "", mapped.quantity or ""),
    ).fetchall()
    for row in rows:
        if int(row[0]) not in claimed_inv:
            return int(row[0]), "identity"
    lo = record.iso_shift(mapped.date, -window_days)
    hi = record.iso_shift(mapped.date, window_days)
    want = _ticker_of(mapped.symbol)
    shares = _shares_of(mapped.quantity)
    rows = conn.execute(
        "SELECT id, symbol, quantity, amount FROM investment_transactions "
        "WHERE account_id=? AND date BETWEEN ? AND ? "
        "ORDER BY ABS(julianday(date) - julianday(?)), id",
        (account_id, lo, hi, mapped.date),
    ).fetchall()
    for row in rows:
        if int(row["id"]) in claimed_inv:
            continue
        if _ticker_of(row["symbol"]) != want:
            continue
        other = _shares_of(row["quantity"])
        if shares is not None:
            # A share move is identified by its shares. Its amount may be absent
            # on either side (a QIF fee removal carries none), so requiring the
            # amounts to agree would reject the very rows this tier is for.
            if other is not None and other == shares:
                return int(row["id"]), "date+shares"
        elif other is None and row["amount"] == mapped.amount_cents:
            # No shares on either side -- a dividend, interest, a cash fee. Here
            # the amount IS the identity, exactly as for a cash row.
            return int(row["id"]), "date+amount"
    return None, ""


def _shares_of(quantity):
    """A row's share count as a Decimal magnitude, or None when it moves no
    shares. Compared numerically, never as text: the ledger stores Decimal TEXT,
    so "0.144" and "0.1440" are the same trade written two ways."""
    text = str(quantity or "").strip()
    if not text:
        return None
    try:
        value = abs(Decimal(text))
    except (InvalidOperation, ValueError):
        return None
    return None if value == 0 else value


def classify_row(conn, account_id: int, mapped: MappedRow, *,
                 window_days: int = DEFAULT_WINDOW_DAYS,
                 claimed_txn=None, claimed_inv=None,
                 allow_scheduled: bool = True) -> ReviewEntry:
    """Classify a single mapped row against ``account_id``'s register.

    ``claimed_txn``/``claimed_inv`` (optional, supplied by :func:`build_review`)
    are the existing-row ids already matched by earlier rows of the SAME batch, so
    count-aware multiset dedup holds across the batch: N identical rows match N
    distinct existing rows and the surplus classifies NEW. Called without them
    (e.g. a lone UI reclassify) it behaves exactly as a single-row classify."""
    txn_id, method = _find_match(conn, account_id, mapped, window_days,
                                 claimed_txn or frozenset(),
                                 claimed_inv or frozenset(),
                                 allow_scheduled=allow_scheduled)
    if txn_id is not None:
        return ReviewEntry(mapped=mapped, label=LABEL_MATCHING,
                           matched_txn_id=txn_id, match_method=method)
    return ReviewEntry(mapped=mapped, label=LABEL_NEW)


def _claim_match(entry: ReviewEntry, claimed_txn: set, claimed_inv: set) -> None:
    """Record ``entry``'s matched existing-row id so no later row in the batch can
    reuse it (multiset dedup). Investment ids live in their own table, so they are
    tracked separately from cash-transaction ids."""
    if not entry.is_matching or entry.matched_txn_id is None:
        return
    if entry.mapped.is_investment:
        claimed_inv.add(int(entry.matched_txn_id))
    else:
        claimed_txn.add(int(entry.matched_txn_id))


# NOTE: payee renaming used to happen HERE, when the review row was built
# (an ``_apply_learned_payee`` helper). It moved into :func:`predict_fields`,
# which the register calls when it opens the row. Two reasons: writing the tree's
# guess into ``mapped.payee`` edited the review list itself -- which is supposed
# to show what the bank sent, verbatim -- and predicting once at build time meant
# a row could not benefit from a rename the user taught two rows earlier in the
# same session. Do not reintroduce a build-time rename.


def _apply_learned_transfer(mapped: MappedRow, rules) -> None:
    """Set ``mapped.transfer_account_id`` from a learned transfer rule.

    The transfer-side twin of the payee rename in :func:`predict_fields`: for a
    TRANSFER row whose raw ``statementDescription`` matches a learned keyword,
    pre-fill the account the user last pointed that kind of transfer at (the
    user's request: transfer learning "as it does for dividends"). ``rules`` is
    the pre-loaded :func:`transfer_rules.load_rules` list, so a whole batch
    shares one query.

    Applies to EVERY row, not just ones the PARSER called a transfer. A learned
    rule is the user's own statement that this description means a transfer to
    that account, and it gets learned precisely for text the parser does not
    recognise -- "AUTOMATIC DEPOSIT, VENMO CASHOUT PPD" never reads as one.
    Gating on the parser's verdict made the rule unreachable for exactly the
    descriptions it exists to serve.

    ``mapped.is_transfer`` is deliberately NOT set here: it records what the
    SOURCE said. A row whose transfer-ness the user taught still has a payee
    worth renaming, where a parser-derived transfer carries "Transfer from X".
    """
    rule = transfer_rules.match_rule(mapped.memo, rules)
    if rule and rule.get("transfer_account_id") is not None:
        mapped.transfer_account_id = int(rule["transfer_account_id"])


def predict_transfer_account(conn, mapped: MappedRow) -> Optional[int]:
    """Predict a TRANSFER row's account id from live learned rules.

    Called when the register's editable pending row is created (like
    :func:`predict_fields`), so it reflects rules learned earlier in the same
    review session. Returns ``None`` when nothing is learned for this text.

    Not gated on ``mapped.is_transfer``: a learned rule is the user's own
    statement that this description IS a transfer, and it is learned for text the
    parser does not recognise as one (see :func:`_apply_learned_transfer`)."""
    return transfer_rules.apply_rules(conn, mapped.memo)


def _is_empty_row(mapped: MappedRow) -> bool:
    """True for a mapped row that cannot be a transaction at all.

    No date, no money, and no text -- there is nothing to review, nothing to
    match, and nothing the user could ever accept. Such rows reached the review
    list because a scrape payload was flattened across ALL its arrays, so a
    bank's ``{"id": ..., "shortName": "Checking"}`` lookup table arrived as
    eleven blank lines at the top of the list (:func:`mammon.webslinger._rows_from`
    now declines to gather those; this is the backstop, because the review list
    must never show a row the user cannot act on whatever the source did).

    Deliberately narrow: a zero-amount row that carries a date or a description
    is a real, reviewable transaction and is KEPT. All three must be missing.
    """
    return not (mapped.date or mapped.amount_cents or mapped.memo
                or mapped.payee or mapped.check_number
                or mapped.is_investment or mapped.symbol)


def build_review(conn, account_id: int, rows, *,
                 window_days: int = DEFAULT_WINDOW_DAYS) -> list[ReviewEntry]:
    """Map + classify a batch of downloaded rows into a review list.

    This is the primary entry point the import-review UI calls after a
    download. Learned transfer rules fill each transfer row's account before
    classification. It never mutates the database.

    The rename tree is deliberately NOT run here. Baking a rename into the stored
    review row destroyed the very thing the review list exists to show -- the
    source data as the bank sent it -- and it ran a second time in the register,
    from a tree that had learned more in between, so the two disagreed. Renaming
    now happens once, in :func:`predict_fields`, at the moment the register's
    editable row is created.
    """
    trules = transfer_rules.load_rules(conn)
    entries: list[ReviewEntry] = []
    claimed_txn: set[int] = set()
    claimed_inv: set[int] = set()
    mapped_rows: list[MappedRow] = []
    for row in rows:
        mapped = map_row(row)
        if _is_empty_row(mapped):
            continue
        _apply_learned_transfer(mapped, trules)
        mapped_rows.append(mapped)

    # TWO PASSES, and the order is the point. Pass 1 offers every row only the
    # EXACT tiers (id, transfer leg, same-cent amount); pass 2 then lets what is
    # still NEW try the tolerant scheduled tier. Within one row the tolerant tier
    # already ran last, but that is not enough across a BATCH: a tolerant row
    # processed early claimed the register line, and the row that matched it to
    # the cent -- arriving later in the file -- found the line taken and
    # classified NEW. The user hit exactly that, hand-matched the exact row onto
    # the line, and then found a second review row auto-matched to the same line.
    # Claiming is global, so precedence has to be global too.
    for mapped in mapped_rows:
        entry = classify_row(conn, account_id, mapped, window_days=window_days,
                             claimed_txn=claimed_txn, claimed_inv=claimed_inv,
                             allow_scheduled=False)
        _claim_match(entry, claimed_txn, claimed_inv)
        entries.append(entry)
    for i, entry in enumerate(entries):
        if not entry.is_new:
            continue
        retry = classify_row(conn, account_id, entry.mapped,
                             window_days=window_days, claimed_txn=claimed_txn,
                             claimed_inv=claimed_inv, allow_scheduled=True)
        if retry.is_matching:
            _claim_match(retry, claimed_txn, claimed_inv)
            entries[i] = retry
    return entries


def mapped_from_record(rec) -> MappedRow:
    """Map a parsed :class:`~mammon.importers.record.NormalizedTxn` to a provisional
    :class:`MappedRow` -- the single-account *file* twin of :func:`map_row`.

    A file's records are already normalised (signed cents, ISO date, split of the
    ``[Account]`` transfer target), so this is a straight field copy that keeps a
    transfer leg intact (``is_transfer`` + ``transfer_account`` NAME). It exists so
    a SINGLE-account QIF (or any single-account file) can go through the very same
    review list a live download does -- classify NEW/MATCHING, then create the
    counterparty mirror on accept, with match detection preventing double-entry.
    """
    return MappedRow(
        transaction_id=(rec.fitid or ""),
        date=rec.date or "",
        amount_cents=rec.amount_cents,
        payee=(rec.payee or "").strip(),
        # A parsed file record's payee is authoritative when present (OFX NAME,
        # QIF payee, or a tabular importer's explicit payee column) -- flag it so
        # the review's rename rules do not rewrite it from the description.
        payee_supplied=bool((rec.payee or "").strip()),
        memo=(rec.memo or "").strip(),
        check_number=(rec.check_number or ""),
        is_transfer=rec.is_transfer,
        transfer_account=(rec.transfer_account or "").strip(),
        # Carry the record's OWN cleared/reconciled (the Quicken 'C' flag) so a
        # single-account QIF's Clr=R survives import; the synthesized counter-leg
        # in the other account keeps its own default (reconciled independently).
        cleared=int(getattr(rec, "cleared", 0) or 0),
        reconciled=int(getattr(rec, "reconciled", 0) or 0),
        # investment fields (empty/False for a cash record)
        is_investment=getattr(rec, "is_investment", False),
        action=(rec.action or ""),
        symbol=(rec.symbol or ""),
        quantity=(rec.quantity or ""),
        price=(rec.price or ""),
        commission_cents=(rec.commission_cents or 0),
        # Stash the record's own category NAME so the accept step can resolve it
        # (MappedRow itself carries only a resolved category_id, like a download).
        raw={"category": (rec.category or "").strip()},
    )


def build_review_from_records(conn, account_id: int, records, *,
                              window_days: int = DEFAULT_WINDOW_DAYS) -> list[ReviewEntry]:
    """Classify a SINGLE account's parsed records into a NEW/MATCHING review list.

    The file-import twin of :func:`build_review`. Every record must belong to
    ``account_id`` (a single-account import/download). Investment rows ARE included
    (an investment CSV/OFX lands in review like any other file): they classify
    against ``account_id``'s ``investment_transactions`` (dedup by source id, then
    identical date/symbol/quantity/amount) and, on accept, post into that table --
    never the cash register. A transfer leg whose counterparty mirror already lives
    in this account's register -- e.g. the other side was imported first, or entered
    by hand -- classifies as MATCHING so accepting it clears that leg instead of
    double-entering; an unmatched transfer stays NEW and :func:`save_new` turns it
    into a real double-entry pair, creating the counterparty leg. Never mutates the
    database.
    """
    entries: list[ReviewEntry] = []
    claimed_txn: set[int] = set()
    claimed_inv: set[int] = set()
    for rec in records:
        mapped = mapped_from_record(rec)
        entry = classify_row(conn, account_id, mapped, window_days=window_days,
                             claimed_txn=claimed_txn, claimed_inv=claimed_inv)
        _claim_match(entry, claimed_txn, claimed_inv)
        entries.append(entry)
    return entries


def _prior_txn_for(conn, mapped: MappedRow) -> Optional[dict]:
    """The payee/category prior register rows with this exact statement text
    AGREE on, or ``None``. Feeds the CATEGORY back-fill only.

    Matched on exact ``memo`` (the raw ``statementDescription`` that
    :func:`save_new` preserves) and the same amount sign, ignoring transfers.

    It used to take the single most RECENT row, with no corroboration at all --
    the one path in the whole predictor with no threshold. On the real ledger
    five rows share the text "AUTOMATIC DEPOSIT, PAYPAL TRANSFER PPD" carrying
    FOUR different payees, and the newest of them was filled in as though it
    were established fact. Identical text is strong evidence, but only when the
    rows carrying it agree: the dominant payee must hold :data:`PRIOR_TXN_PURITY`
    of at least :data:`PRIOR_TXN_MIN_ROWS` rows.

    It no longer fills the PAYEE. Register rows that never went through review
    -- QIF history, hand entries -- carry memos the user typed, and matching a
    downloaded description against those is how a 1998 note of "deposit" once
    renamed MOBILE DEPOSIT. Every rename the user has actually made lives in the
    rename tree's own corpus (live-labelled, so a register edit counts), which
    is the only source the payee cell is filled from now.
    """
    memo = (mapped.memo or "").strip()
    if not memo:
        return None
    sign = 1 if mapped.amount_cents >= 0 else -1
    rows = conn.execute(
        "SELECT payee, category_id FROM transactions "
        "WHERE memo=? AND transfer_account_id IS NULL "
        "  AND ((amount >= 0 AND ?=1) OR (amount < 0 AND ?=-1)) "
        "ORDER BY id DESC",
        (memo, sign, sign),
    ).fetchall()
    if not rows:
        return None
    row = _dominant_prior(rows)
    if row is None:
        return None
    return {"payee": row["payee"], "category_id": row["category_id"]}


def _dominant_prior(rows):
    """The prior row whose PAYEE the others agree on, or ``None``.

    Counts payees across every row sharing the statement text; the winner must
    hold :data:`PRIOR_TXN_PURITY` of them and clear the rename corroboration
    floor. Rows with no payee do not vote. The returned row is the newest
    carrying that payee, so its category comes along.
    """
    votes = Counter()
    for r in rows:
        name = (r["payee"] or "").strip()
        if name:
            votes[name] += 1
    if not votes:
        return None
    top, n = votes.most_common(1)[0]
    if n < PRIOR_TXN_MIN_ROWS:
        return None
    if n / sum(votes.values()) < PRIOR_TXN_PURITY:
        return None
    for r in rows:                       # rows are newest-first
        if (r["payee"] or "").strip() == top:
            return r
    return None


def predict_action(conn, mapped: MappedRow) -> str:
    """The action to seed an investment row with: the learned mapping when it is
    high-confidence, else the importer's own guess.

    The importer maps a source's activity vocabulary onto Quicken actions with a
    lookup table, which is a guess ("RECORDKEEPING FEE" -> ShrsOut?). The action
    tree has seen every accept the user ever made, so it overrules that guess --
    but only when high-confidence, and always visibly in an editable row.

    Lives here, not in the register model, because Accept All needs the SAME
    answer the pending row shows. It was in the model alone, so the bulk path
    silently used the importer's raw guess instead.
    """
    from mammon import investments

    action = mapped.action or ""
    try:
        raw = "" if investments.is_known_action(action) else action
        sugg = rename_tree.suggest(conn, mapped.memo or "", kind="action",
                                   extra=raw)
        if sugg.action == rename_tree.ACTION_AUTO and sugg.high_confidence:
            return sugg.payee
    except Exception:
        pass
    return action


def predict_fields(conn, mapped: MappedRow, *,
                   account_id: Optional[int] = None) -> tuple[str, Optional[int]]:
    """Predict ``(payee, category_id)`` for a NEW mapped row from live history.

    Called AT THE MOMENT the register's editable pending row is created (not once
    at download time), so it reflects every rename and accept made since --
    notably learning from earlier rows of the SAME review session.

    PAYEE (the user's rule): the cell shows the source's own payee field when
    the record carried one, else the bank's description, until the rename tree
    (:mod:`rename_tree`) has seen the same text renamed at least twice and its
    matched leaf names ONE payee -- then that payee is filled in. A contested or
    unsure match leaves the raw text in the cell and puts the candidates in the
    payee dropdown (``mapped.payee_candidates``). The tree reads the source's
    payee field as evidence alongside the description (sometimes it is all the
    source sends), so a supplied payee is renamed exactly when a description
    would be: by a matched, corroborated, single-payee leaf.

    CATEGORY: resolved after the payee and scoped to it (:mod:`category_tree`),
    then a hand-written keyword rule, then -- for a payee that is SETTLED, i.e.
    filled by the tree or supplied by the source -- what the register's own
    QuickFill would pre-enter for that payee typed by hand
    (:func:`categorize.quickfill`, this ``account_id``'s row preferred), then
    the category prior register rows with the same statement text agree on
    (:func:`_prior_txn_for`). The last two fill the category only, never the
    payee.

    Transfer rows keep their derived ``TRANSFER FROM/TO`` payee and get NO plain
    payee/category prediction: the transfer mapping wins (requirement 4).

    An INVESTMENT row gets neither. ``investment_transactions`` has no
    ``category_id`` column, so a category here would be a value the user cannot
    correct and nothing can learn from. The bulk path
    (:func:`_accept_one_predicted`) already skips this function for such rows;
    the guard is repeated here so the interactive path cannot depend on which
    widget happens to own the row.
    """
    if mapped.is_investment:
        return mapped.payee, None
    if mapped.is_transfer:
        return mapped.payee, None
    category_id: Optional[int] = None
    # The source's own payee field (an OFX NAME, a Venmo From/To column, the
    # Costco card's only text) is the DEFAULT and is EVIDENCE: its tokens join
    # the description's in the tree's query, so a feed that sends nothing but a
    # payee field still trains and matches. It stands verbatim unless the tree
    # fills -- which takes a matched, single-payee leaf behind at least
    # rename_tree.MIN_FILL corrections, the same bar a description clears.
    payee_supplied = bool(getattr(mapped, "payee_supplied", False)) and bool(mapped.payee)
    extra = mapped.payee if payee_supplied else ""
    sugg = rename_tree.suggest(conn, mapped.memo, extra=extra)
    # The dropdown candidates ride on the row so the register's payee editor can
    # offer them (transient; lost when a review row is reloaded). The tree has
    # already applied RENAME_MIN_SIGHTINGS: a payee chosen once is neither
    # filled nor offered.
    mapped.payee_candidates = list(sugg.payees)
    if sugg.action == rename_tree.ACTION_AUTO:
        payee = sugg.payee
    elif payee_supplied:
        payee = mapped.payee
    else:
        # Nothing confident: the bank's own text, tidied for reading (an
        # ALL-CAPS line title-cased). Accepting it as-is is not a rename and
        # teaches the tree nothing -- the tree recognises this exact string.
        payee = _clean_payee(mapped.memo)
    # CATEGORY -- resolved after the payee and SCOPED TO IT. The tree hangs
    # below the payee just settled above, so every candidate is a category that
    # payee has actually carried (:mod:`mammon.category_tree`). The ranked
    # candidates ride on the row so the register's picker can promote them even
    # when nothing is confident enough to fill in.
    src = category_tree.source_text(
        mapped.memo, mapped.payee,
        bool(getattr(mapped, "payee_supplied", False)))
    csugg = category_tree.suggest(conn, payee, src)
    mapped.category_candidates = list(csugg.category_ids)
    allowed = set(mapped.category_candidates)
    if csugg.action == category_tree.ACTION_AUTO:
        category_id = csugg.category_id
    else:
        # Then a keyword rule. Every one of these is now something the user typed
        # into the Rules manager: migration 57 purged the rows the old learner
        # had minted, and nothing writes the table any more. That is what makes
        # it safe to honour without the payee filter the learned tree lives
        # under -- an explicit instruction is not the system guessing an
        # unrelated category at someone, which is the thing being prevented.
        crule = category_rules.match_rule(
            mapped.memo, category_rules.load_rules(conn))
        if crule and crule.get("category_id") is not None:
            category_id = int(crule["category_id"])
    # QUICKFILL -- the register's own answer for a payee it knows. The tree
    # withholds until four review votes corroborate a category and never reads
    # the register, so an Enbridge Gas the ledger has categorized for years
    # sat blank when the tree filled the payee -- while TYPING the same payee
    # pre-entered the category at once, because the register's blank row asks
    # QuickFill. An auto-filled payee must not do worse than a typed one, so
    # the same question is asked here, for the same payee, from the same
    # source (the payee's most recent posted row, this account's preferred).
    # Only for a SETTLED payee: the raw bank text in the cell is not a payee
    # the register can be asked about. Withheld when review history has
    # already shown the payee to be a catalogue -- coherence below the tree's
    # gate on at least MIN_COUNT votes -- because there the tree's refusal is
    # measured and the last row's category is a coin toss.
    payee_settled = sugg.action == rename_tree.ACTION_AUTO or payee_supplied
    if category_id is None and payee_settled and payee:
        known = category_tree.known_categories(conn, payee)
        votes = sum(n for _, n in known)
        catalogue = (votes >= category_tree.MIN_COUNT
                     and known[0][1] / votes < category_tree.PAYEE_COHERENCE)
        if not catalogue:
            path = (categorize.quickfill(conn, payee, account_id).get("category") or "")
            if path and not path.startswith("["):
                category_id = category_id_for_name(conn, path)
    # Prior-transaction fallback back-fills the CATEGORY only (never the payee:
    # see _prior_txn_for).
    if category_id is None:
        prior = _prior_txn_for(conn, mapped)
        if prior is not None and prior.get("category_id") is not None:
            # A prior row matched on EXACT statement text. When it also
            # carries the same payee, its category has demonstrably been
            # seen for this payee -- on a byte-identical description -- so
            # it satisfies the never-a-new-category rule directly and does
            # not need to be in the tree's tally as well. It often will not
            # be: the tally is seeded from the register at bootstrap and
            # grown by review accepts and register edits, so a row that
            # arrived by plain file import has not voted yet.
            same_payee = (category_tree.normalized_key(prior.get("payee"))
                          == category_tree.normalized_key(payee))
            if same_payee or int(prior["category_id"]) in allowed:
                category_id = int(prior["category_id"])
    return payee, category_id


# ---------------------------------------------------------------------------
# persistence -- the ONLY place reviewed rows enter (or touch) the register.
# The UI holds the review list in memory and calls these to commit an accept /
# save (and to undo one). ``ledger`` remains the sole writer of txn rows.
# ---------------------------------------------------------------------------
def category_id_for_name(conn, name) -> Optional[int]:
    """Resolve a category *name* typed in the review list to a category id.

    Case/space-insensitive match against existing categories; returns ``None``
    for a blank or unrecognised name (we never invent categories here)."""
    key = _MULTISPACE.sub(" ", str(name or "").strip()).lower()
    if not key:
        return None
    # ledger.list_categories yields {'id', 'path'} where 'path' is the
    # hierarchical 'Parent:Child' name; match the typed name against it.
    for cat in ledger.list_categories(conn, include_hidden=True):
        cpath = cat["path"]
        if cpath and _MULTISPACE.sub(" ", str(cpath).strip()).lower() == key:
            return int(cat["id"])
    return None


def resolve_or_create_category(conn, name) -> Optional[int]:
    """Get-or-create a category id from a name typed into a review row that the
    user is ACCEPTING.

    Unlike :func:`category_id_for_name` (lookup-only, used while merely
    previewing/predicting -- we never invent a category for an unaccepted row),
    this defers to :func:`ledger.resolve_category`, the SINGLE writer of category
    rows. When the user types a brand-new category into the register's pending
    row and confirms it, accept must both CREATE that category and assign it to
    the resulting transaction -- the lookup-only path returned ``None`` for
    unknown text, so the category was silently dropped and the user had to re-add
    it by hand. ``ledger.resolve_category`` reuses an existing category
    case-insensitively and creates each missing 'Parent:Child' level. Returns
    ``None`` for a blank name; the caller resolves a bracketed ``[Account]``
    transfer target FIRST, so this is normally reached only for a plain
    category. Bracketed text is refused outright anyway: it names the transfer
    namespace, so an UNKNOWN ``[Account]`` (one that resolves to no account)
    must stay uncategorized, never mint a literal ``[Name]`` category row."""
    s = str(name or "").strip()
    if not s:
        return None
    if s.startswith("[") and s.endswith("]"):
        return None
    return ledger.resolve_category(conn, s)


def transfer_account_id_for_name(conn, name) -> Optional[int]:
    """Resolve Quicken's ``[Account Name]`` transfer text to an account id.

    Returns ``None`` for text that is not bracketed or names no known account,
    so the caller can fall back to a plain category. Case/space-insensitive on
    the inner name."""
    s = str(name or "").strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    key = _MULTISPACE.sub(" ", s[1:-1].strip()).lower()
    if not key:
        return None
    for acct in ledger.list_accounts(conn, include_closed=True):
        if _MULTISPACE.sub(" ", str(acct["name"]).strip()).lower() == key:
            return int(acct["id"])
    return None


def resolve_or_create_transfer_account(conn, name, *, default_type: str = "checking") -> Optional[int]:
    """Get-or-create the counterparty account a single-account transfer leg names.

    Unlike :func:`transfer_account_id_for_name` (lookup-only, for UI-typed
    ``[Account]`` text), this CREATES the account when a single-account import
    references a counterparty not yet in the ledger -- so the mirror leg
    :func:`save_new` posts has an account to live in. ``name`` is the plain
    counterparty name (``rec.transfer_account``), not bracketed. Returns ``None``
    only for a blank name."""
    plain = str(name or "").strip()
    key = _MULTISPACE.sub(" ", plain).lower()
    if not key:
        return None
    for acct in ledger.list_accounts(conn, include_closed=True):
        if _MULTISPACE.sub(" ", str(acct["name"]).strip()).lower() == key:
            return int(acct["id"])
    return ledger.create_account(conn, plain, default_type)


def import_records_via_review(conn, account_id: int, records, *,
                              window_days: int = DEFAULT_WINDOW_DAYS,
                              learn: bool = False) -> dict:
    """Ingest a SINGLE account's ``records`` THROUGH the review list, then accept.

    This is the headless equivalent of a user clicking "accept all" on the review
    the UI builds from a single-account file/download -- the path a single-account
    QIF now takes (treated the same as a download). Each record is classified
    against ``account_id``'s register (:func:`build_review_from_records`); then:

      * a MATCHING transfer/plain row clears the existing register leg rather than
        inserting a second one (double-entry protection);
      * a NEW transfer row is saved as a real double-entry pair -- the counterparty
        account is get-or-created and its mirror leg posted (:func:`save_new` ->
        :func:`ledger.create_transfer`);
      * a NEW plain row is inserted as an ordinary transaction.

    Returns ``{"added", "matched"}``. ``learn`` defaults to False so a bulk file
    import does not spawn payee/transfer rules from its own rows.
    """
    entries = build_review_from_records(conn, account_id, records,
                                        window_days=window_days)
    added = matched = 0
    for entry in entries:
        if entry.is_matching:
            accept_match(conn, entry)
            matched += 1
            continue
        m = entry.mapped
        taid = None
        category_id = None
        if m.is_investment:
            pass  # investment rows carry no transfer/category; save_new branches.
        elif m.is_transfer and m.transfer_account:
            taid = resolve_or_create_transfer_account(conn, m.transfer_account)
        else:
            cat_name = (m.raw or {}).get("category") or ""
            if cat_name:
                from .importers.core import _resolve_category
                category_id = _resolve_category(conn, cat_name, {})
        save_new(conn, account_id, m, category_id=category_id,
                 transfer_account_id=taid, learn=learn)
        added += 1
    return {"added": added, "matched": matched}


def account_label_for_id(conn, aid: Optional[int]) -> str:
    """The ``[Account Name]`` transfer label for an account id, or ``""`` when
    ``None``/unknown -- lets a predicted transfer account seed the register's
    editable category text field."""
    if aid is None:
        return ""
    acct = ledger.get_account(conn, aid)
    return f"[{acct['name']}]" if acct is not None else ""


def category_name_for_id(conn, cid: Optional[int]) -> str:
    """Reverse of :func:`category_id_for_name`: the 'Parent:Child' path for a
    category id, or ``""`` when it is ``None`` / unknown. Lets a predicted
    ``category_id`` seed the register's editable category text field."""
    if cid is None:
        return ""
    for cat in ledger.list_categories(conn, include_hidden=True):
        if int(cat["id"]) == int(cid):
            return cat["path"] or ""
    return ""


def _set_state(conn, review_id: Optional[int], state: str, **cols) -> None:
    """UPDATE the persisted ``review_items`` row (no-op when unpersisted).

    ``state`` and any extra column=value pairs in ``cols`` are written; the
    caller owns the ``commit`` (so a state change rides the same transaction as
    the register write it accompanies)."""
    if review_id is None:
        return
    assignments = ["state=?"]
    values: list = [state]
    for name, value in cols.items():
        assignments.append(f"{name}=?")
        values.append(value)
    values.append(review_id)
    conn.execute(
        f"UPDATE review_items SET {', '.join(assignments)} WHERE id=?", values)


def save_new(conn, account_id: int, mapped: MappedRow, *,
             payee: Optional[str] = None, category_id: Optional[int] = None,
             memo: Optional[str] = None, transfer_account_id: Optional[int] = None,
             review_id: Optional[int] = None, learn: bool = True,
             date: Optional[str] = None, amount_cents: Optional[int] = None,
             num: Optional[str] = None, action: Optional[str] = None,
             symbol: Optional[str] = None, quantity: Optional[str] = None,
             price: Optional[str] = None) -> int:
    """Insert a reviewed NEW row into ``account_id``'s register and return its
    txn id. The ``payee``/``memo`` overrides (when not ``None``) replace the
    provisional mapped values, so the user's edits win. The source id is stored
    as ``fitid`` so a later download dedupes against it.

    When ``review_id`` names a persisted ``review_items`` row, that row is
    stamped accepted (with the new txn id) so the pending review shrinks.

    When ``learn`` is set (the default) on a non-transfer row, the saved payee is
    fed to the online rename tree (:func:`rename_tree.learn` via
    :func:`_learn_payee_rename`) so future imports of the same bank text grow more
    confident, and the applied/overridden tallies are updated."""
    # The register's pending row is editable, so the user may have corrected the
    # date, amount or check number before accepting. Those corrections are taken
    # as ARGUMENTS and never written back onto ``mapped``: that object is the
    # review row's ground truth -- what the bank actually sent -- and overwriting
    # it made the greyed row afterwards show the edit instead of the source,
    # disagreeing with its own stored row until a reload put it back.
    d = mapped.date if date is None else date
    amt_cents = mapped.amount_cents if amount_cents is None else int(amount_cents)
    chk = mapped.check_number if num is None else num
    if not d:
        raise ValueError("cannot save a reviewed row that has no date")
    if mapped.is_investment:
        # Investment rows post into investment_transactions (their own table), not
        # the cash register; they carry no payee/category and feed no rename tree.
        # The register's pending row is editable in EVERY field that matters here
        # -- an importer maps a source's activity vocabulary onto Quicken actions
        # by a lookup table, and that is a guess -- so the corrections arrive as
        # arguments, never written back onto ``mapped`` (see above).
        return _save_investment(
            conn, account_id, mapped, memo=memo, review_id=review_id, learn=learn,
            date=d, amount_cents=amt_cents, action=action, symbol=symbol,
            quantity=quantity, price=price)
    # The caller (register) passes the edited payee. With none given, fall back
    # to the prediction rather than the source field, which is now empty for a
    # row whose bank text carried no payee.
    p = (mapped.predicted_payee or mapped.payee) if payee is None else payee
    m = mapped.memo if memo is None else memo
    if transfer_account_id is not None and amt_cents != 0:
        # A reviewed row pointed at a real account is a real double-entry
        # transfer (Quicken's '[Account]'), not a plain single-sided txn: create
        # both legs and return the leg in THIS account. Money out of this account
        # (negative) makes it the 'from' side; money in makes it the 'to' side.
        amt = abs(amt_cents)
        if amt_cents < 0:
            from_id, _to = ledger.create_transfer(
                conn, account_id, transfer_account_id, d, amt,
                memo=(m or None), num=(chk or None), payee=(p or None))
            txn_id = from_id
        else:
            _from, to_id = ledger.create_transfer(
                conn, transfer_account_id, account_id, d, amt,
                memo=(m or None), num=(chk or None), payee=(p or None))
            txn_id = to_id
        # Reconciliation is PER-ACCOUNT: stamp THIS account's leg with its own
        # Quicken cleared/reconciled status. create_transfer wrote both legs at
        # the default (0,0), so the synthesized counter-leg in the other account
        # keeps its own status and reconciles independently -- do NOT copy this
        # leg's flag onto it, and do NOT blank this leg (that was the Clr=R loss).
        if mapped.cleared or mapped.reconciled:
            conn.execute(
                "UPDATE transactions SET cleared=?, reconciled=? WHERE id=?",
                (int(mapped.cleared), int(mapped.reconciled), txn_id))
            conn.commit()
        # Stamp the source id onto this leg so a re-download dedupes against it.
        if mapped.transaction_id:
            conn.execute("UPDATE transactions SET fitid=? WHERE id=?",
                         (mapped.transaction_id, txn_id))
            conn.commit()
    else:
        txn_id = ledger.add_transaction(
            conn, account_id, d, amt_cents,
            payee=(p or None),
            memo=(m or None),
            num=(chk or None),
            category_id=category_id,
            fitid=(mapped.transaction_id or None),
            # carry this row's own Quicken cleared/reconciled ('C') status
            cleared=int(mapped.cleared),
            reconciled=int(mapped.reconciled),
        )
    if review_id is not None:
        _set_state(conn, review_id, "accepted", accepted_txn_id=txn_id)
        conn.commit()
    # Learn renaming/category rules from the correction LAST so their self-commit
    # finalises the register write too, and never opens a dup-review window before
    # the review row is stamped accepted. Transfers keep their derived payee and
    # learn nothing (requirement 4). The category tree learns EVERY accepted
    # row, not just corrections -- an accepted prediction is a confirmation, and
    # the counts are what the confidence and coherence gates read.
    #
    # The rename tree keeps an EXAMPLE LOG rather than learning online: the row
    # is recorded with the id of the transaction it created, and the label is
    # read from that transaction at prediction time, so an edit in the register
    # or an undo here is honoured. A bulk accept (``learn=False``) records only
    # a row whose payee was actually RENAMED -- an auto-fill that was applied --
    # never one that kept the bank's text: a prediction nobody looked at is not
    # evidence, and a kept default is not a correction anyway.
    if not mapped.is_transfer and not learn:
        _record_applied_rename(conn, mapped, p, txn_id=txn_id, review_id=review_id)
    if learn:
        # Learn the transfer TARGET whenever the user pointed this row at an
        # account -- not only when the bank's text happened to parse as a
        # transfer. Keying on ``mapped.is_transfer`` meant a row the PARSER never
        # recognised could be sent to the same account forever without teaching
        # anything: "AUTOMATIC DEPOSIT, VENMO CASHOUT PPD" does not read as a
        # transfer, so five accepts onto [Venmo] learned nothing and the sixth
        # still arrived blank. What the user DID is the correction to learn from,
        # not what the parser guessed.
        if transfer_account_id is not None or mapped.is_transfer:
            transfer_rules.learn_from_edit(
                conn, mapped.memo, transfer_account_id,
                provisional=mapped.transfer_account_id)
        if not mapped.is_transfer:
            # A parser-detected transfer keeps its DERIVED payee, so there is no
            # rename to learn. A row the USER turned into a transfer has a payee
            # they chose, which is worth learning like any other.
            _learn_payee_rename(conn, mapped, p, txn_id=txn_id, review_id=review_id)
            # The payee-scoped tree replaces category_rules.learn_from_edit as
            # the learner. That call minted a GLOBAL keyword rule from a single
            # correction, which is how a town name became a Utilities rule; the
            # tree records a vote under this payee instead, where it can only
            # ever affect this payee. The keyword table is left alone -- still
            # readable, still editable in the Rules manager, no longer grown
            # behind the user's back.
            category_tree.learn(
                conn, p,
                category_tree.source_text(
                    mapped.memo, mapped.payee,
                    bool(getattr(mapped, "payee_supplied", False))),
                category_id)
    return txn_id


def _save_investment(conn, account_id: int, mapped: MappedRow, *,
                     memo: Optional[str] = None, review_id: Optional[int] = None,
                     date: Optional[str] = None, amount_cents: Optional[int] = None,
                     action: Optional[str] = None, symbol: Optional[str] = None,
                     quantity: Optional[str] = None,
                     price: Optional[str] = None, learn: bool = True) -> int:
    """Insert a reviewed NEW investment row into ``investment_transactions`` and
    rebuild the account's holdings. Returns the new investment-transaction id. The
    source id is stored as ``fitid`` so a later import dedupes against it."""
    from . import investments
    m = mapped.memo if memo is None else memo
    # Each override falls back to what the importer read, so an untouched field
    # keeps the source value.
    use_date = date or mapped.date
    use_action = (action or "").strip() or (mapped.action or "Buy")
    use_symbol = mapped.symbol if symbol is None else symbol
    use_qty = mapped.quantity if quantity is None else quantity
    use_price = mapped.price if price is None else price
    use_amount = mapped.amount_cents if amount_cents is None else int(amount_cents)
    cur = conn.execute(
        "INSERT INTO investment_transactions"
        "(account_id, date, action, symbol, quantity, price, amount, commission, memo, fitid) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            account_id, use_date, use_action,
            use_symbol or None, use_qty or None, use_price or None,
            use_amount, mapped.commission_cents or None,
            (m or None), (mapped.transaction_id or None),
        ),
    )
    txn_id = int(cur.lastrowid)
    conn.commit()
    # Refresh holdings so the account values correctly. A malformed action must not
    # abort the accept -- the row is already persisted -- so failures are swallowed.
    try:
        investments.rebuild_holdings(conn, account_id)
    except Exception:
        pass
    # The row states what a share was worth on its date; that is price history
    # the broker already handed us, and a 401(k)'s funds are quoted nowhere
    # public. Captured per accepted row, with the same swallow-on-failure rule --
    # the transaction is already persisted and must not be lost to a price.
    try:
        investments.learn_prices_from_transactions(conn, txn_id=txn_id)
    except Exception:
        pass
    # Learn the ACTION the user kept for this description -- the importer maps a
    # source's activity vocabulary onto Quicken actions by lookup tables that
    # are guesses; every accept is one correction the action tree replays for
    # the next import. The raw activity text is the strongest evidence there
    # is, so when the mapped action is still raw source text (not a canonical
    # Quicken action) it joins the description tokens.
    if learn:
        try:
            raw = (mapped.action or "").strip()
            rename_tree.learn(
                conn, (m or mapped.memo or ""), use_action, kind="action",
                extra="" if investments.is_known_action(raw) else raw,
                txn_id=txn_id, review_id=review_id)
        except Exception:
            pass
    if review_id is not None:
        _set_state(conn, review_id, "accepted", accepted_txn_id=txn_id)
        conn.commit()
    return txn_id


def _learn_payee_rename(conn, mapped: MappedRow, final_payee: Optional[str], *,
                        txn_id: Optional[int] = None,
                        review_id: Optional[int] = None) -> None:
    """Record an individually accepted row in the rename tree's example log
    and update the applied/overridden tallies.

    ``final_payee`` is the payee actually committed. It is compared to what the
    tree suggested for this text (:func:`rename_tree.suggest`, the same call
    :func:`predict_fields` made) for the management tallies only:

      * AUTO suggestion kept    -> tally it applied;
      * AUTO suggestion changed -> tally the suggested payee overridden;
      * DROPDOWN candidate picked -> tally it applied.

    The example itself is recorded whatever was shown. Whether it COUNTS is the
    corpus loader's call: a payee equal to the text the row was shown with (the
    description, its tidied form, the supplied field) is a kept default, not a
    rename, and is skipped there -- but it is still logged against ``txn_id``,
    so that if the user later fixes the payee in the register the row becomes a
    correction without anyone re-teaching it.
    """
    final = (final_payee or "").strip()
    if not final:
        return
    # The same evidence the prediction used (a supplied payee's tokens are part
    # of the description's pool), so the tally reflects what was actually shown.
    extra = (mapped.payee or "") if getattr(mapped, "payee_supplied", False) else ""
    sugg = rename_tree.suggest(conn, mapped.memo, extra=extra)
    if sugg.action == rename_tree.ACTION_AUTO:
        if final == sugg.payee:
            rename_tree.note_applied(conn, final)
        else:
            rename_tree.note_overridden(conn, sugg.payee)
    elif sugg.action == rename_tree.ACTION_DROPDOWN and final in sugg.payees:
        rename_tree.note_applied(conn, final)
    rename_tree.learn(conn, mapped.memo, final, extra=extra,
                      txn_id=txn_id, review_id=review_id)


def _record_applied_rename(conn, mapped: MappedRow, final_payee: Optional[str], *,
                           txn_id: Optional[int] = None,
                           review_id: Optional[int] = None) -> None:
    """The bulk-accept half of :func:`_learn_payee_rename`: log the row only
    when its payee is a RENAME of what it was shown with. A kept default from a
    row nobody read is not evidence and is not logged; an applied auto-fill is
    a real rename in the register and is, so that a later register edit of it
    is honoured like any other."""
    final = (final_payee or "").strip()
    extra = (mapped.payee or "") if getattr(mapped, "payee_supplied", False) else ""
    if not rename_tree.is_correction(final, mapped.memo, extra):
        return
    rename_tree.learn(conn, mapped.memo, final, extra=extra,
                      txn_id=txn_id, review_id=review_id)


def accept_match(conn, entry: ReviewEntry) -> dict:
    """Accept a MATCHING entry against its existing register transaction.

    Stamps the downloaded row's source id onto the matched txn's ``fitid`` (only
    when it has none, so a real fitid is never clobbered) and marks the txn
    cleared -- the Quicken "this download == this register line" gesture. It also
    RAISES the txn's ``reconciled`` flag when the incoming row carries its own
    reconciled status (a QIF ``C`` line of ``X``/``R`` -> ``mapped.reconciled=1``),
    so a single-account QIF re-import does not drop the R off ~30 years of already
    reconciled transfers it matches here; raise-only, so a plain download
    (reconciled=0) never knocks an already-reconciled register row back down.
    Returns the prior ``{fitid, cleared, reconciled}`` so :func:`revert_match` can
    undo it exactly.

    When the entry is persisted (``entry.review_id`` set), its ``review_items``
    row is stamped accepted with the matched txn id and the prior
    fitid/cleared/reconciled for a later undo."""
    txn_id = entry.matched_txn_id
    if txn_id is None:
        raise ValueError("entry is not a MATCHING row")
    if entry.mapped.is_investment:
        # The identical investment txn already exists (its id is in
        # investment_transactions, not transactions). Stamp the source id onto it
        # if it has none; there is no 'cleared' flag on investment rows.
        cur = conn.execute(
            "SELECT fitid FROM investment_transactions WHERE id=?", (txn_id,)
        ).fetchone()
        if cur is None:
            raise KeyError("no investment transaction %s" % txn_id)
        prior = {"fitid": cur[0], "cleared": None}
        if cur[0] in (None, "") and entry.mapped.transaction_id:
            conn.execute("UPDATE investment_transactions SET fitid=? WHERE id=?",
                         (entry.mapped.transaction_id, txn_id))
        prior["price"] = _fill_missing_price(conn, txn_id, entry)
        _set_state(conn, entry.review_id, "accepted", accepted_txn_id=txn_id,
                   prior_fitid=prior["fitid"], prior_cleared=None)
        conn.commit()
        return prior
    cur = conn.execute(
        "SELECT fitid, cleared, reconciled, amount FROM transactions WHERE id=?",
        (txn_id,)
    ).fetchone()
    if cur is None:
        raise KeyError("no transaction %s" % txn_id)
    prior = {"fitid": cur[0], "cleared": cur[1], "reconciled": cur[2],
             "amount": cur[3]}
    new_fitid = cur[0] if cur[0] not in (None, "") else (entry.mapped.transaction_id or None)
    # A match means this register line IS the incoming row, so it is at least
    # CLEARED (Quicken's "accept" gesture -> cleared=1). Additionally carry the
    # incoming row's RECONCILED status -- the QIF 'C' line's X/R token, mapped
    # into entry.mapped.reconciled. Without this, a single-account QIF re-import
    # matched ~30 years of already-reconciled transfers here and silently dropped
    # their R (only cleared=1 was written), so reconciled transfer legs imported
    # WITHOUT the R flag. RAISE-only: never knock an already-reconciled register
    # row back down when a plain download (reconciled=0) merely matches it.
    incoming_reconciled = int(getattr(entry.mapped, "reconciled", 0) or 0)
    new_reconciled = 1 if (incoming_reconciled or int(cur[2] or 0)) else 0
    # THE AMOUNT COMES FROM THE BANK. A match means this register line IS the
    # downloaded row, and the bank's figure is what actually left the account.
    # The line's own amount may be a FORECAST -- a scheduled pre-entry carries
    # the finance calendar's median, a loan pre-entry an amortization figure
    # whose escrow has since moved -- which is the very reason it needed a
    # tolerant or hand-made match in the first place. Leaving it stale meant the
    # register kept a number that never happened.
    #
    # PAYEE, CATEGORY and SPLIT are NOT touched: those are the user's, and
    # keeping them is the whole point of pre-entering (the file-import path says
    # the same -- "the payee is the pre-entry's own, not the bank's descriptor").
    # A split whose lines no longer sum to the new amount is allowed and stays
    # visible; that sum invariant was deliberately removed so a row with a
    # discrepancy can still be edited. (By request.)
    conn.execute(
        "UPDATE transactions SET fitid=?, cleared=1, reconciled=?, amount=? "
        "WHERE id=?",
        (new_fitid, new_reconciled, int(entry.mapped.amount_cents), txn_id))
    _set_state(conn, entry.review_id, "accepted", accepted_txn_id=txn_id,
               prior_fitid=prior["fitid"], prior_cleared=prior["cleared"],
               prior_reconciled=prior["reconciled"],
               prior_amount=prior["amount"])
    conn.commit()
    return prior


def _fill_missing_price(conn, txn_id: int, entry) -> Optional[str]:
    """Fill a matched investment row's EMPTY price from the incoming row, and
    record that price in ``price_history``. Returns the prior price so
    :func:`revert_match` can undo it exactly.

    Accepting a match used to stamp ``fitid`` and nothing else, so a download
    that carried a price for a row the register held without one threw it away
    -- and for a plan fund quoted nowhere public, that download is the only
    price that will ever exist. A dormant 401(k) then held its last contribution
    price for six years, then revaluing the entire position in one day when a
    price finally landed.

    Two rules, and both matter:

    * **Never replace a price that is already there.** A match says "this
      register line IS that download", not "restate it"; silently rewriting a
      figure the user already has is the same complaint people make about
      Quicken changing a date on accept. Empty means missing, not wrong.
    * **A derived price carries its uncertainty.** When the row states no price
      but does state a value and a share count, the quotient is recorded with
      the interval that value's and the count's rounding imply
      (``investments.price_bounds``), so a fee-derived number is visibly less
      trustworthy than a quote rather than silently equal to one.
    """
    from mammon import investments
    row = conn.execute(
        "SELECT symbol, date, price, amount, commission, quantity "
        "FROM investment_transactions WHERE id=?", (txn_id,)).fetchone()
    if row is None or (row["price"] not in (None, "")):
        return None                       # nothing missing; leave it alone
    mapped = entry.mapped
    price = getattr(mapped, "price", None)
    bounds = None
    if price in (None, ""):
        price = investments._price_from_value(
            row["amount"], row["commission"], row["quantity"])
        bounds = investments.price_bounds(
            row["amount"], row["commission"], row["quantity"])
    if price in (None, ""):
        return None
    conn.execute("UPDATE investment_transactions SET price=? WHERE id=?",
                 (str(price), txn_id))
    symbol = (row["symbol"] or "").strip()
    if symbol and row["date"]:
        lo, hi = bounds if bounds else (None, None)
        investments.record_prices_if_absent(
            conn, [(symbol, row["date"], price, "txn", lo, hi)])
    return ""                             # prior price was empty


def revert_match(conn, entry: ReviewEntry, prior: dict) -> None:
    """Undo an :func:`accept_match`, restoring the txn's prior
    fitid/cleared/reconciled."""
    txn_id = entry.matched_txn_id
    if txn_id is None:
        return
    if entry.mapped.is_investment:
        conn.execute(
            "UPDATE investment_transactions SET fitid=? WHERE id=?",
            (prior.get("fitid"), txn_id))
        # A price the accept FILLED is put back to empty; one it left alone is
        # not touched (prior["price"] is None in that case). The price_history
        # row it wrote stays -- a recorded price is not the transaction's to
        # withdraw, and it is DO-NOTHING precedence, so it displaced nothing.
        if prior.get("price") is not None:
            conn.execute(
                "UPDATE investment_transactions SET price=? WHERE id=?",
                (prior.get("price") or None, txn_id))
        _set_state(conn, entry.review_id, "pending", accepted_txn_id=None)
        conn.commit()
        return
    if prior.get("amount") is None:
        conn.execute(
            "UPDATE transactions SET fitid=?, cleared=?, reconciled=? WHERE id=?",
            (prior.get("fitid"), prior.get("cleared", 0),
             prior.get("reconciled", 0), txn_id))
    else:
        # accept_match adopted the bank's amount -- put the line's own back.
        conn.execute(
            "UPDATE transactions SET fitid=?, cleared=?, reconciled=?, amount=? "
            "WHERE id=?",
            (prior.get("fitid"), prior.get("cleared", 0),
             prior.get("reconciled", 0), int(prior["amount"]), txn_id))
    _set_state(conn, entry.review_id, "pending", accepted_txn_id=None)
    conn.commit()


def delete_saved(conn, txn_id: int, review_id: Optional[int] = None) -> None:
    """Undo a :func:`save_new`: remove the register row it created. When
    ``review_id`` is given, return its persisted row to pending.

    The category vote that accept recorded is withdrawn too. Without this an
    accept the user immediately undoes still counts toward the payee's tally and
    its trie node forever -- and those counts ARE the confidence gate, so a
    mis-accepted row would keep pushing a category the user explicitly rejected.
    Read from the register row BEFORE it is deleted; the review row supplies the
    source text, which is the only place it survives for a supplied-payee source.
    The rename example logged against the transaction goes with it -- otherwise
    an undone accept would keep teaching the tree a payee the user took back.
    """
    rename_tree.forget_examples(conn, txn_id=txn_id, commit=False)
    txn = ledger.get_transaction(conn, txn_id)
    if (txn is not None and txn["category_id"] is not None
            and txn["transfer_account_id"] is None):
        text = ""
        if review_id is not None:
            row = conn.execute(
                "SELECT memo, payee, payee_supplied, is_investment "
                "FROM review_items WHERE id=?", (review_id,)).fetchone()
            if row is not None and not row["is_investment"]:
                text = category_tree.source_text(
                    row["memo"], row["payee"], bool(row["payee_supplied"] or 0))
        category_tree.unlearn(conn, txn["payee"], text, txn["category_id"])
    ledger.delete_transaction(conn, txn_id)
    if review_id is not None:
        _set_state(conn, review_id, "pending", accepted_txn_id=None)
        conn.commit()


# ---------------------------------------------------------------------------
# Import BATCHES (schema v29).
#
# A batch is ONE import operation, which may span several files: a webSlinger
# download can pull a set of files in one go, and an institution that will not
# export an arbitrary date range must be fetched piecemeal (typically a file per
# month). Picking several files in the import dialog is likewise one batch. They
# are reviewed together, so they are retained and shown together.
#
# Batches are also the unit of RETENTION. Elapsed time is not a valid metric --
# a user can go months without touching the register, and that gap says nothing
# about whether the review data is still useful. What makes a review stale is
# SUBSEQUENT ACTIVITY: once a couple more periods have been imported into the
# same account, nobody refers back to the older rows.
# ---------------------------------------------------------------------------
# Retention is the MORE generous of two rules, because download cadence varies
# enormously: someone pulling daily or weekly would lose a fortnight of history
# under a batch count alone, while someone who imports twice a year would lose
# everything under a time window alone.
KEEP_BATCHES = 3          # the current batch plus the two before it, per account
KEEP_DAYS = 365           # ...or anything from the last year, whichever is more

# Visibility modes for the review panel (persisted per account by the UI).
SHOW_PENDING = "pending"      # hide anything already actioned
SHOW_BATCH = "batch"          # + actioned rows of the batches pending rows are in
SHOW_ALL = "all"              # + every retained batch
SHOW_MODES = (SHOW_PENDING, SHOW_BATCH, SHOW_ALL)


def start_batch(conn, account_id: int, *, source: str = "import",
                file_count: int = 0, note: str = "") -> int:
    """Open a new import batch for ``account_id`` and return its id.

    Call ONCE per import operation, not once per file: several files chosen
    together (or downloaded together) belong to the same batch."""
    cur = conn.execute(
        "INSERT INTO import_batches(account_id, source, file_count, note) "
        "VALUES (?,?,?,?)",
        (account_id, source, int(file_count), note or None))
    conn.commit()
    return int(cur.lastrowid)


def current_batch_id(conn, account_id: int) -> Optional[int]:
    """The newest batch for ``account_id``, or None if it has never imported."""
    row = conn.execute(
        "SELECT id FROM import_batches WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,)).fetchone()
    return int(row[0]) if row else None


def retained_batch_ids(conn, account_id: int, keep: int = KEEP_BATCHES) -> list:
    """The newest ``keep`` batch ids for ``account_id``, newest first."""
    rows = conn.execute(
        "SELECT id FROM import_batches WHERE account_id=? ORDER BY id DESC LIMIT ?",
        (account_id, int(keep))).fetchall()
    return [int(r[0]) for r in rows]


def pending_batch_ids(conn, account_id: int) -> list:
    """Batch ids that still contain PENDING rows, newest first.

    A user can import several times before reviewing any of it, so "the batch
    being worked on" is not one batch -- it is however many still hold unactioned
    rows."""
    rows = conn.execute(
        "SELECT DISTINCT batch_id FROM review_items "
        "WHERE account_id=? AND state='pending' AND batch_id IS NOT NULL "
        "ORDER BY batch_id DESC", (account_id,)).fetchall()
    return [int(r[0]) for r in rows]


def purge_old_batches(conn, account_id: int, keep: int = KEEP_BATCHES,
                      keep_days: int = KEEP_DAYS) -> int:
    """Drop review rows from batches that are past BOTH retention rules.

    A batch is kept when ANY of these holds:

    * it is one of the newest ``keep`` batches, or
    * it was created within the last ``keep_days``, or
    * it still contains PENDING rows.

    The first two are deliberately whichever-is-more: a daily downloader would
    lose a fortnight under a batch count alone, and a twice-a-year importer would
    lose everything under a time window alone. The third is absolute -- unreviewed
    work is never thrown away to satisfy a retention rule, however old it is.

    Returns the number of ``review_items`` deleted. Rows with a NULL ``batch_id``
    predate schema v29 and are left alone: they cannot be placed in the ordering,
    and discarding a user's existing queue would be worse than a little extra
    history.

    Only the REVIEW rows go. Anything a row produced in the register stays --
    accepting a row created a real transaction, and that belongs to the ledger,
    not to the review queue."""
    keep_ids = set(retained_batch_ids(conn, account_id, keep))
    keep_ids.update(
        int(r[0]) for r in conn.execute(
            "SELECT id FROM import_batches WHERE account_id=? "
            "AND created_at >= datetime('now', ?)",
            (account_id, f"-{int(keep_days)} days")).fetchall())
    keep_ids.update(pending_batch_ids(conn, account_id))
    if not keep_ids:
        return 0
    ids = sorted(keep_ids)
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"DELETE FROM review_items WHERE account_id=? AND batch_id IS NOT NULL "
        f"AND state<>'pending' AND batch_id NOT IN ({marks})",
        (account_id, *ids))
    deleted = cur.rowcount or 0
    # A batch row itself only goes once nothing references it.
    conn.execute(
        f"DELETE FROM import_batches WHERE account_id=? AND id NOT IN ({marks}) "
        f"AND id NOT IN (SELECT DISTINCT batch_id FROM review_items "
        f"              WHERE batch_id IS NOT NULL)",
        (account_id, *ids))
    conn.commit()
    return deleted


# ---------------------------------------------------------------------------
# persisted review list -- rows survive restarts / account switches in
# ``review_items`` (schema v11). ``persist_entries`` stores a freshly built
# review; ``load_pending`` rebuilds the still-actionable entries; the bulk
# operations act on a whole account's pending/accepted set.
# ---------------------------------------------------------------------------
def persist_entries(conn, account_id: int, entries, batch_id=None) -> int:
    """Store ``entries`` (from :func:`build_review`) as pending ``review_items``.

    Uses ``INSERT OR IGNORE`` so a re-download does not duplicate a row already
    present for the same ``(account_id, transaction_id)``; rows with an empty
    ``transaction_id`` always insert. Each entry's ``review_id`` is set to its
    row id (new or pre-existing). Returns the count of NEWLY inserted rows.

    ``batch_id`` groups this import operation's rows (see :func:`start_batch`);
    all files of one multi-file import share it."""
    inserted = 0
    for entry in entries:
        m = entry.mapped
        cur = conn.execute(
            "INSERT OR IGNORE INTO review_items "
            "(account_id, transaction_id, account_ref, date, amount, payee, memo, "
            " check_number, is_transfer, transfer_account, raw_json, label, "
            " matched_txn_id, match_method, is_investment, action, symbol, quantity, "
            " price, commission, payee_supplied, batch_id, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', datetime('now'))",
            (account_id, (m.transaction_id or None), m.account_ref, m.date,
             m.amount_cents, m.payee, m.memo, m.check_number,
             1 if m.is_transfer else 0, m.transfer_account,
             json.dumps(m.raw), entry.label, entry.matched_txn_id,
             entry.match_method,
             1 if m.is_investment else 0, (m.action or None), (m.symbol or None),
             (m.quantity or None), (m.price or None), (m.commission_cents or None),
             1 if m.payee_supplied else 0, batch_id),
        )
        if cur.rowcount:
            entry.review_id = int(cur.lastrowid)
            inserted += 1
        else:
            row = conn.execute(
                "SELECT id FROM review_items WHERE account_id=? AND transaction_id=?",
                (account_id, m.transaction_id),
            ).fetchone()
            if row is not None:
                entry.review_id = int(row[0])
    conn.commit()
    return inserted


def _entry_from_row(row) -> ReviewEntry:
    keys = row.keys()

    def _g(name, default=None):
        return row[name] if name in keys else default

    mapped = MappedRow(
        transaction_id=row["transaction_id"] or "",
        account_ref=row["account_ref"] or "",
        date=row["date"] or "",
        amount_cents=row["amount"] or 0,
        payee=row["payee"] or "",
        memo=row["memo"] or "",
        check_number=row["check_number"] or "",
        is_transfer=bool(row["is_transfer"]),
        transfer_account=row["transfer_account"] or "",
        is_investment=bool(_g("is_investment", 0)),
        action=_g("action", "") or "",
        symbol=_g("symbol", "") or "",
        quantity=_g("quantity", "") or "",
        price=_g("price", "") or "",
        commission_cents=_g("commission", 0) or 0,
        # Restore the authoritative-payee flag so a reloaded file row (OFX NAME /
        # QIF payee / tabular From-To) still skips the description renamer in
        # predict_fields. Absent on pre-V27 rows -> default 0 (behaves as derived).
        payee_supplied=bool(_g("payee_supplied", 0)),
        raw=json.loads(row["raw_json"] or "{}"),
    )
    return ReviewEntry(
        mapped=mapped,
        label=row["label"] or LABEL_NEW,
        matched_txn_id=row["matched_txn_id"],
        match_method=row["match_method"] or "",
        review_id=int(row["id"]),
        state=_g("state", "pending") or "pending",
        accepted_txn_id=_g("accepted_txn_id", None),
        batch_id=_g("batch_id", None),
    )


def set_check_number(conn, review_id, value) -> None:
    """Persist an edited Num onto its stored review row.

    Num is the one review cell the user edits directly -- a check number, or a
    third-party label (Venmo/PayPal/'Sched') they need in order to identify the
    payee. It is a real correction, so it is saved like any other field rather
    than living only in memory, where a restart silently discarded it."""
    if review_id is None:
        return
    conn.execute("UPDATE review_items SET check_number=? WHERE id=?",
                 ((value or "").strip() or None, int(review_id)))
    conn.commit()


def set_symbol(conn, review_id, value) -> None:
    """Persist an edited Security onto its stored review row.

    Security is the one review cell the user edits directly on an INVESTMENT
    account, and it is the highest-stakes edit in the review list. Brokers rename
    funds and append tickers to their names, so an imported row routinely names a
    security the ledger does not hold. Accepted unedited it creates a SECOND,
    phantom security -- and the per-share price derived from that row is recorded
    against the phantom, leaving the fund the user actually owns with no price
    history at all. Correcting the name here is what puts the price on the right
    security, so it is saved like any other correction rather than living only in
    memory, where a restart would silently discard it."""
    if review_id is None:
        return
    conn.execute("UPDATE review_items SET symbol=? WHERE id=?",
                 ((value or "").strip() or None, int(review_id)))
    conn.commit()


_EDITABLE_REVIEW_FIELDS = ("date", "amount", "payee", "memo", "action",
                           "symbol", "quantity", "price", "commission")


def update_review_row(conn, review_id, **fields) -> None:
    """Persist a full edit of a stored review row.

    The review list has to be correctable in every field, not just the one the
    importer is most likely to get wrong. An importer maps a source's own
    activity vocabulary onto Quicken actions by a lookup table, and that table is
    a GUESS about what one institution means by "RECORDKEEPING FEE" or
    "ADMINISTRATIVE FEES" -- Quicken itself gets these wrong often enough that
    correcting them is a normal part of importing. What makes a wrong guess
    survivable is being able to open the row and fix it before accepting.

    Unknown keys raise rather than being dropped silently."""
    if review_id is None:
        return
    bad = set(fields) - set(_EDITABLE_REVIEW_FIELDS)
    if bad:
        raise ValueError(f"unknown review field(s): {sorted(bad)}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE review_items SET {sets} WHERE id=?",
                 (*fields.values(), int(review_id)))
    conn.commit()


def entry_states(conn, entries) -> dict:
    """Count the CURRENT stored state of each entry in ``entries``.

    After :func:`persist_entries` every entry carries a ``review_id``: newly
    inserted rows point at their new row, and rows that collided with an existing
    ``(account_id, transaction_id)`` point at the row already there. Looking those
    states up is what lets a caller tell apart two outcomes that otherwise look
    identical -- a file that yielded nothing, and a file whose every row was
    imported and ACCEPTED on an earlier run. Returns e.g. ``{"accepted": 30}``.
    """
    ids = [e.review_id for e in entries if getattr(e, "review_id", None)]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT state, COUNT(*) AS n FROM review_items WHERE id IN ({marks}) "
        "GROUP BY state", ids).fetchall()
    return {(r["state"] or "pending"): int(r["n"]) for r in rows}


def load_pending(conn, account_id: int) -> list[ReviewEntry]:
    """Rebuild the still-pending review entries for ``account_id`` from the DB.

    This is what a re-opened / returned-to review shows: only the rows that are
    neither accepted nor discarded."""
    rows = conn.execute(
        "SELECT * FROM review_items WHERE account_id=? AND state='pending' "
        "ORDER BY date, id",
        (account_id,),
    ).fetchall()
    return [_entry_from_row(r) for r in rows]


def load_review(conn, account_id: int, mode: str = SHOW_PENDING) -> list:
    """Rebuild the review list for ``account_id`` under a visibility ``mode``.

    Pending rows are ALWAYS included; the mode only widens what else is shown:

    * ``SHOW_PENDING`` -- pending only. What the panel used to show, and the
      reason accepting a row made it vanish for good.
    * ``SHOW_BATCH``   -- plus the actioned rows of the batches that still hold
      pending work. Not "the current batch": several imports can pile up before
      any of them is reviewed, so the review in progress can span batches, and
      the accepted rows worth seeing are the ones sitting alongside it.
    * ``SHOW_ALL``     -- plus every retained batch (see :func:`purge_old_batches`),
      for going back to an earlier period's ground truth.

    Newest batch first, then by date, so the work in progress is at the top.
    """
    if mode not in SHOW_MODES:
        mode = SHOW_PENDING
    if mode == SHOW_PENDING:
        return load_pending(conn, account_id)
    if mode == SHOW_ALL:
        rows = conn.execute(
            "SELECT * FROM review_items WHERE account_id=? "
            "ORDER BY COALESCE(batch_id, -1) DESC, date, id",
            (account_id,)).fetchall()
        return [_entry_from_row(r) for r in rows]

    # SHOW_BATCH: the batches pending work lives in. With nothing pending at all
    # (everything just accepted), fall back to the newest batch -- otherwise the
    # mode would show an empty list the moment the user finishes a review, which
    # is exactly when they most want to look back at what they just did.
    ids = pending_batch_ids(conn, account_id)
    if not ids:
        cur = current_batch_id(conn, account_id)
        ids = [cur] if cur is not None else []
    if not ids:
        return load_pending(conn, account_id)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM review_items WHERE account_id=? "
        f"AND (state='pending' OR batch_id IN ({marks})) "
        f"ORDER BY COALESCE(batch_id, -1) DESC, date, id",
        (account_id, *ids)).fetchall()
    return [_entry_from_row(r) for r in rows]


def count_pending(conn, account_id: int) -> int:
    """Number of pending review rows for ``account_id`` (drives the sidebar dot)."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM review_items WHERE account_id=? AND state='pending'",
        (account_id,),
    ).fetchone()[0])


def accept_all(conn, account_id: int) -> int:
    """Accept every pending row for ``account_id``, applying the SAME predictions
    the register's editable pending row would show.

    Accept All is "accept every row without editing it", and it has to agree with
    doing exactly that by hand. It did not: NEW rows were saved with the RAW
    mapped values -- no learned rename, no learned category, no learned
    investment action -- so a rename tree the user had spent weeks training
    produced bank gobbledygook the moment they used the bulk button instead of
    the single one.

    Predictions are recomputed per row inside the loop, so a rename learned from
    an earlier row of the SAME batch reaches the later ones, exactly as it does
    when accepting one at a time.

    Nothing is LEARNED from a bulk accept (``learn=False``). A prediction nobody
    looked at is not evidence: reinforcing a dropdown candidate the user never
    chose -- or, where nothing is learned yet, teaching the tree that a statement
    text maps to a tidied copy of ITSELF -- would make the tree more confident
    precisely where it got no confirmation. Accepting a row individually is the
    act that teaches; accepting a hundred unread ones is not.

    Returns the number of rows actioned.
    """
    count = 0
    for entry in load_pending(conn, account_id):
        if entry.is_matching and entry.matched_txn_id is not None:
            accept_match(conn, entry)
            count += 1
        elif entry.is_new and entry.mapped.date:
            _accept_one_predicted(conn, account_id, entry)
            count += 1
    return count


def _accept_one_predicted(conn, account_id: int, entry: ReviewEntry) -> int:
    """Save one NEW entry using the predictions alone, learning nothing.

    The prediction fields are stashed on the mapped row the way the register
    model stashes them, so anything downstream that compares "what was predicted"
    against "what was saved" sees them agree -- an unedited accept is not a
    correction."""
    m = entry.mapped
    if m.is_investment:
        return save_new(conn, account_id, m, review_id=entry.review_id,
                        learn=False, action=predict_action(conn, m))
    payee, category_id = predict_fields(conn, m, account_id=account_id)
    m.predicted_payee = payee
    m.category_id = category_id
    transfer_account_id = None
    if m.is_transfer:
        transfer_account_id = predict_transfer_account(conn, m)
        if transfer_account_id is None and m.transfer_account:
            transfer_account_id = transfer_account_id_for_name(
                conn, "[%s]" % m.transfer_account)
        m.transfer_account_id = transfer_account_id
        category_id = None
    return save_new(conn, account_id, m, payee=payee, category_id=category_id,
                    transfer_account_id=transfer_account_id,
                    review_id=entry.review_id, learn=False)


def discard_all(conn, account_id: int) -> int:
    """Discard every pending row for ``account_id`` -- DELETE them (no register
    writes). Returns the number of rows removed.

    Discarding used to TOMBSTONE: the row stayed with ``state='discarded'`` so a
    re-download's ``INSERT OR IGNORE`` would not re-add it. That inverted the
    gesture's meaning. Discarding a review row is "I am not dealing with this
    now", and the user's next move is to download the range again and take
    another run at matching it -- but the tombstone silently ate the re-download:
    106 discarded rows suppressed everything but the 5 genuinely new ids, and
    with no un-discard anywhere, discard was a one-way door.

    A row the user genuinely never wants is excluded by choosing a different
    date range, which they control directly; it does not need a permanent
    per-row veto that only a hand-written UPDATE could lift. (By request.)

    ACCEPTED rows still stay: those became register transactions, and re-offering
    them would be a duplicate.
    """
    cur = conn.execute(
        "DELETE FROM review_items WHERE account_id=? AND state='pending'",
        (account_id,),
    )
    conn.commit()
    return cur.rowcount or 0


def discard_one(conn, review_id: Optional[int]) -> None:
    """Discard a SINGLE pending row without touching the register.

    DELETES it, exactly as :func:`discard_all` does and for the same reason: a
    discarded row must come back on the next download of that range, because
    "discard" means "not now", not "never again". A ``None`` id (an unpersisted
    in-memory entry) is a no-op at the DB layer; the caller still drops it from
    the in-memory list."""
    if review_id is None:
        return
    conn.execute(
        "DELETE FROM review_items WHERE id=? AND state='pending'",
        (review_id,),
    )
    conn.commit()


def _restore_matched_line(conn, row) -> None:
    """Put back the register line an accepted match stamped.

    Shared by :func:`unmatch_one` and :func:`undo_all_matches` so the two cannot
    drift: both restore ``fitid``/``cleared``/``reconciled``, both put back the
    amount when accepting adopted the bank's, and both restore an INVESTMENT row
    in ``investment_transactions``. That last point is not cosmetic -- an
    investment row's ``accepted_txn_id`` is an id in its own table and means
    something entirely different in ``transactions``.
    """
    txn_id = row["accepted_txn_id"]
    if txn_id is None:
        return
    if row["is_investment"]:
        conn.execute("UPDATE investment_transactions SET fitid=? WHERE id=?",
                     (row["prior_fitid"], int(txn_id)))
        return
    if row["prior_amount"] is None:
        conn.execute(
            "UPDATE transactions SET fitid=?, cleared=?, reconciled=? WHERE id=?",
            (row["prior_fitid"], row["prior_cleared"] or 0,
             row["prior_reconciled"] or 0, int(txn_id)))
    else:
        conn.execute(
            "UPDATE transactions SET fitid=?, cleared=?, reconciled=?, amount=? "
            "WHERE id=?",
            (row["prior_fitid"], row["prior_cleared"] or 0,
             row["prior_reconciled"] or 0, int(row["prior_amount"]), int(txn_id)))


def unmatch_one(conn, review_id: Optional[int]) -> bool:
    """Break the match on ONE review row and return it to pending NEW.

    The single-row companion to :func:`undo_all_matches`, which was the only way
    to undo a match at all -- so correcting one wrong match meant tearing down
    every right one too (by request).

    Handles both states. A row still PENDING has touched nothing, so only the
    review row is cleared. A row already ACCEPTED stamped the register line it
    matched, so its prior ``fitid``/``cleared``/``reconciled`` are put back
    first -- from the values ``accept_match`` recorded for exactly this purpose.
    Investment rows are restored in their OWN table: their ids come from
    ``investment_transactions`` and mean nothing in ``transactions``.

    Returns True when a match was broken.
    """
    if review_id is None:
        return False
    row = conn.execute(
        "SELECT id, label, state, matched_txn_id, accepted_txn_id, prior_fitid, "
        "       prior_cleared, prior_reconciled, prior_amount, is_investment "
        "FROM review_items WHERE id=?", (int(review_id),)).fetchone()
    if row is None or row["label"] != LABEL_MATCHING:
        return False
    if row["state"] == "accepted":
        _restore_matched_line(conn, row)
    conn.execute(
        "UPDATE review_items SET state='pending', label=?, matched_txn_id=NULL, "
        "match_method='', accepted_txn_id=NULL, prior_fitid=NULL, "
        "prior_cleared=NULL, prior_reconciled=NULL, prior_amount=NULL "
        "WHERE id=?",
        (LABEL_NEW, int(review_id)))
    conn.commit()
    return True


def release_txn_matches(conn, account_id: int, txn_id: int,
                        except_review_id: Optional[int] = None) -> int:
    """Unmatch every review row in ``account_id`` claiming ``txn_id``.

    One register line is one event, so at most one review row may be matched to
    it. The user hand-matched an exact row onto a line another row had already
    taken, and ended with two review rows pointing at the same transaction --
    the second silently reconciling a line that was already spoken for.
    Returns how many were released."""
    rows = conn.execute(
        "SELECT id FROM review_items WHERE account_id=? AND label=? "
        "  AND (matched_txn_id=? OR accepted_txn_id=?)",
        (account_id, LABEL_MATCHING, int(txn_id), int(txn_id))).fetchall()
    n = 0
    for r in rows:
        if except_review_id is not None and int(r["id"]) == int(except_review_id):
            continue
        if unmatch_one(conn, int(r["id"])):
            n += 1
    return n


def undo_all_matches(conn, account_id: int) -> int:
    """Revert every accepted MATCHING row for ``account_id`` back to pending.

    Restores each matched txn's fitid/cleared/reconciled to the recorded prior
    values and returns its review row to pending. Returns the number of rows
    reverted."""
    rows = conn.execute(
        "SELECT id, accepted_txn_id, prior_fitid, prior_cleared, "
        "       prior_reconciled, prior_amount, is_investment "
        "FROM review_items WHERE account_id=? AND state='accepted' AND label=?",
        (account_id, LABEL_MATCHING),
    ).fetchall()
    count = 0
    for row in rows:
        # Restores through the SHARED helper, so the bulk undo cannot drift from
        # the single-row one -- and so an investment row is no longer restored by
        # writing its ``investment_transactions`` id into ``transactions``.
        # The rows stay MATCHING: this un-ACCEPTS them, it does not unmatch them
        # (that is Unmatch, :func:`unmatch_one`).
        _restore_matched_line(conn, row)
        _set_state(conn, int(row["id"]), "pending", accepted_txn_id=None,
                   prior_amount=None)
        count += 1
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# manual match -- the user right-clicks a NEW row and hand-picks the existing
# register line it corresponds to (wider window than the auto match, any amount).
# ---------------------------------------------------------------------------
def manual_match_candidates(conn, account_id: int, mapped: MappedRow, *,
                            window_days: int = MANUAL_WINDOW_DAYS) -> list[dict]:
    """Existing register transactions in ``account_id`` matching ``mapped``.

    The user has already been failed by the automatic matcher and is looking for
    the row themselves, so BOTH tolerances open up: +/- ``window_days`` (capped
    at :data:`MANUAL_WINDOW_DAYS`, a fortnight -- past that a "candidate" is
    guesswork about a different month) and any amount within
    :data:`SCHED_AMOUNT_TOLERANCE`, not just the exact cents. That second half is
    the point: a scheduled payment whose escrow moved is precisely the row the
    exact-amount filter hid, and it is the row the user is hunting for.

    The SIGN still cannot vary -- a payment is never answered by a deposit, and
    offering one would be noise, not tolerance. Ordered nearest-amount first
    (so an exact match still heads the list) then nearest date, for the user to
    eyeball. Falls back to the whole account when the mapped row carries no
    date. Each dict is ``{id, date, payee, amount}``."""
    want = int(mapped.amount_cents)
    span = min(int(window_days), MANUAL_WINDOW_DAYS)
    tol = abs(want) * SCHED_AMOUNT_TOLERANCE
    sign_ok = ("((amount < 0 AND ? < 0) OR (amount > 0 AND ? > 0)) "
               "AND ABS(ABS(amount) - ABS(?)) <= ? ")
    if mapped.date:
        lo = record.iso_shift(mapped.date, -span)
        hi = record.iso_shift(mapped.date, span)
        rows = conn.execute(
            "SELECT id, date, payee, amount FROM transactions "
            "WHERE account_id=? AND date BETWEEN ? AND ? AND " + sign_ok +
            "ORDER BY ABS(amount - ?), ABS(julianday(date) - julianday(?)), id",
            (account_id, lo, hi, want, want, want, tol, want, mapped.date),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, date, payee, amount FROM transactions "
            "WHERE account_id=? AND " + sign_ok +
            "ORDER BY ABS(amount - ?), date, id",
            (account_id, want, want, want, tol, want),
        ).fetchall()
    return [{"id": int(r["id"]), "date": r["date"], "payee": r["payee"],
             "amount": r["amount"]} for r in rows]


def set_manual_match(conn, entry: ReviewEntry, matched_txn_id: int) -> None:
    """Flip ``entry`` to a manual MATCHING against ``matched_txn_id``.

    Mutates the in-memory entry and, when persisted, its ``review_items`` row so
    the choice survives (still pending -- the user still accepts it).

    The chosen line must be the SAME SIGN and within
    :data:`SCHED_AMOUNT_TOLERANCE` of the downloaded amount; anything else is
    rejected (``ValueError``) rather than turned into a bogus match, even when
    hand-picked past the candidate list. This is the last gate for the manual
    path.

    Any OTHER review row already matched to that line is unmatched first
    (:func:`release_txn_matches`) -- one register line is one event.

    It used to demand the cents match exactly, which made the manual path
    useless for the case that most needs it: a scheduled payment whose escrow
    changed after it was pre-entered never equals the real debit, so the user
    could neither auto-match it nor hand-match it. The SIGN gate is what the
    strictness was really protecting -- a payment is never the same event as a
    deposit -- and that still holds absolutely. (Cross-account transfer-leg
    *pairing* is a separate path in ``importers/core.py`` and is intentionally
    unaffected.)"""
    table = ("investment_transactions" if entry.mapped.is_investment
             else "transactions")
    cur = conn.execute(
        f"SELECT amount, account_id FROM {table} WHERE id=?",
        (int(matched_txn_id),)
    ).fetchone()
    if cur is None:
        raise KeyError("no transaction %s" % matched_txn_id)
    got, want = int(cur["amount"]), int(entry.mapped.amount_cents)
    if (got < 0) != (want < 0):
        raise ValueError(
            "manual match rejected: candidate amount %s is the opposite sign to "
            "the downloaded amount %s (signed cents); a payment is never the "
            "same event as a deposit" % (got, want))
    if abs(abs(got) - abs(want)) > abs(want) * SCHED_AMOUNT_TOLERANCE:
        raise ValueError(
            "manual match rejected: candidate amount %s is more than %d%% from "
            "the downloaded amount %s (signed cents)"
            % (got, round(SCHED_AMOUNT_TOLERANCE * 100), want))
    # One register line is one event: a line another review row already claims
    # is RELEASED first. Hand-matching onto a taken line used to leave both rows
    # pointing at it, and the second then reconciled a transaction that was
    # already spoken for.
    release_txn_matches(conn, int(cur["account_id"]), int(matched_txn_id),
                        except_review_id=entry.review_id)
    entry.label = LABEL_MATCHING
    entry.matched_txn_id = int(matched_txn_id)
    entry.match_method = "manual"
    if entry.review_id is not None:
        _set_state(conn, entry.review_id, "pending",
                   label=LABEL_MATCHING, matched_txn_id=int(matched_txn_id),
                   match_method="manual")
        conn.commit()


# ---------------------------------------------------------------------------
# Amazon invoice itemization (SRD 6.7 / requirements A6, A8)
# ---------------------------------------------------------------------------
# Loading a time-tagged Amazon invoice JSON turns each order into a review row
# for the CARD register: the per-item split proposed by
# :mod:`mammon.importers.amazon_items` (one leg per item, tax and any gift-card /
# reward-point offset spread PROPORTIONALLY so the legs net to the charged amount
# in exact integer cents), classified NEW when no card charge matches it yet, or
# MATCHING when the charge is already an accepted register line -- in which case
# accepting REPLACES that line's splits/categories rather than adding a duplicate.
#
# Nothing here is persisted: Amazon invoice data is NEVER stored (requirement A6).
# The file is re-loadable across sessions, and re-loading after an accept simply
# re-classifies the now-existing charge as MATCHING. The review lives only in
# memory (:attr:`ReviewEntry.amazon_alloc`); ``persist_entries`` is never called
# for it, and no table records the invoice. This module stays the SOLE writer of
# the review flow and :mod:`ledger` the SOLE writer of transaction rows -- Amazon
# accepts funnel through :func:`ledger.add_transaction` / :func:`ledger.set_splits`,
# adding no second write path.

#: Payee written onto an Amazon-sourced charge and used (via LIKE) to gate a
#: match: only an already-Amazon register line may absorb an invoice's splits, so
#: an unrelated same-day, same-amount charge is never silently itemized.
AMAZON_PAYEE = "Amazon"
AMAZON_PAYEE_LIKE = "%amazon%"


def _amazon_default_category(conn) -> str:
    """The default item-leg category (requirement A1): 'household', then the
    user's own past-Amazon categories, most-used first. Ordering only -- it never
    overrides a learned rule and proposes nothing the data has not shown."""
    from mammon.importers import amazon_items
    order = amazon_items.default_category_order(
        amazon_items.amazon_categories_from_history(conn))
    return order[0] if order else ""


def _order_memo(order) -> str:
    """A one-line human summary of an order's items for the review row's memo."""
    items = [str(i).strip() for i in (getattr(order, "items", None) or [])
             if str(i).strip()]
    if not items:
        return "Amazon order"
    head = "; ".join(items[:3])
    if len(items) > 3:
        head += " (+%d more)" % (len(items) - 3)
    return head


def _find_amazon_match(conn, account_id, date_iso, amount_cents, window_days,
                       claimed):
    """The id of an already-accepted Amazon card charge this order reconciles
    against, or ``None``.

    Mirrors :func:`_find_match`'s date+amount tier -- same signed cents, within
    +/- ``window_days`` of the order date, closest date first -- and ADDS the
    payee gate requirement A8 asks for: the register line must already read as
    Amazon (``payee LIKE '%amazon%'``). A line already claimed by an earlier
    order in this same load is skipped, and a whole-transaction transfer is
    excluded (a transfer cannot be split)."""
    if not date_iso:
        return None
    lo = record.iso_shift(date_iso, -window_days)
    hi = record.iso_shift(date_iso, window_days)
    rows = conn.execute(
        "SELECT id FROM transactions "
        "WHERE account_id=? AND amount=? AND date BETWEEN ? AND ? "
        "AND transfer_account_id IS NULL "
        "AND payee IS NOT NULL AND LOWER(payee) LIKE ? "
        "ORDER BY ABS(julianday(date) - julianday(?)), id",
        (account_id, int(amount_cents), lo, hi, AMAZON_PAYEE_LIKE.lower(),
         date_iso),
    ).fetchall()
    for row in rows:
        if int(row[0]) not in claimed:
            return int(row[0])
    return None


def build_amazon_review(conn, account_id, path, *, card_number="",
                        window_days=DEFAULT_WINDOW_DAYS):
    """Classify a time-tagged Amazon invoice file into in-memory review rows.

    Reads the Downloads file at ``path`` on demand (nothing is stored --
    requirement A6), turns each order that reached the card into one review row
    for ``account_id``, and classifies it NEW or MATCHING an already-accepted
    Amazon charge (requirement A8, via :func:`_find_amazon_match`). Each returned
    :class:`ReviewEntry` carries its proposed per-item split (``amazon_alloc``);
    accepting it later writes those legs through :func:`accept_amazon_new` /
    :func:`accept_amazon_match`.

    Orders that never hit the card (fully covered by a gift-card balance, so
    ``charged_cents == 0``) and refunds (out of scope -- requirement A7) create
    no card review row. A single order is treated as ONE charge equal to its
    invoice Grand Total; splitting one order across several shipment charges is a
    later phase (:func:`amazon_items.allocate_charges`)."""
    from mammon.importers import amazon_items
    orders = amazon_items.load_invoice_orders(path, card_number)
    default_cat = _amazon_default_category(conn)
    claimed: set = set()
    entries: list = []
    for order in orders:
        charged = int(getattr(order, "charged_cents", 0) or 0)
        if charged <= 0:
            continue                    # zero-charge / non-card order: nothing to reconcile
        d = getattr(order, "date", None)
        date_iso = d.isoformat() if d is not None else ""
        if not date_iso:
            continue                    # unmatchable and uncreatable without a date
        alloc = amazon_items.allocate_order(order, default_category=default_cat)
        amount_cents = int(alloc.charge_cents)     # signed debit, == -charged
        mapped = MappedRow(
            date=date_iso,
            amount_cents=amount_cents,
            payee=AMAZON_PAYEE,
            memo=_order_memo(order),
            raw={"amazon_order_number":
                 str(getattr(order, "order_number", "") or "")},
        )
        matched = _find_amazon_match(conn, account_id, date_iso, amount_cents,
                                     window_days, claimed)
        if matched is not None:
            claimed.add(matched)
            entries.append(ReviewEntry(
                mapped=mapped, label=LABEL_MATCHING, matched_txn_id=matched,
                match_method="date+amount", amazon_alloc=alloc))
        else:
            entries.append(ReviewEntry(
                mapped=mapped, label=LABEL_NEW, amazon_alloc=alloc))
    return entries


def _amazon_split_lines(conn, alloc, category_ids=None) -> list:
    """The :func:`ledger.set_splits` lines for an allocation: one dict per leg,
    ``{category_id, amount, memo}``.

    Each leg's category NAME (item legs default to 'household'; the offset legs
    are 'gift cards' / 'reward points' -- CATEGORIES, never accounts, per
    requirement A3/A5) is get-or-created through
    :func:`resolve_or_create_category`, the single category writer.
    ``category_ids`` optionally overrides per-leg category ids (a future per-item
    picker) positionally; ``None`` in a slot falls back to the leg's own category
    name. Amounts are the allocator's exact signed cents, passed through
    untouched, so the legs still sum to the charge to the cent."""
    ids = list(category_ids or [])
    lines: list = []
    for i, leg in enumerate(alloc.legs):
        cid = ids[i] if i < len(ids) and ids[i] is not None else \
            resolve_or_create_category(conn, getattr(leg, "category", "") or "")
        lines.append({
            "category_id": cid,
            "amount": int(leg.amount_cents),
            "memo": (getattr(leg, "memo", "") or None),
        })
    return lines


def accept_amazon_new(conn, account_id, entry, *, category_ids=None) -> int:
    """Post an Amazon NEW review row as a real card charge with its item splits.

    Creates the charge through :func:`ledger.add_transaction` (payee 'Amazon',
    the invoice's item summary as memo) and, when the order has more than one
    leg, writes the per-item split through :func:`ledger.set_splits` -- the legs
    already sum to the charge in exact cents, so nothing is fabricated. A lone-leg
    order (one item, no offset) collapses to a plain categorised transaction,
    since a split needs two legs. Returns the new txn id."""
    if entry.amazon_alloc is None:
        raise ValueError("entry carries no Amazon allocation")
    m = entry.mapped
    lines = _amazon_split_lines(conn, entry.amazon_alloc, category_ids)
    if len(lines) <= 1:
        cid = lines[0]["category_id"] if lines else None
        return ledger.add_transaction(
            conn, account_id, m.date, int(m.amount_cents),
            payee=AMAZON_PAYEE, memo=(m.memo or None), category_id=cid)
    txn_id = ledger.add_transaction(
        conn, account_id, m.date, int(m.amount_cents),
        payee=AMAZON_PAYEE, memo=(m.memo or None))
    ledger.set_splits(conn, txn_id, lines)
    return txn_id


def accept_amazon_match(conn, entry, *, category_ids=None) -> int:
    """Itemize the EXISTING card charge an Amazon MATCHING row points at.

    Replaces that register line's splits/categories with the invoice's per-item
    split (requirement A8) rather than inserting a duplicate -- the charge, its
    date and its amount are the user's already-accepted line and are left
    untouched; only the split legs change (plus the payee, if it was blank).
    Returns the matched txn id."""
    txn_id = entry.matched_txn_id
    if txn_id is None:
        raise ValueError("entry is not a MATCHING row")
    if entry.amazon_alloc is None:
        raise ValueError("entry carries no Amazon allocation")
    lines = _amazon_split_lines(conn, entry.amazon_alloc, category_ids)
    if len(lines) <= 1:
        cid = lines[0]["category_id"] if lines else None
        ledger.update_transaction(conn, txn_id, category_id=cid)
    else:
        ledger.set_splits(conn, txn_id, lines)
    # Name a bank line that still reads as bare statement gobbledygook, but never
    # overwrite a payee the user already chose.
    cur = conn.execute("SELECT payee FROM transactions WHERE id=?",
                       (txn_id,)).fetchone()
    if cur is not None and not (cur[0] or "").strip():
        ledger.update_transaction(conn, txn_id, payee=AMAZON_PAYEE)
    return txn_id
