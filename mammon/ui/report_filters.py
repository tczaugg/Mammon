"""mammon.ui.report_filters -- reusable customization controls for the reporting
dialogs (Spending by Category, Spending Pie, Net Worth Over Time).

The reporting dialogs used to open over the *entire* ledger with no way to narrow
them; this module reinstates the time-range / accounts / categories controls as a
single embeddable :class:`ReportFilterBar`. It emits ``applied`` when the user
clicks Apply so the host dialog can recompute and re-render its report/chart.

One bar, every report: time range, accounts, categories, and an **Include hidden
accounts** toggle (off by default -- hiding is how a user excludes an account
whose records are incomplete; see :class:`ReportFilterBar`).

The pure helpers :func:`filter_spending_report` and :func:`spending_pie_from_report`
apply the *category* selection in memory (the spending report groups BY category,
so restricting to a subset of top-level categories is a display concern, not a new
SQL predicate). They live here -- not in the dialogs -- so they stay headless-testable.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal, QDate
from PyQt5.QtWidgets import (
    QWidget, QDialog, QComboBox, QHBoxLayout, QVBoxLayout, QFormLayout, QDateEdit,
    QListWidget, QListWidgetItem, QPushButton, QGroupBox, QToolButton,
    QCheckBox,
)

from mammon import ledger


# Ordered (label, key) pairs for the ONE report period dropdown shared by every
# report window (§5.9b). This is the UNION of the presets this dropdown has ever
# offered: the original calendar ranges (This/Last Month, This/Last Year,
# Year-to-Date) AND the rolling ranges added by the report-unification work
# (rolling 7/30-day windows, rolling 12 months, this/last quarter, earliest to
# date). An earlier change wrongly REPLACED the calendar ranges with the rolling
# ones; both must remain reachable, so neither set of habits is broken. Ordered
# shortest span first, widening to the whole ledger, with ``"custom"`` last.
#
# Keys resolve through :func:`resolve_period`: the rolling and calendar ranges
# delegate to :func:`mammon.reports.spending.preset_range`, ``"earliest"`` spans
# the ledger's own bounds, and ``"custom"`` opens the customize (gear) dialog for
# an explicit range. ``PERIOD_DEFAULT`` ("Year-to-Date") is the default for every
# report and chart window, so a freshly opened report answers "how am I doing this
# year" without a first trip to the dropdown. Net Worth Over Time is the one
# deliberate exception -- it opens on ``NET_WORTH_PERIOD_DEFAULT`` ("earliest")
# because a cumulative curve is meaningless over a partial-year slice (§5.8d).
PERIOD_PRESETS = [
    ("Last 7 days", "last_7_days"),
    ("Last 30 days", "last_30_days"),
    ("This Month", "this_month"),
    ("Last Month", "last_month"),
    ("This quarter", "this_quarter"),
    ("Last quarter", "last_quarter"),
    ("Last 12 months", "last_12_months"),
    ("Year-to-Date", "ytd"),
    ("This Year", "this_year"),
    ("Last Year", "last_year"),
    ("Earliest to date", "earliest"),
    ("Custom", "custom"),
]
PERIOD_DEFAULT = "ytd"
# Net Worth is a cumulative curve, not a within-period sum: a YTD slice would lop
# off decades of history and mislead, so its window keeps the whole-ledger span.
NET_WORTH_PERIOD_DEFAULT = "earliest"


def make_period_combo(default_key: str = PERIOD_DEFAULT) -> QComboBox:
    """Build the ONE Period dropdown every report and chart window shares (§5.9b).

    Constructed here, in one place, so its sizing stays identical across the two
    call sites that used to build it inline (:class:`ReportWindow` and the legacy
    chart dialogs' ``_report_period_header``). It is widened to the longest preset
    label (``"Earliest to date"``) plus room for the drop-down arrow: sitting in a
    stretch layout the combo was otherwise squeezed to a default width that clipped
    that label to ``"Earliest to d..."``. Seeding the current index here fires no
    signal, and callers connect their own ``currentIndexChanged`` handler AFTER this
    returns, so no premature refresh runs.
    """
    combo = QComboBox()
    combo.setToolTip("Report period")
    for label, key in PERIOD_PRESETS:
        combo.addItem(label, key)
    idx = combo.findData(default_key)
    if idx >= 0:
        combo.setCurrentIndex(idx)
    fm = combo.fontMetrics()
    measure = getattr(fm, "horizontalAdvance", fm.width)  # Qt >= 5.11 renamed it
    widest = max(measure(label) for label, _ in PERIOD_PRESETS)
    combo.setMinimumWidth(widest + 44)  # + drop-down arrow and frame padding
    return combo


def resolve_period(key, conn, today):
    """Resolve a period-dropdown key to an inclusive ``(start, end)`` ISO range.

    Returns ``None`` for ``"custom"`` -- the caller opens the customize (gear)
    dialog so the user picks an explicit range. ``"earliest"`` spans the ledger's
    earliest transaction through ``today`` (extended to the latest transaction if
    that is later, so the whole ledger is always covered); on an empty ledger it
    falls back to the last 30 days. Every other key delegates to
    :func:`mammon.reports.spending.preset_range`, so the date arithmetic lives in
    the pure report layer, not the UI."""
    from mammon.reports.spending import preset_range
    if key == "custom":
        return None
    if key == "earliest":
        bstart, bend = ledger.transaction_date_bounds(conn)
        if not bstart:
            return preset_range("last_30_days", today)
        end = today.strftime("%Y-%m-%d")
        if bend and bend > end:
            end = bend
        return bstart, end
    return preset_range(key, today)


def _to_qdate(iso) -> QDate:
    d = QDate.fromString(str(iso or ""), "yyyy-MM-dd")
    return d if d.isValid() else QDate.currentDate()


class ReportFilterBar(QWidget):
    """The one customization control every report uses: date range, accounts,
    categories, and whether hidden accounts count.

    ``categories`` (an iterable of category names) turns the flat category
    check-list on; leaving it falsy hides it. This is only a *picker* of which
    categories are eligible -- the expandable tree lives in the report itself,
    not here. ``show_accounts`` toggles the account check-list. Each check-list gets
    "Mark all" and "Clear all" buttons -- narrowing to one account out of ninety
    is Clear-all-then-tick-one, and widening back is one click rather than
    ninety. A control returns ``None`` from its
    ``selected_*`` getter when *every* item is checked, meaning "no filter" so
    the caller can pass ``None`` straight through to the report functions.

    **Include hidden accounts** is UNCHECKED by default, so every report starts
    from the same accounts as the account bar and net worth. Hiding is how a user
    excludes an account whose records are incomplete -- an employer plan whose
    internals were never entered carries a balance the ledger cannot justify --
    and a report must not put that money back unasked.

    Ticking it adds hidden accounts to the picker AND to the report, so the list
    and the result never disagree. It earns its place on a growth curve: an
    account zeroed before it was hidden contributes nothing today but held money
    for years, and dropping it makes decades of saving look like a recent
    windfall.
    """

    applied = pyqtSignal()

    def __init__(self, conn, start, end, *, show_accounts=True,
                 categories=None, show_hidden_toggle=True, parent=None):
        super().__init__(parent)
        self._conn = conn
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 8)
        row = QHBoxLayout()
        outer.addLayout(row)

        dates = QFormLayout()
        self.start_edit = self._date_edit(start)
        self.end_edit = self._date_edit(end)
        dates.addRow("From:", self.start_edit)
        dates.addRow("To:", self.end_edit)
        row.addLayout(dates)

        self.hidden_check = None
        self.account_list = None
        if show_accounts:
            self.account_list = self._check_list()
            account_buttons = (self._mark_button(self.mark_accounts),
                               self._clear_button(self.clear_accounts))
            extra = None
            if show_hidden_toggle:
                self.hidden_check = QCheckBox("Include hidden accounts")
                self.hidden_check.setChecked(False)
                self.hidden_check.setToolTip(
                    "Hidden accounts are left out, as they are everywhere else. "
                    "Tick to add them to this picker and this report -- useful "
                    "on a growth curve, where an account that was zeroed and "
                    "hidden still held money for years.")
                self.hidden_check.toggled.connect(self._reload_accounts)
                extra = self.hidden_check
            self._reload_accounts()
            row.addWidget(self._boxed("Accounts", self.account_list,
                                      account_buttons, extra=extra))

        self.category_list = None
        if categories:
            self.category_list = self._check_list()
            for name in categories:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                self.category_list.addItem(item)
            row.addWidget(self._boxed(
                "Categories", self.category_list,
                (self._mark_button(self.mark_categories),
                 self._clear_button(self.clear_categories))))

        row.addStretch(1)

        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self.applied.emit)
        # Enter anywhere in the bar means "apply what I typed" -- the only
        # button here that should answer the Return key.
        self.apply_button.setAutoDefault(True)
        self.apply_button.setDefault(True)
        apply_row.addWidget(self.apply_button)
        outer.addLayout(apply_row)

    # -- construction helpers ------------------------------------------------
    def _date_edit(self, iso) -> QDateEdit:
        """The shared app date editor, so report filters read in the same format
        as the registers (they were pinned to ISO regardless of the preference)."""
        from mammon.ui.delegates import make_date_edit
        return make_date_edit(iso=str(iso or ""))

    def _check_list(self) -> QListWidget:
        lst = QListWidget()
        lst.setMaximumHeight(96)
        lst.setMaximumWidth(220)
        return lst

    def _boxed(self, title, widget, buttons=(), extra=None) -> QGroupBox:
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.addWidget(widget)
        if extra is not None:
            lay.addWidget(extra)
        buttons = [b for b in buttons if b is not None]
        if buttons:
            btn_row = QHBoxLayout()
            btn_row.addStretch(1)
            for b in buttons:
                btn_row.addWidget(b)
            lay.addLayout(btn_row)
        return box

    def _list_button(self, text, tooltip, on_click) -> QPushButton:
        """A check-list action button (Mark all / Clear all).

        Every one of these is built here so none of them can become the dialog's
        default. A QPushButton in a dialog is autoDefault, and Enter fires the
        FIRST one in the focus chain -- which is how pressing Enter in the To
        date field once wiped every account checkbox. Adding a button to this
        panel must never be able to reclaim the Return key.
        """
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.clicked.connect(on_click)
        btn.setAutoDefault(False)
        btn.setDefault(False)
        return btn

    def _clear_button(self, on_click) -> QPushButton:
        return self._list_button("Clear all", "Uncheck every item", on_click)

    def _mark_button(self, on_click) -> QPushButton:
        return self._list_button("Mark all", "Check every item", on_click)

    def _reload_accounts(self) -> None:
        """(Re)fill the account check-list for the current hidden setting.

        Ticks are preserved by account id across the rebuild -- toggling "include
        hidden" to glance at the roster must not silently undo a selection the
        user has already made. Accounts appearing for the first time arrive
        checked, matching the all-checked-means-no-filter default."""
        if self.account_list is None:
            return
        previous = {}
        for i in range(self.account_list.count()):
            it = self.account_list.item(i)
            previous[int(it.data(Qt.UserRole))] = it.checkState()
        self.account_list.clear()
        for acct in ledger.list_accounts(self._conn, include_closed=True,
                                         include_hidden=self.include_hidden()):
            item = QListWidgetItem(acct["name"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(previous.get(int(acct["id"]), Qt.Checked))
            item.setData(Qt.UserRole, int(acct["id"]))
            self.account_list.addItem(item)

    # -- mark-all / clear-all actions -----------------------------------------
    @staticmethod
    def _set_all(lst, state) -> None:
        if lst is None:
            return
        for i in range(lst.count()):
            lst.item(i).setCheckState(state)

    def clear_accounts(self) -> None:
        self._set_all(self.account_list, Qt.Unchecked)

    def mark_accounts(self) -> None:
        self._set_all(self.account_list, Qt.Checked)

    def clear_categories(self) -> None:
        self._set_all(self.category_list, Qt.Unchecked)

    def mark_categories(self) -> None:
        self._set_all(self.category_list, Qt.Checked)

    # -- range setter --------------------------------------------------------
    def set_range(self, start, end) -> None:
        """Overwrite the From/To dates (ISO strings). Used by the report period
        dropdown to apply a preset range without touching the account/category
        selections."""
        self.start_edit.setDate(_to_qdate(start))
        self.end_edit.setDate(_to_qdate(end))

    # -- selection getters ---------------------------------------------------
    def start_iso(self) -> str:
        return self.start_edit.date().toString("yyyy-MM-dd")

    def end_iso(self) -> str:
        return self.end_edit.date().toString("yyyy-MM-dd")

    def include_hidden(self) -> bool:
        """Whether hidden accounts count. False when the toggle is absent, so a
        report built without it matches the account bar."""
        return False if self.hidden_check is None else self.hidden_check.isChecked()

    def selected_account_ids(self) -> Optional[list]:
        """The chosen account ids, or None for "no filter".

        None means the report may use every account it likes, so it is only
        honest when the picker really is showing every account. With hidden
        accounts excluded, an all-checked list is a REAL filter -- returning None
        there let a report quietly include the accounts the user had just
        excluded, since a report that receives None goes on to query them all.
        """
        ids = self._checked(self.account_list, Qt.UserRole, cast=int)
        if ids is None and not self.include_hidden() and self.account_list is not None:
            visible = [int(self.account_list.item(i).data(Qt.UserRole))
                       for i in range(self.account_list.count())]
            if self._has_hidden_accounts():
                return visible
        return ids

    def _has_hidden_accounts(self) -> bool:
        """Whether the ledger has any hidden account at all -- if it does not,
        an all-checked list really is 'everything' and None stays correct."""
        row = self._conn.execute(
            "SELECT 1 FROM accounts WHERE hidden=1 LIMIT 1").fetchone()
        return row is not None

    def selected_categories(self) -> Optional[set]:
        vals = self._checked(self.category_list, None, cast=str)
        return None if vals is None else set(vals)

    def _checked(self, lst, role, *, cast):
        if lst is None:
            return None
        picked, all_checked = [], True
        for i in range(lst.count()):
            it = lst.item(i)
            if it.checkState() == Qt.Checked:
                picked.append(cast(it.text() if role is None else it.data(role)))
            else:
                all_checked = False
        return None if all_checked else picked


# ---------------------------------------------------------------------------
# Customize popup: the gear button opens the filter bar in a dialog
# ---------------------------------------------------------------------------
class CustomizeDialog(QDialog):
    """A small popup that hosts a :class:`ReportFilterBar`. The report dialogs no
    longer show the controls inline; a gear button (see :func:`customize_button`)
    opens this instead. Its ``filters`` attribute is the live bar -- the host
    reads ``filters.start_iso()`` etc. -- and it re-emits ``applied`` when the
    user clicks Apply, closing the popup so the host can re-render."""

    applied = pyqtSignal()

    def __init__(self, conn, start, end, *, show_accounts=True,
                 categories=None, show_hidden_toggle=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Customize Report")
        self.filters = ReportFilterBar(conn, start, end,
                                       show_accounts=show_accounts,
                                       categories=categories,
                                       show_hidden_toggle=show_hidden_toggle)
        lay = QVBoxLayout(self)
        lay.addWidget(self.filters)
        # Apply in the bar -> tell the host to refresh, then dismiss the popup.
        self.filters.applied.connect(self.applied.emit)
        self.filters.applied.connect(self.accept)

    def add_saved_filter_row(self, row) -> None:
        """Place a caller-built controls row (the report window's saved-filter
        combo + Save/Delete) at the TOP of this popup, so every report's
        customization -- range, accounts, categories AND named saved sets --
        lives behind the one gear rather than inline in the host window."""
        self.layout().insertLayout(0, row)


def customize_button(customize_dialog, parent=None) -> QToolButton:
    """A gear tool-button that opens ``customize_dialog`` (modal) on click.

    Rendered a few points larger than the ambient font: it is the single
    customization affordance every report shares, so it should be easy to spot
    and to hit."""
    btn = QToolButton(parent)
    btn.setText("⚙")                       # gear glyph
    btn.setToolTip("Customize report…")
    btn.setAutoRaise(True)
    btn.setStyleSheet("QToolButton { font-size: 20px; padding: 2px 6px; }")
    btn.clicked.connect(lambda: customize_dialog.exec_())
    return btn


# ---------------------------------------------------------------------------
# In-memory category filtering (headless-testable, no Qt / no SQL)
# ---------------------------------------------------------------------------
def expense_only(report, types):
    """Return ``report`` with income-type top-level rows removed, so the spending
    pie shows EXPENSE categories only. ``types`` is ``{category_id: 'income'|
    'expense'}`` (e.g. from :func:`mammon.category_types.classify_categories`).
    Uncategorized rows (``category_id is None``) have no type and are kept."""
    from mammon.category_types import INCOME
    from mammon.reports.spending import SpendingReport
    rows = [r for r in report.rows
            if getattr(r, "category_id", None) is None
            or types.get(r.category_id) != INCOME]
    return SpendingReport(start=report.start, end=report.end,
                          account_ids=report.account_ids, rows=rows,
                          total_cents=sum(r.total_cents for r in rows))


def filter_spending_report(report, categories):
    """Return ``report`` restricted to top-level ``categories`` (a set of names).

    ``categories is None`` returns the report unchanged. The total is recomputed
    from the surviving rows so the rendered percentages still sum to 100%.
    """
    if categories is None:
        return report
    from mammon.reports.spending import SpendingReport
    rows = [r for r in report.rows if r.name in categories]
    return SpendingReport(start=report.start, end=report.end,
                          account_ids=report.account_ids, rows=rows,
                          total_cents=sum(r.total_cents for r in rows))


def spending_pie_from_report(report, categories=None, max_slices=8):
    """Build a :class:`SpendingPie` from a report's top-level rows, honoring an
    optional ``categories`` filter. Mirrors :func:`mammon.reports.charts.spending_pie`
    (largest first, long tail collapsed into "Other") but works from an
    already-computed report so the category filter is applied consistently -- a
    category collapsed into "Other" by the plain ``spending_pie`` can't be
    filtered by name, this can."""
    from mammon.reports.charts import SpendingPie, PieSlice, _OTHER
    if max_slices < 2:
        raise ValueError("max_slices must be >= 2")
    rows = [r for r in report.rows if r.total_cents > 0
            and (categories is None or r.name in categories)]
    total = sum(r.total_cents for r in rows)
    if total <= 0 or not rows:
        return SpendingPie(start=report.start, end=report.end, slices=[],
                           total_cents=0)
    rows = sorted(rows, key=lambda r: r.total_cents, reverse=True)
    if len(rows) > max_slices:
        keep, tail = rows[:max_slices - 1], rows[max_slices - 1:]
    else:
        keep, tail = rows, []
    slices = [PieSlice(r.name, r.total_cents, r.total_cents / total) for r in keep]
    if tail:
        other = sum(r.total_cents for r in tail)
        slices.append(PieSlice(_OTHER, other, other / total))
    return SpendingPie(start=report.start, end=report.end, slices=slices,
                       total_cents=total)
