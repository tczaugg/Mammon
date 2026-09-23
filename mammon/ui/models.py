"""Qt table models for the Mammon register and accounts overview.

These sit STRICTLY on :mod:`mammon.ledger` (no direct SQL), so the domain
invariants -- Quicken mirror transfers, running balances, net worth -- live in
one tested place and the GUI is a thin projection over them.

Money is integer cents in the ledger; the register shows it split across two
columns: a negative amount renders under Payment, a positive one under Deposit.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from PyQt5.QtCore import (QAbstractTableModel, QDate, QModelIndex, Qt, QTimer,
                          pyqtSignal)
from PyQt5.QtGui import QBrush, QColor

from mammon import (categorize, category_tree, crypto, import_review,
                    investments, ledger, undo)
from mammon.ui import style


_WARNING_ICON = None


def warning_triangle_icon():
    """The app's ONE warning mark: a small amber triangle with an exclamation.

    Drawn in exactly one place so every use is the same shape in the same color.
    Two callers, and they assert the same thing -- "this number is not the whole
    story":

      * the register's Category cell, before '--Split--', when a split still
        holds an uncategorized remainder (money the user has yet to assign);
      * the account bar's Net Worth strip, IN PLACE OF the total, when a foreign
        balance has no FX rate and so cannot be folded in (SRD 5.4a).

    Built lazily and cached -- a QPixmap needs a live QApplication, so it cannot
    be a module constant. Named without a leading underscore because widgets.py
    uses it too; it was ``_uncategorized_warning_icon`` while the register was its
    only caller, and that name would now misdescribe half of its uses."""
    global _WARNING_ICON
    if _WARNING_ICON is None:
        from PyQt5.QtGui import QIcon, QPixmap, QPainter, QPolygonF, QPen
        from PyQt5.QtCore import QPointF
        pm = QPixmap(16, 16)
        pm.fill(QColor(0, 0, 0, 0))                 # transparent background
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        tri = QPolygonF([QPointF(8, 1.5), QPointF(15, 14.5), QPointF(1, 14.5)])
        p.setPen(QPen(QColor("#8a6d00"), 1))
        p.setBrush(QColor("#f2c200"))               # warning yellow
        p.drawPolygon(tri)
        p.setPen(QPen(QColor("#3a2f00"), 1.6))
        p.drawLine(QPointF(8, 6), QPointF(8, 10))   # exclamation stroke
        p.drawPoint(QPointF(8, 12))                 # exclamation dot
        p.end()
        _WARNING_ICON = QIcon(pm)
    return _WARNING_ICON


# ---------------------------------------------------------------------------
# money helpers
# ---------------------------------------------------------------------------
def parse_amount(text) -> int:
    """Parse a user-typed dollar string to signed integer cents.

    Tolerates ``$``, thousands commas, a leading ``-`` and accountant-style
    ``(...)`` negatives. Blank or unparseable input yields 0 (the UI never
    raises on a stray keystroke)."""
    s = "" if text is None else str(text).strip()
    if not s:
        return 0
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").strip()
    if s.startswith("-"):
        neg, s = True, s[1:].strip()
    if not s:
        return 0
    try:
        cents = int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0
    return -cents if neg else cents


def fmt_cents(cents) -> str:
    """Integer cents -> ``1,234.56`` with thousands separators (no currency
    symbol). Used for register amounts and per-account balances."""
    if cents is None:
        return ""
    c = int(cents)
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100:,}.{c % 100:02d}"


# Common ISO 4217 currencies -> the glyph shown before the amount. Anything not
# listed renders with its bare code as a prefix (e.g. ``SEK 1,234.56``), so a
# never-mapped currency is still LABELLED rather than silently shown as dollars.
# This is display formatting only -- no conversion or arithmetic happens here
# (that is :mod:`mammon.fx`'s job); the UI stays a thin projection.
_CURRENCY_SYMBOLS = {
    "USD": "$", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "HKD": "HK$",
    "SGD": "S$", "EUR": "€", "GBP": "£", "JPY": "¥",
    "CNY": "CN¥", "CHF": "CHF ", "INR": "₹", "KRW": "₩",
    "MXN": "MX$", "BRL": "R$", "RUB": "₽", "ZAR": "R", "SEK": "SEK ",
    "NOK": "NOK ", "DKK": "DKK ", "PLN": "zł", "ILS": "₪",
}


def currency_symbol(code) -> str:
    """The prefix glyph for an ISO 4217 code (``$`` for USD or a blank/None code).
    An unmapped code returns itself with a trailing space so the amount is still
    labelled (e.g. ``THB 500.00``). Presentation only -- no money math."""
    if not code:
        return "$"
    code = str(code).strip().upper()
    return _CURRENCY_SYMBOLS.get(code, f"{code} ")


def fmt_money(cents, symbol: bool = True, currency=None) -> str:
    """Integer cents -> ``$1,234.56`` (leading symbol, sign before the symbol:
    ``-$5.00``). Used for category subtotals and net worth. ``symbol=False``
    falls back to :func:`fmt_cents`. ``currency`` chooses the leading glyph
    (default ``$`` for USD); the value is still rendered straight from integer
    cents -- NO conversion happens here."""
    if cents is None:
        return ""
    if not symbol:
        return fmt_cents(cents)
    c = int(cents)
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{currency_symbol(currency)}{c // 100:,}.{c % 100:02d}"


def fmt_amount_ccy(cents, currency=None) -> str:
    """A per-account amount in the account's OWN currency. The base currency (or a
    blank one) renders bare like :func:`fmt_cents` -- the dense classic look the
    all-USD ledger has always had -- while a FOREIGN currency is tagged with its
    symbol/code so a non-base account's balance is never mistaken for base
    dollars. Presentation only; the cents are unconverted."""
    from mammon import fx
    code = str(currency).strip().upper() if currency else ""
    if not code or code == fx.BASE_CURRENCY:
        return fmt_cents(cents)
    return fmt_money(cents, currency=code)


def fmt_date(iso, fmt: str | None = None) -> str:
    """A stored/parsed ISO ``YYYY-MM-DD`` date -> the user's chosen DISPLAY
    format. ``fmt`` is one of :data:`mammon.ui.prefs.DATE_FORMATS`; when ``None``
    (the normal case) the persisted :func:`mammon.ui.prefs.date_format`
    preference is read (default US ``MM/DD/YYYY``). This is the SINGLE display
    chokepoint -- every register/review/report date renders through it, so
    changing the preference re-renders them all. Anything that is not a
    well-formed ISO date passes through unchanged (the register never hides a
    value it cannot parse)."""
    s = str(iso or "").strip()
    parts = s.split("-")
    if len(parts) == 3 and all(parts) and len(parts[0]) == 4:
        y, m, d = parts
        if fmt is None:
            from mammon.ui import prefs  # lazy: avoid import cycle at module load
            fmt = prefs.date_format()
        if fmt == "DD/MM/YYYY":
            return f"{d}/{m}/{y}"
        if fmt == "YYYY-MM-DD":
            return f"{y}-{m}-{d}"
        return f"{m}/{d}/{y}"  # MM/DD/YYYY -- the default
    return s


def fmt_qty(value) -> str:
    """Decimal-text share quantity or per-share price for display. The store
    keeps these as exponent-free Decimal strings (so no float error creeps into
    share math), so show them verbatim; ``None``/blank renders empty. Used by
    the investment register's Quantity and Price columns."""
    return "" if value in (None, "") else str(value)


# Display format -> the Qt format string that renders and PARSES it. Every date
# editor takes its format from here, so one preference drives them all instead of
# each widget hardcoding an answer (they used to disagree five different ways).
_QT_DATE_FORMATS = {
    "MM/DD/YYYY": "MM/dd/yyyy",
    "DD/MM/YYYY": "dd/MM/yyyy",
    "YYYY-MM-DD": "yyyy-MM-dd",
}


def qt_date_format(fmt: str | None = None) -> str:
    """The Qt display/parse format for the user's chosen date format."""
    if fmt is None:
        from mammon.ui import prefs      # lazy: avoid import cycle at module load
        fmt = prefs.date_format()
    return _QT_DATE_FORMATS.get(fmt, "MM/dd/yyyy")


def parse_date(value, fmt: str | None = None) -> str:
    """Anything the user might type (or a QDate) -> ISO ``YYYY-MM-DD``, or "".

    The counterpart to :func:`fmt_date`: that one is the single DISPLAY
    chokepoint, this is the single PARSE chokepoint. Storage, the ledger's
    validators and every download script speak ISO, so the conversion has to
    happen once, here, rather than at each of the places a date can be typed.

    ISO is always accepted -- it is the storage format and what scripts hand
    back. Beyond that a two-slash date is genuinely ambiguous (03/04 is March 4th
    or April 3rd depending on where you live), so the USER'S chosen format
    decides, which is the whole point of having the preference. Separators may be
    ``/``, ``-`` or ``.``; a two-digit year maps into 1970-2069, the same window
    the QIF importer uses. Anything unparseable returns "" rather than a guess.
    """
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd") if value.isValid() else ""
    text = str(value or "").strip()
    if not text:
        return ""

    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        y, m, d = (int(g) for g in iso.groups())
        return _iso_or_blank(y, m, d)

    parts = re.fullmatch(r"(\d{1,4})[/.-](\d{1,2})[/.-](\d{1,4})", text)
    if not parts:
        return ""
    a, b, c = (int(g) for g in parts.groups())
    if fmt is None:
        from mammon.ui import prefs
        fmt = prefs.date_format()
    if len(parts.group(1)) == 4:            # 2026/03/04 -- year first, unambiguous
        y, m, d = a, b, c
    elif fmt == "DD/MM/YYYY":
        d, m, y = a, b, c
    else:
        m, d, y = a, b, c
    return _iso_or_blank(y, m, d)


def _iso_or_blank(y: int, m: int, d: int) -> str:
    """Format y/m/d as ISO, or "" when it is not a real calendar date."""
    if y < 100:                             # two-digit year: 70-99 -> 19xx, else 20xx
        y += 1900 if y >= 70 else 2000
    try:
        return _dt.date(y, m, d).isoformat()
    except ValueError:
        return ""


def _to_iso(value) -> str:
    """A QDate (from a date editor) or typed text -> ISO 'YYYY-MM-DD'.

    Unparseable input passes through UNCHANGED so the ledger's own validator
    reports it, rather than this silently blanking a date the user typed."""
    iso = parse_date(value)
    if iso:
        return iso
    return "" if isinstance(value, QDate) else str(value or "").strip()


# ---------------------------------------------------------------------------
# register model
# ---------------------------------------------------------------------------
@dataclass
class RegisterFilter:
    """What the register's filter bar narrows the rows to. An empty field is
    no constraint; :meth:`is_empty` means the whole filter is a no-op, in which
    case the model shows every row and reports no filter at all.

    ``text`` is a case-insensitive substring over payee, memo, num, tag, the
    category label AND the formatted amount, so typing ``520`` finds the
    520.00 charge as readily as typing its payee. Amount bounds are absolute
    cents (a payment and a deposit of the same size both fall inside a range).
    ``clr`` reads the register's own glyphs: ``cleared`` is the ``c`` state
    alone, ``reconciled`` the ``R`` state, ``uncleared`` neither."""

    text: str = ""
    date_from: str = ""            # ISO, inclusive
    date_to: str = ""              # ISO, inclusive
    amount_min: int | None = None  # absolute cents
    amount_max: int | None = None
    clr: str = "any"               # any | uncleared | cleared | reconciled

    def is_empty(self) -> bool:
        return not (self.text.strip() or self.date_from or self.date_to
                    or self.amount_min is not None or self.amount_max is not None
                    or self.clr != "any")

    def matches(self, r: dict) -> bool:
        needle = self.text.strip().lower()
        if needle:
            hay = " | ".join(
                str(r.get(k) or "")
                for k in ("payee", "memo", "num", "tag", "category_label"))
            hay = f"{hay} | {fmt_cents(abs(int(r['amount'])))}".lower()
            if needle not in hay:
                return False
        if self.date_from and r["date"] < self.date_from:
            return False
        if self.date_to and r["date"] > self.date_to:
            return False
        magnitude = abs(int(r["amount"]))
        if self.amount_min is not None and magnitude < self.amount_min:
            return False
        if self.amount_max is not None and magnitude > self.amount_max:
            return False
        if self.clr == "uncleared" and (r["cleared"] or r["reconciled"]):
            return False
        if self.clr == "cleared" and not (r["cleared"] and not r["reconciled"]):
            return False
        if self.clr == "reconciled" and not r["reconciled"]:
            return False
        return True


class RegisterModel(QAbstractTableModel):
    """One account's ledger as a table with the classic Quicken columns plus a
    blank quick-entry row at the bottom."""

    # Quicken column order: Clr sits BETWEEN Payment and Deposit; Tag follows
    # Category (Category/Tag are Quicken's paired classification).
    DATE, NUM, PAYEE, CATEGORY, TAG, MEMO, PAYMENT, CLR, DEPOSIT, BALANCE = range(10)
    HEADERS = ["Date", "Num", "Payee", "Category", "Tag", "Memo",
               "Payment", "Clr", "Deposit", "Balance"]

    # Custom role: in two-line ("2-line") mode the Payee cell's SECOND line
    # carries the transaction's classification (category / memo / tag). The
    # PayeeTwoLineDelegate reads it; every other cell returns "".
    SECOND_LINE_ROLE = Qt.UserRole + 1

    # Custom role on the Tag cell: the (name, color) chips to paint -- the row's
    # own tags plus its split legs' tags (deduped), each mapped to its identity
    # color (None when uncolored). The TagDelegate (one-line) and the two-line
    # payee tag zone both read it; every other cell returns [].
    TAG_COLORS_ROLE = Qt.UserRole + 2

    committed = pyqtSignal()          # a write hit the DB; refresh siblings
    error = pyqtSignal(str)           # a write failed; surface to the user
    # A write that genuinely ALTERED or CREATED a transaction, carrying its id.
    # NOT off `committed`, which also fires for a no-op rewrite (see _write).
    #
    # The id matters because the register saves PER FIELD: editing an existing
    # row commits each cell as its editor closes, so tabbing across four fields
    # is four writes to one transaction. The listener coalesces on the id so the
    # user hears one confirmation per TRANSACTION, which is the unit they think
    # in, rather than one per keystroke-worth of storage.
    transactionSaved = pyqtSignal(int)

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        # Session-scoped undo/redo for this register. Records the inverse of each
        # edit and replays it THROUGH the ledger (no second write path); see
        # mammon/undo.py. Ctrl+Z / Ctrl+Y are wired to model.undo()/redo().
        self.undo_stack = undo.UndoManager(conn)
        self._rows: list[dict] = []
        # Tag identity colors (casefolded name -> #rrggbb) and split-leg tag names
        # by parent txn id, both refreshed on reload so the Tag cell can paint a
        # colored square per tag (the row's own tags unioned with its legs').
        self._tag_colors: dict[str, str] = {}
        self._leg_tags: dict[int, list[str]] = {}
        self._new: dict = {}          # blank quick-entry buffer
        # A not-yet-accepted "pending" review row: an editable register row a
        # NEW import-review item opens, holding {"entry": ReviewEntry, "buf":
        # {field->str}} until it is accepted (persisted) or cleared. None when
        # no review NEW row is being edited. It sits just below the real rows,
        # above the blank quick-entry row.
        self._pending: dict | None = None
        self._transfer_targets: dict[str, int] = {}
        # The rows the VIEW shows: _rows (register order: date, then amount high
        # to low, then insertion -- ledger.register_rows)
        # projected through the current sort and filter. Every row-indexed
        # method reads _view; only reload() and _project() touch _rows. The
        # dicts are shared, so an edit through a sorted/filtered row reaches
        # the same transaction. Balance stays each row's date-ordered running
        # value whatever the sort, as Quicken does -- sorting by payee never
        # recomputes a balance.
        self._view: list[dict] = []
        self._sort: tuple[int, int] = (self.DATE, Qt.AscendingOrder)
        self._filter: RegisterFilter | None = None
        self.two_line = False         # Quicken one-line vs two-line register view
        # Liability/loan accounts label the money columns Increase/Decrease (Task
        # 51) and, when the account carries loan parameters, render each payment's
        # principal/interest/escrow split. Recomputed on every reload.
        self.account_type = ""
        self._is_liability = False
        self._is_loan = False
        self._loan_leg_cache: dict[int, list] = {}
        self.reload()

    # ---- view mode (one-line <-> two-line) -------------------------------
    def set_two_line(self, on: bool) -> None:
        """Toggle Quicken's two-line register projection. This only governs the
        Payee cell's SECOND_LINE_ROLE content (the delegate paints it); the row
        set, editing, and every other column are unchanged, so the switch is a
        pure presentation change."""
        on = bool(on)
        if on == self.two_line:
            return
        self.two_line = on
        # The Payee header gains/loses its second-line labels; repaint it.
        self.headerDataChanged.emit(Qt.Horizontal, self.PAYEE, self.PAYEE)
        if self._view:
            top = self.index(0, self.PAYEE)
            bottom = self.index(len(self._view) - 1, self.PAYEE)
            self.dataChanged.emit(top, bottom, [self.SECOND_LINE_ROLE])

    def _second_line_text(self, row) -> str:
        """The Payee cell's second line: category, then memo, then #tag -- the
        classification fields that move under the payee in two-line mode. Only
        the present ones show (blank fields are skipped)."""
        r = self._view[row]
        parts = []
        if r["category_label"]:
            parts.append(r["category_label"])
        if r["memo"]:
            parts.append(r["memo"])
        if r["tag"]:
            parts.append(f"#{r['tag']}")
        return "    ".join(parts)

    # ---- data plumbing ----------------------------------------------------
    def reload(self):
        self.beginResetModel()
        self._rows = ledger.register_rows(self.conn, self.account_id)
        self._tag_colors = ledger.tag_colors(self.conn)
        self._leg_tags = ledger.split_leg_tags_by_txn(self.conn, self.account_id)
        self._view = self._project()
        self._new = {}
        # QuickFill's pre-entered fields for the blank row (see quickfill_fields):
        # kept apart from _new so a pre-entered amount can never trip the
        # "date + money typed" auto-commit, and so what the user typed always
        # shows over what was guessed.
        self._auto = {}
        self._payee_choices = None
        self._loan_leg_cache = {}
        acct = ledger.get_account(self.conn, self.account_id)
        self.account_type = (acct["type"] if acct else "") or ""
        self._is_liability = self.account_type == "liability"
        # A loan account is a liability that has stored loan parameters (Task 50).
        self._is_loan = False
        if self._is_liability:
            from mammon import loans
            self._is_loan = loans.get_loan_params(self.conn, self.account_id) is not None
        self.endResetModel()

    # ---- sort + filter (the view's projection of the ledger rows) ---------
    def sort(self, column, order=Qt.AscendingOrder):   # QAbstractItemModel API
        self.set_sort(column, order)

    def set_sort(self, column, order=Qt.AscendingOrder) -> None:
        """Order the displayed rows by ``column``. Date ascending is the ledger
        order itself (ledger.register_rows), so it is the identity projection;
        Date descending is its exact reverse. Ties in any other column fall
        back to ledger order, so the result is stable across reloads."""
        column = int(column)
        order = Qt.DescendingOrder if order == Qt.DescendingOrder else Qt.AscendingOrder
        if (column, order) == self._sort:
            return
        self._sort = (column, order)
        self._reproject()

    def sort_state(self) -> tuple[int, int]:
        return self._sort

    def set_filter(self, flt: RegisterFilter | None) -> None:
        """Narrow the displayed rows to those ``flt`` matches (``None`` or an
        empty filter shows everything). The blank quick-entry row stays, so
        entry keeps working while filtered; a new row that does not match the
        filter simply is not shown, as in Quicken."""
        if flt is not None and flt.is_empty():
            flt = None
        if flt == self._filter:
            return
        self._filter = flt
        self._reproject()

    def filter_state(self) -> RegisterFilter | None:
        return self._filter

    def view_counts(self) -> tuple[int, int]:
        """``(shown, total)`` real rows -- the filter bar's 'Showing n of m'."""
        return len(self._view), len(self._rows)

    def _reproject(self) -> None:
        self.beginResetModel()
        self._view = self._project()
        self.endResetModel()

    def _project(self) -> list[dict]:
        rows = self._rows
        flt = self._filter
        if flt is not None and not flt.is_empty():
            rows = [r for r in rows if flt.matches(r)]
        column, order = self._sort
        if column == self.DATE:
            return list(rows) if order == Qt.AscendingOrder else list(reversed(rows))
        return sorted(rows, key=self._sort_key(column),
                      reverse=(order == Qt.DescendingOrder))

    def _sort_key(self, column):
        """A total order for one column, ledger order breaking ties. Num sorts
        numerically when it is a number, then text, blanks last; the money
        columns sort by their own magnitude (a row with nothing in that column
        counts as 0); Clr ranks blank < c < R."""
        def text(key):
            return lambda r: (r.get(key) or "").lower()

        def num_key(r):
            n = (r.get("num") or "").strip()
            if not n:
                return (2, 0, "")
            return (0, int(n), "") if n.isdigit() else (1, 0, n.lower())

        keys = {
            self.NUM: num_key,
            self.PAYEE: text("payee"),
            self.CATEGORY: text("category_label"),
            self.TAG: text("tag"),
            self.MEMO: text("memo"),
            self.PAYMENT: lambda r: max(-int(r["amount"]), 0),
            self.DEPOSIT: lambda r: max(int(r["amount"]), 0),
            self.CLR: lambda r: 2 if r["reconciled"] else (1 if r["cleared"] else 0),
            self.BALANCE: lambda r: int(r["balance"]),
        }
        primary = keys.get(column, lambda r: r["date"])
        return lambda r: (primary(r), r["date"], r["id"])

    # ---- liability / loan register (Increase/Decrease + payment split) ----
    def uses_increase_decrease(self) -> bool:
        """True for a liability/loan account, whose money columns are labelled
        Increase (balance owed goes up) / Decrease (goes down) instead of
        Payment/Deposit. The widget reads this to size the Category column."""
        return self._is_liability

    def is_loan(self) -> bool:
        """True for a liability account that has stored loan parameters (Task 50)
        -- the register reads this to offer the principal-projection chart."""
        return self._is_loan

    def loan_payment_legs(self, row) -> list:
        """The principal / interest / escrow legs of a loan-payment row as
        ``[(label, cents), ...]`` -- from the STORED split if one exists (e.g. an
        import expanded by Task 52), else computed with :func:`loans.payment_split`
        so interest and escrow are visible even when no split was stored. Empty
        for anything that is not a loan payment (a non-loan account, a charge/draw,
        or a payment too small to carry a positive principal). Memoized per reload."""
        r = self.txn_at(row)
        if r is None:
            return []
        tid = r["id"]
        if tid in self._loan_leg_cache:
            return self._loan_leg_cache[tid]
        legs = self._compute_loan_legs(r)
        self._loan_leg_cache[tid] = legs
        return legs

    def _compute_loan_legs(self, r) -> list:
        if not self._is_loan or r["amount"] <= 0:   # only a Decrease (payment)
            return []
        stored = ledger.get_splits(self.conn, r["id"])
        if stored:
            return [(s["category_label"] or "Uncategorized", int(s["amount"]))
                    for s in stored]
        # A bare transfer with no split of its own is an ADDITIONAL PRINCIPAL
        # payment (money moved in from checking): it is all principal, so it must
        # render as the normal transfer ([Account]) -- NOT be force-split into
        # interest/escrow/principal. The amortization engine still credits it as
        # principal and shortens the term (loans._extra_principal_payments).
        if r["transfer_account_id"] is not None:
            return []
        from mammon import loans
        try:
            split = loans.payment_split(self.conn, self.account_id, r["date"], r["amount"])
        except LookupError:
            return []
        if split.principal <= 0:
            return []
        legs = []
        if split.interest:
            legs.append(("Interest", split.interest))
        for ex in split.extras:
            if ex.amount:
                legs.append((ex.category, ex.amount))
        legs.append(("Principal", split.principal))
        return legs

    def _loan_split_label(self, row) -> str:
        """A one-line 'Label 1,234.56   Label 78.90 ...' summary of a loan
        payment's split, or '' when the row is not a loan payment."""
        legs = self.loan_payment_legs(row)
        if not legs:
            return ""
        return "   ".join(f"{label} {fmt_cents(amt)}" for label, amt in legs)

    def account_name(self) -> str:
        acct = ledger.get_account(self.conn, self.account_id)
        return acct["name"] if acct else ""

    def account_currency(self) -> str:
        """This register's account native currency ('USD' when unset -- the base).
        Read through :mod:`mammon.fx` so the normalization lives in the domain
        layer, not here."""
        from mammon import fx
        return fx.get_account_currency(self.conn, self.account_id)

    def is_scheduled_row(self, row) -> bool:
        """True for a pending pre-entry (``scheduled=1``)."""
        txn = self.txn_at(row)
        return bool(txn and txn.get("scheduled"))

    def post_row(self, row) -> bool:
        """Enter Pending Payment: make a pre-entry real as it stands (its
        transfer mirror with it)."""
        return self._write(lambda t: ledger.set_scheduled(self.conn, t["id"], False), row)

    def current_balance(self) -> int:
        return ledger.account_balance(self.conn, self.account_id)

    def txn_at(self, row):
        return self._view[row] if 0 <= row < len(self._view) else None

    def is_blank_row(self, row) -> bool:
        # The blank quick-entry row is always last; a pending review row (when
        # present) sits between the real rows and it.
        return row == len(self._view) + (1 if self._pending is not None else 0)

    # ---- pending review row (an editable, not-yet-accepted NEW import row) --
    def has_pending(self) -> bool:
        return self._pending is not None

    def pending_row(self) -> int:
        """Row index of the editable pending review row, or -1 when none."""
        return len(self._view) if self._pending is not None else -1

    def is_pending_row(self, row) -> bool:
        return self._pending is not None and row == len(self._view)

    def pending_entry(self):
        """The ReviewEntry backing the pending row, or None."""
        return self._pending["entry"] if self._pending is not None else None

    def set_pending(self, entry) -> None:
        """Open an editable pending register row from a NEW review ``entry``.
        All its fields (date, num, payee, category, memo, amount) are editable in
        the register; nothing is persisted until :meth:`pending_values` is
        accepted.

        Payee AND category are PRE-FILLED here -- at the moment the row is created
        -- by matching the raw statement text against live history (learned
        payee/category rules + prior register transactions, including accepts made
        earlier in this same review session). Predicting here rather than once at
        download time is what lets in-session learning reach later rows. The
        predictions are stashed on the mapped row's PREDICTION fields (never over
        its source fields) so a later accept can tell whether the user corrected
        them (see :func:`import_review.save_new`)."""
        m = entry.mapped
        amt = m.amount_cents
        payee, cat_id = import_review.predict_fields(self.conn, m,
                                                     account_id=self.account_id)
        # Stash the prediction in its OWN field. Writing it to ``m.payee`` wrote
        # through to the review entry the panel is rendering, so selecting a row
        # silently rewrote the review list's own Payee cell -- the review list is
        # ground truth and must never be edited by a prediction.
        m.predicted_payee = payee
        m.category_id = cat_id
        # A transfer's Category cell holds the linked account as '[Account]'.
        # Pre-fill a learned transfer account (by request) so accepting keeps the
        # double-entry; fall back to the parsed counterparty name only when it
        # resolves to a real account.
        #
        # The learned rule is consulted for EVERY row, not only ones the parser
        # called a transfer. It is learned for text the parser does not
        # recognise -- "AUTOMATIC DEPOSIT, VENMO CASHOUT PPD" -- so gating the
        # lookup on the parser's verdict made it unreachable for the very rows it
        # was taught from, and the account had to be picked by hand every time.
        taid = import_review.predict_transfer_account(self.conn, m)
        if taid is None and m.is_transfer and m.transfer_account:
            taid = import_review.transfer_account_id_for_name(
                self.conn, f"[{m.transfer_account}]")
        if taid is not None:
            m.transfer_account_id = taid
            cat_text = import_review.account_label_for_id(self.conn, taid)
        elif m.is_transfer:
            m.transfer_account_id = None
            cat_text = import_review.account_label_for_id(self.conn, None)
        else:
            cat_text = import_review.category_name_for_id(self.conn, cat_id)
        self.beginResetModel()
        self._pending = {"entry": entry, "buf": {
            "date": m.date or "",
            "num": m.check_number or "",
            "payee": payee or "",
            "category": cat_text,
            "tag": "",
            "memo": m.memo or "",
            "payment": f"{abs(amt) / 100:.2f}" if amt < 0 else "",
            "deposit": f"{abs(amt) / 100:.2f}" if amt > 0 else "",
        }}
        self.endResetModel()

    def clear_pending(self) -> None:
        if self._pending is None:
            return
        self.beginResetModel()
        self._pending = None
        self.endResetModel()

    def pending_values(self) -> dict:
        """The edited pending-row fields as {date(ISO), num, payee, category,
        memo, amount_cents(signed)} for :func:`import_review.save_new`."""
        if self._pending is None:
            return {}
        b = self._pending["buf"]
        net = abs(parse_amount(b.get("deposit"))) - abs(parse_amount(b.get("payment")))
        return {
            "date": _to_iso(b.get("date")),
            "num": (b.get("num") or "").strip(),
            "payee": (b.get("payee") or "").strip(),
            "category": (b.get("category") or "").strip(),
            "memo": (b.get("memo") or "").strip(),
            "amount_cents": net,
        }

    def _pending_text(self, col) -> str:
        if self._pending is None:
            return ""
        key = {self.DATE: "date", self.NUM: "num", self.PAYEE: "payee",
               self.CATEGORY: "category", self.TAG: "tag", self.MEMO: "memo",
               self.PAYMENT: "payment", self.DEPOSIT: "deposit"}.get(col)
        return self._pending["buf"].get(key, "") if key else ""

    def row_for_txn(self, txn_id):
        """Row index of the transaction with this id, or -1 (used by Find to
        scroll+select a result in this account's register)."""
        for i, r in enumerate(self._view):
            if r["id"] == txn_id:
                return i
        return -1

    # ---- category picker choices -----------------------------------------
    def category_choices(self, row=None) -> list[str]:
        """Real category paths PLUS a ``[Other Account]`` entry per other
        account, so selecting one turns the row into a transfer.

        With a ``row``, the categories THIS row's payee has actually carried are
        lifted to the top, most likely first (:mod:`mammon.category_tree`); the
        full alphabetical list follows underneath so any category is still
        reachable. That ranking is the other half of the auto-categorizer: when
        the tree is not confident enough to fill the category in, it still knows
        which few categories are plausible for this merchant, and the picker is
        where that knowledge is worth something. Nothing is REMOVED -- a payee's
        first-ever category has to come from the full list.

        ``row`` is optional so the validation callers (``category_matches``, the
        new-category guard) keep getting the plain list they compare against.
        """
        self._transfer_targets = {}
        transfers = []
        for a in ledger.list_accounts(self.conn, include_closed=True):
            if a["id"] == self.account_id:
                continue
            label = f"[{a['name']}]"
            transfers.append(label)
            self._transfer_targets[label] = a["id"]
        cats = [c["path"] for c in ledger.list_categories(self.conn)]
        promoted = self._promoted_category_paths(row)
        if not promoted:
            return transfers + cats
        rest = [c for c in cats if c not in promoted]
        return promoted + transfers + rest

    def _promoted_category_paths(self, row) -> list[str]:
        """Category paths to lift to the top of the picker for ``row``.

        For the PENDING review row the ids come straight off the mapped row --
        :func:`import_review.predict_fields` already computed and stashed them,
        so the picker cannot disagree with the prediction that populated (or
        deliberately did not populate) the cell. For a posted row they are
        recomputed from the payee and its memo.
        """
        if row is None:
            return []
        ids: list = []
        try:
            if self._pending is not None and row == self.pending_row():
                m = self._pending["entry"].mapped
                ids = list(getattr(m, "category_candidates", None) or [])
                if not ids:
                    payee = getattr(m, "predicted_payee", None) or m.payee
                    desc = category_tree.source_text(
                        m.memo, m.payee,
                        bool(getattr(m, "payee_supplied", False)))
                    ids = [c for c, _ in
                           category_tree.ranked_categories(self.conn, payee, desc)]
            else:
                txn = self.txn_at(row)
                if txn is not None and txn["payee"]:
                    ids = [c for c, _ in category_tree.ranked_categories(
                        self.conn, txn["payee"], txn["memo"] or "")]
        except Exception:
            return []                      # a picker must never fail to open
        out: list[str] = []
        for cid in ids:
            path = ledger.category_path(self.conn, cid)
            if path and path not in out:
                out.append(path)
        return out

    def transfer_target(self, label):
        """Account id if ``label`` names a '[Account]' transfer target, else None."""
        if not self._transfer_targets:
            self.category_choices()
        return self._transfer_targets.get((label or "").strip())

    # ---- QAbstractTableModel API -----------------------------------------
    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._view) + (1 if self._pending is not None else 0) + 1

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            # Two-line mode collapses Category/Tag/Memo under the payee, so the
            # Payee header carries BOTH lines' labels (line-1 label on top, the
            # classification labels beneath) -- the user's fix (a). A tall header
            # (set by the view) makes the second line visible.
            if self.two_line and section == self.PAYEE:
                return "Payee\nCategory     Memo     Tag"
            # Liability/loan accounts: Quicken labels the money columns Increase
            # (balance owed goes up: a charge / the opening principal, stored as a
            # negative amount) and Decrease (goes down: a principal payment, a
            # positive amount) -- the same sign->column mapping, relabelled.
            if self._is_liability and section == self.PAYMENT:
                return "Increase"
            if self._is_liability and section == self.DEPOSIT:
                return "Decrease"
            return self.HEADERS[section]
        if role == Qt.DisplayRole and orientation == Qt.Vertical:
            return "*" if section >= len(self._view) else str(section + 1)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        col = index.column()
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        # Clr shows a centered letter and toggles on a single click (handled by
        # the view), and Balance is derived -- both are display-only cells.
        if col in (self.CLR, self.BALANCE):
            return base
        row = index.row()
        if self.is_pending_row(row):
            # Every field of the pending review row is editable in the register,
            # including Num so an imported check number can be entered/corrected
            # here before the row is accepted. Tag is not part of the reviewed
            # data, so leave it fixed.
            if col == self.TAG:
                return base
            return base | Qt.ItemIsEditable
        if not self.is_blank_row(row) and 0 <= row < len(self._view):
            r = self._view[row]
            # A plain two-sided transfer's Category cell names the linked
            # account; it CAN now be re-pointed inline (picking another
            # "[Account]" rewires the mirror pair -- see _set_existing). Only a
            # split-transfer (managed in the split dialog) or a one-sided mirror
            # leg (no counter-leg to move) stays read-only here.
            if (col == self.CATEGORY and r["transfer_account_id"] is not None
                    and (r.get("is_split") or r["transfer_pair_id"] is None)):
                return base
            # A split transaction's category is '--Split--' and its total is the
            # sum of the split lines; both are managed in the Split dialog, so
            # Category/Payment/Deposit are read-only inline to keep them in sync.
            if r.get("is_split") and col in (self.CATEGORY, self.PAYMENT, self.DEPOSIT):
                return base
            # A loan payment's Category shows the derived principal/interest/escrow
            # split (Task 51); it is not a free-typed category, so keep it read-only.
            if col == self.CATEGORY and self._is_loan and r["amount"] > 0 \
                    and self.loan_payment_legs(row):
                return base
        return base | Qt.ItemIsEditable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        pending = self.is_pending_row(row)
        blank = self.is_blank_row(row)

        # Defensive bounds guard (the user's IndexError at models.py data()): a view
        # whose cached rowCount went stale across a shrink/reload -- e.g. after a
        # backup restore reloads fewer rows, an account switch, or a filter that
        # shrinks the list -- can still paint an old, larger index. Such a row is
        # a real-row index past the current list end (not the pending or blank
        # row, whose positions are recomputed from the *current* len), so the
        # self._view[row] accesses below would raise. Return None and let the
        # in-flight reset repaint with the correct row count.
        if not pending and not blank and not (0 <= row < len(self._view)):
            return None

        if role == self.SECOND_LINE_ROLE:
            if self.two_line and col == self.PAYEE and not blank and not pending:
                return self._second_line_text(row)
            return ""

        if role == self.TAG_COLORS_ROLE:
            if col == self.TAG and not blank and not pending:
                return self._tag_swatches(row)
            return []

        if role == Qt.DisplayRole:
            if pending:
                txt = self._pending_text(col)
                return fmt_date(txt) if col == self.DATE and txt else txt
            if blank:
                txt = self._blank_text(col)
                return fmt_date(txt) if col == self.DATE and txt else txt
            if col == self.DATE:
                return fmt_date(self._view[row]["date"])
            # A loan payment shows its principal/interest/escrow split inline in
            # the Category cell, so interest and escrow are never lost from view.
            if col == self.CATEGORY:
                summary = self._loan_split_label(row)
                if summary:
                    return summary
            return self._row_text(row, col)

        if role == Qt.DecorationRole:
            # A split whose category/transfer lines don't fully cover the total
            # keeps the leftover in an uncategorized line; flag it with a yellow
            # warning triangle before '--Split--' in the Category cell so the user sees
            # money he still has to assign. The default delegate draws a
            # DecorationRole icon to the LEFT of the display text, so it lands
            # exactly in front of '--Split--'. Blank/pending rows have no split.
            if not blank and not pending and col == self.CATEGORY:
                if self._view[row].get("uncat_split", 0):
                    return warning_triangle_icon()
            return None

        if role == Qt.ToolTipRole:
            # PENDING is excluded here, not merely inside: the pending row's index
            # IS len(self._view) (see is_pending_row), so `self._view[row]` on it
            # raises IndexError every time. The inner `not pending` check came too
            # late -- the row had already been dereferenced. Typing a category in a
            # new transaction and pressing Enter asks the view for that cell's
            # tooltip, which crashed the app. A not-yet-committed row has no stored
            # split and no loan legs, so it has nothing to describe.
            if (not blank and not pending and col != self.CATEGORY
                    and self._view[row].get("scheduled")):
                return ("Pending pre-entry: scheduled, not yet posted. It posts when "
                        "a download merges into it, when you mark it cleared, or "
                        "from the right-click menu's Enter Pending Payment.")
            if not blank and not pending and col == self.CATEGORY:
                r = self._view[row]
                if r.get("uncat_split", 0):
                    return (f"Uncategorized: {fmt_money(r['uncat_split'])} of this "
                            "split is not assigned to a category")
                return self._loan_split_label(row) or None
            return None

        if role == Qt.EditRole:
            # Edit in the underlying form: dates as ISO, amounts as plain numbers.
            if pending:
                return self._pending_text(col)
            return self._blank_text(col) if blank else self._row_text(row, col)

        if role == Qt.TextAlignmentRole:
            if col in (self.PAYMENT, self.DEPOSIT, self.BALANCE):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if col == self.CLR:
                return int(Qt.AlignCenter)

        # A PENDING pre-entry (scheduled=1: placed by a reminder, not yet real)
        # reads gray in every column (its tooltip, above, says how it becomes
        # real). It was indistinguishable from a posted row before, which made
        # "accept the pending row" an instruction with nothing to point at.
        if (role == Qt.ForegroundRole and not blank and not pending
                and self._view[row].get("scheduled")):
            return QBrush(QColor("#8a8a8a"))

        if role == Qt.ForegroundRole:
            if not blank and not pending:
                if col == self.BALANCE and self._view[row]["balance"] < 0:
                    return QBrush(QColor(style.negative_color()))
                if col == self.CLR:
                    return QBrush(QColor("#2e7d32"))  # cleared/reconciled glyph, green
            # Every other cell -- one-line rows, the two-line payee's FIRST line
            # (the delegate paints it via super().paint() from this palette color),
            # and the blank quick-entry row -- takes the active theme's explicit
            # cell-text color. In dark mode item text would otherwise fall back to
            # the palette's near-black default and be illegible; in light mode
            # cell_text_color() is None, so the look is byte-for-byte unchanged.
            ct = style.cell_text_color()
            if ct:
                return QBrush(QColor(ct))
        return None

    def _clr_glyph(self, r) -> str:
        """Quicken's cleared marker: 'R' reconciled, 'c' cleared, blank otherwise."""
        if r["reconciled"]:
            return "R"
        if r["cleared"]:
            return "c"
        return ""

    def _tag_swatches(self, row) -> list:
        """The ``(name, color)`` chips the Tag cell paints for this row: the row's
        own tags, then any tags its SPLIT LEGS carry (deduped case-insensitively),
        each mapped to its identity color (``None`` when uncolored). The union
        matches :func:`reports._lines._line_tags`, so a split's per-leg tags
        surface on the collapsed register row without double-counting."""
        r = self._view[row]
        names = list(ledger.parse_tags(r.get("tag") or ""))
        seen = {n.casefold() for n in names}
        for n in self._leg_tags.get(r.get("id"), []):
            if n.casefold() not in seen:
                seen.add(n.casefold())
                names.append(n)
        return [(n, self._tag_colors.get(n.casefold())) for n in names]

    def _row_text(self, row, col) -> str:
        r = self._view[row]
        if col == self.DATE:
            return r["date"]
        if col == self.NUM:
            return r["num"] or ""
        if col == self.PAYEE:
            return r["payee"] or ""
        if col == self.CATEGORY:
            return r["category_label"]
        if col == self.TAG:
            return r["tag"] or ""
        if col == self.MEMO:
            return r["memo"] or ""
        if col == self.PAYMENT:
            return fmt_cents(-r["amount"]) if r["amount"] < 0 else ""
        if col == self.CLR:
            return self._clr_glyph(r)
        if col == self.DEPOSIT:
            return fmt_cents(r["amount"]) if r["amount"] > 0 else ""
        if col == self.BALANCE:
            return fmt_cents(r["balance"])
        return ""

    def _blank_text(self, col) -> str:
        key = {self.DATE: "date", self.NUM: "num", self.PAYEE: "payee",
               self.CATEGORY: "category", self.TAG: "tag", self.MEMO: "memo",
               self.PAYMENT: "payment", self.DEPOSIT: "deposit"}.get(col)
        return self.blank_values().get(key, "") if key else ""

    # ---- QuickFill (feature parity: memorized payees) ---------------------
    def payee_choices(self) -> list[str]:
        """Payees the register has used, most recent first, for the payee
        completer. Cached until the next reload (every write reloads)."""
        if getattr(self, "_payee_choices", None) is None:
            self._payee_choices = ledger.list_payees(self.conn)
        return self._payee_choices

    def quickfill_fields(self, payee: str) -> dict:
        """The register-shaped fields QuickFill would pre-enter for ``payee``:
        ``category`` (a path or ``[Account]``), ``memo``, ``tag`` and ONE of
        ``payment``/``deposit`` as display text. Empty for an unknown payee.
        The choice of what to recall lives in :func:`categorize.quickfill`."""
        payee = (payee or "").strip()
        if not payee:
            return {}
        fill = categorize.quickfill(self.conn, payee, self.account_id)
        out = {}
        for key in ("category", "memo", "tag"):
            if fill.get(key):
                out[key] = fill[key]
        amount = int(fill.get("amount") or 0)
        if amount < 0:
            out["payment"] = fmt_cents(-amount)
        elif amount > 0:
            out["deposit"] = fmt_cents(amount)
        return out

    def blank_values(self) -> dict:
        """The blank quick-entry row as the user sees it: what they typed laid
        over what QuickFill pre-entered. A field the user cleared stays cleared,
        and a typed amount in EITHER money column drops the pre-entered one, so
        a guessed deposit can never net against a typed payment."""
        auto = dict(getattr(self, "_auto", {}) or {})
        if self._new.get("payment") or self._new.get("deposit"):
            auto.pop("payment", None)
            auto.pop("deposit", None)
        return {**auto, **self._new}

    def commit_blank(self) -> bool:
        """Record the blank row when it is complete: a date plus an amount,
        typed or pre-entered. This is the register's Enter gesture on the blank
        row -- the one way a QuickFilled amount gets recorded without the user
        retyping it. Returns False, leaving the buffer intact, when the row is
        not ready."""
        v = self.blank_values()
        if not (v.get("date") and (v.get("payment") or v.get("deposit"))):
            return False
        return self.add_from_values(v)

    def commit_blank_returning_id(self):
        """Commit the blank quick-entry row exactly like :meth:`commit_blank`,
        but return the id of the transaction it created -- or None when the row
        is not ready (no date + amount). A transfer typed on the blank row
        returns the leg in THIS account, which is splittable like any other
        transfer (ledger.set_splits moves the transfer onto one split line).
        This lets the register's Split gesture treat a
        half-typed NEW transaction the way it already treats a pending review
        row: persist it through the ledger first, then open the split editor on
        the row it created (Quicken splits the SAVED transaction). It reloads
        synchronously via add_from_values, so a following row_for_txn sees the
        new row at once; callers run from a toolbar/menu action, never inside a
        delegate's editor teardown, so the reset hazard defer_reload guards
        against does not apply here."""
        v = self.blank_values()
        if not (v.get("date") and (v.get("payment") or v.get("deposit"))):
            return None
        if not self.add_from_values(v):
            return None
        return self._last_new_id

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or role != Qt.EditRole:
            return False
        row, col = index.row(), index.column()
        if self.is_pending_row(row):
            return self._set_pending(col, value)
        if self.is_blank_row(row):
            return self._set_blank(col, value)
        # Bounds guard mirroring data(): reject an edit aimed at a stale
        # out-of-range real-row index rather than IndexError in _set_existing.
        if not (0 <= row < len(self._view)):
            return False
        return self._set_existing(row, col, value)

    # ---- pending review row inline edits ---------------------------------
    def _set_pending(self, col, value) -> bool:
        """Buffer an edit to the pending review row (no DB write until accept)."""
        if self._pending is None:
            return False
        key = {self.DATE: "date", self.NUM: "num", self.PAYEE: "payee",
               self.CATEGORY: "category", self.TAG: "tag", self.MEMO: "memo",
               self.PAYMENT: "payment", self.DEPOSIT: "deposit"}.get(col)
        if key is None:
            return False
        self._pending["buf"][key] = (
            _to_iso(value) if col == self.DATE else str(value or "").strip())
        r = self.pending_row()
        self.dataChanged.emit(self.index(r, 0), self.index(r, len(self.HEADERS) - 1))
        return True

    # ---- blank quick-entry row -------------------------------------------
    def _set_blank(self, col, value) -> bool:
        key = {self.DATE: "date", self.NUM: "num", self.PAYEE: "payee",
               self.CATEGORY: "category", self.TAG: "tag", self.MEMO: "memo",
               self.PAYMENT: "payment", self.DEPOSIT: "deposit"}.get(col)
        if key is None:
            return False
        self._new[key] = _to_iso(value) if col == self.DATE else str(value or "").strip()
        if col == self.PAYEE:
            # QuickFill: a known payee pre-enters its last category, memo, tag and
            # amount into the fields the user has not typed. Re-entering the
            # payee re-guesses; the user's own fields are never touched.
            self._auto = self.quickfill_fields(self._new["payee"])
        # Commit as soon as the USER has supplied a date and a money column. A
        # pre-entered amount never trips this on its own: it waits in _auto until
        # the user types an amount (commits here) or presses Enter (commit_blank).
        # defer_reload is not optional here: setData runs inside the just-typed
        # cell's delegate.setModelData while Qt is still tearing that editor down,
        # so resetting the model synchronously (add_from_values -> reload) would
        # invalidate the index the view still holds and hang, then drop the row
        # instead of committing it -- the exact hazard _write defers reloads for.
        if self._new.get("date") and (self._new.get("payment") or self._new.get("deposit")):
            self.add_from_values(self.blank_values(), defer_reload=True)
        else:
            row = self.rowCount() - 1
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.HEADERS) - 1))
        return True

    # ---- existing-row inline edits ---------------------------------------
    def _set_existing(self, row, col, value) -> bool:
        txn = self._view[row]
        text = str(value or "").strip()
        if col == self.DATE:
            fn = lambda t: ledger.update_transaction(self.conn, t["id"], date=_to_iso(value))
        elif col == self.NUM:
            fn = lambda t: ledger.update_transaction(self.conn, t["id"], num=text or None)
        elif col == self.PAYEE:
            fn = lambda t: ledger.update_transaction(self.conn, t["id"], payee=text or None)
        elif col == self.MEMO:
            fn = lambda t: ledger.update_transaction(self.conn, t["id"], memo=text or None)
        elif col == self.TAG:
            fn = lambda t: ledger.update_transaction(self.conn, t["id"], tag=text or None)
        elif col == self.CATEGORY:
            if txn["transfer_account_id"] is not None:
                # Re-point an existing transfer to a different account inline:
                # rewire the mirror pair (move the counter-leg). Only an
                # "[Account]" target is accepted here; converting a transfer
                # back to a plain category is not an inline gesture.
                retarget = self.transfer_target(text)
                if retarget is None:
                    return False
                fn = lambda t: ledger.retarget_transfer(self.conn, t["id"], retarget)
                return self._write(fn, row)
            target = self.transfer_target(text)
            if target is not None:
                # Category set to an "[Account]": turn this plain transaction into
                # a transfer, spawning the linked mirror in the target account.
                if ledger.has_splits(self.conn, txn["id"]):
                    return False  # a split can't collapse into a plain transfer inline
                fn = lambda t: ledger.convert_to_transfer(self.conn, t["id"], target)
            else:
                cid = ledger.resolve_category(self.conn, text)

                def fn(t):
                    ledger.update_transaction(self.conn, t["id"], category_id=cid)
                    # A user's manual category edit is an override that outranks
                    # learned mappings and teaches future suggestions (SRD 5.5).
                    if text and t["payee"]:
                        categorize.record_user_categorization(self.conn, t["payee"], cid)
        elif col == self.PAYMENT:
            # A leading '-' (parse_amount returns a negative) flips the magnitude
            # to the Deposit column as a positive amount, like Quicken; a plain
            # value keeps the Payment column's money-out sign.
            fn = lambda t: ledger.update_transaction(self.conn, t["id"], amount=-parse_amount(value))
        elif col == self.DEPOSIT:
            # Symmetrically, a leading '-' in Deposit moves the magnitude to the
            # Payment column (negative); a plain value stays money-in.
            fn = lambda t: ledger.update_transaction(self.conn, t["id"], amount=parse_amount(value))
        else:
            return False
        # Inline edit: the editor is still alive, so defer the reset (see _write).
        return self._write(fn, row, defer_reload=True)

    # ---- write helpers (used inline and by the details dialog) -----------
    def add_from_values(self, v: dict, *, defer_reload: bool = False) -> bool:
        """Insert a transaction (or a transfer, if the category is a
        '[Account]' target) from a field dict. Reused by the blank row and the
        new/edit dialog.

        ``defer_reload`` is set only on the inline blank-row auto-commit, which
        runs inside a delegate's setModelData while Qt is tearing the editor
        down. The DB write is safe there (it touches no model index); it is the
        model RESET that must wait a turn (see _write). So this writes and clears
        the blank-row buffer synchronously -- a fresh connection and a racing
        Enter/closeEditor commit both see the finished row immediately, with no
        double insert -- and only the view refresh is pushed to _reload_timer."""
        # Id of the transaction this creates, so the blank-row Split gesture
        # (commit_blank_returning_id) can open the split editor on it. A transfer
        # records the leg in THIS account -- a transfer is splittable now, with
        # the transfer becoming one LINE of the split. reload() below does not
        # touch it, so it survives to the caller.
        self._last_new_id = None
        net = abs(parse_amount(v.get("deposit"))) - abs(parse_amount(v.get("payment")))
        category = (v.get("category") or "").strip()
        target = self.transfer_target(category)
        date = _to_iso(v.get("date"))
        cleared = int(v.get("cleared") or 0)
        try:
            payee = (v.get("payee") or "").strip() or None
            if target is not None and net != 0:
                if net < 0:  # money out of this account
                    from_id, to_id = ledger.create_transfer(
                        self.conn, self.account_id, target, date,
                        -net, memo=v.get("memo") or None,
                        num=v.get("num") or None, cleared=cleared,
                        payee=payee)
                    self._last_new_id = from_id
                else:        # money into this account
                    from_id, to_id = ledger.create_transfer(
                        self.conn, target, self.account_id, date,
                        net, memo=v.get("memo") or None,
                        num=v.get("num") or None, cleared=cleared,
                        payee=payee)
                    self._last_new_id = to_id
                self.undo_stack.record_add([from_id, to_id], transfer=True)
            else:
                user_typed_cat = bool(category) and target is None
                if user_typed_cat:
                    cid = ledger.resolve_category(self.conn, category)
                elif payee:
                    # No category typed: auto-fill from history if a confident
                    # payee->category pattern exists (SRD 5.5).
                    cid = categorize.suggest_category(self.conn, payee)
                else:
                    cid = None
                self._last_new_id = ledger.add_transaction(
                    self.conn, self.account_id, date, net,
                    payee=payee,
                    memo=v.get("memo") or None,
                    num=v.get("num") or None,
                    category_id=cid,
                    tag=(v.get("tag") or None),
                    cleared=cleared)
                # A category the USER typed is an override that teaches history.
                if user_typed_cat and payee:
                    categorize.record_user_categorization(self.conn, payee, cid)
                if self._last_new_id is not None:
                    self.undo_stack.record_add([self._last_new_id])
        except (ValueError, KeyError) as exc:
            self.error.emit(str(exc))
            return False
        # A brand-new transaction is unambiguously a save, and a complete one:
        # the blank row commits as a whole, so it needs no coalescing.
        self.transactionSaved.emit(0)
        if defer_reload:
            # Clear the quick-entry buffer NOW (blank_values() goes empty, so a
            # racing Enter/closeEditor commit_blank is a no-op, not a duplicate),
            # but defer the model reset a turn so it never runs while the editor
            # is being destroyed. reload() re-clears both harmlessly.
            self._new = {}
            self._auto = {}
            self._reload_timer().start(0)
        else:
            self.reload()
            self.committed.emit()
        return True

    def update_from_values(self, row, v: dict) -> bool:
        """Apply the details dialog's fields to an existing transaction."""
        txn = self.txn_at(row)
        if not txn:
            return False
        net = abs(parse_amount(v.get("deposit"))) - abs(parse_amount(v.get("payment")))
        fields = {
            "date": _to_iso(v.get("date")),
            "num": (v.get("num") or "").strip() or None,
            "memo": (v.get("memo") or "").strip() or None,
            "tag": (v.get("tag") or "").strip() or None,
            "amount": net,
            "cleared": int(v.get("cleared") or 0),
        }
        override = None  # (payee, category_id) to teach if the user set a category
        convert_target = None  # account id when the category names a transfer target
        retarget_target = None  # account id when an existing transfer is re-pointed
        is_transfer = txn["transfer_account_id"] is not None
        # A plain two-sided transfer can be re-pointed here; a split-transfer or a
        # one-sided mirror leg keeps its category/payee locked (see _load).
        retargetable = (is_transfer and txn["transfer_pair_id"] is not None
                        and not ledger.has_splits(self.conn, txn["id"]))
        if not is_transfer:
            fields["payee"] = (v.get("payee") or "").strip() or None
            cat = (v.get("category") or "").strip()
            target = self.transfer_target(cat)
            if target is not None and not ledger.has_splits(self.conn, txn["id"]):
                # Category set to an "[Account]": convert this plain transaction
                # into a transfer once its other fields (incl. amount) are applied.
                convert_target = target
            elif target is None:
                cid = ledger.resolve_category(self.conn, cat) if cat else None
                fields["category_id"] = cid
                if cat and fields["payee"]:
                    override = (fields["payee"], cid)
        elif retargetable:
            # Payee mirrors to both legs via update_transaction; the Category
            # field re-points the transfer to a different account.
            fields["payee"] = (v.get("payee") or "").strip() or None
            cat = (v.get("category") or "").strip()
            target = self.transfer_target(cat)
            if target is not None and target != txn["transfer_account_id"]:
                retarget_target = target
            elif target is None and cat and cat != txn["category_label"]:
                # The user typed something that is not an "[Account]": turning a
                # transfer back into a plain category is not supported here.
                self.error.emit(
                    "A transfer's Category must be an [Account]; delete and "
                    "re-enter it to make it a plain category.")
                return False
        undo_before = self.undo_stack.capture(txn["id"])
        try:
            ledger.update_transaction(self.conn, txn["id"], **fields)
            if convert_target is not None:
                ledger.convert_to_transfer(self.conn, txn["id"], convert_target)
            if retarget_target is not None:
                ledger.retarget_transfer(self.conn, txn["id"], retarget_target)
            if override is not None:
                categorize.record_user_categorization(self.conn, *override)
        except (ValueError, KeyError) as exc:
            self.error.emit(str(exc))
            return False
        # A convert/retarget is detected as a structural change and drops the redo
        # stack rather than recording a step it could not cleanly invert.
        self.undo_stack.record_edit(txn["id"], undo_before)
        self.reload()
        self.committed.emit()
        return True

    def delete_row(self, row) -> bool:
        txn = self.txn_at(row)
        if not txn:
            return False
        # A transfer deletes BOTH mirror legs, so snapshot both before deleting so
        # Undo can recreate the whole transfer. Read the pair id from the ledger
        # (authoritative) rather than the projected view row.
        full = ledger.get_transaction(self.conn, txn["id"])
        is_transfer = full is not None and full["transfer_account_id"] is not None
        ids = [int(txn["id"])]
        if is_transfer and full["transfer_pair_id"] is not None:
            ids.append(int(full["transfer_pair_id"]))
        snaps = self.undo_stack.capture_many(ids)
        ledger.delete_transaction(self.conn, txn["id"])
        self.undo_stack.push_delete(snaps, transfer=is_transfer)
        self.reload()
        self.committed.emit()
        return True

    # ---- undo / redo (Ctrl+Z / Ctrl+Y, wired from the Edit menu) -------------
    def can_undo(self) -> bool:
        return self.undo_stack.can_undo()

    def can_redo(self) -> bool:
        return self.undo_stack.can_redo()

    def undo_label(self):
        return self.undo_stack.undo_label()

    def redo_label(self):
        return self.undo_stack.redo_label()

    def undo(self) -> bool:
        """Reverse the most recent register edit, replaying its inverse through
        the ledger, then refresh. ``committed`` also refreshes any OTHER open
        register, so a transfer's mirror side updates too."""
        if not self.undo_stack.undo():
            return False
        self.reload()
        self.committed.emit()
        return True

    def redo(self) -> bool:
        if not self.undo_stack.redo():
            return False
        self.reload()
        self.committed.emit()
        return True

    # ---- batch operations over a multi-row selection -------------------------
    def void_row(self, row) -> bool:
        """Quicken's Void on one row (see :func:`ledger.void_transaction`)."""
        return self._write(lambda t: ledger.void_transaction(self.conn, t["id"]), row)

    def txn_ids_at(self, rows) -> list[int]:
        """The transaction ids behind display rows (blank/pending rows skipped)."""
        out = []
        for row in rows:
            t = self.txn_at(row)
            if t:
                out.append(int(t["id"]))
        return out

    def apply_to_transactions(self, txn_ids, fn) -> tuple[int, int]:
        """Run ``fn(txn_row)`` for every id, ONE reload at the end. ``fn``
        returns True when it changed the row, False to report it skipped (a
        transfer offered a category, say); a ``ValueError``/``KeyError`` from
        the ledger counts as skipped too. Returns ``(changed, skipped)``. Emits
        ``committed`` once, and no per-row save sound -- a batch is one act."""
        changed = skipped = 0
        for tid in txn_ids:
            t = ledger.get_transaction(self.conn, tid)
            if t is None:
                continue
            try:
                ok = fn(t)
            except (ValueError, KeyError) as exc:
                self.error.emit(str(exc))
                ok = False
            if ok:
                changed += 1
            else:
                skipped += 1
        # A batch is one act, not individually undoable in this version: it is a
        # barrier that drops the redo stack (so a stale redo cannot replay across
        # it) while leaving earlier undo history intact.
        if changed:
            self.undo_stack.barrier()
        self.reload()
        self.committed.emit()
        return changed, skipped

    def batch_set_category(self, txn_ids, text: str) -> tuple[int, int]:
        """Set one category (or ``[Account]`` transfer target) on many rows.
        A split, and a transfer offered a plain category, are skipped; a plain
        row offered an ``[Account]`` becomes a transfer. A category the user
        chose here teaches the payee mapping like a single edit would."""
        text = (text or "").strip()
        target = self.transfer_target(text)
        cid = None if target is not None else ledger.resolve_category(self.conn, text)
        taught: set[str] = set()

        def fn(t):
            if ledger.has_splits(self.conn, t["id"]):
                return False
            if target is not None:
                if t["transfer_account_id"] is not None:
                    if t["transfer_pair_id"] is None:
                        return False
                    ledger.retarget_transfer(self.conn, t["id"], target)
                    return True
                ledger.convert_to_transfer(self.conn, t["id"], target)
                return True
            if t["transfer_account_id"] is not None:
                return False
            ledger.update_transaction(self.conn, t["id"], category_id=cid)
            if cid is not None and t["payee"] and t["payee"] not in taught:
                taught.add(t["payee"])
                categorize.record_user_categorization(self.conn, t["payee"], cid)
            return True

        return self.apply_to_transactions(txn_ids, fn)

    def batch_set_field(self, txn_ids, field: str, text: str) -> tuple[int, int]:
        """Set payee, memo, tag or num on many rows (``""`` clears)."""
        if field not in ("payee", "memo", "tag", "num"):
            raise ValueError(f"cannot batch-set {field!r}")
        value = (text or "").strip() or None

        def fn(t):
            ledger.update_transaction(self.conn, t["id"], **{field: value})
            return True

        return self.apply_to_transactions(txn_ids, fn)

    def batch_set_cleared(self, txn_ids, cleared: bool) -> tuple[int, int]:
        """Mark many rows cleared (``c``) or uncleared. A reconciled row is
        skipped either way: 'R' is reconcile-managed and never flipped in bulk."""
        def fn(t):
            if t["reconciled"]:
                return False
            if bool(t["cleared"]) == bool(cleared):
                return False
            ledger.update_transaction(self.conn, t["id"], cleared=1 if cleared else 0)
            return True

        return self.apply_to_transactions(txn_ids, fn)

    def batch_void(self, txn_ids) -> tuple[int, int]:
        return self.apply_to_transactions(
            txn_ids, lambda t: ledger.void_transaction(self.conn, t["id"]))

    def batch_delete(self, txn_ids) -> tuple[int, int]:
        def fn(t):
            ledger.delete_transaction(self.conn, t["id"])
            return True

        return self.apply_to_transactions(txn_ids, fn)

    def clr_cycle_next(self, row):
        """The next Clr state for the register's blank -> c -> R -> blank click
        cycle on ``row`` (matches the classic desktop ledger).

        Returns ``(cleared, reconciled, touches_reconciled, setting_reconciled)``
        or ``None`` for a blank/pending row or an invalid index. ``touches_r``
        is True for the two steps that SET or CLEAR 'R' (reconcile-managed --
        the register widget confirms those before writing); ``setting_r``
        distinguishes the c -> R set from the R -> blank clear.
        """
        txn = self.txn_at(row)
        if not txn:
            return None
        if txn["reconciled"]:
            # R -> blank: fully unmarks a reconciled row (including a transfer
            # leg) so it can be reconciled afresh. Clearing 'R' is a manual
            # override of a reconcile-managed flag -> confirm.
            return (0, 0, True, False)
        if txn["cleared"]:
            # c -> R: manually promote to reconciled -> confirm.
            return (1, 1, True, True)
        # blank -> c: ordinary cleared toggle, no confirmation.
        return (1, 0, False, False)

    def toggle_cleared(self, row) -> bool:
        """Advance the Clr flag one step in Quicken's blank -> c -> R -> blank
        cycle and persist it to THIS leg only. Transfers reconcile per-leg;
        ``update_transaction`` never mirrors cleared/reconciled, so the
        counter-leg is untouched. The 'R' steps are reconcile-managed and the
        register widget confirms them before calling here; a direct call
        applies the cycle unconditionally.
        """
        nxt = self.clr_cycle_next(row)
        if nxt is None:
            return False
        cleared, reconciled, _touches_r, _setting_r = nxt

        def fn(t):
            ledger.update_transaction(self.conn, t["id"],
                                      cleared=cleared, reconciled=reconciled)
            # A cleared row is by definition real: marking a pending pre-entry
            # cleared posts it (mirror included), so it never lingers as a
            # placeholder that the next download or Enter would double.
            if cleared and t.get("scheduled"):
                ledger.set_scheduled(self.conn, t["id"], False)

        return self._write(fn, row)

    def _write(self, fn, row, *, defer_reload: bool = False) -> bool:
        """Apply ``fn`` to row ``row``'s transaction, then refresh.

        ``defer_reload`` pushes the model reset to the next event-loop turn. It
        is set on the INLINE EDIT path, and it is not cosmetic: Qt calls
        setModelData BEFORE it destroys the editor, so resetting the model here
        invalidates every index while the view is still holding the one it is
        about to tear down. Qt then dereferences freed internals -- a hard access
        violation inside the event loop, with no Python frame and no Qt warning,
        which is what made it so hard to place. Letting the editor finish first
        costs one event-loop turn and removes the whole class of failure."""
        txn = self.txn_at(row)
        if not txn:
            return False
        before = self._sound_snapshot(txn["id"])
        undo_before = self.undo_stack.capture(txn["id"])
        try:
            fn(txn)
        except (ValueError, KeyError) as exc:
            self.error.emit(str(exc))
            return False
        # Only a write that actually ALTERED the row counts as a save. Qt commits
        # an editor on focus-out whether or not it was touched, so keying this off
        # "an edit was committed" would chime for clicking a cell and clicking
        # away -- and a confirmation that fires on nothing is one you learn to
        # ignore, which costs you the signal for real saves too.
        if self._sound_snapshot(txn["id"]) != before:
            self.transactionSaved.emit(int(txn["id"]))
        # Record the inverse for Undo. A no-op write records nothing; a transfer
        # convert/retarget (which has no clean ledger inverse) drops the redo
        # stack instead of pushing a bad step (see undo.record_edit).
        self.undo_stack.record_edit(txn["id"], undo_before)
        if defer_reload:
            # One pending reload at a time. Several edits committed in the same
            # turn (or a reload that itself triggers another write) would
            # otherwise queue a timer each and reset the model repeatedly.
            self._reload_timer().start(0)
        else:
            self.reload()
            self.committed.emit()
        return True

    _SOUND_FIELDS = ("date", "num", "payee", "memo", "tag", "amount",
                     "category_id", "transfer_account_id", "cleared", "reconciled")

    def _sound_snapshot(self, txn_id):
        """The fields whose change means the user saved something. Deliberately
        excludes bookkeeping columns (import ids, fitid) that a write may touch
        without the user having changed anything they can see."""
        row = ledger.get_transaction(self.conn, txn_id)
        if row is None:
            return None
        keys = row.keys() if hasattr(row, "keys") else ()
        return tuple(row[f] for f in self._SOUND_FIELDS if f in keys)

    def _reload_timer(self) -> QTimer:
        """A single-shot timer OWNED by this model.

        Deliberately not ``QTimer.singleShot``: that has no owner, holds the
        bound method alive, and fires even after the model's database connection
        has been closed -- which in a test run meant a timer scheduled by one
        case firing during the next, against a dead connection. A parented timer
        is destroyed with the model, and restarting it coalesces several edits in
        the same turn into one reset."""
        t = getattr(self, "_reload_qtimer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._deferred_reload)
            self._reload_qtimer = t
        return t

    def _deferred_reload(self) -> None:
        """The tail of an inline edit, run once Qt has finished with the editor."""
        try:
            self.reload()
            self.committed.emit()
        except Exception:               # pragma: no cover - model/connection gone
            pass


# ---------------------------------------------------------------------------
# investment register model
# ---------------------------------------------------------------------------
@dataclass
class InvestmentFilter:
    """What the investment register's filter bar narrows to -- the investment
    parallel of :class:`RegisterFilter`, over the investment columns.

    ``text`` is a case-insensitive substring over the action, the security /
    category, the memo AND the formatted amount, so typing part of a symbol, a
    fee's description or ``520`` all find their rows. ``action`` matches one
    action verb exactly (case-insensitive). Amount bounds are absolute cents over
    the row's own stored amount (a buy and a sell of the same size both fall in a
    range). An empty field is no constraint; :meth:`is_empty` means the whole
    filter is a no-op."""

    text: str = ""
    date_from: str = ""            # ISO, inclusive
    date_to: str = ""              # ISO, inclusive
    amount_min: int | None = None  # absolute cents
    amount_max: int | None = None
    action: str = ""

    def is_empty(self) -> bool:
        return not (self.text.strip() or self.date_from or self.date_to
                    or self.amount_min is not None or self.amount_max is not None
                    or self.action.strip())

    def matches(self, r: dict) -> bool:
        needle = self.text.strip().lower()
        if needle:
            hay = " | ".join(
                str(r.get(k) or "")
                for k in ("action", "symbol", "memo", "category_label"))
            amt = r.get("amount")
            if amt is not None:
                hay = f"{hay} | {fmt_cents(abs(int(amt)))}"
            if needle not in hay.lower():
                return False
        if self.date_from and r["date"] < self.date_from:
            return False
        if self.date_to and r["date"] > self.date_to:
            return False
        if self.amount_min is not None or self.amount_max is not None:
            magnitude = abs(int(r.get("amount") or 0))
            if self.amount_min is not None and magnitude < self.amount_min:
                return False
            if self.amount_max is not None and magnitude > self.amount_max:
                return False
        want = self.action.strip().lower()
        if want and (r.get("action") or "").strip().lower() != want:
            return False
        return True


class InvestmentRegisterModel(QAbstractTableModel):
    """One investment account's activity as a table with the classic Quicken
    investment columns (the user's data/InvestmentRegister.png).

    A cash :class:`RegisterModel` splits money across Payment/Deposit/Balance over
    the ``transactions`` table; an investment account's activity is Buys/Sells/
    Divs/ReinvDivs with security, quantity, price fields that live in a SEPARATE
    ``investment_transactions`` table, so it needs its own projection. This is a
    THIN projection over :func:`mammon.investments.register_rows`, which derives the
    per-row running balances (share_bal per security, cash_amt, running cash_bal)
    -- Quicken's Share Bal / Cash Amt / Cash Bal columns. We keep our own
    Quantity/Price columns in place of Quicken's combined 'Description' (the user OK'd
    that split). v1 is READ-ONLY -- inline editing of lots is out of scope."""

    (DATE, ACTION, SECURITY, QUANTITY, PRICE,
     SHARE_BAL, INV_AMT, CASH_AMT, CASH_BAL) = range(9)
    # The SECURITY column does double duty: a security trade shows its symbol, a
    # cash line shows the transfer account / category it points at -- so it reads
    # 'Security / Category' (reusing the column rather than adding a new one).
    HEADERS = ["Date", "Action", "Security / Category", "Quantity", "Price",
               "Share Bal", "Inv Amt", "Cash Amt", "Cash Bal"]
    # Right-aligned numeric columns (quantities/prices/amounts/balances).
    _NUMERIC = (QUANTITY, PRICE, SHARE_BAL, INV_AMT, CASH_AMT, CASH_BAL)

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        self._rows: list = []
        self._symbol_filter = None       # None -> show every security
        # The VIEW: _rows (application order) projected through the current sort
        # and free-text filter -- the exact pattern the cash RegisterModel uses.
        # Every row-indexed method reads _view; only reload() and _project() touch
        # _rows. (The security combo pre-filters into _rows so a filtered row still
        # carries its correct running share balance; sort/filter layer on top.)
        self._view: list = []
        self._sort: tuple[int, int] = (self.DATE, Qt.AscendingOrder)
        self._filter: InvestmentFilter | None = None
        # The not-yet-accepted review row, edited in place at the bottom of the
        # register before it is committed -- the same gesture the cash register
        # offers. {"entry": ReviewEntry, "buf": {field: text}} or None.
        self._pending = None
        self.reload()

    # ---- pending review row ------------------------------------------------
    _PENDING_FIELDS = ("date", "action", "security", "quantity", "price",
                       "amount", "memo")

    def set_pending(self, entry):
        """Open an editable row at the bottom for a NEW review entry, seeded from
        what the importer read. The user corrects it there -- security, action,
        shares, price -- and accepts; nothing is written until then."""
        m = entry.mapped
        # The importer's action is a lookup-table guess over the source's own
        # activity vocabulary. The action TREE has seen every accept the user
        # ever made (bootstrapped from register history), so when it is
        # high-confidence for this description it seeds the editable row --
        # visibly, correctably -- and the review list keeps showing the raw
        # ground truth. Silent or unsure, the importer's mapping stands.
        # One implementation, shared with Accept All (import_review.accept_all),
        # so the bulk path cannot drift from what this row displays.
        action = import_review.predict_action(self.conn, m)
        self.beginResetModel()
        self._pending = {"entry": entry, "buf": {
            "date": m.date or "",
            "action": action,
            "security": m.symbol or "",
            "quantity": str(m.quantity or ""),
            "price": str(m.price or ""),
            "amount": fmt_cents(abs(m.amount_cents or 0)),
            # The DESCRIPTION of a cash line lives in memo. investment_transactions
            # has no category_id at all, so this is the only free text such a row
            # carries, and the Security/Category column falls back to it (see
            # investments.category_display). Without it in the buffer, whatever
            # the editor put there was dropped on the way back to the row.
            "memo": m.memo or "",
        }}
        self.endResetModel()

    def pending_set_memo(self, text) -> None:
        """Set the pending row's memo (a cash line's category) and redraw."""
        if self._pending is None:
            return
        self._pending["buf"]["memo"] = str(text or "").strip()
        row = self.pending_row()
        self.dataChanged.emit(self.index(row, 0),
                              self.index(row, len(self.HEADERS) - 1))

    def clear_pending(self):
        if self._pending is None:
            return
        self.beginResetModel()
        self._pending = None
        self.endResetModel()

    def has_pending(self) -> bool:
        return self._pending is not None

    def pending_row(self) -> int:
        """Index of the pending row -- always last, after the shown history."""
        return len(self._view)

    def is_pending_row(self, row) -> bool:
        return self._pending is not None and row == len(self._view)

    def pending_entry(self):
        return self._pending["entry"] if self._pending else None

    def pending_values(self) -> dict:
        """The edited fields, as import_review.save_new takes them. Amount keeps
        the SIGN the import derived: an edit changes the magnitude, and a fee or
        sale stays money-out."""
        if self._pending is None:
            return {}
        buf = self._pending["buf"]
        original = self._pending["entry"].mapped.amount_cents or 0
        cents = parse_amount(buf.get("amount"))
        if original < 0:
            cents = -abs(cents)
        return {
            "date": _to_iso(buf.get("date")),
            "action": (buf.get("action") or "").strip(),
            "symbol": (buf.get("security") or "").strip(),
            "quantity": (buf.get("quantity") or "").strip(),
            "price": (buf.get("price") or "").strip(),
            "amount_cents": cents,
            "memo": (buf.get("memo") or "").strip() or None,
        }

    def _pending_text(self, col) -> str:
        buf = self._pending["buf"]
        if col == self.SECURITY:
            # The column does double duty exactly as it does for a posted row
            # (investments.category_display): a trade shows its security, a cash
            # line shows its DESCRIPTION. A dividend or a fee names no security,
            # and leaving the cell blank hid the only text such a row carries.
            return buf.get("security") or buf.get("memo") or ""
        key = {self.DATE: "date", self.ACTION: "action",
               self.QUANTITY: "quantity", self.PRICE: "price",
               self.INV_AMT: "amount"}.get(col)
        if key is None:
            return ""
        val = buf.get(key, "")
        return fmt_date(val) if key == "date" else val

    def set_symbol_filter(self, symbol):
        """Restrict the register to one security (``None`` shows all). The running
        ``share_bal`` per row is derived over the FULL history first (in
        :func:`mammon.investments.register_rows`), so filtering afterward keeps each
        kept row's correct running share balance for that security."""
        self._symbol_filter = symbol or None
        self.reload()

    def reload(self):
        self.beginResetModel()
        # register_rows augments each txn with share_bal / inv_amt / cash_amt /
        # cash_bal so the view stays a thin projection.
        rows = investments.register_rows(self.conn, self.account_id)
        if self._symbol_filter is not None:
            rows = [r for r in rows if r["symbol"] == self._symbol_filter]
        self._rows = rows
        self._view = self._project()
        self.endResetModel()

    # ---- sort + filter (the view's projection, parity with the cash register) --
    def sort(self, column, order=Qt.AscendingOrder):   # QAbstractItemModel API
        self.set_sort(column, order)

    def set_sort(self, column, order=Qt.AscendingOrder) -> None:
        """Order the displayed rows by ``column``. Date ascending is the ledger
        order itself, so it is the identity projection; Date descending its
        reverse. Ties in any other column fall back to (date, id), so the result
        is stable across reloads."""
        column = int(column)
        order = Qt.DescendingOrder if order == Qt.DescendingOrder else Qt.AscendingOrder
        if (column, order) == self._sort:
            return
        self._sort = (column, order)
        self._reproject()

    def sort_state(self) -> tuple[int, int]:
        return self._sort

    def set_filter(self, flt: "InvestmentFilter | None") -> None:
        """Narrow the displayed rows to those ``flt`` matches (``None`` or an
        empty filter shows everything). The pending review row is unaffected."""
        if flt is not None and flt.is_empty():
            flt = None
        if flt == self._filter:
            return
        self._filter = flt
        self._reproject()

    def filter_state(self) -> "InvestmentFilter | None":
        return self._filter

    def view_counts(self) -> tuple[int, int]:
        """``(shown, total)`` real rows -- the filter bar's 'Showing n of m'."""
        return len(self._view), len(self._rows)

    def _reproject(self) -> None:
        self.beginResetModel()
        self._view = self._project()
        self.endResetModel()

    def _project(self) -> list:
        rows = self._rows
        flt = self._filter
        if flt is not None and not flt.is_empty():
            rows = [r for r in rows if flt.matches(r)]
        column, order = self._sort
        if column == self.DATE:
            return list(rows) if order == Qt.AscendingOrder else list(reversed(rows))
        return sorted(rows, key=self._sort_key(column),
                      reverse=(order == Qt.DescendingOrder))

    def _sort_key(self, column):
        """A total order for one column, (date, id) breaking ties. Quantity/Price/
        Share Bal sort by their Decimal magnitude (blank counts as 0); the money
        columns by their signed cents; Action and Security/Category by text."""
        def dec(key):
            def f(r):
                v = r.get(key)
                if v in (None, ""):
                    return Decimal(0)
                try:
                    return Decimal(str(v))
                except (InvalidOperation, ValueError):
                    return Decimal(0)
            return f

        def cents(key):
            return lambda r: int(r.get(key) or 0)

        keys = {
            self.ACTION: lambda r: (self._action_label(r) or "").lower(),
            self.SECURITY: lambda r: (r.get("symbol") or r.get("category_label") or "").lower(),
            self.QUANTITY: dec("quantity"),
            self.PRICE: dec("price"),
            self.SHARE_BAL: dec("share_bal"),
            self.INV_AMT: cents("inv_amt"),
            self.CASH_AMT: cents("cash_amt"),
            self.CASH_BAL: cents("cash_bal"),
        }
        primary = keys.get(column, lambda r: r["date"])
        return lambda r: (primary(r), r["date"], r["id"])

    # ---- multi-row batch edits, void, find/replace (parity) ---------------
    def txn_ids_at(self, rows) -> list[int]:
        """The ``investment_transactions`` ids behind display rows, skipping the
        pending row and any cash-only transfer leg. A cash leg's id belongs to the
        ``transactions`` table -- a DIFFERENT id space -- so feeding it to
        :func:`investments.get_investment_txn` would silently hit an unrelated
        investment row (the shared-id-space hazard)."""
        out = []
        for row in rows:
            t = self.txn_at(row)
            if t and not t.get("cash_leg"):
                out.append(int(t["id"]))
        return out

    def apply_to_investments(self, txn_ids, fn) -> tuple[int, int]:
        """Run ``fn(txn_row)`` for each id; ``fn`` returns True when it changed the
        row, False to report it skipped (a ValueError/KeyError counts as skipped
        too). Returns ``(changed, skipped)``. Does NOT rebuild holdings or reload
        -- the widget does that once, after (matching the single-edit path)."""
        changed = skipped = 0
        for tid in txn_ids:
            t = investments.get_investment_txn(self.conn, tid)
            if t is None:
                continue
            try:
                ok = fn(t)
            except (ValueError, KeyError):
                ok = False
            if ok:
                changed += 1
            else:
                skipped += 1
        return changed, skipped

    def batch_set_memo(self, txn_ids, text: str) -> tuple[int, int]:
        """Set the memo on many investment rows (``""`` clears it)."""
        value = (text or "").strip() or None
        return self.apply_to_investments(
            txn_ids,
            lambda t: investments.update_investment_fields(self.conn, t["id"], memo=value))

    def batch_void(self, txn_ids) -> tuple[int, int]:
        return self.apply_to_investments(
            txn_ids, lambda t: investments.void_investment(self.conn, t["id"]))

    def batch_delete(self, txn_ids) -> tuple[int, int]:
        return self.apply_to_investments(
            txn_ids, lambda t: investments.delete_investment(self.conn, t["id"]))

    def void_row(self, row) -> bool:
        """Quicken's Void on one investment row (see
        :func:`investments.void_investment`)."""
        t = self.txn_at(row)
        if t is None or t.get("cash_leg"):
            return False
        return investments.void_investment(self.conn, t["id"])

    def find_replace(self, field: str, find: str, replace: str) -> int:
        """Case-insensitive substring find-and-replace over one text field
        (``memo`` or ``symbol``) across this account's shown rows, each change
        written through :func:`investments.update_investment_fields`. Returns the
        number of rows changed. (The security field also has the richer, holding-
        fusing rename in :func:`investments.plan_security_rename`; this is the memo
        counterpart plus a plain symbol substitution.)"""
        if field not in ("memo", "symbol"):
            raise ValueError(f"cannot find/replace {field!r}")
        needle = find or ""
        if not needle:
            return 0
        low = needle.lower()
        changed = 0
        for r in list(self._rows):
            if r.get("cash_leg"):
                continue
            current = r.get(field) or ""
            if low not in current.lower():
                continue
            new = re.sub(re.escape(needle), lambda _m: replace, current,
                         flags=re.IGNORECASE)
            investments.update_investment_fields(
                self.conn, int(r["id"]), **{field: new or None})
            changed += 1
        return changed

    def account_name(self) -> str:
        acct = ledger.get_account(self.conn, self.account_id)
        return acct["name"] if acct else ""

    def txn_at(self, row):
        return self._view[row] if 0 <= row < len(self._view) else None

    def row_for_txn(self, txn_id) -> int:
        """Index of the row for ``txn_id`` in the CURRENT (security-filtered,
        sorted, text-filtered) view, or -1 if it is not shown."""
        for i, r in enumerate(self._view):
            if r["id"] == txn_id:
                return i
        return -1

    # ---- QAbstractTableModel API -----------------------------------------
    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._view) + (1 if self._pending is not None else 0)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def flags(self, index):
        # Posted rows are read-only (inline editing of lots is out of scope); the
        # PENDING review row is editable, which is the whole point of it.
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if self.is_pending_row(index.row()):
            if index.column() in (self.DATE, self.ACTION, self.SECURITY,
                                  self.QUANTITY, self.PRICE, self.INV_AMT):
                return base | Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        if not self.is_pending_row(index.row()):
            return False
        key = {self.DATE: "date", self.ACTION: "action", self.SECURITY: "security",
               self.QUANTITY: "quantity", self.PRICE: "price",
               self.INV_AMT: "amount"}.get(index.column())
        if key is None:
            return False
        if key == "security" and not self._pending["buf"].get("security"):
            # The cell is showing the DESCRIPTION of a cash line, so an edit there
            # edits that -- writing it to `security` would invent a holding out of
            # a fee.
            key = "memo"
        text = _to_iso(value) if key == "date" else str(value or "").strip()
        self._pending["buf"][key] = text
        row = self.pending_row()
        self.dataChanged.emit(self.index(row, 0),
                              self.index(row, len(self.HEADERS) - 1))
        return True

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col = index.column()
        if self.is_pending_row(index.row()):
            if role in (Qt.DisplayRole, Qt.EditRole):
                return self._pending_text(col)
            if role == Qt.TextAlignmentRole and col in self._NUMERIC:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if role == Qt.ForegroundRole:
                ct = style.cell_text_color()
                return QBrush(QColor(ct)) if ct else None
            return None
        if not (0 <= index.row() < len(self._view)):
            return None  # stale/out-of-range index after a shrink+reload
        r = self._view[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            return self._cell_text(r, col)
        if role == Qt.ToolTipRole and col == self.SECURITY and r["symbol"]:
            # The cell shows the IDENTITY (the ticker); the description lives in
            # `securities.name` and is hovered rather than spelled out, because
            # this register is deliberately dense and "VGT -- VANGUARD INFO TECH
            # ETF" in every row buys nothing over a tooltip. Before the
            # ticker/name split the column had to carry both at once, which is
            # what let one holding end up under two spellings.
            from mammon import securities as _sec
            return _sec.name_of(self.conn, r["symbol"])
        if role == Qt.TextAlignmentRole:
            if col in self._NUMERIC:
                return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            # Cash Amt / Cash Bal go red when negative (Quicken); Inv Amt is a
            # gross magnitude and stays neutral.
            if col == self.CASH_AMT and (r["cash_amt"] or 0) < 0:
                return QBrush(QColor(style.negative_color()))
            if col == self.CASH_BAL and (r["cash_bal"] or 0) < 0:
                return QBrush(QColor(style.negative_color()))
            ct = style.cell_text_color()  # legible item text in dark mode; None in light
            if ct:
                return QBrush(QColor(ct))
        return None

    def _action_label(self, r) -> str:
        # Quicken shows a bare investment-account 'Cash' entry as a misc income /
        # expense line, and carries the category alongside (our Security/Category
        # column). Positive cash reads as MiscInc, a fee (negative) as MiscExp;
        # every other action shows verbatim as imported.
        act = r["action"] or ""
        if act.strip().lower() == "cash":
            label = "MiscInc" if (r.get("cash_amt") or 0) >= 0 else "MiscExp"
        else:
            label = act
        # A voided row has no payee to stamp (investment_transactions has none),
        # so the **VOID** mark lives on the memo; surface it on the Action cell --
        # the register's parallel to the cash register's voided payee.
        if investments.is_void_investment(r):
            return f"{ledger.VOID_PREFIX} {label}".strip()
        return label

    def _cell_text(self, r, col) -> str:
        if col == self.DATE:
            return fmt_date(r["date"])
        if col == self.ACTION:
            return self._action_label(r)
        if col == self.SECURITY:
            # A row names EITHER a security OR a category/transfer destination,
            # never both: show the symbol for a trade, else the [transfer]/
            # category the cash line points at (register_rows.category_label).
            return r["symbol"] or r.get("category_label") or ""
        if col == self.QUANTITY:
            # A split's stored quantity is an ENCODING (new shares per TEN old),
            # not a share count: 80 for an 8-for-1. Showing it raw told the
            # holder of 26 shares nothing -- the ratio the broker announced does.
            if (r["action"] or "").strip().lower() == "stksplit":
                return investments.split_display(r)
            return fmt_qty(r["quantity"])
        if col == self.PRICE:
            return fmt_qty(r["price"])
        if col == self.SHARE_BAL:
            # None on cash-only / non-share-moving rows (Quicken leaves it blank).
            return fmt_qty(r["share_bal"])
        if col == self.INV_AMT:
            return fmt_cents(r["inv_amt"]) if r["inv_amt"] is not None else ""
        if col == self.CASH_AMT:
            # Blank for cash-neutral rows (ReinvDiv, share transfers) where the
            # cash effect is 0 -- matches Quicken.
            return fmt_cents(r["cash_amt"]) if r["cash_amt"] else ""
        if col == self.CASH_BAL:
            return fmt_cents(r["cash_bal"])
        return ""


def _norm_action(value) -> str:
    return str(value or "").strip().upper()


def _fmv_cents(price, quantity):
    """Fair market value in cents from a per-unit price and a quantity, or 0 when
    the source recorded no price. Used to SEED a row that is being turned into a
    trade: an imported coin row states what the coin was worth, which is a far
    better starting point than an empty cell the user is blocked on."""
    if not price:
        return 0
    try:
        value = Decimal(str(price)) * abs(Decimal(str(quantity or "0")))
    except (InvalidOperation, ValueError):
        return 0
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _coin_qty(text):
    """A typed coin quantity as a positive :class:`Decimal`, or ``None`` when the
    cell is blank, unparseable or zero. Quantities are Decimal text everywhere in
    the crypto layer (fractional and multi-decimal precision is the point), so
    the blank quick-entry row parses them as Decimals, never cents."""
    s = str(text or "").replace(",", "").strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return abs(d) if d != 0 else None


class CryptoRegisterModel(QAbstractTableModel):
    """One crypto wallet's activity as a table, the crypto twin of
    :class:`InvestmentRegisterModel`.

    A crypto account's events (Buy/Sell, a coin-for-coin SWAP as two linked legs,
    a same-coin wallet TRANSFER mirror, Send/income, gas Fees) live in the
    ``crypto_transactions`` table -- neither the cash ``transactions`` table nor
    ``investment_transactions`` -- so it needs its own projection. This is a THIN
    projection over :func:`mammon.crypto.register_rows`, which derives the running
    per-coin balance, the fiat cash-sleeve balance, the transfer/swap column label
    and the gas ``fee`` label; the view holds NO quantity or cents math. A swap's
    two legs render with a shared ``OUT->IN`` label so they read as one paired
    trade; a wallet transfer renders as ``[Other Wallet]`` (the mirror model, in
    coin).

    BEHAVIORAL PARITY WITH THE CASH REGISTER. Events still arrive mostly by
    import, but the register is no longer read-only: a trailing BLANK quick-entry
    row (:meth:`commit_blank`) enters a new event by hand through
    :mod:`mammon.crypto`, the correctable posted columns edit inline, and the
    widget's context menu edits/deletes a row through :meth:`apply_edit` /
    :meth:`delete_txn`. Only the CONTENT differs from the cash register (coin
    quantities as Decimal text, not USD cents; a wallet has no price/amount/cash
    column); the behavior is the same, and every write still funnels through
    :mod:`mammon.crypto`, the sole writer of the ``crypto_*`` tables.

    THE COLUMN SET DEPENDS ON THE ACCOUNT KIND, and that is the whole point of the
    redesign. An EXCHANGE account is custodial: it holds a fiat cash sleeve, buys
    and sells coin for dollars, and its rows genuinely carry a per-unit price, a
    signed dollar amount and a running cash balance. A WALLET is a single address:
    coin arrives and leaves, the network fee is paid IN THE COIN, and there is no
    dollar leg anywhere -- so Price, Amount and Cash Bal are not merely empty for
    it, they are meaningless, and showing them invites a reading of the register
    that is simply false. Its increases and decreases get their own columns (the
    source's Value_IN / Value_OUT), because on-chain those are different events
    with different counterparties and one signed column hides that.

    USD is not absent from a wallet, it is just not on the ROW: each coin is a
    security valued at quantity x market price, and that valuation happens at the
    holdings and net-worth layers."""

    # Stable column KEYS. They are not positions: each kind lists the keys it
    # shows, in order, and the model maps position -> key. The exchange list is
    # deliberately keys 0..9 in order, so the constants still read as indices for
    # an exchange account (and every existing caller keeps working).
    (DATE, ACTION, COIN, PAYEE, QUANTITY, PRICE,
     COIN_BAL, AMOUNT, CASH_BAL, FEE, COIN_IN, COIN_OUT, MEMO,
     TRANSFER) = range(14)

    # The COIN column does double duty: a trade/income shows its coin symbol, a
    # wallet transfer shows [Other Wallet], a swap shows the OUT->IN pair. PAYEE is
    # the on-chain counterparty (From on a coin credit, To on a coin debit) that
    # crypto.register_rows carries straight through from crypto_transactions.payee.
    _HEADER_TEXT = {
        DATE: "Date", ACTION: "Action", COIN: "Coin / Wallet", PAYEE: "Payee",
        QUANTITY: "Quantity", PRICE: "Price", COIN_BAL: "Coin Bal",
        AMOUNT: "Amount", CASH_BAL: "Cash Bal", FEE: "Fee",
        COIN_IN: "Coin In", COIN_OUT: "Coin Out", MEMO: "Memo",
        TRANSFER: "Transfer",
    }
    # MEMO is APPENDED rather than slotted in beside Payee, so the ten constants
    # above still equal their own positions in this layout and every existing
    # caller keeps working. It has to be here at all because an exchange's source
    # says things its columns cannot ("Sold 2 ETH for 1222.01 USD", "Withdrawal
    # to <bank>") -- that sentence is often the only record of what a row was.
    _EXCHANGE_COLUMNS = (DATE, ACTION, COIN, PAYEE, TRANSFER, QUANTITY, PRICE,
                         COIN_BAL, AMOUNT, CASH_BAL, FEE, MEMO)
    _WALLET_COLUMNS = (DATE, ACTION, COIN, PAYEE, TRANSFER, MEMO,
                       COIN_OUT, COIN_IN, COIN_BAL, FEE)
    # Kept for the exchange layout's callers; HEADERS is the exchange header row.
    # Spelled out rather than derived: a class-body comprehension cannot see the
    # class's own names.
    HEADERS = ["Date", "Action", "Coin / Wallet", "Payee", "Quantity", "Price",
               "Coin Bal", "Amount", "Cash Bal", "Fee"]
    _NUMERIC = (QUANTITY, PRICE, COIN_BAL, AMOUNT, CASH_BAL, COIN_IN, COIN_OUT)

    committed = pyqtSignal()          # a write hit the DB; refresh siblings
    error = pyqtSignal(str)           # a write failed; surface it to the user

    # What may be corrected on a row that is ALREADY POSTED. An import is not an
    # oracle -- an address is mistyped, a coin symbol comes through wrong, a
    # source dates a row a day off -- and a register you cannot fix is a register
    # you cannot trust, so EVERY field the row legitimately has is editable, both
    # here inline and through the Edit dialog. The ONLY cells that stay read-only
    # are the DERIVED running balances (Coin Bal, Cash Bal): they are computed
    # from the events, never stored, so there is nothing there to edit. The Coin
    # In / Coin Out pair edits the ONE signed quantity between them, so only the
    # side the row's direction currently uses is offered (see flags); the
    # direction itself is changed through the Action cell, which re-signs the
    # magnitude to match (see _write_action). PAYEE is free text; the TRANSFER
    # gesture (naming one of your own accounts) lives in its own column and means
    # a transfer here exactly as it does in every other register.
    _POSTED_EDITABLE = (DATE, ACTION, COIN, PAYEE, TRANSFER, MEMO,
                        COIN_OUT, COIN_IN, FEE)
    # An EXCHANGE row shows the fiat trade instead of the coin-in/out pair, so its
    # editable set is the signed Quantity, the per-unit Price and the fiat Amount
    # rather than Coin In / Coin Out. Everything else matches the wallet: an
    # imported coin-only row can be turned into a full trade by filling Price and
    # Amount, and a mis-recorded direction is flipped through Action.
    _POSTED_EDITABLE_EXCHANGE = (DATE, ACTION, COIN, PAYEE, TRANSFER,
                                 QUANTITY, PRICE, AMOUNT, FEE, MEMO)

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        self._rows: list = []
        self._symbol_filter = None        # None -> show every coin
        self.is_wallet = crypto.is_wallet_account(
            crypto.get_account(conn, account_id))
        self._columns = (self._WALLET_COLUMNS if self.is_wallet
                         else self._EXCHANGE_COLUMNS)
        # The not-yet-accepted review row, shown at the bottom of the register
        # before it is committed -- the same gesture the cash and investment
        # registers offer. {"entry": ReviewEntry, "buf": {field: text}} or None.
        self._pending = None
        # The blank quick-entry row's typed values, {field: text}. Always present
        # (rowCount adds one trailing blank row) so a new crypto transaction can
        # be entered by hand, exactly as the cash register's blank row allows.
        self._new: dict = {}
        self.reload()

    # ---- pending review row ------------------------------------------------
    # What a user may CORRECT on a chain row, and nothing more. Date, coin,
    # quantity and fee are facts the chain reported -- editing them would be
    # inventing history, and the review list is meant to be ground truth. What is
    # genuinely a judgement is the ACTION (the chain cannot tell a plain receive
    # from a staking reward, an airdrop or mined coin), the PAYEE (an address is
    # the truthful default, but a name the user recognises is more useful), and
    # the MEMO. Those three are exactly what import_review._save_crypto accepts
    # as overrides.
    _PENDING_EDITABLE = (ACTION, PAYEE, TRANSFER, MEMO)
    # An EXCHANGE row carries fiat the source may have got wrong (a trade's cash
    # leg, a deposit), so its AMOUNT is correctable too. A wallet's is not: there
    # is no amount on a wallet row to correct.
    _PENDING_EDITABLE_EXCHANGE = (ACTION, PAYEE, TRANSFER, MEMO, AMOUNT)

    # The trailing BLANK quick-entry row -- the cash register's manual-entry
    # gesture, brought to the crypto register. Its editable cells are the ones a
    # user fills to state a brand-new event; the running balances (Coin Bal, Cash
    # Bal) are DERIVED and never typed, so they stay read-only even here. A wallet
    # types its increase or decrease into Coin In / Coin Out (coin-native, no USD
    # on the row); an exchange types a signed Quantity/Price/Amount trade.
    _BLANK_EDITABLE = (DATE, ACTION, COIN, PAYEE, TRANSFER, MEMO,
                       COIN_OUT, COIN_IN, FEE)
    _BLANK_EDITABLE_EXCHANGE = (DATE, ACTION, COIN, PAYEE, TRANSFER,
                                QUANTITY, PRICE, AMOUNT, FEE, MEMO)

    def set_pending(self, entry) -> None:
        """Open the not-yet-accepted row at the bottom for a NEW review entry,
        seeded from what the importer read off the chain.

        The PAYEE is auto-filled from the learned rename tree exactly as the cash
        register's pending row is (payee resolved first): when the user has
        already renamed this counterparty before, the friendly name appears here
        instead of the raw address. With nothing learned yet the address stands
        as the honest default -- the tree only ever fills from CORRECTIONS, so it
        never invents a name for an address the user has not taught it."""
        m = entry.mapped
        self.beginResetModel()
        self._pending = {"entry": entry, "buf": {
            "action": (m.action or "RECEIVE").upper(),
            "payee": import_review.predict_crypto_payee(self.conn, m),
            "memo": m.memo or "",
            "transfer": "",
            "amount": fmt_cents(abs(m.amount_cents or 0)) if m.amount_cents else "",
        }}
        self.endResetModel()

    def _pending_editable(self):
        return (self._PENDING_EDITABLE if self.is_wallet
                else self._PENDING_EDITABLE_EXCHANGE)

    def clear_pending(self) -> None:
        if self._pending is None:
            return
        self.beginResetModel()
        self._pending = None
        self.endResetModel()

    def has_pending(self) -> bool:
        return self._pending is not None

    def pending_row(self) -> int:
        """Index of the pending row -- after the real history, before the blank
        quick-entry row (which is always last, exactly as the cash register lays
        its pending and blank rows out)."""
        return len(self._rows) if self._pending is not None else -1

    def is_pending_row(self, row) -> bool:
        return self._pending is not None and row == len(self._rows)

    # ---- blank quick-entry row -------------------------------------------
    def blank_row(self) -> int:
        """Index of the trailing blank quick-entry row -- always last, after the
        real history and any pending review line."""
        return len(self._rows) + (1 if self._pending is not None else 0)

    def is_blank_row(self, row) -> bool:
        return row == self.blank_row()

    def _blank_editable(self):
        return (self._BLANK_EDITABLE if self.is_wallet
                else self._BLANK_EDITABLE_EXCHANGE)

    def pending_entry(self):
        return self._pending["entry"] if self._pending else None

    def pending_values(self) -> dict:
        """The edited fields, as :func:`import_review.save_new` takes them. No
        quantity, price or amount: a wallet row's numbers come from the chain and
        are not editable, and there is no fiat leg to state."""
        if self._pending is None:
            return {}
        buf = self._pending["buf"]
        values = {
            "action": (buf.get("action") or "").strip().upper(),
            "payee": (buf.get("payee") or "").strip(),
            "memo": (buf.get("memo") or "").strip() or None,
            # Consumed by the widget AFTER the row is saved: a transfer needs a
            # real transaction id on both sides before the two can be linked.
            "transfer_account": (buf.get("transfer") or "").strip() or None,
        }
        if not self.is_wallet:
            # The fiat leg keeps the SIGN the import derived: an edit changes the
            # magnitude, and money out of the sleeve stays money out.
            original = self._pending["entry"].mapped.amount_cents or 0
            cents = parse_amount(buf.get("amount"))
            values["amount_cents"] = -abs(cents) if original < 0 else cents
        return values

    def pending_actions(self) -> list:
        """The actions the pending row may take, given the DIRECTION the chain
        already fixed. A coin-out row can only be a send; a coin-in row is where
        the real choice lives (receive / reward / interest / airdrop / mining /
        fork), because the chain shows coin arriving and cannot say why."""
        if self._pending is None:
            return []
        act = (self._pending["entry"].mapped.action or "").strip().upper()
        if not self.is_wallet:
            # An exchange's vocabulary is the full one: a row can be a trade, a
            # transfer between the user's venues, in-kind income, or a bare cash
            # movement -- and which it is is exactly what the source's product
            # names leave ambiguous. Offer the actions of the SAME direction, so
            # a coin-out row cannot be turned into a deposit by a mis-click.
            if act in crypto.CASH_ACTIONS:
                return sorted(crypto.CASH_ACTIONS)
            if act in crypto.REMOVE_ACTIONS:
                return sorted(crypto.REMOVE_ACTIONS)
            return sorted(crypto.ADD_ACTIONS)
        if act in crypto.WALLET_DEBIT_ACTIONS:
            return sorted(crypto.WALLET_DEBIT_ACTIONS)
        return sorted(crypto.WALLET_CREDIT_ACTIONS)

    _TRANSFER_RE = re.compile(r"^\s*\[(?P<name>.+?)\]\s*$")

    def _write_posted(self, index, value) -> bool:
        """Commit one correction to an already-posted event.

        Every write goes through :func:`mammon.crypto.update_event` (or
        :func:`link_as_transfer`), so ``crypto.py`` stays the sole writer of the
        ``crypto_*`` tables and the transfer-mirror invariants keep being
        enforced in exactly one place.

        The reload is DEFERRED and failures are reported through :attr:`error`
        rather than a message box, for the same reason: both a model reset and a
        modal would run inside the editor's teardown -- the ``setModelData`` heap
        hazard CLAUDE.md records -- freeing the editor the frame still holds."""
        if not (0 <= index.column() < len(self._columns)):
            return False
        col = self._columns[index.column()]
        if col not in (self._POSTED_EDITABLE if self.is_wallet
                       else self._POSTED_EDITABLE_EXCHANGE):
            return False
        row = self.txn_at(index.row())
        if row is None:
            return False
        txn_id = int(row["id"])
        text = str(value or "").strip()
        try:
            if col == self.PAYEE:
                # Free text, exactly as in the cash register. A payee is a NAME,
                # not a link; resolving an account here was the wrong field.
                crypto.update_event(self.conn, txn_id, payee=text or None)
            elif col == self.DATE:
                # value is a QDate from the calendar editor (or typed text);
                # _to_iso normalizes both to ISO the domain layer stores.
                iso = _to_iso(value)
                if iso:
                    crypto.update_event(self.conn, txn_id, date=iso)
            elif col == self.COIN:
                crypto.update_event(self.conn, txn_id, symbol=text.upper() or None)
            elif col == self.QUANTITY:
                # A signed column, but the sign is the ACTION's, not the user's:
                # a magnitude is typed and the direction re-signs it, exactly as
                # the Edit dialog does, so quantity can never disagree with action.
                signed = self._signed_qty(row, text)
                if signed is not None:
                    crypto.update_event(self.conn, txn_id, quantity=signed)
            elif col in (self.COIN_IN, self.COIN_OUT):
                # The two wallet columns are one signed quantity; the column the
                # user typed into fixes the sign (In = +, Out = -). Only the
                # active side is editable (see flags), so this keeps direction.
                mag = _coin_qty(text)
                if mag is not None:
                    signed = -mag if col == self.COIN_OUT else mag
                    crypto.update_event(self.conn, txn_id, quantity=str(signed))
            elif col == self.PRICE:
                crypto.update_event(self.conn, txn_id, price=text or None)
            elif col == self.FEE:
                fsym, fqty = self._parse_fee(text, row["symbol"])
                crypto.update_event(self.conn, txn_id,
                                    fee_symbol=fsym, fee_quantity=fqty)
            elif col == self.TRANSFER:
                self._write_transfer(row, txn_id, text)
            elif col == self.AMOUNT:
                # UNLINK FIRST. The cash leg in the other account was created
                # from this row's amount and is found by it, so changing the
                # amount before removing it leaves the old leg unfindable -- and
                # re-linking then adds a SECOND one, double-counting the money.
                other = row["transfer_account_id"]
                if other is not None:
                    crypto.unlink_transfer(self.conn, txn_id)
                crypto.update_event(self.conn, txn_id,
                                    amount=self._signed_amount(row, text))
                if other is not None:
                    crypto.link_as_transfer(self.conn, txn_id, int(other))
            elif col == self.MEMO:
                crypto.update_event(self.conn, txn_id, memo=text or None)
            elif col == self.ACTION:
                self._write_action(row, txn_id, text)
        except Exception as exc:                    # a domain refusal, surfaced
            self.error.emit(str(exc))
            return False
        QTimer.singleShot(0, self._reload_and_notify)
        return True

    def _signed_amount(self, row, text):
        """An edited fiat amount, keeping the sign the ACTION implies: a purchase
        or withdrawal is money out, a sale or deposit money in (same table as the
        dialog's own ``_signed_amount``). ``None`` clears it."""
        cents = parse_amount(text)
        if not str(text or "").strip():
            return None
        act = _norm_action(row["action"])
        if act in ("BUY", "BUYX", "WITHDRAW"):
            return -abs(cents)
        if act in ("SELL", "SELLX", "DEPOSIT"):
            return abs(cents)
        return cents

    def _signed_qty(self, row, text):
        """An edited coin quantity as signed Decimal TEXT, the sign taken from the
        row's ACTION (a remove is negative, an add positive) so the stored sign
        can never contradict the direction. ``None`` when the cell is blank."""
        mag = _coin_qty(text)
        if mag is None:
            return None
        act = _norm_action(row["action"])
        return str(-mag if act in crypto.REMOVE_ACTIONS else mag)

    def _write_action(self, row, txn_id, text) -> None:
        """Set the action, and keep the row's SIGNED numbers consistent with it.

        Changing the action can REVERSE the movement's direction -- SEND becomes
        RECEIVE, BUY becomes SELL. When it does, the stored quantity keeps its
        magnitude but takes the new direction's sign, and a trade's fiat amount
        flips with it (a buy is money out, a sale money in). Coin In / Coin Out
        render from the quantity's sign and the cash column from the amount's, so
        without this an action-only edit would leave the row saying one thing in
        its Action cell and the opposite in its coin/cash cells. That
        inconsistency is exactly why the direction used to be un-editable at all;
        re-signing here is what makes SEND <-> RECEIVE safe to allow.

        Deliberately NOT enforced against the Transfer field. A trade-with-a-
        transfer is ONE concept spread over two cells, and a register is edited
        one cell at a time -- refusing an X action for want of a transfer while
        also refusing the transfer for want of a trade is a deadlock the user
        cannot get out of. So an incomplete state is allowed to exist between two
        keystrokes; a row turning INTO a trade with no proceeds is seeded from
        price x quantity rather than left at nothing for the user to be blocked
        on. They can correct it -- the Amount cell is editable for that reason."""
        act = (text or "").strip().upper()
        old = _norm_action(row["action"])
        flipping = ((old in crypto.REMOVE_ACTIONS) != (act in crypto.REMOVE_ACTIONS))
        if row["transfer_account_id"] is not None and act not in crypto.CROSS_ACTIONS:
            # Leaving the X family withdraws the claim that the cash went
            # elsewhere, so the link goes with it.
            crypto.unlink_transfer(self.conn, txn_id)
        fields = {"action": act}
        if flipping and row["quantity"] not in (None, ""):
            fields["quantity"] = str(-Decimal(str(row["quantity"])))
        if act in ("BUY", "SELL", "BUYX", "SELLX"):
            if row["amount"]:
                mag = abs(int(row["amount"]))
                fields["amount"] = -mag if act.startswith("BUY") else mag
            else:
                seeded = _fmv_cents(row["price"], row["quantity"])
                if seeded:
                    fields["amount"] = -seeded if act.startswith("BUY") else seeded
        elif act in crypto.CASH_ACTIONS and row["amount"]:
            # A bare cash movement flips the SAME way: a deposit is money in, a
            # withdrawal money out. Its magnitude is kept, its sign re-derived.
            mag = abs(int(row["amount"]))
            fields["amount"] = -mag if act == "WITHDRAW" else mag
        crypto.update_event(self.conn, txn_id, **fields)

    def _write_transfer(self, row, txn_id, text) -> None:
        """Name the OTHER account this coin moved to or from -- or clear it.

        Coin moving between two accounts the user controls is not a disposal: it
        realizes no gain and its cost basis rides along. So this field does not
        store a string, it performs the link (:func:`crypto.link_as_transfer`),
        which adopts the counter-leg the other account already holds rather than
        minting a duplicate. Blanking it withdraws the CLAIM that the two rows
        are one movement; both rows stay, because both movements happened.

        Brackets are accepted but not required -- the cash register's Category
        cell needs them to tell an account from a category, and this column holds
        nothing but accounts."""
        name = self._TRANSFER_RE.sub(r"\g<name>", text).strip()
        # A row is linked when it has EITHER a paired crypto leg or a cash leg.
        # A cash leg has no transfer_pair_id -- its other half lives in
        # `transactions`, so there is no crypto id to pair with -- and testing
        # only for the pair left those links impossible to clear or change.
        linked = (row["transfer_pair_id"] is not None
                  or row["transfer_account_id"] is not None)
        if not name:
            if linked:
                crypto.unlink_transfer(self.conn, txn_id)
            return
        other = self._account_id_for_name(name)
        if other is None:
            raise ValueError(
                "No account named %r. This field names one of YOUR accounts, so "
                "the money or coin is recorded as moving between them rather "
                "than leaving your books." % name)
        if linked:
            crypto.unlink_transfer(self.conn, txn_id)
        crypto.link_as_transfer(self.conn, txn_id, other)

    def transfer_choices(self, row=None) -> list:
        """``[Account]`` entries for every OTHER account -- byte-identical to the
        transfer half of the cash register's :meth:`RegisterModel.category_choices`.

        Every account, not just the crypto ones. Coin leaves an exchange for a
        bank as readily as it moves between two wallets, and offering only crypto
        accounts made the common case -- a withdrawal to checking -- unsayable.
        Bracketed, for the same reason and in the same shape: the user is typing
        the gesture they already know from every other register."""
        return [f"[{a['name']}]"
                for a in ledger.list_accounts(self.conn, include_closed=True)
                if int(a["id"]) != int(self.account_id)]

    def _account_name(self, account_id):
        acct = ledger.get_account(self.conn, account_id)
        return (acct["name"] if acct else "") or None

    def _reload_and_notify(self):
        self.reload()
        self.committed.emit()

    # ---- blank quick-entry row: rendering, buffering and commit ----------
    def _blank_text(self, col) -> str:
        """One cell of the blank quick-entry row -- whatever the user has typed so
        far. Dates render in the user's chosen format; everything else is the raw
        buffered text (a coin quantity keeps full Decimal precision)."""
        if col == self.DATE:
            iso = self._new.get("date", "")
            return fmt_date(iso) if iso else ""
        key = {self.ACTION: "action", self.COIN: "coin", self.PAYEE: "payee",
               self.TRANSFER: "transfer", self.MEMO: "memo",
               self.COIN_IN: "coin_in", self.COIN_OUT: "coin_out",
               self.QUANTITY: "quantity", self.PRICE: "price",
               self.AMOUNT: "amount", self.FEE: "fee"}.get(col)
        return self._new.get(key, "") if key else ""

    def _set_blank(self, index, value) -> bool:
        """Buffer one typed cell of the blank quick-entry row. No DB write happens
        here -- the row commits through :meth:`commit_blank` on Enter, so a
        half-typed multi-field crypto event never posts itself the way a two-field
        cash row can."""
        if not (0 <= index.column() < len(self._columns)):
            return False
        col = self._columns[index.column()]
        if col not in self._blank_editable():
            return False
        key = {self.DATE: "date", self.ACTION: "action", self.COIN: "coin",
               self.PAYEE: "payee", self.TRANSFER: "transfer", self.MEMO: "memo",
               self.COIN_IN: "coin_in", self.COIN_OUT: "coin_out",
               self.QUANTITY: "quantity", self.PRICE: "price",
               self.AMOUNT: "amount", self.FEE: "fee"}.get(col)
        if key is None:
            return False
        if col == self.DATE:
            self._new[key] = _to_iso(value)
        else:
            text = str(value or "").strip()
            self._new[key] = text.upper() if key == "action" else text
        row = index.row()
        # The action decides which coin column a wallet quantity renders in, so
        # redraw the whole row rather than the one cell.
        self.dataChanged.emit(self.index(row, 0),
                              self.index(row, len(self._columns) - 1))
        return True

    def blank_values(self) -> dict:
        """The blank row's typed fields (inspection and tests)."""
        return dict(self._new)

    def _blank_ready(self) -> bool:
        """Whether the blank row carries enough to state a real event. A wallet
        needs a date, a coin and a non-zero Coin In or Coin Out; an exchange needs
        a date, an action and either an amount (a cash movement) or a coin +
        quantity (a trade or in-kind row)."""
        v = self._new
        if not v.get("date"):
            return False
        if self.is_wallet:
            return bool(v.get("coin")
                        and (_coin_qty(v.get("coin_in"))
                             or _coin_qty(v.get("coin_out"))))
        act = (v.get("action") or "").strip().upper()
        if not act:
            return False
        if act in crypto.CASH_ACTIONS:
            return bool(str(v.get("amount") or "").strip())
        return bool(v.get("coin") and _coin_qty(v.get("quantity")))

    def commit_blank(self) -> bool:
        """Record the blank quick-entry row as a new crypto event and reset it.

        Every write goes through :mod:`mammon.crypto` -- the SOLE writer of the
        ``crypto_*`` tables -- so no second write path is introduced and the
        transfer/holdings invariants stay enforced in one place. Returns False,
        leaving the buffer intact, when the row is not ready or a domain rule
        refuses it (surfaced through :attr:`error`)."""
        if not self._blank_ready():
            return False
        try:
            txn_id = self._create_from_blank()
        except Exception as exc:                    # a domain refusal, surfaced
            self.error.emit(str(exc))
            return False
        if not txn_id:
            return False
        crypto.rebuild_holdings(self.conn, self.account_id)
        self._new = {}
        self.reload()
        self.committed.emit()
        return True

    @staticmethod
    def _parse_fee(text, default_symbol):
        """Split a typed fee cell (``"0.001 ETH"`` or a bare ``"0.001"``) into a
        (symbol, quantity) pair. A bare number takes the row's own coin. Returns
        (None, None) when blank or unparseable."""
        s = str(text or "").strip()
        if not s:
            return None, None
        parts = s.split()
        qty = _coin_qty(parts[0])
        if qty is None:
            return None, None
        sym = parts[1].strip().upper() if len(parts) > 1 else (default_symbol or None)
        return sym, str(qty)

    def _create_from_blank(self) -> int:
        """Turn the blank row's buffer into a crypto event through the right
        :mod:`mammon.crypto` writer, and link a named transfer afterwards (both
        sides must be real rows before they can be paired). Returns the new txn
        id, or 0 when there is nothing to write."""
        v = self._new
        date = v["date"]
        symbol = (v.get("coin") or "").strip().upper()
        payee = (v.get("payee") or "").strip() or None
        memo = (v.get("memo") or "").strip() or None
        action = (v.get("action") or "").strip().upper()
        fee_sym, fee_qty = self._parse_fee(v.get("fee"), symbol)
        txn_id = 0
        if self.is_wallet:
            qin = _coin_qty(v.get("coin_in"))
            qout = _coin_qty(v.get("coin_out"))
            if qin:
                act = action if action in crypto.WALLET_CREDIT_ACTIONS else "RECEIVE"
                txn_id = crypto.record_wallet_credit(
                    self.conn, self.account_id, date, symbol, str(qin),
                    payee=payee, action=act, memo=memo)
            elif qout:
                act = action if action in crypto.WALLET_DEBIT_ACTIONS else "SEND"
                txn_id = crypto.record_wallet_debit(
                    self.conn, self.account_id, date, symbol, str(qout),
                    payee=payee, action=act, fee_symbol=fee_sym,
                    fee_quantity=fee_qty, memo=memo)
        else:
            amount_cents = (parse_amount(v.get("amount"))
                            if str(v.get("amount") or "").strip() else None)
            price = (v.get("price") or "").strip() or None
            if action in crypto.CASH_ACTIONS:
                signed = (-abs(amount_cents) if action == "WITHDRAW"
                          else abs(amount_cents or 0))
                txn_id = crypto.record_cash(
                    self.conn, self.account_id, date, signed,
                    action=action, payee=payee, memo=memo)
            else:
                qty = _coin_qty(v.get("quantity"))
                if not qty:
                    return 0
                if action == "BUY":
                    cost = (abs(amount_cents) if amount_cents is not None
                            else _fmv_cents(price, str(qty)))
                    txn_id = crypto.record_buy(
                        self.conn, self.account_id, date, symbol, str(qty), cost,
                        price=price, fee_symbol=fee_sym, fee_quantity=fee_qty,
                        memo=memo)
                elif action == "SELL":
                    proceeds = (abs(amount_cents) if amount_cents is not None
                                else _fmv_cents(price, str(qty)))
                    txn_id = crypto.record_sell(
                        self.conn, self.account_id, date, symbol, str(qty),
                        proceeds, price=price, fee_symbol=fee_sym,
                        fee_quantity=fee_qty, memo=memo)
                else:
                    # Any other add/remove action (income, transfer leg, ...):
                    # the low-level writer takes the signed quantity directly.
                    signed = (-qty if action in crypto.REMOVE_ACTIONS else qty)
                    txn_id = crypto.record_event(
                        self.conn, self.account_id, date, action, symbol=symbol,
                        quantity=str(signed), price=price, amount=amount_cents,
                        payee=payee, memo=memo, fee_symbol=fee_sym,
                        fee_quantity=fee_qty)
        transfer = (v.get("transfer") or "").strip()
        if txn_id and transfer:
            other = self._account_id_for_name(transfer)
            if other is not None:
                crypto.link_as_transfer(self.conn, int(txn_id), other)
        return int(txn_id or 0)

    # ---- context-menu edit / delete (through crypto.py) ------------------
    def delete_txn(self, txn_id) -> bool:
        """Delete a posted event (both legs of a transfer or all legs of a swap,
        per :func:`crypto.delete_event`) and refresh. The register's context-menu
        Delete, the crypto twin of the cash register's."""
        try:
            crypto.delete_event(self.conn, int(txn_id))
            crypto.rebuild_holdings(self.conn, self.account_id)
        except Exception as exc:
            self.error.emit(str(exc))
            return False
        self.reload()
        self.committed.emit()
        return True

    def apply_edit(self, txn_id, fields: dict) -> bool:
        """Apply the context-menu Edit dialog's changes through
        :func:`crypto.update_event` (the sole writer), then rebuild holdings and
        refresh. The scalar ``fields`` are already restricted to update_event's
        editable keys by the dialog.

        ``transfer`` is the exception: it is not a stored column but the LINK
        gesture (name one of your own accounts), so it is pulled out and applied
        through the same :meth:`_write_transfer` the inline Transfer cell uses --
        still :mod:`mammon.crypto`, still no second write path. It is only acted
        on when it actually CHANGED, so an untouched dialog never re-links (which
        would needlessly rebuild the paired cash leg)."""
        fields = dict(fields)
        transfer = fields.pop("transfer", None)
        try:
            crypto.update_event(self.conn, int(txn_id), **fields)
            if transfer is not None:
                row = crypto.get_event(self.conn, int(txn_id))
                if row is not None:
                    current = self._account_name(row["transfer_account_id"]) or ""
                    want = self._TRANSFER_RE.sub(
                        r"\g<name>", str(transfer or "")).strip()
                    if want.lower() != current.strip().lower():
                        self._write_transfer(row, int(txn_id), transfer)
            crypto.rebuild_holdings(self.conn, self.account_id)
        except Exception as exc:
            self.error.emit(str(exc))
            return False
        self.reload()
        self.committed.emit()
        return True

    def _account_id_for_name(self, name):
        """ANY account with this NAME, or ``None``. Matched case-insensitively,
        because the user is typing a name they can SEE in the sidebar, not an
        identifier. Not restricted to crypto accounts -- see
        :meth:`transfer_choices`."""
        want = self._TRANSFER_RE.sub(r"\g<name>", str(name or "")).strip().lower()
        for a in ledger.list_accounts(self.conn, include_closed=True):
            if (a["name"] or "").strip().lower() == want:
                return int(a["id"])
        return None

    def actions_for_row(self, row) -> list:
        """The actions offered at ``row`` -- the pending line's, a posted row's,
        or the blank quick-entry line's.

        A POSTED row may be flipped to the OPPOSITE direction now (SEND ->
        RECEIVE, BUY -> SELL): the user reported that a mis-recorded direction
        was uncorrectable, and it should be. The one boundary kept is coin vs
        cash -- a coin row cannot become a bare DEPOSIT and vice versa, because
        that swaps a quantity for a fiat amount that isn't there. Within the coin
        vocabulary every direction is offered, and :meth:`_write_action` re-signs
        the quantity (and a trade's amount) so the flip stays consistent."""
        if self.is_blank_row(row):
            # A fresh manual entry may become anything the account kind allows: a
            # wallet moves coin only (credit or debit), an exchange has the full
            # vocabulary (trade, transfer, in-kind income, cash movement).
            if self.is_wallet:
                return sorted(crypto.WALLET_CREDIT_ACTIONS
                              | crypto.WALLET_DEBIT_ACTIONS)
            return sorted(crypto.ACTIONS)
        if self.is_pending_row(row):
            return self.pending_actions()
        r = self.txn_at(row)
        if r is None:
            return []
        act = (r["action"] or "").strip().upper()
        if self.is_wallet:
            # A wallet holds coin only; its full direction pair is send/receive
            # plus the in-kind income credits the chain cannot name for itself.
            # The row's OWN action is always kept in the list (a transfer or swap
            # leg carries one outside that set), so the inline combo -- which,
            # unlike the dialog, cannot inject a missing current value -- always
            # opens with the row's action selected rather than silently on RECEIVE.
            choices = set(crypto.WALLET_CREDIT_ACTIONS
                          | crypto.WALLET_DEBIT_ACTIONS)
            if act:
                choices.add(act)
            return sorted(choices)
        if act in crypto.CASH_ACTIONS:
            return sorted(crypto.CASH_ACTIONS)
        return sorted(crypto.ADD_ACTIONS | crypto.REMOVE_ACTIONS)

    def _pending_text(self, col) -> str:
        """One cell of the pending row. The chain-supplied fields render exactly
        as a posted row would; the three editable ones come from the buffer."""
        m = self._pending["entry"].mapped
        buf = self._pending["buf"]
        if col == self.DATE:
            return fmt_date(m.date)
        if col == self.ACTION:
            return buf.get("action", "")
        if col == self.COIN:
            return m.symbol or ""
        if col == self.PAYEE:
            return buf.get("payee", "")
        if col == self.MEMO:
            return buf.get("memo", "")
        if col == self.TRANSFER:
            return buf.get("transfer", "")
        if col in (self.COIN_IN, self.COIN_OUT, self.QUANTITY):
            qty = (m.quantity or "").strip()
            if not qty:
                return ""
            out = buf.get("action", "").upper() in crypto.WALLET_DEBIT_ACTIONS
            if col == self.QUANTITY:
                return fmt_qty(("-" + qty) if out else qty)
            if out != (col == self.COIN_OUT):
                return ""
            return fmt_qty(qty)
        if col == self.FEE:
            return (f"{fmt_qty(m.fee_quantity)} {m.fee_symbol}".strip()
                    if m.fee_quantity else "")
        if col == self.PRICE:
            return fmt_qty(m.price) if m.price else ""
        if col == self.AMOUNT:
            return buf.get("amount", "")
        return ""

    def column_index(self, key) -> int:
        """Where a column KEY sits in this account's layout, or -1 when the kind
        does not show it. The view configures widths through this rather than
        through the constants, so a wallet cannot be handed an exchange column."""
        try:
            return self._columns.index(key)
        except ValueError:
            return -1

    def set_symbol_filter(self, symbol):
        """Restrict the register to one coin (``None`` shows all). The running
        ``coin_bal`` per row is derived over the FULL history first (in
        :func:`mammon.crypto.register_rows`), so filtering afterward keeps each
        kept row's correct running balance for that coin."""
        self._symbol_filter = symbol or None
        self.reload()

    def reload(self):
        self.beginResetModel()
        rows = crypto.register_rows(self.conn, self.account_id)
        if self._symbol_filter is not None:
            rows = [r for r in rows if r["symbol"] == self._symbol_filter]
        self._rows = rows
        self.endResetModel()

    def account_name(self) -> str:
        acct = ledger.get_account(self.conn, self.account_id)
        return acct["name"] if acct else ""

    def txn_at(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_for_txn(self, txn_id) -> int:
        for i, r in enumerate(self._rows):
            if r["id"] == txn_id:
                return i
        return -1

    # ---- QAbstractTableModel API -----------------------------------------
    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        # + the pending review line (when open) + the always-present trailing
        # blank quick-entry row, laid out exactly as the cash register's are.
        return len(self._rows) + (1 if self._pending is not None else 0) + 1

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            if 0 <= section < len(self._columns):
                return self._HEADER_TEXT[self._columns[section]]
        return None

    def _coin_side_active(self, r, col) -> bool:
        """Whether the Coin In / Coin Out cell ``col`` is the POPULATED side for
        row ``r`` -- the same sign test :meth:`_cell_text` renders by, so the
        editable side is exactly the one showing a number. Editing it changes the
        magnitude; the DIRECTION (which side is used) is the Action's to change."""
        q = r.get("quantity") if r is not None else None
        if q is None or str(q).strip() == "":
            return False
        try:
            out = Decimal(str(q)) < 0
        except (InvalidOperation, ValueError):
            return False
        return out == (col == self.COIN_OUT)

    def flags(self, index):
        # Every field a crypto row legitimately has is editable -- an import is
        # not an oracle (see _POSTED_EDITABLE). Only the DERIVED running balances
        # stay read-only, and of the Coin In / Coin Out pair only the side the
        # row's direction currently uses (the other renders blank). The PENDING
        # review row keeps its narrower judgement-only set (_PENDING_EDITABLE).
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if self.is_blank_row(index.row()):
            # The blank quick-entry row: the cells a user fills to state a new
            # event are editable; the derived running balances are not.
            if (0 <= index.column() < len(self._columns)
                    and self._columns[index.column()] in self._blank_editable()):
                return base | Qt.ItemIsEditable
            return base
        if not self.is_pending_row(index.row()):
            posted = (self._POSTED_EDITABLE if self.is_wallet
                      else self._POSTED_EDITABLE_EXCHANGE)
            if not (0 <= index.column() < len(self._columns)):
                return base
            col = self._columns[index.column()]
            if col not in posted:
                return base
            if col in (self.COIN_IN, self.COIN_OUT):
                # Only the side the row's direction uses is editable; the empty
                # side is a rendering of the same quantity, not its own cell.
                if not self._coin_side_active(self.txn_at(index.row()), col):
                    return base
            return base | Qt.ItemIsEditable
        if not (0 <= index.column() < len(self._columns)):
            return base
        if self._columns[index.column()] in self._pending_editable():
            return base | Qt.ItemIsEditable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < self.rowCount()):
            return None
        if not (0 <= index.column() < len(self._columns)):
            return None
        col = self._columns[index.column()]
        if self.is_blank_row(index.row()):
            if role in (Qt.DisplayRole, Qt.EditRole):
                # The date EDITOR (DateDelegate) parses EditRole as ISO, so hand
                # it the raw ISO date; the cell still DISPLAYS the user's format.
                if role == Qt.EditRole and col == self.DATE:
                    return self._new.get("date", "")
                return self._blank_text(col)
            if role == Qt.ToolTipRole:
                return self._blank_text(col) or None
            if role == Qt.TextAlignmentRole and col in self._NUMERIC:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if role == Qt.ForegroundRole:
                ct = style.cell_text_color()
                if ct:
                    return QBrush(QColor(ct))
            return None
        if self.is_pending_row(index.row()):
            if role in (Qt.DisplayRole, Qt.EditRole):
                if role == Qt.EditRole and col == self.DATE:
                    return self._pending["entry"].mapped.date or ""
                return self._pending_text(col)
            if role == Qt.ToolTipRole:
                return self._pending_text(col) or None
            if role == Qt.TextAlignmentRole and col in self._NUMERIC:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if role == Qt.ForegroundRole:
                ct = style.cell_text_color()
                if ct:
                    return QBrush(QColor(ct))
            return None
        r = self._rows[index.row()]
        if role in (Qt.DisplayRole, Qt.EditRole):
            if role == Qt.EditRole and col == self.DATE:
                return r["date"] or ""
            return self._cell_text(r, col)
        if role == Qt.ToolTipRole:
            if col == self.PAYEE and r.get("payee"):
                # Name WHICH end of the movement the counterparty is, so a
                # direction flip's effect on its meaning is visible: coin coming
                # in names its sender (From), coin going out its recipient (To).
                role_ = crypto.payee_role(r.get("action"))
                label = {"from": "From", "to": "To"}.get(role_)
                if label:
                    return f"{label}: {r['payee']}"
            # A coin quantity carries up to 18 decimals, so a column sized for
            # the ordinary case still elides the occasional long one. The full
            # value is always one hover away rather than lost behind an ellipsis.
            return self._cell_text(r, col) or None
        if role == Qt.TextAlignmentRole and col in self._NUMERIC:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            # Amount / Cash Bal go red when negative (Quicken convention).
            if col == self.AMOUNT and (r.get("cash_amt") or 0) < 0:
                return QBrush(QColor(style.negative_color()))
            if col == self.CASH_BAL and (r.get("cash_bal") or 0) < 0:
                return QBrush(QColor(style.negative_color()))
            ct = style.cell_text_color()   # legible item text in dark mode
            if ct:
                return QBrush(QColor(ct))
        return None

    def setData(self, index, value, role=Qt.EditRole):
        """Writes land in one of two places and nowhere else: the PENDING row's
        buffer, or -- through :mod:`mammon.crypto` -- an already-posted event.
        Never on the review entry, which is the source's ground truth."""
        if role != Qt.EditRole or not index.isValid():
            return False
        if self.is_blank_row(index.row()):
            return self._set_blank(index, value)
        if not self.is_pending_row(index.row()):
            return self._write_posted(index, value)
        if not (0 <= index.column() < len(self._columns)):
            return False
        col = self._columns[index.column()]
        if col not in self._pending_editable():
            return False
        key = {self.ACTION: "action", self.PAYEE: "payee",
               self.MEMO: "memo", self.AMOUNT: "amount",
               self.TRANSFER: "transfer"}[col]
        text = str(value or "").strip()
        self._pending["buf"][key] = text.upper() if key == "action" else text
        row = index.row()
        # The action decides which side the quantity renders on, so redraw the
        # whole row rather than the one cell.
        self.dataChanged.emit(self.index(row, 0),
                              self.index(row, len(self._columns) - 1))
        return True

    def _cell_text(self, r, col) -> str:
        if col == self.DATE:
            return fmt_date(r["date"])
        if col == self.ACTION:
            return r["action"] or ""
        if col == self.COIN:
            # The COIN column names the COIN. It used to double as the transfer
            # target, which meant a transfer row could not state its own symbol --
            # and left the register with no transfer field at all, unlike every
            # other account type. A swap still shows its OUT->IN pair here,
            # because that genuinely is a statement about coins.
            if r.get("swap_group_id") is not None:
                return r.get("label") or r["symbol"] or ""
            return r["symbol"] or ""
        if col == self.TRANSFER:
            # The other account, when this row is one leg of a transfer, rendered
            # `[Account]` exactly as the cash register renders one. The brackets
            # are not needed to disambiguate here -- this column holds nothing but
            # accounts -- but the user reads and types this field in every other
            # register, and consistency beats concision.
            name = r.get("transfer_name") or ""
            return f"[{name}]" if name else ""
        if col == self.PAYEE:
            # The on-chain counterparty (From on a credit, To on a debit).
            return r.get("payee") or ""
        if col == self.MEMO:
            return r.get("memo") or ""
        if col == self.QUANTITY:
            # Stored SIGNED (an OUT leg is negative), shown verbatim.
            return fmt_qty(r["quantity"]) if r["quantity"] is not None else ""
        if col in (self.COIN_IN, self.COIN_OUT):
            # The wallet layout splits the signed quantity into the two columns
            # the source exports it in (Value_IN / Value_OUT), so a receive and a
            # send never share a column. Sign comes off the stored quantity, which
            # crypto.record_wallet_debit writes negative.
            q = r.get("quantity")
            if q is None or str(q).strip() == "":
                return ""
            try:
                out = Decimal(str(q)) < 0
            except (InvalidOperation, ValueError):
                return ""
            if out != (col == self.COIN_OUT):
                return ""
            return fmt_qty(abs(Decimal(str(q))))
        if col == self.PRICE:
            return fmt_qty(r["price"]) if r["price"] else ""
        if col == self.COIN_BAL:
            # None on rows that move no coin (Quicken leaves the balance blank).
            return fmt_qty(r["coin_bal"]) if r.get("coin_bal") is not None else ""
        if col == self.AMOUNT:
            # The TRADE's fiat value, which is not the same as its effect on this
            # account's cash. An X row's proceeds went straight out, so its
            # sleeve effect is zero -- but the sale still happened for a sum, and
            # blanking the cell made a SELLX look like it sold for nothing.
            # `cash_amt`/`cash_bal` remain the sleeve story; this is the row's.
            amt = r.get("amount")
            return fmt_cents(amt) if amt else ""
        if col == self.CASH_BAL:
            return fmt_cents(r.get("cash_bal") or 0)
        if col == self.FEE:
            return r.get("fee_label") or ""
        return ""


# ---------------------------------------------------------------------------
# search results model (Find dialog: within-account + global)
# ---------------------------------------------------------------------------
class SearchResultsModel(QAbstractTableModel):
    """Read-only projection of :func:`mammon.ledger.search_transactions` results
    for the Find dialog. Leads with the account name so global (cross-account)
    results stay legible, then the key transaction fields; the amount renders
    signed like the ledger (red when negative) in one column."""

    ACCOUNT, DATE, NUM, PAYEE, CATEGORY, TAG, MEMO, AMOUNT = range(8)
    HEADERS = ["Account", "Date", "Num", "Payee", "Category", "Tag", "Memo", "Amount"]

    def __init__(self, results=None, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = list(results or [])

    def set_results(self, results):
        self.beginResetModel()
        self._rows = list(results or [])
        self.endResetModel()

    def result_at(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if not (0 <= index.row() < len(self._rows)):
            return None  # stale/out-of-range index after a shrink+reload
        r = self._rows[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            if col == self.ACCOUNT:
                return r["account_name"]
            if col == self.DATE:
                return fmt_date(r["date"])
            if col == self.NUM:
                return r["num"]
            if col == self.PAYEE:
                return r["payee"]
            if col == self.CATEGORY:
                return r["category_label"]
            if col == self.TAG:
                return r["tag"]
            if col == self.MEMO:
                return r["memo"]
            if col == self.AMOUNT:
                return fmt_cents(r["amount"])
        if role == Qt.TextAlignmentRole and col == self.AMOUNT:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if col == self.AMOUNT and r["amount"] < 0:
                return QBrush(QColor(style.negative_color()))
            ct = style.cell_text_color()  # legible item text in dark mode; None in light
            if ct:
                return QBrush(QColor(ct))
        return None


# ---------------------------------------------------------------------------
# accounts overview model
# ---------------------------------------------------------------------------
class AccountsModel(QAbstractTableModel):
    """All open accounts with their balances; feeds the overview and net worth."""

    NAME, TYPE, BALANCE = range(3)
    HEADERS = ["Account", "Type", "Balance"]

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._rows: list[dict] = []
        self.reload()

    def reload(self):
        self.beginResetModel()
        self._rows = [
            {"id": a["id"], "name": a["name"], "type": a["type"],
             # each account's native currency, so a foreign balance renders in it
             "currency": a["currency"],
             # investment accounts show their market valuation (cash + securities)
             "balance": investments.display_balance(self.conn, a["id"])}
            for a in ledger.list_accounts(self.conn)
        ]
        self.endResetModel()

    def net_worth(self) -> int:
        """Net worth in the BASE currency. Foreign-currency accounts are folded in
        through :mod:`mammon.fx` (the domain layer owns every conversion and the
        cents math). A non-zero foreign balance with no recorded FX rate is left
        OUT of the total rather than folded in at a dishonest 1:1 -- the total is
        then honestly incomplete, not silently overstated by the raw foreign
        number. An all-USD ledger converts through the identity and is unchanged."""
        from mammon import fx
        return fx.net_worth_currencies(self.conn).total_cents

    def unconverted_currencies(self) -> list:
        """The currencies whose balances could NOT be folded into net worth for
        want of an FX rate (base first, then A->Z, as ``fx`` reports them).

        Empty means the total is complete. Non-empty means :meth:`net_worth` is a
        partial figure, which is why the account bar shows a warning mark instead
        of it: the per-currency subtotals are each complete, and only the roll-up
        is impossible. ``mammon.fx`` decides this -- the UI layer holds no
        conversion logic of its own."""
        from mammon import fx
        return list(fx.net_worth_currencies(self.conn).unconverted)

    def rows(self) -> list[dict]:
        """The account rows (id/name/type/balance) for the account bar."""
        return list(self._rows)

    def account_id_at(self, row):
        return self._rows[row]["id"] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if not (0 <= index.row() < len(self._rows)):
            return None  # stale/out-of-range index after a shrink+reload
        r = self._rows[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            if col == self.NAME:
                return r["name"]
            if col == self.TYPE:
                return r["type"]
            if col == self.BALANCE:
                return fmt_amount_ccy(r["balance"], r.get("currency"))
        if role == Qt.TextAlignmentRole and col == self.BALANCE:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if col == self.BALANCE and r["balance"] < 0:
                return QBrush(QColor(style.negative_color()))
            ct = style.cell_text_color()  # legible item text in dark mode; None in light
            if ct:
                return QBrush(QColor(ct))
        return None


# ---------------------------------------------------------------------------
# net worth broken out per coin / per currency (the crypto-era view)
# ---------------------------------------------------------------------------
class NetWorthByAssetModel(QAbstractTableModel):
    """Net worth as a per-asset grid: one COLUMN per coin/currency plus a trailing
    Total column, a NATIVE row (coin quantity, or native cents for a fiat bucket)
    and a USD row (each converted to the base currency via price history + fx).

    A THIN projection of :func:`mammon.fx.net_worth_by_asset` -- it holds no SQL
    and no coin/cents math, only formatting for display. The domain function keeps
    the crypto holdings from being double-counted (they are the same halves
    ``display_balance`` already sums), so the Total equals the account bar's."""

    NATIVE, USD = range(2)
    VHEADERS = ["Native", "USD"]

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._lines: list = []
        self._total = 0
        self.reload()

    def reload(self):
        from mammon import fx
        self.beginResetModel()
        try:
            bd = fx.net_worth_by_asset(self.conn)
            self._lines = list(bd.lines)
            self._total = bd.total_usd_cents
        except fx.FxRateUnavailable:
            # A missing FX rate is surfaced as an empty breakdown; the account
            # bar's own net-worth label falls back to the naive base sum.
            self._lines = []
            self._total = 0
        self.endResetModel()

    def total_cents(self) -> int:
        return self._total

    def _is_total_col(self, col) -> bool:
        return col == len(self._lines)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 2

    def columnCount(self, parent=QModelIndex()):
        # one column per asset, plus the trailing Total column
        return 0 if parent.isValid() else len(self._lines) + 1

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if self._is_total_col(section):
                return "Total"
            return self._lines[section].asset if 0 <= section < len(self._lines) else None
        return self.VHEADERS[section] if 0 <= section < len(self.VHEADERS) else None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role not in (Qt.DisplayRole, Qt.EditRole):
            if role == Qt.ForegroundRole:
                ct = style.cell_text_color()
                if ct:
                    return QBrush(QColor(ct))
            return None
        if self._is_total_col(col):
            # No cross-coin native sum is meaningful; only the USD grand total is.
            return fmt_cents(self._total) if row == self.USD else ""
        if not (0 <= col < len(self._lines)):
            return None
        line = self._lines[col]
        if row == self.NATIVE:
            if line.is_coin:
                return fmt_qty(line.quantity) if line.quantity is not None else ""
            return fmt_cents(line.native_cents or 0)
        if row == self.USD:
            return fmt_cents(line.usd_cents)
        return None
