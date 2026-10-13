"""The Retirement Planner page: what the plan pays out, year by year (SRD 5.8o).

This is the central page the View menu switches to, beside the Financial
Calendar and the Investment Dashboard. It answers ONE question: given the people
on file, their earnings records and the draws and conversions already planned,
what does each future year's income look like and what happens to the fund it
comes out of?

Why a stacked bar and not a table
--------------------------------
The three sources of retirement income behave differently under tax and under
the RMD rules, and the user's whole reason for planning is to see their relative
weight change over time: Social Security is fixed once claimed, a tax-deferred
draw is ordinary income with a floor under it, a Roth draw is neither. A stacked
bar shows the mix AND the total in one read; three separate lines would show
neither. The bars are the plan's SPENDABLE income, which is why a Roth
conversion - taxable but not spendable, per ``retirement.AccountFlow`` - is not
one of the stacked series but belongs to the Roth section below.

Why the Roth work happens on the page and not in a dialog
---------------------------------------------------------
Planning a conversion is a question about ONE number's relation to another: how
much room is left under a bracket edge this year. That is a question a picture
answers and a form does not, and the picture and the edit have to be the same
gesture - a dialog that had to be opened, read, filled in and closed put the
bracket edge and the amount on two different screens. So the Roth section draws
each year's projected taxable income against the bracket edges for the household's
filing status, a LEFT-click on an edge inside a bar sets that year's conversion
to exactly the gap up to it, and a RIGHT-click opens the schedule INLINE, focused
on the year clicked. Nothing here is modal: an ``exec_()`` under the offscreen
platform never returns (CLAUDE.md's headless-modal hazard), and a modal over a
chart hides the thing being planned against.

Taxable income is STORED, not derived: Mammon cannot see wages, a pension or a
capital gain, so the series is seeded from the plan's own flows and is then the
user's to edit (``retirement.set_taxable_income``). What is stored is the BASE -
the year without its conversion - because the conversion is drawn stacked on top
of it, and a stored total would double count the moment a click filled a gap.

There is no spousal Roth conversion: IRC 408A(d)(3) rolls a distribution into a
Roth of the SAME individual, so a row's target picker offers only Roth accounts
owned by the same person as its source account.

Why the fund line is on a second axis
-------------------------------------
The pool a draw comes out of is one to two orders of magnitude larger than the
draw itself, so plotting both against a single y axis flattens the bars to
nothing. The line therefore gets a ``twinx()`` right-hand axis and is pushed
BEHIND the bars (matplotlib draws a twin axes after its parent, so the parent's
z-order is raised and its patch hidden - otherwise the line lies on top of the
bars and reads as a fourth series). It is the median band of the same
``mammon/forecast.py`` projection the Investment Dashboard draws, restricted to
the retirement accounts and fed the plan's own net flows, so the two screens can
never tell different stories about the same money.

Why longevity is a case and not a number
----------------------------------------
There is no honest single answer to "how long will I live", and a planner that
prints one invites the user to treat it as fact. So the horizon is a selectable
terminal-age CASE - 70, 80, 90, 100 - and the page says so. Mammon never
counsels: every caption here states a mechanism, and nothing on this page names
an amount to withdraw, a year to convert in, or an age to claim at.

Where the rules went
--------------------
The captions used to carry the provenance of every published table behind a
figure, with an amber triangle when one of them was an edition behind. That was
unreadable at the point of use and it misused the triangle, which means MISSING
OR CONFLICTING DATA. The rules, the pros and cons, and ONE provenance table now
live in ``ui/retirement_faq.py``, reached from the FAQ button in the header.
What stays inline is an assumption the user can CORRECT - that a balance is
carried flat, what a year's withdrawal is - because that is an input, not a
citation. Whether an account is a CURRENT EMPLOYER's plan is not set here at
all: it is a property of the account, so it is a checkbox in Account Details
(SRD 5.8o) and this page only reads it. The column header says so in words -
"(current employer plan)" - and nothing on this page is colored to mean it.

No raw SQL lives here, and no projection arithmetic either. Accounts come from
``mammon.ledger`` and ``mammon.portfolio``, the plan from ``mammon.retirement``,
and everything the page computes from ``mammon.retirement_plan`` - the engine
the editors here are widgets over, which the MCP surface projects through too,
so the page and a connected model cannot disagree. Every amount
crossing this module is integer cents until the moment it is handed to
matplotlib (which cannot take cents) or formatted for display.
"""

from __future__ import annotations

import datetime as _dt
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Mapping, Optional, Sequence

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator

from mammon import ledger, retirement
from mammon.retirement import PlanAccount
from mammon.retirement_plan import (
    stale_withdrawal_years,
    PlanTaxes,
    DEFAULT_TERMINAL_AGE,
    IncomeYear,
    MAX_TERMINAL_AGE,
    MIN_TERMINAL_AGE,
    PLAN_WRITTEN_THROUGH_AGE,
    aca_cliff_in,
    aca_cost,
    aca_credit_in,
    aca_marketplace_months,
    account_value_cents,
    appealed_premium_years,
    basis_ratios,
    default_filing_status,
    horizon_people,
    household_irmaa,
    income_rows,
    inflows_measured_once,
    living_couple,
    medicare_months_in,
    other_taxable_cents,
    plan_accounts,
    plan_horizon,
    planning_people,
    retirement_start_year,
    year_status,
    HouseholdEngine,
    PlanEngine,
)
from mammon.ui import charts, style
from mammon.ui.budget_basis import BudgetBasisDialog
from mammon.ui.models import fmt_cents, fmt_date, fmt_money, parse_amount
from mammon.ui.retirement_faq import RetirementFaqWindow
from mammon.ui.social_security_dialog import SocialSecurityDialog
from mammon.ui.delegates import NoWheelComboBox, NoWheelSpinBox


# A size up from 9/9 (reported: the tick and axis labels were too small).
TICK_FONT_SIZE = 10
AXIS_FONT_SIZE = 11
LEGEND_FONT_SIZE = 10      # up from 8 (reported: legends too small)
#: The "top of 22%" labels in the Roth chart's right margin keep the smaller
#: size: a bigger one would not fit the margin both charts share.
BRACKET_LABEL_FONT_SIZE = 8
EMPTY_FONT_SIZE = 10

# Room on BOTH sides: the income axis is labeled in dollars on the left and the
# fund axis in dollars on the right, and a clipped "$1,200,000" is worse than a
# narrower plot. The right margin is the wider of the two because it carries
# THREE things - the twin axis's dollar ticks, its rotated "Retirement funds"
# label, and (on the Roth chart) the bracket percentages, which live outside the
# plot so a bar can never cover them. Derived by measuring the rendered figure at
# its 504px default width: at the 9pt tick size "$300,000" measures 43px and
# "$4,000,000" 52px, the tick mark and its pad another 4, a rotated axis label
# ~12. Annual income is the smaller number, so the left needs ~61px (0.121); the
# fund totals on the right need ~78px (0.155). Both charts share the pair, so
# their plot areas stay
# aligned one above the other - which is why neither uses tight_layout.
PLOT_AXES_LEFT = 0.121
PLOT_AXES_RIGHT = 0.845

#: The width of one year's bar, in years. Shared by both charts so a click's
#: x coordinate can be turned back into the year whose bar was hit.
BAR_WIDTH = 0.72

#: How many bracket edges above the tallest bar are still drawn - and kept in
#: view at the left, where they are lowest. Three, so the room above the plan
#: is visible without the 37% edge flattening every bar (two hid the 24% and
#: 32% lines once the conversions were cleared - reported).
BRACKET_LINES_ABOVE = 3

#: How close to a bracket line a click counts as ON it, as a fraction of the
#: y axis. Generous on purpose: the target is a one-pixel line.
LINE_HIT_FRACTION = 0.04

SERIES_SOCIAL_SECURITY = "Social Security"
SERIES_DEFERRED = "IRA / 401(k) draws"
SERIES_ROTH = "Roth draws"
SERIES_FUND = "Invested funds"      # retirement AND taxable accounts
SERIES_CONVERSION = "Planned Roth conversions"
SERIES_TAXABLE = "Projected taxable income"

#: The stacked series, bottom to top. Social Security sits at the bottom
#: because it is the one course the plan cannot change year by year.
SERIES_OTHER = "Other income"
SERIES_TAXABLE_DRAW = "Taxable account draws"
SERIES_TAX_DRAW = "Income tax paid from IRA"
SERIES_GAINS = "Capital gains (their own rates)"
SERIES_RMD = "Required minimum (RMD)"
RMD_COLOR = "#d62728"
#: The year the chart's "above the tallest bar" test is judged in.
TABLE_YEAR_FOR_CHART = retirement.TAX_TABLE_YEAR
INCOME_SERIES = (SERIES_SOCIAL_SECURITY, SERIES_OTHER, SERIES_DEFERRED,
                 SERIES_TAXABLE_DRAW, SERIES_ROTH)

WITHDRAWAL_BUTTON_TIP = ("The spending plan and the year-by-year withdrawals. "
                         "Clicking an income bar opens it on that year.")

INCOME_EMPTY_TEXT = ("No projected retirement income yet - record a person's "
                     "earnings, or plan a withdrawal.")
ROTH_EMPTY_TEXT = ("No projected taxable income to convert against yet - record "
                   "a person's earnings, or enter a year's taxable income in the "
                   "schedule.")

# Two of the five series have no key in ui/style.py, which carries only the
# colors the CHROME needs (blue, amber, red). Defining them here, per theme and
# with their contrast measured against that theme's page, is deliberate: a
# series color invented at the call site is how a chart ends up unreadable in
# one palette, and adding app-wide palette keys for two chart series would
# imply the rest of the app may use them.
_GREEN = {"light": "#1c6b45", "dark": "#7fd6a2"}    # 6.3:1 light, 9.4:1 dark
_VIOLET = {"light": "#6a4bab", "dark": "#c2a8ff"}   # 6.1:1 light, 8.9:1 dark
_TEAL = {"light": "#17607a", "dark": "#79c6de"}     # 6.0:1 light, 9.1:1 dark
_BROWN = {"light": "#8a5a2b", "dark": "#e0b07a"}    # other income
_SLATE = {"light": "#4a5a70", "dark": "#a9b8cf"}    # taxable account draws
_RUST = {"light": "#a8472a", "dark": "#f09a7c"}     # income tax paid from IRA
_IRMAA = {"light": "#9c1f73", "dark": "#e58ac6"}    # IRMAA tier lines
_OLIVE = {"light": "#5b7f3a", "dark": "#a9d18e"}    # realized capital gains
_ACA = {"light": "#006d77", "dark": "#5fd3d9"}      # the ACA cliff line
#: Bracket lines: their own color, not the grid's (reported: the two read as
#: one, and the lines were too dim in dark mode). Amber, bright in dark mode.
_BRACKET = {"light": "#8f4f00", "dark": "#f2b441"}


def series_colors(for_print: bool = False) -> dict:
    """The color for each series, plus the chrome colors a caption needs.

    The theme NAME is resolved once and the whole palette taken from it, rather
    than mixing ``style.theme()`` with a per-color lookup: a print render forces
    the light palette, and asking two different questions about the theme is how
    a light-palette figure ends up with dark-palette series in it.
    """
    theme = "light" if for_print else style.theme()
    if theme not in ("light", "dark"):
        theme = "light"
    pal = style.palette_for(theme)
    return {
        SERIES_SOCIAL_SECURITY: pal["blue"],
        SERIES_OTHER: _BROWN[theme],
        SERIES_TAXABLE_DRAW: _SLATE[theme],
        SERIES_DEFERRED: pal["highlight"],
        SERIES_ROTH: _GREEN[theme],
        SERIES_CONVERSION: _VIOLET[theme],
        SERIES_TAXABLE: _TEAL[theme],
        SERIES_TAX_DRAW: _RUST[theme],
        SERIES_GAINS: _OLIVE[theme],
        SERIES_FUND: pal["muted"],
        "text": pal["text"],
        "line": pal["line"],
        "bracket": _BRACKET[theme],
        "irmaa": _IRMAA[theme],
        "aca": _ACA[theme],
    }




def plan_mix_caption(mix) -> str:
    """(stocks, bonds, cash) -> ``60% stocks / 35% bonds / 5% cash``."""
    return " / ".join(f"{Decimal(str(v)).normalize():f}% {name}"
                      for v, name in zip(mix, ("stocks", "bonds", "cash")))


class PlanMixDialog(QDialog):
    """Three percentages - stocks, bonds, cash - or the mix held today."""

    def __init__(self, mix, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Retirement Plan Mix")
        from PyQt5.QtWidgets import QDialogButtonBox
        layout = QVBoxLayout(self)
        note = QLabel("The asset mix the plan projects every account at. Stocks "
                      "are split 60/40 U.S./international, as the Investment "
                      "Center's thermometer does.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.held = QCheckBox("Use the mix each account holds today", self)
        layout.addWidget(self.held)
        form = QFormLayout()
        self.fields = []
        for name, value in zip(("Stocks %", "Bonds %", "Cash %"),
                               mix or ("60", "35", "5")):
            edit = QLineEdit(str(value), self)
            edit.setMaximumWidth(60)
            form.addRow(name, edit)
            self.fields.append(edit)
        layout.addLayout(form)
        self.held.toggled.connect(lambda on: [f.setEnabled(not on) for f in self.fields])
        self.held.setChecked(mix is None)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        """(stocks, bonds, cash) as typed, or None for the mix held today."""
        if self.held.isChecked():
            return None
        out = []
        for edit in self.fields:
            try:
                out.append(Decimal(edit.text().strip().rstrip("%") or "0"))
            except InvalidOperation:
                out.append(Decimal(-1))       # set_plan_mix refuses it, and says so
        return tuple(out)




def defer(owner, fn) -> None:
    """Run ``fn`` on the next event-loop tick, but only while ``owner`` lives.

    ``QTimer.singleShot(0, fn)`` keeps a pending call even after the widget it
    touches is deleted, and firing into a deleted widget is a native crash with
    no traceback. A timer that is the owner's CHILD dies with it. Found when a
    chain of deferred re-applies outlived its page and crashed the next test.
    """
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(fn)
    timer.timeout.connect(timer.deleteLater)
    timer.start(0)




# ---------------------------------------------------------------------------
# the two charts
# ---------------------------------------------------------------------------
class _PlanCanvas(FigureCanvasQTAgg):
    """Shared chrome for this page's charts.

    Its own canvas rather than a ``ui/charts.py`` class for the same reason the
    dashboard's hole charts are: no title, no frame, a transparent background
    over a themed page. Every COLOR rule is imported from ``charts.py`` so this
    family cannot drift from the app's other plots, and the font sizes match the
    dashboard's so the two pages read alike.
    """

    MIN_HEIGHT = 120

    def __init__(self, parent=None, *, height: float = 3.0):
        fig = Figure(figsize=(7.0, height), dpi=72)
        fig.patch.set_alpha(0.0)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(self.MIN_HEIGHT)
        #: The axes a click is measured against, and the years actually drawn on
        #: it. Both stay empty in the empty state, which is what makes a click on
        #: a chart with nothing on it do nothing.
        self.axes = None
        self._years_drawn: tuple[int, ...] = ()
        # Details on hover instead of more series: the chart was getting busy
        # (reported), so everything about a year - its sources, draws,
        # conversion and taxable income - is in a tooltip, not another bar.
        self.hover_text = None
        self.mpl_connect("motion_notify_event", self._on_hover)

    def _on_hover(self, event) -> None:
        from PyQt5.QtGui import QCursor
        from PyQt5.QtWidgets import QToolTip
        year = (self.year_at(getattr(event, "xdata", None))
                if getattr(event, "inaxes", None) is not None else None)
        if year is None or self.hover_text is None:
            QToolTip.hideText()
            return
        QToolTip.showText(QCursor.pos(), self.hover_text(int(year)), self)

    def year_at(self, xdata) -> Optional[int]:
        """The year whose bar contains this x coordinate, or None beside a bar.

        Bars are drawn AT the year, so the year is the rounded coordinate - but
        only within half a bar width of it, because the gap between two bars
        belongs to neither. Shared by both charts so the two cannot disagree
        about which bar a click landed on.
        """
        if xdata is None or not self._years_drawn:
            return None
        try:
            x = float(xdata)
        except (TypeError, ValueError):
            return None
        year = int(round(x))
        if abs(x - year) > BAR_WIDTH / 2.0:
            return None
        return year if year in self._years_drawn else None

    def _new_axes(self, *, twin: bool = False):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.patch.set_alpha(0.0)
        hide = ("top",) if twin else ("top", "right")
        for side in hide:
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=TICK_FONT_SIZE, length=2, pad=2)
        # The top margin holds the legend, which sits ABOVE the plot rather
        # than on it (reported: over the bars it could not be read).
        # A small bottom margin: the tick labels need it, the caption under the
        # canvas does not (reported: too much space between plot and caption).
        self.figure.subplots_adjust(left=PLOT_AXES_LEFT, right=PLOT_AXES_RIGHT,
                                   top=0.86, bottom=0.11)
        return ax

    def _grid(self, ax) -> None:
        charts._theme_grid(ax, charts._chart_palette(), axis="y")

    def _theme(self, ax) -> None:
        """Color the chrome from the palette active RIGHT NOW - at the end of
        every render, never at construction, because the page is rebuilt on a
        theme change and a captured color would keep matplotlib's near-black
        defaults on the dark palette."""
        pal = charts._chart_palette()
        charts._theme_axes_chrome(ax, pal)
        if pal is None:                     # light mode: matplotlib's defaults
            return
        for label in (ax.xaxis.label, ax.yaxis.label):
            label.set_color(pal["text"])
        for gridline in ax.get_xgridlines():
            gridline.set_color(pal["line"])

    def _legend(self, ax, handles, labels, *, above: float = 0.0) -> None:
        """Name the series ON the plot, frameless: the figure is transparent
        over a themed page, so a legend box would be a white rectangle in dark
        mode. ``above`` raises it by that fraction of the plot's height, over
        the labels of lines that leave through the top of the Roth chart."""
        if not handles:
            return
        # Above the axes, in rows of four: on the plot it sat over the bars and
        # could not be read (reported).
        leg = ax.legend(handles, labels, loc="lower left",
                        bbox_to_anchor=(0.0, 1.01 + above),
                        fontsize=LEGEND_FONT_SIZE, frameon=False,
                        ncol=min(4, len(handles)), handlelength=1.3,
                        handletextpad=0.5, columnspacing=1.2, labelspacing=0.2,
                        borderpad=0.0, borderaxespad=0.0)
        leg.set_zorder(8)
        pal = charts._chart_palette()
        if pal is not None:
            for text in leg.get_texts():
                text.set_color(pal["text"])

    def _empty(self, text: str) -> None:
        ax = self._new_axes()
        ax.axis("off")
        label = ax.text(0.5, 0.5, text, ha="center", va="center",
                        fontsize=EMPTY_FONT_SIZE, transform=ax.transAxes)
        pal = charts._chart_palette()
        if pal is not None:
            label.set_color(pal["muted"])
        self._theme(ax)
        self.draw_idle()

    @staticmethod
    def _dollars(ax) -> None:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"${v:,.0f}"))

    @staticmethod
    def _years(ax) -> None:
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=12))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{int(v)}"))


class IncomeChart(_PlanCanvas):
    """The main display: stacked income bars with the fund-value line behind.

    ``bars`` maps a series label to its matplotlib container and ``fund_line``
    holds the Line2D, so a test (and a future export) can read what was drawn
    without scraping the figure.

    A click on a bar asks the page to edit THAT year's withdrawals. The bars are
    the only place on this page where a year is a thing you can point at, and
    the alternative - hunting the year down in a table - is what made the plan
    feel read-only.
    """

    #: A bar was clicked: open the withdrawal editor on this year.
    year_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent, height=3.4)
        self.rows: list[IncomeYear] = []
        self.bars: dict = {}
        self.fund_line = None
        self.rmd_cents: dict[int, int] = {}      # year -> cents (the page sets it)
        self.rmd_lines = None
        self.fund_axes = None
        # Connected once, at construction: a render replaces the axes, not the
        # canvas, so reconnecting per draw would stack duplicate handlers and
        # fire one click twice.
        self.mpl_connect("button_press_event", self.on_click)

    def series_cents(self) -> dict:
        """label -> one cents figure per row, in stacking order."""
        return {
            SERIES_SOCIAL_SECURITY: [r.social_security_cents for r in self.rows],
            SERIES_OTHER: [r.other_income_cents for r in self.rows],
            SERIES_DEFERRED: [r.deferred_draw_cents for r in self.rows],
            SERIES_TAXABLE_DRAW: [r.taxable_draw_cents for r in self.rows],
            SERIES_ROTH: [r.roth_draw_cents for r in self.rows],
        }

    def fund_cents(self) -> list[int]:
        return [r.fund_value_cents for r in self.rows]

    def set_rows(self, rows: Sequence[IncomeYear]) -> None:
        self.rows = list(rows)
        self.bars = {}
        self.fund_line = None
        self.fund_axes = None
        self.axes = None
        self._years_drawn = ()
        if not self.rows or not any(r.income_cents for r in self.rows):
            self._empty(INCOME_EMPTY_TEXT)
            return

        colors = series_colors()
        fund = self.fund_cents()
        wants_line = any(fund)
        ax = self._new_axes(twin=wants_line)
        years = [r.year for r in self.rows]
        bottom = [0.0] * len(years)
        for label, cents in self.series_cents().items():
            values = [c / 100.0 for c in cents]
            container = ax.bar(years, values, bottom=list(bottom), width=BAR_WIDTH,
                               color=colors[label], label=label, zorder=3)
            self.bars[label] = container
            if label == SERIES_DEFERRED:
                ira_floor = list(bottom)        # where the IRA segment starts
            bottom = [b + v for b, v in zip(bottom, values)]
        ax.set_ylabel("Annual income", fontsize=AXIS_FONT_SIZE)
        ax.set_ylim(bottom=0)
        self._dollars(ax)
        self._years(ax)
        ax.margins(x=0.01)
        self._grid(ax)

        handles = [self.bars[label] for label in INCOME_SERIES]
        labels = list(INCOME_SERIES)
        # The year's required minimum, as a red line ACROSS the IRA segment at
        # the height the minimum reaches in it (reported): a draw above the
        # line is a choice, the part below it is the law.
        marks = [(year, floor + self.rmd_cents.get(year, 0) / 100.0)
                 for year, floor in zip(years, ira_floor)
                 if self.rmd_cents.get(year, 0) > 0]
        if marks:
            self.rmd_lines = ax.hlines(
                [y for _x, y in marks],
                [x - BAR_WIDTH / 2 for x, _y in marks],
                [x + BAR_WIDTH / 2 for x, _y in marks],
                colors=RMD_COLOR, linewidth=2.0, zorder=4)
            from matplotlib.lines import Line2D
            handles.append(Line2D([], [], color=RMD_COLOR, linewidth=2.0))
            labels.append(SERIES_RMD)
        twin = None
        if wants_line:
            twin = ax.twinx()
            (self.fund_line,) = twin.plot(years, [c / 100.0 for c in fund],
                                          color=colors[SERIES_FUND], linewidth=1.6,
                                          label=SERIES_FUND, zorder=1)
            twin.set_ylabel("Invested funds", fontsize=AXIS_FONT_SIZE)
            twin.set_ylim(bottom=0)
            twin.tick_params(labelsize=TICK_FONT_SIZE, length=2, pad=2)
            twin.spines["top"].set_visible(False)
            self._dollars(twin)
            # matplotlib draws a twin axes AFTER its parent, so without this the
            # line lies on top of the bars and reads as a fourth series. Raising
            # the parent and hiding its (already transparent) patch puts the
            # bars in front, which is what "behind the bars" means.
            ax.set_zorder(twin.get_zorder() + 1)
            ax.patch.set_visible(False)
            handles.append(self.fund_line)
            labels.append(SERIES_FUND)
            self.fund_axes = twin

        self._legend(ax, handles, labels)
        self._theme(ax)
        if twin is not None:
            self._theme(twin)
        self.axes = ax
        self._years_drawn = tuple(years)
        self.draw_idle()

    def on_click(self, event) -> None:
        """A click on a bar: ask for that year's withdrawals.

        A METHOD, not a closure, and it reads its event with ``getattr``, so a
        test hands it a stand-in carrying the same attributes and never needs a
        real mouse. The fund line's twin axes counts as a hit too: it sits
        directly under the bars, and a user aiming at a bar should not have to
        know which of two overlapping axes matplotlib decided they hit.
        """
        if self.axes is None:
            return
        # Built by filtering rather than written as a literal pair: with no fund
        # line ``fund_axes`` is None, and an event that landed outside every
        # axes reports ``inaxes`` as None too.
        targets = [a for a in (self.axes, self.fund_axes) if a is not None]
        if getattr(event, "inaxes", None) not in targets:
            return
        year = self.year_at(getattr(event, "xdata", None))
        if year is None:
            return
        self.year_clicked.emit(int(year))


class RothChart(_PlanCanvas):
    """The Roth section's display, and the page's only clickable chart.

    A bar per projected year of PROJECTED TAXABLE INCOME - the stored base
    series, with that year's planned conversion stacked on top - crossed by a
    horizontal line at each ordinary-income bracket edge for the household's
    filing status. Reading the two together is the whole Roth question: how much
    room is left under the next rate, and how much of it is already claimed.

    The conversion is stacked here even though :class:`IncomeChart` keeps it out
    of the income bars, and the two do not disagree: a conversion is not income
    to SPEND, which is what that chart measures, but it is income to be TAXED,
    which is what this one does.

    Clicks arrive through :meth:`on_click`, a plain method rather than a closure
    handed to ``mpl_connect``, so a test drives it with a stand-in event and
    never needs a real mouse. ``bars``, ``lines`` and ``axes`` are kept as
    attributes for the same reason: what was drawn has to be readable without
    scraping the figure. ``axes`` stays None in the empty state, which is what
    makes a click on an empty chart do nothing.
    """

    #: A bracket line was clicked inside a year's bar: (year, edge in cents).
    fill_requested = pyqtSignal(int, int)
    #: A year's conversion was clicked off: the bar's conversion segment, or the
    #: line it is already filled to. The page clears that year's conversions.
    clear_requested = pyqtSignal(int)

    #: A bar was right-clicked: show the schedule, focused on this year.
    schedule_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent, height=2.6)
        self.rows: list[IncomeYear] = []
        self.base_cents: dict[int, int] = {}
        self.status = "single"
        self.index_pct = Decimal(0)
        self.bars: dict = {}
        self.lines: list[tuple[int, object]] = []
        self.axes = None
        self._years_drawn: tuple[int, ...] = ()
        # Connected once, at construction: a render replaces the axes, not the
        # canvas, so reconnecting per draw would stack duplicate handlers and
        # fire one click twice.
        self.mpl_connect("button_press_event", self.on_click)

    def conversion_cents(self) -> list[int]:
        """What each year's conversion ADDS to taxable income: the conversion
        plus any Social Security it makes taxable (IRC 86)."""
        totals = getattr(self, "total_cents", {})
        return [totals.get(r.year, self.base_cents.get(r.year, 0)
                                     + r.conversion_cents)
                - self.base_cents.get(r.year, 0) for r in self.rows]

    def taxable_cents(self) -> list[int]:
        """The height of each bar in cents: base, what the conversion adds, and
        any capital gains (their own rates) on top."""
        gains = getattr(self, "gains_cents", {})
        return [self.base_cents.get(r.year, 0) + c + gains.get(r.year, 0)
                for r, c in zip(self.rows, self.conversion_cents())]

    #: Year -> filing status, where it differs from ``status`` (a survivor
    #: files single): that year's line is the same bracket on its own ladder.
    status_by_year: dict = {}

    def edge_in(self, edge_cents: int, year: int) -> int:
        """A bracket top, published for the table year, as of ``year``."""
        here = self.status_by_year.get(int(year), self.status)
        if here != self.status:
            try:
                main = [b.upper_cents for b in retirement.tax_brackets(self.status)]
                other = [b.upper_cents for b in retirement.tax_brackets(here)]
                if edge_cents in main and other[main.index(edge_cents)] is not None:
                    edge_cents = other[main.index(edge_cents)]
            except ValueError:
                pass
        return retirement.indexed_cents(edge_cents, self.index_pct, year)

    def bracket_edges_cents(self) -> list[tuple[int, str]]:
        """(edge, label) for every bracket line this chart should draw.

        Edges are the PUBLISHED tops; each is drawn rising year by year
        (:meth:`edge_in`), so "above the tallest bar" is judged in table-year
        dollars.

        Every edge up to the tallest bar, plus :data:`BRACKET_LINES_ABOVE` more,
        so the room above the plan is visible without the 37% edge - millions,
        for a joint filer - flattening every bar into the axis.
        """
        try:
            ladder = retirement.tax_brackets(self.status)
        except ValueError:
            ladder = retirement.tax_brackets("single")
        tallest = max((height * self.edge_in(100_000_00, TABLE_YEAR_FOR_CHART)
                       // max(1, self.edge_in(100_000_00, r.year))
                       for height, r in zip(self.taxable_cents(), self.rows)),
                      default=0)
        out: list[tuple[int, str]] = []
        above = 0
        for bracket in ladder:
            if bracket.upper_cents is None:
                continue
            if bracket.upper_cents > tallest:
                above += 1
                if above > BRACKET_LINES_ABOVE:
                    break
            out.append((int(bracket.upper_cents), f"top of {bracket.rate_label}"))
        return out

    def set_rows(self, rows: Sequence[IncomeYear], base_by_year: Mapping[int, int],
                 status: str, *, index_pct=0,
                 total_by_year: Optional[Mapping[int, int]] = None,
                 tax_draw_by_year: Optional[Mapping[int, int]] = None,
                 irmaa_lines: Optional[Sequence[Mapping[int, int]]] = None,
                 gains_by_year: Optional[Mapping[int, int]] = None,
                 aca_line: Optional[Mapping[int, int]] = None) -> None:
        self.rows = list(rows)
        self.aca_values = dict(aca_line or {})
        self.gains_cents = {int(y): int(c) for y, c in dict(gains_by_year or {}).items()}
        self.irmaa_values = [dict(line) for line in (irmaa_lines or [])]
        self.irmaa_drawn: list[tuple[int, dict, object]] = []
        self.tax_draw_cents = {int(y): int(c)
                               for y, c in dict(tax_draw_by_year or {}).items()}
        self.base_cents = {int(y): int(c) for y, c in dict(base_by_year).items()}
        self.total_cents = {int(y): int(c) for y, c in dict(total_by_year or {}).items()}
        self.status = str(status or "single")
        self.index_pct = Decimal(str(index_pct or 0))
        self.bars = {}
        self.lines = []
        self.axes = None
        self._years_drawn = ()
        heights = self.taxable_cents()
        if not self.rows or not any(heights):
            self._empty(ROTH_EMPTY_TEXT)
            return

        colors = series_colors()
        ax = self._new_axes()
        years = [r.year for r in self.rows]
        base = [self.base_cents.get(r.year, 0) / 100.0 for r in self.rows]
        # The IRA draw that pays the year's income tax is part of the base, and
        # drawn as its own slice: the conversion AND the tax it causes both
        # have to fit under a bracket line (reported).
        tax_draw = [min(self.tax_draw_cents.get(r.year, 0),
                        self.base_cents.get(r.year, 0)) / 100.0 for r in self.rows]
        own = [b - t for b, t in zip(base, tax_draw)]
        converted = [c / 100.0 for c in self.conversion_cents()]
        self.bars[SERIES_TAXABLE] = ax.bar(
            years, own, width=BAR_WIDTH, color=colors[SERIES_TAXABLE],
            label=SERIES_TAXABLE, zorder=3)
        self.bars[SERIES_TAX_DRAW] = ax.bar(
            years, tax_draw, bottom=own, width=BAR_WIDTH,
            color=colors[SERIES_TAX_DRAW], label=SERIES_TAX_DRAW, zorder=3)
        self.bars[SERIES_CONVERSION] = ax.bar(
            years, converted, bottom=base, width=BAR_WIDTH,
            color=colors[SERIES_CONVERSION], label=SERIES_CONVERSION, zorder=3)
        # Gains from selling taxable accounts: taxable income, but at their
        # own rates on top of ordinary income - so above the conversion, where
        # they cannot be read as filling an ordinary bracket.
        gained = [self.gains_cents.get(y, 0) / 100.0 for y in years]
        if any(gained):
            self.bars[SERIES_GAINS] = ax.bar(
                years, gained, bottom=[b + c for b, c in zip(base, converted)],
                width=BAR_WIDTH, color=colors[SERIES_GAINS], hatch="//",
                label=SERIES_GAINS, zorder=3)
        ax.set_ylabel("Taxable income", fontsize=AXIS_FONT_SIZE)
        self._dollars(ax)
        self._years(ax)
        ax.margins(x=0.01)
        self._grid(ax)

        pal = charts._chart_palette()
        line_color = colors["bracket"]
        top = 0.0
        self._side_labels = []
        self._label_lines: dict = {}
        for edge, label in self.bracket_edges_cents():
            # A STEP per year, because the tops are indexed every year: a flat
            # line held the 2026 top to 2065 (reported).
            steps = [self.edge_in(edge, year) / 100.0 for year in years]
            (line,) = ax.step(years, steps, where="mid", color=line_color,
                              linewidth=1.4, linestyle="-", zorder=2)
            self.lines.append((edge, line))
            top = max(top, max(steps))
            dollars = steps[-1]
            # OUTSIDE the plot, in the right margin: drawn inside it, a label sat
            # on top of whatever bar reached that height and the percentage - the
            # one thing the line is there to say - became unreadable. The x is an
            # axes fraction just past the right edge and clipping is off; the y
            # stays the bracket's own dollar value in data coordinates, so the
            # label still reads against its own dashed line.
            text = ax.text(1.01, dollars, label, ha="left", va="center",
                           fontsize=BRACKET_LABEL_FONT_SIZE, clip_on=False,
                           transform=ax.get_yaxis_transform())
            self._side_labels.append(text)
            self._label_lines[id(text)] = (years, steps)
            if pal is not None:
                text.set_color(colors["text"])
        top = max(top, self._draw_irmaa(ax, years, heights, colors))
        ax.set_ylim(0, self._ceiling(years, heights) or 1.0)
        self._place_clipped_labels(ax)
        self._spread_side_labels(ax)
        # The bracket labels form a second, right-hand axis; name it the way
        # the left one is named (reported: "label the tax brackets axis").
        side = ax.text(1.17, 0.5, "Tax brackets", rotation=90, ha="center",
                       va="center", fontsize=AXIS_FONT_SIZE, clip_on=False,
                       transform=ax.transAxes)
        if pal is not None:
            side.set_color(colors["text"])

        shown = [k for k in (SERIES_TAXABLE, SERIES_TAX_DRAW, SERIES_CONVERSION,
                             SERIES_GAINS) if k in self.bars]
        # The labels of lines leaving through the top share the legend's
        # margin, so the legend rises a line of label text clear of them.
        room = 0.0
        if getattr(self, "_top_labels", []):
            height = (ax.get_position().height * self.figure.get_figheight()
                      * self.figure.dpi)
            if height > 0:
                room = BRACKET_LABEL_FONT_SIZE * 1.5 * self.figure.dpi / 72.0 / height
        self._legend(ax, [self.bars[k] for k in shown], shown, above=room)
        self._theme(ax)
        self.axes = ax
        self._years_drawn = tuple(years)
        self.draw_idle()

    def _ceiling(self, years, heights) -> float:
        """The top of the plot, in dollars: the tallest bar, the first line
        just above it in that bar's year, and every bracket line drawn where it
        starts - whichever is highest - plus a margin.

        Set by the BARS. Set by the highest line drawn, a tier-5 IRMAA line
        indexed out to 2055 pushed the bars into the bottom third (reported);
        lines above the ceiling are clipped at the top instead."""
        if not heights:
            return 0.0
        at = max(range(len(heights)), key=lambda i: heights[i])
        peak = heights[at] / 100.0
        above = [steps[at] for _years, steps in self._label_lines.values()
                 if steps[at] == steps[at] and steps[at] > peak]
        # And every BRACKET line drawn (up to BRACKET_LINES_ABOVE over the
        # tallest bar) at least where it starts, on the left: with the
        # conversions cleared the bars are short, and a ceiling set by them
        # alone hid the 24% and 32% lines entirely (reported).
        brackets = [self.edge_in(edge, self.rows[0].year) / 100.0
                    for edge, _line in self.lines] if self.rows else []
        return max(peak, min(above, default=peak), max(brackets, default=0.0)) * 1.08

    @staticmethod
    def line_exit(years, steps, ceiling):
        """Where a drawn step line leaves the plot, as ``(side, x, y)``, or
        None when no part of it shows.

        ``side`` is "right" (it runs to the last year inside the plot), "top"
        (it rises through the ceiling, at the step between two years) or "end"
        (it stops inside the plot, the IRMAA and ACA lines being drawn only in
        some years). The exit taken is the line's LAST visible stretch, so a
        line that dips back in is labeled where it finally goes. ``x`` is where
        the step line itself turns there - ``ax.step(where="mid")`` changes
        value halfway between years - and ``y`` its last height inside."""
        last = None
        for i, v in enumerate(steps):
            if v == v and v <= ceiling:
                last = i
        if last is None:
            return None
        if last == len(steps) - 1:
            return "right", float(years[last]), float(steps[last])
        following = steps[last + 1]
        x = (float(years[last]) + float(years[last + 1])) / 2.0
        if following == following:          # finite, and above the ceiling
            return "top", x, float(steps[last])
        return "end", x, float(steps[last])

    def _place_clipped_labels(self, ax) -> None:
        """Label every line where it LEAVES the plot (reported: "label them at
        the point they exit the plot"). They used to sit in the right margin
        at the height the line reached in the last year, and a line that rose
        out through the top was stacked there with an arrow - so "top of 24%"
        was printed a whole plot away from where the 24% line was last seen,
        and "ACA cliff" at the right edge beside a line that had stopped a
        decade earlier.

        Leaving through the right side: in the right margin, level with the
        line, as before. Through the top: just ABOVE the plot, centered on the
        crossing. Both are outside the plot, where no bar can cover them (the
        report that moved the labels out of the plot in the first place).
        Stopping inside the plot: at the line's end, on the page's own
        background and above the bars, since the end IS inside. A line that
        never shows loses its label."""
        from matplotlib.transforms import blended_transform_factory

        ceiling = ax.get_ylim()[1]
        pal = charts._chart_palette()
        background = pal["window"] if pal is not None else "white"
        side, top, ends = [], [], []
        over_top = blended_transform_factory(ax.transData, ax.transAxes)
        for text in self._side_labels:
            years, steps = self._label_lines.get(id(text), ((), ()))
            where = self.line_exit(years, steps, ceiling)
            if where is None:
                text.set_visible(False)
                continue
            kind, x, y = where
            if kind == "right":
                side.append(text)
            elif kind == "top":
                text.set_transform(over_top)
                text.set_position((x, 1.0))
                text.set_ha("center")
                text.set_va("bottom")
                top.append(text)
            else:
                text.set_transform(ax.transData)
                text.set_position((x, y))
                text.set_ha("left")
                text.set_va("center")
                text.set_zorder(6)          # over the bars (zorder 3)
                text.set_bbox({"facecolor": background, "edgecolor": "none",
                               "pad": 1.0, "alpha": 0.9})
                ends.append(text)
        self._side_labels = side
        self._top_labels = top
        self._end_labels = ends
        self._spread_top_labels(ax)

    def _spread_top_labels(self, ax) -> None:
        """Keep the labels above the plot from printing over each other: left
        to right, each at least a space clear of the one before, then pulled
        back left as a group if the last would run past the plot's right edge
        (the right margin belongs to the labels level with their lines)."""
        labels = getattr(self, "_top_labels", [])
        if len(labels) < 2:
            return
        renderer = self.figure.canvas.get_renderer()
        pad = BRACKET_LABEL_FONT_SIZE * 0.6 * self.figure.dpi / 72.0
        placed = []
        for text in sorted(labels, key=lambda t: t.get_position()[0]):
            box = text.get_window_extent(renderer=renderer)
            placed.append([text, box.x0, box.width])
        for i in range(1, len(placed)):
            floor = placed[i - 1][1] + placed[i - 1][2] + pad
            placed[i][1] = max(placed[i][1], floor)
        limit = ax.get_window_extent(renderer=renderer).x1
        for i in range(len(placed) - 1, -1, -1):
            ceiling = limit if i == len(placed) - 1 else placed[i + 1][1] - pad
            placed[i][1] = min(placed[i][1], ceiling - placed[i][2])
        to_data = ax.transData.inverted()
        for text, x0, width in placed:
            text.set_x(to_data.transform((x0 + width / 2.0, 0))[0])

    def _spread_side_labels(self, ax) -> None:
        """Push apart right-margin labels that would print over each other.

        Each label sits at its own line's height, and an IRMAA tier can land
        within a few dollars of a bracket top (reported: "top of 24%" printed
        over "IRMAA 4")."""
        # One pass over the right-margin labels from the top down: each is
        # kept at least a line of text below the one above it. (The labels of
        # lines that leave through the top sit above the plot and are spread
        # sideways instead - ``_spread_top_labels``.)
        labels = sorted(getattr(self, "_side_labels", []),
                        key=lambda t: t.get_position()[1], reverse=True)
        if len(labels) < 2:
            return
        gap = BRACKET_LABEL_FONT_SIZE * 1.3 * self.figure.dpi / 72.0
        above = None
        for text in labels:
            y = ax.transData.transform((0, text.get_position()[1]))[1]
            if above is not None and y > above - gap:
                y = above - gap
                text.set_y(ax.transData.inverted().transform((0, y))[1])
            above = y

    def _draw_irmaa(self, ax, years, heights, colors) -> float:
        """Dotted IRMAA lines, one per tier up to the first above the tallest
        bar, drawn only in income years whose premium year has someone on
        Medicare. Returns the highest value drawn, in dollars."""
        tallest = max(heights, default=0)
        top = 0.0
        for tier, values in enumerate(getattr(self, "irmaa_values", []), start=1):
            if not values:
                continue
            steps = [values[y] / 100.0 if y in values else float("nan") for y in years]
            (line,) = ax.step(years, steps, where="mid", color=colors["irmaa"],
                              linewidth=1.4, linestyle="-", zorder=2)
            self.irmaa_drawn.append((tier, values, line))
            last = next(v for v in reversed(steps) if v == v)
            text = ax.text(1.01, last, f"IRMAA {tier}", ha="left", va="center",
                           fontsize=BRACKET_LABEL_FONT_SIZE, clip_on=False,
                           color=colors["irmaa"], transform=ax.get_yaxis_transform())
            text.set_zorder(4)
            self._side_labels.append(text)
            self._label_lines[id(text)] = (years, steps)
            top = max(top, max(v for v in steps if v == v))
            if min(values.values()) > tallest:
                break                       # the first tier above every bar
        # The ACA cliff, before 65: its own color, a click target like a tier.
        values = getattr(self, "aca_values", {})
        if values:
            steps = [values[y] / 100.0 if y in values else float("nan") for y in years]
            (line,) = ax.step(years, steps, where="mid", color=colors["aca"],
                              linewidth=1.4, linestyle="-", zorder=2)
            self.irmaa_drawn.append((0, values, line))
            last = next(v for v in reversed(steps) if v == v)
            text = ax.text(1.01, last, "ACA cliff", ha="left", va="center",
                           fontsize=BRACKET_LABEL_FONT_SIZE, clip_on=False,
                           color=colors["aca"], transform=ax.get_yaxis_transform())
            self._side_labels.append(text)
            self._label_lines[id(text)] = (years, steps)
            top = max(top, max(v for v in steps if v == v))
        return top

    # -- clicks -------------------------------------------------------------
    def bracket_edge_at(self, ydata, year: Optional[int] = None) -> Optional[int]:
        """The drawn bracket edge this y coordinate lands on, in cents, or None.

        In ``year``'s dollars when a year is given: the lines step up every year,
        and a click fills to the top as it stands in the year clicked.

        The tolerance is a fraction of the axis rather than a number of dollars:
        the line is one pixel tall and the axis spans anything from tens of
        thousands to millions, so a fixed dollar window would be unhittable on
        one plan and sloppy on another.
        """
        if ydata is None or self.axes is None or not self.lines:
            return None
        try:
            y = float(ydata)
        except (TypeError, ValueError):
            return None
        low, high = self.axes.get_ylim()
        tolerance = abs(high - low) * LINE_HIT_FRACTION
        best: Optional[int] = None
        best_gap = None
        for edge, _line in self.lines:
            here = edge if year is None else self.edge_in(edge, year)
            gap = abs(y - here / 100.0)
            if gap <= tolerance and (best_gap is None or gap < best_gap):
                best, best_gap = int(here), gap
        # An IRMAA line is a line to fill to as well - its own value per year.
        for _tier, values, _line in getattr(self, "irmaa_drawn", []):
            if year is None or int(year) not in values:
                continue
            here = values[int(year)]
            gap = abs(y - here / 100.0)
            if gap <= tolerance and (best_gap is None or gap < best_gap):
                best, best_gap = int(here), gap
        return best

    def on_click(self, event) -> None:
        """Turn a click on the plot into one of this section's two requests.

        A METHOD, not a closure, and it reads its event with ``getattr``:
        matplotlib's event is a plain object, so a test hands this a stand-in
        carrying the same four attributes. Nothing is written here - the chart
        only says what was asked for, and the page owns every plan write.
        """
        if self.axes is None or getattr(event, "inaxes", None) is not self.axes:
            return
        year = self.year_at(getattr(event, "xdata", None))
        if year is None:
            return
        button = getattr(event, "button", None)
        if button == 3:
            self.schedule_requested.emit(int(year))
            return
        if button != 1:
            return
        ydata = getattr(event, "ydata", None)
        edge = self.bracket_edge_at(ydata, int(year))
        row = next((r for r in self.rows if r.year == int(year)), None)
        converted = row.conversion_cents if row is not None else 0
        base = self.base_cents.get(int(year), 0)
        # A TOGGLE (reported: undoing a conversion meant editing the table).
        # The line the bar is already filled to clears it, and so does a click
        # on the conversion segment itself; any other line fills to it.
        if edge is not None:
            if converted and self.filled_to(base + converted, edge):
                self.clear_requested.emit(int(year))
            else:
                self.fill_requested.emit(int(year), int(edge))
            return
        try:
            y = float(ydata) * 100.0
        except (TypeError, ValueError):
            return
        if converted and base <= y <= base + converted:
            self.clear_requested.emit(int(year))

    @staticmethod
    def filled_to(top_cents: int, edge_cents: int) -> bool:
        """Whether a bar's top sits on a line: within $50 or 0.5%, because the
        top is re-derived from the plan's draws after a fill and can land a few
        cents off the line it was aimed at."""
        return abs(int(top_cents) - int(edge_cents)) <= max(50_00,
                                                            int(edge_cents) // 200)


#: How "Fill years" names an IRMAA tier line, beside a bracket's bare rate
#: ("22"): ``irmaa:2`` is the chart's "IRMAA 2" line.
IRMAA_FILL_PREFIX = "irmaa:"


def irmaa_fill_key(tier: int) -> str:
    """The "Fill years" value for IRMAA tier ``tier`` (1 is the first
    surcharge)."""
    return f"{IRMAA_FILL_PREFIX}{int(tier)}"


#: The "Fill years" value for the chart's "ACA cliff" line.
ACA_FILL_KEY = "aca"


def irmaa_fill_tier(line: str) -> Optional[int]:
    """The IRMAA tier a "Fill years" value names, or None for a bracket rate."""
    text = str(line)
    if not text.startswith(IRMAA_FILL_PREFIX):
        return None
    return int(text[len(IRMAA_FILL_PREFIX):])


def allocate_conversion(total_cents: int, weights: Sequence[int]) -> list[int]:
    """Split one year's total conversion across the source accounts, in
    proportion to ``weights`` - what each source holds entering that year.

    The plan treats the tax-deferred accounts as ONE hypothetical IRA; the
    per-account rows exist so the Investment Dashboard can show each account's
    projection. Drawn in proportion to what each holds, every account keeps its
    share of the pool, the same way the household withdrawals draw
    (``retirement._draw_proportionally``); draining them one after another
    gains nothing and leaves the dashboard showing one account emptied while
    the others sit untouched. The rounding remainder lands on the last funded
    row so the parts add up to the total exactly - a click that stored a dollar
    less than the gap would leave the bar a hair under the line it was aimed
    at. With no weight anywhere, it all goes to the first account.
    """
    total = max(0, int(total_cents))
    count = len(weights)
    if count == 0:
        return []
    values = [max(0, int(w)) for w in weights]
    pool = sum(values)
    if pool <= 0:
        out = [0] * count
        out[0] = total
        return out
    out = [total * v // pool for v in values]
    last = max(i for i, v in enumerate(values) if v > 0)
    out[last] += total - sum(out)
    return out




# ---------------------------------------------------------------------------
# the inline conversion schedule
# ---------------------------------------------------------------------------
#: The combo id of a planned Roth that does not exist yet. Real accounts are
#: positive and planned ones negative (``retirement.is_planned``), so zero can
#: mean only this.
NEW_PLANNED_TARGET = 0

SCHEDULE_COLUMNS = ("Year / account", "Taxable income", "Conversion", "To Roth")
COL_YEAR, COL_TAXABLE, COL_CONVERSION, COL_TARGET = range(4)
#: A tree item's role holding what the row is: ("year", year) or
#: ("account", year, account_id).
ROW_ROLE = Qt.UserRole
#: A target cell's role holding the chosen Roth's key.
TARGET_ROLE = Qt.UserRole + 1

#: What an unowned source says. The owner decides where a conversion lands (the
#: same owner's Roth at the same institution), so without one the destination is
#: a guess, and Account Details is where it is fixed.
TAXABLE_TIP = ("Computed: the taxable share of Social Security, taxable Other "
               "income and the year's IRA draws, less the standard deduction. "
               "Conversions are stacked on top of it. Add extra income for a "
               "year as an Other income source.")

NO_OWNER_TIP = ("No owner is set for this account, so the planner cannot tell "
                "whose Roth its conversions belong in. Double-click the account "
                "to open Account Details and set the Owner.")


class _ReadOnlyDelegate(QStyledItemDelegate):
    """No editor: the column is computed, not typed."""

    def createEditor(self, parent, option, index):  # noqa: N802 (Qt's name)
        return None


class _TargetDelegate(QStyledItemDelegate):
    """The To Roth cell's editor: a combo that exists only while editing.

    A combo per row, built up front, was one cell widget for every account in
    every year; rebuilding hundreds of them on each edit is what pegged the CPU.
    ``setModelData`` only stores the choice on the item - the write happens in
    the tree's ``itemChanged`` handler and the rebuild is deferred, so nothing
    modal or destructive runs while Qt is tearing the editor down (CLAUDE.md).
    """

    def __init__(self, schedule):
        super().__init__(schedule.tree)
        self._schedule = schedule

    def createEditor(self, parent, option, index):  # noqa: N802 (Qt's name)
        kind = index.sibling(index.row(), COL_YEAR).data(ROW_ROLE)
        if not kind or kind[0] != "account":
            return None
        acct = self._schedule.account_for(kind[2])
        combo = NoWheelComboBox(parent)
        for target in self._schedule.eligible_targets(acct) if acct else []:
            combo.addItem(target.name, target.account_id)
        return combo

    def setEditorData(self, editor, index):  # noqa: N802
        at = editor.findData(index.data(TARGET_ROLE))
        editor.setCurrentIndex(max(at, 0))

    def setModelData(self, editor, model, index):  # noqa: N802
        if editor.currentData() is None:
            return
        model.setData(index, editor.currentText(), Qt.DisplayRole)
        model.setData(index, int(editor.currentData()), TARGET_ROLE)


class _PlanEditor(QWidget, PlanEngine):
    """The page's two inline tables: a QWidget over :class:`PlanEngine`.

    What they COMPUTE - accounts, balances, the RMD floor, the projections -
    is the engine's (``mammon/retirement_plan.py``), shared with the MCP
    surface; what is here is the widget, its signals and the writes.

    Both of them are a grid of plan years that writes through
    ``mammon.retirement``, and both have to answer "what is the smallest
    distribution the law allows here" the same way. Two copies of that question
    would eventually give two answers on one screen - which is exactly what
    happened when the withdrawal table was added - so the rule lives once, here,
    and defers to the domain layer for the rule itself.

    Neither table has a notice label of its own. Messages leave by :attr:`said`
    so the page can show them in one always-visible place: a notice inside a
    collapsible table is a notice the user never sees when the table is hidden.
    """

    #: The stored plan changed; whoever is showing it should re-read.
    changed = pyqtSignal()

    #: Something the user has to be told (an RMD clamp, an ineligible target).
    said = pyqtSignal(str)

    def __init__(self, conn, parent=None, *, today: Optional[_dt.date] = None):
        super().__init__(parent)
        self._init_engine(conn, today)
        self._rows: list[dict] = []
        # Raised around a rebuild so the items being written do not read back as
        # the user editing them: setItem emits itemChanged just as a typed edit
        # does, and without this flag a reload would rewrite the plan it is
        # displaying.
        self._loading = False
        self._focus_year: Optional[int] = None
        self._build()

    def _build(self) -> None:  # pragma: no cover - subclasses build themselves
        raise NotImplementedError

    def reload(self) -> None:  # pragma: no cover - subclasses fill themselves
        raise NotImplementedError

    # -- what the table covers ----------------------------------------------
    def set_years(self, years: Sequence[int]) -> None:
        """Point the table at a horizon and rebuild it."""
        self._years = [int(y) for y in years]
        self.invalidate_plan()
        self.reload()

    def years_shown(self) -> list[int]:
        return list(self._years)


    def account_label(self, account_id: int) -> str:
        for acct in self.accounts():
            if acct.account_id == int(account_id):
                return acct.name
        row = ledger.get_account(self.conn, int(account_id))
        return row["name"] if row else f"account {account_id}"







    def floor_citation(self, account_id: int, year: int) -> str:
        """The rule line behind a clamped cell, or "" when the cell has no floor."""
        floor = self.rmd_floor_cents(account_id, year)
        if floor <= 0:
            return ""
        birth_year, _ = self.birth_for_account(account_id)
        age = int(year) - int(birth_year)
        divisor = retirement.uniform_lifetime_divisor(age)
        return (
            f"IRC 401(a)(9) requires at least {fmt_money(floor)} from "
            f"{self.account_label(account_id)} in {year}: the {year - 1}-12-31 "
            f"balance of {fmt_money(self.prior_year_end_balance_cents(account_id, year))} "
            f"over Uniform Lifetime Table divisor {divisor} at age {age}."
        )













    # -- writes, all through mammon.retirement ------------------------------
    def say(self, text: str) -> None:
        if text:
            self.said.emit(text)

    def set_withdrawal(self, account_id: int, year: int, amount_cents: int) -> int:
        """Store a planned distribution, clamped both ways. Returns what was stored.

        Up to the required minimum (IRC 401(a)(9)) and down to what the account
        is projected to still hold that year. The floor wins a collision: a
        minimum the account cannot cover is a fact about the plan, not a number
        Mammon is free to reduce.
        """
        wanted = abs(int(amount_cents))
        floor = self.rmd_floor_cents(account_id, year)
        cap = self.cap_cents(account_id, year)
        stored = max(floor, min(wanted, max(cap, floor)))
        retirement.set_withdrawal(self.conn, int(account_id), int(year), stored)
        self.invalidate_plan()
        if stored > wanted:
            self.say(
                f"{fmt_money(wanted)} is below the required minimum and was raised "
                f"to {fmt_money(stored)}. " + self.floor_citation(account_id, year)
            )
        elif stored < wanted:
            self.say(
                f"{fmt_money(wanted)} is more than {self.account_label(account_id)} "
                f"is projected to hold in {year}, so it was reduced to "
                f"{fmt_money(stored)} - the whole of what is left."
            )
        self.changed.emit()
        return stored

    def clear_withdrawal(self, account_id: int, year: int) -> None:
        """Forget a planned distribution entirely.

        The year goes back to UNPLANNED, which is what the seeding pass fills:
        clearing a cell puts the required minimum back rather than leaving a
        hole.
        """
        retirement.delete_withdrawal(self.conn, int(account_id), int(year))
        self.invalidate_plan()
        self.changed.emit()

    @staticmethod
    def _item(text: str, *, editable: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if editable:
            flags |= Qt.ItemIsEditable
        item.setFlags(flags)
        return item


class ConversionSchedule(_PlanEditor):
    """The year-by-year conversion plan, editable IN the page.

    One compact line per YEAR - its base taxable income and its total
    conversion - which expands to the tax-deferred accounts converting that
    year, each with an amount and a destination Roth. Only IRA-type sources get
    a row: a Roth converts into nothing, and withdrawals have their own table
    (a withdrawal column here was a second, confusing place to edit them).
    Editing a year's total spreads it over the sources exactly as a click on a
    bracket line does (:meth:`set_total_conversion`).

    Reported: clicking in the old flat table (a row per year per account, a
    live combo in every row, every column sized to its contents) pegged the
    CPU - each rebuild re-measured every row for every cell set. The tree builds
    an account's rows only when its year is expanded, sizes columns once, and
    makes the destination combo only while that cell is being edited.

    Every plan write in the Roth section happens here, through
    ``mammon.retirement`` - the chart only reports clicks and the page only
    routes them. A source with no owner is flagged with the amber triangle and
    points to Account Details (:attr:`accountDetailsRequested`), because the
    owner decides which Roth the money lands in.
    """

    #: The page asks the window to open Account Details for an account; the
    #: schedule never reaches for the window itself.
    accountDetailsRequested = pyqtSignal(int)
    #: "Fill years": (first year, last year, line) - a bracket rate ("22") or
    #: an IRMAA tier (``irmaa_fill_key``). The page owns the base taxable
    #: income the gap is measured from, so it does the filling.
    fillRangeRequested = pyqtSignal(int, int, str)

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        # What to do here, in a few sentences - the same treatment as the
        # withdrawal window's Spending plan section (reported: organize it and
        # "explain (just a bit) what to do").
        help_box = QGroupBox("Roth conversions", self)
        help_layout = QVBoxLayout(help_box)
        self.help_text = QLabel(
            "A conversion moves money from an IRA or 401(k) into a Roth: taxed "
            "now, tax-free later. Each line below is a year - its taxable income "
            "and its total conversion. Open a year to set each account's amount "
            "and the Roth it goes to; typing a year's total spreads it across "
            "the accounts.\n"
            "On the chart, click a bracket line to fill a year up to it, and "
            "click it again - or the purple part of the bar - to remove it. An "
            "account with an amber triangle has no owner: double-click it to set "
            "one in Account Details.", help_box)
        self.help_text.setWordWrap(True)
        help_layout.addWidget(self.help_text)
        # Fill a run of years at once - one click per year on the chart was the
        # only way before (reported).
        fill = QHBoxLayout()
        fill.addWidget(QLabel("Fill", help_box))
        self.fill_from = NoWheelSpinBox(help_box)
        self.fill_to = NoWheelSpinBox(help_box)
        for box in (self.fill_from, self.fill_to):
            box.setRange(self._today.year, self._today.year + 60)
        self.fill_from.setValue(self._today.year)
        self.fill_to.setValue(self._today.year + 5)
        fill.addWidget(self.fill_from)
        fill.addWidget(QLabel("through", help_box))
        fill.addWidget(self.fill_to)
        fill.addWidget(QLabel("up to", help_box))
        # Every line the chart draws to fill to, named as the chart names it:
        # the bracket tops ("top of 22%"), then the IRMAA tiers ("IRMAA 2").
        # The tiers used to be a checkbox that stopped a bracket fill at "the
        # next IRMAA tier" - which tier that was changed from year to year,
        # and none of them could be aimed at (reported).
        self.fill_bracket = NoWheelComboBox(help_box)
        for bracket in retirement.tax_brackets("joint"):
            if bracket.upper_cents is not None:
                self.fill_bracket.addItem(f"top of {bracket.rate_label}",
                                          bracket.rate_label.rstrip("%"))
        self.fill_bracket.insertSeparator(self.fill_bracket.count())
        tiers = len(retirement.irmaa_ceilings_cents("joint", self._today.year))
        for tier in range(1, tiers + 1):
            self.fill_bracket.addItem(f"IRMAA {tier}", irmaa_fill_key(tier))
            self.fill_bracket.setItemData(
                self.fill_bracket.count() - 1,
                f"The income at which Medicare surcharge tier {tier} begins. It "
                f"is drawn only for years whose income sets a Medicare premium "
                f"(two years later, once someone is 65); other years are left "
                f"as they are.", Qt.ToolTipRole)
        self.fill_bracket.addItem("ACA cliff", ACA_FILL_KEY)
        self.fill_bracket.setItemData(
            self.fill_bracket.count() - 1,
            "The income at which the ACA premium tax credit ends all at once "
            "(400% of the poverty line). It is drawn only for years someone "
            "buys marketplace coverage - retired and not yet on Medicare, with "
            "a benchmark premium entered; other years are left as they are.",
            Qt.ToolTipRole)
        self.fill_bracket.setCurrentIndex(max(0, self.fill_bracket.findData("22")))
        fill.addWidget(self.fill_bracket)
        self.fill_button = QPushButton("Fill years", help_box)
        self.fill_button.setToolTip(
            "Set each year's conversion so its taxable income reaches that line: "
            "a bracket top as indexed for the year, or an IRMAA tier, which a "
            "fill reaches at or just under because a tier is a cliff. A year "
            "already above the line converts nothing.")
        self.fill_button.clicked.connect(
            lambda: self.fillRangeRequested.emit(
                self.fill_from.value(), self.fill_to.value(),
                str(self.fill_bracket.currentData())))
        fill.addWidget(self.fill_button)
        fill.addStretch(1)
        help_layout.addLayout(fill)
        self._filing_row = QHBoxLayout()
        help_layout.addLayout(self._filing_row)
        layout.addWidget(help_box)
        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(len(SCHEDULE_COLUMNS))
        self.tree.setHeaderLabels(list(SCHEDULE_COLUMNS))
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(14)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.DoubleClicked
                                  | QAbstractItemView.SelectedClicked
                                  | QAbstractItemView.EditKeyPressed)
        header = self.tree.header()
        header.setStretchLastSection(True)
        for col, width in ((COL_YEAR, 260), (COL_TAXABLE, 120),
                           (COL_CONVERSION, 110)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            self.tree.setColumnWidth(col, width)
        self.tree.setItemDelegateForColumn(COL_TARGET, _TargetDelegate(self))
        # Taxable income is COMPUTED (the page's ``_seed_taxable``): the year
        # line is editable for its total conversion, so this column needs a
        # delegate that makes no editor.
        self.tree.setItemDelegateForColumn(COL_TAXABLE, _ReadOnlyDelegate(self.tree))
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._warning_icon = None
        layout.addWidget(self.tree, 1)

    def eligible_targets(self, acct: PlanAccount) -> list[PlanAccount]:
        """The Roths this account may convert into, the default first.

        The default is the custodian's answer (``retirement.resolve_conversion_
        target``): the same owner's Roth at the same institution, and when the
        ledger has none, a PLANNED one there. A planned Roth not created yet is
        offered with :data:`NEW_PLANNED_TARGET` as its id and only comes into
        being when a conversion is written to it - opening the page writes
        nothing. The owner's other Roths follow, because moving the money on to
        another custodian is a real choice, just not the default one.
        """
        if acct.is_roth:
            return []
        key, name = retirement.resolve_conversion_target(self.conn, acct.account_id)
        first = next((r for r in self._roths if r.account_id == key), None)
        if first is None:
            first = PlanAccount(account_id=NEW_PLANNED_TARGET, name=name,
                                treatment="roth",
                                owner_person_id=acct.owner_person_id,
                                institution=acct.institution)
        others = [r for r in self._roths
                  if r.account_id != first.account_id and not r.planned
                  and r.owner_person_id == acct.owner_person_id]
        return [first] + others

    def _materialize(self, from_account_id: int, target: int) -> Optional[int]:
        """A target key ready to store: creates the planned Roth on first use."""
        if int(target) != NEW_PLANNED_TARGET:
            return int(target)
        return retirement.conversion_target(self.conn, int(from_account_id))

    # -- writes, all through mammon.retirement ------------------------------
    def set_taxable_income(self, year: int, amount_cents: int) -> int:
        """Store a year's BASE projected taxable income as the user's own figure."""
        amount = abs(int(amount_cents))
        retirement.set_taxable_income(self.conn, int(year), amount, source="entered")
        self.changed.emit()
        return amount

    def not_convertible_reason(self, account_id: int, year: int) -> Optional[str]:
        """Why this account cannot convert in ``year``, or None. Money in a
        CURRENT employer's plan moves to a Roth only through an in-plan
        rollover or an in-service distribution, which the law forbids before
        59 1/2 (IRC 402A(c)(4), 401(k)(2)(B)) and a plan is free to refuse at
        any age. Mammon cannot see what a plan allows, and a plan built on an
        in-service distribution the plan refuses cannot be carried out, so a
        current employer's plan converts nothing until the job ends (reported:
        a plan converted a current employer's 401(k) the owner could not yet
        draw). An
        inherited account (non-spouse) cannot be converted at all
        (IRC 408(d)(3)(C))."""
        acct = next((a for a in self.accounts() if a.account_id == int(account_id)), None)
        if acct is None:
            return None
        if acct.inherited_death_year is not None:
            return "an inherited account cannot be converted"
        if not acct.current_employer_plan:
            return None
        ends = retirement.employer_plan_ends(self.conn, self.retirement_year()).get(
            acct.account_id)
        if ends is not None and int(year) >= int(ends):
            return None
        return "a current employer's plan cannot be converted before leaving the employer"

    def conversion_cap_cents(self, account_id: int, year: int) -> int:
        """The most this account can convert in ``year``: what it is projected
        to hold entering the year, less that year's planned distribution - and
        nothing at all while :meth:`not_convertible_reason` says so.

        Reported: a small 401(k) was converting millions into a Roth over a
        decade. Nothing capped a conversion, and the source's projection clamps
        at zero, so the planner showed an empty 401(k) beside a Roth that had
        been handed money that never existed - and the dashboard's projection
        of that Roth climbed with it."""
        if self.not_convertible_reason(account_id, year) is not None:
            return 0
        flow = self.flows_for(int(year)).get(int(account_id))
        drawn = int(flow.distribution_cents) if flow else 0
        return max(0, self.cap_cents(account_id, year) - drawn)

    def set_conversion(self, from_account_id: int, to_account_id: int, year: int,
                       amount_cents: int) -> int:
        """Store a planned conversion, capped at what the source holds.

        Returns what was stored, in cents; zero clears the conversion."""
        wanted = abs(int(amount_cents))
        amount = min(wanted, self.conversion_cap_cents(from_account_id, year))
        why = self.not_convertible_reason(from_account_id, year)
        if why is not None and wanted > 0:
            self.say(f"Nothing converts from {self.account_label(from_account_id)} "
                     f"in {year}: {why}.")
        elif amount < wanted:
            self.say(
                f"{fmt_money(wanted)} is more than "
                f"{self.account_label(from_account_id)} is projected to hold in "
                f"{year}, so the conversion was reduced to {fmt_money(amount)}.")
        if amount <= 0:
            self.delete_conversion(from_account_id, to_account_id, year)
            return 0
        retirement.set_conversion(
            self.conn, int(from_account_id), int(to_account_id), int(year), amount
        )
        self.invalidate_plan()
        self.changed.emit()
        return amount

    def delete_conversion(self, from_account_id: int, to_account_id: int,
                          year: int) -> None:
        retirement.delete_conversion(
            self.conn, int(from_account_id), int(to_account_id), int(year)
        )
        self.invalidate_plan()
        self.changed.emit()

    def clear_conversions(self, account_id: int, year: int,
                          keep_target: Optional[int] = None) -> None:
        """Drop this source's conversions for the year, except one target.

        One source converts into at most one Roth per year on this screen. The
        limit is the table's, not the law's: the stored model allows several, and
        a row that could only show one of them would hide the rest.
        """
        for row in retirement.list_conversions(self.conn, int(year)):
            if int(row["from_account_id"]) != int(account_id):
                continue
            target = int(row["to_account_id"])
            if keep_target is not None and target == int(keep_target):
                continue
            self.delete_conversion(account_id, target, year)

    def set_total_conversion(self, year: int, total_cents: int, *,
                             rebuild: bool = True) -> int:
        """Make a year's conversions add up to exactly ``total_cents``.

        This is what a click on a bracket line, a typed year total and every
        fill do. The total is split in proportion to what each source can
        convert that year (:func:`allocate_conversion`), whatever split the
        year held before; a source with no Roth of its own cannot take a share,
        and if no source has one, nothing is written and the user is told why.
        """
        year = int(year)
        total = max(0, int(total_cents))
        self._read_accounts()
        deferred = [a for a in self._accounts if not a.is_roth]
        usable = [a for a in deferred if self.eligible_targets(a)]
        for acct in deferred:
            if acct not in usable:
                self.clear_conversions(acct.account_id, year)
        if not usable:
            self.say("There is no tax-deferred account with a Roth of its own "
                     "owner to convert into, so nothing was changed. A Roth "
                     "conversion has to land in a Roth owned by the same person "
                     "as the source account (IRC 408A(d)(3)).")
            self._rebuild_after_edit(rebuild)
            return 0
        stored_targets = {}
        for row in retirement.list_conversions(self.conn, year):
            stored_targets.setdefault(int(row["from_account_id"]),
                                      int(row["to_account_id"]))
        caps = [self.conversion_cap_cents(a.account_id, year) for a in usable]
        # Weighted by what each source ENTERS the year holding - its share of
        # the hypothetical IRA - not by what is left after this year's stored
        # draws: those were written by the last apply and go stale the moment
        # any conversion moves, and weighting by them let the split drift away
        # from the plan's own balances year after year.
        parts = allocate_conversion(total, [
            self.cap_cents(a.account_id, year) if cap > 0 else 0
            for a, cap in zip(usable, caps)])
        # No source converts more than it holds (rounding is all a proportional
        # split can put over); what one cannot take moves to the others that
        # still have room, and whatever nobody can take is reported rather than
        # silently created.
        parts = [min(p, c) for p, c in zip(parts, caps)]
        spill = total - sum(parts)
        for index, cap in enumerate(caps):
            take = min(spill, cap - parts[index])
            if take > 0:
                parts[index] += take
                spill -= take
        if spill > 0:
            self.say(f"The tax-deferred accounts are projected to hold only "
                     f"{fmt_money(sum(parts))} to convert in {year}, short of "
                     f"the {fmt_money(total)} this bracket needs.")
        stored = 0
        for acct, part in zip(usable, parts):
            if part <= 0:
                self.clear_conversions(acct.account_id, year)
                continue
            targets = self.eligible_targets(acct)
            wanted = stored_targets.get(acct.account_id)
            target = wanted if any(t.account_id == wanted for t in targets) \
                else targets[0].account_id
            # Clear BEFORE creating a planned Roth: clearing prunes planned Roths
            # nothing converts into, which would include a brand-new one.
            self.clear_conversions(acct.account_id, year, keep_target=target)
            target = self._materialize(acct.account_id, target)
            stored += self.set_conversion(acct.account_id, target, year, part)
        unowned = [a.name for a, part in zip(usable, parts)
                   if part > 0 and self.unowned(a)]
        if unowned:
            self.say(f"{', '.join(unowned)} {'has' if len(unowned) == 1 else 'have'} "
                     "no owner set, so where the conversion lands is a guess. "
                     "Double-click the account in the schedule to open Account "
                     "Details and set the Owner.")
        self._rebuild_after_edit(rebuild)
        return stored

    # -- the tree -----------------------------------------------------------
    def sources(self) -> list[PlanAccount]:
        """The accounts that can convert: tax-deferred, in the ledger."""
        return [a for a in self._accounts if a.treatment == "deferred"]

    def account_for(self, account_id: int) -> Optional[PlanAccount]:
        return next((a for a in self._accounts
                     if a.account_id == int(account_id)), None)

    @staticmethod
    def unowned(acct: PlanAccount) -> bool:
        return acct.owner_person_id is None and not acct.planned

    def warning_icon(self):
        if self._warning_icon is None:
            from PyQt5.QtGui import QIcon
            from mammon.ui.asset_allocation import warning_pixmap
            self._warning_icon = QIcon(warning_pixmap())
        return self._warning_icon

    def _conversions(self, year: int) -> dict[int, tuple[int, int]]:
        """Source -> (target key, amount) for one year; the largest if several."""
        out: dict[int, tuple[int, int]] = {}
        for row in retirement.list_conversions(self.conn, int(year)):
            src, amount = int(row["from_account_id"]), int(row["amount_cents"])
            if src not in out or amount > out[src][1]:
                out[src] = (int(row["to_account_id"]), amount)
        return out

    def reload(self) -> None:
        """Rebuild the year lines from the stored plan, keeping what was open."""
        self._read_accounts()
        taxable = retirement.taxable_income_map(self.conn)
        expanded = {int(y) for y in self.expanded_years()}
        self._loading = True
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            for year in self._years:
                item = QTreeWidgetItem(self.tree)
                item.setData(COL_YEAR, ROW_ROLE, ("year", int(year)))
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                self._fill_year(item, int(year), taxable.get(int(year)))
                if int(year) in expanded:
                    item.setExpanded(True)
                    self._fill_accounts(item)
        finally:
            self.tree.setUpdatesEnabled(True)
            self._loading = False
        self.focus_year(self._focus_year)

    def _fill_year(self, item, year: int, taxable_cents: Optional[int]) -> None:
        conversions = self._conversions(year)
        total = sum(amount for _t, amount in conversions.values())
        item.setText(COL_YEAR, str(year))
        item.setText(COL_TAXABLE, fmt_cents(taxable_cents or 0))
        item.setToolTip(COL_TAXABLE, TAXABLE_TIP)
        item.setText(COL_CONVERSION, fmt_cents(total) if total else "")
        names = sorted({self.account_label(t) for t, _a in conversions.values()})
        item.setText(COL_TARGET, ", ".join(names))
        flagged = [self.account_for(src) for src in conversions]
        flagged = [a for a in flagged if a is not None and self.unowned(a)]
        if flagged:
            item.setIcon(COL_YEAR, self.warning_icon())
            item.setToolTip(COL_YEAR, NO_OWNER_TIP)
        font = item.font(COL_CONVERSION)
        font.setBold(bool(total))
        item.setFont(COL_CONVERSION, font)

    def _fill_accounts(self, year_item) -> None:
        """Build a year's account lines - only ever for an expanded year."""
        if year_item.childCount():
            return
        year = int(year_item.data(COL_YEAR, ROW_ROLE)[1])
        conversions = self._conversions(year)
        was_loading, self._loading = self._loading, True
        try:
            for acct in self.sources():
                child = QTreeWidgetItem(year_item)
                child.setData(COL_YEAR, ROW_ROLE,
                              ("account", year, acct.account_id))
                child.setFlags(child.flags() | Qt.ItemIsEditable)
                child.setText(COL_YEAR, acct.name)
                if self.unowned(acct):
                    child.setIcon(COL_YEAR, self.warning_icon())
                    child.setToolTip(COL_YEAR, NO_OWNER_TIP)
                stored = conversions.get(acct.account_id)
                child.setText(COL_CONVERSION, fmt_cents(stored[1]) if stored else "")
                if stored:
                    target = stored[0]
                    name = self.account_label(target)
                else:
                    targets = self.eligible_targets(acct)
                    target = targets[0].account_id if targets else None
                    name = targets[0].name if targets else ""
                child.setText(COL_TARGET, name)
                child.setData(COL_TARGET, TARGET_ROLE, target)
        finally:
            self._loading = was_loading

    def add_filing_status(self, combo: QComboBox) -> None:
        """Show the page's filing-status choice in this window (reported: it
        belongs with the conversions its bracket lines decide)."""
        self._filing_row.addWidget(QLabel("Filing status"))
        combo.setParent(self)
        self._filing_row.addWidget(combo)
        self._filing_row.addStretch(1)

    def _rebuild_after_edit(self, now: bool) -> None:
        if now:
            self.reload()

    def _on_expanded(self, item) -> None:
        if item.parent() is None:
            self._fill_accounts(item)

    def expanded_years(self) -> list[int]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.isExpanded():
                out.append(int(item.data(COL_YEAR, ROW_ROLE)[1]))
        return out

    def year_item(self, year: int):
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if int(item.data(COL_YEAR, ROW_ROLE)[1]) == int(year):
                return item
        return None

    def account_item(self, year: int, account_id: int):
        """A year's line for one account, expanding the year to build it."""
        item = self.year_item(year)
        if item is None:
            return None
        item.setExpanded(True)
        self._fill_accounts(item)
        for i in range(item.childCount()):
            child = item.child(i)
            if int(child.data(COL_YEAR, ROW_ROLE)[2]) == int(account_id):
                return child
        return None

    def _on_double_clicked(self, item, column: int) -> None:
        """Double-clicking an account's name opens its Account Details.

        Deferred a tick: the double-click is still being delivered, and the
        window answers with a modal dialog."""
        kind = item.data(COL_YEAR, ROW_ROLE)
        if column != COL_YEAR or not kind or kind[0] != "account":
            return
        account_id = int(kind[2])
        if retirement.is_planned(account_id):
            return
        defer(self, lambda: self.accountDetailsRequested.emit(account_id))

    # -- edits --------------------------------------------------------------
    def _on_item_changed(self, item, column: int) -> None:
        """A cell was edited: write it, then rebuild on the next tick.

        The DB write is safe inline; the REBUILD is not, because tearing the
        tree down inside the signal its own cell just emitted frees the editor
        Qt is still holding (CLAUDE.md's deferral rule).
        """
        if self._loading or item is None:
            return
        kind = item.data(COL_YEAR, ROW_ROLE)
        if not kind:
            return
        year = int(kind[1])
        if kind[0] == "year":
            if column == COL_CONVERSION:
                # NOT rebuilt here: this runs inside the edited item's own
                # itemChanged, and tearing the tree down under it is the native
                # crash CLAUDE.md warns about. The handler defers the rebuild.
                self.set_total_conversion(year, parse_amount(item.text(COL_CONVERSION)),
                                          rebuild=False)
            else:
                return
        elif column in (COL_CONVERSION, COL_TARGET):
            self._write_conversion(year, int(kind[2]),
                                   parse_amount(item.text(COL_CONVERSION)),
                                   item.data(COL_TARGET, TARGET_ROLE))
        else:
            return
        defer(self, self.reload)

    def _write_conversion(self, year: int, account_id: int, amount_cents: int,
                          target: Optional[int]) -> None:
        acct = self.account_for(account_id)
        if acct is None or acct.is_roth:
            return
        amount = abs(int(amount_cents))
        if amount <= 0:
            self.clear_conversions(acct.account_id, year)
            return
        if target is None:
            self.say(f"{acct.name} has no Roth owned by the same person, so there "
                     "is nowhere to convert into: a Roth conversion cannot cross "
                     "people (IRC 408A(d)(3)).")
            return
        # Clear BEFORE creating a planned Roth (see set_total_conversion).
        self.clear_conversions(acct.account_id, year, keep_target=target)
        target = self._materialize(acct.account_id, target)
        self.set_conversion(acct.account_id, target, year, amount)
        if self.unowned(acct):
            self.say(f"{acct.name} has no owner set, so where its conversion "
                     "lands is a guess. Double-click it in the schedule to open "
                     "Account Details and set the Owner.")

    # -- the focused year ---------------------------------------------------
    def focus_year(self, year: Optional[int]) -> None:
        """Select, expand and scroll to a year. The right-click's whole point."""
        self._focus_year = None if year is None else int(year)
        if self._focus_year is None:
            return
        item = self.year_item(self._focus_year)
        if item is None:
            return
        item.setExpanded(True)
        self._fill_accounts(item)
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item, QAbstractItemView.PositionAtTop)

    def focused_year(self) -> Optional[int]:
        item = self.tree.currentItem()
        if item is None:
            return None
        kind = item.data(COL_YEAR, ROW_ROLE)
        return int(kind[1]) if kind else None


INCOME_COLUMNS = ("Source", "Per year", "From", "To", "Change %/yr", "Taxable",
                  "Investment (NIIT)", "Capital-gains rates")
(IN_COL_NAME, IN_COL_AMOUNT, IN_COL_START, IN_COL_END, IN_COL_CHANGE,
 IN_COL_TAXABLE, IN_COL_NIIT, IN_COL_PREFERENTIAL) = range(8)


class IncomeSourcesTable(QWidget):
    """OTHER income: rentals, royalties, a pension - never the salary.

    One row per ``retirement.IncomeSource`` of kind 'other': an amount per year
    from a start year, changing by a percentage a year (0 for flat rent, -10 for
    royalties that fall off), through an optional end year, and whether it is
    taxable. The salary has its own form in :class:`IncomeDialog` (reported: one
    table holding both "tried to fit disparate things on the same row"). Plain
    cells rather than cell widgets (the conversion table's CPU lesson); every
    edit writes through ``mammon.retirement`` and the rebuild is deferred a tick
    (CLAUDE.md's deferral rule).
    """

    changed = pyqtSignal()
    said = pyqtSignal(str)

    def __init__(self, conn, parent=None, *, today: Optional[_dt.date] = None):
        super().__init__(parent)
        self.conn = conn
        self._today = today or _dt.date.today()
        self._loading = False
        self._ids: list[int] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add income", self)
        self.add_button.clicked.connect(self.add_source)
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.clicked.connect(self.remove_selected)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.table = QTableWidget(0, len(INCOME_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(INCOME_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(IN_COL_NAME, 200)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

    def reload(self) -> None:
        self._loading = True
        try:
            sources = [src for src in retirement.list_income_sources(self.conn)
                       if src.kind == "other"]
            self._ids = [src.id for src in sources]
            self.table.setRowCount(len(sources))
            for row, src in enumerate(sources):
                cells = {
                    IN_COL_NAME: src.name,
                    IN_COL_AMOUNT: fmt_cents(src.amount_cents),
                    IN_COL_START: str(src.start_year),
                    IN_COL_END: "" if src.end_year is None else str(src.end_year),
                    IN_COL_CHANGE: str(src.change_pct),
                }
                for col, text in cells.items():
                    self.table.setItem(row, col, QTableWidgetItem(text))
                taxable = QTableWidgetItem("")
                taxable.setFlags((taxable.flags() | Qt.ItemIsUserCheckable)
                                 & ~Qt.ItemIsEditable)
                taxable.setCheckState(Qt.Checked if src.taxable else Qt.Unchecked)
                self.table.setItem(row, IN_COL_TAXABLE, taxable)
                # Net investment income (IRC 1411): rent usually is.
                niit = QTableWidgetItem("")
                niit.setFlags((niit.flags() | Qt.ItemIsUserCheckable)
                              & ~Qt.ItemIsEditable)
                niit.setCheckState(Qt.Checked if src.niit else Qt.Unchecked)
                niit.setToolTip("Net investment income - rent, interest, "
                                "dividends - for the 3.8% tax above $250,000 of "
                                "income (joint). Rent is, unless you are a "
                                "real-estate professional.")
                self.table.setItem(row, IN_COL_NIIT, niit)
                # Qualified dividends and long-term gains (IRC 1(h)(11)) stack
                # on ordinary income at 0/15/20%, not the bracket rates.
                pref = QTableWidgetItem("")
                pref.setFlags((pref.flags() | Qt.ItemIsUserCheckable)
                              & ~Qt.ItemIsEditable)
                pref.setCheckState(Qt.Checked if src.preferential else Qt.Unchecked)
                pref.setToolTip("Taxed at the capital-gains rates (0/15/20%): "
                                "qualified dividends and long-term gains from a "
                                "fund. Interest, rent and a pension are not.")
                self.table.setItem(row, IN_COL_PREFERENTIAL, pref)
        finally:
            self._loading = False

    def add_source(self) -> int:
        source_id = retirement.add_income_source(
            self.conn, "New income", 0, self._today.year)
        self.reload()
        self.changed.emit()
        return source_id

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self._ids)):
            return
        retirement.delete_income_source(self.conn, self._ids[row])
        self.reload()
        self.changed.emit()

    def _on_item_changed(self, item) -> None:
        if self._loading or item is None:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(self._ids)):
            return
        text = item.text().strip()
        try:
            if col == IN_COL_NAME:
                if not text:
                    raise ValueError("an income source needs a name")
                fields = {"name": text}
            elif col == IN_COL_AMOUNT:
                fields = {"amount_cents": abs(parse_amount(text))}
            elif col == IN_COL_START:
                fields = {"start_year": int(text)}
            elif col == IN_COL_END:
                fields = {"end_year": int(text) if text else None}
            elif col == IN_COL_CHANGE:
                fields = {"change_pct": Decimal(text.rstrip("%") or "0")}
            elif col == IN_COL_TAXABLE:
                fields = {"taxable": item.checkState() == Qt.Checked}
            elif col == IN_COL_NIIT:
                fields = {"niit": item.checkState() == Qt.Checked}
            elif col == IN_COL_PREFERENTIAL:
                fields = {"preferential": item.checkState() == Qt.Checked}
            else:
                return
            retirement.update_income_source(self.conn, self._ids[row], **fields)
        except Exception as exc:  # a bad cell is a message, not a crash
            self.said.emit(f"That income entry was not saved: {exc}")
        defer(self, self.reload)
        self.changed.emit()


class SalaryForm(QGroupBox):
    """One earner's salary: gross pay, the years it runs, its raise, and the
    401(k) deferral and match with the account they go into.

    One per earner, because each retires on their own schedule (reported): the
    Through year ends that salary's contributions
    and turns the plan it funds into a former employer's the year after.
    Every field saves as it is left; a cleared amount deletes the salary.
    """

    changed = pyqtSignal()

    def __init__(self, conn, person: Optional[Mapping], parent=None, *,
                 today: _dt.date):
        who = (person or {}).get("name")
        super().__init__("", parent)
        self.source_name = f"Salary - {who}" if who else "Salary"
        self.setFlat(True)
        self.conn = conn
        self.person_id = int(person["id"]) if person else None
        self._today = today
        self._loading = False
        form = QFormLayout(self)
        self.amount = QLineEdit(self)
        self.amount.setPlaceholderText("gross, per year")
        self.start = NoWheelSpinBox(self)
        self.end = NoWheelSpinBox(self)
        for box in (self.start, self.end):
            box.setRange(today.year - 5, today.year + 60)
        self.raise_pct = QLineEdit(self)
        self.deferral = QLineEdit(self)
        self.match = QLineEdit(self)
        for edit in (self.raise_pct, self.deferral, self.match):
            edit.setPlaceholderText("0")
        self.into = NoWheelComboBox(self)
        self.into.addItem("(none)", None)
        for acct in plan_accounts(conn):
            if not acct.planned:
                self.into.addItem(acct.name, acct.account_id)
        form.addRow("Gross salary per year", self.amount)
        form.addRow("From", self.start)
        form.addRow("Through", self.end)
        form.addRow("Raise % per year", self.raise_pct)
        form.addRow("401(k) deferral %", self.deferral)
        form.addRow("Employer match %", self.match)
        form.addRow("Goes into", self.into)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        form.addRow(self.note)
        self.end.setToolTip("The last year this salary is earned: that earner's "
                            "retirement. Contributions stop after it, and the plan "
                            "it funds owes required minimums once it ends.")
        self.deferral.setToolTip(
            "Pre-tax unless it goes into a Roth account: it lowers taxable income "
            "and never pays the household's spending.")
        self.into.setToolTip(
            "The account the deferral and match go into. Its contributions come "
            "from this salary instead of a measured average.")
        for edit in (self.amount, self.raise_pct, self.deferral, self.match):
            edit.editingFinished.connect(self.save)
        for box in (self.start, self.end):
            box.editingFinished.connect(self.save)
        self.into.activated.connect(lambda _i: self.save())
        self.load()

    def summary(self) -> str:
        src = self.salary()
        if src is None:
            return "none"
        text = (f"{fmt_money(src.amount_cents)}/yr, {src.start_year}-"
                f"{src.end_year if src.end_year is not None else ''}")
        if src.deferral_pct or src.match_pct:
            into = next((self.into.itemText(i) for i in range(self.into.count())
                         if self.into.itemData(i) == src.into_account_id), "")
            text += (f", {src.deferral_pct}% + {src.match_pct}%"
                     + (f" into {into}" if into else ""))
        return text

    def salary(self):
        return retirement.salary_source(self.conn, self.person_id) \
            if self.person_id is not None else retirement.salary_source(self.conn)

    def load(self) -> None:
        self._loading = True
        try:
            salary = self.salary()
            fallback_end = (retirement_start_year(self.conn) or self._today.year + 1) - 1
            if salary is None:
                self.amount.setText("")
                self.start.setValue(self._today.year)
                self.end.setValue(max(self._today.year, fallback_end))
                for edit in (self.raise_pct, self.deferral, self.match):
                    edit.setText("")
                self.into.setCurrentIndex(0)
                return
            self.amount.setText(fmt_cents(salary.amount_cents))
            self.start.setValue(salary.start_year)
            self.end.setValue(salary.end_year if salary.end_year is not None
                              else max(self._today.year, fallback_end))
            self.raise_pct.setText(str(salary.change_pct) if salary.change_pct else "")
            self.deferral.setText(str(salary.deferral_pct) if salary.deferral_pct else "")
            self.match.setText(str(salary.match_pct) if salary.match_pct else "")
            self.into.setCurrentIndex(max(0, self.into.findData(salary.into_account_id)))
        finally:
            self._loading = False

    def save(self) -> None:
        """Create the salary on its first amount, delete it when the amount is
        cleared, update it otherwise."""
        if self._loading:
            return
        salary = self.salary()
        text = self.amount.text().strip()
        try:
            if not text or parse_amount(text) == 0:
                if salary is not None:
                    retirement.delete_income_source(self.conn, salary.id)
                    self.changed.emit()
                return
            fields = dict(
                amount_cents=abs(parse_amount(text)),
                start_year=self.start.value(),
                end_year=max(self.start.value(), self.end.value()),
                change_pct=Decimal(self.raise_pct.text().strip().rstrip("%") or "0"),
                deferral_pct=Decimal(self.deferral.text().strip().rstrip("%") or "0"),
                match_pct=Decimal(self.match.text().strip().rstrip("%") or "0"),
                into_account_id=self.into.currentData(),
                person_id=self.person_id,
            )
            if salary is None:
                source_id = retirement.add_income_source(
                    self.conn, self.source_name, fields["amount_cents"],
                    fields["start_year"], kind="salary")
                retirement.update_income_source(self.conn, source_id, **fields)
            else:
                if all(getattr(salary, key) == value for key, value in fields.items()):
                    return
                retirement.update_income_source(self.conn, salary.id, **fields)
            self.note.setText("")
            self.changed.emit()
        except (InvalidOperation, ValueError) as exc:
            self.note.setText(f"The salary was not saved: {exc}")


class ScheduleWindow(QDialog):
    """A schedule in its own MODELESS window, beside the page rather than in it.

    Reported: the embedded schedules crowded the page. Modeless - ``show()``,
    never ``exec_()`` - so the charts stay live while it is open (a click on a
    bracket line still fills a year the window then shows), and there is no
    modal to block a headless test. The editor keeps its page as its logical
    owner (its signals are the page's to wire); this window only frames it,
    with its own copy of the notice line so a refusal is read where it happens.
    """

    def __init__(self, title: str, editor: QWidget, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        layout = QVBoxLayout(self)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.PlainText)
        layout.addWidget(editor, 1)
        layout.addWidget(self.notice)
        editor.said.connect(self.notice.setText)
        self.editor = editor
        self.resize(900, 560)

    def open_on(self, year: Optional[int] = None) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        if year is not None:
            self.editor.focus_year(int(year))


class SimpleIncomeForm(QGroupBox):
    """A pension (one per earner) or the household's investment income: an
    amount per year, when it starts, how fast it rises, and whether it is
    taxable. Each is its own kind of source (db._V93; reported: "we are
    missing interest and dividends ... we are missing pension income"). Saves
    as it is left; a cleared amount deletes it.
    """

    changed = pyqtSignal()

    def __init__(self, conn, kind: str, title: str, person: Optional[Mapping],
                 parent=None, *, today: _dt.date, rise_label: str,
                 estimate: bool = False):
        super().__init__("", parent)
        self.source_name = title
        self.setFlat(True)
        self.conn = conn
        self.kind = kind
        self.person_id = int(person["id"]) if person else None
        self._today = today
        self._loading = False
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.amount = QLineEdit(self)
        self.amount.setPlaceholderText("per year")
        self.amount.setMaximumWidth(110)
        amount_row = QHBoxLayout()
        amount_row.addWidget(self.amount)
        if estimate:
            self.estimate_button = QPushButton("Estimate from last 12 months", self)
            self.estimate_button.setToolTip(
                "Dividends, capital-gain distributions and interest paid by the "
                "taxable investment accounts over the last year.")
            self.estimate_button.clicked.connect(self._estimate)
            amount_row.addWidget(self.estimate_button)
        form.addRow("Amount per year", amount_row)
        self.start = NoWheelSpinBox(self)
        self.start.setRange(today.year - 5, today.year + 60)
        self.start.setValue(today.year)
        form.addRow("Starts", self.start)
        self.rise = QLineEdit(self)
        self.rise.setPlaceholderText("0")
        self.rise.setMaximumWidth(50)
        form.addRow(rise_label, self.rise)
        self.taxable = QCheckBox("Taxable", self)
        self.taxable.setChecked(True)
        form.addRow("", self.taxable)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        form.addRow(self.note)
        for edit in (self.amount, self.rise):
            edit.editingFinished.connect(self.save)
        self.start.editingFinished.connect(self.save)
        self.taxable.toggled.connect(lambda _on: self.save())
        self.load()

    def source(self):
        return retirement.salary_source(self.conn, self.person_id, kind=self.kind)

    def summary(self) -> str:
        src = self.source()
        if src is None:
            return "none"
        rise = f", rising {src.change_pct}%" if src.change_pct else ""
        return f"{fmt_money(src.amount_cents)}/yr from {src.start_year}{rise}"

    def load(self) -> None:
        self._loading = True
        try:
            src = self.source()
            self.amount.setText(fmt_cents(src.amount_cents) if src else "")
            self.start.setValue(src.start_year if src else self._today.year)
            self.rise.setText(str(src.change_pct) if src and src.change_pct else "")
            self.taxable.setChecked(src.taxable if src else True)
        finally:
            self._loading = False

    def _estimate(self) -> None:
        cents = retirement.trailing_investment_income_cents(
            self.conn, self._today.isoformat())
        self.amount.setText(fmt_cents(cents))
        self.note.setText(f"{fmt_money(cents)} over the last 12 months. Bank "
                          f"interest recorded in checking or savings is not "
                          f"included; add it here if it matters.")
        self.save()

    def save(self) -> None:
        if self._loading:
            return
        src = self.source()
        text = self.amount.text().strip()
        try:
            if not text or parse_amount(text) == 0:
                if src is not None:
                    retirement.delete_income_source(self.conn, src.id)
                    self.changed.emit()
                return
            fields = dict(amount_cents=abs(parse_amount(text)),
                          start_year=self.start.value(),
                          change_pct=Decimal(self.rise.text().strip().rstrip("%") or "0"),
                          taxable=self.taxable.isChecked(),
                          person_id=self.person_id)
            if src is None:
                source_id = retirement.add_income_source(
                    self.conn, self.source_name, fields["amount_cents"],
                    fields["start_year"], kind=self.kind)
                retirement.update_income_source(self.conn, source_id, **fields)
            else:
                if all(getattr(src, key) == value for key, value in fields.items()):
                    return
                retirement.update_income_source(self.conn, src.id, **fields)
            self.changed.emit()
        except (InvalidOperation, ValueError) as exc:
            self.note.setText(f"Not saved: {exc}")


class CollapsibleSection(QWidget):
    """One line - an arrow, a title and a summary - that opens to its form.

    Reported: the Income dialog grew taller than the screen with every section
    open, so each is a single line until clicked. ``refresh`` re-reads the
    summary after an edit."""

    def __init__(self, title: str, content: QWidget, summary, parent=None):
        super().__init__(parent)
        self._title, self._summary = title, summary
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.header = QToolButton(self)
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.RightArrow)
        self.header.setAutoRaise(True)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.toggled.connect(self.set_open)
        layout.addWidget(self.header)
        self.content = content
        content.setParent(self)
        content.setVisible(False)
        layout.addWidget(content)
        self.refresh()

    def set_open(self, on: bool) -> None:
        self.header.setChecked(bool(on))
        self.header.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.content.setVisible(bool(on))

    def is_open(self) -> bool:
        return self.header.isChecked()

    def refresh(self) -> None:
        self.header.setText(f"{self._title}:  {self._summary()}")


class IncomeDialog(QDialog):
    """Income besides Social Security: each earner's salary, then other income.

    A salary is a FORM (:class:`SalaryForm`), one per earner - the self person
    and a spouse - because each is one thing with several parts and retires on
    its own schedule; other income is the table of sources. Reported: the
    embedded panel "tried to fit disparate things on the same row", and did not
    belong in the page. Every field saves as it is left; the page re-applies the
    plan once, when the dialog closes (``changed_anything``).

    Built here and RUN by the page's ``_run_dialog`` seam, never exec_()-ed
    inline (CLAUDE.md's headless-modal hazard).
    """

    changed = pyqtSignal()

    def __init__(self, conn, parent=None, *, today: Optional[_dt.date] = None):
        super().__init__(parent)
        self.conn = conn
        self._today = today or _dt.date.today()
        self.changed_anything = False
        self.setWindowTitle("Income")
        layout = QVBoxLayout(self)

        earners = [p for p in retirement.list_people(conn)
                   if p.get("relationship") in ("self", "spouse")] or [None]
        self.salaries: list[SalaryForm] = []
        self.pensions: list[SimpleIncomeForm] = []
        self.sections: list[CollapsibleSection] = []

        def section(title, form, summary):
            part = CollapsibleSection(title, form, summary, self)
            form.changed.connect(part.refresh)
            form.changed.connect(self._note_change)
            self.sections.append(part)
            layout.addWidget(part)
            return part

        for person in earners:
            who = (person or {}).get("name")
            form = SalaryForm(conn, person, self, today=self._today)
            self.salaries.append(form)
            section(form.source_name, form, form.summary)
            pension = SimpleIncomeForm(
                conn, "pension", f"Pension - {who}" if who else "Pension", person,
                self, today=self._today, rise_label="COLA % per year")
            self.pensions.append(pension)
            section(pension.source_name, pension, pension.summary)
        self.investment = SimpleIncomeForm(
            conn, "investment",
            "Investment income (interest and dividends outside retirement accounts)",
            None, self, today=self._today, rise_label="Change % per year",
            estimate=True)
        section(self.investment.source_name, self.investment, self.investment.summary)

        self.other = IncomeSourcesTable(conn, self, today=self._today)
        self.other.said.connect(self.salaries[0].note.setText)
        self.other.reload()

        def other_summary():
            found = [src for src in retirement.list_income_sources(conn)
                     if src.kind == "other"]
            if not found:
                return "none"
            year = self._today.year
            return (f"{len(found)} source(s), {fmt_money(sum(src.cents_in(year) for src in found))}"
                    f" this year")
        section("Other income", self.other, other_summary)
        layout.addStretch(1)

        close = QPushButton("Close", self)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.resize(760, 440)

    def salary_for(self, person_id: Optional[int]) -> SalaryForm:
        return next(f for f in self.salaries if f.person_id == person_id)

    def _note_change(self) -> None:
        self.changed_anything = True
        self.changed.emit()


#: The Spending plan's fine print, in the words of the Investment Dashboard's
#: projection disclaimer (``investment_dashboard.PROJECTION_DISCLAIMER``).
PLAN_DISCLAIMER = (
    "Disclaimer: these amounts come from projections of estimated future "
    "returns, taxes and benefits based on models and assumptions, but no model "
    "can predict the actual future.")

WITHDRAWAL_FIXED_COLUMNS = ("Year", "Age")

WD_COL_YEAR, WD_COL_AGE = range(2)


class WithdrawalSchedule(_PlanEditor, HouseholdEngine):
    """The distributions themselves: a year down the side, an account across.

    A grid rather than the Roth schedule's tall list, because the question the
    user asks here - "what am I taking out, and does it last?" - is read across
    accounts within a year and down years within an account, and a one-row-per
    pair list answers neither without scrolling.

    Every cell is bounded on both sides and the bounds are not symmetrical. The
    FLOOR is law: IRC 401(a)(9) sets what must come out of a tax-deferred
    account once the owner reaches the applicable age, and a plan that showed
    less would be a plan of a penalty. The CEILING is arithmetic: an account
    cannot pay out more than it is projected to still hold, and it comes from
    the same projection the page draws behind the bars, so the table and the
    chart cannot disagree about what is left.

    The per-year control row exists because the floor alone is not a plan. What
    it sets is a HOUSEHOLD amount, not an account's: a person retires, not an IRA,
    and asking which of four accounts a year's spending comes from is asking
    them to do the arithmetic themselves. It used to ask exactly that, and then
    refused amounts the household could easily fund because it measured them
    against one account's balance while the rest sat untouched. Now one starting
    amount and one annual increase are split across the whole pool in proportion
    to each account's projected balance, required minimums first; a plan the
    pool cannot carry is written anyway and the notice says when it runs out. See :func:`mammon.retirement.plan_household_withdrawals`
    for the allocation itself, which is domain arithmetic and lives there.
    """

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        self._columns: list[PlanAccount] = []
        self._plan_restored = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # A labeled SECTION, not a row. It was one crowded line of unlabeled
        # boxes (reported: the yearly increase was "unlabeled or invisible").
        # Before that it hid behind a CHECKABLE button Qt drew as a wide
        # highlighted bar; the plan's few numbers need no disclosure.
        plan_box = QGroupBox("Spending plan", self)
        plan_layout = QVBoxLayout(plan_box)
        self.per_year_label = QLabel(
            "Enter what the household will spend each year - including giving, "
            "but not income tax - from the year you retire. Social Security and "
            "other income pay part of it; the rest, plus the estimated federal "
            "income tax, is drawn from the retirement accounts. Apply writes "
            "every year below; type in a cell to change one year.", plan_box)
        self.per_year_label.setWordWrap(True)
        plan_layout.addWidget(self.per_year_label)
        # Left: the plan's fields, each only as wide as its value, with Apply
        # directly under them. Right: the ending reserve and the button that
        # maximizes spending against it (reported layout).
        columns = QHBoxLayout()
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.per_year_amount = QLineEdit(plan_box)
        self.per_year_amount.setPlaceholderText("120,000")
        self.per_year_amount.setMaximumWidth(110)
        self.per_year_amount.returnPressed.connect(self._on_apply_per_year)
        # The other end of the budget seam (SRD 5.12i). What a household spends
        # is the one number this whole page turns on, and it is the number the
        # Budget Planner already holds; typing it twice is how the two drift.
        # This button only FILLS the box beside it - a copy of a figure, not a
        # link - and Apply is still the only thing that writes a plan.
        amount_row = QHBoxLayout()
        amount_row.setContentsMargins(0, 0, 0, 0)
        amount_row.addWidget(self.per_year_amount)
        self.from_budget = QPushButton("From budget...", plan_box)
        self.from_budget.setToolTip(
            "Work this amount out from a budget: its yearly total, less each "
            "line that stops at retirement, each with the evidence that picked "
            "it. It fills the box beside this button and nothing else.")
        self.from_budget.clicked.connect(self._on_from_budget)
        amount_row.addWidget(self.from_budget)
        amount_row.addStretch(1)
        form.addRow("Spending per year, to start", amount_row)
        self.per_year_increase = QLineEdit(plan_box)
        self.per_year_increase.setPlaceholderText("3")
        self.per_year_increase.setMaximumWidth(50)
        self.per_year_increase.setToolTip(
            "How much the spending amount rises each year, so it keeps up with "
            "prices.")
        self.per_year_increase.returnPressed.connect(self._on_apply_per_year)
        form.addRow("Increase % per year", self.per_year_increase)
        # When spending starts: retiring before Social Security starts is the
        # common case, so it is its own number rather than "this year".
        self.per_year_start = NoWheelSpinBox(plan_box)
        self.per_year_start.setRange(self._today.year, self._today.year + 60)
        self.per_year_start.setValue(self._today.year)
        self.per_year_start.setToolTip(
            "The first year the household lives on this amount. Before it, only "
            "required minimums are drawn.")
        form.addRow("Starting year", self.per_year_start)
        self.per_year_bracket = NoWheelComboBox(plan_box)
        self.per_year_bracket.addItem("no bracket target", None)
        for bracket in retirement.tax_brackets("joint"):
            if bracket.upper_cents is not None:
                self.per_year_bracket.addItem(bracket.rate_label,
                                              bracket.rate_label.rstrip("%"))
        self.per_year_bracket.setToolTip(
            "Draw from IRAs and 401(k)s only up to the top of this bracket "
            "(after the standard deduction, Social Security, other income and "
            "planned conversions), and take the rest of the year's need from "
            "Roth. Required minimums are drawn regardless.")
        form.addRow("Keep IRA draws within", self.per_year_bracket)
        # Reported: paid from the IRA, the tax used bracket room a conversion
        # could have filled; paid from a taxable account it realizes gains.
        self.tax_from = NoWheelComboBox(plan_box)
        self.tax_from.addItem("the spending order", "spending")
        self.tax_from.addItem("taxable accounts first", "taxable")
        self.tax_from.setToolTip(
            "Where the year's income tax comes from. Taxable accounts first keeps "
            "IRA bracket room for conversions; selling there realizes capital "
            "gains, taxed at their own rates.")
        self.tax_from.setCurrentIndex(
            max(0, self.tax_from.findData(retirement.get_tax_paid_from(self.conn))))
        self.tax_from.currentIndexChanged.connect(self._on_tax_from)
        form.addRow("Pay income tax from", self.tax_from)
        self.per_year_apply = QPushButton("Apply to every year", plan_box)
        self.per_year_apply.clicked.connect(self._on_apply_per_year)
        form.addRow("", self.per_year_apply)
        columns.addLayout(form)
        columns.addStretch(1)

        right = QVBoxLayout()
        right.addWidget(QLabel("Ending reserve", plan_box))
        self.reserve_amount = QLineEdit(plan_box)
        self.reserve_amount.setPlaceholderText("0")
        self.reserve_amount.setMaximumWidth(120)
        self.reserve_amount.setToolTip(
            "What the retirement accounts should still hold at the end of the "
            "last year shown, in that year's dollars.")
        right.addWidget(self.reserve_amount)
        self.most_button = QPushButton("Maximize Spending Rate", plan_box)
        self.most_button.setToolTip(
            "Find the largest starting amount - with this increase, start year "
            "and bracket target - that lasts through the age shown and leaves "
            "the ending reserve, and apply it.")
        self.most_button.clicked.connect(self._on_most_we_can_spend)
        right.addWidget(self.most_button)
        right.addStretch(1)
        columns.addLayout(right)
        plan_layout.addLayout(columns)
        # Where a figure taken from a budget came from, in words, computed from
        # the basis itself and never hardcoded. Empty - and hidden - until one
        # arrives, because an unexplained provenance line is noise.
        self.basis_provenance = QLabel("", plan_box)
        self.basis_provenance.setWordWrap(True)
        self.basis_provenance.setVisible(False)
        plan_layout.addWidget(self.basis_provenance)
        plan_layout.addLayout(self._build_sources(plan_box))
        # Fine print under both buttons, as the Investment Dashboard's projection
        # carries: a spending rate found by a model reads as a promise unless
        # it says it is not one (reported).
        self.disclaimer = QLabel(PLAN_DISCLAIMER, plan_box)
        self.disclaimer.setWordWrap(True)
        font = self.disclaimer.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.85))
        font.setItalic(True)
        self.disclaimer.setFont(font)
        plan_layout.addWidget(self.disclaimer)
        layout.addWidget(plan_box)

        self.table = QTableWidget(0, len(WITHDRAWAL_FIXED_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(WITHDRAWAL_FIXED_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

    #: The most checkboxes per row in the Draw from grid; fewer when the
    #: names do not fit the width (re-laid out on resize).
    SOURCE_COLUMNS = 4

    def _build_sources(self, parent) -> QHBoxLayout:
        """The spending order and the accounts the plan may draw from, side by
        side. Both save as soon as they change and re-apply the plan
        (reported: make the order adjustable, and let an account be left out -
        selling appreciated stock in the year the tax is due is a choice).

        The order moves with Up and Down buttons, not by dragging: a dragged
        row that never reached the save left the order unchangeable (reported).
        The accounts are a grid of checkboxes, several to a row - one row per
        account took a line for every account (reported)."""
        row = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("Spending order", parent))
        fixed = QLabel("First: required minimums, always", parent)
        fixed.setEnabled(False)
        left.addWidget(fixed)
        steps = QHBoxLayout()
        self.order_list = QListWidget(parent)
        self.order_list.setToolTip(
            "Where each year's spending and income tax come from, top first, "
            "after required minimums. Select a row and move it. Without a "
            "bracket target, the two IRA rows are one.")
        self.order_list.setMaximumHeight(90)
        steps.addWidget(self.order_list, 1)
        moves = QVBoxLayout()
        self.order_up = QToolButton(parent)
        self.order_up.setArrowType(Qt.UpArrow)
        self.order_up.setToolTip("Move the selected source earlier")
        self.order_up.clicked.connect(lambda: self.move_step(-1))
        self.order_down = QToolButton(parent)
        self.order_down.setArrowType(Qt.DownArrow)
        self.order_down.setToolTip("Move the selected source later")
        self.order_down.clicked.connect(lambda: self.move_step(1))
        moves.addWidget(self.order_up)
        moves.addWidget(self.order_down)
        moves.addStretch(1)
        steps.addLayout(moves)
        left.addLayout(steps)
        row.addLayout(left, 2)

        right = QVBoxLayout()
        right.addWidget(QLabel("Draw from", parent))
        self.source_grid = QWidget(parent)
        self.source_grid.setToolTip(
            "Unchecked accounts are never drawn for spending or tax - a taxable "
            "account whose sale would realize a gain, say. An IRA or 401(k) "
            "still pays its required minimum.")
        self.source_layout = QGridLayout(self.source_grid)
        self.source_layout.setContentsMargins(0, 0, 0, 0)
        self.source_layout.setHorizontalSpacing(12)
        self.source_layout.setVerticalSpacing(2)
        self.source_boxes: dict[int, QCheckBox] = {}
        scroll = QScrollArea(parent)
        scroll.setWidget(self.source_grid)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(110)
        scroll.viewport().installEventFilter(self)
        self.source_scroll = scroll
        right.addWidget(scroll)
        row.addLayout(right, 5)
        return row

    def _load_sources(self) -> None:
        current = self.order_list.currentItem()
        keep = current.data(Qt.UserRole) if current is not None else None
        self.order_list.clear()
        for step in retirement.get_spending_order(self.conn):
            item = QListWidgetItem(retirement.SPENDING_STEP_LABELS[step])
            item.setData(Qt.UserRole, step)
            self.order_list.addItem(item)
            if step == keep:
                self.order_list.setCurrentItem(item)
        exempt = retirement.spending_exempt_ids(self.conn)
        for box in self.source_boxes.values():
            self.source_layout.removeWidget(box)
            box.deleteLater()
        self.source_boxes = {}
        real = [a for a in self.accounts() if not a.planned
                and self.balance_cents(a.account_id) > 0]
        for index, acct in enumerate(real):
            box = QCheckBox(acct.name, self.source_grid)
            box.setChecked(acct.account_id not in exempt)
            box.toggled.connect(
                lambda on, aid=int(acct.account_id): self.set_source(aid, on))
            self.source_boxes[int(acct.account_id)] = box
        self._lay_out_sources()

    def source_columns(self) -> int:
        """The most columns, up to SOURCE_COLUMNS, whose widths fit - each
        column as wide as ITS widest name, so one long name does not force
        every column to its width."""
        widths = [box.sizeHint().width() for box in self.source_boxes.values()]
        if not widths:
            return 1
        room = self.source_scroll.viewport().width()
        spacing = self.source_layout.horizontalSpacing()
        for columns in range(min(self.SOURCE_COLUMNS, len(widths)), 1, -1):
            need = sum(max(widths[c::columns]) for c in range(columns))                 + spacing * (columns - 1)
            if need <= room:
                return columns
        return 1

    def _lay_out_sources(self) -> None:
        columns = self.source_columns()
        for box in self.source_boxes.values():
            self.source_layout.removeWidget(box)
        for index, box in enumerate(self.source_boxes.values()):
            self.source_layout.addWidget(box, index // columns, index % columns)
        self._source_columns = columns

    def eventFilter(self, watched, event):   # noqa: N802 (Qt's name)
        scroll = getattr(self, "source_scroll", None)
        if (scroll is not None and watched is scroll.viewport()
                and event.type() == QEvent.Resize
                and self.source_columns() != getattr(self, "_source_columns", 0)):
            self._lay_out_sources()
        return super().eventFilter(watched, event)

    def move_step(self, delta: int) -> None:
        """Move the selected spending step ``delta`` rows, save, re-apply."""
        row = self.order_list.currentRow()
        if row < 0:
            return
        order = [self.order_list.item(i).data(Qt.UserRole)
                 for i in range(self.order_list.count())]
        to = max(0, min(len(order) - 1, row + int(delta)))
        if to == row:
            return
        order.insert(to, order.pop(row))
        retirement.set_spending_order(self.conn, order)
        self._load_sources()
        self._sources_changed()

    def _on_tax_from(self, _index: int) -> None:
        retirement.set_tax_paid_from(self.conn, str(self.tax_from.currentData()))
        self._sources_changed()

    def set_source(self, account_id: int, drawn: bool) -> None:
        retirement.set_spending_exempt(self.conn, int(account_id), not drawn)
        self._sources_changed()

    def _sources_changed(self) -> None:
        """Re-apply the stored plan with the new order or sources, once, on the
        next tick (out of the list's own signal)."""
        if getattr(self, "_sources_pending", False):
            return
        self._sources_pending = True

        def reapply():
            self._sources_pending = False
            params = retirement.get_withdrawal_plan(self.conn)
            if params.is_set:
                self.apply_household(params.start_cents, params.increase_pct,
                                     start_year=params.start_year,
                                     bracket_rate=params.bracket_rate)

        defer(self, reapply)

    def _load_plan_params(self) -> None:
        """Put the stored household plan back in the boxes.

        The two numbers are an intent, and a user reopening the page should see
        what they asked for rather than have to read it back out of sixty rows
        of derived figures.
        """
        params = retirement.get_withdrawal_plan(self.conn)
        if not params.is_set:
            return
        self.per_year_amount.setText(fmt_cents(params.start_cents))
        self.per_year_increase.setText(str(params.increase_pct))
        if params.start_year is not None:
            self.per_year_start.setValue(int(params.start_year))
        at = self.per_year_bracket.findData(params.bracket_rate)
        self.per_year_bracket.setCurrentIndex(max(at, 0))














    def _net_roth_draws_against_conversions(self, amounts: dict, accounts,
                                            plan=None, *,
                                            from_year: Optional[int] = None) -> list[int]:
        """Never draw from a Roth in a year that also converts INTO one.

        Reported: some years drew from the Roth while the same years converted
        into it. Converting C and drawing R
        from the Roth is, to the cent, the same tax and the same balances as
        converting C - R and drawing R from the IRA, so spending comes first:
        the conversion shrinks by the Roth draw, the converting accounts pay
        that amount as an ordinary draw, and the Roth draw goes away. Edits
        ``amounts`` (account -> year -> cents) in place and rewrites the
        conversions; returns the years it changed.
        """
        roth_ids = [a.account_id for a in accounts if a.is_roth]
        changed: list[int] = []
        # Years before ``from_year`` are left exactly as stored (see
        # apply_household): a fill never reaches back.
        years = sorted({int(y) for by_year in amounts.values() for y in by_year
                        if from_year is None or int(y) >= int(from_year)})
        held = ({entry.year: entry.balances for entry in plan.years}
                if plan is not None else {})
        for year in years:
            # First, a conversion can only move what its source still HOLDS in
            # the plan that year, after that year's draw. One larger - planned
            # against a projection that still showed money in an IRA the plan
            # had already emptied - is cut back to what is there. Netted instead,
            # it became IRA draws from empty accounts (reported: IRA draws for
            # years after the IRAs ran out).
            if year in held:
                room: dict[int, int] = {}
                for row in retirement.list_conversions(self.conn, year):
                    src, target = int(row["from_account_id"]), int(row["to_account_id"])
                    if retirement.is_planned(src):
                        continue
                    if src not in room:
                        room[src] = max(0, int(held[year].get(src, 0))
                                        - int(amounts.get(src, {}).get(year, 0)))
                    amount = int(row["amount_cents"])
                    fits = min(amount, room[src])
                    room[src] -= fits
                    if fits < amount:
                        if fits > 0:
                            retirement.set_conversion(self.conn, src, target, year, fits)
                        else:
                            retirement.delete_conversion(self.conn, src, target, year)
                        if year not in changed:
                            changed.append(year)
            drawn = {aid: amounts.get(aid, {}).get(year, 0) for aid in roth_ids}
            rows = [r for r in retirement.list_conversions(self.conn, year)
                    if not retirement.is_planned(int(r["from_account_id"]))]
            converted = sum(int(r["amount_cents"]) for r in rows)
            shift = min(sum(drawn.values()), converted)
            if shift <= 0:
                continue
            cut = retirement.apportion_cents(
                shift, {i: int(r["amount_cents"]) for i, r in enumerate(rows)})
            for i, row in enumerate(rows):
                source, target = int(row["from_account_id"]), int(row["to_account_id"])
                left = int(row["amount_cents"]) - cut.get(i, 0)
                if left > 0:
                    retirement.set_conversion(self.conn, source, target, year, left)
                else:
                    retirement.delete_conversion(self.conn, source, target, year)
                by_year = amounts.setdefault(source, {})
                by_year[year] = by_year.get(year, 0) + cut.get(i, 0)
            for aid, cents in retirement.apportion_cents(shift, drawn).items():
                amounts[aid][year] = drawn[aid] - cents
            changed.append(year)
        changed = sorted(set(changed))
        if changed:
            self.invalidate_plan()
        return changed





    def apply_household(self, start_cents: int, increase_pct=0, *,
                        start_year: Optional[int] = None,
                        bracket_rate: Optional[str] = None,
                        from_year: Optional[int] = None) -> bool:
        """Spread one household amount across the whole pool, every year. True if written.

        ``start_cents`` is the household's SPENDING need in ``start_year`` and
        ``increase_pct`` raises it every year after, so a plan keeps up with
        prices instead of quietly shrinking. Social Security and other income
        pay part of it; only the rest is drawn. With ``bracket_rate`` ("22")
        IRA and 401(k) draws stop at that bracket's top and Roth pays the rest. The split, the required-minimum floors and the
        redistribution when an account empties are all
        :func:`retirement.plan_household_withdrawals`; this method supplies the
        balances, the floors and the growth, writes the result through
        :func:`retirement.set_withdrawal`, and reports.

        Running out is never a refusal. A single account running dry is the
        plan switching to the accounts that still have money; the whole pool
        running dry is written too - each later year takes what is left - and
        the notice says "You run out at age X (YYYY)", so the chart shows how
        long the money lasts instead of refusing to draw it.
        """
        wanted = abs(int(start_cents))
        years = self.apply_years()
        if not years:
            self.say("There are no plan years to apply a withdrawal to yet.")
            return False
        if wanted <= 0:
            self.say("Enter a starting amount for the household to withdraw "
                     "each year.")
            return False
        try:
            pct = Decimal(str(increase_pct or 0))
        except (InvalidOperation, ValueError):
            self.say("The yearly increase has to be a percentage, like 2.5.")
            return False
        accounts = self.accounts()
        if not accounts:
            self.say("There are no retirement accounts to withdraw from yet.")
            return False

        plan = self.plan_for(wanted, pct, start_year=start_year,
                             bracket_rate=bracket_rate)

        rise = f", rising {pct}% a year," if pct else ""
        # A plan that runs dry is still a plan: it is written and drawn, each
        # year taking what is left, and the notice says when the money runs
        # out. Reported: refusing it (first at the longest case, then at the
        # case on screen) hid the very picture that answers "how long does
        # this last?".

        amounts = plan.amounts_by_account()
        netted = self._net_roth_draws_against_conversions(amounts, accounts, plan,
                                                          from_year=from_year)
        # The netting rewrites CONVERSIONS, and a plan's draws - the tax a
        # conversion causes above all - were computed for the conversions as
        # they stood. Writing those draws beside the rewritten conversions left
        # the stored plan inconsistent, so the next re-apply of an unchanged
        # plan changed it again (reported: "Re-applying the plan should
        # produce exactly the same results"). Re-plan until the conversions
        # hold still, and write the draws of THAT plan.
        all_netted = list(netted)
        for _round in range(8):
            if not netted:
                break
            plan = self.plan_for(wanted, pct, start_year=start_year,
                                 bracket_rate=bracket_rate)
            amounts = plan.amounts_by_account()
            netted = self._net_roth_draws_against_conversions(
                amounts, accounts, plan, from_year=from_year)
            all_netted += [y for y in netted if y not in all_netted]
        netted = sorted(all_netted)
        live_keys = {a.account_id for a in retirement.spending_accounts(self.conn)}
        for account_id, by_year in amounts.items():
            if int(account_id) not in live_keys:
                continue                # a planned Roth pruned with its last conversion
            for year, cents in by_year.items():
                # ``from_year``: a fill of one year re-applies the plan to
                # measure that year, and must leave every EARLIER year exactly
                # as stored - draws and conversions alike (reported: filling
                # 2031 cut 2027's and 2030's conversions; "no years before
                # that year should be affected at all").
                if from_year is not None and int(year) < int(from_year):
                    continue
                retirement.set_withdrawal(self.conn, account_id, year, cents)
        # The plan owns every year through its horizon; anything stored past it
        # was written under an older, longer horizon and would otherwise sit in
        # the schedule (and the dashboard's projection) forever.
        retirement.delete_withdrawals_after(self.conn, years[-1])
        retirement.set_withdrawal_plan(self.conn, wanted, pct,
                                       start_year=start_year,
                                       bracket_rate=bracket_rate)
        self.invalidate_plan()
        raised = plan.floor_raised_years
        note = (f" {len(raised)} year(s) take more, starting {raised[0]}, "
                f"because required minimums already exceed that total."
                if raised else "")
        runs_out = ""
        if not plan.lasts:
            born = min((int(p["birth_year"]) for p in horizon_people(self.conn)),
                       default=None)
            when = (f"age {plan.depleted_year - born} ({plan.depleted_year})"
                    if born is not None else str(plan.depleted_year))
            runs_out = f" You run out at {when}."
        first = years[0] if start_year is None else int(start_year)
        within = (f" IRA draws stay within the {bracket_rate}% bracket; Roth "
                  f"pays the rest." if bracket_rate else "")
        fined = [y.year for y in plan.years if y.penalty_cents]
        early = (f" {len(fined)} year(s) draw from an IRA or 401(k) before 59 1/2 "
                 f"and pay the 10% additional tax (IRC 72(t)), starting {fined[0]}."
                 if fined else "")
        self.say(
            f"The household is set to live on {fmt_money(wanted)}{rise} from "
            f"{first} through {years[-1]}, with Social Security and other income "
            f"paying part of it and {len(accounts)} retirement account(s) the "
            f"rest, plus the estimated federal income tax.{within}{note}{early}{runs_out}"
            + (f" Planned conversions were reduced in "
               f"{', '.join(str(y) for y in netted)}: to what the account still "
               f"held, or so the IRA pays what the Roth would have (same tax, "
               f"same balances)." if netted else "")
        )
        self.changed.emit()
        return True

    def _on_most_we_can_spend(self) -> None:
        pct = self.per_year_increase.text().strip().rstrip("%") or "0"
        try:
            Decimal(pct)
        except InvalidOperation:
            self.say("The yearly increase has to be a percentage, like 2.5.")
            return
        try:
            reserve = abs(parse_amount(self.reserve_amount.text() or "0"))
        except ValueError:
            self.say("The ending reserve has to be an amount, like 500,000.")
            return
        best = self.most_we_can_spend(pct, start_year=self.per_year_start.value(),
                                      bracket_rate=self.per_year_bracket.currentData(),
                                      reserve_cents=reserve)
        end = self._years[-1] if self._years else ""
        if best <= 0:
            self.say(f"No spending amount lasts through {end} and leaves "
                     f"{fmt_money(reserve)} with these settings.")
            return
        self.per_year_amount.setText(fmt_cents(best))
        self._on_apply_per_year()
        left = (f", leaving at least {fmt_money(reserve)} at the end of {end}"
                if reserve else "")
        self.say(f"The most the household can spend and last through {end}"
                 f"{left} is about {fmt_money(best)} a year to start. "
                 + getattr(self, "last_notice", ""))

    def say(self, text: str) -> None:
        self.last_notice = text
        super().say(text)

    def _on_apply_per_year(self) -> None:
        self.apply_household(parse_amount(self.per_year_amount.text()),
                             self.per_year_increase.text().strip().rstrip("%"),
                             start_year=self.per_year_start.value(),
                             bracket_rate=self.per_year_bracket.currentData())
        defer(self, self.reload)

    # -- the budget seam (SRD 5.12i) ----------------------------------------
    def budget_basis_dialog(self) -> BudgetBasisDialog:
        """Build the budget's subtraction table. Construction ONLY - see
        :meth:`_run_dialog`.

        The starting year travels with it as EVIDENCE, not as a setting the
        budget side keeps: it is what a loan's payoff year is compared against,
        so a mortgage that clears before then can cite its own schedule. Nothing
        indexed crosses - the figure coming back is measured dollars in a stated
        base year, and this page applies its own increase rate to it.

        ``as_of`` is this page's own today rather than the clock the report would
        otherwise reach for, so the twelve-month window is the one the rest of
        the page is reasoning about. No budget id is passed: the household's
        ACTIVE plan is what the report reads, because this page holds no notion
        of which budget is in view.
        """
        return BudgetBasisDialog(self.conn, as_of=self._today.isoformat(),
                                 retirement_year=self.per_year_start.value(),
                                 ok_text="Use this figure", parent=self)

    def _run_dialog(self, dialog) -> bool:
        """Show a dialog modally and say whether it was accepted. The ONE
        overridable seam a headless test replaces, because an ``exec_()`` under
        the offscreen platform never returns."""
        return dialog.exec_() == QDialog.Accepted

    def _on_from_budget(self) -> None:
        dialog = self.budget_basis_dialog()
        try:
            if not self._run_dialog(dialog):
                return
            self.stage_spending_basis(dialog.accepted_basis())
        finally:
            dialog.deleteLater()

    def stage_spending_basis(self, basis) -> bool:
        """Put a budget's basis in the spending field and say where it came from.

        STAGES, nothing more: the figure lands in the same box the user could
        have typed it into, and :meth:`_on_apply_per_year` - unchanged, and still
        the only writer here - is what turns it into a plan. It is a COPY, so a
        later edit to the budget does not move it, and the provenance line says
        so. This method neither judges the figure nor suggests a different one.
        """
        if basis is None or basis.annual_cents <= 0:
            self.say("That budget leaves nothing to spend in retirement: its "
                     "exclusions account for the whole window.")
            return False
        self.per_year_amount.setText(fmt_cents(basis.annual_cents))
        self.basis_provenance.setText(
            f"{basis.note} Taken from the budget on "
            f"{fmt_date(self._today.isoformat())}; a copy, not a link.")
        self.basis_provenance.setVisible(True)
        self.say(f"{fmt_money(basis.annual_cents)} a year is in the spending "
                 f"box, in {basis.basis_year} dollars and not inflated. Apply "
                 f"is what writes it into the plan.")
        return True

    # -- the table ----------------------------------------------------------
    def column_accounts(self) -> list[PlanAccount]:
        return list(self._columns)

    def column_for_account(self, account_id: int) -> Optional[int]:
        for index, acct in enumerate(self._columns):
            if acct.account_id == int(account_id):
                return len(WITHDRAWAL_FIXED_COLUMNS) + index
        return None

    def reload(self) -> None:
        """Rebuild every row from the stored plan, then restore the focused year."""
        self._read_accounts()
        # An account holding nothing is left out of the table (reported); it
        # stays in the plan - a new 401(k) can hold nothing yet and still be
        # paid into. A planned Roth is shown: conversions fill it.
        self._columns = [a for a in self._accounts
                         if a.planned or self.balance_cents(a.account_id) > 0]
        if not self._plan_restored:
            # Once per page, not per rebuild: put the stored household numbers
            # back in the boxes when the page first opens, and never again --
            # a rebuild must not overwrite what the user is mid-way through
            # typing into them.
            self._plan_restored = True
            self._load_plan_params()
        # Every rebuild: an account may have been added, renamed or retyped.
        self._load_sources()
        born = min((int(p["birth_year"]) for p in horizon_people(self.conn)),
                   default=None)
        headers = list(WITHDRAWAL_FIXED_COLUMNS) + \
            [a.label for a in self._columns] + ["Total"]
        self._loading = True
        try:
            self.table.clear()
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            header = self.table.horizontalHeader()
            # Content-sized columns are set AFTER the rows exist: sized while
            # filling, every setItem re-measures every row (the conversion
            # table's CPU spike).
            for col in range(len(headers)):
                header.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setRowCount(0)
            self._rows = []
            for year in self._years:
                self._add_row(year, born)
            for col in range(len(headers)):
                header.setSectionResizeMode(
                    col, QHeaderView.Stretch
                    if col >= len(WITHDRAWAL_FIXED_COLUMNS)
                    else QHeaderView.ResizeToContents)
        finally:
            self._loading = False
        self.focus_year(self._focus_year)

    def _add_row(self, year: int, born: Optional[int]) -> None:
        index = self.table.rowCount()
        self.table.insertRow(index)
        self._rows.append({"year": int(year)})
        self.table.setItem(index, WD_COL_YEAR, self._item(str(year)))
        self.table.setItem(index, WD_COL_AGE, self._item(
            str(year - born) if born is not None else ""))
        planned = {int(w["account_id"]): int(w["amount_cents"])
                   for w in retirement.list_withdrawals(self.conn, year)}
        total = 0
        for offset, acct in enumerate(self._columns):
            amount = planned.get(acct.account_id)
            total += amount or 0
            item = self._item(fmt_cents(amount) if amount is not None else "",
                              editable=True)
            item.setToolTip(self.cell_tooltip(acct.account_id, year))
            self.table.setItem(index, len(WITHDRAWAL_FIXED_COLUMNS) + offset, item)
        total_col = len(WITHDRAWAL_FIXED_COLUMNS) + len(self._columns)
        self.table.setItem(index, total_col, self._item(fmt_cents(total)))

    def cell_tooltip(self, account_id: int, year: int) -> str:
        """What bounds this cell, in words - the only place the ceiling is shown."""
        citation = self.floor_citation(account_id, year)
        cap = self.cap_cents(account_id, year)
        ceiling = (f"At most {fmt_money(cap)}: what {self.account_label(account_id)} "
                   f"is projected to hold entering {year}.")
        return f"{citation}\n{ceiling}" if citation else ceiling

    # -- edits --------------------------------------------------------------
    def _on_item_changed(self, item) -> None:
        """One cell was typed into: write it, then rebuild on the next tick.

        Deferred for the same reason the Roth schedule defers - rebuilding the
        table inside the signal its own cell emitted frees the editor Qt still
        holds (CLAUDE.md's deferral rule).
        """
        if self._loading or item is None:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(self._rows)):
            return
        offset = col - len(WITHDRAWAL_FIXED_COLUMNS)
        if not (0 <= offset < len(self._columns)):
            return
        year = self._rows[row]["year"]
        account_id = self._columns[offset].account_id
        if item.text().strip():
            self.set_withdrawal(account_id, year, parse_amount(item.text()))
        else:
            self.clear_withdrawal(account_id, year)
        defer(self, self.reload)

    # -- the focused year ---------------------------------------------------
    def focus_year(self, year: Optional[int]) -> None:
        """Select and scroll to a year's row - what a click on a bar asks for."""
        self._focus_year = None if year is None else int(year)
        if self._focus_year is None:
            return
        for index, info in enumerate(self._rows):
            if info["year"] == self._focus_year:
                column = (len(WITHDRAWAL_FIXED_COLUMNS)
                          if self._columns else WD_COL_YEAR)
                self.table.setCurrentCell(index, column)
                item = self.table.item(index, WD_COL_YEAR)
                if item is not None:
                    self.table.scrollToItem(item, QAbstractItemView.PositionAtTop)
                return

    def focused_year(self) -> Optional[int]:
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            return int(self._rows[row]["year"])
        return None


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
class RetirementPlannerPage(QWidget):
    """The View-menu page. Switched to exactly like the Investment Dashboard.

    The Roth question is answered HERE, on the page, and not in a dialog: the
    chart shows the room left under each bracket, a click on a bracket line
    claims that room, and a right-click opens the schedule inline to edit the
    numbers behind it. A dialog made every one of those a round trip away from
    the picture that prompted it.

    The Social Security dialog that remains is built by its own small method and
    RUN through :meth:`_run_dialog`, never built-and-``exec_()``-ed inline: under
    the offscreen platform an inline modal blocks forever, so the seam is what
    makes this page testable headless (CLAUDE.md's headless-modal hazard). The
    Roth section needs no such seam, because it opens nothing.
    """

    #: An unowned account in the conversion schedule was double-clicked. The
    #: window opens Account Details for it (ui/widgets.py); the page has no
    #: window of its own to reach for, being built standalone in tests.
    accountDetailsRequested = pyqtSignal(int)
    #: The "Go to Investment Dashboard" link; the window switches pages.
    dashboardRequested = pyqtSignal()
    #: Something the Investment Dashboard's projection reads has changed: a
    #: draw, a conversion, a seeded minimum, the plan's mix, its horizon age, a
    #: person, a salary, the survivor scenario. The window connects it to the
    #: dashboard's ``mark_stale``. ONE signal for every write this page makes,
    #: because wiring the two editors' ``changed`` alone left the dashboard
    #: drawing a plan without the minimums the Social Security dialog had
    #: just seeded, and without a mix typed a moment before (reported by audit).
    planChanged = pyqtSignal()

    def __init__(self, conn, parent=None, *, today: Optional[_dt.date] = None):
        super().__init__(parent)
        self.conn = conn
        self._today = today or _dt.date.today()
        self._stale = True
        self._terminal_age = DEFAULT_TERMINAL_AGE
        self.rows: list[IncomeYear] = []
        #: Year -> stored BASE projected taxable income, conversions excluded.
        self._base: dict[int, int] = {}
        self._faq: Optional[RetirementFaqWindow] = None
        self._build()

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("Retirement Planner")
        font = title.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 2)
        title.setFont(font)
        header.addWidget(title)
        # Reported: a way back to the Investment Dashboard from here.
        self.dashboard_link = QLabel('<a href="dashboard">Go to Investment Dashboard</a>')
        self.dashboard_link.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.dashboard_link.linkActivated.connect(
            lambda _href: self.dashboardRequested.emit())
        header.addStretch(1)

        header.addWidget(QLabel("Plan through age"))
        self.terminal_age = NoWheelSpinBox()
        self.terminal_age.setRange(MIN_TERMINAL_AGE, MAX_TERMINAL_AGE)
        self.terminal_age.setValue(
            max(MIN_TERMINAL_AGE, min(MAX_TERMINAL_AGE,
                                      retirement.get_plan_through_age(self.conn))))
        # Typing "95" must not redraw at "9"; the arrows still redraw per step.
        self.terminal_age.setKeyboardTracking(False)
        self.terminal_age.setToolTip(
            "A longevity CASE, not a forecast: the plan is drawn out to the "
            "year the household turns this age.")
        self.terminal_age.valueChanged.connect(self._terminal_age_changed)
        header.addWidget(self.terminal_age)
        # The survivor scenario: who dies, and at the end of which year.
        self.survivor_combo = NoWheelComboBox()
        self.survivor_combo.setToolTip(
            "A survivor scenario: the plan runs on for the other spouse - "
            "single filing, the larger Social Security benefit, the IRAs rolled "
            "over, one Medicare enrollee, spending and a pension cut to the "
            "shares set in the Social Security dialog.")
        self.survivor_year = NoWheelSpinBox()
        self.survivor_year.setRange(self._today.year, self._today.year + 70)
        self.survivor_year.setKeyboardTracking(False)
        self.survivor_year.setToolTip("Dies at the end of this year.")
        header.addWidget(self.survivor_combo)
        header.addWidget(self.survivor_year)
        self._load_survivor()
        self.survivor_combo.currentIndexChanged.connect(self._survivor_changed)
        self.survivor_year.valueChanged.connect(self._survivor_changed)

        self.faq_button = QPushButton("FAQ…")
        self.faq_button.setToolTip(
            "The rules behind these figures, and where every published table "
            "comes from.")
        self.faq_button.clicked.connect(self.open_faq)
        # The plan's own asset mix, typed as stocks/bonds/cash (reported: set it
        # explicitly here rather than copy whatever What If said at some point).
        self.mix_button = QPushButton("Plan mix: as held today")
        self.mix_button.clicked.connect(self.edit_plan_mix)
        # The mix, with the way to the Investment Dashboard - where that mix
        # shows on the thermometer - right under it (reported).
        mix_column = QVBoxLayout()
        mix_column.setSpacing(0)
        mix_column.addWidget(self.mix_button)
        self.dashboard_link.setAlignment(Qt.AlignHCenter)
        mix_column.addWidget(self.dashboard_link)
        header.addLayout(mix_column)
        header.addWidget(self.faq_button)
        # What the figures rest on, shown on hover rather than as a paragraph
        # under the page. The assumptions THEMSELVES (COLA, bracket indexing,
        # the Social Security shortfall) moved into the Social Security dialog.
        self.assumptions = QToolButton(self)
        self.assumptions.setIcon(
            self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        self.assumptions.setAutoRaise(True)
        self.assumptions.setAccessibleName("Assumptions")
        # A CLICK shows it too (reported: "the info button does nothing" - it
        # only had a hover). A tooltip popup, not a dialog: nothing to dismiss,
        # and nothing modal to block a headless test.
        self.assumptions.clicked.connect(self.show_assumptions)
        header.addWidget(self.assumptions)
        layout.addLayout(header)

        layout.addWidget(self._heading("Income in Retirement by Source"))
        self.income_chart = IncomeChart(self)
        self.income_chart.hover_text = self.year_details
        self.income_chart.year_clicked.connect(self.show_withdrawals_for)
        layout.addWidget(self.income_chart, 3)
        self.income_caption = self._caption(layout)

        # The page's dialogs, in the order a plan is built (reported): who the
        # household is and what Social Security pays, what else comes in, what
        # to convert, and finally what to spend.
        steps = QHBoxLayout()
        steps.addStretch(1)
        self.ss_button = QPushButton("Social Security…")
        self.ss_button.setToolTip("The household, its earnings and benefits, and "
                                  "the planner's assumptions.")
        self.ss_button.clicked.connect(self.open_social_security)
        self.income_button = QPushButton("Income…")
        self.income_button.setToolTip(
            "Salaries (with their 401(k) deferral and match), pensions, "
            "investment income and other income.")
        self.income_button.clicked.connect(self.open_income)
        self.schedule_button = QPushButton("Conversion Schedule…")
        self.schedule_button.setToolTip(
            "The year-by-year Roth conversions. Right-clicking a bar on the "
            "taxable income chart opens it on that year.")
        self.schedule_button.clicked.connect(lambda: self.show_schedule_for(None))
        self.withdrawal_button = QPushButton("Withdrawal Schedule…")
        self.withdrawal_button.setToolTip(WITHDRAWAL_BUTTON_TIP)
        self.withdrawal_button.clicked.connect(lambda: self.show_withdrawals_for(None))
        for button in (self.ss_button, self.income_button, self.schedule_button,
                       self.withdrawal_button):
            steps.addWidget(button)
        steps.addStretch(1)
        layout.addLayout(steps)

        # Always visible, for the same reason the Roth section's notice is: a
        # refusal shown inside a window the user just closed is a refusal
        # nobody reads, and this section refuses things.
        self.withdrawal_notice = QLabel()
        self.withdrawal_notice.setWordWrap(True)
        self.withdrawal_notice.setTextFormat(Qt.PlainText)
        layout.addWidget(self.withdrawal_notice)

        self.withdrawals = WithdrawalSchedule(self.conn, self, today=self._today)
        self.withdrawals.changed.connect(self._plan_changed)
        self.withdrawals.changed.connect(self.planChanged)
        self.withdrawals.said.connect(self.withdrawal_notice.setText)
        self.withdrawals_window = ScheduleWindow("Withdrawal schedule",
                                                 self.withdrawals, self)

        roth_head = QHBoxLayout()
        # Reported: a way to start the conversions over in one click - on the
        # left, with the title centered over the plot (a spacer as wide as the
        # button balances it on the right).
        self.clear_conversions_button = QPushButton("Clear")
        self.clear_conversions_button.setToolTip("Remove every planned Roth conversion.")
        self.clear_conversions_button.clicked.connect(self.clear_all_conversions)
        roth_head.addWidget(self.clear_conversions_button)
        roth_head.addWidget(
            self._heading("Estimated Taxable Income and Roth Conversions"), 1)
        balance = QWidget()
        balance.setFixedWidth(self.clear_conversions_button.sizeHint().width())
        roth_head.addWidget(balance)
        layout.addLayout(roth_head)
        # The filing status lives in the Conversion Schedule window (reported);
        # built here because the page reads it, placed there once it exists.
        self.filing_combo = NoWheelComboBox()
        # Single and joint only. A separate return is two returns with each
        # spouse's own income, and the page has one household's; running that
        # through the separate ladder taxed two people's income as one
        # person's (found in an audit), so the choice is not offered.
        for status in ("single", "joint"):
            self.filing_combo.addItem(status.capitalize(), status)
        self.filing_combo.setCurrentIndex(
            max(0, self.filing_combo.findData(default_filing_status(self.conn))))
        self.filing_combo.setToolTip(
            "Which ordinary-income bracket ladder the lines are drawn from. A "
            "VIEW of the published table, not a stored tax election.")
        self.filing_combo.currentIndexChanged.connect(self._filing_changed)

        self.roth_chart = RothChart(self)
        self.roth_chart.hover_text = self.year_details
        self.roth_chart.fill_requested.connect(self._fill_to_bracket)
        self.roth_chart.clear_requested.connect(self._clear_year_conversions)
        self.roth_chart.schedule_requested.connect(self.show_schedule_for)
        layout.addWidget(self.roth_chart, 2)
        self.roth_caption = self._caption(layout)
        # The figures on a line of their own, as label: value pairs in fixed
        # widths and a fixed-pitch font, so a changed number stays where it was
        # (reported: the caption was one long sentence of numbers).
        self.roth_figures = self._caption(layout)
        from PyQt5.QtGui import QFontDatabase
        fixed = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        fixed.setPointSizeF(self.roth_figures.font().pointSizeF())
        self.roth_figures.setFont(fixed)
        self.roth_figures.setWordWrap(False)

        # One always-visible line for whatever the section has to say. It is not
        # inside the schedule, because a message hidden with the table is a
        # message the user never reads.
        self.roth_notice = QLabel()
        self.roth_notice.setWordWrap(True)
        self.roth_notice.setTextFormat(Qt.PlainText)
        layout.addWidget(self.roth_notice)

        self.schedule = ConversionSchedule(self.conn, self, today=self._today)
        self.schedule.changed.connect(self._conversions_changed)
        # Both editors cap a cell at what the HOUSEHOLD plan says the account
        # holds, when there is one (``_PlanEditor.projected_values``).
        self.schedule.plan_balances = self.withdrawals.household_balances
        self.withdrawals.plan_balances = self.withdrawals.household_balances
        self.schedule.plan_balance_at = self.withdrawals.household_balance_at
        self.withdrawals.plan_balance_at = self.withdrawals.household_balance_at
        self.schedule.changed.connect(self.planChanged)
        self._reapply_pending = False
        self.schedule.said.connect(self.roth_notice.setText)
        self.schedule.accountDetailsRequested.connect(self.accountDetailsRequested)
        self.schedule.fillRangeRequested.connect(self.fill_years_to_bracket)
        self.schedule.add_filing_status(self.filing_combo)
        self.schedule_window = ScheduleWindow("Conversion schedule",
                                              self.schedule, self)


    def rmd_by_year(self, years) -> dict[int, int]:
        """Each year's required minimums, summed over the accounts that owe one.

        With a household plan on file, the plan's OWN minimums - divided from
        the balances it carries (the pool's median, by account) - so the red
        line and the draws cannot disagree: projected separately, the line
        showed a minimum in a year the plan had already emptied the IRAs.
        Without one, the withdrawal schedule's floors."""
        wd = self.withdrawals
        params = retirement.get_withdrawal_plan(self.conn)
        if params.is_set:
            plan = wd.plan_for(params.start_cents, params.increase_pct,
                               start_year=params.start_year,
                               bracket_rate=params.bracket_rate)
            self._plan = plan
            floors = {entry.year: int(entry.floor_cents) for entry in plan.years}
            return {int(y): floors.get(int(y), 0) for y in years}
        self._plan = None
        wd.invalidate_plan()
        accounts = [a for a in wd.accounts() if a.treatment == "deferred"]
        return {int(y): sum(wd.rmd_floor_cents(a.account_id, y) for a in accounts)
                for y in years}

    def _sync_mix_button(self) -> None:
        mix = retirement.get_plan_mix(self.conn)
        if mix is None:
            self.mix_button.setText("Plan mix: as held today")
            self.mix_button.setToolTip(
                "The accounts are projected at the mix they hold today. Click to "
                "set the stocks / bonds / cash mix the plan will hold instead.")
        else:
            self.mix_button.setText(f"Plan mix: {plan_mix_caption(mix)}")
            self.mix_button.setToolTip(
                "Every account is projected at this mix - the Investment Dashboard's "
                "thermometer shows it with the Retirement Plan on. Click to change.")

    def edit_plan_mix(self) -> None:
        dlg = PlanMixDialog(retirement.get_plan_mix(self.conn), self)
        try:
            self._run_dialog(dlg)
            if dlg.result() == QDialog.Accepted:
                self.apply_plan_mix(dlg.values())
        finally:
            dlg.deleteLater()

    def apply_plan_mix(self, mix) -> bool:
        """Store the plan's mix (or None for the mix held today) and re-project."""
        try:
            retirement.set_plan_mix(self.conn, mix)
        except ValueError as exc:
            self.withdrawal_notice.setText(f"Plan mix not changed: {exc}.")
            return False
        self._sync_mix_button()
        self.withdrawals.invalidate_plan()
        self.schedule.invalidate_plan()
        self._income_changed()
        self.planChanged.emit()             # the dashboard projects at this mix
        return True

    def tax_by_year(self) -> dict[int, int]:
        """Estimated income tax per year (:meth:`PlanTaxes.tax_by_year`)."""
        return self._plan_taxes().tax_by_year()

    def seniors_in(self, year: int) -> int:
        return self._plan_taxes().seniors_in(year)

    def penalty_in(self, year: int) -> int:
        return self._plan_taxes().penalty_in(year)

    def taxable_with(self, year: int, conversion_cents: int) -> int:
        return self._plan_taxes().taxable_with(year, conversion_cents)

    def gains_in(self, year: int) -> int:
        return self._plan_taxes().gains_in(year)

    def exempt_in(self, year: int) -> int:
        return self._plan_taxes().exempt_in(year)

    def taxable_share(self, year: int, out_of_iras: int) -> int:
        return self._plan_taxes().taxable_share(year, out_of_iras)

    def tax_in(self, year: int, conversion_cents: int):
        return self._plan_taxes().tax_in(year, conversion_cents)

    def _plan_taxes(self) -> PlanTaxes:
        """The years' taxes as last read (:meth:`_seed_taxable`) - the
        domain's :class:`PlanTaxes`, which the MCP surface states too."""
        taxes = getattr(self, "_taxes", None)
        if taxes is None:
            taxes = PlanTaxes(self.conn, [], getattr(self, "_plan", None),
                              self.filing_status(), self.withdrawals,
                              base=getattr(self, "_base", {}))
        return taxes

    def aca_magi(self, year: int, conversion_cents: int = 0) -> Optional[int]:
        """ACA MAGI: AGI plus the untaxed part of Social Security - so all of
        the benefit, with the other income, the conversion and any gains."""
        parts = getattr(self, "_parts", {}).get(int(year))
        if parts is None:
            return None
        benefit, other, _deduction = parts
        gains = self.gains_in(year)
        return (other + self.taxable_share(year, conversion_cents) + max(0, gains)
                + retirement.loss_offset_cents(gains) + int(benefit) + self.exempt_in(year))

    def aca_baseline(self, year: int) -> int:
        """ACA MAGI with none of the plan's draws or conversions: the whole
        benefit plus the other taxable income - what the credit lost is
        measured from."""
        parts = getattr(self, "_parts", {}).get(int(year))
        if parts is None:
            return 0
        return (getattr(self, "_other_base", {}).get(int(year), parts[1]) + int(parts[0])
                + self.exempt_in(year))

    def aca_by_year(self) -> dict[int, int]:
        """Year -> the ACA premium credit the plan's draws and conversions
        cost: the slope under the cliff, and all of it over."""
        out = {}
        for r in self.rows:
            magi = self.aca_magi(r.year, r.conversion_cents)
            if magi is not None:
                cost = aca_cost(self.conn, r.year, magi, self.status_in(r.year),
                                baseline_magi_cents=self.aca_baseline(r.year))
                if cost:
                    out[r.year] = cost
        return out

    def aca_line(self) -> dict[int, int]:
        """Income year -> the TAXABLE income at which ACA MAGI reaches the cliff
        (the untaxed Social Security and the gains taken back off), in years a
        credit is at stake."""
        out = {}
        for r in self.rows:
            value = self.aca_value_in(r.year, self.gains_in(r.year))
            if value is not None:
                out[r.year] = value
        return out

    def aca_value_in(self, year: int, gains_cents: int) -> Optional[int]:
        """The ACA cliff line in ``year`` at ``gains_cents`` of that year's
        capital gains, or None when no one buys marketplace coverage in it."""
        status = self.status_in(year)
        parts = getattr(self, "_parts", {}).get(int(year))
        if parts is None or not aca_marketplace_months(self.conn, int(year), status):
            return None
        benefit, _other, deduction = parts
        gains, exempt = int(gains_cents), self.exempt_in(year)
        cliff = aca_cliff_in(self.conn, int(year), status)
        # The taxable share of the benefit AT the cliff: there, everything
        # but the benefit comes to (cliff - benefit), whatever the year's
        # base income was. Measured at the base income instead, the share
        # was too small, the line sat too low, and it moved with every
        # draw the plan re-applied - so a fill aimed at it chased a moving
        # target and landed over the cliff (found adding it to Fill years).
        taxed = retirement.taxable_social_security_cents(
            benefit, max(0, cliff - benefit), status)
        # ACA MAGI adds the untaxed benefit and tax-exempt interest to AGI.
        agi = cliff - benefit + taxed - exempt
        return max(0, agi - max(0, gains) - deduction
                   - retirement.senior_deduction_cents(
                       status, self.seniors_in(year), agi, int(year)))

    def conversion_room(self, year: int, taxable_top_cents: int) -> int:
        """The conversion that brings ``year``'s taxable income up to
        ``taxable_top_cents`` - Social Security it pulls in included."""
        parts = getattr(self, "_parts", {}).get(int(year))
        if parts is None:
            return max(0, int(taxable_top_cents) - int(self._base.get(int(year), 0)))
        benefit, other, deduction = parts
        taxable = retirement.taxable_room_cents(
            int(taxable_top_cents), other, benefit, self.status_in(year),
            self.gains_in(year), deduction, seniors=self.seniors_in(year),
            year=int(year), tax_exempt_cents=self.exempt_in(year))
        ratio = getattr(self, "_ratio", {}).get(int(year), Decimal(0))
        if ratio >= 1:
            return taxable
        # A conversion that is partly after-tax basis is that much bigger for
        # the same taxable income.
        return int((Decimal(taxable) / (Decimal(1) - ratio))
                   .quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def ira_left_at_end_cents(self) -> int:
        """What the tax-deferred accounts hold at the END of the last year
        shown: the plan's own balances when a plan is on file, otherwise the
        withdrawal schedule's projection."""
        if not self.rows:
            return 0
        last = self.rows[-1].year
        wd = self.withdrawals
        deferred = [a.account_id for a in wd.accounts() if a.treatment == "deferred"]
        plan = getattr(self, "_plan", None)
        if plan is not None:
            entries = {e.year: e for e in plan.years}
            if last + 1 in entries:
                return sum(int(entries[last + 1].balances.get(a, 0)) for a in deferred)
            entry = entries.get(last)
            if entry is None:
                return 0
            return max(0, sum(int(entry.balances.get(a, 0)) - int(entry.amounts.get(a, 0))
                              for a in deferred))
        return sum(int(wd.projected_values(a).get(last + 1, 0)) for a in deferred)

    def end_ira_tax(self) -> tuple[int, int, int]:
        """(left in the IRAs at the end, the tax on it, that tax in today's
        dollars) at the end-of-plan rate (reported: count it, so conversions
        that shrink it compare fairly)."""
        left = self.ira_left_at_end_cents()
        rate = retirement.get_end_ira_tax_pct(self.conn)
        tax = int((Decimal(left) * rate / 100).quantize(Decimal(1),
                                                       rounding=ROUND_HALF_UP))
        pct = Decimal(str(retirement.get_bracket_index_pct(self.conn)))
        years = (self.rows[-1].year + 1 - self._today.year) if self.rows else 0
        today = Decimal(tax) / (1 + pct / 100) ** max(0, years)
        return left, tax, int(today.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def tax_summary(self) -> str:
        """One sentence: income tax across the plan, the tax on the IRAs left
        at its end, and the total - each in today's dollars too."""
        total, today = self.tax_totals()
        left, end_tax, end_today = self.end_ira_tax()
        irmaa, irmaa_today = self.irmaa_totals()
        if not total and not end_tax and not irmaa:
            return ""
        rate = retirement.get_end_ira_tax_pct(self.conn)
        medicare = (f", plus {fmt_money(irmaa)} ({fmt_money(irmaa_today)}) in "
                    f"Medicare surcharges (IRMAA)" if irmaa else "")
        return (f"Estimated federal income tax across the plan: {fmt_money(total)} "
                f"({fmt_money(today)} in today's dollars){medicare}, plus "
                f"{fmt_money(end_tax)} ({fmt_money(end_today)}) at {rate}% on the "
                f"{fmt_money(left)} left in IRAs at the end: "
                f"{fmt_money(total + irmaa + end_tax)} in all "
                f"({fmt_money(today + irmaa_today + end_today)} in today's dollars).")

    def magi_cents(self, year: int, conversion_cents: int = 0) -> Optional[int]:
        """``year``'s income before the deduction - what IRMAA looks at - with
        ``conversion_cents`` converted, or None for a year not on the page."""
        parts = getattr(self, "_parts", {}).get(int(year))
        if parts is None:
            return None
        benefit, other, _deduction = parts
        gains, exempt = self.gains_in(year), self.exempt_in(year)
        return (retirement.gross_with_social_security_cents(
            other + self.taxable_share(year, conversion_cents), benefit,
            self.status_in(year), gains, exempt) + max(0, gains) + exempt)

    def irmaa_by_year(self) -> dict[int, tuple[int, int, int, int]]:
        """Premium year -> (surcharge, tier, enrolled person-months, the
        income year that set it): the income two years earlier, or - in a year
        the SSA-44 appeal covers - the year's own when that is lower. Years
        with neither on the page are left out."""
        people = planning_people(self.conn)
        conversions = {r.year: r.conversion_cents for r in self.rows}
        appealed = appealed_premium_years(self.conn)
        first = self.rows[0].year if self.rows else 0
        out = {}
        for r in self.rows:
            choices = []
            sources = [r.year - 2] + ([r.year] if r.year in appealed else [])
            for income_year in sources:
                magi = self.magi_cents(income_year, conversions.get(income_year, 0))
                if magi is None and income_year < first:
                    # Before the page: the return the household filed (typed).
                    magi = retirement.get_prior_magi(self.conn, income_year)
                if magi is None:
                    continue
                found = household_irmaa(self.conn, r.year, magi,
                                        self.status_in(income_year), people,
                                        self.status_in(r.year))
                choices.append(found + (income_year,))
            if choices:
                best = min(choices, key=lambda c: c[0])
                if best[2]:
                    out[r.year] = best
        return out

    def premiums_set_by(self, income_year: int) -> list[int]:
        """The premium years an income year sets: two years later unless that
        year is appealed, and the year itself when it is."""
        appealed = appealed_premium_years(self.conn)
        years = []
        if int(income_year) + 2 not in appealed:
            years.append(int(income_year) + 2)
        if int(income_year) in appealed:
            years.append(int(income_year))
        return years

    def irmaa_lines(self) -> list[dict[int, int]]:
        """Per tier, income year -> the TAXABLE income at which that year's
        income reaches the tier for the premium year two later (the MAGI
        ceiling less the year's deduction). Only years whose premium year has
        someone on Medicare."""
        people = planning_people(self.conn)
        lines: list[dict[int, int]] = []
        for r in self.rows:
            values = self.irmaa_values_in(r.year, self.gains_in(r.year), people)
            for tier, value in enumerate(values or ()):
                while len(lines) <= tier:
                    lines.append({})
                lines[tier][r.year] = value
        return lines

    def irmaa_values_in(self, year: int, gains_cents: int,
                        people=None) -> Optional[list[int]]:
        """Every IRMAA tier line in ``year``, lowest first, at ``gains_cents``
        of that year's capital gains; None when the year's income sets no
        premium. Separate from :meth:`irmaa_lines` so a fill can ask where the
        line would be under the gains a trial plan realizes."""
        people = planning_people(self.conn) if people is None else people
        pct = retirement.get_bracket_index_pct(self.conn)
        status = self.status_in(year)               # this year's return sets the tier
        parts = getattr(self, "_parts", {}).get(int(year))
        if parts is None:
            return None
        # The premium years this income sets (two later, and itself when
        # appealed) that have someone enrolled; the lower ceilings bind.
        premium = [y for y in self.premiums_set_by(int(year))
                   if any(medicare_months_in(self.conn, p, y)
                          for p in living_couple(self.conn, y, self.status_in(y), people))]
        if not premium:
            return None
        ceilings = [min(c) for c in zip(*(
            retirement.irmaa_ceilings_cents(status, y, pct) for y in premium))]
        # The chart is ORDINARY taxable income; MAGI adds the year's gains
        # back, so the line sits that much lower (found on a ledger: a
        # fill stopped "under" a tier, and the gains put it over).
        gains = max(0, int(gains_cents)) + self.exempt_in(year)
        return [max(0, int(ceiling) - int(parts[2]) - gains
                    - retirement.senior_deduction_cents(
                        status, self.seniors_in(year), int(ceiling), int(year)))
                for ceiling in ceilings]

    def irmaa_totals(self) -> tuple[int, int]:
        """(total, in today's dollars) of the Medicare surcharges, deflated at
        the bracket-indexing rate like the tax."""
        pct = Decimal(str(retirement.get_bracket_index_pct(self.conn)))
        total, today = 0, Decimal(0)
        for year, (cents, _tier, _months, _source) in self.irmaa_by_year().items():
            total += cents
            today += Decimal(cents) / (1 + pct / 100) ** max(0, year - self._today.year)
        for year, cents in self.aca_by_year().items():
            total += cents
            today += Decimal(cents) / (1 + pct / 100) ** max(0, year - self._today.year)
        return total, int(today.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def tax_totals(self) -> tuple[int, int]:
        """(total, in today's dollars) (:meth:`PlanTaxes.tax_totals`)."""
        return self._plan_taxes().tax_totals(self._today.year)

    def show_assumptions(self) -> None:
        from PyQt5.QtWidgets import QToolTip
        button = self.assumptions
        QToolTip.showText(button.mapToGlobal(button.rect().bottomLeft()),
                          button.toolTip(), button)

    @staticmethod
    def _heading(text: str) -> QLabel:
        """A section title over a chart - the charts themselves carry none."""
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        label.setAlignment(Qt.AlignHCenter)     # centered over its plot (reported)
        return label

    def _caption(self, layout) -> QLabel:
        """A chart's caption: what the bars total and where they came from.

        What it does NOT carry is provenance. The citation list that used to sit
        here was unreadable, and the staleness triangle beside it misused a mark
        that means missing or conflicting data everywhere else in the app. Both
        moved to the Retirement FAQ, which is the one place a published table's
        edition is now rendered.
        """
        text = QLabel()
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignHCenter)
        text.setTextFormat(Qt.PlainText)
        text.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(text)
        return text

    # -- the plan -----------------------------------------------------------
    def terminal_age_case(self) -> int:
        return int(self.terminal_age.value())

    def _terminal_age_changed(self, _value: int) -> None:
        retirement.set_plan_through_age(self.conn, self.terminal_age.value())
        self._terminal_age_redraw()
        self.planChanged.emit()             # the dashboard's "Plan (N years)"

    def _terminal_age_redraw(self) -> None:
        """A new age: redraw, and re-apply a stored household plan.

        Reported: raising the age past 100 showed years the plan had never been
        written for (it is written only when applied). Those years were
        UNPLANNED, so the seeding pass filled them with required minimums off a
        projection that did not know the plan had emptied the IRAs - IRA draws
        "way below the spend rate" reappeared after the money had run out.
        """
        self.refresh()
        self.withdrawals.set_apply_years(self.max_horizon())
        if retirement.get_withdrawal_plan(self.conn).is_set:
            self._income_changed()

    def income_rows(self) -> list[IncomeYear]:
        return list(self.rows)

    def assumption_lines(self) -> list[str]:
        """What the figures rest on, in the user's words. Mechanisms and rules
        only: nothing here names an amount, a year or an account."""
        age = self.terminal_age_case()
        lines = [
            f"Longevity is shown as a case: the plan runs to the year the "
            f"household turns {age}. Switch the case to see another - Mammon "
            f"does not pick one for you.",
            "Social Security is each person's own earnings record run through "
            "the published formula at their claim age, plus a spousal benefit "
            "where half the other's amount is more than their own, less the "
            "earnings test while a salary runs before full retirement age; in "
            "today's dollars, raised each year after this one by the COLA set "
            "in the Social Security dialog (default: the Trustees Report's "
            "long-range CPI assumption) and cut to the payable share once the "
            "trust fund is projected to run out.",
            "A planned tax-deferred withdrawal is checked against the RMD floor "
            "for that year; a Roth account has no lifetime RMD (IRC 408A(c)(4)).",
            "Every year an IRA or a former employer's plan has no withdrawal of "
            "your own is filled with that year's required minimum, out to the "
            "longest longevity case. Anything you type stays yours; clearing a "
            "cell puts the minimum back.",
            "A withdrawal you enter is held between two bounds: never below the "
            "required minimum, and never above what that account is projected "
            "to still hold entering the year - the same projection the line "
            "behind the bars is drawn from.",
            "A Roth conversion is taxable in the year it happens but is not "
            "spendable income, so it is stacked into the taxable-income bars of "
            "the Roth section and left out of the income bars above. A "
            "conversion does not satisfy an RMD (IRC 408A(d)(3)(E)).",
            "A conversion has to land in a Roth owned by the same person as the "
            "account it comes from - there is no spousal conversion (IRC "
            "408A(d)(3)) - so the target list only offers that person's Roths.",
            "Each year's projected taxable income starts as the taxable part of "
            "Social Security plus that year's tax-deferred withdrawals, and "
            "stays yours once you edit it: bracket lines are drawn from the "
            "published ladder for the filing status shown.",
        ]
        if self.income_chart.fund_line is not None:
            lines.append(
                "The line behind the bars is the median of the same projection "
                "the Investment Dashboard draws, over the retirement accounts "
                "only, with the plan's own draws and conversions applied. It is "
                "the value at the START of each year.")
        else:
            lines.append(
                "No fund line is drawn: the retirement accounts' investment mix "
                "could not be measured, and a made-up return assumption is not "
                "worth drawing.")
        if not planning_people(self.conn):
            lines.append(
                "Nobody on file has a birth year, so no benefit and no terminal "
                "year can be computed - open Social Security to add a person.")
        return lines

    def filing_status(self) -> str:
        data = self.filing_combo.currentData()
        return str(data) if data else "single"

    def _load_survivor(self) -> None:
        """Fill the scenario combo from the household: hidden without a couple."""
        couple = [p for p in planning_people(self.conn)
                  if p.get("relationship") in ("self", "spouse")]
        scenario = retirement.get_survivor_scenario(self.conn)
        for widget in (self.survivor_combo, self.survivor_year):
            widget.blockSignals(True)
        self.survivor_combo.clear()
        self.survivor_combo.addItem("Both living", None)
        for person in couple:
            self.survivor_combo.addItem(f"If {person.get('name') or 'this person'} dies",
                                        int(person["id"]))
        if scenario is not None:
            self.survivor_combo.setCurrentIndex(
                max(0, self.survivor_combo.findData(scenario.deceased_id)))
            self.survivor_year.setValue(scenario.death_year)
        self.survivor_year.setEnabled(scenario is not None)
        visible = len(couple) == 2
        self.survivor_combo.setVisible(visible)
        self.survivor_year.setVisible(visible)
        for widget in (self.survivor_combo, self.survivor_year):
            widget.blockSignals(False)

    def _survivor_changed(self, *_args) -> None:
        person = self.survivor_combo.currentData()
        if person is None:
            retirement.set_survivor_scenario(self.conn, None)
        else:
            if not self.survivor_year.isEnabled():
                # First turned on: default to the year that person turns 80.
                born = next((int(p["birth_year"]) for p in planning_people(self.conn)
                             if int(p["id"]) == int(person)), self._today.year - 70)
                self.survivor_year.blockSignals(True)
                self.survivor_year.setValue(max(self._today.year, born + 80))
                self.survivor_year.blockSignals(False)
            retirement.set_survivor_scenario(self.conn, int(person),
                                             self.survivor_year.value())
        self.survivor_year.setEnabled(person is not None)
        # Re-apply once, off this widget's own signal.
        defer(self, self._income_changed)
        self.planChanged.emit()             # the horizon person may have changed

    def status_in(self, year: int) -> str:
        """The filing status in ``year``: single for a survivor."""
        return year_status(self.conn, self.filing_status(), int(year))

    def _filing_changed(self, _index: int) -> None:
        # The deduction and the bracket tops both follow the filing status.
        self.withdrawals.filing_status = self.filing_status()
        self._seed_taxable()
        self._render_roth()

    # -- rendering ----------------------------------------------------------
    def refresh(self) -> None:
        self._stale = False
        # Balances, people, income or holdings may have moved since the plan's
        # balances were last computed.
        self.withdrawals.invalidate_household()
        self._load_survivor()           # a spouse may have been added or removed
        with inflows_measured_once():
            self._reread_plan(reload_schedule=True)

    def _reread_plan(self, *, reload_schedule: bool) -> None:
        """Recompute the plan and redraw everything that shows it.

        ``reload_schedule`` is False when the plan changed because a cell in the
        schedule was edited: the table rebuilds itself on its own deferred tick,
        and tearing it down from inside the signal its own cell just emitted is
        the crash CLAUDE.md's deferral rule is about.
        """
        self._seed_withdrawals()
        self.rows = income_rows(self.conn, terminal_age=self.terminal_age_case(),
                                today=self._today)
        # The plan first: its realized gains are in the seeded taxable income.
        self.income_chart.rmd_cents = self.rmd_by_year([r.year for r in self.rows])
        self._seed_taxable()
        self.income_chart.set_rows(self.rows)
        if reload_schedule:
            self.schedule.set_years([r.year for r in self.rows])
            self.withdrawals.set_apply_years(self.max_horizon())
            self.withdrawals.set_years([r.year for r in self.rows])
        self._render_roth()
        self._render_captions()
        self._flag_stale_withdrawals()
        self.assumptions.setToolTip("Assumptions\n" + "\n".join(
            f"- {line}" for line in self.assumption_lines()))

    def _seed_taxable(self) -> None:
        """Fill in any year's taxable income the user has not entered, then re-read.

        Computed for EVERY year, typed ones included (``overwrite_entered``):
        extra income for a year is an Other income source now, not an edit to
        this column, which is read-only in the schedule.

        Seeded from the plan's OWN flows - the taxable share of Social Security,
        taxable other income and that year's tax-deferred withdrawals - LESS the
        standard deduction, because taxable income is what the bracket tops
        apply to. Reported: seeded gross, the bars sat a whole deduction too
        high against the bracket lines. Conversions are deliberately left out:
        they are stacked on top of this base at render time, and counting them
        here would make a click on a bracket line chase its own tail.
        """
        taxes = PlanTaxes(self.conn, self.rows, getattr(self, "_plan", None),
                          self.filing_status(), self.withdrawals)
        self._parts, self._seniors = taxes.parts, taxes.seniors
        self._other_base, self._exempt = taxes.other_base, taxes.exempt
        self._pref, self._pensions = taxes.pref, taxes.pensions
        self._ratio, self._invest = taxes.ratio, taxes.invest
        retirement.seed_taxable_income(self.conn, taxes.seeded, overwrite_entered=True)
        self._base = retirement.taxable_income_map(self.conn)
        taxes.base = dict(self._base)
        self._taxes = taxes

    def max_horizon(self) -> list[int]:
        """Every year through age 100 or the chosen age, whichever is later.

        Both the seeding pass and the per-year button work on this rather than
        on the displayed horizon. A required minimum is owed in a year whether
        or not that year is currently drawn, and a decision about what to
        withdraw is a decision about the whole plan - if switching the
        terminal-age combo changed which years those two touched, the plan
        would quietly depend on a view setting.
        """
        return plan_horizon(self.conn,
                            max(PLAN_WRITTEN_THROUGH_AGE, self.terminal_age_case()),
                            self._today)

    def _seed_withdrawals(self) -> None:
        """Give every unplanned year of every RMD-floored account its minimum.

        Run on every re-read, not once: the plan is empty until somebody's birth
        year is on file, so the pass that fills it has to be the one that runs
        after the Social Security dialog closes. It is idempotent and never
        overwrites a stored number, so running it this often costs nothing and
        means the page is never showing a tax-deferred account taking less than
        the law requires (IRC 401(a)(9)).
        """
        years = self.max_horizon()
        self.withdrawals.set_apply_years(years)

        def prior_year_end(account_id, year):
            # Each seeded year changes the next one's balance, so re-project.
            self.withdrawals.invalidate_plan()
            return self.withdrawals.prior_year_end_balance_cents(account_id, year)

        written = retirement.seed_withdrawals(
            self.conn, years,
            lambda account_id: account_value_cents(self.conn, account_id),
            balance_for_year=prior_year_end,
            employer_until=retirement.employer_plan_ends(
                self.conn, retirement_start_year(self.conn)))
        self.withdrawals.invalidate_plan()
        if written:
            # Seeded through the domain layer, not the editor, so the editor's
            # own ``changed`` never fires for these rows.
            self.planChanged.emit()

    def _render_roth(self) -> None:
        self.roth_chart.status_by_year = {r.year: self.status_in(r.year)
                                          for r in self.rows}
        self.roth_chart.set_rows(self.rows, self._base, self.filing_status(),
                                 index_pct=retirement.get_bracket_index_pct(self.conn),
                                 total_by_year={r.year: self.taxable_with(
                                     r.year, r.conversion_cents) for r in self.rows},
                                 tax_draw_by_year=self.tax_draw_taxable(),
                                 gains_by_year={r.year: self.gains_in(r.year)
                                                for r in self.rows},
                                 irmaa_lines=self.irmaa_lines(),
                                 aca_line=self.aca_line())

    def tax_draw_taxable(self) -> dict[int, int]:
        """Per year, how much of the base taxable income is the IRA draw that
        pays the year's income tax (the plan's ``tax_deferred_cents``, with the
        Social Security it pulls in)."""
        plan = getattr(self, "_plan", None)
        parts = getattr(self, "_parts", {})
        if plan is None:
            return {}
        out: dict[int, int] = {}
        for entry in plan.years:
            extra = int(getattr(entry, "tax_deferred_cents", 0))
            found = parts.get(int(entry.year))
            if not extra or found is None:
                continue
            benefit, other, deduction = found

            status = self.status_in(entry.year)
            gains = self.gains_in(entry.year)

            def taxable(x: int) -> int:
                return max(0, retirement.gross_with_social_security_cents(
                    x, benefit, status, gains) - deduction)

            out[int(entry.year)] = max(0, taxable(other) - taxable(other - extra))
        return out

    def _flag_stale_withdrawals(self) -> None:
        """An amber triangle on the Withdrawal Schedule button while the stored
        withdrawals are not what the stored plan computes today
        (:func:`retirement_plan.stale_withdrawal_years`), and a tooltip saying
        why and what to do. Flagged, not rewritten: re-applying is the user's
        decision. The triangle means data that is missing or out of date,
        which this is."""
        years = stale_withdrawal_years(self.conn, getattr(self, "_plan", None))
        self.stale_withdrawal_years = years
        if years:
            from PyQt5.QtGui import QIcon
            from mammon.ui.asset_allocation import warning_pixmap
            self.withdrawal_button.setIcon(QIcon(warning_pixmap()))
            self.withdrawal_button.setToolTip(
                f"The data this plan depends on has changed since it was last "
                f"applied - balances, prices, deposits or the date - so the "
                f"stored withdrawals from {years[0]} on no longer match it. "
                f"Re-apply the spending plan in the Withdrawal Schedule to "
                f"update them.\n\n" + WITHDRAWAL_BUTTON_TIP)
        else:
            from PyQt5.QtGui import QIcon
            self.withdrawal_button.setIcon(QIcon())
            self.withdrawal_button.setToolTip(WITHDRAWAL_BUTTON_TIP)

    def _render_captions(self) -> None:
        """The two chart captions: a total and where the bars came from.

        Both sentences describe an INPUT the user can act on - the total these
        bars add up to, and the fact that the conversion amounts are entered
        rather than derived from a transaction. Nothing here cites a published
        table; that is the FAQ's job.
        """
        # The explanation first, the total after it (reported).
        income = ("Spendable income by year, stacked by where each dollar comes "
                  "from.")
        total = sum(r.income_cents for r in self.rows)
        if total:
            income += f" Projected income across the plan: {fmt_money(total)}."
        self.income_caption.setText(income)

        # One line of what the chart is, then the figures (reported: too
        # verbose). The full sentence is still the re-apply notice's.
        self.roth_caption.setText(
            "Taxable income with planned conversions on top; taxes in today's "
            "dollars. Click a line in a bar to fill to it, again to undo; "
            "right-click to edit the year.")
        self.roth_figures.setText(self.roth_figures_text())

    def roth_figures_text(self) -> str:
        """The conversion chart's figures as fixed-width label: value pairs."""
        _total, tax = self.tax_totals()
        _irmaa, irmaa = self.irmaa_totals()
        _left, _end, end = self.end_ira_tax()
        converted = sum(r.conversion_cents for r in self.rows)

        def pair(label: str, cents: int) -> str:
            dollars = f"${int(round(cents / 100)):,}"
            return f"{label}: {dollars:>11}"

        state = self.state_totals()
        return "   ".join((pair("Converted", converted),
                           pair("Federal tax", tax - state), pair("State tax", state),
                           pair("IRMAA/ACA", irmaa), pair("Tax on IRAs left", end),
                           pair("Total tax", tax + irmaa + end)))

    def state_totals(self) -> int:
        """The state tax across the plan, in today's dollars."""
        pct = Decimal(str(retirement.get_bracket_index_pct(self.conn)))
        today = Decimal(0)
        for r in self.rows:
            today += (Decimal(self.tax_in(r.year, r.conversion_cents).state)
                      / (1 + pct / 100) ** max(0, r.year - self._today.year))
        return int(today.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    # -- the Roth section's two gestures -----------------------------------
    def _fill_to_bracket(self, year: int, edge_cents: int) -> int:
        """A bracket line was clicked inside a bar: claim exactly that gap.

        The gap is measured from the year's BASE taxable income, not from the top
        of the bar, so clicking the same line twice is idempotent instead of
        stacking a second conversion on the first. Nothing is guessed about
        whether converting is wise; the user aimed at a line and Mammon fills to
        it.
        """
        year = int(year)
        if self.conversion_room(year, int(edge_cents)) <= 0:
            self.roth_notice.setText(
                f"{year} already has more taxable income than that bracket's "
                f"top, so there is no room under it to convert into.")
            return 0
        stored = self._fill_to_tops({year: int(edge_cents)}).get(year, 0)
        outcome = self.fill_outcome(year, int(edge_cents), stored)
        text = ""
        if outcome == "filled":
            text = (f"{year} conversions set to {fmt_money(stored)}, filling the "
                    f"gap up to {fmt_money(int(edge_cents))} of taxable income, "
                    f"the tax the conversion adds included.")
        elif outcome == "short":
            # "that line": the click may have been on an IRMAA or ACA line.
            text = (f"{year} conversions set to {fmt_money(stored)}: all the "
                    f"tax-deferred accounts have left, short of that line.")
        elif outcome == "empty":
            text = (f"{year} converts nothing: the tax-deferred accounts have "
                    f"nothing left to convert by then.")
        text += self.fill_side_effect_text()
        if text:
            self.roth_notice.setText(text.strip())
        self._after_fill()
        return stored

    def _after_fill(self) -> None:
        """A fill has already re-applied the plan every round, so it only
        needs the redraw. Queuing the ordinary deferred re-apply as well ran
        the plan once more on the next tick, and that pass could shrink the
        conversions again (the no-Roth-draw-while-converting rule): the
        result then depended on whether a refresh had run first (found on a
        ledger). Without a plan on file nothing was applied, so the ordinary
        path stands."""
        if retirement.get_withdrawal_plan(self.conn).is_set:
            self._plan_changed()
        else:
            self._conversions_changed()

    def fill_outcome(self, year: int, top_cents: int, stored_cents: int) -> str:
        """Why a fill ended where it did: "filled" to the line, "short" of it
        because the tax-deferred accounts ran out, "empty" because they had
        nothing left, or "over" because the year was already past the line.
        Reported: an emptied-IRA year was reported as "already had more
        taxable income", which it did not."""
        reached = self.taxable_with(year, stored_cents) >= int(top_cents) - 1_00
        if stored_cents > 0:
            return "filled" if reached else "short"
        return "over" if reached else "empty"

    def _fill_to_tops(self, tops: Mapping[int, int], retop=None,
                      line_at=None) -> dict[int, int]:
        """Convert in each year until its taxable income reaches its top.
        Returns what each year stores.

        With a household plan on file the plan PAYS the year's tax, from the
        IRA while there is room, so a conversion raises the IRA draw too - and
        a gap filled with the conversion alone went over the line every time
        (reported). So the fill is solved against the re-applied plan: convert,
        re-apply, measure how far over or under the line the year landed, and
        correct by the measured slope (secant), a few rounds at most. Without a
        plan nothing pays the tax and one measured gap is exact."""
        from PyQt5.QtWidgets import QApplication

        def totals() -> dict[int, int]:
            out: dict[int, int] = {}
            for row in retirement.list_conversions(self.conn):
                out[int(row["year"])] = out.get(int(row["year"]), 0) + int(row["amount_cents"])
            return out

        before = totals()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with inflows_measured_once():
                return self._fill_to_tops_measured(tops, retop, line_at)
        finally:
            QApplication.restoreOverrideCursor()
            # Re-applying the household plan re-checks EVERY year's
            # conversions against what its source holds in that year, so a
            # fill of one year can cut another - a conversion sized to empty
            # an IRA is the one a re-projection most easily finds too big. The
            # fill's own message used to replace the note saying so (reported:
            # "I clicked to fill 2042 ... and the conversion for 2031
            # disappeared"); callers now append this.
            after = totals()
            asked = {int(y) for y in tops}
            self._fill_side_effects = {
                y: (before.get(y, 0), after.get(y, 0))
                for y in sorted(set(before) | set(after))
                if y not in asked and before.get(y, 0) != after.get(y, 0)}

    def fill_side_effect_text(self) -> str:
        """The other years a fill's re-applied plan changed, as a sentence, or
        "" when it changed none."""
        changed = getattr(self, "_fill_side_effects", {})
        if not changed:
            return ""
        parts = []
        for year, (was, now) in changed.items():
            parts.append(f"{year} from {fmt_money(was)} to {fmt_money(now)}"
                         if now else f"{year} ({fmt_money(was)}) removed")
        return (" Re-applying the plan also changed later years' conversions, "
                "cut to what their accounts hold in the re-applied plan: "
                + "; ".join(parts) + ".")

    def _fill_to_tops_measured(self, tops: Mapping[int, int], retop=None,
                               line_at=None) -> dict[int, int]:
        """The fill itself: :meth:`_solve_fill` with a household plan on file,
        one write at the end. Without one nothing pays the conversion's tax
        out of the accounts, and the room to the line is the conversion,
        exactly."""
        params = retirement.get_withdrawal_plan(self.conn)
        tops = {int(y): int(t) for y, t in tops.items()}
        stored: dict[int, int] = {}
        if not params.is_set:
            self.schedule.blockSignals(True)
            try:
                for year, top in sorted(tops.items()):
                    room = self.conversion_room(year, top)
                    has = any(True for _ in retirement.list_conversions(self.conn, year))
                    if room <= 0 and not has:
                        stored[year] = 0
                        continue
                    stored[year] = self.schedule.set_total_conversion(
                        year, max(0, int(room)), rebuild=False)
            finally:
                self.schedule.blockSignals(False)
            self.schedule.reload()
            return stored
        self._solve_fill(tops, params, line_at)
        # One write, of the plan the last round already computed - nothing
        # before the first year filled is touched (``from_year``). Silent: the
        # caller redraws once (``_after_fill``), and the write's own signal
        # redrew the page a second time for nothing.
        self.withdrawals.blockSignals(True)
        try:
            self.withdrawals.apply_household(
                params.start_cents, params.increase_pct, start_year=params.start_year,
                bracket_rate=params.bracket_rate, from_year=min(tops, default=None))
        finally:
            self.withdrawals.blockSignals(False)
        self.planChanged.emit()
        for year in tops:
            stored[year] = sum(int(r["amount_cents"])
                               for r in retirement.list_conversions(self.conn, year))
        self.schedule.reload()
        return stored

    #: The fill aims this far UNDER its line and accepts anything from here
    #: to the line: an IRMAA tier or the ACA cliff is a cliff, and a fill that
    #: landed cents over one charged all of it (found on a ledger).
    FILL_AIM_UNDER_CENTS = 50

    #: The most rounds a fill takes. Each round moves every unsettled year to
    #: the next linear piece of its income, so this bounds the breaks crossed.
    FILL_MAX_ROUNDS = 12

    def _solve_fill(self, tops: Mapping[int, int], params, line_at=None) -> None:
        """Set each year's conversion so its taxable income reaches its line,
        the tax it causes paid as the household plan pays it.

        Taxable income as a function of a year's conversion is PIECEWISE
        LINEAR: within a piece, each dollar converted adds one dollar of income
        plus the income of the draw that pays its tax, at a fixed slope. Its
        breaks are the bracket edges, the Social Security thresholds (IRC 86),
        the senior deduction's phase-out, and the tax source changing account.
        So the solve is exact, not a tolerance: the first trial is the formula
        - a gap of A at marginal rate r converts A(1 - r), the rest being the
        tax on the whole gap - and every later trial steps along the slope
        measured between a year's last two points, landing exactly on the line
        unless a break lies in between, in which case it lands on the next
        piece. A year settles when a trial lands in the window under its line,
        or when its accounts can convert no more.

        Every year is tried in the SAME round, and a round is one plan
        COMPUTED, never written: the plan is causal, so each year's income is
        read off one computation whatever the later years are doing. The
        round-by-round re-apply this replaces wrote the plan and re-read the
        page every round, up to seven times, and stopped on a tolerance
        (reported: "It should result in much faster calculation and should be
        deterministic"). It is deterministic: the same plan gives the same
        trials in the same order.
        """
        wd = self.withdrawals
        aim = self.FILL_AIM_UNDER_CENTS
        years = sorted(int(y) for y in tops)
        sources = retirement.plan_income_sources(self.conn)
        deferred = {a.account_id for a in wd.accounts() if a.treatment == "deferred"}
        # A year still moving: its next trial and the points measured so far.
        # The first point is FREE: the stored plan is the plan at the stored
        # conversions (it reproduces itself), so the page already knows each
        # year's income at them, and the first trial is the formula from there.
        guess: dict[int, int] = {}
        points: dict[int, list[tuple[int, int]]] = {}
        settled: dict[int, bool] = {}
        for year in years:
            have = sum(int(r["amount_cents"])
                       for r in retirement.list_conversions(self.conn, year))
            taxable = self.taxable_with(year, have)
            line = int(tops[year]) if line_at is None else line_at(year, self.gains_in(year))
            target = (int(tops[year]) if line is None else int(line)) - aim
            points[year] = [(have, taxable)]
            rate = Decimal(retirement.marginal_bracket(
                max(0, taxable), self.status_in(year)).rate_label.rstrip("%")) / 100
            guess[year] = max(0, have + int((Decimal(target - taxable) * (1 - rate))
                                            .quantize(Decimal(1), rounding=ROUND_DOWN)))
            settled[year] = False
        wd._frozen_balances = None
        try:
            for _round in range(self.FILL_MAX_ROUNDS + 1):
                self.schedule.blockSignals(True)
                try:
                    asked = {}
                    for year in years:
                        asked[year] = self.schedule.set_total_conversion(
                            year, max(0, int(guess[year])), rebuild=False)
                finally:
                    self.schedule.blockSignals(False)
                wd._frozen_balances = None
                plan = wd.plan_for(params.start_cents, params.increase_pct,
                                   start_year=params.start_year,
                                   bracket_rate=params.bracket_rate)
                entries = {int(e.year): e for e in plan.years}
                # The next round's caps: this plan's balances, which cover
                # every conversion but the ones being tried in their own year.
                frozen: dict[int, dict[int, int]] = {}
                for entry in plan.years:
                    for aid, cents in entry.balances.items():
                        frozen.setdefault(int(aid), {})[int(entry.year)] = max(0, int(cents))
                wd._frozen_balances = frozen
                ratios = self._trial_basis_ratios(entries, years[-1])
                moving = False
                # A year may settle only once every EARLIER year had settled
                # before this round: its income and its cap both hang on them,
                # and a year settled against earlier years still moving kept a
                # cap from a plan they had since changed (a capped year's
                # result depended on the path the solve took).
                earlier_done = True
                done_before = dict(settled)
                for year in years:
                    ready = earlier_done
                    earlier_done = earlier_done and bool(done_before.get(year))
                    entry = entries.get(year)
                    stored = asked[year]
                    if entry is None:
                        taxable, line = self.taxable_with(year, stored), int(tops[year])
                    else:
                        gains = int(entry.gains_cents) + getattr(self, "_pref", {}).get(year, 0)
                        taxable = self.taxable_in_plan(year, entry, stored, sources, deferred,
                                                       ratio=ratios.get(year, Decimal(0)))
                        line = int(tops[year]) if line_at is None else line_at(year, gains)
                        line = int(tops[year]) if line is None else int(line)
                    target = line - aim
                    here = points[year]
                    if settled.get(year):
                        continue
                    landed = abs(taxable - target) <= aim
                    capped = stored < guess[year] and taxable < target
                    if landed or capped:
                        if ready:
                            settled[year] = True          # on the line, or out of money
                            guess[year] = stored if landed else guess[year]
                        else:
                            moving = True                 # measure again once they rest
                        continue
                    last_c, last_t = here[-1]
                    here.append((stored, taxable))
                    if stored == last_c:
                        if ready:
                            settled[year] = True          # it can move no further
                        else:
                            moving = True
                        continue
                    slope = Decimal(taxable - last_t) / Decimal(stored - last_c)
                    if slope <= 0:
                        slope = Decimal(1)
                    guess[year] = max(0, stored + int((Decimal(target - taxable) / slope)
                                                      .quantize(Decimal(1), rounding=ROUND_DOWN)))
                    moving = True
                if not moving and all(settled.get(y) for y in years):
                    break
            # Leave each year's conversion at its settled trial, capped by the
            # last plan's balances.
            self.schedule.blockSignals(True)
            try:
                for year in years:
                    self.schedule.set_total_conversion(year, max(0, int(guess[year])),
                                                       rebuild=False)
            finally:
                self.schedule.blockSignals(False)
        finally:
            wd._frozen_balances = None
            wd.invalidate_household()

    def _trial_basis_ratios(self, entries: Mapping[int, object], through: int) -> dict:
        """The after-tax basis share of each year (Form 8606) in a COMPUTED
        plan: it hangs on what earlier years drew and converted, which a fill
        moves, so a solve reads it off the plan it is trying."""
        converted_out: dict[tuple[int, int], int] = {}
        for row in retirement.list_conversions(self.conn):
            key = (int(row["year"]), int(row["from_account_id"]))
            converted_out[key] = converted_out.get(key, 0) + int(row["amount_cents"])
        return basis_ratios(
            self.withdrawals.accounts(), [y for y in sorted(entries) if y <= through],
            lambda aid, y: int(entries[y].balances.get(aid, 0)),
            lambda aid, y: (int(entries[y].amounts.get(aid, 0))
                            + converted_out.get((y, aid), 0)))

    def taxable_in_plan(self, year: int, entry, conversion_cents: int,
                        sources=None, deferred=None, *, ratio=None) -> int:
        """``year``'s taxable income in a COMPUTED plan: its IRA draws (the
        spending and the tax they pay), its realized gains and
        ``conversion_cents`` converted - exactly what :meth:`taxable_with`
        reads once that plan is written and the page re-read, so the solver
        and the chart measure the same number."""
        parts = getattr(self, "_parts", {}).get(int(year))
        if parts is None:
            return self.taxable_with(year, conversion_cents)
        sources = retirement.plan_income_sources(self.conn) if sources is None else sources
        if deferred is None:
            deferred = {a.account_id for a in self.withdrawals.accounts()
                        if a.treatment == "deferred"}
        benefit, _other, deduction = parts

        def share(cents: int) -> int:
            if ratio is None:
                return self.taxable_share(year, cents)
            return int(cents) - int((Decimal(int(cents)) * Decimal(ratio))
                                    .quantize(Decimal(1), rounding=ROUND_HALF_UP))

        draws = sum(int(entry.amounts.get(aid, 0)) for aid in deferred)
        other = other_taxable_cents(self.conn, int(year), share(draws), sources)
        gains = int(entry.gains_cents) + getattr(self, "_pref", {}).get(int(year), 0)
        return retirement.taxable_ordinary_cents(
            other + share(conversion_cents), benefit,
            self.status_in(year), gains, deduction,
            seniors=self.seniors_in(year), year=int(year),
            tax_exempt_cents=self.exempt_in(year))

    def irmaa_fill_tops(self, tier: int, years: Sequence[int]) -> dict[int, int]:
        """Year -> the taxable income at IRMAA tier ``tier``'s line, for each
        of ``years`` that has one. A year whose income sets no Medicare premium
        has no line and is absent."""
        lines = self.irmaa_lines()
        line = lines[int(tier) - 1] if 0 < int(tier) <= len(lines) else {}
        return {int(y): int(line[int(y)]) for y in years if int(y) in line}

    def fill_years_to_bracket(self, first: int, last: int, rate: str) -> int:
        """Fill each year from ``first`` through ``last`` up to one line and
        return the total converted. ``rate`` is either a bracket ("22", the top
        as it stands in the year, indexed) or an IRMAA tier (``irmaa:2``, the
        chart's "IRMAA 2" line; see :func:`irmaa_fill_key`).

        An IRMAA tier exists only in a year whose income sets a Medicare
        premium, so a year without one is left exactly as it was, and the
        notice says which. The tier's line moves as the plan does - paying the
        conversion's tax can sell stock whose gains are in MAGI - so it is
        re-measured every round of the fill, and the fill lands at or just
        under it: a tier is a cliff.

        One batch: the schedule's signals are held while the years are written,
        so the plan re-applies once at the end instead of once per year.
        """
        asked = list(range(int(min(first, last)), int(max(first, last)) + 1))
        tier = irmaa_fill_tier(rate)
        retop = None
        untiered: list[int] = []
        if rate == ACA_FILL_KEY:
            # The same shape as an IRMAA tier: a cliff drawn only in some
            # years, re-measured as the plan moves, filled to at or just under.
            def retop() -> dict[int, int]:
                line = self.aca_line()
                return {y: int(line[y]) for y in asked if y in line}

            def line_at(year: int, gains: int) -> Optional[int]:
                return self.aca_value_in(year, gains)

            tops = retop()
            untiered = [y for y in asked if y not in tops]
            target = "the ACA cliff, landing at or just under it"
            missing = ("buy no marketplace coverage, so they have no ACA cliff "
                       "and were left as they were.")
            if not tops:
                message = (f"No one in {asked[0]}-{asked[-1]} buys marketplace "
                           f"coverage, so there is no ACA cliff to fill to and "
                           f"nothing was changed.")
                self.roth_notice.setText(message)
                self.schedule_window.notice.setText(message)
                return 0
        elif tier is None:
            index_pct = retirement.get_bracket_index_pct(self.conn)
            tops: dict[int, int] = {}
            for year in asked:
                top = retirement.bracket_top_cents(rate, self.status_in(year),
                                                   year=year, index_pct=index_pct)
                if top is not None:
                    tops[year] = int(top)
            target = f"the top of the {rate}% bracket"
        else:
            def retop() -> dict[int, int]:
                return self.irmaa_fill_tops(tier, asked)

            def line_at(year: int, gains: int) -> Optional[int]:
                values = self.irmaa_values_in(year, gains)
                return values[tier - 1] if values and len(values) >= tier else None

            tops = retop()
            untiered = [y for y in asked if y not in tops]
            target = f"IRMAA {tier}, landing at or just under it"
            missing = (f"set no Medicare premium, so they have no IRMAA {tier} "
                       f"line and were left as they were.")
            if not tops:
                message = (f"None of {asked[0]}-{asked[-1]} sets a Medicare "
                           f"premium, so there is no IRMAA {tier} line to fill "
                           f"to and nothing was changed.")
                self.roth_notice.setText(message)
                self.schedule_window.notice.setText(message)
                return 0
        # A year with no room under the line converts NOTHING: left alone, a
        # fill to a lower bracket kept the higher fill's conversions (reported:
        # "it should have deleted them"). _fill_to_tops writes the zero.
        stored = self._fill_to_tops(tops, retop,
                                    line_at if retop is not None else None)
        if retop is not None:
            tops = retop()
        outcome = {y: self.fill_outcome(y, tops[y], stored[y])
                   for y in sorted(stored) if y in tops}
        sources = [a for a in self.schedule.accounts() if not a.is_roth]
        for year, kind in outcome.items():
            # "Short of the top" only when the accounts really ran out: a fill
            # that stopped a few dollars under a moving IRMAA line converted
            # what it meant to (found taking the README screenshots).
            if kind == "short" and stored[year] < sum(
                    self.schedule.conversion_cap_cents(a.account_id, year) for a in sources):
                outcome[year] = "filled"

        def years(kind: str) -> str:
            return ", ".join(str(y) for y, o in outcome.items() if o == kind)

        total = sum(stored.values())
        filled = [y for y, o in outcome.items() if o == "filled"]
        message = (f"Filled {len(filled)} year(s) to {target}: "
                   f"{fmt_money(total)} converted in all.")
        if years("short"):
            message += (f" {years('short')}: the tax-deferred accounts ran out "
                        f"short of the line.")
        if years("empty"):
            message += (f" {years('empty')}: nothing left in the tax-deferred "
                        f"accounts to convert.")
        if years("over"):
            message += (f" {years('over')} already had more taxable income than "
                        f"that, so they convert nothing.")
        if untiered:
            message += f" {', '.join(str(y) for y in untiered)} {missing}"
        message += self.fill_side_effect_text()
        self.roth_notice.setText(message)
        self.schedule_window.notice.setText(message)
        self._after_fill()
        return total

    def confirm(self, title: str, text: str) -> bool:
        """Ask yes or no. The seam headless tests replace: an exec_()-ed box
        blocks forever under the offscreen platform."""
        from PyQt5.QtWidgets import QMessageBox
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def clear_all_conversions(self) -> int:
        """Remove every planned conversion, after asking, and re-apply the
        plan. Returns how many years were cleared."""
        rows = retirement.list_conversions(self.conn)
        if not rows:
            self.roth_notice.setText("There are no planned conversions to clear.")
            return 0
        years = sorted({int(r["year"]) for r in rows})
        total = sum(int(r["amount_cents"]) for r in rows)
        if not self.confirm(
                "Clear conversions",
                f"Remove all {fmt_money(total)} of planned conversions in "
                f"{len(years)} year(s), {years[0]} through {years[-1]}?"):
            return 0
        self.schedule.blockSignals(True)
        try:
            for row in rows:
                self.schedule.delete_conversion(int(row["from_account_id"]),
                                                int(row["to_account_id"]),
                                                int(row["year"]))
        finally:
            self.schedule.blockSignals(False)
        self.schedule.reload()
        self.roth_notice.setText(
            f"Cleared {fmt_money(total)} of conversions in {len(years)} year(s).")
        self._conversions_changed()
        return len(years)

    def _clear_year_conversions(self, year: int) -> None:
        """Take a year's conversions off - the other half of the toggle."""
        year = int(year)
        had = sum(int(r["amount_cents"])
                  for r in retirement.list_conversions(self.conn, year))
        if not had:
            return
        for acct in self.schedule.sources():
            self.schedule.clear_conversions(acct.account_id, year)
        self.schedule.reload()
        self.roth_notice.setText(f"{year} conversions of {fmt_money(had)} removed.")
        self._plan_changed()
        self._sync_mix_button()

    def show_schedule_for(self, year: Optional[int]) -> None:
        """A bar was right-clicked (or the button pressed): open the conversion
        schedule's window, on that year when there is one."""
        self.schedule_window.open_on(year)

    # -- the income section's one gesture ----------------------------------
    def show_withdrawals_for(self, year: Optional[int]) -> None:
        """A bar was clicked (or the button pressed): open the withdrawal
        schedule's window, on that year when there is one.

        A MODELESS window (reported: the embedded schedules crowded the page).
        It used to be a panel so the editor never had to be dismissed before the
        bars could be looked at again; a modeless window keeps that - the page
        stays live beside it.
        """
        self.withdrawals_window.open_on(year)

    # -- the budget seam (SRD 5.12i) ----------------------------------------
    def stage_spending_basis(self, basis) -> bool:
        """Take a spending basis the Budget Planner arrived at, put it in the
        withdrawal schedule's spending box, and open that window so the user can
        see where it landed and what it says it is.

        This page is the only thing that touches both sides: the budget page
        EMITS a basis and imports nothing from :mod:`mammon.retirement`, so no
        indexed figure can leak into the budget code. Nothing is written here
        either - the schedule's own Apply is still what commits a plan.
        """
        staged = self.withdrawals.stage_spending_basis(basis)
        if staged:
            self.withdrawals_window.open_on(None)
        return staged

    def _plan_changed(self) -> None:
        """The stored plan changed under us: redraw without rebuilding the table."""
        self._reread_plan(reload_schedule=False)

    def irmaa_details(self, row) -> list[str]:
        """IRMAA both ways: what this year pays (set two years back), and
        what this year's income sets for the year two ahead."""
        out = []
        status = self.status_in(row.year)       # this year's return sets the tier
        paid = self.irmaa_by_year().get(row.year)
        if paid is not None:
            cents, tier, months, source = paid
            who = f"{months / 12:g} enrolled" if months % 12 == 0 else f"{months} enrolled months"
            appeal = (" (SSA-44 appeal)" if source == row.year else "")
            typed = (" (entered)" if source not in getattr(self, "_parts", {}) else "")
            out.append(f"Medicare surcharge (IRMAA): "
                       f"{fmt_money(cents) if cents else 'none'}"
                       + (f", tier {tier}" if tier else "")
                       + f", {who}, from {source} income{typed}{appeal}")
        magi = self.magi_cents(row.year, row.conversion_cents)
        if magi is None:
            return out
        for ahead in self.premiums_set_by(row.year):
            cents, tier, months = household_irmaa(self.conn, ahead, magi, status,
                                                  None, self.status_in(ahead))
            if not months:
                continue
            ceilings = retirement.irmaa_ceilings_cents(
                status, ahead, retirement.get_bracket_index_pct(self.conn))
            nxt = next((c for c in ceilings if c >= magi), None)
            room = (f"; {fmt_money(nxt - magi)} below tier {tier + 1}"
                    if nxt is not None else "")
            label = "its own" if ahead == row.year else f"{ahead}'s"
            out.append(f"Sets {label} IRMAA"
                       + (" (SSA-44 appeal)" if ahead == row.year else "") + ": "
                       + (f"tier {tier} (+{fmt_money(cents)})" if tier else "no surcharge")
                       + room)
        return out

    def year_details(self, year: int) -> str:
        """Everything about one year, for the charts' hover."""
        row = next((r for r in self.rows if r.year == int(year)), None)
        if row is None:
            return ""
        lines = [f"{row.year}" + (f"  (age {row.age})" if row.age is not None else "")]

        def add(label, cents):
            if cents:
                lines.append(f"{label}: {fmt_money(cents)}")

        add("Social Security", row.social_security_cents)
        add("Other income", row.other_income_cents)
        add("IRA / 401(k) draws", row.deferred_draw_cents)
        add("Taxable account draws", row.taxable_draw_cents)
        add("Roth draws", row.roth_draw_cents)
        add("Roth conversion", row.conversion_cents)
        lines.append(f"Taxable income: "
                     f"{fmt_money(self.taxable_with(row.year, row.conversion_cents))}")
        parts = getattr(self, "_parts", {}).get(row.year)
        if parts is not None and parts[0]:
            taxed = retirement.taxable_social_security_cents(
                parts[0], parts[1] + row.conversion_cents, self.status_in(row.year))
            lines.append(f"Social Security taxed: {fmt_money(taxed)} "
                         f"({taxed * 100 // parts[0]}%)")
        parts = self.tax_in(row.year, row.conversion_cents)
        tax = parts.total
        lines.append(f"Estimated federal income tax: {fmt_money(tax)}")
        senior = retirement.senior_deduction_cents(
            self.status_in(row.year), self.seniors_in(row.year),
            self.magi_cents(row.year, row.conversion_cents) or 0, row.year)
        if senior:
            lines.append(f"  senior deduction (2025-2028): {fmt_money(senior)}")
        penalty = self.penalty_in(row.year)
        if penalty:
            lines.append(f"  additional tax on early distributions "
                         f"(10%, before 59 1/2): {fmt_money(penalty)}")
        gains = self.gains_in(row.year)
        if gains > 0:
            lines.append(f"  capital gains realized {fmt_money(gains)}, "
                         f"taxed {fmt_money(parts.gains)}")
        elif gains < 0:
            lines.append(f"  net capital loss: {fmt_money(-gains)} off ordinary income")
        if self.exempt_in(row.year):
            lines.append(f"  tax-exempt interest: {fmt_money(self.exempt_in(row.year))} "
                         "(in provisional income and MAGI)")
        if parts.niit:
            lines.append(f"  net investment income tax (3.8%): {fmt_money(parts.niit)}")
        if parts.state:
            lines.append(f"  state income tax: {fmt_money(parts.state)}")
        status = self.status_in(row.year)
        if aca_marketplace_months(self.conn, row.year, status):
            magi = self.aca_magi(row.year, row.conversion_cents) or 0
            cliff = aca_cliff_in(self.conn, row.year, status)
            credit = aca_credit_in(self.conn, row.year, magi, status)
            lost = aca_cost(self.conn, row.year, magi, status,
                            baseline_magi_cents=self.aca_baseline(row.year))
            if magi > cliff:
                lines.append(f"ACA premium credit lost: {fmt_money(lost)} (over the cliff)")
            else:
                lines.append(f"ACA premium credit: {fmt_money(credit)}"
                             + (f", {fmt_money(lost)} less for this year's draws "
                                f"and conversions" if lost else "")
                             + f"; {fmt_money(cliff - magi)} below the cliff")
        if row.conversion_cents:
            without = self.tax_in(row.year, 0).total
            lines.append(f"  of which the conversion adds: {fmt_money(tax - without)}")
        plan = getattr(self, "_plan", None)
        entry = (next((e for e in plan.years if e.year == row.year), None)
                 if plan is not None else None)
        if entry is not None and entry.tax_cents:
            lines.append(f"Tax paid from the accounts: {fmt_money(entry.tax_cents)}")
        lines += self.irmaa_details(row)
        add("Invested funds entering the year", row.fund_value_cents)
        return "\n".join(lines)


    def _conversions_changed(self) -> None:
        """A conversion moved: redraw now, and re-apply the household plan once.

        A conversion changes what each account holds and how much bracket room
        is left for IRA draws, so the draws have to be recomputed. One edit can
        write several conversions (a bracket click spreads over every source),
        so the re-apply is coalesced onto the next tick."""
        self._plan_changed()
        if not self._reapply_pending:
            self._reapply_pending = True
            defer(self, self._reapply_after_conversions)


    def _reapply_after_conversions(self) -> None:
        self._reapply_pending = False
        self._income_changed()


    def _income_changed(self, from_year: Optional[int] = None) -> None:
        """Other income moved: re-apply the stored household plan, if any.

        The draws are the spending need LESS other income, so a new rental or a
        royalty that falls off changes every year's draw; leaving the old draws
        in place would chart income the plan no longer needs, or miss a gap.
        ``from_year`` leaves every earlier year as stored (a fill's re-apply).

        The account inflows are measured once for the whole re-apply: it
        re-read them some thirty times, most of each re-apply's seconds."""
        with inflows_measured_once():
            self._income_changed_measured(from_year)

    def _income_changed_measured(self, from_year: Optional[int] = None) -> None:
        params = retirement.get_withdrawal_plan(self.conn)
        if params.is_set:
            self.withdrawals.apply_household(
                params.start_cents, params.increase_pct,
                start_year=params.start_year, bracket_rate=params.bracket_rate,
                from_year=from_year)
        self._reread_plan(reload_schedule=False)
        # (The tax totals are the conversion chart's figures line; appended
        # here too, they repeated under the income chart - reported.)
        # The withdrawal table's draws changed too. Rebuilt NOW: nothing that
        # calls this is that table's own signal, and a deferred rebuild left a
        # second queued call behind the first - one that could outlive its page.
        self.withdrawals.reload()

    # -- the stale-page contract (same as the Investment Dashboard) ---------
    def mark_stale(self) -> None:
        self._stale = True
        if self.isVisible():
            self.refresh_if_stale()

    def refresh_if_stale(self) -> bool:
        if not self._stale:
            return False
        self.refresh()
        return True

    def changeEvent(self, event) -> None:
        """Redraw the charts when the app's theme changes. Reported: switched
        to light mode, the charts kept the dark theme's near-white tick labels
        and legends - matplotlib colors are set when a chart is DRAWN, so a
        chart has to be drawn again to follow the theme. Deferred and
        coalesced: a theme switch sends several of these."""
        super().changeEvent(event)
        if event.type() not in (QEvent.StyleChange, QEvent.PaletteChange)                 or not hasattr(self, "roth_chart"):
            return
        if not self.isVisible():
            # Hidden (another page is showing): redraw when shown, not now -
            # nothing on screen needs it, and a page that is not in use must
            # not reach for its data on every theme change.
            self._retheme_on_show = True
            return
        if not getattr(self, "_retheme_pending", False):
            self._retheme_pending = True
            defer(self, self._retheme_charts)

    def _retheme_charts(self) -> None:
        self._retheme_pending = False
        self._retheme_on_show = False
        if self.rows:
            self.income_chart.set_rows(self.rows)
            self._render_roth()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self.refresh_if_stale() and getattr(self, "_retheme_on_show", False):
            self._retheme_on_show = False
            self._retheme_charts()

    # -- the one remaining dialog ------------------------------------------
    def social_security_dialog(self) -> SocialSecurityDialog:
        """Build the Social Security dialog. Construction ONLY - see
        :meth:`_run_dialog`."""
        return SocialSecurityDialog(self.conn, self, today=self._today)

    def _run_dialog(self, dlg) -> None:
        """Show a dialog modally. The ONE overridable seam a headless test
        replaces, because an ``exec_()`` under the offscreen platform never
        returns."""
        dlg.exec_()

    def _open(self, dlg) -> None:
        dlg.changed.connect(self.mark_stale)
        try:
            self._run_dialog(dlg)
        finally:
            dlg.deleteLater()
        # The dialog may have changed a person or an earnings row, and it closes
        # rather than "accepts", so the page re-reads the plan on the way out
        # instead of trusting a return code.
        self.refresh()

    def income_dialog(self) -> "IncomeDialog":
        """Build the Income dialog. Construction ONLY - see :meth:`_run_dialog`."""
        return IncomeDialog(self.conn, self, today=self._today)

    def open_income(self) -> None:
        """Show the Income dialog; re-apply the plan once if anything changed."""
        dlg = self.income_dialog()
        try:
            self._run_dialog(dlg)
            changed = dlg.changed_anything
        finally:
            dlg.deleteLater()
        if changed:
            self._income_changed()
            self.planChanged.emit()         # a salary is a planned contribution

    def open_social_security(self) -> None:
        """Run the Social Security dialog; re-apply the plan if anything in it
        changed - a benefit, a claim age or one of the assumptions it now
        holds (COLA, bracket indexing, the shortfall) moves every year."""
        dlg = self.social_security_dialog()
        edits = []
        dlg.changed.connect(lambda: edits.append(True))
        self._open(dlg)
        if edits:
            self._income_changed()
            self.planChanged.emit()         # a birth year ends the plan's horizon

    # -- the FAQ -----------------------------------------------------------
    def open_faq(self) -> RetirementFaqWindow:
        """Open the Retirement FAQ beside this page.

        Not modal and not a dialog: it is the reference sheet for the screen
        underneath it, so it has to be readable WHILE the planner is, and the
        page keeps a reference only so the window is not garbage collected the
        moment this returns.
        """
        if self._faq is None:
            self._faq = RetirementFaqWindow(self, today=self._today)
            self._faq.destroyed.connect(self._faq_closed)
        self._faq.show()
        self._faq.raise_()
        return self._faq

    def _faq_closed(self, *_args) -> None:
        self._faq = None
