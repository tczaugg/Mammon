"""Generic tabular-import engine for downloaded/scraped data (CSV today, JSON-ish
future). OFX and QIF are EXEMPT and keep their own parsers -- this engine only
ever runs for delimited/tabular sources, and everything it produces still lands
in the import REVIEW queue.

Why this exists
---------------
:mod:`mammon.importers.csvimp` maps a bank CSV by trying known header names. That
works for statements whose first line is the header and whose columns use common
words ("Date", "Amount", "Description"). It FAILS for real downloads whose
structure it cannot read: a Venmo statement prefaces the data with two title
rows and trails a multi-line legal disclaimer, its money column is
``Amount (total)`` with ``+ $``/``- $`` text, and its counterparty is split
across ``From``/``To`` by direction. Fed to a plain ``DictReader`` that treats
line 1 as the header, such a file imports as nothing -- or, once a stray column
lines up, as blank rows (a date but no payee/amount). This module fixes that:

* **Structure detection** -- scan for the real header (rows above are preamble,
  trailing non-conforming rows are a footer) and drop blank rows, honouring
  quoted multi-line fields and a sniffed delimiter/encoding/BOM.
* **Role inference by value, not just name** -- date; amount as EITHER a single
  signed column OR separate debit/credit; payee (its own column, a directional
  ``From``/``To`` pair, or concatenated out of a description); memo/description.
* **Profiles** -- a source is fingerprinted by its header signature. On the first
  import of a new/changed format the caller shows a PREVIEW and the user ACCEPTs
  or runs a WIZARD; the resulting column map is saved as a profile so later
  imports of that format need no prompt. See :func:`plan_tabular`,
  :func:`apply_wizard_answers`, :func:`accept_profile`.

The engine is deliberately free of Qt and of the ledger; it converges on
:class:`mammon.importers.record.NormalizedTxn` like every other parser and is
unit-tested headlessly. :mod:`mammon.importers.csvimp` calls into it -- known-alias
files keep their legacy path unchanged; only headers the aliases don't recognise
reach the inference path here.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from mammon.importers.record import NormalizedTxn, dollars_to_cents, parse_date

# ---------------------------------------------------------------------------
# Header aliases (case-insensitive, matched in priority order). Single source of
# truth for BOTH the legacy csvimp path and the inference path here.
# ---------------------------------------------------------------------------
DATE_COLS = ("Date", "Posted", "Posted Date", "Post Date", "Transaction Date",
             "Trade Date", "Run Date", "Posting Date", "Due Date")
PAYEE_COLS = ("Payee", "Description", "statementDescription", "Name", "Merchant",
              "To/From", "To", "From", "Memo")
AMOUNT_COLS = ("Amount", "transactionAmount", "Value", "Amount ($)", "Net Amount")
DEBIT_COLS = ("Debit", "Withdrawal", "Withdrawals", "Charge", "Debits")
CREDIT_COLS = ("Credit", "Deposit", "Deposits", "Payment", "Payments", "Credits")
MEMO_COLS = ("Memo", "Notes", "Note", "Description")
CHECK_COLS = ("Check", "Check Number", "Check #")
FITID_COLS = ("FITID", "Reference", "Transaction ID", "ID")
STATUS_COLS = ("Status", "Transaction Status")
TYPE_COLS = ("Type", "Transaction Type")
CATEGORY_COLS = ("Category",)

# Status values that mean the transaction has settled (-> cleared=1).
_CLEARED_STATUS = {"posted", "cleared", "settled", "paid", "processed", "complete"}

# Month names for the flexible date parser (real downloads write "June 26, 2026").
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"]) if m}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHNAME_RE = re.compile(
    r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")   # June 26, 2026
_DAYMONTH_RE = re.compile(
    r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})$")   # 26 June 2026


# ---------------------------------------------------------------------------
# robust scalar parsers
# ---------------------------------------------------------------------------
def parse_money(value) -> int:
    """Signed integer cents from a human money cell, extending
    :func:`dollars_to_cents` with trailing/leading ``CR``/``DR`` markers (CR is a
    credit -> positive, DR a debit -> negative). ``$``, thousands commas, spaces,
    a leading/trailing sign, ``+ $``/``- $``, and ``()`` negatives are handled by
    the delegate. Blank/unparseable -> 0 (never raises -- a junk cell must not
    abort a whole import)."""
    if value is None:
        return 0
    t = str(value).strip()
    if not t:
        return 0
    neg = None
    up = t.upper()
    if up.endswith("CR"):
        t, neg = t[:-2].strip(), False
    elif up.endswith("DR"):
        t, neg = t[:-2].strip(), True
    elif up.startswith("CR "):
        t, neg = t[3:].strip(), False
    elif up.startswith("DR "):
        t, neg = t[3:].strip(), True
    try:
        cents = dollars_to_cents(t)
    except ValueError:
        return 0
    if neg is True:
        return -abs(cents)
    if neg is False:
        return abs(cents)
    return cents


def parse_date_flex(value) -> Optional[str]:
    """Parse a date to ISO ``YYYY-MM-DD`` or return ``None`` (never raises).

    Delegates to :func:`mammon.importers.record.parse_date` (ISO, T-datetimes,
    OFX-compact, US ``M/D/Y``) and adds spelled-out month names
    ("June 26, 2026", "26 Jun 2026"). Unambiguous EU order (``D/M/Y`` where the
    day exceeds 12, which US order would misread as an impossible month) is
    accepted too; genuinely ambiguous ``D/M`` vs ``M/D`` is left to the wizard."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        iso = parse_date(s)
        y, mo, d = (int(x) for x in iso.split("-"))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return iso
        # US read produced an impossible month/day -> retry as EU D/M/Y.
        if mo > 12 and d <= 12:
            return f"{y:04d}-{d:02d}-{mo:02d}"
    except ValueError:
        pass
    m = _MONTHNAME_RE.match(s) or _DAYMONTH_RE.match(s)
    if m:
        if m.re is _MONTHNAME_RE:
            name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
        else:
            day, name, year = int(m.group(1)), m.group(2), int(m.group(3))
        mo = _MONTHS.get(name.lower())
        if mo and 1 <= day <= 31:
            return f"{year:04d}-{mo:02d}-{day:02d}"
    return None


def _looks_date(s) -> bool:
    return parse_date_flex(s) is not None


_MONEY_CORE = re.compile(r"\d+\.\d+|\.\d+|\d+")


def _looks_money(s) -> bool:
    """A value the amount sniffer treats as money. Requires a decimal point or a
    money marker, or a SHORT bare integer -- so a long reference/account number
    (all digits) is NOT mistaken for a dollar amount."""
    if s is None:
        return False
    t = str(s).strip()
    if not t:
        return False
    up = t.upper()
    marked = False
    if up.endswith(("CR", "DR")):
        t, marked = t[:-2].strip(), True
    core = t.replace("$", "").replace(",", "").replace(" ", "")
    if core.startswith("(") and core.endswith(")"):
        core, marked = core[1:-1], True
    if core[:1] in "+-":
        core, marked = core[1:], True
    if not core:
        return False
    if re.fullmatch(r"\d+\.\d+", core) or re.fullmatch(r"\.\d+", core):
        return True
    if re.fullmatch(r"\d+", core):
        return marked or len(core) <= 9      # bare long integer -> ref number, not money
    return False


_URL_RE = re.compile(r"^(https?://|/|www\.)")


def _looks_url(s) -> bool:
    return bool(_URL_RE.match(str(s).strip()))


# ---------------------------------------------------------------------------
# decode / delimiter / grid
# ---------------------------------------------------------------------------
def _strip_bom_blanklines(text: str) -> str:
    text = text.lstrip("﻿")
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def sniff_delimiter(text: str) -> str:
    """Detect the field delimiter among ``, ; \\t |``. Prefer csv.Sniffer, but
    fall back to the busiest candidate on the sample so a scrape whose quoting
    confuses the Sniffer still parses."""
    sample = "\n".join(text.splitlines()[:60])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        best, best_n = ",", 0
        for line in text.splitlines()[:60]:
            for d in (",", ";", "\t", "|"):
                n = line.count(d)
                if n > best_n:
                    best, best_n = d, n
        return best


def read_grid(text: str) -> list[list[str]]:
    """Parse ``text`` into a list of records (each a list of trimmed cells),
    honouring quoted multi-line fields and the sniffed delimiter."""
    text = _strip_bom_blanklines(text)
    if not text.strip():
        return []
    delim = sniff_delimiter(text)
    grid: list[list[str]] = []
    for rec in csv.reader(io.StringIO(text), delimiter=delim):
        grid.append([(c or "").strip() for c in rec])
    return grid


@dataclass
class Frame:
    """The structure-detected shape of a tabular source."""
    header: list[str]
    rows: list[dict]              # one dict per data record: header-name -> cell
    header_index: int
    preamble_rows: int
    footer_rows: int
    dropped_blank: int


def _nonempty(rec) -> list[str]:
    return [c for c in rec if str(c).strip()]


def locate_and_frame(text: str) -> Optional[Frame]:
    """Detect header row, drop preamble/footer/blank rows, and build per-row dicts.

    The data block is the span of records that carry a parseable date; the header
    is the nearest non-blank row above the first dated record that has >= 2
    non-empty cells (rows above it are preamble, rows after the last dated record
    are footer). When data begins at the very top with no header above it,
    synthetic ``col0..`` names are used so a header-less export still imports.
    Returns ``None`` when nothing date-like is present (e.g. a non-financial
    scrape) so the caller yields zero rows rather than blank ones."""
    grid = read_grid(text)
    if not grid:
        return None
    dated = [i for i, rec in enumerate(grid) if any(_looks_date(c) for c in rec)]
    if not dated:
        return None
    first, last = dated[0], dated[-1]

    header_index = None
    for i in range(first - 1, -1, -1):
        if len(_nonempty(grid[i])) >= 2:
            header_index = i
            break

    if header_index is None:
        width = max(len(grid[i]) for i in range(first, last + 1))
        header = [f"col{i}" for i in range(width)]
        data_start = first
        preamble = first          # rows before data (all skipped)
    else:
        header = [h.strip() for h in grid[header_index]]
        data_start = header_index + 1
        preamble = header_index

    rows: list[dict] = []
    dropped = 0
    for rec in grid[data_start:last + 1]:
        if not _nonempty(rec):
            dropped += 1
            continue
        row: dict = {}
        for i, name in enumerate(header):
            if not name:
                continue
            row[name] = rec[i].strip() if i < len(rec) else ""   # last dup wins, ragged -> ""
        rows.append(row)
    footer = len(grid) - (last + 1)
    return Frame(header=header, rows=rows, header_index=(header_index or 0),
                 preamble_rows=preamble, footer_rows=footer, dropped_blank=dropped)


# ---------------------------------------------------------------------------
# column roles + inference
# ---------------------------------------------------------------------------
@dataclass
class CashRoles:
    """Which source column feeds each ledger field for a cash tabular source.

    Amount is EITHER ``amount`` (a single signed column) OR the ``debit``/
    ``credit`` pair. Payee is EITHER ``payee`` (one column), the directional
    ``payee_from``/``payee_to`` pair (From when money comes in, To when it goes
    out -- a Venmo/PayPal statement), or -- when neither is set -- the joined
    ``description`` columns (payee-inside-description). ``description`` columns
    are concatenated into the memo regardless."""
    date: Optional[str] = None
    amount: Optional[str] = None
    debit: Optional[str] = None
    credit: Optional[str] = None
    invert_amount: bool = False          # source's positive means money OUT
    debit_positive: bool = False         # debit column is already money-out signed
    payee: Optional[str] = None
    payee_from: Optional[str] = None
    payee_to: Optional[str] = None
    description: list = field(default_factory=list)
    category: Optional[str] = None
    check: Optional[str] = None
    fitid: Optional[str] = None
    status: Optional[str] = None
    type: Optional[str] = None


def _pick(header, aliases) -> Optional[str]:
    """First header (in ``aliases`` priority order) present in ``header``,
    case-insensitively; returns the ACTUAL header string so the cell can be read."""
    lut = {}
    for h in header:
        lut.setdefault(str(h).strip().lower(), h)
    for a in aliases:
        h = lut.get(a.lower())
        if h is not None:
            return h
    return None


def _column_values(rows, name) -> list[str]:
    return [str(r.get(name, "")).strip() for r in rows]


def _sniff_date_col(header, rows, used) -> Optional[str]:
    best, best_score = None, 0
    for h in header:
        if not h or h in used:
            continue
        vals = [v for v in _column_values(rows, h) if v]
        if not vals:
            continue
        ok = sum(1 for v in vals if _looks_date(v))
        if ok >= max(1, 0.6 * len(vals)) and ok > best_score:
            best, best_score = h, ok
    return best


_AMOUNT_EXCLUDE = ("balance", "fee", "tax", "tip", "rate", "price", "spot", "quantity")


def _sniff_amount_col(header, rows, used) -> Optional[str]:
    best, best_score = None, -1
    for h in header:
        if not h or h in used:
            continue
        vals = [v for v in _column_values(rows, h) if v]
        if not vals:
            continue
        money = [v for v in vals if _looks_money(v)]
        if len(money) < max(1, 0.6 * len(vals)):
            continue
        name = h.lower()
        if any(x in name for x in _AMOUNT_EXCLUDE):
            continue
        score = sum(1 for v in money if parse_money(v) != 0)
        if any(x in name for x in ("amount", "total", "debit", "credit",
                                   "withdraw", "deposit", "charge", "payment")):
            score += 1000
        if score > best_score:
            best, best_score = h, score
    return best


def _sniff_payee_col(header, rows, used) -> Optional[str]:
    best, best_score = None, -1
    for h in header:
        if not h or h in used:
            continue
        vals = [v for v in _column_values(rows, h) if v]
        if not vals:
            continue
        if sum(1 for v in vals if _looks_money(v) or _looks_date(v) or _looks_url(v)) \
                >= 0.6 * len(vals):
            continue
        name = h.lower()
        avg_len = sum(len(v) for v in vals) / len(vals)
        score = avg_len + len(set(vals))
        if any(x in name for x in ("payee", "name", "merchant", "description",
                                   "note", "memo", "counterparty", "to", "from")):
            score += 10_000
        if score > best_score:
            best, best_score = h, score
    return best


def infer_cash_roles(header, rows) -> CashRoles:
    """Infer a :class:`CashRoles` for a cash tabular source: known header names
    first (so a conventional bank CSV maps exactly as before), then value-sniffing
    fills any role a name didn't -- the path a Venmo-style export needs."""
    r = CashRoles()
    r.date = _pick(header, DATE_COLS)
    r.amount = _pick(header, AMOUNT_COLS)
    r.debit = _pick(header, DEBIT_COLS)
    r.credit = _pick(header, CREDIT_COLS)
    frm, to = _pick(header, ("From",)), _pick(header, ("To",))
    if frm and to:
        r.payee_from, r.payee_to = frm, to
    else:
        r.payee = _pick(header, PAYEE_COLS)
    memo = _pick(header, MEMO_COLS)
    if memo:
        r.description = [memo]
    r.category = _pick(header, CATEGORY_COLS)
    r.check = _pick(header, CHECK_COLS)
    r.fitid = _pick(header, FITID_COLS)
    r.status = _pick(header, STATUS_COLS)
    r.type = _pick(header, TYPE_COLS)

    used = {x for x in (r.date, r.amount, r.debit, r.credit, r.payee, r.payee_from,
                        r.payee_to, r.category, r.check, r.fitid, r.status,
                        r.type, *r.description) if x}
    if not r.date:
        r.date = _sniff_date_col(header, rows, used)
        used.add(r.date)
    if not (r.amount or r.debit or r.credit):
        r.amount = _sniff_amount_col(header, rows, used)
        used.add(r.amount)
    if not (r.payee or r.payee_from or r.payee_to or r.description):
        p = _sniff_payee_col(header, rows, used)
        if p:
            r.payee = p
    return r


# ---------------------------------------------------------------------------
# record building
# ---------------------------------------------------------------------------
def _get(row, name) -> str:
    return "" if not name else str(row.get(name, "")).strip()


def build_cash_records(frame: Frame, roles: CashRoles,
                       default_account: Optional[str]) -> list[NormalizedTxn]:
    """Apply ``roles`` to a framed cash source, dropping rows with no date and
    rows that carry no signal at all (no amount AND no payee/memo/check -- the
    "blank row" a mis-read structure used to leak)."""
    out: list[NormalizedTxn] = []
    for row in frame.rows:
        iso = parse_date_flex(_get(row, roles.date))
        if iso is None:
            continue
        if roles.amount:
            cents = parse_money(_get(row, roles.amount))
            if roles.invert_amount:
                cents = -cents
        else:
            cents = 0
            d, c = _get(row, roles.debit), _get(row, roles.credit)
            if d:
                cents += parse_money(d) if roles.debit_positive else -abs(parse_money(d))
            if c:
                cents += abs(parse_money(c))

        if roles.payee_from or roles.payee_to:
            frm, to = _get(row, roles.payee_from), _get(row, roles.payee_to)
            payee = (frm if cents > 0 else to) or frm or to
        elif roles.payee:
            payee = _get(row, roles.payee)
        else:
            payee = " ".join(p for p in (_get(row, c) for c in roles.description) if p)

        memo = " ".join(p for p in (_get(row, c) for c in roles.description) if p)
        check = _get(row, roles.check)
        typ = _get(row, roles.type)
        if not payee:
            payee = typ                      # transfers/adjustments name themselves
        status = _get(row, roles.status).lower()

        if cents == 0 and not payee and not memo and not check:
            continue                          # blank row -- do not leak into review

        out.append(NormalizedTxn(
            external_account=default_account or "",
            date=iso,
            amount_cents=cents,
            payee=payee,
            memo=memo,
            category=_get(row, roles.category),
            check_number=check,
            fitid=_get(row, roles.fitid),
            type=typ,
            cleared=1 if status in _CLEARED_STATUS else 0,
        ))
    return out


# ---------------------------------------------------------------------------
# fingerprint + profile persistence
# ---------------------------------------------------------------------------
def fingerprint(header) -> str:
    """Stable signature of a source's header (its non-empty column names, order
    preserved, lower-cased). Two exports of the same format share a signature so a
    saved profile applies; a changed layout gets a new one and re-prompts."""
    names = [str(h).strip().lower() for h in header if str(h).strip()]
    return hashlib.sha1("|".join(names).encode("utf-8")).hexdigest()


def _roles_to_json(roles: CashRoles) -> str:
    return json.dumps(asdict(roles), sort_keys=True)


def _roles_from_json(text: str) -> CashRoles:
    data = json.loads(text)
    r = CashRoles()
    for k, v in data.items():
        if hasattr(r, k):
            setattr(r, k, v)
    if r.description is None:
        r.description = []
    return r


def save_profile(conn, signature: str, name: str, roles: CashRoles,
                 account_type: str = "checking") -> None:
    """Persist (or replace) a column-map profile keyed by header signature."""
    conn.execute(
        "INSERT INTO import_profiles(signature, name, config) VALUES(?,?,?) "
        "ON CONFLICT(signature) DO UPDATE SET name=excluded.name, config=excluded.config",
        (signature, name, json.dumps(
            {"roles": json.loads(_roles_to_json(roles)), "account_type": account_type})),
    )
    conn.commit()


def get_profile(conn, signature: str) -> Optional[dict]:
    """Return ``{"name", "roles": CashRoles, "account_type"}`` for a saved profile,
    or ``None``. Tolerant of a DB without the table (older file) -> ``None``."""
    try:
        row = conn.execute(
            "SELECT name, config FROM import_profiles WHERE signature=?", (signature,)
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    cfg = json.loads(row["config"] if not isinstance(row, tuple) else row[1])
    name = row["name"] if not isinstance(row, tuple) else row[0]
    return {"name": name,
            "roles": _roles_from_json(json.dumps(cfg.get("roles", {}))),
            "account_type": cfg.get("account_type", "checking")}


# ---------------------------------------------------------------------------
# preview / wizard / accept  (headless orchestration the UI drives)
# ---------------------------------------------------------------------------
@dataclass
class TabularPlan:
    signature: str
    is_new: bool                     # True until a matching profile is saved
    header: list
    roles: CashRoles
    records: list                    # list[NormalizedTxn], interpreted with roles
    preview: list                    # first rows as {date, amount_cents, payee, memo}
    preamble_rows: int
    footer_rows: int
    dropped_blank: int
    account_type: str = "checking"
    name: str = ""
    roles_authoritative: bool = False   # user-confirmed map, not a guess
    _text: str = ""
    _default_account: Optional[str] = None


def _preview_rows(records, limit=12) -> list[dict]:
    return [{"date": r.date, "amount_cents": r.amount_cents,
             "payee": r.payee, "memo": r.memo} for r in records[:limit]]


def plan_tabular(conn, text: str, default_account: Optional[str] = None,
                 account_type: str = "checking") -> Optional[TabularPlan]:
    """Interpret ``text`` and describe the planned import.

    Looks up a saved profile by the source's header signature. If found, its
    column map is used and ``is_new`` is False (import silently). If not, roles
    are inferred, ``is_new`` is True, and the caller shows ``preview`` so the user
    can ACCEPT (:func:`accept_profile`) or refine via
    :func:`apply_wizard_answers`. ``conn`` may be ``None`` (no persistence, always
    ``is_new``). Returns ``None`` for a source with no detectable table."""
    frame = locate_and_frame(text)
    if frame is None:
        return None
    sig = fingerprint(frame.header)
    saved = get_profile(conn, sig) if conn is not None else None
    if saved:
        roles, account_type, name, is_new = (
            saved["roles"], saved["account_type"], saved["name"], False)
    else:
        roles, name, is_new = infer_cash_roles(frame.header, frame.rows), "", True
    # Records come from parse_csv so investment/NAV routing stays single-sourced;
    # `roles` only steers the cash branch (a known-alias/investment file ignores it).
    from mammon.importers.csvimp import parse_csv
    authoritative = bool(saved)      # a saved profile was confirmed by the user
    records = parse_csv(text, default_account=default_account, roles=roles,
                        roles_authoritative=authoritative)
    return TabularPlan(
        signature=sig, is_new=is_new, header=frame.header, roles=roles,
        records=records, preview=_preview_rows(records),
        preamble_rows=frame.preamble_rows, footer_rows=frame.footer_rows,
        dropped_blank=frame.dropped_blank, account_type=account_type, name=name,
        roles_authoritative=authoritative,
        _text=text, _default_account=default_account)


def apply_wizard_answers(plan: TabularPlan, answers: dict) -> TabularPlan:
    """Re-interpret ``plan`` with the user's wizard choices and return an updated
    plan (still ``is_new`` until accepted) -- the caller loops preview->wizard
    until the preview is right.

    Recognised keys (all optional; unset falls back to the current roles):
    ``date_col``; ``amount_mode`` ("signed"|"debit_credit"); ``amount_col``,
    ``debit_col``, ``credit_col``; ``invert_amount``, ``debit_positive``;
    ``payee_mode`` ("column"|"from_to"|"in_description"); ``payee_col``,
    ``payee_from_col``, ``payee_to_col``; ``description_cols`` (list);
    ``account_type``."""
    r = plan.roles
    new = CashRoles(**{**asdict(r), "description": list(r.description)})
    if "date_col" in answers:
        new.date = answers["date_col"]
    mode = answers.get("amount_mode")
    if mode == "signed":
        new.amount = answers.get("amount_col", new.amount)
        new.debit = new.credit = None
    elif mode == "debit_credit":
        new.debit = answers.get("debit_col", new.debit)
        new.credit = answers.get("credit_col", new.credit)
        new.amount = None
    elif "amount_col" in answers:
        new.amount = answers["amount_col"]
    if "invert_amount" in answers:
        new.invert_amount = bool(answers["invert_amount"])
    if "debit_positive" in answers:
        new.debit_positive = bool(answers["debit_positive"])
    pmode = answers.get("payee_mode")
    if pmode == "column":
        new.payee = answers.get("payee_col", new.payee)
        new.payee_from = new.payee_to = None
    elif pmode == "from_to":
        new.payee_from = answers.get("payee_from_col", new.payee_from)
        new.payee_to = answers.get("payee_to_col", new.payee_to)
        new.payee = None
    elif pmode == "in_description":
        new.payee = new.payee_from = new.payee_to = None
    elif "payee_col" in answers:
        new.payee = answers["payee_col"]
    if "description_cols" in answers:
        new.description = list(answers["description_cols"])
    account_type = answers.get("account_type", plan.account_type)

    from mammon.importers.csvimp import parse_csv
    records = parse_csv(plan._text, default_account=plan._default_account,
                        roles=new, roles_authoritative=True)
    frame = locate_and_frame(plan._text)
    return TabularPlan(
        signature=plan.signature, is_new=True, header=plan.header, roles=new,
        records=records, preview=_preview_rows(records),
        preamble_rows=frame.preamble_rows if frame else 0,
        footer_rows=frame.footer_rows if frame else 0,
        dropped_blank=frame.dropped_blank if frame else 0,
        account_type=account_type, name=plan.name,
        roles_authoritative=True,
        _text=plan._text, _default_account=plan._default_account)


def accept_profile(conn, plan: TabularPlan, name: Optional[str] = None) -> str:
    """Save ``plan``'s column map as a profile so this format imports with no
    prompt next time. Returns the signature."""
    save_profile(conn, plan.signature, name or plan.name or "Imported format",
                 plan.roles, plan.account_type)
    return plan.signature
