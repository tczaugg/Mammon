"""Per-budget-item bars: one horizontal envelope gauge each, in a flowing grid.

This is what the Financial Calendar shows under the month WHILE BUDGET MODE IS
ON, in the band the income-and-spending chart occupies the rest of the time. The
trend chart answers "how has spending moved over a year"; in budget mode the
question on screen is "which envelope is in trouble THIS month", and a
twelve-month trend cannot answer it -- so the band changes with the mode instead
of showing a chart nobody is reading.

The bar measures WHAT IS LEFT, not what was spent: an untouched envelope is a
full green bar and spending drains it. That direction is deliberate. A
spent-so-far bar fills up as the month goes wrong, so a nearly-full bar means
trouble in one reading and comfort in the other; a remaining bar always means
the same thing -- more green is more room.

An overspent envelope cannot be drawn inside the bar, because there is no room
left inside it by definition. So the bar reads empty and the overrun is painted
in RED TO THE LEFT of its left edge, growing leftward into a reserved gutter, at
THE SAME cents-per-pixel as the bar itself -- that shared scale is the whole
reason the red is comparable to the green beside it ("twice as far over as the
neighboring envelope holds" is readable at a glance). The run is capped at the
gutter so one catastrophic overrun cannot push the drawing off the widget, and
the figure lives in the text line above the bar rather than inside the run, so
clipping the picture never costs the number.

No QProgressBar: it cannot paint outside its own groove, which is precisely what
the overrun needs, and it takes its colors from the platform style rather than
the app's theme. Painted, like :mod:`mammon.ui.asset_allocation`'s bars.

Money never arrives here as anything but integer cents from
:func:`mammon.budgets.month_category_status`; this module does arithmetic on
PIXELS only and formats through :func:`mammon.ui.models.fmt_cents`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (
    QFrame, QGridLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from mammon.ui import style
from mammon.ui.models import fmt_cents

#: The bar's own height in pixels, matching the allocation bars' 16px feel at
#: the smaller type these labels use.
BAR_HEIGHT = 14
#: Pixels reserved at the LEFT of every item for the red overrun run. Reserved
#: on every item, spent or not, so that every bar in the grid starts at the same
#: x and the greens can be read against each other down a column.
OVER_GUTTER = 44
#: The red run never comes closer than this to the widget's left edge, so a
#: clipped overrun still reads as "clipped" rather than as the whole row.
OVER_PAD = 4
#: Breathing room to the right of the bar, so a full bar does not touch its
#: neighboring column.
RIGHT_PAD = 10
#: Gap between the label line and the bar.
LABEL_GAP = 2

#: A column narrower than this cannot hold a category name plus an amount, so
#: the grid drops to fewer columns rather than eliding everything.
MIN_COLUMN_WIDTH = 200
#: Three to four columns fit the chart's band; more would make each bar too
#: short for its own scale to mean anything.
MAX_COLUMNS = 4
COLUMN_GAP = 14
ROW_GAP = 6
#: The band's height: the same 180px the spending chart is given, so switching
#: modes does not resize the calendar above it.
GRID_HEIGHT = 180

#: The remaining fill: (light, dark). The calendar's deposit green and the
#: spending chart's income green are the same color -- green already means
#: "money you have" on this page, and the bar means exactly that.
_REMAINING = ("#2e6b4e", "#7dbb98")


def remaining_color() -> str:
    """The green a bar's remaining room is filled with, for the ACTIVE theme."""
    return _REMAINING[1 if style.theme() == "dark" else 0]


def over_color() -> str:
    """The red an overrun is painted in: the app's negative-amount color, so an
    over-budget envelope is the same red as an overdrawn balance."""
    return style.negative_color()


def track_color() -> str:
    """The empty part of the bar: the header shade, a quiet trough in both
    themes rather than an invented gray."""
    return style.header_bg_color()


@dataclass(frozen=True)
class BarGeometry:
    """Where one item's ink goes, in pixels, for a given widget width.

    Separated from :meth:`BudgetItemBar.paintEvent` so the arithmetic that
    decides the picture can be asserted without rendering one: a test reads the
    numbers the painter would use.

    ``cents_per_pixel`` is the ONE scale in play -- the bar's own -- and the red
    run is measured with it, which is what makes the two runs comparable.
    """
    bar_x: float
    bar_width: float
    fill_width: float
    over_x: float
    over_width: float
    cents_per_pixel: float
    over_clipped: bool = False

    @property
    def fill_end(self) -> float:
        return self.bar_x + self.fill_width


class BudgetItemBar(QWidget):
    """One budget item: its name and figure, and a bar of what is LEFT.

    Green fills from the bar's left edge for the remaining cents; when the
    envelope is overspent the bar is empty and a red run sits outside its left
    edge (see the module docstring for why outside).
    """

    def __init__(self, status, parent=None):
        super().__init__(parent)
        self.status = status
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        metrics = QFontMetrics(self.font())
        self._text_height = metrics.height()
        self.setMinimumHeight(self._text_height + LABEL_GAP + BAR_HEIGHT + 2)
        self.setMinimumWidth(OVER_GUTTER + 60)
        self.setToolTip(self.describe())

    # -- what it says ---------------------------------------------------------
    def amount_text(self) -> str:
        """The figure beside the name: what is left, or how far past the limit.

        Two words rather than a signed number, because "-30.00" beside a
        category could be read as "spent 30" by anybody who has not just read
        the legend.
        """
        s = self.status
        if s.over_cents:
            return f"{fmt_cents(s.over_cents)} over"
        return f"{fmt_cents(s.remaining_cents)} left"

    def describe(self) -> str:
        """The hover text: every figure behind the picture."""
        s = self.status
        parts = [f"{s.category_name}",
                 f"Allowance {fmt_cents(s.allowance_cents)}",
                 f"Spent {fmt_cents(s.spent_cents)}"]
        if s.committed_cents:
            parts.append(f"Scheduled {fmt_cents(s.committed_cents)}")
        parts.append(f"{fmt_cents(s.over_cents)} over its limit" if s.over_cents
                     else f"{fmt_cents(s.remaining_cents)} left")
        return "\n".join(parts)

    # -- where the ink goes ---------------------------------------------------
    def bar_geometry(self, width: Optional[int] = None) -> BarGeometry:
        """This item's pixel layout at ``width`` (the widget's own by default)."""
        w = self.width() if width is None else int(width)
        bar_x = float(OVER_GUTTER)
        bar_width = max(0.0, float(w) - OVER_GUTTER - RIGHT_PAD)
        s = self.status
        if bar_width <= 0 or s.allowance_cents <= 0:
            # A zero or negative allowance has no scale to draw against, so the
            # bar itself stays empty rather than inventing one. Money charged to
            # such an item -- a budget item the plan gives nothing THIS month,
            # which the domain still keeps on screen all year -- is nonetheless an
            # overrun, so the red run takes the whole gutter and reports itself
            # CLIPPED: past any scale this bar can measure. The figure is in the
            # label line above, so the picture never owes the number.
            cap = max(0.0, OVER_GUTTER - OVER_PAD)
            run = cap if (bar_width > 0 and s.over_cents > 0) else 0.0
            return BarGeometry(bar_x=bar_x, bar_width=bar_width, fill_width=0.0,
                               over_x=bar_x - run, over_width=run,
                               cents_per_pixel=0.0, over_clipped=run > 0)
        cents_per_pixel = s.allowance_cents / bar_width
        fill = min(bar_width, max(0, s.remaining_cents) / cents_per_pixel)
        run = s.over_cents / cents_per_pixel if s.over_cents else 0.0
        cap = max(0.0, OVER_GUTTER - OVER_PAD)
        clipped = run > cap
        run = min(run, cap)
        return BarGeometry(bar_x=bar_x, bar_width=bar_width, fill_width=fill,
                           over_x=bar_x - run, over_width=run,
                           cents_per_pixel=cents_per_pixel, over_clipped=clipped)

    def paintEvent(self, event):        # noqa: N802 (Qt's name)
        g = self.bar_geometry()
        if g.bar_width <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        metrics = QFontMetrics(self.font())
        # The label line, above the bar: name left, figure right. The figure
        # never sits inside the red run, so capping the run cannot cost the
        # number.
        amount = self.amount_text()
        amount_w = metrics.horizontalAdvance(amount)
        baseline = metrics.ascent()
        p.setPen(QPen(QColor(over_color() if self.status.over_cents
                             else style.text_color())))
        p.drawText(int(self.width() - amount_w), baseline, amount)
        name_w = max(0, self.width() - amount_w - 8)
        p.setPen(QPen(QColor(style.text_color())))
        p.drawText(0, baseline,
                   metrics.elidedText(self.status.category_name, Qt.ElideRight, name_w))

        y = float(metrics.height() + LABEL_GAP)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(track_color())))
        p.drawRect(QRectF(g.bar_x, y, g.bar_width, BAR_HEIGHT))
        if g.fill_width > 0:
            p.setBrush(QBrush(QColor(remaining_color())))
            p.drawRect(QRectF(g.bar_x, y, g.fill_width, BAR_HEIGHT))
        if g.over_width > 0:
            p.setBrush(QBrush(QColor(over_color())))
            p.drawRect(QRectF(g.over_x, y, g.over_width, BAR_HEIGHT))
        p.setPen(QPen(QColor(style.line_color()), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(g.bar_x, y, g.bar_width, BAR_HEIGHT))
        p.end()


class BudgetBarGrid(QScrollArea):
    """Every budget item as a bar, flowed into 3-4 columns of one fixed band.

    ORDER IS THE DOMAIN'S (:func:`mammon.budgets._order_category_status`): by
    category display path, the same order the Budget Planner's Set tab and the
    category pickers use. This grid NEVER re-sorts what it was handed. Laid out
    ROW-MAJOR, left to right, because that is reading order; only the COLUMN
    COUNT follows the width, so the sequence of bars is the same in a narrow
    panel as a wide one.

    THE SET IS THE DOMAIN'S TOO, and it is the BUDGET's items rather than the
    month's: every item of the budget arrives in every month of it
    (:func:`mammon.budgets.budget_item_category_ids`), so a category never
    disappears as the user pages months -- the reported defect. A month the user
    wrote no line for arrives with a zero allowance and its real spending, which
    draws as an empty bar with the overrun outside it. This grid adds and removes
    nothing: it draws exactly the items it was handed.

    The order is deliberately month-INDEPENDENT. An earlier worst-first order
    (ascending by what is left) put the trouble in the top-left corner, but it
    moved every bar whenever the month's spending changed, so paging month to
    month made it impossible to follow ONE envelope -- the reported defect. The
    overrun is already unmistakable in red, so the loss is nothing.

    The column count follows the available width rather than the item count, so
    a bar is the same length whether the plan has four lines or forty and the
    greens can be compared across the grid. The band is fixed at the height the
    spending chart occupies and scrolls vertically when the plan outgrows it.
    """

    def __init__(self, items: Iterable, parent=None, *,
                 columns: Optional[int] = None,
                 empty_text: str = "No budget items for this month"):
        super().__init__(parent)
        self._items = tuple(items)
        self._fixed_columns = int(columns) if columns else None
        self._empty_text = empty_text
        self._columns = 0
        self._bars: list = []
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        # No horizontal scrolling: the columns are chosen to fit the width, so a
        # horizontal bar would only ever mean the layout got it wrong.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumHeight(GRID_HEIGHT)
        self.setMaximumHeight(GRID_HEIGHT)

        self._inner = QWidget()
        if not self._items:
            box = QVBoxLayout(self._inner)
            empty = QLabel(self._empty_text)
            empty.setObjectName("registerSub")
            empty.setAlignment(Qt.AlignCenter)
            box.addWidget(empty)
            self._layout = None
            self.setWidget(self._inner)
            return
        self._layout = QGridLayout(self._inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(COLUMN_GAP)
        self._layout.setVerticalSpacing(ROW_GAP)
        self._bars = [BudgetItemBar(s, self._inner) for s in self._items]
        self.setWidget(self._inner)
        self._relayout(self.column_count())

    # -- accessors the panel and the tests read -------------------------------
    def statuses(self) -> tuple:
        return self._items

    def bars(self) -> list:
        """The bar widgets, in the order they are laid out."""
        return list(self._bars)

    def column_count(self, width: Optional[int] = None) -> int:
        """How many columns fit ``width`` (the viewport's by default): as many
        MIN_COLUMN_WIDTH columns as fit, never more than MAX_COLUMNS."""
        if self._fixed_columns:
            return self._fixed_columns
        w = self.viewport().width() if width is None else int(width)
        fits = (int(w) + COLUMN_GAP) // (MIN_COLUMN_WIDTH + COLUMN_GAP)
        return int(max(1, min(MAX_COLUMNS, fits)))

    def columns(self) -> int:
        """The column count currently laid out."""
        return self._columns

    # -- layout ---------------------------------------------------------------
    def _relayout(self, columns: int) -> None:
        if self._layout is None:
            return
        columns = max(1, int(columns))
        while self._layout.count():
            self._layout.takeAt(0)
        for col in range(MAX_COLUMNS + 1):
            self._layout.setColumnStretch(col, 1 if col < columns else 0)
        for i, bar in enumerate(self._bars):
            self._layout.addWidget(bar, i // columns, i % columns)
        rows = (len(self._bars) + columns - 1) // columns
        # Soak up leftover height at the bottom: bars keep their own height
        # instead of being stretched apart when the plan is short.
        self._layout.setRowStretch(rows, 1)
        self._columns = columns

    def resizeEvent(self, event):       # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        wanted = self.column_count()
        if wanted != self._columns:
            self._relayout(wanted)
