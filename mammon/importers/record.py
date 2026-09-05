"""Normalized import record + small parsing helpers shared by every importer.

Every parser (QIF/OFX/JSON/CSV) converges on :class:`NormalizedTxn` (SRD 6.4),
and the shared insert path in :mod:`mammon.importers.core` consumes only that.
Keeping the parsers ignorant of the database, and the core ignorant of file
formats, is what lets each side be tested in isolation.

Money is signed integer cents everywhere (negative = money out). Share
quantities and prices stay as Decimal-precision TEXT strings.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import List, Optional


@dataclass
class NormalizedTxn:
    """One transaction as produced by a parser, before it hits the ledger.

    A cash transaction uses the top block. A transfer additionally sets
    ``transfer_account`` (the OTHER account's name). An investment transaction
    sets ``action`` (Buy/Sell/Div/...) plus the security fields.
    """

    external_account: str = ""          # account name/id this row belongs to
    date: str = ""                      # ISO YYYY-MM-DD
    amount_cents: int = 0               # signed; negative = money out
    payee: str = ""
    memo: str = ""
    category: str = ""                  # "Parent:Child"; empty for transfers
    check_number: str = ""
    fitid: str = ""                     # source-unique id, for exact dedup
    type: str = ""                      # source's own type hint (informational)
    cleared: int = 0
    reconciled: int = 0
    account_type: str = "checking"      # used only when auto-creating the account

    # transfer
    transfer_account: str = ""          # counter-account name -> mirror transfer

    # investment extension
    action: str = ""                    # Buy|Sell|Div|ReinvDiv|IntInc|XIn|XOut|...
    symbol: str = ""
    quantity: str = ""                  # Decimal text
    price: str = ""                     # Decimal text
    commission_cents: int = 0
    # A stock split's EXACT ratio (new : old). OFX states it as
    # NUMERATOR/DENOMINATOR, and 4:3 has no exact decimal form, so the pair is
    # carried whole rather than pre-divided (see investments.apply_split).
    split_num: int = 0
    split_den: int = 0

    # cash splits: list of (category, amount_cents, memo)
    splits: list = field(default_factory=list)

    @property
    def is_investment(self) -> bool:
        return bool(self.action)

    @property
    def is_transfer(self) -> bool:
        return bool(self.transfer_account.strip())


@dataclass
class ImportResult:
    """Outcome of an import run (also persisted to the ``imports`` table)."""

    import_id: Optional[int] = None
    added: int = 0
    duplicates: int = 0
    errors: int = 0
    transfers: int = 0
    investments: int = 0
    matched: int = 0        # imports that posted into a pending scheduled pre-entry
    # reconcile mismatches: an import landed near a pre-entry but with a
    # different amount (a rate/escrow change). Surfaced for user-confirmed
    # auto-fix (loans_schedule.apply_payment_change); each is a PaymentChange.
    payment_changes: List = field(default_factory=list)
    # OFX investment action types found in the source that this importer does not
    # map to a ledger effect -- surfaced by the post-import audit so an unhandled
    # action is visible instead of silently dropped (and corrupting holdings).
    unmapped_actions: List[str] = field(default_factory=list)
    # Broker <INVPOS> holdings-snapshot mismatches: the reported share count or a
    # conflicting on-file price differed from what transactions imply. Surfaced for
    # review rather than silently overwriting the computed holdings. Each is a dict
    # {symbol, date, field, computed, reported} from investments.reconcile_positions.
    position_discrepancies: List = field(default_factory=list)

    def summary(self) -> str:
        chg = (f", {len(self.payment_changes)} payment-change"
               if self.payment_changes else "")
        unmapped = (f", {len(self.unmapped_actions)} unmapped-action"
                    if self.unmapped_actions else "")
        disc = (f", {len(self.position_discrepancies)} position-discrepancy"
                if self.position_discrepancies else "")
        return (
            f"import#{self.import_id}: +{self.added} added "
            f"({self.transfers} transfers, {self.investments} investments, "
            f"{self.matched} matched{chg}{unmapped}{disc}), "
            f"{self.duplicates} dup, {self.errors} err"
        )


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------
_CENTS = Decimal("1")


def dollars_to_cents(value) -> int:
    """Parse a human money value (dollars) to signed integer cents.

    Handles ``$``, thousands commas, a leading/trailing sign, and accounting
    parentheses for negatives. ``""``/``None`` -> 0. Numeric inputs are treated
    as DOLLARS (use the ``amount_cents`` field directly if you already have
    cents).
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):  # guard: bool is an int subclass
        return 0
    s = str(value).strip()
    if not s:
        return 0
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    # Quicken can write a split's balancing amount as "$=746.36"; the leading
    # "=" is a QIF marker, not part of the number (see the split example in the
    # QIF spec). Strip it so a spec-conformant file does not crash the import.
    s = s.replace("$", "").replace(",", "").replace(" ", "").lstrip("=")
    if s in ("", "-", "+"):
        return 0
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"cannot parse money value {value!r}")
    cents = int((d * 100).quantize(_CENTS, rounding=ROUND_HALF_UP))
    return -cents if neg else cents


def parse_date(value) -> str:
    """Parse the many source date shapes into ISO ``YYYY-MM-DD``.

    Accepts ISO already, OFX compact ``YYYYMMDD[HHMMSS...]``, and QIF/CSV US
    ``M/D/YY``, ``MM/DD/YYYY`` including Quicken's ``MM/DD'YY`` apostrophe-year.
    """
    if value is None:
        raise ValueError("empty date")
    s = str(value).strip()
    if not s:
        raise ValueError("empty date")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    # ISO-8601 datetime with fractional seconds and/or timezone offset, e.g.
    # "2026-07-31T00:00:00.000-06:00", "...Z", or a space separator. We only
    # keep the calendar day the source names, dropping the time/zone entirely
    # (a bank posting date is date-only). Try datetime.fromisoformat first for
    # correctness, then fall back to the leading date so odd fractional-second
    # widths or 'Z' on older interpreters still parse.
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]", s):
        iso = (s[:-1] + "+00:00") if s.endswith(("Z", "z")) else s
        try:
            dt = _dt.datetime.fromisoformat(iso)
            return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        except ValueError:
            head = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
            if head:
                y, mo, d = (int(x) for x in head.groups())
                return f"{y:04d}-{mo:02d}-{d:02d}"
    # OFX compact: 8 digits (optionally followed by time / tz), year first.
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    if m and re.fullmatch(r"\d{8}(\d{6})?(\.\d+)?(\[[^\]]*\])?", s):
        y, mo, d = (int(x) for x in m.groups())
        if 1900 <= y <= 2200:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # QIF / CSV US order, "'" is Quicken's 2000s year separator.
    t = s.replace("'", "/").replace(" ", "")
    parts = re.split(r"[/.\-]", t)
    if len(parts) == 3 and all(parts):
        mo, d, y = (int(p) for p in parts)
        if y < 100:
            y += 2000 if y < 70 else 1900
        return f"{y:04d}-{mo:02d}-{d:02d}"
    raise ValueError(f"unrecognized date {value!r}")


def iso_shift(iso_date: str, days: int) -> str:
    d = _dt.date.fromisoformat(iso_date) + _dt.timedelta(days=days)
    return d.isoformat()


def normalize_payee(payee: str) -> str:
    """A loose canonical form for fuzzy matching (case, spacing, trailing
    store/reference numbers removed). Not stored on the txn -- the original
    payee text is preserved; this is only a comparison key."""
    if not payee:
        return ""
    s = payee.upper().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[#*]\s*\d+.*$", "", s).strip()      # "SAFEWAY #123" -> "SAFEWAY"
    s = re.sub(r"\b\d{4,}\b", "", s).strip()          # long ref numbers
    return re.sub(r"\s+", " ", s).strip()


def identity_key(account_id, date, amount_cents, payee, memo) -> tuple:
    """The content identity that makes two transactions "the same" for dedup.

    A transaction is identified by its ACCOUNT, posting DATE (same day), signed
    AMOUNT (cents), and normalized PAYEE and MEMO/description. It deliberately
    EXCLUDES the source id (``fitid``) and any row order: the user legitimately makes
    several identical purchases on one day, and those share a single key.

    Dedup is therefore count-aware and multiset: for one key, if the register
    already holds R matching rows and an import carries I, only the surplus
    ``max(0, I - R)`` is inserted -- N identical siblings in a single batch are
    NEVER collapsed against each other (they only ever dedup against what was
    ALREADY stored). This function is the single source of truth for that key; the
    register-match predicates (:func:`mammon.importers.core._find_dup_cash`,
    :func:`mammon.import_review._find_match`) additionally allow a small date window
    and fuzzy payee so a bank download still reconciles against a hand-entered row,
    but they consume each existing register row at most once so the count stays
    exact."""
    return (
        int(account_id),
        date or "",
        int(amount_cents),
        normalize_payee(payee or ""),
        re.sub(r"\s+", " ", (memo or "").strip().upper()),
    )


def clean_category(raw: str) -> str:
    """Strip a QIF class suffix ('Category:Sub/Class' -> 'Category:Sub') and
    surrounding whitespace. A transfer '[Account]' must be handled by the caller
    before this."""
    if not raw:
        return ""
    return raw.split("/", 1)[0].strip()


def decimal_text(value) -> str:
    """Normalize a share quantity / price to a clean Decimal string, or ''."""
    if value is None or value == "":
        return ""
    s = str(value).strip().replace(",", "").replace("$", "")
    if not s:
        return ""
    try:
        return str(Decimal(s))
    except InvalidOperation:
        return ""


def _to_decimal(value):
    """Parse a numeric/money-ish string to Decimal, or ``None`` when blank/unparseable."""
    if value is None or value == "":
        return None
    s = str(value).strip().replace(",", "").replace("$", "").replace(" ", "")
    if s in ("", "-", "+"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _trim6(d: Decimal) -> str:
    """A DERIVED share/price Decimal as fixed-point text, quantized to 6 dp and
    stripped of trailing zeros (40.000000 -> '40', never '4E+1' from normalize())."""
    d = d.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def derive_investment_amounts(shares, price, total):
    """Reconcile an investment row's (shares, price-per-share, total) triple.

    Accepts any TWO of the three (as raw strings/numbers) and derives the third via
    ``shares * price = total`` (magnitudes; a per-share price is inherently
    non-negative). When all three are present the given ``total`` is kept -- its
    sign is the signed cash amount -- and only a consistency flag is computed, with
    rounding tolerated to about a penny per share. Returns
    ``(quantity_text, price_text, total_cents, consistent)`` where ``quantity_text``
    / ``price_text`` are '' and ``total_cents`` is ``None`` when neither read nor
    derivable. GIVEN shares/price keep their source text verbatim; only a DERIVED
    value is quantized to 6 dp (so a repeating division like 3.05 / 0.102 does not
    carry 28 digits).
    """
    q = _to_decimal(shares)
    p = _to_decimal(price)
    t = _to_decimal(total)   # dollars, signed as given
    q_text = decimal_text(shares)   # '' when shares is blank/unparseable
    p_text = decimal_text(price)
    consistent = True
    if q is not None and p is not None:
        product = q * p
        if t is None:
            t = product
        else:
            tol = (abs(q) * Decimal("0.01")) + Decimal("0.01")
            if abs(abs(t) - abs(product)) > tol:
                consistent = False
    elif q is not None and t is not None and q != 0:
        p_text = _trim6(abs(t) / abs(q))
    elif p is not None and t is not None and p != 0:
        q_text = _trim6(abs(t) / p)
    total_cents = None if t is None else int((t * 100).quantize(_CENTS, rounding=ROUND_HALF_UP))
    return q_text, p_text, total_cents, consistent


# ---------------------------------------------------------------------------
# Sign conventions (inferred per file, never assumed)
# ---------------------------------------------------------------------------
# The ledger stores investment quantities and amounts as MAGNITUDES and lets the
# ACTION carry the direction: ShrsOut always removes, ShrsIn always adds. Measured
# across the real ledger, Buy is 5075/0 positive quantities, Sell 1126/0, amounts
# gross-positive throughout.
#
# Sources disagree about this, and they disagree per source: one plan's export
# writes a fee as "-0.144 shares / -$3.04" (direction in BOTH the sign and the
# activity text), another writes "0.144 / $3.04" (direction in the text alone).
# Neither is wrong, so the convention is DETECTED from the file rather than
# assumed -- hardcoding "fees come in negative" is exactly the per-institution
# tailoring that has to stay out of these parsers.
#
# A signed quantity is not merely redundant when the action already says the
# direction -- it INVERTS the transaction. ShrsOut of -0.144 shares subtracts a
# negative and ADDS 0.144 shares to the holding, silently, on accept.
def _direction_of(action) -> int:
    """+1 if ``action`` adds shares, -1 if it removes them, 0 if it says nothing.

    Sourced from :mod:`mammon.investments` (imported lazily, so a parser stays a
    pure function that costs nothing to import) rather than restated here, so the
    two cannot drift into disagreeing about what ShrsOut means.
    """
    from mammon import investments

    a = str(action or "").strip().lower().replace(" ", "")
    if a in investments._ADD_ACTIONS:
        return 1
    if a in investments._REMOVE_ACTIONS:
        return -1
    return 0


def detect_sign_convention(records) -> str:
    """``"signed"``, ``"magnitude"`` or ``"unknown"`` for a batch of records.

    ``signed`` -- the file also encodes direction in the number: removals came in
    negative. ``magnitude`` -- every direction-bearing row is positive, so the
    action alone carries it. ``unknown`` -- nothing to judge by (no row whose
    action states a direction).
    """
    neg = pos = 0
    for r in records:
        d = _direction_of(getattr(r, "action", ""))
        q = _to_decimal(getattr(r, "quantity", "") or "")
        if not d or q is None or q == 0:
            continue
        if q < 0:
            neg += 1
        else:
            pos += 1
    if not neg and not pos:
        return "unknown"
    return "signed" if neg else "magnitude"


def normalize_investment_signs(records) -> dict:
    """Bring a batch of parsed records onto the ledger's sign convention.

    A record whose action states a direction has its quantity and amount stored
    as MAGNITUDES -- whatever the file's convention, the meaning survives in the
    action. A record whose action states NO direction is left exactly as parsed:
    there the sign is the only direction signal there is, and flattening it would
    destroy the one thing that says which way the shares moved. Guessing an
    action from the sign is not this layer's job either -- review is where an
    unmapped activity type gets resolved.

    Returns ``{"convention", "normalized", "conflicts"}``. A conflict is a row
    whose sign CONTRADICTS its action in a file that is otherwise unsigned (a
    negative Buy in a magnitude file) -- that is evidence the action mapping is
    wrong, not that the number is, so it is reported rather than quietly flipped.
    """
    convention = detect_sign_convention(records)
    normalized = 0
    conflicts: list = []
    for r in records:
        d = _direction_of(getattr(r, "action", ""))
        if not d:
            continue
        q = _to_decimal(r.quantity or "")
        signed_q = q is not None and q < 0
        if convention == "magnitude" and signed_q:
            conflicts.append(r)
        changed = False
        if signed_q:
            r.quantity = decimal_text(abs(q))
            changed = True
        if r.amount_cents and r.amount_cents < 0:
            r.amount_cents = abs(r.amount_cents)
            changed = True
        if changed:
            normalized += 1
    return {"convention": convention, "normalized": normalized,
            "conflicts": conflicts}


def parse_price_text(value) -> str:
    """Normalize a QIF security price to a clean Decimal string, or ''.

    Handles plain decimals ('25.93'), a '$'/comma prefix, and the LEGACY
    fractional stock quotes Quicken exports for pre-decimalization dates
    ('22 3/4' -> '22.75', '1/2' -> '0.5'). Returns '' when the value cannot be
    parsed so one malformed price line never aborts a 20k-row price import."""
    if value is None:
        return ""
    s = str(value).strip().replace("$", "").replace(",", "")
    if not s:
        return ""
    if "/" in s:                       # legacy fraction: "whole num/den" or "num/den"
        parts = s.split()
        try:
            if len(parts) == 2:
                num, den = parts[1].split("/")
                val = Decimal(parts[0]) + Decimal(num) / Decimal(den)
            else:
                num, den = parts[0].split("/")
                val = Decimal(num) / Decimal(den)
        except (InvalidOperation, ValueError, ZeroDivisionError):
            return ""
        return str(val)
    try:
        return str(Decimal(s))
    except InvalidOperation:
        return ""


# ---------------------------------------------------------------------------
# Security names
# ---------------------------------------------------------------------------
_HTML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
                  "&apos;": "'", "&#38;": "&", "&nbsp;": " "}


def normalize_security_name(name) -> str:
    """Repair a security name's ENCODING. Does not interpret it.

    The line this draws matters. A CSV that writes ``S&amp;P 500 EQUITY INDEX``
    has a broken escape, not a different fund -- repairing it is reading the file
    correctly, and no user should be asked to adjudicate an ampersand. Whitespace
    is the same kind of defect.

    What this deliberately does NOT do is decide that ``TARGET 2030 FUND(TDLB)``
    means the same fund as ``TARGET 2030 FUND``. That is a judgement about
    SECURITY IDENTITY, and no rule can make it reliably: investment firms rename
    funds often, and a rename like "TARGET 2030" -> "TARGET RETIREMENT 2030"
    defeats any normalization while a parenthetical can equally mark a genuinely
    different share class. Quicken asks the user whether an unrecognised security
    is new or matches an existing one, and that is the right owner for the
    decision -- see the security matching step in the import review.
    """
    text = str(name or "").strip()
    if not text:
        return ""
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    return " ".join(text.split())
