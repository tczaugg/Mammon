"""mammon.ui.custom_report_window -- the **Taxes and Custom Reports** window
(SRD 5.9r): build a report definition, point its items at categories or
accounts, and read the evaluated numbers.

This is the user-visible face of `mammon.reports.custom`. Everything it shows is
recomputed on the spot: no amount or subtotal is ever written back, so the window
cannot become a second, staler source of truth than the ledger it reads.

The shapes below are deliberate and load-bearing.

**The item table never edits in place.** Editing happens in the panel beside it,
and the table is read-only. A delegate's ``setModelData`` runs while Qt is
destroying the editor, so a category picker or a confirmation opened from there
frees the frame's own editor mid-frame (``0xc0000374``, no traceback) -- the
hazard CLAUDE.md names. A window whose editors are ordinary widgets in an
ordinary layout cannot reach that state at all, and the picker needs far more
room than a table cell in any case.

**Every user choice goes through an overridable method.** ``_prompt_text``,
``_confirm`` and ``_warn`` are the whole set, and each is one line over a
``QMessageBox``/``QInputDialog`` static. A ``QDialog`` built and ``exec_()``-ed
directly blocks forever under the offscreen platform, which would make this
window untestable; routing the three choices through named methods means a test
drives the window by calling ``create_report("Tithing")`` instead of by
answering a modal. Evaluation FAILURES are not choices and never become modals,
and neither is a REFUSED exclusion: both render into the status line under the
tree, where the user can read the message and act on it.

**The report is a drill-down, and that replaced a summary plus a warnings
panel.** The tree is line item -> tag -> category -> transaction, so every figure
on a tax form opens until the rows that made it are on screen. It replaced two
things at once. The flat summary could show a number but never why it was that
number. The Coverage panel below it could say a report had eighty findings but
printed them as evidence -- one line per transaction, thirty-six near-identical
rows for two actual facts -- and a user cannot act on a wall. Everything Coverage
existed to tell the user is now told AT the number it is about: a line an
exclusion tag removed is drawn struck through instead of vanishing, and the two
findings that mean a figure is not what it looks like (a category feeding two
items, both legs of a transfer pulled in by a tag) are amber triangles on the
item, carrying the explanation in a tooltip. ``custom.Coverage`` still computes
all of it -- the analysis was never the problem, its presentation was.

**The tree never sums a child into its parent.** Each node shows what the domain
layer computed for it, and a line item shows ``evaluate``'s row verbatim. A line
wearing two of an item's tags is deliberately counted under each, so tag rows can
add up to more than the item above them; rolling children up would quietly invent
a total different from the one that reaches the tax form. When the tag rows are
MEANT to partition (an item with an explicit tag list) and do not, that is the
third triangle.

**CSV export is a pure function plus a path seam.** :func:`drill_tree_rows`
flattens the tree into depth-tagged rows, :func:`report_def_to_csv` turns those
into a string and :meth:`CustomReportWindow.export_csv_to` writes it to an
explicit path; the file dialog lives in :meth:`_export_csv_dialog` and nothing
else calls it. The tree and the file walk the SAME row list, so what the user
exports is by construction what he was looking at (the convention SRD 5.9b
established for the other report windows), with the first column indented by
depth so the hierarchy survives the flatten.

**The exclusion toggle is this window's only write.** Right-clicking a
transaction row adds or removes ``!<item name>``, which is how a user carves one
transaction out of a tax line without leaving the report he is checking. WHERE an
exclusion may live is a fact about tag storage, not about Qt, so the rule lives
in :func:`mammon.ledger.toggle_report_exclusion` and this window only asks: a
transaction holds many tags, a split leg holds exactly one, and an exclusion is
refused on a transaction whose legs carry their own tags because the parent's
tags reach every leg.

**The per-tag breakdown is ONE widget and no new row path.** It is ON by
default, so the editor carries a single opt-OUT box, "Do not subtotal by tag",
and no tag-entry field at all: nobody should have to type the names of his own
properties to see them subtotalled, and typing them was also how a tag got
silently left out. Unchecked writes nothing; checked stores
``no_tag_breakdown`` in the item's ``options`` blob, and the tree then shows
categories directly under the line item with no tag level at all. A stored
definition that still carries an explicit tag LIST is honoured -- and reads
BETTER than discovery here, since its buckets partition and an empty one is
shown, which is how a property with no rent booked to it this year announces
itself.

**TXF export is the same path seam as CSV.** :meth:`export_txf_to` takes a path
and an optional ``export_date``; :meth:`_export_txf_dialog` is the only caller
that opens a file dialog, and it asks :meth:`txf_record_count` first so a report
carrying no reference numbers says so instead of writing an empty file. TXF
describes one tax year, so it always exports the range in the date fields.

**Definition files are reached through the same one-modal rule** (SRD 5.9v).
"From definition…" and "Update from definition…" are the UI end of
`mammon.reports.report_defs`: this window never parses a definition itself and
never writes a report row of its own -- it asks WHICH definition and hands the
answer to :func:`~mammon.reports.report_defs.create_from_definition` or
:func:`~mammon.reports.report_defs.update_report`. The choice is one method,
:meth:`CustomReportWindow._choose_definition`, which offers every file on the
search path and a "Browse…" entry; the browse itself is a second one-line method
over ``QFileDialog.getOpenFileName`` so an arbitrary ``.yaml`` anywhere on disk
is as reachable as a shipped one. What a migration did and could not do is a
RESULT, not a choice, so it goes through :meth:`_inform` over the pure
:func:`migration_text` -- an update that silently dropped a line the user had
spent an evening pointing at categories would be the one failure he must not
have to go looking for.

**The item editor has THREE picker pages, and COMPUTED is the third.** It used
to have two, and a COMPUTED item fell through to the account list -- a list of
accounts in front of an item that sums other ITEMS, with nowhere to type the
formula, which left a kind the evaluator has always supported unreachable from
this window. The formula page carries the box and a LIST of the report's other
lines: a brace name has to match an item exactly, so the names are offered
(double-click inserts ``{name}`` at the cursor) rather than transcribed. An item
is never offered its own name, and the formula is written through only for the
COMPUTED kind, so it cannot survive a kind change and come back later.

The pickers are `ui.report_filters`' -- one module decides what a category
picker is, and its tri-state ticks are exactly the vocabulary a report item
stores (a partially checked parent means "this parent's own postings, plus the
children still ticked").
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
    QPushButton, QGroupBox, QLineEdit, QCheckBox, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QHeaderView, QSplitter,
    QStackedWidget, QMenu, QTreeWidget, QTreeWidgetItem, QListWidget,
    QListWidgetItem, QMessageBox, QInputDialog, QFileDialog,
)

from mammon import ledger
from mammon.reports import custom, custom_export
from mammon.ui import report_filters
from mammon.ui.delegates import make_date_edit, date_edit_iso
from mammon.ui.models import fmt_cents, warning_triangle_icon


# The tree's columns, and the export's: one list, because the tree and the CSV
# are two renderings of the same rows (SRD 5.9b). The hierarchy column is FIRST
# so the disclosure triangles sit under it -- the convention the Itemize
# drill-down established in ``ui.report_window``.
DRILL_COLUMNS = ("Line item / Tag / Category", "Date", "Payee / Memo", "Amount")

# How a drill row is set off from its parent in the first column of a FLAT
# export. Leading spaces, not a font or a color: the tree and the CSV are two
# renderings of one row list, so the indent that shows depth on screen has to be
# something a spreadsheet receives too.
DEPTH_INDENT = "    "

# What each item kind is called for a human. The selector offers
# ``custom.IMPLEMENTED_KINDS``, which as of Phase 5 is every kind there is; an
# item can still fail to evaluate for its own reasons (a COMPUTED item with no
# expression, a cyclic formula), and the window reports that into the Coverage
# panel rather than crashing on it.
KIND_LABELS = {
    "SOSC": "Sum of selected categories",
    "EDAB": "Account balance, end date",
    "SDAB": "Account balance, start date",
    "HOLDVAL": "Holdings value",
    "RGAIN": "Realized gain",
    "NETGAIN": "Net gain",
    "COMPUTED": "Computed from other items",
}

# The kinds that take a category selection, and the kinds that take an account
# selection. Everything the editor switches -- which picker is shown, whether
# the tag box is usable -- comes off these two.
#
# The three investment kinds are ACCOUNT kinds: HOLDVAL values what the selected
# accounts held, RGAIN books the sales inside them, NETGAIN differences their
# balances. They were absent from this tuple while they still raised out of
# ``evaluate``, which meant the editor showed the account picker for them and
# then threw the ticks away on Apply -- an item that looked selected and
# evaluated over nothing. (A security narrowing is a separate, optional
# selection; an empty one means "every symbol those accounts held".)
CATEGORY_KINDS = ("SOSC",)
ACCOUNT_KINDS = ("EDAB", "SDAB", "HOLDVAL", "RGAIN", "NETGAIN")

# The kinds that can be broken down per tag. ``SOSC`` sums lines and can bucket
# them by the tags those lines carry; ``COMPUTED`` inherits the buckets of the
# items it references. A balance kind has no lines to bucket, so the control is
# grayed there rather than offered and quietly ignored (SRD 5.9u).
BREAKDOWN_KINDS = ("SOSC", "COMPUTED")

def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


# There is deliberately no parse/format pair for a tag list here any more. The
# editor used to carry a "Limit to tags:" box; subtotalling by tag is now the
# default and discovers the tags itself, so the box -- and the chance to leave a
# property out of it by forgetting to type it -- is gone.


# --------------------------------------------------------------------------
# Pure rendering: one row list, used by the tree and the CSV alike
# --------------------------------------------------------------------------
#
# The report is a DRILL-DOWN, not a summary: a line item opens into the tags its
# money carries, each tag into the categories under it, and each category into
# the transactions themselves, so every figure on a tax form can be followed to
# the rows that made it. The projection below is pure -- it re-packages
# ``custom.drill_down``'s already-computed cents for display and sums nothing --
# and it feeds both the QTreeWidget (which re-nests by depth) and the CSV
# (which indents the first column by depth), the convention SRD 5.9b set for
# every other report window.
#
# Two things it must never do, both of them bugs this report has already paid
# for once:
#
# * **roll a child up into its parent.** Every node carries the amount the
#   domain layer computed for it. A line wearing two of an item's tags is
#   counted under each on purpose, so tag rows can add up to more than the item
#   above them -- and the item's own figure, the one that reaches the tax form,
#   is the evaluator's row verbatim.
# * **hide an excluded line.** A row an exclusion tag pushed out is still drawn,
#   struck through, carrying its real amount. It contributed nothing and the
#   strike says so; deleting it from the view would restore exactly the silence
#   that makes a wrong total look like a right one.


@dataclass
class DrillRow:
    """One flattened node: ``(depth, cells)`` plus what the renderer needs.

    ``cells`` already holds display strings aligned to :data:`DRILL_COLUMNS`
    (dates through :func:`fmt_date`, money through the one :func:`fmt_cents`
    chokepoint). ``amount`` is kept only so the tree can right-align and tint the
    last column; nothing here does arithmetic on it.

    ``marks`` are the amber triangles the domain layer decided on, with their
    tooltips already written. ``txn_id``/``split_id``/``item_name`` address the
    line for the exclusion toggle -- they are None on every grouping row, which
    is how the context menu knows a row is not togglable."""

    depth: int
    kind: str                       # 'item' | 'tag' | 'category' | 'txn'
    cells: list
    amount: int
    excluded: bool = False
    bold: bool = False
    expanded: bool = False
    marks: tuple = ()
    item_name: str = ""
    item_id: object = None
    txn_id: object = None
    split_id: object = None

    @property
    def tooltip(self) -> str:
        """The row's marks as one tooltip, blank when it carries none."""
        return "\n\n".join(m.text for m in self.marks)


def _payee_memo(line) -> str:
    """A transaction row's middle column: the payee, with the memo after an em
    dash when there is one (the memo alone when there is no payee)."""
    if line.memo:
        return f"{line.payee} — {line.memo}" if line.payee else line.memo
    return line.payee


def drill_tree_rows(tree) -> list:
    """Project a :class:`~mammon.reports.custom.DrillTree` into depth-tagged
    :class:`DrillRow`\\s: each line item, its tags, their categories and finally
    the transactions.

    Line items are seeded OPEN and everything under them closed, so the report
    opens as the list of tax lines it has always been and expands only where the
    user asks. An item with no children (a balance kind, or a line nothing has
    been pointed at yet) is simply a leaf.

    **There is no grand-total row**, and there is deliberately no number behind
    one either (see :class:`~mammon.reports.custom.DrillTree`). One category
    legitimately feeds several tax lines -- on a real return, state withholding
    is both a W-2 line and a Schedule A deduction -- so the lines of a tax report
    share money by design and adding them up measures nothing. It is also not a
    figure any form asks for: a return is filed line by line. A bold ``TOTAL``
    under the last row is exactly the kind of number a reader trusts because it
    is bold, and this one was worth less than the blank space it occupied. Where
    a report genuinely does want a sum of particular lines, a ``COMPUTED`` item
    names them (SRD 5.9t) and says what it means.
    """
    from mammon.ui.models import fmt_date

    rows: list[DrillRow] = []

    def walk(node, depth: int) -> None:
        if node.kind == "txn":
            line = node.line
            rows.append(DrillRow(
                depth, "txn",
                ["", fmt_date(line.date), _payee_memo(line),
                 fmt_cents(line.amount)],
                line.amount, excluded=line.excluded,
                item_name=node.item_name, item_id=node.item_id,
                txn_id=line.txn_id, split_id=line.split_id))
            return
        is_item = node.kind == "item"
        rows.append(DrillRow(
            depth, node.kind, [node.label, "", "", fmt_cents(node.amount)],
            node.amount, bold=is_item, expanded=is_item,
            marks=tuple(node.marks), item_name=node.item_name,
            item_id=node.item_id))
        for child in node.children:
            walk(child, depth + 1)

    for item in tree.items:
        walk(item, 0)
    return rows


def _drill_row_cells(row) -> list:
    """A row's display strings for a FLAT export: the first column indented by
    depth so the hierarchy survives the flatten, and an excluded row marked in
    text -- a strike-through is a screen effect and a spreadsheet would receive
    a number that looks included."""
    cells = list(row.cells)
    cells[0] = (DEPTH_INDENT * row.depth) + cells[0]
    if row.excluded:
        cells[2] = (cells[2] + "  [excluded]").strip()
    return cells


def drill_rows_to_csv(rows, columns=DRILL_COLUMNS) -> str:
    """Serialize :class:`DrillRow`\\s to CSV (pure; no Qt, no I/O)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(_drill_row_cells(row))
    return buf.getvalue()


def report_def_to_csv(tree) -> str:
    """The drill-down as CSV -- the rows on screen, flattened."""
    return drill_rows_to_csv(drill_tree_rows(tree))


# --------------------------------------------------------------------------
# Definition files (SRD 5.9v): naming and rendering, as pure functions
# --------------------------------------------------------------------------

# The last entry of the definition chooser. Anything the user has on disk must
# be reachable, not just what happens to sit in a search root.
BROWSE_CHOICE = "Browse for a definition file…"


def definition_report_name(definition) -> str:
    """The report name offered for ``definition``: its title, plus its year
    when it names one.

    A definition is reused year after year, and report names are unique, so the
    year is part of the name rather than a detail hidden in the range."""
    title = (definition.title or definition.id or "Report").strip()
    return f"{title} {definition.year}" if definition.year else title


def definition_choice_label(definition, path, user_root=None) -> str:
    """One line of the chooser: title, year, id and WHICH root it came from.

    The root matters: a file the user edited in his own directory shadows the
    shipped one of the same name, and a chooser that did not say so would make
    that shadowing look like the shipped file changing under him."""
    year = f" {definition.year}" if definition.year else ""
    where = "shipped with Mammon"
    if user_root is not None:
        try:
            if Path(path).resolve().parent == Path(user_root).resolve():
                where = "your definitions"
        except OSError:
            pass
    return (f"{(definition.title or definition.id)}{year} "
            f"[{definition.id}] -- {where}")


def migration_text(result) -> str:
    """Render a :class:`~mammon.reports.report_defs.MigrationResult` for the
    user, in names he can act on.

    Carried lines are COUNTED and renames are SPELLED OUT: fifty unchanged
    lines are noise, but a line that changed name is the one the user has to
    recognise before he trusts the number under it. Everything that could not
    be carried is listed with its lost selections, because the whole point of
    updating rather than rebuilding is knowing what did not come across."""
    parts: list[str] = []
    renamed = [c for c in result.carried if c.name != c.from_name]
    parts.append("Carried over: %d line(s), selections included."
                 % len(result.carried))
    if renamed:
        parts.append("Renamed (selections carried):\n" + "\n".join(
            f"  {c.from_name} -> {c.name}" for c in renamed))
    if result.new_lines:
        parts.append("Added, nothing selected yet (%d):\n%s"
                     % (len(result.new_lines),
                        "\n".join(f"  {n}" for n in result.new_lines)))
    if result.dropped:
        parts.append("Dropped -- these selections were NOT carried (%d):\n%s"
                     % (len(result.dropped), "\n".join(
                         _dropped_line(d) for d in result.dropped)))
    if result.kind_changed:
        parts.append("Changed what they compute, so nothing was copied "
                     "(%d):\n%s" % (len(result.kind_changed), "\n".join(
                         f"  {k.from_name} -> {k.name}"
                         f"  ({k.from_kind} -> {k.to_kind})"
                         for k in result.kind_changed)))
    if result.unresolved:
        parts.append("Pointed at something that is not there (%d):\n%s"
                     % (len(result.unresolved), "\n".join(
                         f"  {u.name} -> {u.ref}: {u.reason}"
                         for u in result.unresolved)))
    if result.clean:
        parts.append("Nothing needs your attention: every line either carried "
                     "over or is honestly new.")
    return "\n\n".join(parts)


def _dropped_line(dropped) -> str:
    kept = []
    for label, names in (("categories", dropped.categories),
                         ("accounts", dropped.accounts),
                         ("securities", dropped.securities)):
        if names:
            kept.append(f"{label}: {', '.join(names)}")
    tail = f"  ({'; '.join(kept)})" if kept else ""
    return f"  {dropped.label or dropped.name} [{dropped.kind}]{tail}"


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

class CustomReportWindow(QDialog):
    """List, build and evaluate custom/tax report definitions.

    A modeless ``QDialog`` like the other report windows: it is ``show()``-n,
    never ``exec_()``-ed. A user assigning fifty tax lines needs his register
    beside him, and an ``exec_()``-ed dialog would additionally block forever
    under the offscreen platform the tests run on.

    It carries the minimize/maximize hints a ``QDialog`` does not get by
    default. This one is a WORKSPACE, not a question: the drill-down is four
    levels deep beside a category picker, and a window that could not be taken
    full-screen made the user scroll a tree in a letterbox.
    """

    def __init__(self, conn, report_id=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Taxes and Custom Reports")
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowMinMaxButtonsHint)
        self.resize(1100, 800)
        self._report_id = None
        self._item_id = None
        # Which item the editor's widgets are currently showing. It exists so a
        # reload can tell "show me a different line" from "re-read the line I am
        # editing", and refuse the second -- see _reload_editor.
        self._editor_item_id = None
        self._loading = False
        # Guards the item-list <-> report selection sync against its own echo:
        # each side's selection signal drives the other.
        self._syncing_selection = False
        # Remembered across printouts in this window -- see _print_dialog.
        self._print_settings = None
        self._tree = None
        self._migration = None

        outer = QVBoxLayout(self)
        outer.addLayout(self._build_report_row())
        outer.addLayout(self._build_range_row())
        outer.addWidget(self._build_editor_split(), 1)
        outer.addWidget(self._build_results(), 2)

        self.reload_reports()
        if report_id is not None:
            self.select_report(int(report_id))

    # -- construction --------------------------------------------------------
    def _build_report_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.report_combo = QComboBox()
        self.report_combo.setMinimumWidth(240)
        self.report_combo.currentIndexChanged.connect(self._on_report_changed)
        row.addWidget(QLabel("Report:"))
        row.addWidget(self.report_combo)
        for text, slot in (("New…", self.create_report),
                           ("From definition…",
                            self.create_report_from_definition),
                           ("Update from definition…",
                            self.update_report_from_definition),
                           ("Duplicate…", self.duplicate_report),
                           ("Delete…", self.delete_report)):
            btn = QPushButton(text)
            btn.setAutoDefault(False)
            btn.clicked.connect(lambda _checked=False, s=slot: s())
            row.addWidget(btn)
        row.addStretch(1)
        return row

    def _build_range_row(self) -> QHBoxLayout:
        year = _dt.date.today().year
        row = QHBoxLayout()
        self.start_edit = make_date_edit(iso=f"{year:04d}-01-01")
        self.end_edit = make_date_edit(iso=f"{year:04d}-12-31")
        apply_btn = QPushButton("Apply range")
        apply_btn.setAutoDefault(False)
        apply_btn.clicked.connect(self.apply_range)
        expand_btn = QPushButton("Expand all")
        expand_btn.setAutoDefault(False)
        expand_btn.clicked.connect(lambda: self.result_tree.expandAll())
        collapse_btn = QPushButton("Collapse all")
        collapse_btn.setAutoDefault(False)
        collapse_btn.clicked.connect(lambda: self.result_tree.collapseAll())
        export_btn = QPushButton("Export CSV…")
        export_btn.setAutoDefault(False)
        export_btn.clicked.connect(self._export_csv_dialog)
        txf_btn = QPushButton("Export TXF…")
        txf_btn.setAutoDefault(False)
        txf_btn.clicked.connect(self._export_txf_dialog)
        self.txf_button = txf_btn
        print_btn = QPushButton("Print…")
        print_btn.setAutoDefault(False)
        print_btn.clicked.connect(self._print_dialog)
        self.print_button = print_btn
        row.addWidget(QLabel("From:"))
        row.addWidget(self.start_edit)
        row.addWidget(QLabel("To:"))
        row.addWidget(self.end_edit)
        row.addWidget(apply_btn)
        row.addWidget(expand_btn)
        row.addWidget(collapse_btn)
        row.addStretch(1)
        row.addWidget(print_btn)
        row.addWidget(export_btn)
        row.addWidget(txf_btn)
        return row

    def _build_editor_split(self) -> QSplitter:
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_item_table())
        split.addWidget(self._build_item_editor())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        return split

    def _build_item_table(self) -> QWidget:
        box = QGroupBox("Items")
        lay = QVBoxLayout(box)
        self.item_table = QTableWidget(0, 4)
        self.item_table.setHorizontalHeaderLabels(["Item", "Kind", "Group", "Tag"])
        # Read-only on purpose: all editing happens in the panel to the right,
        # so no delegate ever opens a picker or a prompt from setModelData.
        self.item_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.item_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.item_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.item_table.verticalHeader().setVisible(False)
        self.item_table.horizontalHeader().setStretchLastSection(True)
        self.item_table.itemSelectionChanged.connect(self._on_item_selected)
        lay.addWidget(self.item_table)
        btns = QHBoxLayout()
        for text, slot in (("Add", self.add_item),
                           ("Remove…", self.remove_item),
                           ("Up", lambda: self.move_item(-1)),
                           ("Down", lambda: self.move_item(1))):
            btn = QPushButton(text)
            btn.setAutoDefault(False)
            btn.clicked.connect(lambda _checked=False, s=slot: s())
            btns.addWidget(btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        return box

    def _build_item_editor(self) -> QWidget:
        box = QGroupBox("Selected item")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.label_edit = QLineEdit()
        self.group_edit = QLineEdit()
        self.kind_combo = QComboBox()
        for kind in custom.IMPLEMENTED_KINDS:
            self.kind_combo.addItem(kind_label(kind), kind)
        self.kind_combo.currentIndexChanged.connect(self._sync_kind_widgets)
        self.sign_combo = QComboBox()
        self.sign_combo.addItem("As in the register (+1)", 1)
        self.sign_combo.addItem("Flipped (-1)", -1)
        self.tag_check = QCheckBox("Allow per-transaction tag override")
        # An opt-OUT, unchecked, and the only breakdown widget there is. It used
        # to be an opt-in checkbox plus a "Limit to tags:" box, and both halves
        # were wrong: a Schedule E wants its properties subtotalled every time,
        # and asking the user to TYPE the tag names meant a property he forgot
        # to type quietly emptied into the untagged remainder. The evaluator
        # discovers the tags, so the question left for the user is only whether
        # he wants the sub-lines at all.
        self.no_break_tag_check = QCheckBox(
            "Do not subtotal by tag (one total line only)")
        form.addRow("Name:", self.name_edit)
        form.addRow("Label:", self.label_edit)
        form.addRow("Group:", self.group_edit)
        form.addRow("Kind:", self.kind_combo)
        form.addRow("Sign:", self.sign_combo)
        # Both boxes are about TAGS, and an unlabelled pair in a labelled form
        # reads as two loose switches belonging to the row above them.
        form.addRow("Tags:", self.tag_check)
        form.addRow("", self.no_break_tag_check)
        lay.addLayout(form)

        # ``transfers=True``: an SOSC item can select transfer counterparties
        # alongside categories, so the Transfers branch belongs in the one list
        # the user is already reading (SRD 5.9r).
        self.category_picker = report_filters.build_category_picker(
            self.conn, transfers=True)
        self.account_picker = report_filters.build_account_picker(self.conn)
        self.picker_stack = QStackedWidget()
        self.picker_stack.addWidget(self.category_picker)
        self.picker_stack.addWidget(self.account_picker)
        self.picker_stack.addWidget(self._build_expr_page())
        lay.addWidget(self.picker_stack, 1)

        apply_btn = QPushButton("Apply item")
        apply_btn.setAutoDefault(False)
        apply_btn.clicked.connect(self.apply_item)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(apply_btn)
        lay.addLayout(row)
        self._editor_box = box
        return box

    def _build_results(self) -> QWidget:
        box = QGroupBox("Report")
        lay = QVBoxLayout(box)
        self.result_tree = QTreeWidget()
        self.result_tree.setColumnCount(len(DRILL_COLUMNS))
        self.result_tree.setHeaderLabels(list(DRILL_COLUMNS))
        self.result_tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        self.result_tree.setUniformRowHeights(True)
        self.result_tree.setAlternatingRowColors(True)
        self.result_tree.header().setStretchLastSection(False)
        self.result_tree.header().setSectionResizeMode(
            0, QHeaderView.Interactive)
        self.result_tree.header().setSectionResizeMode(
            len(DRILL_COLUMNS) - 1, QHeaderView.ResizeToContents)
        # The exclusion toggle is the one place this window writes to the
        # ledger, and it is reached only from here.
        self.result_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.result_tree.customContextMenuRequested.connect(self._row_menu)
        # The item list and the report are two views of ONE thing, so selecting
        # in either points the other at the same line. On a 68-line tax report
        # the line being edited is routinely off-screen in the report below,
        # and hunting for it by name is the tax-form equivalent of losing your
        # place.
        self.result_tree.itemSelectionChanged.connect(self._on_report_row_selected)
        lay.addWidget(self.result_tree)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)
        return box

    # -- overridable choice seams -------------------------------------------
    # Each of these is the ONE place a modal can appear. Tests override them;
    # nothing else in this window opens a dialog.
    def _prompt_text(self, title, label, default="") -> str:
        text, ok = QInputDialog.getText(self, title, label, text=default)
        return text.strip() if ok else ""

    def _confirm(self, title, text) -> bool:
        return QMessageBox.question(
            self, title, text, QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No) == QMessageBox.Yes

    def _warn(self, title, text) -> None:
        QMessageBox.warning(self, title, text)

    def _inform(self, title, text) -> None:
        """Report what just happened (a migration result). Not a choice, so it
        never asks anything -- but it is still a modal, so it is still a seam."""
        QMessageBox.information(self, title, text)

    def _choose_definition(self):
        """Which definition to build from: a definition id, a :class:`Path`, or
        ``None`` if the user cancelled.

        The ONE modal on the definition path. It lists everything on the search
        path (SRD 5.9v) and ends with :data:`BROWSE_CHOICE`, which delegates to
        :meth:`_browse_definition_file`; a test overrides either method and
        drives the rest of the path for real."""
        choices = self.definition_choices()
        labels = [label for label, _ in choices] + [BROWSE_CHOICE]
        text, ok = QInputDialog.getItem(
            self, "Report definition", "Definition:", labels, 0, False)
        if not ok or not text:
            return None
        if text == BROWSE_CHOICE:
            return self._browse_definition_file()
        for label, path in choices:
            if label == text:
                return path
        return None

    def _browse_definition_file(self):
        """Pick an arbitrary definition file anywhere on disk -- a year
        definition mailed to the user is as usable as a shipped one."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open report definition", "",
            "Report definitions (*.yaml *.yml *.json);;All files (*)")
        return Path(path) if path else None

    def definition_choices(self) -> list:
        """``(label, path)`` for every definition file on the search path.

        A file that will not parse is left OUT rather than allowed to break the
        chooser: the others are still perfectly usable, and the user learns
        what is wrong with his file when he browses to it by name."""
        from mammon.reports import report_defs
        roots = report_defs.search_roots()
        user_root = roots[0] if roots else None
        out = []
        for path in report_defs.list_definitions():
            try:
                defn = report_defs.load_definition(path)
            except (ValueError, OSError):
                continue
            out.append((definition_choice_label(defn, path, user_root), path))
        return out

    # -- report list ---------------------------------------------------------
    def report_ids(self) -> list:
        return [self.report_combo.itemData(i)
                for i in range(self.report_combo.count())]

    def current_report_id(self):
        return self._report_id

    def reload_reports(self) -> None:
        """Refill the report dropdown, keeping the current selection if it
        survived."""
        keep = self._report_id
        self._loading = True
        try:
            self.report_combo.clear()
            for rd in custom.list_reports(self.conn):
                self.report_combo.addItem(rd.name, int(rd.id))
        finally:
            self._loading = False
        ids = self.report_ids()
        if keep in ids:
            self.select_report(keep)
        elif ids:
            self.select_report(ids[0])
        else:
            self._report_id = None
            self._load_items()

    def select_report(self, report_id) -> None:
        idx = self.report_combo.findData(int(report_id))
        if idx < 0:
            return
        if self.report_combo.currentIndex() != idx:
            self.report_combo.setCurrentIndex(idx)          # fires _on_report_changed
            return
        self._activate_report(int(report_id))

    def _on_report_changed(self, _idx=None) -> None:
        if self._loading:
            return
        data = self.report_combo.currentData()
        self._activate_report(None if data is None else int(data))

    def _activate_report(self, report_id) -> None:
        self._report_id = report_id
        self._item_id = None
        if report_id is not None:
            self._show_stored_range(report_id)
        self._load_items()

    def _show_stored_range(self, report_id) -> None:
        """Put the report's own range in the date fields. A range the report
        cannot resolve (a definition whose stored parts are incomplete) leaves
        the fields alone and says so under the tree -- it is not a reason to
        refuse to open the report."""
        try:
            start, end = custom.resolve_range(self.conn, report_id)
        except (ValueError, KeyError) as exc:
            self._status(str(exc))
            return
        self._set_date_edits(start, end)

    def _set_date_edits(self, start, end) -> None:
        from PyQt5.QtCore import QDate
        for edit, iso in ((self.start_edit, start), (self.end_edit, end)):
            d = QDate.fromString(str(iso or ""), "yyyy-MM-dd")
            if d.isValid():
                edit.setDate(d)

    # -- report CRUD ---------------------------------------------------------
    def create_report(self, name=None, *, kind="custom"):
        """Create an empty report and select it. Returns its id, or ``None`` if
        the user cancelled or the name was refused."""
        if name is None:
            name = self._prompt_text("New report", "Report name:")
        if not name:
            return None
        try:
            report_id = custom.create_report(
                self.conn, name, kind=kind, range_kind="fixed",
                range_start=self.range_start(), range_end=self.range_end())
        except ValueError as exc:
            self._warn("New report", str(exc))
            return None
        self._report_id = report_id
        self.reload_reports()
        return report_id

    def duplicate_report(self, name=None):
        """Copy the current definition -- items, selections and range -- under a
        new name. The copy is what makes "same report, next year" a two-click
        operation without touching the original, which keeps evaluating as it
        always did."""
        if self._report_id is None:
            return None
        source = custom.get_report(self.conn, self._report_id)
        if name is None:
            name = self._prompt_text("Duplicate report", "New report name:",
                                     f"{source.name} (copy)")
        if not name:
            return None
        try:
            new_id = custom.create_report(
                self.conn, name, kind=source.kind,
                definition_id=source.definition_id,
                range_kind=source.range_kind, range_start=source.range_start,
                range_end=source.range_end, range_year=source.range_year,
                range_preset=source.range_preset, notes=source.notes)
        except ValueError as exc:
            self._warn("Duplicate report", str(exc))
            return None
        for item in custom.list_items(self.conn, self._report_id):
            # A tag-enabled name is unique LEDGER-wide, so the copy cannot keep
            # the tag on: two items answering to one tag would fight over it.
            new_item = custom.add_item(
                self.conn, new_id, item.name, item.kind, label=item.label,
                group_label=item.group_label, seq=item.seq, sign=item.sign,
                tag_enabled=0, options=item.options, expr=item.expr,
                txf_refnum=item.txf_refnum, txf_copy=item.txf_copy,
                txf_format=item.txf_format)
            cats = custom.item_categories(self.conn, item.id)
            if cats:
                custom.set_item_categories(self.conn, new_item, cats)
            accts = custom.item_accounts(self.conn, item.id)
            if accts:
                custom.set_item_accounts(self.conn, new_item, accts)
        self._report_id = new_id
        self.reload_reports()
        return new_id

    def delete_report(self) -> bool:
        if self._report_id is None:
            return False
        rd = custom.get_report(self.conn, self._report_id)
        if not self._confirm(
                "Delete report",
                f"Delete the report {rd.name!r} and all of its items?\n"
                "The transactions it reports on are not touched."):
            return False
        custom.delete_report(self.conn, self._report_id)
        self._report_id = None
        self.reload_reports()
        return True

    # -- reports built from a definition file (SRD 5.9v) ----------------------
    def _load_definition(self, source, title):
        """Parse ``source`` through ``report_defs``, or warn and return None.

        Parsing lives in ``report_defs`` and nowhere else; this window only
        turns its loud ``ValueError`` into a message the user can read."""
        from mammon.reports import report_defs
        try:
            return report_defs.load_definition(source)
        except (ValueError, OSError) as exc:
            self._warn(title, str(exc))
            return None

    def create_report_from_definition(self, source=None, name=None):
        """Create a report holding every line of a definition file, and select
        it. Returns the new report id, or ``None`` if the user cancelled.

        ``source`` is a definition id or a path; left out, it is asked for
        through :meth:`_choose_definition`. The new report carries NO
        selections -- every line is on the to-do list Coverage prints -- because
        a guessed category on a tax line is a wrong number nobody re-reads."""
        from mammon.reports import report_defs
        title = "New report from definition"
        if source is None:
            source = self._choose_definition()
        if not source:
            return None
        defn = self._load_definition(source, title)
        if defn is None:
            return None
        if name is None:
            name = self._prompt_text(title, "Report name:",
                                     definition_report_name(defn))
        if not name:
            return None
        try:
            report_id = report_defs.create_from_definition(
                self.conn, defn, name=name)
        except (ValueError, KeyError) as exc:
            self._warn(title, str(exc))
            return None
        self._report_id = report_id
        self.reload_reports()
        return report_id

    def update_report_from_definition(self, source=None, name=None):
        """Rebuild the selected report over a definition, carrying its
        selections across, and show what could not be carried.

        The source report is never touched -- last year's numbers were filed and
        stay filed -- so this always produces a NEW report, which is then
        selected. Returns its id, or ``None`` if the user cancelled."""
        from mammon.reports import report_defs
        title = "Update from definition"
        if self._report_id is None:
            return None
        source_id = self._report_id
        if source is None:
            source = self._choose_definition()
        if not source:
            return None
        defn = self._load_definition(source, title)
        if defn is None:
            return None
        if name is None:
            name = self._prompt_text(title, "Name for the updated report:",
                                     definition_report_name(defn))
        if not name:
            return None
        try:
            new_id, result = report_defs.update_report(
                self.conn, source_id, defn, name=name)
        except (ValueError, KeyError) as exc:
            self._warn(title, str(exc))
            return None
        self._migration = result
        self._report_id = new_id
        self.reload_reports()
        self._inform(title, migration_text(result))
        return new_id

    def last_migration(self):
        """The last :class:`~mammon.reports.report_defs.MigrationResult` this
        window produced, or ``None``."""
        return self._migration

    # -- items ---------------------------------------------------------------
    def item_ids(self) -> list:
        return [int(self.item_table.item(r, 0).data(Qt.UserRole))
                for r in range(self.item_table.rowCount())]

    def current_item_id(self):
        return self._item_id

    def _load_items(self) -> None:
        self._loading = True
        try:
            self.item_table.setRowCount(0)
            items = ([] if self._report_id is None
                     else custom.list_items(self.conn, self._report_id))
            self.item_table.setRowCount(len(items))
            for row, item in enumerate(items):
                cells = (item.name, kind_label(item.kind),
                         item.group_label or "", "yes" if item.tag_enabled else "")
                for col, text in enumerate(cells):
                    cell = QTableWidgetItem(text)
                    if col == 0:
                        cell.setData(Qt.UserRole, int(item.id))
                    self.item_table.setItem(row, col, cell)
            self.item_table.resizeColumnsToContents()
        finally:
            self._loading = False
        ids = self.item_ids()
        if self._item_id in ids:
            self.select_item(self._item_id)
        elif ids:
            self.select_item(ids[0])
        else:
            self._item_id = None
            self._clear_editor()
        self.refresh()

    def select_item(self, item_id) -> None:
        ids = self.item_ids()
        if int(item_id) not in ids:
            return
        row = ids.index(int(item_id))
        if self.item_table.currentRow() != row:
            self.item_table.selectRow(row)                  # fires the handler
            if self._item_id == int(item_id):
                return
        self._item_id = int(item_id)
        self._reload_editor(int(item_id))
        # Also from HERE, not only from the table's own signal: selecting the row
        # that is already current fires no signal at all, and the report would
        # then stay pointed wherever it was.
        self._highlight_report_row(int(item_id))

    # -- keeping the item list and the report pointed at the same line -------
    def _highlight_report_row(self, item_id) -> None:
        """Select and scroll to ``item_id``'s row in the report tree."""
        if self._syncing_selection or item_id is None:
            return
        widget = self._report_row_for(item_id)
        if widget is None:
            return
        self._syncing_selection = True
        try:
            self.result_tree.setCurrentItem(widget)
            self.result_tree.scrollToItem(widget)
        finally:
            self._syncing_selection = False
        # Remembered across printouts in this window -- see _print_dialog.
        self._print_settings = None

    def _report_row_for(self, item_id):
        """The report tree's top-level widget for one item, or None. Matched on
        the item ID rather than the label: two lines may share a label (a report
        has ``Salary`` under W-2 and under Schedule C), and the id cannot."""
        for i in range(self.result_tree.topLevelItemCount()):
            widget = self.result_tree.topLevelItem(i)
            row = widget.data(0, Qt.UserRole)
            if row is not None and row.item_id == int(item_id):
                return widget
        return None

    def _on_report_row_selected(self) -> None:
        """Clicking anywhere in the report points the item list at the line that
        row belongs to -- including a transaction four levels down, which is
        where a user notices something is wrong with the line above it."""
        if self._syncing_selection:
            return
        widget = self.result_tree.currentItem()
        if widget is None:
            return
        row = widget.data(0, Qt.UserRole)
        if row is None or row.item_id is None:
            return
        ids = self.item_ids()
        if int(row.item_id) not in ids:
            return
        self._syncing_selection = True
        try:
            self.item_table.selectRow(ids.index(int(row.item_id)))
            self.item_table.scrollToItem(
                self.item_table.item(ids.index(int(row.item_id)), 0))
        finally:
            self._syncing_selection = False
        # Remembered across printouts in this window -- see _print_dialog.
        self._print_settings = None
        # The editor follows the list, exactly as a click in the list does.
        if self._item_id != int(row.item_id):
            self._item_id = int(row.item_id)
            self._reload_editor(self._item_id)

    def _reload_editor(self, item_id) -> None:
        """Load the editor unless it is ALREADY showing this item.

        Re-reading the row the user is in the middle of editing can only throw
        his work away: the ticks and the text are pending edits that live in the
        widgets until Apply writes them, and a reload silently replaces them with
        what is still stored. Nothing is gained either -- the database cannot
        have changed under an item the editor is already showing, except through
        the save in :meth:`apply_item`, which reloads deliberately.

        The symptom this fixes is ticks that vanish and have to be entered twice:
        anything that rebuilds the item TABLE (adding a line, reordering one,
        re-selecting) re-enters :meth:`select_item` for the SAME item, and every
        one of those used to wipe the picker."""
        if int(item_id) == self._editor_item_id:
            return
        self._load_editor(int(item_id))

    def _on_item_selected(self) -> None:
        if self._loading:
            return
        row = self.item_table.currentRow()
        if row < 0:
            return
        cell = self.item_table.item(row, 0)
        if cell is None:
            return
        self._item_id = int(cell.data(Qt.UserRole))
        self._reload_editor(self._item_id)
        self._highlight_report_row(self._item_id)

    def add_item(self, name=None, kind="SOSC"):
        """Append an item and select it for editing. The default name is the
        first free ``Item N``: a new line has to exist before it can be named,
        and a prompt here would be a modal on the commonest action in the
        window."""
        if self._report_id is None:
            return None
        if not name:
            taken = {it.name for it in custom.list_items(self.conn, self._report_id)}
            n = len(taken) + 1
            while f"Item {n}" in taken:
                n += 1
            name = f"Item {n}"
        try:
            item_id = custom.add_item(self.conn, self._report_id, name, kind)
        except ValueError as exc:
            self._warn("Add item", str(exc))
            return None
        self._item_id = item_id
        self._load_items()
        return item_id

    def remove_item(self) -> bool:
        if self._item_id is None:
            return False
        item = custom.get_item(self.conn, self._item_id)
        if not self._confirm("Remove item",
                             f"Remove the item {item.name!r} from this report?"):
            return False
        custom.delete_item(self.conn, self._item_id)
        self._item_id = None
        self._load_items()
        return True

    def move_item(self, delta: int) -> bool:
        """Move the selected item up (-1) or down (+1) one place."""
        if self._report_id is None or self._item_id is None:
            return False
        ids = self.item_ids()
        i = ids.index(self._item_id)
        j = i + int(delta)
        if j < 0 or j >= len(ids):
            return False
        ids[i], ids[j] = ids[j], ids[i]
        custom.reorder_items(self.conn, self._report_id, ids)
        self._load_items()
        return True

    # -- the item editor -----------------------------------------------------
    def _clear_editor(self) -> None:
        self._editor_item_id = None
        self._loading = True
        try:
            self.name_edit.clear()
            self.label_edit.clear()
            self.group_edit.clear()
            self.kind_combo.setCurrentIndex(0)
            self.sign_combo.setCurrentIndex(0)
            self.tag_check.setChecked(False)
            # Unchecked = subtotal by tag, which is the default a new item gets.
            self.no_break_tag_check.setChecked(False)
            report_filters.set_item_selections(self.category_picker, (), ())
            report_filters.set_account_picker_ids(self.account_picker, ())
            self.expr_edit.clear()
            self._load_expr_items()
        finally:
            self._loading = False
        self._editor_box.setEnabled(self._item_id is not None)
        self._sync_kind_widgets()

    def _load_editor(self, item_id) -> None:
        item = custom.get_item(self.conn, item_id)
        self._editor_item_id = int(item_id)
        self._loading = True
        try:
            self.name_edit.setText(item.name)
            self.label_edit.setText(item.label or "")
            self.group_edit.setText(item.group_label or "")
            idx = self.kind_combo.findData(item.kind)
            self.kind_combo.setCurrentIndex(max(idx, 0))
            self.sign_combo.setCurrentIndex(0 if int(item.sign) >= 0 else 1)
            self.tag_check.setChecked(bool(item.tag_enabled))
            # ``break_by_tag`` is None (off), () (every tag) or -- for a stored
            # definition written before the tag box went away -- the listed
            # names. Only None ticks the opt-out; a surviving list reads as ON,
            # and re-applying the item keeps it, because this editor merges into
            # the options blob rather than rewriting it.
            self.no_break_tag_check.setChecked(item.break_by_tag is None)
            self.expr_edit.setText(item.expr or "")
            self._load_expr_items()
            report_filters.set_item_selections(
                self.category_picker, custom.item_categories(self.conn, item_id),
                custom.item_accounts(self.conn, item_id))
            report_filters.set_account_picker_ids(
                self.account_picker, custom.item_accounts(self.conn, item_id))
        finally:
            self._loading = False
        self._editor_box.setEnabled(True)
        self._sync_kind_widgets()

    def current_kind(self) -> str:
        data = self.kind_combo.currentData()
        return str(data) if data else custom.IMPLEMENTED_KINDS[0]

    def _build_expr_page(self) -> QWidget:
        """The third picker page: a COMPUTED item's formula, and a list of the
        lines it can name.

        The list is the point. A brace name has to match another item EXACTLY --
        a formula naming a line this report has not got is refused at save, which
        is correct and unhelpful if the names are things like ``W-2:Soc Sec tax
        withhld, spouse`` and the only way in is to retype one. Double-clicking a
        line inserts ``{its name}`` at the cursor, so a formula is assembled from
        the report rather than transcribed from it."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.expr_edit = QLineEdit()
        self.expr_edit.setPlaceholderText("{Gross salary} - {401k deferral}")
        lay.addWidget(QLabel("Formula:"))
        lay.addWidget(self.expr_edit)
        lay.addWidget(QLabel("Double-click a line to insert it:"))
        self.expr_items = QListWidget()
        self.expr_items.itemDoubleClicked.connect(
            lambda it: self.insert_expr_name(it.data(Qt.UserRole)))
        lay.addWidget(self.expr_items, 1)
        hint = QLabel("+ - * / and parentheses. A line reads as the amount it "
                      "shows, after its own sign.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return page

    def insert_expr_name(self, name) -> None:
        """Put ``{name}`` into the formula at the cursor. Public because it is
        what the double-click means, and a test should drive the verb rather
        than synthesize the gesture."""
        if not name:
            return
        self.expr_edit.insert("{%s}" % name)
        self.expr_edit.setFocus()

    def _load_expr_items(self) -> None:
        """Refill the insertable-line list with this report's OTHER items.

        Other, because a formula naming its own line is a cycle -- caught at save
        either way, but an editor that offers the mistake is an editor that
        invites it."""
        self.expr_items.clear()
        if self._report_id is None:
            return
        for item in custom.list_items(self.conn, self._report_id):
            if item.id == self._item_id:
                continue
            entry = QListWidgetItem(item.display_label if item.label
                                    else item.name)
            entry.setData(Qt.UserRole, item.name)
            entry.setToolTip(item.name)
            self.expr_items.addItem(entry)

    def _sync_kind_widgets(self, _idx=None) -> None:
        """Show the picker this kind uses and gray the tag box out where a tag
        means nothing. ``N``/``!N`` are ignored on the balance kinds ("include
        this transaction in an end-of-year balance" is not a coherent
        instruction), so the box says so instead of silently doing nothing."""
        kind = self.current_kind()
        # Three pages, not two. COMPUTED used to fall through to the account
        # picker -- a list of accounts in front of an item that sums other ITEMS,
        # with nowhere to type the formula at all, so the kind was unreachable
        # from this window even though the evaluator has always supported it.
        if kind in CATEGORY_KINDS:
            page = self.category_picker
        elif kind in ACCOUNT_KINDS:
            page = self.account_picker
        else:
            page = self.picker_stack.widget(2)
            self._load_expr_items()
        self.picker_stack.setCurrentWidget(page)
        usable = kind in CATEGORY_KINDS
        self.tag_check.setEnabled(usable)
        self.tag_check.setToolTip(
            "" if usable else
            f"{kind_label(kind)} is a balance, so a per-transaction tag cannot "
            "change it.")
        if not usable and not self._loading:
            self.tag_check.setChecked(False)
        # Eligibility is unchanged: only a kind that sums LINES has anything to
        # bucket. On the others the opt-out is grayed and forced off, because
        # the evaluator will not break them down either way.
        breakable = kind in BREAKDOWN_KINDS
        self.no_break_tag_check.setEnabled(breakable)
        self.no_break_tag_check.setToolTip(
            "" if breakable else
            f"{kind_label(kind)} sums no transactions, so there is nothing to "
            "bucket by tag.")
        if not breakable and not self._loading:
            self.no_break_tag_check.setChecked(False)

    def _item_options(self, item_id, kind) -> dict:
        """The item's stored ``options`` blob with the breakdown folded in.

        MERGED, never replaced: ``options`` also carries per-kind settings this
        editor does not show (an RGAIN ``term``, for one), and writing a fresh
        dict from the two visible widgets would silently drop them.

        The breakdown is the DEFAULT, so leaving it on writes nothing at all --
        the opt-out key is removed and any ``break_by_tag: false`` an older
        build left behind goes with it, or the item would stay off forever with
        the box unticked. A surviving tag LIST is left exactly where it is: the
        editor cannot show it any more, but re-applying an item is no reason to
        silently drop a setting the evaluator still honours.
        """
        options = dict(custom.get_item(self.conn, item_id).options_dict())
        if self.no_break_tag_check.isChecked() and kind in BREAKDOWN_KINDS:
            options[custom.NO_TAG_BREAKDOWN_OPTION] = True
            return options
        options.pop(custom.NO_TAG_BREAKDOWN_OPTION, None)
        if options.get(custom.BREAK_BY_TAG_OPTION) is False:
            options.pop(custom.BREAK_BY_TAG_OPTION, None)
        return options

    def apply_item(self) -> bool:
        """Write the editor back to the selected item, selections included, and
        re-evaluate. One button, because a half-applied item (new kind, old
        selection) is a number nobody can explain."""
        if self._item_id is None:
            return False
        kind = self.current_kind()
        item_id = self._item_id
        try:
            custom.update_item(
                self.conn, item_id,
                name=self.name_edit.text().strip(),
                label=self.label_edit.text().strip() or None,
                group_label=self.group_edit.text().strip() or None,
                kind=kind, sign=int(self.sign_combo.currentData()),
                tag_enabled=1 if (self.tag_check.isChecked()
                                  and kind in CATEGORY_KINDS) else 0,
                options=self._item_options(item_id, kind),
                # Only a COMPUTED item owns a formula. Writing the box through
                # on every kind would let a formula survive a kind change and
                # come back the next time the kind did, which is a stored
                # contradiction nobody typed.
                **({"expr": self.expr_edit.text().strip() or None}
                   if kind == "COMPUTED" else {}))
            if kind in CATEGORY_KINDS:
                custom.set_item_categories(
                    self.conn, item_id,
                    report_filters.category_tree_selections(self.category_picker))
                # For SOSC the same table means "transfers whose far side is
                # here" -- see custom._selected_transfer_ids.
                custom.set_item_accounts(
                    self.conn, item_id,
                    report_filters.transfer_tree_selections(self.category_picker))
            elif kind in ACCOUNT_KINDS:
                custom.set_item_accounts(
                    self.conn, item_id,
                    report_filters.account_picker_ids(self.account_picker))
        except ValueError as exc:
            self._warn("Apply item", str(exc))
            return False
        # The one reload that MUST happen: the row was just written, so the
        # editor should show what was stored (a trimmed name, a normalized
        # option) rather than what was typed at it.
        self._editor_item_id = None
        self._load_items()
        return True

    # -- range and evaluation ------------------------------------------------
    def range_start(self) -> str:
        return date_edit_iso(self.start_edit)

    def range_end(self) -> str:
        return date_edit_iso(self.end_edit)

    def set_range(self, start, end) -> None:
        """Point the current report at ``[start, end]`` and re-evaluate."""
        self._set_date_edits(start, end)
        self.apply_range()

    def apply_range(self) -> None:
        """Re-evaluate over the dates now in the fields, and remember them.

        A report whose range is a FIXED pair is re-bound to what the user just
        typed -- that pair is the definition's own range and there is nothing
        else for it to mean. A report bound to a calendar year or a preset is
        NOT re-bound: the typed dates are a one-off re-pointing (the
        apples-to-apples look at another year), and silently converting "tax
        year 2025" into a pair of dates would destroy the binding that makes it
        re-resolve.
        """
        if self._report_id is not None:
            rd = custom.get_report(self.conn, self._report_id)
            if rd.range_kind == "fixed":
                try:
                    custom.update_report(self.conn, self._report_id,
                                         range_start=self.range_start(),
                                         range_end=self.range_end())
                except ValueError as exc:
                    self._status(str(exc))
        self.refresh()

    def refresh(self) -> None:
        """Re-evaluate and repaint. Nothing is cached: the numbers are computed
        from the ledger every time, so an edit in the register -- or the
        exclusion toggle in this very tree -- is one refresh away from being
        visible here.

        Expansion state survives, keyed by the path down the tree rather than by
        row number, so toggling an exclusion two levels down does not slam the
        branch the user was reading."""
        self._tree = None
        if self._report_id is None:
            self.result_tree.clear()
            self._status("No report selected.")
            return
        open_paths = self._expanded_paths()
        try:
            self._tree = custom.drill_down(
                self.conn, self._report_id, self.range_start(), self.range_end())
        except (ValueError, KeyError) as exc:
            # An item kind that has not landed yet, or an unresolvable range.
            # The window says what is wrong where the user is looking; it does
            # not pop a modal and it does not leave stale numbers on screen.
            self.result_tree.clear()
            self._status(f"This report cannot be evaluated: {exc}")
            return
        self._fill_results(self._tree, open_paths)

    def _fill_results(self, tree, open_paths=None) -> None:
        """Rebuild the tree from the pure projection.

        Depth re-nesting is the same trick the Itemize drill-down uses: the
        projection is a flat list carrying each row's depth, and a stack turns it
        back into parents and children, so the export and the screen walk one
        row list."""
        rows = drill_tree_rows(tree)
        self.result_tree.clear()
        stack: list = []
        for row in rows:
            widget = QTreeWidgetItem(list(row.cells))
            widget.setData(0, Qt.UserRole, row)
            self._style_row(widget, row)
            while len(stack) > row.depth:
                stack.pop()
            if stack:
                stack[-1].addChild(widget)
            else:
                self.result_tree.addTopLevelItem(widget)
            stack.append(widget)
        self._restore_expanded(open_paths)
        self.result_tree.resizeColumnToContents(0)
        self._status("")

    def _style_row(self, widget, row) -> None:
        """One row's fonts, alignment and marks.

        A struck-through row is a line an exclusion tag pushed out: it is drawn
        with its real amount so the user can see what he removed, and the strike
        is what says the number above it does not include this."""
        from PyQt5.QtGui import QFont
        last = len(DRILL_COLUMNS) - 1
        widget.setTextAlignment(last, Qt.AlignRight | Qt.AlignVCenter)
        if row.bold or row.excluded:
            font = QFont(widget.font(0))
            font.setBold(bool(row.bold))
            font.setStrikeOut(bool(row.excluded))
            for col in range(len(DRILL_COLUMNS)):
                widget.setFont(col, font)
        if row.marks:
            widget.setIcon(0, warning_triangle_icon())
            tip = row.tooltip
            for col in range(len(DRILL_COLUMNS)):
                widget.setToolTip(col, tip)
        if row.excluded:
            widget.setToolTip(
                0, f"Excluded from {row.item_name!r} by the tag "
                   f"'{ledger.exclusion_tag_name(row.item_name)}'. "
                   "Right-click to put it back.")

    def _status(self, text) -> None:
        self.status_label.setText(str(text or ""))

    # -- expansion state ------------------------------------------------------
    def _row_path(self, widget) -> tuple:
        """A widget's identity for expansion purposes: its first-column labels
        from the root down. Row NUMBERS would not survive a refresh that changed
        what a line matched, which is exactly the refresh this has to survive."""
        parts = []
        node = widget
        while node is not None:
            parts.append(node.text(0))
            node = node.parent()
        return tuple(reversed(parts))

    def _expanded_paths(self) -> set:
        out: set = set()

        def walk(widget) -> None:
            if widget.isExpanded():
                out.add(self._row_path(widget))
            for i in range(widget.childCount()):
                walk(widget.child(i))

        for i in range(self.result_tree.topLevelItemCount()):
            walk(self.result_tree.topLevelItem(i))
        return out

    def _restore_expanded(self, open_paths) -> None:
        """Re-open what was open. With nothing remembered (a first paint, or a
        report just selected) the line items open and nothing below them does --
        the report reads as the list of tax lines it has always been until the
        user asks a question of one."""
        def walk(widget) -> None:
            row = widget.data(0, Qt.UserRole)
            if open_paths is None:
                widget.setExpanded(bool(row is not None and row.expanded))
            else:
                widget.setExpanded(self._row_path(widget) in open_paths)
            for i in range(widget.childCount()):
                walk(widget.child(i))

        for i in range(self.result_tree.topLevelItemCount()):
            walk(self.result_tree.topLevelItem(i))

    # -- reading the tree (the test seam) ------------------------------------
    def tree_rows(self) -> list:
        """What the tree is showing, as ``(depth, first cell, amount)`` -- the
        shape a test asserts against without touching Qt's item API."""
        out: list = []

        def walk(widget, depth) -> None:
            out.append((depth, widget.text(0), widget.text(len(DRILL_COLUMNS) - 1)))
            for i in range(widget.childCount()):
                walk(widget.child(i), depth + 1)

        for i in range(self.result_tree.topLevelItemCount()):
            walk(self.result_tree.topLevelItem(i), 0)
        return out

    def drill_rows(self) -> list:
        """The :class:`DrillRow`\\s behind the tree, in display order."""
        out: list = []

        def walk(widget) -> None:
            row = widget.data(0, Qt.UserRole)
            if row is not None:
                out.append(row)
            for i in range(widget.childCount()):
                walk(widget.child(i))

        for i in range(self.result_tree.topLevelItemCount()):
            walk(self.result_tree.topLevelItem(i))
        return out

    def column_headers(self) -> list:
        """The tree's header texts."""
        return [self.result_tree.headerItem().text(c)
                for c in range(self.result_tree.columnCount())]

    # -- the exclusion toggle (the one write this window makes) ---------------
    def _row_menu(self, pos) -> None:
        """The right-click menu on a transaction row.

        Only a transaction row has one. Everything above it is an aggregate, and
        "exclude this category" is not a thing the tag model can express -- it
        would have to write a tag onto every line underneath, which is a
        different and much larger promise than the one the user is making."""
        widget = self.result_tree.itemAt(pos)
        if widget is None:
            return
        row = widget.data(0, Qt.UserRole)
        if row is None or row.kind != "txn":
            return
        self._show_row_menu(
            row, self.result_tree.viewport().mapToGlobal(pos))

    def row_menu_entries(self, row) -> list:
        """The menu for one row as ``(label, enabled)`` pairs.

        Pure, and separate from popping the menu, because WHAT is offered is a
        rule -- and a rule reachable only by right-clicking a live popup is a
        rule no test can hold. A report line whose name cannot BE a tag is shown
        a disabled entry saying why, rather than a working-looking one that
        refuses on click: the refusal is a property of the line, knowable before
        the user commits to the gesture."""
        label = ("Include in %s" % row.item_name if row.excluded
                 else "Exclude from %s" % row.item_name)
        if "," in (row.item_name or ""):
            return [(label, False),
                    ("(this line's name contains a comma, which a tag "
                     "cannot hold)", False)]
        return [(label, True)]

    def _show_row_menu(self, row, global_pos) -> None:
        """Pop the menu and act on what was chosen. The ONE modal the tree opens,
        and therefore an overridable seam: a ``QMenu.exec_`` blocks forever under
        the offscreen platform, so a test drives :meth:`toggle_exclusion` through
        this method rather than by answering a popup."""
        menu = QMenu(self.result_tree)
        first = None
        for text, enabled in self.row_menu_entries(row):
            action = menu.addAction(text)
            action.setEnabled(enabled)
            if first is None and enabled:
                first = action
        if first is not None and menu.exec_(global_pos) is first:
            self.toggle_exclusion(row)

    def toggle_exclusion(self, row) -> bool:
        """Add or remove the exclusion tag on one line, then repaint.

        The RULE about where an exclusion may live belongs to
        :func:`mammon.ledger.toggle_report_exclusion`, not here -- a split leg
        holds one tag and a parent's tags reach every leg, and both of those are
        facts about tag storage. This method only asks, repaints, and puts the
        refusal where the user is looking. A refusal is not a choice, so it never
        becomes a modal."""
        try:
            ledger.toggle_report_exclusion(
                self.conn, row.item_name, row.txn_id, row.split_id)
        except (ValueError, KeyError) as exc:
            self._status(str(exc))
            return False
        self.refresh()
        return True

    # -- export --------------------------------------------------------------
    def to_csv(self) -> str:
        if self._tree is None:
            return report_def_to_csv(custom.DrillTree(
                report_id=self._report_id or 0, name="", start="", end=""))
        return report_def_to_csv(self._tree)

    def export_csv_to(self, path) -> None:
        """Write the drill-down to ``path`` as CSV, flattened with the first
        column indented by depth so the hierarchy survives.

        Testable seam: the file dialog lives in :meth:`_export_csv_dialog`; this
        method takes an explicit path so a test can drive the write without a
        modal.
        """
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(self.to_csv())

    def _export_csv_dialog(self) -> None:
        name = (self._tree.name if self._tree else "report") or "report"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export report as CSV", f"{name}.csv", "CSV files (*.csv)")
        if path:
            self.export_csv_to(path)

    # -- print ---------------------------------------------------------------
    def visible_drill_rows(self) -> list:
        """The rows a printout would carry: exactly what is on screen.

        Expansion state IS the user's statement of what he wants on paper -- he
        has already opened the lines he is checking and closed the ones he is not
        -- so a row under a collapsed parent is not printed, and there is no
        "print all levels" option because the tree already is one."""
        out: list = []

        def walk(widget):
            row = widget.data(0, Qt.UserRole)
            if row is not None:
                out.append(row)
            if widget.isExpanded():
                for i in range(widget.childCount()):
                    walk(widget.child(i))

        for i in range(self.result_tree.topLevelItemCount()):
            walk(self.result_tree.topLevelItem(i))
        return out

    def print_title(self) -> str:
        return (self._tree.name if self._tree else "") or "Report"

    def print_subtitle(self) -> str:
        if self._tree is None:
            return ""
        from mammon.ui.models import fmt_date
        return "%s to %s" % (fmt_date(self._tree.start), fmt_date(self._tree.end))

    def print_html(self, settings=None) -> str:
        """The printable HTML for what is on screen. Pure enough to assert on."""
        from mammon.ui import report_print
        return report_print.drill_print_html(
            self.visible_drill_rows(), self.print_title(),
            settings or report_print.PrintSettings(),
            subtitle=self.print_subtitle())

    def print_to_pdf(self, path, settings=None) -> str:
        """Render the visible report to a PDF at ``path``. Testable seam: no
        dialog, an explicit path, and the same HTML the printer gets."""
        from mammon.ui import printing
        return printing.render_html_to_pdf(
            self.print_html(settings), str(path), title=self.print_title())

    def _ask_print_settings(self, rows, printer):
        """The setup modal. One overridable method, like every other choice in
        this window, so a test prints without answering a dialog."""
        from mammon.ui.report_print_dialog import PrintSetupDialog
        dlg = PrintSetupDialog(rows, self._print_settings, parent=self,
                               printer=printer)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.settings()

    def _print_dialog(self) -> None:
        from PyQt5.QtPrintSupport import QPrinter, QPrintDialog
        from mammon.ui import printing, report_print

        rows = self.visible_drill_rows()
        if not rows:
            self._warn("Print", "There is nothing on screen to print.")
            return
        printer = QPrinter(QPrinter.HighResolution)
        printer.setDocName("Mammon - %s" % self.print_title())
        settings = self._ask_print_settings(rows, printer)
        if settings is None:
            return
        # Remembered for the next printout: a user who chose landscape and 8pt
        # for this report wants them again, and re-choosing every time is how a
        # setup window becomes the reason not to print.
        self._print_settings = settings
        printer.setOrientation(QPrinter.Landscape if settings.landscape
                               else QPrinter.Portrait)
        # The page budget is re-measured against the printer the USER picked,
        # which may not be the one the preview assumed.
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("Print")
        if dlg.exec_() != QDialog.Accepted:
            return
        settings = settings.with_width(
            report_print.page_width_chars(settings, printer))
        html_str = report_print.drill_print_html(
            rows, self.print_title(), settings, subtitle=self.print_subtitle())
        try:
            printing.render_html_to_printer(html_str, printer)
        except Exception as exc:          # pragma: no cover - printer/IO failure
            self._warn("Print", "Could not print:\n%s" % exc)

    def txf_record_count(self) -> int:
        """How many items WOULD be exported to TXF over the current range.

        Zero is the ordinary answer for a non-tax report: only refnum-bearing,
        non-``COMPUTED`` items emit. The button asks this so the user learns
        that before a file dialog rather than after an empty file."""
        if self._report_id is None:
            return 0
        try:
            return len(custom_export.txf_records(
                self.conn, self._report_id,
                self.range_start(), self.range_end()))
        except (ValueError, KeyError):
            return 0

    def export_txf_to(self, path, *, export_date=None) -> int:
        """Write a TXF v042 file for the current range and return its item count.

        Testable seam, matching :meth:`export_csv_to`: no dialog, an explicit
        path, and ``export_date`` exposed so a test can assert bytes without
        owning the clock. TXF describes ONE tax year, so the export always uses
        the range in the date fields.
        """
        return custom_export.export_txf_to(
            self.conn, self._report_id, path,
            self.range_start(), self.range_end(), export_date=export_date)

    def _export_txf_dialog(self) -> None:
        if self._report_id is None:
            return
        if self.txf_record_count() == 0:
            self._warn("Export TXF",
                       "No item in this report carries a TXF reference "
                       "number, so a TXF file would hold no lines.")
            return
        name = (self._tree.name if self._tree else "") or "report"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export tax data as TXF", f"{name}.txf", "TXF files (*.txf)")
        if path:
            self.export_txf_to(path)
