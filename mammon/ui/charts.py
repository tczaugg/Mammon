"""mammon.ui.charts -- matplotlib-backed chart widgets for Mammon's reports.

Kept in its OWN module so importing :mod:`mammon.ui` (models / widgets) stays
cheap and matplotlib-free for headless/model use; :class:`MainWindow` imports
this lazily, only when a chart menu action fires. The number crunching lives in
:mod:`mammon.reports.charts`; here we only render its plain data structures onto
an embedded Figure (no ``pyplot`` -- we drive the object API so nothing depends
on a global figure/backend state).
"""
from __future__ import annotations

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QToolTip, QVBoxLayout

from .models import fmt_date  # single date-display chokepoint (honors the pref)
from . import style            # active theme (dark/light) -- same source the register reads

# A calm, Quicken-ish categorical palette (blue-led), reused across the category
# pies. It carries enough VISUALLY DISTINCT hues for the worst realistic pie --
# ~19 wedges, which happens when every real category is roughly 5% of the period
# and the sub-10% tail rolls up into an ``Other`` that is itself >=10%. The ten
# calm lead colours are unchanged, so the common (few-category) pie looks exactly
# as before; the tail extends them with further separated hues. The FINAL entry
# is a neutral gray RESERVED for the ``Other`` wedge: pinning ``Other`` there
# (see :func:`wedge_colors`) keeps it a stable, recognizable colour no matter how
# many real categories precede it. The old 10-colour list wrapped with
# ``i % len``, so an 11th category collided with -- and was indistinguishable
# from -- the ``Other`` wedge.
_PIE_PALETTE = [
    "#3b6ea5",  # blue
    "#e0803a",  # orange
    "#4f9d69",  # green
    "#c0504d",  # red
    "#8064a2",  # purple
    "#4bacc6",  # cyan
    "#d9a441",  # gold
    "#9bbb59",  # yellow-green
    "#a5568c",  # plum
    "#5b9bd5",  # sky blue
    "#e15759",  # coral
    "#1b9e77",  # teal
    "#7570b3",  # periwinkle
    "#e7298a",  # magenta
    "#a6761d",  # bronze
    "#66a61e",  # grass green
    "#b15928",  # rust
    "#6a3d9a",  # deep violet
    "#f2c80f",  # bright yellow
    "#8a949e",  # neutral gray -- RESERVED for the 'Other' wedge (always last)
]
_OTHER_LABEL = "Other"
_BLUE = "#3b6ea5"
_RED = "#c0504d"          # SPENDING bars (money out)
_GREEN = "#4f9d69"        # INCOME bars (money in)

# Grid-line weight/opacity for the Net Worth chart. matplotlib's default y-grid
# (alpha 0.25, no vertical lines) reads as barely-there hairlines; these draw a
# crisp ~1px line at near-full opacity on BOTH axes, matching the app's financial
# calendar table grid (a solid 1px line in the theme's ``grid`` colour) so values
# read against horizontal and vertical guides alike.
_GRID_LINEWIDTH = 0.8
_GRID_ALPHA = 0.9
_GRID_LIGHT = "#c9ced8"   # light-theme grid colour (charts skip the dark palette in light mode)


def wedge_colors(labels, group_label=_OTHER_LABEL, palette=None):
    """Map each wedge label to a stable colour. Real categories take the leading
    palette entries in order; the ``group_label`` (``Other``) wedge ALWAYS takes
    the palette's FINAL entry, so it never shares a colour with a real category
    however many divisions there are. With 18 real categories plus ``Other`` --
    the worst realistic pie -- all 19 wedges get distinct colours.

    Pinning ``Other`` to the last slot (rather than letting it fall wherever the
    slice order put it and wrapping the list past its length) is the whole fix:
    before, an 11th category wrapped back onto ``Other``'s colour and the two
    were indistinguishable.
    """
    pal = palette if palette is not None else _PIE_PALETTE
    non_other = pal[:-1]
    other_color = pal[-1]
    colors, i = [], 0
    for lab in labels:
        if lab == group_label:
            colors.append(other_color)
        else:
            colors.append(non_other[i % len(non_other)])
            i += 1
    return colors


def _dollars(cents: int) -> str:
    """Integer-cents -> '$1,234.56' (matches the register's money display)."""
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def _chart_palette(for_print: bool = False):
    """The active theme's palette dict when the app is in DARK mode, else ``None``.

    These canvases drive matplotlib's object API, so they consult
    :mod:`mammon.ui.style` -- the SAME theme source the register and account bar
    read -- rather than inventing a chart-specific mechanism. Light mode returns
    ``None`` so the figure keeps matplotlib's default (original) light styling
    byte-for-byte; dark mode returns the palette so the figure, axes, ticks,
    labels and bars can be recoloured to stay legible on the dark background.

    ``for_print`` forces ``None`` regardless of the active theme, so a chart
    printed or exported to PDF is always drawn on a white page with black chrome
    -- the same "print is always white" rule the report/register HTML export
    follows (:mod:`mammon.ui.printing`, :func:`report_window.report_rows_to_html`).
    """
    if for_print:
        return None
    return style.palette_for("dark") if style.theme() == "dark" else None


def _active_palette(for_print: bool = False) -> dict:
    """The ACTIVE theme's palette dict, light INCLUDED.

    :func:`_chart_palette` deliberately returns ``None`` in light mode so the
    chrome matplotlib already draws well (near-black ticks on white) is left
    byte-for-byte alone. That rule cannot serve a colour the chart must pick for
    ITSELF in both themes -- a series line, a shaded band -- because there is no
    matplotlib default to fall back on: hardcoding one gives a hue tuned for one
    background and unreadable on the other, which is exactly what the investment
    dashboard's two hole charts were reported for. Those colours come from here,
    so ``style.py``'s semantic names (``blue``, ``negative``, ``muted``) stay the
    single source of truth in either theme. ``for_print`` forces the light
    palette, matching :func:`_chart_palette`'s "print is always white" rule."""
    if for_print:
        return style.palette_for("light")
    return style.palette_for(style.theme())


def _theme_grid(ax, pal, *, axis: str = "both") -> None:
    """Turn on a subtle-but-visible grid, coloured for the active theme.

    One helper so every chart's grid has the same weight and the same colour
    rule: the dark palette's ``line`` when ``pal`` is a dark palette, the light
    grid grey otherwise. ``set_axisbelow`` keeps the lines BEHIND the data, so a
    grid never crosses a bar or a filled band."""
    ax.set_axisbelow(True)
    ax.grid(True, axis=axis, color=(pal["line"] if pal else _GRID_LIGHT),
            linewidth=_GRID_LINEWIDTH, alpha=_GRID_ALPHA)


def _theme_axes_chrome(ax, pal) -> None:
    """Recolour an axes' spines, tick marks, tick labels, y-grid and title from a
    dark palette. A no-op when ``pal`` is ``None`` (light mode keeps matplotlib's
    defaults, so the classic light look is unchanged)."""
    if pal is None:
        return
    ax.tick_params(axis="both", colors=pal["line"], labelcolor=pal["text"])
    for spine in ax.spines.values():
        spine.set_color(pal["line"])
    if ax.title.get_text():
        ax.title.set_color(pal["text"])
    for gridline in ax.get_ygridlines():
        gridline.set_color(pal["line"])


def _theme_pie_chrome(ax, texts, pal) -> None:
    """Recolour a pie's title and its slice labels from a dark palette. A no-op
    when ``pal`` is ``None`` (light mode keeps matplotlib's black text). The
    autopct percentages sit ON the coloured wedges (mid-tone in both themes), so
    they keep matplotlib's black -- legible on the wedge in either mode."""
    if pal is None:
        return
    if ax.title.get_text():
        ax.title.set_color(pal["text"])
    for t in texts:
        t.set_color(pal["text"])


class SpendingPieCanvas(FigureCanvasQTAgg):
    """A pie chart of top-level categories from a ``SpendingPie`` payload. The
    same canvas renders the spending (expense) pie and the income pie -- pass
    ``title`` and ``empty_text`` to label which. Colour treatment is identical
    for both (the shared categorical palette)."""

    def __init__(self, pie, parent=None, *, title="Spending by Category",
                 empty_text="No spending in this period", for_print=False):
        fig = Figure(figsize=(6.4, 4.8), tight_layout=True)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        self._pie = pie
        self._title = title
        self._empty_text = empty_text
        self._for_print = for_print
        self.render()

    def render(self) -> None:
        """(Re)draw the pie, re-reading the ACTIVE theme every time (the same
        idiom as :class:`SpendingBarCanvas`): a dark->light toggle followed by a
        re-render snaps the figure back to white, and ``for_print`` forces white
        so a printed/exported pie is legible on paper regardless of theme."""
        pie = self._pie
        fig = self.figure
        fig.clear()
        pal = _chart_palette(self._for_print)
        fig.patch.set_facecolor(pal["window"] if pal else "white")
        ax = fig.add_subplot(111)
        if pal is not None:
            ax.set_facecolor(pal["surface"])
        if pie.is_empty():
            ax.text(0.5, 0.5, self._empty_text,
                    ha="center", va="center", fontsize=11,
                    color=pal["muted"] if pal else "#8a94a0")
            ax.axis("off")
            self.draw_idle()
            return

        labels = [s.label for s in pie.slices]
        sizes = [s.cents for s in pie.slices]
        colors = wedge_colors(labels)

        def _autopct(pct):
            cents = int(round(pct / 100.0 * pie.total_cents))
            return f"{pct:.0f}%\n{_dollars(cents)}"

        _wedges, texts, _autotexts = ax.pie(
            sizes, labels=labels, colors=colors, autopct=_autopct,
            startangle=90, counterclock=False, textprops={"fontsize": 8})
        ax.axis("equal")
        ax.set_title(f"{self._title}   {fmt_date(pie.start)} to "
                     f"{fmt_date(pie.end)}\n"
                     f"Total {_dollars(pie.total_cents)}", fontsize=10)
        _theme_pie_chrome(ax, texts, pal)
        self.draw_idle()


def group_small_slices(slices, target_pct: float, group_label: str = "Other"):
    """``(drawn, grouped)``: the slices to draw, with the SMALLEST-share
    categories folded into one ``group_label`` slice, and the members that went
    into it.

    The ``Other`` bucket is the SET OF LOWEST categories whose combined share
    reaches ``target_pct`` of the total: accumulate categories from the smallest
    upward until their running sum is at least ``target_pct`` percent of the
    whole, and those become ``Other`` (the category that tips the sum past the
    bar is included). Everything else is drawn individually, largest first.

    This is the readability fix, and it is Quicken's in spirit: a pie of a dozen
    tiny holdings puts a dozen labels on top of each other around the same arc
    and stops carrying information, so the low tail collapses into one wedge
    worth clicking while the meaningful categories stay separate. Rolling up
    *by combined share* (rather than a per-slice threshold) means "Other" is
    always a real, clickable fraction of the pie -- never a sliver, never most
    of it.

    Two guards keep the rule from lying: a SINGLE small slice is never renamed
    "Other" (it would hide a name to describe one thing), and when the roll-up
    would swallow every category the pie is drawn ungrouped rather than
    collapsing the whole chart into one wedge.
    """
    rows = [(lab, int(c)) for lab, c in slices if c > 0]
    total = sum(c for _, c in rows)
    if total <= 0:
        return rows, []
    # Accumulate from the smallest share upward until the running total reaches
    # target_pct of the whole; that lowest set is the Other bucket.
    small: list = []
    acc = 0
    for r in sorted(rows, key=lambda r: r[1]):      # smallest first
        small.append(r)
        acc += r[1]
        if acc / total * 100.0 >= target_pct:
            break
    small_ids = {id(r) for r in small}
    keep = [r for r in rows if id(r) not in small_ids]   # original order kept
    if len(small) < 2 or not keep:
        return rows, []
    # A slice genuinely CALLED "Other" (there is an Other asset class) joins the
    # group rather than sitting beside a second wedge with the same name -- two
    # identically labelled wedges are unreadable and ambiguous to click.
    named_other = [r for r in keep if r[0] == group_label]
    if named_other:
        keep = [r for r in keep if r[0] != group_label]
        small = small + named_other
        if not keep:
            return rows, []
    grouped = sorted(small, key=lambda r: (-r[1], r[0].lower()))  # largest first
    return keep + [(group_label, sum(c for _, c in grouped))], grouped


class SlicesPieCanvas(FigureCanvasQTAgg):
    """A pie of labelled cent amounts -- the asset-allocation chart. Takes
    plain ``[(label, cents)]`` so any grouping can be drawn without a payload
    class of its own.

    Two rules keep it readable, both about the same failure: labels crowding
    the same arc until none can be read. The lowest-share categories that
    together make up :attr:`GROUP_TARGET_PCT` of the total are folded into one
    **Other** wedge (:func:`group_small_slices`), and any wedge still under
    :attr:`LABEL_MIN_PCT` of the *drawn* pie is drawn without inline text -- a
    sliver's label would have landed on its neighbour's. Every wedge stays
    identifiable regardless: the un-labelled slivers get a **hover tooltip**
    (:meth:`_on_motion`) naming the category, its share of the whole and its
    dollar amount, so nothing on the pie is anonymous.

    **Colours are stable and distinct** (:func:`wedge_colors`): the palette
    carries enough separated hues for the worst realistic pie (~19 wedges), and
    the **Other** wedge is pinned to the palette's final neutral colour so it
    never collides with a real category as the division count grows.

    **Percentages are of the whole.** Every wedge's label shows its share of the
    overall period total (:meth:`whole_total`), NOT its share of the subset it
    sits in. Drilling into Other keeps each member's percentage honest: a
    category that is 3% of the whole reads "3%" even when it is 40% of the Other
    it was drilled into.

    **Other opens.** Clicking it redraws the pie as its members alone, so their
    slices are readable at full size (while their percentages still read against
    the whole); clicking anywhere off the pie (or the container's Back button,
    via :attr:`zoomChanged`) comes back out. Without a way back a drill-down is
    a trap, so the two always ship together.
    """

    GROUP_LABEL = "Other"
    GROUP_TARGET_PCT = 10.0
    LABEL_MIN_PCT = 5.0
    # The drill path, [] at the top level -- a container shows/hides its Back
    # button on this.
    zoomChanged = pyqtSignal(object)

    def __init__(self, title, slices, parent=None, *, empty_text="Nothing to show",
                 group_target_pct=None, label_min_pct=None, for_print=False):
        fig = Figure(figsize=(5.2, 3.6), tight_layout=True)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        self._title = title
        self._empty_text = empty_text
        self._for_print = for_print
        self._base = [(lab, int(c)) for lab, c in slices if c > 0]
        self._stack: list = []          # [(label, members)] -- one entry per drill
        self._drawn: list = []          # [(label, cents)] actually on the figure
        self._grouped: list = []        # what the Other wedge holds, if any
        self._wedges: list = []         # [(label, matplotlib wedge)]
        self._tooltips: dict = {}       # {label: hover text} -- names every wedge
        self.group_target_pct = (self.GROUP_TARGET_PCT if group_target_pct is None
                                 else float(group_target_pct))
        self.label_min_pct = (self.LABEL_MIN_PCT if label_min_pct is None
                              else float(label_min_pct))
        self.mpl_connect("button_press_event", self._on_click)
        self.mpl_connect("motion_notify_event", self._on_motion)
        self.render()

    # -- state ------------------------------------------------------------
    def current_slices(self) -> list:
        """What this level of the drill is a pie of."""
        return list(self._stack[-1][1]) if self._stack else list(self._base)

    def drawn_slices(self) -> list:
        """``[(label, cents)]`` as drawn -- grouping applied."""
        return list(self._drawn)

    def grouped_members(self) -> list:
        """What the Other wedge holds ( ``[]`` when there is no Other wedge)."""
        return list(self._grouped)

    def has_group(self) -> bool:
        return bool(self._grouped)

    def zoom_path(self) -> list:
        return [lab for lab, _ in self._stack]

    def visible_labels(self) -> list:
        """The labels actually printed around the pie: every wedge at or above
        :attr:`label_min_pct` of the DRAWN pie (a visual-crowding test, so it is
        the subset share that decides whether text fits), plus Other (which is
        the one you click)."""
        total = sum(c for _, c in self._drawn)
        if total <= 0:
            return []
        return [lab for lab, c in self._drawn
                if lab == self.GROUP_LABEL or c / total * 100.0 >= self.label_min_pct]

    def whole_total(self) -> int:
        """The overall period total -- the single denominator for every wedge's
        percentage, at any drill depth. A drilled slice shows its share of THIS,
        not of the Other subset it was drilled into, so "3% of everything" never
        masquerades as "40% of Other"."""
        return sum(c for _, c in self._base)

    def slice_percentages(self) -> list:
        """``[(label, pct)]`` for the drawn slices, each ``pct`` the slice's
        share of :meth:`whole_total` -- the numbers rendered on the wedges."""
        whole = self.whole_total()
        if whole <= 0:
            return [(lab, 0.0) for lab, _ in self._drawn]
        return [(lab, c / whole * 100.0) for lab, c in self._drawn]

    # -- zooming ------------------------------------------------------------
    def drill_into_other(self) -> bool:
        """Redraw as the members of the Other wedge. False when there is none."""
        if not self._grouped:
            return False
        self._stack.append((self.GROUP_LABEL, list(self._grouped)))
        self.render()
        self.zoomChanged.emit(self.zoom_path())
        return True

    def zoom_out(self) -> bool:
        """Back out one level. False when already at the top."""
        if not self._stack:
            return False
        self._stack.pop()
        self.render()
        self.zoomChanged.emit(self.zoom_path())
        return True

    def _on_click(self, event) -> None:
        for label, wedge in self._wedges:
            hit, _ = wedge.contains(event)
            if hit:
                if label == self.GROUP_LABEL and self._grouped:
                    self.drill_into_other()
                return
        self.zoom_out()          # a click off the pie is the way back out

    def tooltip_for_event(self, event) -> str | None:
        """The hover text for the wedge under ``event`` (``None`` when the cursor
        is off the pie). Split out from :meth:`_on_motion` so the mapping from a
        wedge to its ``name: pct of total, $amount`` string is testable without a
        live Qt tooltip."""
        for label, wedge in self._wedges:
            hit, _ = wedge.contains(event)
            if hit:
                return self._tooltips.get(label, label)
        return None

    def _on_motion(self, event) -> None:
        """Show a per-wedge tooltip on hover, so a wedge too small to carry an
        inline label is still identifiable. Hidden when the cursor leaves the
        pie."""
        text = self.tooltip_for_event(event)
        if text:
            QToolTip.showText(QCursor.pos(), text, self)
        else:
            QToolTip.hideText()

    # -- drawing ------------------------------------------------------------
    def render(self) -> None:
        """(Re)draw the pie, re-reading the ACTIVE theme every time (the same
        idiom as :class:`SpendingBarCanvas`), so a live dark<->light toggle is a
        full undo and ``for_print`` forces a white page regardless of theme."""
        fig = self.figure
        fig.clear()
        pal = _chart_palette(self._for_print)
        fig.patch.set_facecolor(pal["window"] if pal else "white")
        ax = fig.add_subplot(111)
        if pal is not None:
            ax.set_facecolor(pal["surface"])
        self._wedges, self._drawn, self._grouped = [], [], []
        self._tooltips = {}
        slices = self.current_slices()
        total = sum(c for _, c in slices)
        if not slices or total <= 0:
            ax.text(0.5, 0.5, self._empty_text, ha="center", va="center",
                    fontsize=11, color=pal["muted"] if pal else "#8a94a0")
            ax.axis("off")
            self.draw_idle()
            return
        drawn, grouped = group_small_slices(slices, self.group_target_pct,
                                            self.GROUP_LABEL)
        self._drawn, self._grouped = drawn, grouped
        shown = set(self.visible_labels())
        labels = [lab if lab in shown else "" for lab, _ in drawn]
        sizes = [c for _, c in drawn]
        colors = wedge_colors([lab for lab, _ in drawn], self.GROUP_LABEL)
        whole = self.whole_total() or total

        # Every wedge is identifiable: big ones carry an inline label+percent,
        # and the slivers whose text was suppressed (they would land on a
        # neighbour) get a hover tooltip instead -- category name, its share of
        # the WHOLE period, and the dollar amount. Built for ALL wedges so the
        # hovered exact dollars are available even on a labelled wedge.
        self._tooltips = {
            lab: f"{lab}: {(c / whole * 100.0 if whole else 0.0):.1f}% of total, "
                 f"{_dollars(c)}"
            for lab, c in drawn
        }

        # matplotlib calls autopct once per wedge in ``sizes`` order, so a plain
        # counter maps each call back to its exact cents -- no re-deriving the
        # amount from the (subset-relative) pct matplotlib hands us. The number
        # shown is the slice's share of the WHOLE, and text is suppressed only
        # for wedges too small in the DRAWN pie to carry a legible label.
        seq = {"i": 0}

        def _autopct(_pct):
            i = seq["i"]
            seq["i"] += 1
            lab, cents = drawn[i]
            if lab not in shown:
                return ""                       # a sliver's text lands on its neighbour
            return f"{cents / whole * 100.0:.0f}%\n{_dollars(cents)}"

        wedges, texts, *_ = ax.pie(sizes, labels=labels, colors=colors,
                                   autopct=_autopct, startangle=90,
                                   counterclock=False, textprops={"fontsize": 8})
        self._wedges = list(zip([lab for lab, _ in drawn], wedges))
        ax.axis("equal")
        ax.set_title(self._title_text(total), fontsize=10)
        _theme_pie_chrome(ax, texts, pal)
        self.draw_idle()

    def _title_text(self, total: int) -> str:
        head = self._title
        if self._stack:
            sep = " ▸ "                 # breadcrumb: "By asset class > Other"
            head = head + sep + sep.join(self.zoom_path())
        text = f"{head}   Total {_dollars(total)}"
        if self._stack:
            text += "\n(click outside the pie to go back)"
        elif self._grouped:
            text += (f"\n(smallest categories totalling {self.group_target_pct:.0f}% "
                     f"grouped as {self.GROUP_LABEL} -- click it to break it out)")
        return text

class NetWorthCanvas(FigureCanvasQTAgg):
    """A line/area chart of net worth over time from a ``NetWorthSeries``."""

    def __init__(self, series, parent=None, *, for_print=False):
        fig = Figure(figsize=(6.4, 4.8), tight_layout=True)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        self._series = series
        self._for_print = for_print
        self.render()

    def render(self) -> None:
        """(Re)draw the line/area chart, re-reading the ACTIVE theme every time
        (the same idiom as :class:`SpendingBarCanvas`): a dark->light toggle plus
        re-render restores the white figure, and ``for_print`` forces white so a
        printed/exported net-worth chart stays legible on paper."""
        series = self._series
        fig = self.figure
        fig.clear()
        pal = _chart_palette(self._for_print)
        fig.patch.set_facecolor(pal["window"] if pal else "white")
        ax = fig.add_subplot(111)
        if pal is not None:
            ax.set_facecolor(pal["surface"])
        if series.is_empty():
            ax.text(0.5, 0.5, "No transactions to chart",
                    ha="center", va="center", fontsize=11,
                    color=pal["muted"] if pal else "#8a94a0")
            ax.axis("off")
            self.draw_idle()
            return

        xs = [p.date for p in series.points]
        ys = [p.cents / 100.0 for p in series.points]
        idx = list(range(len(xs)))
        baseline = min(0.0, min(ys))
        ax.plot(idx, ys, color=_BLUE, linewidth=1.8, marker="o", markersize=3)
        ax.fill_between(idx, ys, baseline, color=_BLUE, alpha=0.12)
        ax.axhline(0, color=_RED, linewidth=0.8, alpha=0.6)
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: _dollars(int(round(v * 100)))))

        step = max(1, len(xs) // 8)
        ticks = list(range(0, len(xs), step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([fmt_date(xs[i]) for i in ticks], rotation=45,
                           ha="right", fontsize=8)
        ax.set_title("Net Worth Over Time", fontsize=10)
        # Both axes, crisp: horizontal lines that were too faint get stronger,
        # and vertical lines (aligned to the x-axis ticks set above) are added,
        # matching the financial calendar's grid weight/opacity. In dark mode the
        # colour is the palette's line colour -- the same value _theme_axes_chrome
        # gives the y-gridlines -- so both axes stay consistent.
        _theme_grid(ax, pal, axis="both")
        _theme_axes_chrome(ax, pal)
        self.draw_idle()


class SpendingBarCanvas(FigureCanvasQTAgg):
    """Grouped income/spending bars per period from a ``SpendingByPeriod``.

    Two bars per bucket: SPENDING (money out) in red and INCOME (money in) in
    green, so a month's cash flow -- what came in against what went out -- reads
    at a glance.

    The value axis deliberately does NOT start at zero. It auto-scales to the
    data's own min-max with a little padding, because on a real ledger every
    month's totals sit in the thousands while the month-to-month *change* -- the
    thing worth seeing -- is only hundreds. A zero-based axis flattens every bar
    to nearly the same height and hides exactly that swing.

    When every value is positive the lower bound is held strictly above zero
    (never snapped back to a zero baseline), so the zoom that makes the trend
    legible is preserved even when one period sits far below the others.
    """

    def __init__(self, report, parent=None, *, title="Income and Spending by Month",
                 empty_text="No activity in this period"):
        fig = Figure(figsize=(6.4, 2.8), tight_layout=True)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        self._report = report
        self._title = title
        self._empty_text = empty_text
        self.render()

    def render(self) -> None:
        """(Re)draw the chart, re-reading the ACTIVE theme every time.

        The palette is consulted HERE, on each render, not once at construction:
        that is the fix for the reported defect where a live dark->light toggle
        left the home chart stuck dark. A fresh :meth:`render` clears the figure
        and recolours the figure, axes, bars and chrome from the current theme --
        and light mode restores matplotlib's white figure background explicitly,
        so switching back out of dark is a full undo (not a leftover dark patch).
        The deliberate non-zero value baseline is preserved across every redraw.
        """
        report = self._report
        fig = self.figure
        fig.clear()
        pal = _chart_palette()                       # dark palette, or None in light
        # Set both ways every render: dark blends with the (dark) home page; light
        # snaps back to white so a toggle out of dark fully clears the dark fill.
        fig.patch.set_facecolor(pal["window"] if pal else "white")
        ax = fig.add_subplot(111)
        if pal is not None:
            ax.set_facecolor(pal["surface"])
        if report.is_empty():
            ax.text(0.5, 0.5, self._empty_text,
                    ha="center", va="center", fontsize=11,
                    color=pal["muted"] if pal else "#8a94a0")
            ax.axis("off")
            self.draw_idle()
            return

        labels = [p.label for p in report.periods]
        spending = [p.cents / 100.0 for p in report.periods]        # dollars
        income = [p.income_cents / 100.0 for p in report.periods]   # dollars
        idx = list(range(len(labels)))
        w = 0.4
        ax.bar([i - w / 2 for i in idx], spending, width=w,
               color=_RED, label="Spending")
        ax.bar([i + w / 2 for i in idx], income, width=w,
               color=_GREEN, label="Income")

        values = spending + income
        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.08 if hi != lo else (abs(hi) * 0.08 or 1.0)
        ymin, ymax = lo - pad, hi + pad
        if lo > 0 and ymin <= 0:
            # A value far below the rest would drag the padded lower bound to or
            # past zero; hold it above zero instead so the min-max zoom -- the
            # whole point of this chart -- survives rather than resetting to a
            # zero baseline.
            ymin = lo * 0.5
        ax.set_ylim(ymin, ymax)

        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: _dollars(int(round(v * 100)))))
        step = max(1, len(labels) // 12)
        ticks = list(range(0, len(labels), step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([labels[i] for i in ticks], rotation=45,
                           ha="right", fontsize=8)
        ax.set_title(self._title, fontsize=10)
        ax.grid(True, axis="y", alpha=0.25)
        legend = ax.legend(loc="upper left", fontsize=8, framealpha=0.6)
        if pal is not None and legend is not None:      # keep legend text legible on dark
            legend.get_frame().set_facecolor(pal["surface"])
            legend.get_frame().set_edgecolor(pal["line"])
            for txt in legend.get_texts():
                txt.set_color(pal["text"])
        _theme_axes_chrome(ax, pal)       # dark: recolour ticks/spines/grid/title
        self.draw_idle()


class PriceHistoryCanvas(FigureCanvasQTAgg):
    """A line chart of ONE security's recorded closing prices over time.

    ``points`` is a list of ``(date, price)`` pairs -- ``price`` a Decimal in
    dollars per share -- exactly as :func:`mammon.investments.price_history`
    returns them. An empty list renders a placeholder instead of an empty chart
    (like the other canvases), so the caller can hand it a symbol with no
    recorded prices without a crash.

    ``currency`` is the native currency of the ACCOUNT the security or coin is
    held in (securities carry no currency of their own -- the account does, see
    :func:`mammon.fx.get_account_currency`). A price plotted for a CAD account
    is in CAD, and a chart that stamps a dollar sign on it invites the reader to
    add it to USD totals; so the axis is LABELLED with the currency, the title
    names it, and the tick formatter prints the ``$`` only for USD. It stays
    OPTIONAL and defaults to USD formatting so a caller with no account context
    (and the two-argument construction in the tests) keeps working."""

    def __init__(self, symbol, points, parent=None, currency=None):
        fig = Figure(figsize=(6.4, 4.8), tight_layout=True)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        ax = fig.add_subplot(111)
        ccy = (currency or "USD").strip().upper() or "USD"
        self.currency = ccy
        points = list(points or [])
        if not points:
            ax.text(0.5, 0.5, f"No recorded prices for {symbol}",
                    ha="center", va="center", fontsize=11, color="#8a94a0")
            ax.axis("off")
            return

        # Accepts (date, price) or (date, price, low, high): a price this app
        # DERIVED from a row's value and share count is known only to an
        # interval, because both inputs were rounded (investments.price_bounds).
        # Drawing that interval is the difference between a chart that looks
        # authoritative and one that tells the truth -- a fee-derived point can
        # span $134-$156 while a quote is exact, and the reader must be able to
        # see which is which.
        xs = [row[0] for row in points]
        ys = [float(row[1]) for row in points]
        bounds = [(row[2], row[3]) if len(row) >= 4 else (None, None)
                  for row in points]
        idx = list(range(len(xs)))
        ax.plot(idx, ys, color=_BLUE, linewidth=1.8, marker="o", markersize=3)
        ax.fill_between(idx, ys, min(ys), color=_BLUE, alpha=0.12)
        self._draw_uncertainty(ax, idx, ys, bounds)
        # Price is currency units per share (not cents): two decimals, and the
        # '$' ONLY when the holding account is in USD -- a CAD price wearing a
        # bare dollar sign reads as USD.
        if ccy == "USD":
            ax.yaxis.set_major_formatter(
                FuncFormatter(lambda v, _pos: f"${v:,.2f}"))
        else:
            ax.yaxis.set_major_formatter(
                FuncFormatter(lambda v, _pos: f"{v:,.2f} {ccy}"))
        ax.set_ylabel(f"Price ({ccy})", fontsize=9)

        step = max(1, len(xs) // 8)
        ticks = list(range(0, len(xs), step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([fmt_date(xs[i]) for i in ticks], rotation=45,
                           ha="right", fontsize=8)
        ax.set_title(f"Price History - {symbol} ({ccy})", fontsize=10)
        ax.grid(True, axis="y", alpha=0.25)

    @staticmethod
    def _draw_uncertainty(ax, idx, ys, bounds) -> None:
        """Error bars on the points whose price was derived, none on the rest.

        An open-ended interval (a share count too coarse to bound the price from
        above) is drawn as an arrow rather than a bar, because a bar with an
        invented top would claim a precision the row does not have. Points with
        no bounds -- every quote -- get nothing at all, so the presence of a bar
        IS the signal that a number was computed rather than read."""
        lows, highs, xs, centres = [], [], [], []
        open_x, open_y = [], []
        for i, (lo, hi) in enumerate(bounds):
            if lo is None:
                continue
            y = ys[i]
            if hi is None:
                open_x.append(i)
                open_y.append(y)
                continue
            xs.append(i)
            centres.append(y)
            lows.append(max(y - float(lo), 0.0))
            highs.append(max(float(hi) - y, 0.0))
        if xs:
            ax.errorbar(xs, centres, yerr=[lows, highs], fmt="none",
                        ecolor=_BLUE, elinewidth=1.0, capsize=3, alpha=0.75)
        for i, y in zip(open_x, open_y):
            ax.annotate("", xy=(i, y * 1.35), xytext=(i, y),
                        arrowprops=dict(arrowstyle="->", color=_BLUE,
                                        alpha=0.6, linewidth=1.0))


class AssetValueCanvas(FigureCanvasQTAgg):
    """A line chart of ONE asset account's recorded market values over time --
    :class:`PriceHistoryCanvas` for a thing you own exactly one of.

    ``points`` is a list of ``(date, value_cents)`` pairs, oldest first, as
    :func:`mammon.asset_values.value_history` yields. Values are whole-dollar
    money rather than dollars-per-share, so the axis uses the register's own
    ``_dollars`` formatter instead of the two-decimal price one -- a house
    reading ``$400,000.00`` is noise where ``$400,000`` is the number.

    ``basis`` (optional integer cents) draws the account's ledger balance as a
    flat reference line. That comparison is the entire reason the two numbers
    are stored apart: the gap between the line and the curve IS the unrealized
    appreciation, and seeing it is what makes a cost-basis register legible.
    An empty ``points`` renders a placeholder rather than an empty chart."""

    def __init__(self, name, points, basis=None, parent=None):
        fig = Figure(figsize=(6.4, 4.8), tight_layout=True)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        ax = fig.add_subplot(111)
        points = list(points or [])
        if not points:
            ax.text(0.5, 0.5, f"No recorded values for {name}",
                    ha="center", va="center", fontsize=11, color="#8a94a0")
            ax.axis("off")
            return

        xs = [d for d, _v in points]
        ys = [c / 100.0 for _d, c in points]
        idx = list(range(len(xs)))
        ax.plot(idx, ys, color=_BLUE, linewidth=1.8, marker="o", markersize=3,
                label="Market value")
        ax.fill_between(idx, ys, min(ys), color=_BLUE, alpha=0.12)
        pal = _chart_palette()
        legend = None
        if basis is not None:
            ax.axhline(basis / 100.0, color=_GREEN, linewidth=1.4,
                       linestyle="--", label="Cost basis")
            legend = ax.legend(loc="best", fontsize=8, framealpha=0.6)
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: _dollars(int(round(v * 100)))))

        step = max(1, len(xs) // 8)
        ticks = list(range(0, len(xs), step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([fmt_date(xs[i]) for i in ticks], rotation=45,
                           ha="right", fontsize=8)
        ax.set_title(f"Value History - {name}", fontsize=10)
        ax.grid(True, axis="y", alpha=0.25)
        if pal is not None and legend is not None:
            legend.get_frame().set_facecolor(pal["surface"])
            legend.get_frame().set_edgecolor(pal["line"])
            for txt in legend.get_texts():
                txt.set_color(pal["text"])
        _theme_axes_chrome(ax, pal)       # dark: recolour ticks/spines/grid/title


class LoanProjectionCanvas(FigureCanvasQTAgg):
    """A line/area chart of a loan's PROJECTED outstanding principal declining
    into the future, taken from its amortization schedule.

    ``schedule`` is the list of ``ScheduleRow`` returned by
    :func:`mammon.loans.amortization_schedule`; each row carries a payment
    ``date`` and the remaining ``balance`` (integer cents) AFTER that payment, so
    plotting balance (y) against date (x) steps the series down toward payoff. An
    empty schedule renders a placeholder instead of an empty chart (like the
    other canvases), so a loan with no parameters won't crash the caller."""

    def __init__(self, schedule, parent=None):
        fig = Figure(figsize=(6.4, 4.8), tight_layout=True)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        ax = fig.add_subplot(111)
        rows = list(schedule or [])
        if not rows:
            ax.text(0.5, 0.5, "No schedule to project",
                    ha="center", va="center", fontsize=11, color="#8a94a0")
            ax.axis("off")
            return

        xs = [r.date for r in rows]
        ys = [r.balance / 100.0 for r in rows]
        idx = list(range(len(xs)))
        ax.plot(idx, ys, color=_BLUE, linewidth=1.8)
        ax.fill_between(idx, ys, 0.0, color=_BLUE, alpha=0.12)
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: _dollars(int(round(v * 100)))))

        step = max(1, len(xs) // 8)
        ticks = list(range(0, len(xs), step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([fmt_date(xs[i]) for i in ticks], rotation=45,
                           ha="right", fontsize=8)
        ax.set_title("Projected Principal Balance", fontsize=10)
        ax.grid(True, axis="y", alpha=0.25)


class ProjectedBalanceCanvas(FigureCanvasQTAgg):
    """Quicken's Projected Balances chart: the end-of-day balance across the
    chosen accounts stepping through the horizon (:func:`mammon.projection
    .project`), a zero line so a dip below it is unmissable, and the lowest
    point marked -- the number a person opens the projection to find."""

    def __init__(self, proj, parent=None):
        fig = Figure(figsize=(6.4, 3.4), tight_layout=True)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        ax = fig.add_subplot(111)
        days = list(proj.days) if proj is not None else []
        if not days:
            ax.text(0.5, 0.5, "Nothing to project",
                    ha="center", va="center", fontsize=11, color="#8a94a0")
            ax.axis("off")
            return
        xs = [d.date for d in days]
        ys = [d.balance / 100.0 for d in days]
        idx = list(range(len(xs)))
        ax.step(idx, ys, where="post", color=_BLUE, linewidth=1.8)
        ax.fill_between(idx, ys, 0.0, step="post", color=_BLUE, alpha=0.12)
        ax.axhline(0.0, color="#b2382c", linewidth=1.0, alpha=0.7)
        low_i = min(idx, key=lambda i: ys[i])
        ax.plot([low_i], [ys[low_i]], "o", color="#b2382c", markersize=5)
        ax.annotate(_dollars(int(round(ys[low_i] * 100))), (low_i, ys[low_i]),
                    textcoords="offset points", xytext=(6, -12), fontsize=8,
                    color="#b2382c")
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: _dollars(int(round(v * 100)))))
        step = max(1, len(xs) // 8)
        ticks = list(range(0, len(xs), step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([fmt_date(xs[i]) for i in ticks], rotation=45,
                           ha="right", fontsize=8)
        ax.set_title("Projected Balance", fontsize=10)
        ax.grid(True, axis="y", alpha=0.25)


class ChartDialog(QDialog):
    """A simple modal frame that embeds a chart canvas + a Close button.

    ``on_edit`` adds an "Edit Price History…" button beside Close. A price chart
    is where a user NOTICES a wrong or missing close -- a spike, a flat run, a
    series that stops -- so it is where the way to fix one belongs; every price
    chart passes it, and nothing else does."""

    def __init__(self, title, canvas, parent=None, on_edit=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.canvas = canvas
        lay = QVBoxLayout(self)
        lay.addWidget(canvas)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        self.edit_button = None
        if on_edit is not None:
            self.edit_button = buttons.addButton(
                "Edit Price History…", QDialogButtonBox.ActionRole)
            self.edit_button.setAutoDefault(False)
            self.edit_button.clicked.connect(lambda _c=False: on_edit())
        lay.addWidget(buttons)
        self.resize(720, 560)
