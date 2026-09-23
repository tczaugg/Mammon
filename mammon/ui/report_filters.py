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

import datetime as _dt
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal, QDate
from PyQt5.QtWidgets import (
    QWidget, QDialog, QComboBox, QHBoxLayout, QVBoxLayout, QFormLayout, QDateEdit,
    QListWidget, QListWidgetItem, QPushButton, QGroupBox, QToolButton,
    QCheckBox, QTreeWidget, QTreeWidgetItem,
)

from mammon import category_types, ledger


# -- the ONE category picker, and the scope its callers choose ----------------
# Every report that narrows by category builds its check-list here, declaring
# which side of the ledger it can honor. The three scopes are the whole of it:
# a spending chart asks for EXPENSE, an income pie for INCOME, and a report with
# both sections (Itemize, Cash Flow, Income vs Expense, Transactions) for BOTH.
#
# This replaced four ad-hoc lists. The worst of them derived its names from
# ``reports.spending_by_category`` -- money OUT only -- so Itemize by Category
# rendered an INCOME section whose categories could not be ticked in its own
# picker, which the user reported as unacceptable. No picker may take its names
# from a report aggregation again: the scope is a parameter, not a side effect
# of which report happened to supply the list.
CATEGORY_KIND_EXPENSE = category_types.EXPENSE
CATEGORY_KIND_INCOME = category_types.INCOME
CATEGORY_KIND_BOTH = category_types.BOTH
CATEGORY_KINDS = (CATEGORY_KIND_EXPENSE, CATEGORY_KIND_INCOME,
                  CATEGORY_KIND_BOTH)


def category_picker_names(conn, kind=CATEGORY_KIND_BOTH, *,
                          include_hidden=False) -> list:
    """Top-level category names for a check-list of the requested ``kind``.

    A thin, SQL-free projection of :func:`mammon.category_types.
    top_level_categories` -- the domain layer decides what a category IS
    (including the income/expense classification, which is money logic and does
    not belong up here); the UI only renders the names. Because it reads the
    category TREE and not a report, a category with no activity in the shown
    range is still offered, so re-dating a report never makes a tick disappear.
    """
    return [c["name"] for c in
            category_types.top_level_categories(conn, kind,
                                                include_hidden=include_hidden)]


def category_picker_tree(conn, kind=CATEGORY_KIND_BOTH, *,
                         include_hidden=False) -> list:
    """The category HIERARCHY for a checkable tree of the requested ``kind``.

    The same thin, SQL-free projection as :func:`category_picker_names`, one
    level deeper: nested ``{'id', 'name', 'type', 'children': [...]}`` dicts from
    :func:`mammon.category_types.category_forest`, which walks the tree with
    :func:`mammon.ledger.category_children`. The UI never issues a hierarchy
    query of its own.

    Scope still bites at the TOP level only: a subtree is offered only under a
    top-level category of the requested kind, so the tree offers exactly the
    names the flat list used to, plus their descendants.
    """
    return category_types.category_forest(conn, kind,
                                          include_hidden=include_hidden)


# The category id a picker row stands for, carried on the item so the selection
# can be read back as IDS. A rename keeps the id (`ledger.rename_category`), so
# an id-keyed saved filter survives a rename that a name-keyed one silently
# dropped -- that defect is what moved the picker off names (§5.9c).
CATEGORY_ID_ROLE = Qt.UserRole


class CategoryTreeItem(QTreeWidgetItem):
    """One category row in the picker, carrying its id.

    The column arguments are DEFAULTED so the item reads like the
    ``QListWidgetItem`` this picker used to hold: ``it.text()``,
    ``it.checkState()`` and ``it.setCheckState(state)`` all mean column 0. Six
    existing test files and `ui.report_saved_filters` drive the picker through
    exactly those three calls, and they are non-virtual inline wrappers in Qt --
    overriding them changes what PYTHON callers see and nothing about how Qt
    renders or sorts the row. The virtual ``data()`` is deliberately left alone.
    """

    def __init__(self, name, category_id=None, account_id=None):
        super().__init__([str(name)])
        self.category_id = None if category_id is None else int(category_id)
        # A TRANSFER row: an account, not a category, shown as ``[Name]`` the way
        # the register and the drill-down already write one. It rides in the same
        # tree because that is where the user looks for it (Quicken lists
        # transfer accounts at the foot of the same category list), and it is a
        # SEPARATE attribute rather than a negative category id so that every
        # existing walker -- all of which test ``category_id is not None`` --
        # steps over it untouched instead of quietly counting an account as a
        # category.
        self.account_id = None if account_id is None else int(account_id)
        # THIS ROW'S OWN tick, which is not the same question as the checkbox.
        # The checkbox shows a ROLLUP: a parent reads PartiallyChecked when some
        # descendant is ticked, whether or not the user asked for the parent's
        # own postings. Three states cannot say both things, so the own tick is
        # kept beside the display state -- see ``category_tree_selections``.
        self.own_checked = True
        if self.category_id is not None:
            super().setData(0, CATEGORY_ID_ROLE, self.category_id)
        self.setFlags(self.flags() | Qt.ItemIsUserCheckable)
        super().setCheckState(0, Qt.Checked)

    def text(self, column=0):
        return super().text(column)

    def checkState(self, column=0):
        return super().checkState(column)

    def setCheckState(self, column, state=None):
        if state is None:                       # setCheckState(state) -- column 0
            column, state = 0, column
        super().setCheckState(column, state)


class CategoryTree(QTreeWidget):
    """The checkable category tree: expandable parents, tri-state ticks.

    Checking a parent implies its whole subtree; unchecking one child leaves the
    parent PARTIALLY checked, which is the state the report reads as "this
    parent's own postings, and the children still ticked". Propagation is done
    here in Python on ``itemChanged`` rather than through
    ``Qt.ItemIsAutoTristate``: the auto flag also makes a user click cycle
    through the partial state, and its behavior across Qt versions under the
    offscreen platform is not something the tests should have to pin down.
    ``_syncing`` guards the re-entrancy, since every programmatic tick re-enters
    this same signal.

    It also answers ``count()``/``item(i)``/``addItem(it)`` over its TOP-LEVEL
    rows, so the callers written against the flat ``QListWidget`` picker keep
    working unchanged.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setUniformRowHeights(True)
        self._syncing = False
        self.itemChanged.connect(self._propagate)

    # -- the flat-list API this picker used to have --------------------------
    def count(self) -> int:
        return self.topLevelItemCount()

    def item(self, i):
        return self.topLevelItem(i)

    def addItem(self, it) -> None:
        self.addTopLevelItem(it)

    # -- walking ------------------------------------------------------------
    def iter_items(self):
        """Every row, parents before their children."""
        stack = [self.topLevelItem(i)
                 for i in range(self.topLevelItemCount() - 1, -1, -1)]
        while stack:
            it = stack.pop()
            yield it
            stack.extend(it.child(i) for i in range(it.childCount() - 1, -1, -1))

    # -- ticking ------------------------------------------------------------
    def set_all(self, state) -> None:
        """Mark/clear the WHOLE tree, not just the top level."""
        self._syncing = True
        try:
            for it in self.iter_items():
                it.own_checked = state == Qt.Checked
                QTreeWidgetItem.setCheckState(it, 0, state)
        finally:
            self._syncing = False

    def set_checked_ids(self, ids) -> None:
        """Tick exactly the categories in ``ids`` (``None`` means all of them),
        then recompute every parent so a parent with a mix of ticked and
        unticked children comes back PARTIALLY checked. This is the inverse of
        :meth:`ReportFilterBar.selected_category_ids`, which is what makes a
        saved filter round-trip."""
        wanted = None if ids is None else {int(i) for i in ids}
        self._syncing = True
        try:
            for it in self.iter_items():
                on = wanted is None or (getattr(it, "category_id", None) is not None
                                        and int(it.category_id) in wanted)
                it.own_checked = bool(on)
                QTreeWidgetItem.setCheckState(
                    it, 0, Qt.Checked if on else Qt.Unchecked)
            for i in range(self.topLevelItemCount()):
                self._rollup(self.topLevelItem(i))
        finally:
            self._syncing = False

    def set_checked_names(self, names) -> None:
        """Tick the TOP-LEVEL rows so named, each with its whole subtree, and
        clear the rest -- what ticking those names meant when the picker was a
        flat list of top levels.

        This is the legacy path, and it matches on NAME rather than id on
        purpose: the ``categories=`` caller offers rows that may be synthesized
        buckets with no category id at all, so resolving through ids would tick
        nothing. What it leaves behind is still id-keyed -- every later read is
        :meth:`ReportFilterBar.selected_category_ids` -- so a set loaded this way
        is immune to the next rename.
        """
        wanted = {str(n) for n in names}
        self._syncing = True
        try:
            for i in range(self.topLevelItemCount()):
                top = self.topLevelItem(i)
                state = Qt.Checked if top.text() in wanted else Qt.Unchecked
                QTreeWidgetItem.setCheckState(top, 0, state)
                self._push_down(top, state)
        finally:
            self._syncing = False

    @staticmethod
    def _implied(item, kid_states):
        """The state ``item`` should now show, given its children's states and
        the state it is CURRENTLY in.

        The current state matters because a category is not merely a bucket for
        its children: it holds postings of its own. Unticking ``Federal``'s only
        child must leave ``Federal`` PARTIALLY checked -- "my own postings, none
        of my children" -- and not clear it, or a user could never ask for
        ``Taxes:Federal`` without ``Taxes:Federal:Withholding``, which is exactly
        the tax-reporting split this picker exists to express. So a parent goes
        fully Unchecked only when its own tick is already gone.

        The DISPLAY cannot say "my children but NOT my own postings" -- ticking a
        lone child rounds the parent up to PartiallyChecked, because three states
        per row is the whole vocabulary. That is a limit on the picture, not on
        the selection: the own tick is tracked beside it (``own_checked``) and is
        what ``category_tree_selections`` reads, so a report item can say exactly
        which of the two the user meant.
        """
        own_on = bool(getattr(item, "own_checked", False))
        if not own_on:
            # Ticking every child does NOT round the parent up to fully ticked:
            # the user asked for the children, not for the parent's own postings,
            # and a full tick here would draw a picture the selection does not
            # match. Partial is the honest mark for "something under me, not me".
            return (Qt.Unchecked if all(s == Qt.Unchecked for s in kid_states)
                    else Qt.PartiallyChecked)
        if all(s == Qt.Checked for s in kid_states):
            return Qt.Checked
        return Qt.PartiallyChecked

    def _rollup(self, item):
        """Post-order: give ``item`` the state its children (and its own current
        tick) imply -- see :meth:`_implied`."""
        n = item.childCount()
        if not n:
            return QTreeWidgetItem.checkState(item, 0)
        states = [self._rollup(item.child(i)) for i in range(n)]
        state = self._implied(item, states)
        QTreeWidgetItem.setCheckState(item, 0, state)
        return state

    def _propagate(self, item, column) -> None:
        if self._syncing or column != 0:
            return
        self._syncing = True
        try:
            state = QTreeWidgetItem.checkState(item, 0)
            if state != Qt.PartiallyChecked:
                item.own_checked = state == Qt.Checked
                self._push_down(item, state)
            parent = item.parent()
            while parent is not None:
                kids = [QTreeWidgetItem.checkState(parent.child(i), 0)
                        for i in range(parent.childCount())]
                QTreeWidgetItem.setCheckState(parent, 0,
                                              self._implied(parent, kids))
                parent = parent.parent()
        finally:
            self._syncing = False

    def _push_down(self, item, state) -> None:
        for i in range(item.childCount()):
            kid = item.child(i)
            kid.own_checked = state == Qt.Checked
            QTreeWidgetItem.setCheckState(kid, 0, state)
            self._push_down(kid, state)


def _build_category_tree(tree, forest) -> None:
    """Hang ``forest`` (nested picker dicts) under ``tree``, everything ticked.
    Population is done with the propagation guard held: each row starts Checked,
    so the rollups it would trigger are all no-ops."""
    tree._syncing = True
    try:
        def add(nodes, parent):
            for node in nodes:
                it = CategoryTreeItem(node["name"], node.get("id"))
                if parent is None:
                    tree.addTopLevelItem(it)
                else:
                    parent.addChild(it)
                add(node.get("children") or (), it)
        add(forest, None)
    finally:
        tree._syncing = False


# -- embeddable pickers for the custom-report editor (§5.9r) ------------------
# The custom-report window edits ONE item at a time and needs the same two
# pickers the filter bar carries, minus the bar. These factories exist so it can
# have them without reaching into ``ReportFilterBar``'s private construction
# helpers: one place still decides what a category picker IS, and a fix there
# reaches both callers.

#: The branch transfer rows hang under, and how one is labelled. Brackets are
#: the register's own notation for "the other side is an account", and the
#: drill-down already writes a transfer row that way.
TRANSFERS_BRANCH = "Transfers"


def transfer_row_label(account_name) -> str:
    return "[%s]" % account_name


def build_category_picker(conn, kind=CATEGORY_KIND_BOTH, *,
                          include_hidden=False,
                          transfers=False) -> CategoryTree:
    """A populated, all-ticked :class:`CategoryTree` for ``kind``.

    ``transfers=True`` appends a "Transfers" branch listing every account as
    ``[Name]``, for the one caller that can act on it: a custom report's ``SOSC``
    item, which sums selected categories AND transfers whose far side is a
    selected account (SRD 5.9r). It is opt-in because nothing else can -- the
    filter bar's picker filters postings by category, and an account row there
    would be a tick that silently did nothing.

    Those rows come back UNTICKED even though the tree ticks categories by
    default. The defaults mean opposite things for the same reason
    :func:`build_account_picker` unticks everything: "every category" is the
    filter bar saying no filter, while "every transfer in the ledger" is never
    what someone adding a tax line meant.
    """
    tree = CategoryTree()
    tree.setMaximumWidth(260)
    _build_category_tree(tree, category_picker_tree(conn, kind,
                                                    include_hidden=include_hidden))
    if transfers:
        _build_transfer_branch(tree, conn, include_hidden=include_hidden)
    return tree


def _build_transfer_branch(tree, conn, *, include_hidden=False) -> None:
    """Hang the "Transfers" branch, unticked, under an existing picker."""
    accounts = ledger.list_accounts(conn, include_closed=True,
                                    include_hidden=include_hidden)
    if not accounts:
        return
    tree._syncing = True
    try:
        root = CategoryTreeItem(TRANSFERS_BRANCH)
        tree.addTopLevelItem(root)
        for acct in accounts:
            row = CategoryTreeItem(transfer_row_label(acct["name"]),
                                   account_id=int(acct["id"]))
            row.own_checked = False
            root.addChild(row)
            QTreeWidgetItem.setCheckState(row, 0, Qt.Unchecked)
        root.own_checked = False
        QTreeWidgetItem.setCheckState(root, 0, Qt.Unchecked)
    finally:
        tree._syncing = False


def transfer_tree_selections(tree) -> list:
    """The checked TRANSFER rows as account ids -- what
    ``reports.custom.set_item_accounts`` stores for an ``SOSC`` item.

    Flat and id-only: an account has no subtree, so there is no rule to store the
    way a category's ticked parent stores one."""
    out = []
    for it in tree.iter_items():
        if getattr(it, "account_id", None) is not None and \
                it.checkState(0) == Qt.Checked:
            out.append(int(it.account_id))
    return out


def set_item_selections(tree, selections, transfer_ids=()) -> None:
    """Restore BOTH halves of an ``SOSC`` item's picker in one pass.

    One function rather than two because the two ticks share a widget:
    :meth:`CategoryTree.set_checked_ids` clears every row it does not recognize
    as a wanted category, so a second call to restore the transfers would undo
    the categories, and the reverse order would undo the transfers. Doing both
    before the one rollup also means a half-restored tree is never rolled up.
    """
    set_category_tree_selections(tree, selections)
    wanted = {int(i) for i in transfer_ids or ()}
    tree._syncing = True
    try:
        for it in tree.iter_items():
            aid = getattr(it, "account_id", None)
            if aid is not None:
                it.own_checked = int(aid) in wanted
                QTreeWidgetItem.setCheckState(
                    it, 0, Qt.Checked if it.own_checked else Qt.Unchecked)
        for i in range(tree.topLevelItemCount()):
            tree._rollup(tree.topLevelItem(i))
    finally:
        tree._syncing = False


def build_account_picker(conn, *, include_closed=True,
                         include_hidden=False) -> QListWidget:
    """A checkable account list, everything UNchecked.

    Unchecked is the right default here and checked is the right default in the
    filter bar, because the two mean opposite things: a filter with every box
    ticked is "no filter", while a balance item with every box ticked would be
    "every account in the ledger", which is never what the user meant to add.
    """
    lst = QListWidget()
    lst.setMaximumWidth(260)
    for acct in ledger.list_accounts(conn, include_closed=include_closed,
                                     include_hidden=include_hidden):
        item = QListWidgetItem(acct["name"])
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Unchecked)
        item.setData(Qt.UserRole, int(acct["id"]))
        lst.addItem(item)
    return lst


def _subtree_all_own(item) -> bool:
    """True when this row AND every descendant carry their own tick -- the shape
    that stores as one subtree rule instead of a list of ids."""
    if not getattr(item, "own_checked", False):
        return False
    return all(_subtree_all_own(item.child(i)) for i in range(item.childCount()))


def category_tree_selections(tree) -> list:
    """The tree's ticks as ``(category_id, include_subtree)`` pairs -- the shape
    ``reports.custom.set_item_categories`` stores.

    It reads each row's OWN tick, never the tri-state checkbox. The checkbox is a
    rollup, and a rollup cannot tell "I want this parent's own postings" from "a
    child of this parent is ticked": both render PartiallyChecked. Reading the
    picture instead of the intent is what put a whole parent category into an
    item that had only one sub-category selected -- ticking
    ``Gradient Research:Donations`` silently added every Gradient Research
    posting that was not in some other sub-category, which on a real ledger was
    most of the expenses.

    Three shapes come out of it:

    * a row whose whole subtree is ticked is ONE pair with ``include_subtree=1``,
      and its descendants are not walked -- storing the subtree as a rule rather
      than a list of ids is what makes a category added later get picked up
      without anyone re-editing the report;
    * a row ticked itself but not throughout contributes ``include_subtree=0`` --
      its own postings only -- and the walk continues into its children, so
      "this parent plus that one child" is expressible;
    * a row that is not ticked contributes nothing, however its checkbox looks.
    """
    out = []

    def walk(item):
        cid = getattr(item, "category_id", None)
        own = bool(getattr(item, "own_checked", False))
        if own and cid is not None and _subtree_all_own(item):
            out.append((int(cid), 1))
            return
        if own and cid is not None:
            out.append((int(cid), 0))
        for i in range(item.childCount()):
            walk(item.child(i))

    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    return out


def set_category_tree_selections(tree, selections) -> None:
    """Restore ticks from stored ``(category_id, include_subtree)`` pairs --
    the inverse of :func:`category_tree_selections`, so an edited item
    round-trips through the database unchanged.

    A stored subtree rule is expanded against the tree as it is TODAY, which is
    the point of storing the rule: a category created since the item was saved
    comes back ticked.
    """
    by_id = {}
    for it in tree.iter_items():
        cid = getattr(it, "category_id", None)
        if cid is not None:
            by_id[int(cid)] = it
    wanted = set()
    for sel in selections or ():
        if isinstance(sel, (tuple, list)):
            cid, sub = int(sel[0]), int(sel[1])
        else:
            cid, sub = int(sel), 0
        wanted.add(cid)
        it = by_id.get(cid)
        if it is not None and sub:
            stack = [it.child(i) for i in range(it.childCount())]
            while stack:
                kid = stack.pop()
                kcid = getattr(kid, "category_id", None)
                if kcid is not None:
                    wanted.add(int(kcid))
                stack.extend(kid.child(i) for i in range(kid.childCount()))
    tree.set_checked_ids(wanted)


def account_picker_ids(lst) -> list:
    """The checked account ids, in list order."""
    out = []
    for i in range(lst.count()):
        it = lst.item(i)
        if it.checkState() == Qt.Checked:
            out.append(int(it.data(Qt.UserRole)))
    return out


def set_account_picker_ids(lst, ids) -> None:
    """Tick exactly ``ids`` in an account picker."""
    wanted = {int(i) for i in ids or ()}
    for i in range(lst.count()):
        it = lst.item(i)
        it.setCheckState(Qt.Checked if int(it.data(Qt.UserRole)) in wanted
                         else Qt.Unchecked)


# Ordered (label, key) pairs for the ONE report period dropdown shared by every
# report window (§5.9b). This is the UNION of the presets this dropdown has ever
# offered: the original calendar ranges (This/Last Month, This/Last Year,
# Year-to-Date) AND the rolling ranges added by the report-unification work
# (rolling 7/30-day windows, rolling 12 months, the rolling 3/5/10-year windows,
# this/last quarter, earliest to date). An earlier change wrongly REPLACED the
# calendar ranges with the rolling ones; both must remain reachable, so neither
# set of habits is broken. Ordered shortest span first, widening to the whole
# ledger, with ``"custom"`` last.
#
# Keys resolve through :func:`resolve_period`: the rolling and calendar ranges
# delegate to :func:`mammon.reports.spending.preset_range`, ``"earliest"`` spans
# the ledger's own bounds, and ``"custom"`` opens the customize (gear) dialog for
# an explicit range. ``PERIOD_DEFAULT`` ("Year-to-Date") is the default for every
# report and chart window, so a freshly opened report answers "how am I doing this
# year" without a first trip to the dropdown. Net Worth Over Time is the one
# deliberate exception -- it opens on ``NET_WORTH_PERIOD_DEFAULT`` ("earliest")
# because a cumulative curve is meaningless over a partial-year slice (§5.9c).
PERIOD_PRESETS = [
    ("Last 7 days", "last_7_days"),
    ("Last 30 days", "last_30_days"),
    ("This Month", "this_month"),
    ("Last Month", "last_month"),
    ("This quarter", "this_quarter"),
    ("Last quarter", "last_quarter"),
    ("Last 12 months", "last_12_months"),
    # The long rolling windows sit with the other "Last N" presets, widening the
    # span before the calendar ranges. They earn their place on the Investment
    # Performance report, whose Gain/Loss is period-bounded: over a 40-year ledger
    # "how have my holdings done over the last 3 / 5 / 10 years" is the question a
    # single calendar year cannot answer.
    ("Last 3 years", "last_3_years"),
    ("Last 5 years", "last_5_years"),
    ("Last 10 years", "last_10_years"),
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


def period_for_range(start_iso, end_iso, conn, today=None):
    """The reverse of :func:`resolve_period`: name the period a range *is*.

    Walks :data:`PERIOD_PRESETS` in order, resolves every non-``"custom"`` key
    through :func:`resolve_period` (so both directions share one definition of
    what "This quarter" means) and returns the first key whose inclusive
    ``(start, end)`` ISO pair matches the one given. Returns ``"custom"`` when no
    preset does -- a hand-picked range is exactly what Custom names.

    This exists because the Period dropdown is not only an input: when the user
    edits the dates in the customize (gear) dialog the dropdown must stop
    advertising the preset that no longer describes the report (§5.9b). Dates
    cross this boundary as ISO strings, the same shape ``resolve_period`` and
    ``ReportFilterBar.start_iso()`` speak.
    """
    if today is None:
        today = _dt.date.today()
    wanted = (str(start_iso or ""), str(end_iso or ""))
    for _label, key in PERIOD_PRESETS:
        if key == "custom":
            continue
        rng = resolve_period(key, conn, today)
        if rng and (str(rng[0]), str(rng[1])) == wanted:
            return key
    return "custom"


def sync_period_combo(combo, start_iso, end_iso, conn, today=None) -> str:
    """Point ``combo`` at the preset the live range matches, WITHOUT re-entering
    the host's ``currentIndexChanged`` handler.

    The signal block is load-bearing, not tidiness: the handler for ``"custom"``
    opens the customize dialog, so syncing the combo from that dialog's own Apply
    would reopen it -- an endless stack of modals. Returns the key selected.
    """
    key = period_for_range(start_iso, end_iso, conn, today)
    idx = combo.findData(key)
    if idx < 0 or idx == combo.currentIndex():
        return key
    was_blocked = combo.blockSignals(True)
    try:
        combo.setCurrentIndex(idx)
    finally:
        combo.blockSignals(was_blocked)
    return key


def _to_qdate(iso) -> QDate:
    d = QDate.fromString(str(iso or ""), "yyyy-MM-dd")
    return d if d.isValid() else QDate.currentDate()


class ReportFilterBar(QWidget):
    """The one customization control every report uses: date range, accounts,
    categories, and whether hidden accounts count.

    ``category_kind`` turns the category picker on and says which categories
    belong in it: ``CATEGORY_KIND_EXPENSE``, ``CATEGORY_KIND_INCOME``
    or ``CATEGORY_KIND_BOTH`` (see :func:`category_picker_tree`). Leaving both
    it and ``categories`` falsy hides the picker -- a report whose pure function
    takes no category argument must not offer a control that does nothing, and
    one that does take it must not offer a list missing half the ledger. The
    older ``categories`` (an explicit iterable of names) still works for callers
    that already hold a list; it builds a flat tree of just those names.

    The picker is a checkable TREE, not a flat list of top levels: a parent
    expands to its sub-categories and each one can be ticked on its own, because
    tax reporting needs ``Taxes:Federal`` without ``Taxes:Property``. Reports
    read the selection back as IDS (:meth:`selected_category_ids`), so renaming
    a category cannot change what a saved filter means.
    ``show_accounts`` toggles the account check-list. Each check-list gets
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
                 account_types=None, categories=None, category_kind=None,
                 show_hidden_toggle=True, parent=None):
        super().__init__(parent)
        self._conn = conn
        # OPT-IN account restriction. None (the default, and what every report
        # window passes) keeps the historical behavior: the picker lists the
        # whole roster, all ticked, meaning "no filter". A caller that can only
        # ever honor some account TYPES -- the Investment Dashboard, which
        # values holdings and cannot draw a checking account -- passes the types
        # it can draw, and then the picker offers exactly those and nothing
        # else. Restricting here rather than at the host keeps the checkboxes
        # honest: the dashboard used to show ticks for accounts its own scope
        # dropped on the way back in.
        self._account_types = None if account_types is None else tuple(account_types)
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

        # ``category_kind`` is the preferred way in: the caller declares the
        # scope it can honor (expense / income / both) and the bar builds the
        # list through the one shared picker, so no caller assembles category
        # names of its own. An explicit ``categories`` list stays supported for
        # the few callers that already hold names (and for tests that pin a
        # list), but it carries no kind -- ``category_kind`` is then None.
        self.category_kind = category_kind
        forest = []
        if category_kind is not None:
            forest = category_picker_tree(conn, category_kind)
        elif categories:
            # A caller that already holds NAMES gets a flat tree of those names.
            # Their ids are resolved here so the selection is still id-keyed
            # wherever the name is a real top-level category; a name that is not
            # (a synthesized bucket) simply carries no id and is offered by name.
            by_name = {c["name"]: c["id"] for c in
                       category_types.top_level_categories(
                           conn, CATEGORY_KIND_BOTH, include_hidden=True)}
            forest = [{"id": by_name.get(n), "name": n, "children": []}
                      for n in categories]
        # What was offered, in order: the net-worth what-if needs the full set to
        # work out which names the user DROPPED, and nothing else should have to
        # rebuild the list to find that out. Top-level names only -- that is the
        # granularity those callers group by.
        self.category_names = [node["name"] for node in forest]

        self.category_list = None
        if category_kind is not None or categories:
            self.category_list = self._category_tree()
            _build_category_tree(self.category_list, forest)
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

    def _category_tree(self) -> CategoryTree:
        """The category picker. Taller than the flat check-list it replaced --
        an expanded parent needs the room, and a picker that shows two rows at a
        time is not one a user can drive down to a leaf."""
        tree = CategoryTree()
        tree.setMaximumHeight(160)
        tree.setMaximumWidth(220)
        return tree

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
        checked, matching the all-checked-means-no-filter default.

        When the caller declared ``account_types``, rows of any other type never
        reach the list at all, so the dialog opens with exactly those accounts
        and exactly those ticked."""
        if self.account_list is None:
            return
        previous = {}
        for i in range(self.account_list.count()):
            it = self.account_list.item(i)
            previous[int(it.data(Qt.UserRole))] = it.checkState()
        self.account_list.clear()
        for acct in ledger.list_accounts(self._conn, include_closed=True,
                                         include_hidden=self.include_hidden()):
            if (self._account_types is not None
                    and acct["type"] not in self._account_types):
                continue
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
        """Clear/mark the WHOLE tree, descendants included -- a "Mark all" that
        left an unticked grandchild behind would still be filtering."""
        if self.category_list is not None:
            self.category_list.set_all(Qt.Unchecked)

    def mark_categories(self) -> None:
        if self.category_list is not None:
            self.category_list.set_all(Qt.Checked)

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

        Under ``account_types`` None stays correct and stays preferable: the
        picker is then showing every account the HOST can use (the dashboard's
        None already means "every investment account"), and naming the ids
        instead would pin the scope, so a brokerage opened tomorrow would not
        join a dashboard whose gear had once been marked all.
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
        """The chosen TOP-LEVEL category names, or None for "no filter".

        Kept for the callers that group by top level and cannot say anything
        about a sub-category (the spending/income pies, the net-worth what-if).
        A partially-checked top level counts as chosen -- some of it survives the
        filter -- and None comes back only when every row in the whole tree is
        ticked, so an unticked grandchild is still a real filter. Reports that
        can honor a sub-category read :meth:`selected_category_ids` instead.
        """
        tree = self.category_list
        if tree is None:
            return None
        if all(it.checkState() == Qt.Checked for it in tree.iter_items()):
            return None
        return {tree.item(i).text() for i in range(tree.count())
                if tree.item(i).checkState() != Qt.Unchecked}

    def selected_category_ids(self) -> Optional[list]:
        """The chosen category IDS, or None for "no filter".

        Ids, not names, because `ledger.rename_category` keeps the id: a
        name-keyed selection silently drops a category the moment it is renamed,
        which is exactly the defect the tax-report design ruled out.

        A row is in the set when it is Checked OR PartiallyChecked. Partial
        earns its place: unchecking ``Taxes:Property`` must still let
        ``Taxes:Federal`` AND Taxes' own direct postings through, so the parent
        stays in the set while the unticked child is simply absent. Because the
        tree has already pushed every tick down its subtree, the result needs no
        descendant expansion -- it is already the exact set of categories the
        report may count.
        """
        tree = self.category_list
        if tree is None:
            return None
        picked, all_checked = [], True
        for it in tree.iter_items():
            state = it.checkState()
            if state != Qt.Checked:
                all_checked = False
            if state != Qt.Unchecked and getattr(it, "category_id", None) is not None:
                picked.append(int(it.category_id))
        return None if all_checked else sorted(picked)

    def set_selected_category_ids(self, ids) -> None:
        """Restore a selection saved as ids (``None`` re-ticks everything)."""
        if self.category_list is not None:
            self.category_list.set_checked_ids(ids)

    def set_selected_category_names(self, names) -> None:
        """Restore a legacy NAME-keyed selection (``None`` re-ticks everything):
        tick the top-level rows so named together with their whole subtrees,
        which is what ticking those names meant when the picker was flat. The
        state it leaves is read back as ids like any other."""
        if self.category_list is None:
            return
        if names is None:
            self.mark_categories()
            return
        self.category_list.set_checked_names(names)

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
                 account_types=None, categories=None, category_kind=None,
                 show_hidden_toggle=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Customize Report")
        self.filters = ReportFilterBar(conn, start, end,
                                       show_accounts=show_accounts,
                                       account_types=account_types,
                                       categories=categories,
                                       category_kind=category_kind,
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
