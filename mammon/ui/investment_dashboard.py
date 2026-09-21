"""The Investment Dashboard page: a donut ring with the portfolio's numbers
living *inside* its hole, a left band of inflow arrows, and four corner
launchers.

Why this shape rather than another stack of tables: the Investment Center
(``ui/investment_center.py``) already renders the tabular views -- allocation,
per-account performance, top holdings, drift. This page is deliberately NOT a
rehash of those. Everything it draws is either a *picture* of the portfolio
(the ring, the arrows) or a single line of numbers that no existing report
states in one place. Anything that wants a table is a corner launcher opening
the window that already owns that table.

The geometry, from the design:

* The ring owns the FULL page height -- nothing is stacked above or below it.
  The four corner launchers (2.6) are **overlays inside the ring area**, not
  rows of the page: a circle inscribed in a square leaves four corner boxes of
  side ``R * (1 - 1/sqrt(2))`` that the ring can never reach, so putting the
  launchers there costs the ring no vertical space at all. They were rows once,
  and the two rows ate ~80px off the ring's diameter for regions that were
  empty by construction.
* The ring is slid RIGHT of page center, because the left band is reserved for
  the inflow arrows, the mode buttons + What If, and the contribution
  thermometer. The band is a narrow fixed-width column (:data:`LEFT_BAND_WIDTH`)
  whose RIGHT EDGE sits on the vertical line tangent to the ring's left outer
  edge (reported: "I want them against the vertical line tangent to the left
  edge of the ring"), less :data:`BAND_RING_GAP` of clear air ("the arrow and
  thermometer need a spacer beteween them and the ring, maybe 50 pixels") -- so
  the arrows still read as *entering* it without appearing to touch it. That
  tangent is ``ring center x - R_outer``, which is why the outer radius has to
  be a pure function of the ring widget's rect -- see :func:`ring_outer_radius`.
* The ring is a **donut** -- inner radius :data:`RING_INNER_RADIUS` = 0.9435,
  i.e. a band 0.0565R wide: HALF the 0.113R it shipped at, which was itself one
  third of the 0.34R first drawn. The design pinned 0.62-0.68 to guarantee the
  hole could hold two charts and the center line; a *thinner* band only makes
  the hole bigger, so that requirement is satisfied with room to spare. The hole
  is a layout container, not empty space: :class:`RingArea` positions a child
  widget over it on every resize (:meth:`RingArea.hole_rect`), and that child
  holds the value-history chart above a blank strip on the horizontal midline,
  with the projection fan below. Both charts therefore grow with the inner
  radius automatically. That rect is NOT the inscribed square: it is widened by
  :data:`HOLE_WIDTH_SCALE` to the widest rectangle whose corners are still on or
  inside the inner circle (reported: "both plots have room to expand to the
  left"), trading height for the scarce axis of a time series. The center line
  is no longer a row of it -- see :data:`CENTER_FONT_SCALE`.
* **The ring canvas is stacked IN FRONT of the hole, not behind it.** The hole's
  two canvases are rectangles, so their corners stick out past the inner circle
  and used to be drawn over the band ("the corners of the plots overlap the
  ring"). Painting the band last hides those corners under it. What makes that
  survivable is the mask: :meth:`RingArea._mask_ring` gives the ring canvas a
  :class:`QRegion` of the ANNULUS it draws in -- the inner disc cut out, the
  corners outside ``R * RING_VIEW_LIMIT`` cut off -- and a Qt mask governs hit
  testing as well as painting, so the plots underneath keep their mouse events
  and the band keeps its wedge clicks. The corner launchers used to be re-raised
  above the ring; they are NOT any more (reported: "the left side corner boxes
  need to be in front of the arrow and thermometer panels but behind the ring
  segments"). The stack is three deep -- left band, corner launchers, ring --
  and that same mask is what still lets a corner click through: a corner box
  lies wholly outside the annulus, so the ring is not over it for the mouse even
  though it is over it for painting, which is exactly what an exploded wedge
  reaching into a corner needs.
* **The two period selectors live outside the hole**, above the top chart and
  below the bottom one (reported). They are children of :class:`RingArea`, hand
  placed in the crescent between the hole rect and the inner circle, so no part
  of the ring can cover them and no part of them eats a plot's height.
* The left band is positioned BY HAND (:meth:`InvestmentDashboardPage._layout_left_band`)
  rather than by a stretch layout, because each of its blocks aligns with
  something inside a *different* widget: the arrows center on the value-history
  canvas and the thermometer centers on the projection canvas, both of which
  live inside the hole. A stretch layout can only center them in the band, which
  is not the same line. The mode buttons and What If sit between the two, mode
  buttons on top.
* The thermometer is drawn at :data:`THERMOMETER_HEIGHT_SCALE` of the height an
  equal share of the band would give it -- it is a selector, not a chart, and a
  full-height column of gradient read as the page's main subject.
* The page's ACCOUNT SCOPE comes from one gear at the top, just left of the
  Performance Report corner (:meth:`InvestmentDashboardPage._build_gear`). It is
  the application's existing ``CustomizeDialog``, not a picker invented here, so
  "which accounts" means the same thing on this page as in every report window;
  its selection bounds the ring in both modes, both charts, the center line, the
  arrows and the fan, and ``None`` means every investment account.

**The hole's two charts take their chrome from the ACTIVE palette at draw
time.** The figure and axes stay transparent so the page's own themed
background shows through, but ticks, tick labels, axis labels, title, spines
and grid are resolved inside ``render()`` from ``charts._chart_palette()`` --
never captured at construction, because the page is re-rendered on a theme
change (``ui/widgets.py`` calls ``mark_stale``) and colors frozen at build
time leave matplotlib's near-black defaults unreadable on the dark palette
(reported). Those colors are imported from :mod:`mammon.ui.charts` rather than
copied, so the two chart families cannot drift apart.

**No small-slice grouping.** :class:`charts.SlicesPieCanvas` folds the smallest
categories into an *Other* wedge, which is right for an allocation pie and
wrong here: a 1% wedge on a ring this size is still tens of pixels of arc, and
the whole point of the ring is that every account (or security) is individually
clickable. :class:`RingCanvas` therefore overrides ``render`` outright rather
than tweaking the base's thresholds -- the base draws its wedges inline in
``render`` with no ``wedgeprops`` hook, so a donut needs the whole method
anyway. Everything structural is still inherited: the click wiring, the hover
tooltip machinery and the wedge bookkeeping.

**Colors are keyed by identity, not by position.** ``ring_colors`` assigns the
palette in sorted-key order, so an account keeps its color as values move
around beneath it, and the same key keeps its color across the
accounts|securities toggle. Sorting (rather than hashing) keeps every color in
a set distinct, which a hash would not.

**The arrows count, they do not fit a cadence.** An investment account with at
least :data:`ARROW_MIN_INFLOWS` positive external flows in the trailing
:data:`INFLOW_WINDOW_DAYS` days gets an arrow inscribed with the *actual* sum
of those flows -- not an annualized extrapolation of an inferred rate. No
cadence is detected, stored or named.

**What If is SCOPED, not portfolio-only.** The toggle used to gray itself out
whenever a ring wedge was selected, and turning it on cleared the filter. That
made the only question it is good for unanswerable (reported: "I want to be
able to do what if on an individual account. Otherwise being able to change the
inflow is meaningless as a tiny inflow in a small account can't move the needle
vs a large total"). Now a selection re-scopes the projection instead: starting
value, measured contribution, measured mix and the edited inflow all come
through the same selection, so both fans are about the same subject and a $100
change to a $5,000 account is a visible change to that account's curve.
Three things hold it together. :meth:`InvestmentDashboardPage._scoped_inflows`
charges an account only its own measured stream (and a security nothing at
all); :meth:`InvestmentDashboardPage._refresh_measured` re-measures the mix on
every refresh, so the baseline can never be the portfolio's while the What If
fan is one account's; and :class:`WhatIfBar` states the scope in words, because
a scoped fan misread as the portfolio's is a worse failure than the gray button
ever was. The filter itself is derived from the ring after every refresh
(:meth:`InvestmentDashboardPage._sync_filter_to_ring`) so the page cannot go on
projecting a subject no wedge is showing as selected.

**Every arrow that is drawn is editable and clickable while What If is on.**
Reported: "The inflow arrow edit is no longer working", which turned out to be
two independent failures that look identical on screen -- an arrow that is
painted and does nothing.

*The scope gate.* Editability used to be conditioned on the arrow's account
being inside :meth:`InvestmentDashboardPage._scoped_inflows`, on the argument
that an edit which cannot move the drawn fan should not be accepted. Selecting
any wedge therefore silenced every other account's arrow, and a security wedge
silenced all of them, with no visible reason. The rule is inverted now
(:meth:`InvestmentDashboardPage._arrow_editable`): drawn means editable, and
:meth:`InvestmentDashboardPage._focus_inflow` re-scopes the page -- to that
account's own wedge, or back out to the whole portfolio -- so the typed number
always lands somewhere the user can watch it land.

*The overlap.* The band is placed by hand, and the ring area is a later sibling
raised over it, so an arrow can be perfectly visible while some other widget
owns its pixels for the mouse. Two of those happened: the top-left corner
launcher reaching into the band's column, and a ``max(0, ...)`` clamp that
pushed a tall arrow stack up underneath the mode buttons.
:meth:`InvestmentDashboardPage._band_top_limit` supplies the real floor and the
furniture below the arrows gives way instead of the arrows moving into it.
Because nothing about this is visible in a screenshot, the tests assert it with
``page.childAt()`` at each editor's center rather than by checking geometry.

All money arithmetic is composed from the domain layer
(:mod:`mammon.investments`, :mod:`mammon.portfolio`); this module adds none of
its own beyond summing cents that the domain already returned.

Note on one citation: the design names ``investments.external_flows``; the
function actually lives at :func:`mammon.portfolio.external_flows` (the module
that also owns :class:`~mammon.portfolio.Performance`), and that is what this
page calls.
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from PyQt5.QtCore import Qt, QEvent, QPoint, QRect, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
    QPolygonF,
    QRegion,
)
from PyQt5.QtCore import QPointF, QRectF
from PyQt5.QtWidgets import (
    QAbstractSlider,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

# No `investments` import on purpose: every per-account valuation here goes
# through portfolio.account_valuation, which dispatches on the account's kind
# (a crypto wallet is not valued by the brokerage engine). Importing the
# brokerage engine directly is how the accounts ring lost crypto wallets.
from mammon import forecast, ledger, portfolio
from mammon.ui import charts, prefs, style
from mammon.ui.models import fmt_date

# --- the shape of the page, as constants so the tests can state them ---------

#: Inner radius of the donut as a fraction of the outer radius. The band is
#: therefore ``1 - 0.9435 = 0.0565R`` wide: HALF the 0.113R the ring shipped at
#: (reported: "reduce the ring width to half its current width"), which was
#: itself ONE SIXTH of the 0.34R first drawn (0.34/6 = 0.05667). Design 2.2
#: pinned 0.62-0.68 to keep the hole big enough for two charts and the center
#: line; a thinner band only enlarges the hole, so that constraint is met from
#: the other side.
RING_INNER_RADIUS = 0.9435
#: What the band used to be, kept so the tests can state the ratio rather than
#: re-hardcoding a number nobody can check.
RING_INNER_RADIUS_WAS = 0.66
#: What the explode offset used to be, kept so the test can state the RATIO
#: rather than re-hardcoding a number nobody can check.
SELECTED_EXPLODE_WAS = 0.04
#: How far a selected wedge is pulled out of the ring -- HALF what it shipped at
#: (reported: "the segments move out too much when clicked. Reduce the
#: displacement to half the current value"). The band is only 0.0565R wide now,
#: so 0.04R threw a wedge most of its own width clear of the ring and read as a
#: detached sliver; 0.02R still reads as pulled out without breaking the circle.
SELECTED_EXPLODE = SELECTED_EXPLODE_WAS / 2.0
#: Half-extent of the ring axes' data range. The pie is drawn in units of the
#: outer radius, so reserving the explode offset here -- and NEVER autoscaling --
#: is what keeps a selected wedge inside the canvas without changing the
#: radius. Pinning the limits is half of the fix for the shrinking ring; see
#: :meth:`RingCanvas.render`.
RING_VIEW_LIMIT = 1.0 + SELECTED_EXPLODE
#: Width of the left band (arrows, then mode + What If, then the thermometer).
#: Narrow on purpose: the arrows have to land close to the ring's rim.
LEFT_BAND_WIDTH = 150
#: Vertical gap between the band's blocks, and the band's own inset.
BAND_SPACING = 4

#: Space between a plot's title and its period dropdown (reported: the title
#: "is sometimes truncated by running into the time-range dropdown ... ensure
#: there is a small space"). BAND_SPACING's 4px is the page's general gutter and
#: read as a collision at the title's weight; this is the one gap that has to be
#: legible as a gap.
TITLE_GAP = 12
#: Clear air between the band's right edge and the ring's left outer tangent
#: (reported: "the arrow and thermometer need a spacer between them and the
#: ring, maybe 50 pixels"). The band stays RIGHT-ALIGNED to the tangent, just
#: offset by this -- the alignment rule is unchanged, the gap is new.
BAND_RING_GAP = 50

#: The two things What If makes editable, and what each one changes. Reported:
#: "when the What-If button is pressed draw line(arrows) from the button to the
#: arrow and the thermometer and label them 'change contributions' and 'change
#: asset mix' respectively." The words are the user's; they are the whole point
#: of the connectors, which exist to say what the mode just turned on.
CONNECTOR_TO_ARROWS = "change contributions"
CONNECTOR_TO_THERMOMETER = "change asset mix"
#: The connector line's own column in the band, and the arrowhead's size.
CONNECTOR_LINE_X = 13
CONNECTOR_HEAD = 7
CONNECTOR_WIDTH = 2
#: Labels sit beside the line, a size down: they annotate, and the controls they
#: point at are what should be read first.
CONNECTOR_FONT_SCALE = 0.85

#: Clear air between the Accounts/Securities switch and the top plot's title,
#: as a multiple of the switch's own height (reported: "moved higher so there is
#: significant space between them and the title of the top plot"). It was
#: BAND_SPACING, the page's 4px general gutter, which read as the two being one
#: stacked block.
#:
#: Expressed in rows rather than pixels so it keeps its proportion when the
#: page's font changes -- the same reasoning as CENTER_RAISE_LINES. It is a
#: REQUEST: how far the switch can actually rise is bounded by the inner circle
#: narrowing above it (see :meth:`RingArea.mode_row_rect`).
MODE_ROW_TITLE_GAP_ROWS = 1.5

#: The securities ring's cash slice. Reported: "add the cash wedge when
#: displaying securities."
#:
#: Without it the two rings totalled different money -- accounts summed to the
#: portfolio, securities summed to the priced holdings alone -- while the center
#: block showed the account-based total in both. Cash simply vanished, which is
#: exactly where an un-reinvested dividend goes.
#:
#: The key is a SENTINEL, not the string "CASH", because a real security may be
#: ticker CASH and the ring keys securities by symbol.
CASH_KEY = "__cash__"
CASH_LABEL = "Cash"
#: What the asterisk on a single security's value means (reported: "we need an
#: asterisk on the value that says 'dividends not reinvested' when that is the
#: case"). Its dividends were paid out rather than buying shares, so they are
#: not in the market value shown -- though they ARE in the return percentages,
#: which is the distinction the note exists to draw.
UNINVESTED_NOTE = "dividends not reinvested"
#: Smallest corner overlay worth reserving, in px.
#: Reported: "shrink the size of the 4 corner tiles so that they look more like
#: buttons than something that is actually trying to convey information."
#:
#: The box used to be the WHOLE corner offcut of the ring's bounding square,
#: which on a wide page is a large panel -- and a large panel reads as content.
#: These are launchers: their job is to be clicked, so they take this share of
#: that offcut and leave the rest as air. The title keeps its own step-down
#: search, so a smaller box means a smaller caption rather than a clipped one,
#: and the glyph drops out on its own below CORNER_ART_MIN.
CORNER_BOX_SCALE = 0.62
#: Inset from the page's outer corner, so a tile reads as sitting ON the page
#: rather than as a panel bolted to its edge.
CORNER_INSET = 8
#: Corner radius of a tile (reported: "add a border around them and round the
#: corners a bit more"). Well above the platform button's 2-3px, which is what
#: makes the set read as one family of buttons at a glance.
CORNER_RADIUS = 12
#: Border width of a tile. One crisp pixel from the palette's button border, in
#: place of the platform bevel, which at this size looked like a sunken panel.
CORNER_BORDER = 1
CORNER_MIN_SIZE = 24
#: How a corner launcher divides itself up. All four share these, which is the
#: whole point: the set has to read as one family -- same box, same margin, the
#: title on top in the same place, the themed graphic under it -- or four
#: hand-tuned layouts drift apart the first time one caption gets longer.
CORNER_PAD = 6
#: The share of the box the title may take before it is stepped down a size. The
#: graphic keeps the rest; below that the title would crowd it out entirely.
CORNER_TITLE_MAX_FRACTION = 0.58
#: Title sizes tried, largest first, as a multiple of the button's own font. The
#: title is meant to be PROMINENT (reported: the plain boxes were unreadable as
#: a set), so the search starts well above the body font and stops at the first
#: size whose word-wrapped block fits; the list is finite so the fit can never
#: loop.
CORNER_TITLE_SCALES = (1.30, 1.20, 1.10, 1.00, 0.90, 0.80)
#: Under this many px in either direction the graphic is dropped and the title
#: keeps the whole box: a 20px pie is a smudge, and an unreadable caption is a
#: worse outcome than no picture.
CORNER_ART_MIN = 22
#: How many category hues a corner graphic may use. Taken from the ring's own
#: wedge palette so the pies in the Rebalancing glyph are the ring's colors.
CORNER_WEDGE_COUNT = 4
#: The two asset mixes the Rebalancing glyph contrasts: the SAME colors in
#: DIFFERENT proportions (the user's words), so the pair reads as one portfolio
#: before and after the arrow rather than as two unrelated charts.
CORNER_PIE_DRIFTED = (0.46, 0.27, 0.17, 0.10)
CORNER_PIE_TARGET = (0.28, 0.24, 0.30, 0.18)
#: The thermometer is drawn at half the height an equal share of the band would
#: give it (see :meth:`InvestmentDashboardPage.thermometer_slot_height`): three
#: quarters of the two thirds it carried before (reported: "the thermometer can
#: be reduced in height to 3/4 its current height"). 2/3 * 3/4 = 1/2.
THERMOMETER_HEIGHT_SCALE = 0.5
#: Floor for the thermometer, scaled with it -- a floor above the scaled height
#: fights the ratio on a short page and silently wins, so it comes down by the
#: same three quarters (60 -> 45).
THERMOMETER_MIN_HEIGHT = 45

MODE_ACCOUNTS = "accounts"
MODE_SECURITIES = "securities"
RING_MODES = (MODE_ACCOUNTS, MODE_SECURITIES)

#: Inflows in the trailing year at or above this count make an account
#: "regular" and give it an arrow (design section 3). Four catches quarterly
#: and everything denser, and rejects a one-off rollover.
ARROW_MIN_INFLOWS = 4
INFLOW_WINDOW_DAYS = 365

#: The horizons the center line annualizes over, longest history first.
ANNUALIZED_YEARS = (1, 3, 5, 10)

#: The upper chart's period selector (design 2.3). ``None`` is "Max", which is
#: measured, not assumed -- see :func:`history_span`.
HISTORY_PERIODS = ((1, "1 year"), (2, "2 years"), (3, "3 years"), (5, "5 years"),
                   (8, "8 years"), (10, "10 years"), (None, "Max"))
#: Both plots open on 10 years (reported). The history default was 1 year, which
#: showed a nearly flat line and made the page look like it had no history; the
#: projection's was 20, so the two plots disagreed about what "the period" meant
#: on first open.
DEFAULT_HISTORY_YEARS = 10
#: Samples drawn across the selected period. Each one is a full valuation, so
#: this is a cost knob, not a cosmetic one: keep it small.
HISTORY_POINTS = 13
#: The furthest back "Max" will look. Nobody's brokerage predates it and the
#: probe that finds the start is a doubling search, so the cap is ~6 probes.
MAX_HISTORY_YEARS = 40

#: The lower chart's horizon selector (design 2.3).
PROJECTION_HORIZONS = (5, 10, 20, 30, 40, 50)
DEFAULT_PROJECTION_YEARS = 10   # see DEFAULT_HISTORY_YEARS

#: Slider units per ladder level: the ladder is continuous (design 4.5), so the
#: integer slider is a tenth-of-a-level grid over [0, MAX_RISK_LEVEL].
RISK_SLIDER_STEPS = 10

#: Fallback literals for the two hole charts. They are the LIGHT theme's values;
#: nothing should read them directly -- :func:`chart_colors` resolves the pair
#: from the palette that is active at DRAW time, so the same series is legible on
#: a white page and on a dark one. They survive as names because the arrows and
#: the old callers spell them.
HISTORY_LINE_COLOR = "#1f5fa8"
BASELINE_FAN_COLOR = "#1f5fa8"
WHAT_IF_FAN_COLOR = "#c0392b"
OUTLINE_COLOR = "#8a94a0"
#: Cold at the bottom (all cash), hot at the top (all stocks) -- design 2.5.
THERMO_COLD = "#2b6cb0"
THERMO_HOT = "#c0392b"

#: Type sizes for the two hole charts. Reported twice: "the numbers on the two
#: plots are too small to read", then "the legend is illegible ... increase the
#: tick fonts by another point". They started at 6pt ticks and a 7pt legend.
#:
#: :data:`PLOT_BASE_FONT_SIZE` is the plots' baseline -- the size body type in a
#: ~200px-tall plot would take. Everything a reader has to DECODE (the axis
#: numbers, the band names) is deliberately above it, and nothing in the plots
#: is below it; the tests assert that ordering rather than the literals, so the
#: next size report can move the numbers without rewriting the assertions.
PLOT_BASE_FONT_SIZE = 8
TICK_FONT_SIZE = 9
AXIS_LABEL_FONT_SIZE = 9
EMPTY_FONT_SIZE = 10
#: The fan's band legend (design 5.3/5.4). It used to be one size DOWN from the
#: ticks on the theory that it sits ON the plot and must not shout; it was
#: reported illegible at the dashboard's default window size, so it is now one
#: size UP -- the band names are prose, and prose needs more type than a number
#: to be read at the same distance.
LEGEND_FONT_SIZE = 10

#: How much bigger the center block's NUMBERS are than the page's type
#: (reported: "the centerline text needs a larger font too", then "increase the
#: font a bit"). They are the figures on the page that are read rather than
#: scanned, and they no longer share a rectangle with the plots, so they can
#: afford the size.
CENTER_FONT_SCALE = 1.45

#: The column headings sit at the page's own size -- deliberately SMALLER than
#: the numbers under them. A stat block reads fastest when the label recedes and
#: the figure carries the weight; matching their sizes makes the eye stop twice.
CENTER_HEADING_SCALE = 1.0

#: How far above the hole's midline the block sits, in lines of its own numbers
#: (reported: "raise the centerline text by about one line"). Expressed in lines
#: rather than pixels so it keeps its relationship to the type when the font
#: scale changes. The strip reserved for it in the hole moves by the same amount
#: -- see :meth:`InvestmentDashboardPage._sync_center_gap` -- or the top plot
#: would run under the raised text.
CENTER_RAISE_LINES = 1.0

#: Horizontal gap between the block's columns. Wide enough that "1-yr gain" and
#: "1-yr dividends" read as separate columns without a rule between them, which
#: is what carries the separation now that there are no lines.
CENTER_COLUMN_SPACING = 22

#: The hole is WIDER than the inscribed square (reported: "both plots have room
#: to expand to the left"). Widening it is bounded by the inner circle, not by
#: taste: half-width ``a`` and half-height ``b`` must satisfy
#: ``a^2 + b^2 <= r_inner^2`` or the plots' corners come out from under the ring
#: and paint over the band, which is the defect the INSCRIBED square was chosen
#: to prevent. :meth:`RingArea.hole_rect` therefore takes the width from this
#: and DERIVES the height, so the corners stay on the circle at any scale up to
#: sqrt(2) (~1.414); 1.10 buys the plots 10% of width for 11% of height, which
#: is the trade the report asked for.
HOLE_WIDTH_SCALE = 1.10

#: A SECOND widening, asked for as "stretch both plots by 10% of their width to
#: the left": the left edge moves out by this fraction of the hole's width and
#: the right edge stays where it was, so the hole is no longer centered in the
#: circle.
#:
#: This one cannot obey ``a^2 + b^2 <= r_inner^2`` and stay useful. A centered
#: rect is the TALLEST rect of a given width that fits a circle, so buying the
#: extra width entirely on the left is the expensive way to buy it: at the
#: current scale, holding the inner circle would cost 43% of the plots' height.
#: What the inner-circle rule was really protecting is narrower than the circle
#: -- a plot corner must not come out FROM UNDER THE RING and paint over the
#: left band -- so the stretch is bounded by the ring's OUTER edge
#: (:data:`RING_VIEW_LIMIT`) instead. Between the two radii the hole's corners
#: are behind the annulus, which is painted in front of it, and the only thing
#: hidden there is figure margin: matplotlib's axes box is inset from the
#: canvas, so no ink of the plots reaches those corners. Where even that cap
#: bites, the stretch is whatever fits -- this is a ceiling, not a promise.
HOLE_LEFT_STRETCH = 0.10

#: The axes box inside each hole canvas, as a fraction of the canvas width.
#:
#: Reported twice, and the second time is why these are literals instead of a
#: fraction derived from :data:`HOLE_WIDTH_SCALE`: "both plots have room to
#: expand to the left", then "increase the width by 10% and shift the center to
#: the left by half that amount". The HOLE cannot grow left to do that -- its
#: corners are capped at the ring's outer edge and are already against that cap,
#: and taking the growth anyway would put them 58px out over the left band. The
#: room the report is pointing at is INSIDE the canvas: the left margin was
#: reserving 23.6% of it for y tick labels that do not need anything like that.
#:
#: Measured at 9pt (:data:`TICK_FONT_SIZE`) on a 689px canvas: "$17,500" is
#: 52px, "$1,250,000" is 72px, "$12,500,000" is 80px -- so 0.161 (111px) clears
#: an eight-figure portfolio with 31px to spare. The axes box therefore grows
#: 10% wider, entirely leftward, which moves its center left by half of that --
#: the asked-for transformation, applied where it is free.
PLOT_AXES_LEFT = 0.161
PLOT_AXES_RIGHT = 0.99

#: The fan's bands, named by the percentiles design 5.3 actually draws. "1 sigma"
#: would be a lie twice over: the fan is lognormal, not normal, and 5.4 requires
#: the outer band to be called out as a MODEL band because Fenton-Wilkinson is
#: optimistic in the tails.
FAN_MEDIAN_LABEL = "median (50th pct)"
FAN_INNER_LABEL = "25th-75th pct"
FAN_OUTER_LABEL = "5th-95th pct (model)"
FAN_BASELINE_LABEL = "measured (5th/50th/95th)"

RING_EMPTY_TEXT = "No investment holdings to show"
EMPTY_TEXT = "No investment accounts yet"
HISTORY_EMPTY_TEXT = "No value history"
PROJECTION_EMPTY_TEXT = "Nothing to project"
WHAT_IF_TOOLTIP = ("Try different contributions and a different mix. Nothing "
                   "here is ever written to the database.")
#: What the What If bar says it is acting on when no wedge is selected. It is
#: the same words :meth:`InvestmentDashboardPage.filter_subject` uses for the
#: center line, so the two readouts can never disagree about the scope.
WHAT_IF_ALL_SUBJECT = "All investments"
#: Prefix on the What If bar's scope label. What If is scoped now, so the bar
#: has to NAME its scope: a fan drawn for one small account and read as the
#: whole portfolio's is the misreading this label exists to prevent.
WHAT_IF_SCOPE_PREFIX = "on "

#: The two hole charts' captions, asked for as "I need a title for the top plot,
#: left of the time-range dropdown ... put 'Projected Future Value' in front of
#: the time-range of the bottom plot". Both sit on the same row as the chart's
#: own period selector, in :class:`PlotHeader`.
#:
#: The top one NAMES THE SCOPE -- "Total Performance", "<account> Performance",
#: "<TICKER> Performance" -- because the plot itself has no other way to say
#: which of the three it is drawing, and a wedge-scoped curve read as the whole
#: portfolio's is the misreading a title is cheapest at preventing. The word
#: used for "no wedge selected" is "Total" rather than
#: :data:`WHAT_IF_ALL_SUBJECT`: this is a possessive in a headline, not the
#: What If bar's sentence fragment.
TOTAL_TITLE_SUBJECT = "Total"
PERFORMANCE_TITLE_SUFFIX = "Performance"
PROJECTION_TITLE = "Projected Future Value"
#: Under the fan's header row, full width and a size down (5.4 requires the fan
#: to be legible AS A MODEL). The user dictated this sentence; it is stored
#: whole so a test can assert it verbatim, and the only edit made to it was the
#: spelling of "Disclaimer".
PROJECTION_DISCLAIMER = (
    "Disclaimer: projections show estimated future performance ranges based on "
    "risk models, but no model can predict the actual future.")
#: The disclaimer's type, as a fraction of the page's. Small enough to read as
#: fine print, not so small it becomes decoration.
NOTE_FONT_SCALE = 0.85


def performance_title(subject: str) -> str:
    """The top plot's title for ``subject`` -- what
    :meth:`InvestmentDashboardPage.filter_subject` returns.

    One function so the three scopes cannot drift apart: the unfiltered subject
    (and an empty one, which is what an unseeded page has) becomes
    :data:`TOTAL_TITLE_SUBJECT`, and everything else -- an account name, a
    ticker -- is used as the user's own word for it."""
    name = (subject or "").strip()
    if not name or name == WHAT_IF_ALL_SUBJECT:
        name = TOTAL_TITLE_SUBJECT
    return f"{name} {PERFORMANCE_TITLE_SUFFIX}"


#: The four corner launchers (2.6). They are overlays in the ring area's four
#: corners, where the circle cannot reach, so they cost the ring no height.
CORNER_NAMES = ("cornerTopLeft", "cornerTopRight", "cornerBottomLeft",
                "cornerBottomRight")

#: What each corner says and what it opens (SRD 5.8k). The four captions are the
#: user's own words and the pairing is theirs too -- the two tax/performance
#: REPORTS on the right-hand diagonal's ends, the two things you go and CHANGE
#: (the mix, the classifications) on the other. Held as data, not as four
#: hand-built buttons, so a test can state the mapping instead of counting
#: children.
CORNER_LABELS = {
    "cornerTopLeft": "Capital Gains and Taxes",
    "cornerTopRight": "Performance Report",
    "cornerBottomLeft": "Set Asset Categories",
    "cornerBottomRight": "Explore Rebalancing",
}
#: Corner -> the name of the page method it calls. The methods are small and
#: overridable so a test can assert the launch without entering a modal loop:
#: a ``QDialog.exec_()`` under the offscreen platform never returns.
CORNER_ACTIONS = {
    "cornerTopLeft": "open_capital_gains",
    "cornerTopRight": "open_performance_report",
    "cornerBottomLeft": "open_asset_categories",
    "cornerBottomRight": "open_rebalancing",
}
CORNER_TOOLTIPS = {
    "cornerTopLeft": ("Which lots are long-term and which are short, when each "
                      "short lot turns long, and what selling it first costs."),
    "cornerTopRight": "Gain, income and annual return per holding.",
    "cornerBottomLeft": ("Asset Allocation, opened on By security -- where a "
                         "security is given its asset class."),
    "cornerBottomRight": "Your target asset mix and how far the real one has drifted.",
}
#: Corner -> the themed graphic painted under its title. The value names the
#: ``CornerButton._paint_<glyph>`` method that draws it, which is how
#: :meth:`CornerButton.paintEvent` dispatches. Each one is a picture of what the corner
#: OPENS, not decoration: the drift-to-target pies for the rebalancer, the taxed
#: share of a gain for the capital-gains report, a rising series for the
#: performance report, a legend of named classes for the asset categories.
#: Held as data beside the captions and the actions so the four cannot drift out
#: of step, and so a test can state the mapping.
CORNER_GLYPHS = {
    "cornerTopLeft": "tax",
    "cornerTopRight": "performance",
    "cornerBottomLeft": "categories",
    "cornerBottomRight": "rebalance",
}

# A label that can never collide with a real account name or ticker, so
# charts.wedge_colors never pins one of our keys to its reserved "Other" slot.
_NO_GROUP = "\x00__no_group__"


# --- small formatters (display only -- no money arithmetic lives here) -------
def fmt_money(cents: int) -> str:
    """Integer cents -> ``$1,234.56``, matching the register's money display."""
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def fmt_signed(cents: int) -> str:
    """A gain or loss, always carrying its sign so a negative year reads as one."""
    return ("+" if cents >= 0 else "") + fmt_money(int(cents))


def fmt_pct(pct: Decimal) -> str:
    """A percentage at one decimal. Never ``Decimal.normalize`` -- it renders
    ``50`` as ``5E+1``."""
    return f"{Decimal(pct).quantize(Decimal('0.1'))}%"


def parse_money(text: str) -> int:
    """``$1,234.56`` -> ``123456``. Raises ``ValueError`` on anything else.

    Only the What If arrows use this, and what they produce never reaches the
    database -- but it is still money, so it rounds ROUND_HALF_UP at the cents
    boundary through Decimal and never through float."""
    s = str(text).strip().replace(",", "").replace("$", "").replace("+", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not s:
        raise ValueError("no amount")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    return int(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def years_before(iso: str, years: int) -> str:
    """``iso`` less ``years`` calendar years, Feb 29 stepping back to Feb 28."""
    d = _dt.date.fromisoformat(iso)
    try:
        return d.replace(year=d.year - years).isoformat()
    except ValueError:                      # 29 Feb -> a non-leap year
        return d.replace(year=d.year - years, day=28).isoformat()


def _today() -> str:
    return _dt.date.today().isoformat()


def ring_colors(keys) -> dict:
    """``{key: color}``, deterministic and stable.

    The palette is handed out in SORTED key order, so (a) every key in a set
    gets a distinct color up to the palette's length, and (b) a key keeps its
    color when values move, when the ring is refreshed, and across the
    accounts|securities toggle wherever the identity is unchanged. A hash-to-
    palette mapping would give (b) but not (a)."""
    ordered = sorted({str(k) for k in keys})
    return dict(zip(ordered, charts.wedge_colors(ordered, group_label=_NO_GROUP)))


def ring_outer_radius(width: int, height: int) -> float:
    """The donut's outer radius in pixels, for a ring canvas of ``width`` x
    ``height``.

    THE one definition. The hole, the corner boxes, the period selectors and
    the left band's right edge all derive from this, and :meth:`RingCanvas.render`
    pins matplotlib so that the circle actually drawn matches it.

    It is a pure function of the rect and the constants -- no slice count, no
    label metrics, no autoscale. It was not always: the ring inherited
    ``Figure(tight_layout=True)`` from :class:`charts.SlicesPieCanvas`, and a
    live layout engine re-fits the axes at EVERY draw, so each re-render shrank
    the circle a little more (measured: 117px -> 99px -> 85px -> 71px across
    four renders). Switching Accounts|Securities re-renders, which is exactly
    what the user saw: "every time I switch from Accounts to Securities, the
    radius of the ring shrinks."

    The axes box is square (``adjustable="box"``) and centered, so the radius is
    half the SHORT side, scaled down by :data:`RING_VIEW_LIMIT` because the data
    range reserves room for an exploded wedge."""
    return min(width, height) / 2.0 / RING_VIEW_LIMIT


# ---------------------------------------------------------------------------
# the ring
# ---------------------------------------------------------------------------
class RingCanvas(charts.SlicesPieCanvas):
    """The donut. One wedge per account (or per security), no grouping, no
    inline labels -- identification is the hover tooltip inherited from the
    base, and the color correspondence with the charts in the hole.

    Slices arrive as ``[(key, label, cents)]``: the *key* is the stable
    identity (an account id as a string, or a ticker) that coloring and the
    click filter work in, the *label* is what a human reads in the tooltip.

    ``render`` is overridden whole rather than parameterised, because the base
    draws ``ax.pie`` inline with no ``wedgeprops`` seam; a donut cannot be
    reached from the base as it stands. The base's ``_on_click`` (drill into
    Other / zoom out) is replaced by wedge selection, which is this ring's
    entire interaction.
    """

    #: emitted with the selected key, or None when the selection was cleared
    sliceClicked = pyqtSignal(object)

    # Class-level defaults: the base's __init__ calls render() before this
    # subclass's __init__ body has run, so render() must find these.
    _keys: dict = {}
    _labels: dict = {}
    _color_by_key: dict = {}
    _selected = None

    def __init__(self, slices=(), parent=None, *, empty_text=RING_EMPTY_TEXT):
        super().__init__("", [], parent, empty_text=empty_text,
                         group_target_pct=0.0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.set_slices(slices)

    # -- state ------------------------------------------------------------
    def set_slices(self, slices) -> None:
        """Replace what the ring is a picture of. A selection that survives the
        new slice set is kept, so a refresh does not silently drop the filter."""
        rows = [(str(k), str(lab), int(c)) for k, lab, c in slices if int(c) > 0]
        self._keys = {lab: k for k, lab, _ in rows}
        self._labels = {k: lab for k, lab, _ in rows}
        self._color_by_key = ring_colors([k for k, _, _ in rows])
        if self._selected is not None and self._selected not in self._labels:
            self._selected = None
        self._base = [(lab, c) for _, lab, c in rows]
        self._stack = []
        self.render()

    def keys(self) -> list:
        """The slice keys, in drawn order."""
        return [self._keys[lab] for lab, _ in self._drawn]

    def wedge_count(self) -> int:
        return len(self._wedges)

    def color_for(self, key) -> str:
        return self._color_by_key.get(str(key), "#999999")

    def wedge_colors(self) -> dict:
        """``{key: color}`` for what is currently drawn."""
        return {k: self.color_for(k) for k in self.keys()}

    def selected(self):
        return self._selected

    # -- interaction --------------------------------------------------------
    def label_at_event(self, event):
        """The wedge label under a matplotlib event, or None off the ring."""
        for label, wedge in self._wedges:
            hit, _ = wedge.contains(event)
            if hit:
                return label
        return None

    def _on_click(self, event) -> None:
        label = self.label_at_event(event)
        self.pick(self._keys.get(label) if label is not None else None)

    def pick(self, key) -> None:
        """Select ``key``; selecting the current selection (or ``None``, which
        is what a click on the ring's background gives) clears back to the
        whole portfolio. Emits :attr:`sliceClicked` either way."""
        key = None if key is None else str(key)
        new = None if key is None or key == self._selected else key
        self._selected = new
        self.render()
        self.sliceClicked.emit(new)

    def pick_label(self, label) -> None:
        """Select by human label -- what a click on that wedge would do."""
        self.pick(self._keys.get(str(label)))

    # -- drawing ------------------------------------------------------------
    def _pinned_axes(self):
        """A fresh axes that always occupies the same square of the canvas.

        Three things are pinned, and all three are load-bearing:

        * **No layout engine.** The base class builds ``Figure(tight_layout=True)``
          (``ui/charts.py``, which this page must not edit), and a
          ``TightLayoutEngine`` re-fits the axes on every single draw. Because
          ``render`` is called again on each mode switch, refresh and selection,
          the ring got measurably smaller each time -- the reported "the radius
          of the ring shrinks". ``set_layout_engine("none")`` stops it.
        * **A free axes, not a subplot.** ``add_axes`` has no subplotspec, so
          nothing downstream (``subplots_adjust``, a stray ``tight_layout``) can
          move it off the full-bleed rect.
        * **Box aspect with fixed limits.** ``ax.axis("equal")`` is
          ``set_aspect("equal", adjustable="datalim")``: it keeps the circle
          round by WIDENING the data range, which shrinks the drawn radius on a
          non-square canvas and compounds with every redraw. ``adjustable="box"``
          keeps the data range fixed and shrinks the AXES to a centered square
          instead, so the radius is exactly :func:`ring_outer_radius` of the
          widget's rect in both modes and after any number of renders."""
        fig = self.figure
        fig.clear()
        fig.set_layout_engine("none")
        fig.patch.set_alpha(0.0)
        ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
        ax.patch.set_alpha(0.0)
        ax.set_axis_off()
        ax.set_aspect("equal", adjustable="box", anchor="C")
        ax.set_xlim(-RING_VIEW_LIMIT, RING_VIEW_LIMIT)
        ax.set_ylim(-RING_VIEW_LIMIT, RING_VIEW_LIMIT)
        ax.set_autoscale_on(False)
        return ax

    def render(self) -> None:
        """Draw the donut. Transparent figure and axes, so the page's theme
        (light or dark) shows through without this canvas reading the palette --
        which matters more now that the ring is painted OVER the hole's charts."""
        ax = self._pinned_axes()
        self._wedges, self._grouped, self._tooltips = [], [], {}
        slices = self.current_slices()
        total = sum(c for _, c in slices)
        self._drawn = list(slices)
        if not slices or total <= 0:
            self._drawn = []
            ax.text(0.0, 0.0, self._empty_text, ha="center", va="center",
                    fontsize=10, color="#8a94a0")
            self.draw_idle()
            return
        labels = [lab for lab, _ in slices]
        sizes = [c for _, c in slices]
        colors = [self.color_for(self._keys.get(lab)) for lab in labels]
        explode = [SELECTED_EXPLODE if self._keys.get(lab) == self._selected
                   else 0.0 for lab in labels]
        self._tooltips = {
            lab: f"{lab}: {c / total * 100.0:.1f}% of total, {fmt_money(c)}"
            for lab, c in slices
        }
        wedges, *_ = ax.pie(
            sizes, labels=None, colors=colors, explode=explode, startangle=90,
            counterclock=False,
            wedgeprops={"width": 1.0 - RING_INNER_RADIUS, "linewidth": 0.5,
                        "edgecolor": "#ffffff"})
        self._wedges = list(zip(labels, wedges))
        # ``Axes.pie`` sets its own limits (+-1.25) and calls ``set_aspect`` on
        # the way out, so the pinning is re-applied AFTER it, never before.
        ax.set_aspect("equal", adjustable="box", anchor="C")
        ax.set_xlim(-RING_VIEW_LIMIT, RING_VIEW_LIMIT)
        ax.set_ylim(-RING_VIEW_LIMIT, RING_VIEW_LIMIT)
        ax.set_position((0.0, 0.0, 1.0, 1.0))
        self.draw_idle()


class RingArea(QWidget):
    """The ring, the widget that lives in its hole, the two period selectors
    and the four corner overlays.

    The hole is not a layout cell -- the canvas is one rectangle -- so the hole
    widget is a child placed by hand over the inscribed square of the donut's
    inner circle on every resize. Inscribed, not bounding: the corners of a
    bounding square would sit outside the hole and under the ring band.

    **Stacking (reported).** Three levels, bottom to top: the left band (the
    arrows and the thermometer, which are the PAGE's children, not this
    widget's), then the four corner launchers, then the ring canvas -- "the left
    side corner boxes need to be in front of the arrow and thermometer panels
    but behind the ring segments". The page raises this whole widget over the
    band, and :meth:`_mask_area` masks it down to the union of its own children,
    so a corner box covers an arrow while a click that lands on neither still
    falls through to the band. The mask is what does that, NOT
    ``WA_TransparentForMouseEvents``: Qt skips a mouse-transparent widget's
    WHOLE SUBTREE when it picks a mouse receiver, so setting that attribute here
    killed every control this widget owns (and, since the page's band once
    carried it too, every control the band owns).

    The ring canvas is raised ABOVE the hole and above the corners, and is
    translucent and MASKED to the ANNULUS between the inner circle and the
    outer view limit. The mask does four things at once: the charts show
    through the hole; the ring's rect stops swallowing the band and the corner
    boxes, which now sit outside the mask entirely; an exploded wedge still
    paints over the corner box it reaches into, because the annulus extends to
    ``R * RING_VIEW_LIMIT``; and -- because a Qt mask governs hit testing too --
    clicks inside the hole reach the charts while clicks on the band of the
    donut still reach the wedges. (The cost is that a click far outside the
    ring no longer clears the filter; one just outside the drawn band still
    does, and so does clicking the selected wedge again.)

    The selectors, the center line and the account gear are re-raised above the
    ring afterwards: they are controls, and a control under a mask hole is
    still a control nobody can hit.

    The corner launchers are placed like the hole, for the opposite reason: the
    ring is a circle in a square, so each corner of that square holds a box of
    side ``R * (1 - 1/sqrt(2))`` that no wedge can ever occupy, plus whatever
    slack the non-square aspect ratio leaves on the long axis. Overlaying them
    there is what lets this widget own the page's full height -- as rows above
    and below the ring, they cost it diameter."""

    #: Where a period selector sits relative to the hole.
    SELECTOR_SLOTS = ("top", "bottom")

    def __init__(self, ring: RingCanvas, hole: QWidget, parent=None):
        super().__init__(parent)
        self.ring = ring
        self.hole = hole
        self.corners: dict = {}
        self.selectors: dict = {}
        self.center = None
        self.gear = None
        self.mode_row = None
        # Set while a zero-geometry mask retry is queued; see _defer_mask.
        self._mask_pending = False
        ring.setParent(self)
        hole.setParent(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(ring)
        # A matplotlib canvas is opaque by default (the Qt backend sets
        # WA_OpaquePaintEvent), which over another widget would paint the hole
        # out in garbage. Turn that off so the transparent figure really is
        # transparent, then raise it over the hole.
        ring.setAttribute(Qt.WA_OpaquePaintEvent, False)
        ring.setAttribute(Qt.WA_TranslucentBackground, True)
        ring.setAutoFillBackground(False)
        ring.raise_()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # -- geometry -----------------------------------------------------------
    def outer_radius(self) -> float:
        """The drawn ring's outer radius in pixels -- see
        :func:`ring_outer_radius`. Everything placed against the ring goes
        through here, so the hole, the corners, the selectors and the left
        band's right edge cannot disagree with what matplotlib drew."""
        return ring_outer_radius(self.width(), self.height())

    def left_tangent_x(self) -> int:
        """x of the vertical line tangent to the ring's LEFT outer edge, in this
        widget's coordinates. The left band's right edge lands here (reported)."""
        return int(round(self.width() / 2.0 - self.outer_radius()))

    def hole_rect(self):
        """The plots' rectangle inside the donut's hole, in this widget's coords.

        A RECTANGLE, no longer the inscribed square: the plots were reported to
        have "room to expand to the left", and the constraint that actually
        binds is the inner circle, not squareness. With half-width ``a`` and
        half-height ``b``, the corners stay on or inside the circle while
        ``a^2 + b^2 <= r_inner^2``; :data:`HOLE_WIDTH_SCALE` sets ``a`` as a
        multiple of the square's half-side and the height is DERIVED from that
        inequality, so widening can never push a chart corner out from under the
        ring and over the band.

        Then the rect is stretched LEFT by :data:`HOLE_LEFT_STRETCH` of its own
        width -- left edge out, right edge and height unchanged, so the hole is
        deliberately off-center in the circle. That stretch is bounded by the
        ring's OUTER radius rather than its inner one (see the constant): the
        left corners end up behind the annulus, which paints in front of them,
        and they still never reach the band."""
        w, h = self.width(), self.height()
        radius = self.outer_radius()
        r_inner = radius * RING_INNER_RADIUS
        half = r_inner / math.sqrt(2.0)          # the inscribed square's half-side
        half_w = min(half * HOLE_WIDTH_SCALE, r_inner)
        # b = sqrt(r^2 - a^2); max(0.0, ...) only guards float dust at a == r.
        half_h = math.sqrt(max(0.0, r_inner * r_inner - half_w * half_w))
        # Round each side to whole pixels FIRST, then center that integer rect:
        # centering the float and truncating afterwards loses a pixel off one
        # side and leaves the hole visibly off-center inside the donut.
        rect_w = int(2.0 * half_w)
        rect_h = int(2.0 * half_h)
        x = (w - rect_w) // 2
        y = (h - rect_h) // 2
        # The left stretch. The cap is the x at which the top-left/bottom-left
        # corner would sit exactly on the ring's outer edge: sqrt(L^2 + b^2) =
        # R * RING_VIEW_LIMIT, so L = sqrt((R*limit)^2 - b^2). Past that the
        # corner leaves the annulus and lands on the left band.
        limit = radius * RING_VIEW_LIMIT
        max_left = math.sqrt(max(0.0, limit * limit - half_h * half_h))
        grow = int(round(rect_w * HOLE_LEFT_STRETCH))
        grow = max(0, min(grow, int(x - (w / 2.0 - max_left))))
        x -= grow
        rect_w += grow
        if x < 0:                    # a narrow page: never start off-widget
            rect_w += x
            x = 0
        return (x, y, max(1, rect_w), rect_h)

    def center_rect(self):
        """The center line's OWN rectangle, laid over the plots' rectangle.

        Reported: the center line "needs its own rectangle on top of the plot
        rectangle so it can extend the full width of the circle". So it spans
        the inner circle's full width -- ``2 * r_inner``, wider than any hole
        rect can be -- centered on the hole's vertical midline, and it is a child
        of THIS widget rather than a row in the hole's layout, which is what
        stops the hole's width from clipping it."""
        w, h = self.width(), self.height()
        r_inner = self.outer_radius() * RING_INNER_RADIUS
        rect_w = max(1, int(2.0 * r_inner))
        widget = self.center
        hint = widget.sizeHint().height() if widget is not None else 0
        rect_h = max(1, hint)
        return ((w - rect_w) // 2, (h - rect_h) // 2 - self.center_lift(),
                rect_w, rect_h)

    def center_lift(self) -> int:
        """Pixels the center block sits ABOVE the midline (reported: "raise the
        centerline text by about one line").

        One number, read by both things that have to agree about it: this
        widget, which places the overlay, and the page, which reserves the blank
        strip under it in the hole's layout. Lifting the overlay alone would
        slide the text off its reserved strip and under the top plot."""
        widget = self.center
        if widget is None:
            return 0
        return int(round(CENTER_RAISE_LINES * widget.line_height()))

    def mode_row_rect(self):
        """``(x, y, w, h)`` for the Accounts/Securities switch, INSIDE the ring
        near the top (reported: "let's move the Accounts/Securities button
        inside the ring near the top").

        Centered on the hole like the captions are, and stacked above the top
        caption in the crescent between the hole's top edge and the inner
        circle. It sits there rather than in the left band because the switch
        says what the RING's wedges are, and a control that renames the wedges
        belongs with them."""
        widget = self.mode_row
        if widget is None:
            return None
        hint = widget.sizeHint()
        mw = max(1, min(hint.width(), max(1, self.width())))
        mh = max(1, hint.height())
        hx, hy, hw, hh = self.hole_rect()
        top = self.selector_rects().get("top")
        above = top[1] if top else hy
        y = above - int(round(MODE_ROW_TITLE_GAP_ROWS * mh)) - mh
        # ...but no higher than the inner circle stays wide enough to hold it.
        # The circle narrows as it rises, so a row placed by the gap alone would
        # eventually have its top corners out past the inner edge and onto the
        # annulus, which is painted in front of it -- the corners would simply
        # disappear under the ring. Solving r_inner^2 = dy^2 + (mw/2)^2 for the
        # highest y whose half-width still covers the row:
        cy = self.height() / 2.0
        r_inner = self.outer_radius() * RING_INNER_RADIUS
        room = r_inner * r_inner - (mw / 2.0) ** 2
        if room > 0:
            y = max(y, int(math.ceil(cy - math.sqrt(room))))
        else:
            y = max(y, 0)          # wider than the circle anywhere; keep it low
        return (hx + (hw - mw) // 2, max(0, min(y, above - mh)), mw, mh)

    def set_mode_row(self, widget) -> None:
        """Adopt the mode switch as a raised overlay, like the gear."""
        widget.setParent(self)
        widget.setMinimumSize(0, 0)
        self.mode_row = widget
        self._place_children()

    def gear_rect(self):
        """``(x, y, w, h)`` for the account gear: top of the page, immediately
        left of the top-right corner launcher (reported -- "put the gear at the
        top just left of the Performance Report button"). Out there it is clear
        of the ring's annulus mask, so nothing steals its clicks."""
        widget = self.gear
        if widget is None:
            return None
        hint = widget.sizeHint()
        gw = max(1, hint.width())
        gh = max(1, hint.height())
        right = self.corner_rects()["cornerTopRight"][0]
        x = max(0, right - BAND_SPACING - gw)
        return (x, 0, gw, gh)

    def corner_rects(self) -> dict:
        """``{name: (x, y, w, h)}`` for the four boxes the ring cannot reach.

        The box is the corner square left outside the inscribed circle, widened
        (or heightened) by the slack the ring's square leaves on the longer
        axis, so a wide page gives the launchers a wide box rather than wasting
        it."""
        w, h = self.width(), self.height()
        radius = self.outer_radius()
        side = radius * (1.0 - 1.0 / math.sqrt(2.0))
        box_w = max(CORNER_MIN_SIZE, int(side + (w - 2.0 * radius) / 2.0))
        box_h = max(CORNER_MIN_SIZE, int(side + (h - 2.0 * radius) / 2.0))
        box_w = min(box_w, max(1, w // 2))
        box_h = min(box_h, max(1, h // 2))
        # Take a SHARE of the offcut and inset it from the page edge, so the
        # tile is a button sitting in the corner rather than a panel filling it.
        box_w = max(CORNER_MIN_SIZE, int(box_w * CORNER_BOX_SCALE))
        box_h = max(CORNER_MIN_SIZE, int(box_h * CORNER_BOX_SCALE))
        pad = CORNER_INSET
        right, bottom = w - box_w - pad, h - box_h - pad
        return {
            "cornerTopLeft": (pad, pad, box_w, box_h),
            "cornerTopRight": (right, pad, box_w, box_h),
            "cornerBottomLeft": (pad, bottom, box_w, box_h),
            "cornerBottomRight": (right, bottom, box_w, box_h),
        }

    def set_corners(self, corners: dict) -> None:
        """Adopt the four launcher widgets as raised overlays. Raised, not
        merely reparented: the ring canvas is painted over the whole area, so an
        un-raised corner would be invisible AND unhittable."""
        for name, widget in corners.items():
            if name not in CORNER_NAMES:
                raise ValueError(f"unknown corner {name!r}; one of {CORNER_NAMES}")
            widget.setParent(self)
            # Any minimum inherited from an earlier layout would clamp
            # setGeometry and push the box back over the ring.
            widget.setMinimumSize(0, 0)
            self.corners[name] = widget
        self._place_children()

    def set_selectors(self, top=None, bottom=None) -> None:
        """Adopt the two period selectors (reported).

        They used to be rows inside the hole's charts, where the ring -- now
        painted in front -- would cover them, and where they ate chart height.
        Here they are this widget's children, placed in the crescent between the
        hole's inscribed square and the inner circle: the top chart's above the
        hole, the projection's below it. Only the placement moves; the combo
        objects, their entries and their signal wiring are untouched."""
        for slot, widget in (("top", top), ("bottom", bottom)):
            if widget is None:
                continue
            widget.setParent(self)       # setParent hides it again
            widget.setMinimumSize(0, 0)
            widget.show()
            self.selectors[slot] = widget
        self._place_children()

    def set_center(self, widget) -> None:
        """Adopt the center line as a full-hole-width overlay (reported).

        It used to be a row in the hole's layout, sharing the plots' rectangle,
        which clipped the longest lines at the hole's width. Here it is this
        widget's child at :meth:`center_rect`, so it can run the full width of
        the circle."""
        widget.setParent(self)               # setParent hides it again
        widget.setMinimumSize(0, 0)
        widget.show()
        self.center = widget
        self._place_children()

    def set_gear(self, widget) -> None:
        """Adopt the account-customization gear (reported). Placed by
        :meth:`gear_rect` and raised with the other controls."""
        widget.setParent(self)
        widget.show()
        self.gear = widget
        self._place_children()

    def selector_rects(self) -> dict:
        """``{slot: (x, y, w, h)}`` for the selectors, OUTSIDE the hole rect.

        Horizontally centered on the hole; vertically in the gap the hole rect
        leaves inside the inner circle, clamped to this widget so a short page
        cannot push one off the top.

        A slot holds a whole header now (:class:`PlotHeader`: a title, the
        chart's period selector, and under the fan a wrapped disclaimer), not a
        bare combo, so the width is capped at THE HOLE'S WIDTH: a caption is as
        wide as the plot it captions, never wider, and the disclaimer's "full
        width" means the width of the fan it disclaims.

        The chord of the inner circle was tried as the cap instead, on the
        theory that nothing should leave the ring. It is unstable: a wrapped
        note is taller the narrower it is, a taller header reaches deeper into
        the crescent where the chord is shorter, and the two chase each other
        down to a one-pixel-wide header at ordinary window sizes. The hole's
        width does not depend on the height, so this settles in one pass -- and
        what a caption's far corners can reach in the crescent below the plots
        is the annulus's own edge, not the left band, which sits outside the
        circle entirely.

        Height comes from :meth:`QWidget.heightForWidth` at that width, clamped
        to the area: a header asked to wrap at a width of one pixel (a page
        laid out before it has a size) reports a height of hundreds of lines,
        and that rect goes into the area's mask."""
        hx, hy, hw, hh = self.hole_rect()
        out = {}
        for slot in self.SELECTOR_SLOTS:
            widget = self.selectors.get(slot)
            if widget is None:
                continue
            hint = widget.sizeHint()
            sw = max(1, hint.width())
            if widget.maximumWidth() > 0:
                sw = min(sw, widget.maximumWidth())
            sw = min(sw, max(1, hw), max(1, self.width()))
            sh = max(1, hint.height())
            if widget.hasHeightForWidth():
                sh = max(1, min(widget.heightForWidth(sw), max(1, self.height())))
            x = hx + (hw - sw) // 2
            if slot == "top":
                y = hy - BAND_SPACING - sh
            else:
                y = hy + hh + BAND_SPACING
            y = max(0, min(y, max(0, self.height() - sh)))
            out[slot] = (x, y, sw, sh)
        return out

    def _mask_ring(self) -> None:
        """Mask the ring canvas down to the ANNULUS it actually draws in.

        The canvas is in FRONT of the hole AND of the corner launchers, so
        without this its transparent rect would swallow every click meant for
        them -- a Qt mask is the one knob that governs painting and hit testing
        together, and ``WA_TransparentForMouseEvents`` cannot be used on the
        canvas because the donut's band must stay clickable for wedge
        selection.

        Outer edge at ``R * RING_VIEW_LIMIT``, which is exactly the canvas's
        inscribed circle, and which is also how far a SELECTED wedge explodes:
        that is what lets a pulled-out wedge paint over the corner box it
        reaches into (reported) while the rest of the box, and the whole left
        band, stay visible and clickable underneath.

        The invariant, and the bug it repays: this canvas must NEVER be left
        with an unmasked full-rect hit region. Every path out of here sets a
        mask.

        Two of them are not the annulus. With no wedges the canvas draws only
        its "nothing here" text, dead center, so the mask is the HOLE RECT --
        big enough for the text, and no bigger than an area the hole widget
        already occupies -- and the canvas goes mouse-transparent for as long as
        it has nothing to click, so the charts underneath keep their clicks.
        (Mouse-transparency is safe HERE, where the canvas owns no children;
        it is not safe on a container -- see the class docstring.) With
        zero geometry, nothing can be computed yet, so the mask goes EMPTY and a
        single re-place is queued for the moment geometry exists; clearing
        instead is what left the canvas swallowing the whole page."""
        w, h = self.ring.width(), self.ring.height()
        if w <= 0 or h <= 0:
            self.ring.setMask(QRegion())
            self._defer_mask()
            return
        if not self.ring.wedge_count():
            self.ring.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            hx, hy, hw, hh = self.hole_rect()
            origin = self.ring.pos()
            self.ring.setMask(QRegion(hx - origin.x(), hy - origin.y(),
                                      max(1, hw), max(1, hh)))
            return
        self.ring.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        radius = self.outer_radius()
        cx, cy = w / 2.0, h / 2.0

        def ellipse(r: float) -> QRegion:
            d = max(1, int(round(2.0 * r)))
            return QRegion(int(round(cx - r)), int(round(cy - r)), d, d,
                           QRegion.Ellipse)

        outer = ellipse(radius * RING_VIEW_LIMIT)
        self.ring.setMask(outer.subtracted(ellipse(radius * RING_INNER_RADIUS)))

    def _defer_mask(self) -> None:
        """Queue ONE re-place for after the event loop has given us geometry.

        Deferred, not recursive, and never re-queued from the deferred call: if
        the widget is still zero-sized then it has not been laid out yet, and
        the resize that lays it out calls :meth:`_place_children` anyway."""
        if self._mask_pending:
            return
        self._mask_pending = True
        QTimer.singleShot(0, self._retry_mask)

    def _retry_mask(self) -> None:
        try:
            self._mask_pending = False
            if self.ring.width() <= 0 or self.ring.height() <= 0:
                return
            self._place_children()
        except RuntimeError:
            pass                       # the C++ widget went away first

    def _mask_area(self) -> None:
        """Mask THIS widget to the union of its children's live regions.

        This widget is raised over the page's left band, and its own rect is
        much bigger than anything it draws, so an unmasked rect would eat every
        click meant for the band's mode buttons, What If and thermometer. A mask
        is the fix; ``WA_TransparentForMouseEvents`` is NOT, because Qt skips a
        mouse-transparent widget's entire subtree when it picks a mouse
        receiver, which is what made every control on this page dead.

        A child contributes its own mask when it has one (the ring canvas is the
        annulus, not its rect) and its geometry otherwise. A parent's mask also
        clips its children's PAINTING, so the union is the smallest region that
        cannot change what the page looks like."""
        region = QRegion()
        for child in self.children():
            if not isinstance(child, QWidget) or not child.isVisibleTo(self):
                continue
            geom = child.geometry()
            if child is self.ring:
                # Always masked by _mask_ring, which runs first.
                region = region.united(self.ring.mask().translated(geom.topLeft()))
            else:
                region = region.united(QRegion(geom))
        self.setMask(region)

    def _place_children(self) -> None:
        x, y, w, h = self.hole_rect()
        self.hole.setGeometry(x, y, w, h)
        # Order matters, and it is the reported three-level stack: the corner
        # launchers go over the hole (and, because this whole widget is raised
        # over it, over the page's left band); the ring goes over the corners so
        # an exploded wedge paints across one; then everything that must stay
        # reachable goes over the ring.
        for name, rect in self.corner_rects().items():
            widget = self.corners.get(name)
            if widget is None:
                continue
            widget.setGeometry(*rect)
            widget.raise_()
        self.ring.raise_()
        self._mask_ring()
        for slot, rect in self.selector_rects().items():
            self.selectors[slot].setGeometry(*rect)
            self.selectors[slot].raise_()
        if self.center is not None:
            self.center.setGeometry(*self.center_rect())
        if self.mode_row is not None:
            rect = self.mode_row_rect()
            if rect is not None:
                self.mode_row.setGeometry(*rect)
                self.mode_row.raise_()
        if self.gear is not None:
            self.gear.setGeometry(*self.gear_rect())
            self.gear.raise_()
        # The center line is raised LAST of all, after the gear: "the center
        # line is invisible again ... needs to be on top of everything". It is
        # the one overlay that crosses the whole circle, so anything raised
        # after it can land on it -- and the raise has to happen here, in the
        # method every geometry and refresh path funnels through, not once at
        # construction, which is how it lost the top twice already. (The gear
        # is in the top-right corner and the center strip is on the midline;
        # they do not overlap, so neither loses a click to the other.)
        self.raise_center()
        # Last: the area's own mask depends on where everything above landed.
        self._mask_area()

    def raise_center(self) -> None:
        """Put the center line back on top of every sibling. Idempotent, and
        cheap enough to call from any path that adds or re-places a child."""
        if self.center is not None:
            self.center.raise_()

    def relayout(self) -> None:
        """Re-place the overlays. Public because the mask depends on what the
        ring last drew, so the page calls it after a refresh as well."""
        self._place_children()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_children()


# ---------------------------------------------------------------------------
# the inflow arrows (design section 3)
# ---------------------------------------------------------------------------
@dataclass
class InflowArrow:
    """One qualifying account's trailing-year inflows."""
    account_id: int
    name: str
    count: int
    total: int                          # cents, the SUM of the year's inflows


def inflow_arrows(conn, as_of: Optional[str] = None, *, account_ids=None,
                  min_inflows: int = ARROW_MIN_INFLOWS,
                  window_days: int = INFLOW_WINDOW_DAYS) -> list:
    """Every investment account with at least ``min_inflows`` positive external
    flows in the trailing window, with the plain sum of those flows.

    ``account_ids`` narrows it to the page's account scope (the gear); ``None``
    means every investment account, as it does everywhere else on this page.

    Deliberately a count, not a cadence fit: the design threw the interval
    grid away because biweekly and semimonthly intervals overlap while their
    counts (26 vs 24) do not, and the dashboard needs neither. Nothing here is
    annualized -- eight months of contributions say eight months' worth."""
    end = as_of or _today()
    start = (_dt.date.fromisoformat(end)
             - _dt.timedelta(days=window_days - 1)).isoformat()
    out = []
    for account_id in (_account_ids(conn) if account_ids is None
                       else list(account_ids)):
        flows = [c for _, c in portfolio.external_flows(conn, account_id, start, end)
                 if c > 0]
        if len(flows) >= min_inflows:
            row = conn.execute("SELECT name FROM accounts WHERE id=?",
                               (account_id,)).fetchone()
            out.append(InflowArrow(account_id, (row["name"] if row else ""),
                                   len(flows), sum(flows)))
    out.sort(key=lambda a: (-a.total, a.name))
    return out


class InflowArrowWidget(QWidget):
    """A drawn arrow -- not a chart -- with the yearly total inscribed in the
    head and the account name under the tail.

    It points toward the ring (right), which is where the money is going; the
    band it lives in is on the left of the page for exactly that reason.

    Under What If the yearly total becomes editable (design 2.5). The editor is
    a real QLineEdit laid over the arrow's head rather than a paint-time text
    cursor, so the platform's selection, undo and IME all work; it is hidden
    while What If is off, and the arrow paints its own amount instead."""

    MIN_HEIGHT = 46

    amountChanged = pyqtSignal(int)         # cents, What If only

    def __init__(self, arrow: InflowArrow, color: str = "#4c78a8", parent=None):
        super().__init__(parent)
        self.arrow = arrow
        self.color = color
        self._amount = int(arrow.total)
        self._editable = False
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setToolTip(f"{arrow.name}: {arrow.count} deposits, "
                        f"{fmt_money(arrow.total)} in the last year")
        self.editor = QLineEdit(self)
        self.editor.setObjectName("arrowAmount")
        self.editor.setAlignment(Qt.AlignCenter)
        self.editor.setText(fmt_money(self._amount))
        self.editor.editingFinished.connect(self._commit)
        self.editor.hide()

    # -- what the arrow says (kept out of paintEvent so it is testable) -----
    def amount(self) -> int:
        """The yearly total the projection should use: measured, or whatever
        What If last accepted."""
        return self._amount

    def set_amount(self, cents: int) -> None:
        """Set the shown total WITHOUT emitting -- this is the page pushing a
        reset back down, not the user typing."""
        self._amount = int(cents)
        self.editor.setText(fmt_money(self._amount))
        self.update()

    def amount_text(self) -> str:
        return fmt_money(self._amount)

    def account_id(self) -> int:
        return int(self.arrow.account_id)

    def account_name(self) -> str:
        return self.arrow.name

    def is_editable(self) -> bool:
        return self._editable

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        self.editor.setText(fmt_money(self._amount))
        self.editor.setVisible(self._editable)
        self._place_editor()
        self.update()

    def _commit(self) -> None:
        try:
            cents = parse_money(self.editor.text())
        except ValueError:                  # gibberish: snap back, say nothing
            self.editor.setText(fmt_money(self._amount))
            return
        changed = cents != self._amount
        self._amount = cents
        self.editor.setText(fmt_money(cents))
        self.update()
        if changed:
            self.amountChanged.emit(cents)

    def commit_text(self, text: str) -> None:
        """Type ``text`` into the editor and accept it (what a test does, and
        what Qt does on editingFinished)."""
        self.editor.setText(str(text))
        self._commit()

    # -- geometry (shared by the painter and the editor) --------------------
    def _arrow_metrics(self):
        w, h = float(self.width()), float(self.height())
        body_h = max(10.0, h * 0.52)
        top = 2.0
        head_w = min(w * 0.3, body_h)
        tail_r = max(0.0, w - head_w - 2.0)
        return w, h, top, body_h, tail_r

    def _place_editor(self) -> None:
        _w, _h, top, body_h, tail_r = self._arrow_metrics()
        self.editor.setGeometry(3, int(top) + 2, max(24, int(tail_r) - 6),
                                max(14, int(body_h) - 4))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_editor()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h, top, body_h, tail_r = self._arrow_metrics()
        w, h = int(w), int(h)
        poly = QPolygonF([
            QPointF(0.0, top + body_h * 0.22),
            QPointF(tail_r, top + body_h * 0.22),
            QPointF(tail_r, top),
            QPointF(float(w), top + body_h / 2.0),
            QPointF(tail_r, top + body_h),
            QPointF(tail_r, top + body_h * 0.78),
            QPointF(0.0, top + body_h * 0.78),
        ])
        p.setPen(QPen(QColor("#ffffff"), 1))
        p.setBrush(QColor(self.color))
        p.drawPolygon(poly)
        if not self._editable:              # the editor is drawing it instead
            p.setPen(QPen(QColor("#ffffff")))
            p.drawText(0, int(top), int(tail_r), int(body_h),
                       Qt.AlignCenter, self.amount_text())
        p.setPen(QPen(self.palette().windowText().color()))
        p.drawText(0, int(top + body_h), w, int(max(12.0, h - body_h - top)),
                   Qt.AlignLeft | Qt.AlignVCenter, self.account_name())
        p.end()


# ---------------------------------------------------------------------------
# the center line (design 2.3)
# ---------------------------------------------------------------------------
@dataclass
class CenterLine:
    """The one row of numbers inside the hole, for the current filter subject.

    Every field is composed from the domain layer; ``annualized`` holds only
    the horizons that HAVE enough history -- a horizon with no data is absent,
    which is how it renders as nothing rather than as a dash or a zero."""
    total: int                                  # cents, market value
    year_gain: Optional[int] = None             # cents
    dividends: int = 0                          # cents, trailing year
    annualized: dict = field(default_factory=dict)   # {years: Decimal percent}
    subject: str = "All investments"
    #: Cents this subject paid out as CASH rather than reinvesting. Non-zero
    #: only for a single security, and only then is the Total asterisked: the
    #: money is real and earned, but it is in the account's cash, not in the
    #: security's market value.
    uninvested_dividends: int = 0
    #: The cash slice's own scope. It has a value and nothing else -- cash has
    #: no gain, no dividends and no rate of return to state.
    cash_only: bool = False

    def columns(self) -> list:
        """``[(heading, value), ...]`` -- one column of the center block.

        THE source of truth for what the block says. It is rendered as a
        two-row table (headings above, figures below, no rules), and
        :meth:`parts` and :meth:`text` are flattened views of the same pairs, so
        a column added here appears in every one of them at once.

        An absent horizon contributes no column at all, which is how a scope
        without enough history renders as nothing rather than as a dash or a
        zero."""
        total = fmt_money(self.total)
        if self.uninvested_dividends:
            total += "*"
        out = [("Total", total)]
        if self.cash_only:
            # Cash has a value and nothing else to say about it.
            return out
        if self.year_gain is not None:
            out.append(("1-yr gain", fmt_signed(self.year_gain)))
        out.append(("1-yr dividends", fmt_money(self.dividends)))
        for years in ANNUALIZED_YEARS:
            pct = self.annualized.get(years)
            if pct is not None:
                # "return", not "gain": this one is an annualized RATE, and the
                # dollar gain already has a column of its own next to it.
                out.append((f"{years}-yr return", fmt_pct(pct)))
        return out

    def footnote(self) -> str:
        """What the asterisk on the Total means, or "" when there is none."""
        return f"* {UNINVESTED_NOTE}" if self.uninvested_dividends else ""

    def headings(self) -> list:
        return [head for head, _ in self.columns()]

    def values(self) -> list:
        return [value for _, value in self.columns()]

    def parts(self) -> list:
        return [f"{head} {value}" for head, value in self.columns()]

    def text(self) -> str:
        return "   |   ".join(self.parts())


def _scope_cash(conn, ids, end: str) -> int:
    """Cents of cash across the scope, which is the securities ring's own
    wedge and the difference between its wedges and the accounts ring's."""
    return sum(portfolio.account_valuation(conn, int(a), end).cash for a in ids)


def _account_ids(conn) -> list:
    return portfolio.scope_account_ids(conn, "investments")


def _value_at(conn, ids, symbol: Optional[str], iso: str) -> int:
    """Market value of the filter subject on one date, in cents.

    The single place that knows a scope is either "these accounts, whole" or
    "this security across them": the center line, the value chart and the
    projection's starting point all agree because they all call this."""
    if symbol is None:
        # portfolio.account_valuation, NOT investments' -- the scope is
        # INVESTMENT_LIKE_TYPES and so includes crypto wallets, whose money the
        # brokerage engine cannot see (it reads the bank transfer legs alone).
        return sum(portfolio.account_valuation(conn, a, iso).total for a in ids)
    alloc = portfolio.allocation(conn, account_ids=list(ids), as_of=iso)
    return sum(s.value for s in alloc.by_security if s.key == symbol)


def _performance(conn, account_ids, symbol, start, end):
    """One pooled :class:`portfolio.Performance` over the scope, per security
    when a security filter is on. ``combine_performances`` does the pooling --
    this module never averages rates itself."""
    if symbol is None:
        perfs = [portfolio.account_performance(conn, a, start, end)
                 for a in account_ids]
    else:
        perfs = [portfolio.security_performance(conn, a, symbol, start, end)
                 for a in account_ids]
    if not perfs:
        return None
    return portfolio.combine_performances(perfs, end)


def center_line(conn, as_of: Optional[str] = None, *, account_ids=None,
                symbol: Optional[str] = None, subject: str = "All investments",
                horizons=ANNUALIZED_YEARS) -> CenterLine:
    """The center readout for a scope: market value now, the trailing year's
    gain and dividends, and the annualized return over each horizon that has
    enough history for :attr:`portfolio.Performance.annual_return` to exist."""
    end = as_of or _today()
    ids = list(_account_ids(conn) if account_ids is None else account_ids)
    if symbol == CASH_KEY:
        return CenterLine(total=_scope_cash(conn, ids, end), subject=subject,
                          cash_only=True)
    line = CenterLine(total=_value_at(conn, ids, symbol, end), subject=subject)
    if symbol:
        # A single security's value is its HOLDINGS, so any dividend it paid in
        # cash is missing from it -- while still counted in every rate below,
        # because security_performance treats a cash dividend as money back.
        # The asterisk is that difference, said out loud.
        line.uninvested_dividends = portfolio.cash_dividends(conn, ids, symbol, end)
    for years in horizons:
        perf = _performance(conn, ids, symbol, years_before(end, years), end)
        if perf is None:
            continue
        if years == 1:
            line.year_gain = perf.gain
            line.dividends = perf.income
        rate = perf.annual_return
        if rate is not None:
            line.annualized[years] = rate
    return line


def _scaled_font(font: QFont, scale: float) -> QFont:
    """``font`` resized by ``scale``.

    Point size and pixel size are ALTERNATIVES in Qt -- a font sized in pixels
    reports ``pointSizeF() == -1`` -- so both are handled; scaling the wrong one
    silently does nothing, which is how a size change can look applied and have
    no effect."""
    out = QFont(font)
    points = out.pointSizeF()
    if points > 0:
        out.setPointSizeF(points * scale)
    else:
        out.setPixelSize(max(1, int(round(out.pixelSize() * scale))))
    return out


class CenterLineWidget(QWidget):
    """The center block: a two-row table in the hole -- headings above, figures
    below, and no rules between them (reported: "make it a two-line table ...
    with the headings ... and the numbers underneath. No lines, though").

    A grid, not two independently laid-out rows, because a heading and its
    figure have to share a column edge: "1-yr dividends" is far wider than
    "Total", and only a shared column keeps each number under its own label.

    The rows are typed and colored differently on purpose. The headings recede
    at the page's own size in the muted color; the figures carry
    :data:`CENTER_FONT_SCALE` and the theme's ``highlight`` accent (reported:
    "increase the font a bit and color it yellow or something so that it stands
    out"). That accent is resolved from the ACTIVE palette on every rebuild, so
    the block follows a light/dark switch instead of keeping one theme's amber
    on the other theme's background; :meth:`restyle` is the hook the page's
    ``changeEvent`` calls when nothing else would rebuild it.
    """

    def __init__(self, line=None, parent=None):
        super().__init__(parent)
        self._line = line or CenterLine(total=0)
        lay = QGridLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        # Columns breathe horizontally; the two ROWS sit tight against each
        # other so a figure reads as belonging to the heading above it.
        lay.setHorizontalSpacing(CENTER_COLUMN_SPACING)
        lay.setVerticalSpacing(0)
        self._headings = []
        self._values = []
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # NOT Qt.WA_TransparentForMouseEvents. It was, on the theory that an
        # overlay must not steal the plots' clicks -- but what it sits on is
        # ``centerGap``, a blank reserved strip, not a plot, so there was no
        # click to steal; and the attribute makes Qt skip the widget AND ITS
        # WHOLE SUBTREE in hit testing, which is also what ``childAt()``
        # answers. The line then reported as "invisible again": unreachable by
        # the one probe that can tell, headlessly, whether something covers it.
        # Leaving it hittable is what lets :meth:`RingArea._place_children`
        # prove the raise order held.
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.set_line(self._line)

    # -- type and color -----------------------------------------------------
    def value_font(self) -> QFont:
        """The figures' font: the page's, enlarged and bolded."""
        font = _scaled_font(self.font(), CENTER_FONT_SCALE)
        font.setBold(True)
        return font

    def heading_font(self) -> QFont:
        """The headings' font: the page's own size, not bold."""
        font = _scaled_font(self.font(), CENTER_HEADING_SCALE)
        font.setBold(False)
        return font

    def scaled_font(self) -> QFont:
        """Back-compat name for :meth:`value_font` -- the block's type size is
        the figures' size, which is what every caller meant by it."""
        return self.value_font()

    def accent_color(self) -> str:
        """The figures' color, from the palette active NOW.

        Resolved per rebuild rather than held as a literal: a literal tuned for
        one background is the exact defect the two hole charts were reported
        for (see :func:`chart_colors`)."""
        return charts._active_palette()["highlight"]

    def heading_color(self) -> str:
        return charts._active_palette()["muted"]

    def line_height(self) -> int:
        """One line of the FIGURES -- the unit :data:`CENTER_RAISE_LINES` is in."""
        return QFontMetrics(self.value_font()).height()

    def restyle(self) -> None:
        """Re-resolve both colors against the active palette. Cheap, idempotent,
        and safe to call from a theme-change handler."""
        accent, muted = self.accent_color(), self.heading_color()
        for lab in self._values:
            lab.setStyleSheet("color: %s;" % accent)
        for lab in self._headings:
            lab.setStyleSheet("color: %s;" % muted)

    # -- content ------------------------------------------------------------
    def set_line(self, line) -> None:
        self._line = line
        lay = self.layout()
        while lay.count():
            item = lay.takeAt(0)
            if item.widget() is not None:
                # hide() as well as deleteLater(): the deletion happens when the
                # event loop next runs, and until then the old label is still a
                # visible child sitting on top of its replacement.
                item.widget().hide()
                item.widget().deleteLater()
        for col in range(lay.columnCount()):
            lay.setColumnStretch(col, 0)
        self._headings = []
        self._values = []
        columns = line.columns()
        # Empty stretch columns on the flanks center the block without
        # stretching the gaps BETWEEN columns, which a stretch on the data
        # columns would do -- and then the headings drift off their figures.
        lay.setColumnStretch(0, 1)
        lay.setColumnStretch(len(columns) + 1, 1)
        value_font, heading_font = self.value_font(), self.heading_font()
        accent, muted = self.accent_color(), self.heading_color()
        for i, (head, value) in enumerate(columns, start=1):
            top = QLabel(head, self)
            top.setAlignment(Qt.AlignCenter)
            top.setFont(heading_font)
            top.setStyleSheet("color: %s;" % muted)
            bottom = QLabel(value, self)
            bottom.setAlignment(Qt.AlignCenter)
            bottom.setFont(value_font)
            bottom.setStyleSheet("color: %s;" % accent)
            if value.endswith("*"):
                # An asterisk with nothing to explain it is worse than none.
                bottom.setToolTip(UNINVESTED_NOTE)
                top.setToolTip(UNINVESTED_NOTE)
            lay.addWidget(top, 0, i)
            lay.addWidget(bottom, 1, i)
            # show() explicitly, for the mirror of the reason the old labels are
            # hide()n above. A child built for a parent that is ALREADY visible
            # stays hidden until the event loop gets round to showing it, and a
            # hidden widget contributes nothing to its layout's sizeHint. Every
            # caller of set_line reads that hint SYNCHRONOUSLY -- _sync_center_gap
            # reserves the strip from it and RingArea.center_rect sizes the
            # overlay from it -- so without this the block measured its layout
            # margins alone and was placed as a sliver. It stayed one: the hint
            # is right again by the time the event loop runs, but nothing
            # re-places the overlay then, so the text vanished on the first
            # wedge click and never came back (reported).
            top.show()
            bottom.show()
            self._headings.append(top)
            self._values.append(bottom)

    def line(self) -> CenterLine:
        return self._line

    def text(self) -> str:
        return self._line.text()

    def heading_texts(self) -> list:
        return [lab.text() for lab in self._headings]

    def value_texts(self) -> list:
        return [lab.text() for lab in self._values]

    def label_texts(self) -> list:
        """Each column flattened to "heading value" -- the same strings
        :meth:`CenterLine.parts` produces, but read off the actual widgets."""
        return [h.text() + " " + v.text()
                for h, v in zip(self._headings, self._values)]


class PlotHeader(QWidget):
    """A hole chart's caption: its title and its period selector on one row,
    and -- for the fan -- a wrapped note underneath.

    The selector is ADOPTED, not duplicated. It is still the very combo the
    chart owns (``chart.period``), reparented into this row, so every existing
    caller and test that reaches for ``page.history_chart.period`` keeps
    working and there is no second control to keep in sync.

    The header is what :class:`RingArea` now places in its top and bottom
    selector slots, which is why it must answer :meth:`heightForWidth`: the
    slots are chords of a circle, so the width a header gets is decided by the
    geometry, and a wrapped note's height only exists once that width is
    known."""

    def __init__(self, selector, title: str = "", note: str = "", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TITLE_GAP)
        self._title = title
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("plotTitle")
        font = QFont(self.title_label.font())
        font.setBold(True)
        self.title_label.setFont(font)
        self.selector = selector
        selector.setParent(self)
        # Title first, selector second: "left of the time-range dropdown".
        row.addStretch(1)
        row.addWidget(self.title_label, 0)
        row.addWidget(selector, 0)
        row.addStretch(1)
        lay.addLayout(row)
        self.note_label = None
        if note:
            self.note_label = QLabel(note, self)
            self.note_label.setObjectName("plotNote")
            self.note_label.setWordWrap(True)
            self.note_label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.note_label.setFont(_scaled_font(self.note_label.font(),
                                                 NOTE_FONT_SCALE))
            lay.addWidget(self.note_label)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_title(self, text: str) -> None:
        self._title = text
        self._apply_title()
        # The slot reads sizeHint() SYNCHRONOUSLY right after this, from
        # RingArea.selector_rects. A label's new text does not reach its
        # layout's hint until the event loop gets round to it, so without these
        # the header keeps the width the PREVIOUS title asked for: "Test IRA
        # Performance" needs 340px and was handed the 289px the shorter title
        # had, with 612px of hole sitting free next to it. Same deferred-hint
        # trap as the center block's labels.
        self.title_label.updateGeometry()
        self.layout().invalidate()
        self.layout().activate()
        self.updateGeometry()

    def title(self) -> str:
        """The FULL title, not what is currently displayed -- :meth:`_apply_title`
        may be showing an elided version of it."""
        return self._title

    def displayed_title(self) -> str:
        return self.title_label.text()

    def _title_budget(self) -> int:
        """Pixels the title may occupy: whatever is left once the dropdown and
        the gap have taken theirs."""
        margins = self.layout().contentsMargins()
        selector = max(self.selector.sizeHint().width(),
                       self.selector.width())
        return (self.width() - margins.left() - margins.right()
                - selector - TITLE_GAP)

    def _apply_title(self) -> None:
        """Show the title, ELIDED if it cannot fit beside the dropdown.

        A QLabel clips; it does not elide. Clipping is what produced the report,
        because a cut-off word reads as a collision with the control next to it
        whereas an ellipsis reads as "there is more". Elision only ever applies
        when the granted width is genuinely short -- :meth:`sizeHint` asks for
        the FULL title, so a header with room shows all of it."""
        budget = self._title_budget()
        if budget <= 0 or self.width() <= 0:
            self.title_label.setText(self._title)
            return
        metrics = QFontMetrics(self.title_label.font())
        self.title_label.setText(
            metrics.elidedText(self._title, Qt.ElideRight, budget))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_title()

    def sizeHint(self):
        """The width the FULL title wants, never the elided one.

        Deliberately independent of what is on screen. Deriving it from the
        label's current text would feed elision back into the hint: a narrowed
        header elides, the shorter text reports a smaller hint, the slot grants
        less, and the title walks itself down to an ellipsis at a width where it
        would have fitted."""
        hint = super().sizeHint()
        margins = self.layout().contentsMargins()
        full = QFontMetrics(self.title_label.font()).horizontalAdvance(self._title)
        want = (full + TITLE_GAP + self.selector.sizeHint().width()
                + margins.left() + margins.right())
        hint.setWidth(max(hint.width(), want))
        return hint

    def note(self) -> str:
        return self.note_label.text() if self.note_label is not None else ""

    # Qt asks the layout for these only when the size POLICY says to; the slot
    # asks them directly, so they are stated outright rather than inherited.
    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        lay = self.layout()
        height = lay.totalHeightForWidth(max(1, int(width)))
        return max(1, height if height > 0 else self.sizeHint().height())


# ---------------------------------------------------------------------------
# what the two charts in the hole are made of (design 2.3, 4.5, 5.3)
# ---------------------------------------------------------------------------
def history_span(conn, ids, symbol: Optional[str], end: str, *,
                 limit: int = MAX_HISTORY_YEARS) -> int:
    """How many years back "Max" should reach for this subject.

    A doubling probe, not a scan: valuing a scope is expensive, so this asks
    1, 2, 4, 8 ... years back and stops at the first date where the subject was
    worth nothing. It overshoots by design -- the caller trims the leading
    zeros off the sampled series, which costs one flat point and never a wrong
    start date."""
    years = 1
    while years < limit:
        if _value_at(conn, ids, symbol, years_before(end, years)) <= 0:
            return years
        years = min(limit, years * 2)
    return limit


def _trim_leading_zeros(points: list) -> list:
    """Drop the run of worthless samples at the left, keeping the last of them
    as the line's origin so the first purchase reads as a rise from zero."""
    first = 0
    while first < len(points) and points[first][1] <= 0:
        first += 1
    if first >= len(points):
        return []
    return points[max(0, first - 1):]


def value_series(conn, years: Optional[int] = DEFAULT_HISTORY_YEARS, *,
                 as_of: Optional[str] = None, account_ids=None,
                 symbol: Optional[str] = None,
                 points: int = HISTORY_POINTS) -> list:
    """``[(iso, cents)]`` -- the subject's market value across the period.

    ``years=None`` means "Max". There is no stored value-over-time series in
    the domain layer (``investments.valuation_as_of`` knows one date), so this
    samples ``_value_at`` on an even date grid. That is why ``points`` is
    small: every extra sample is a whole valuation."""
    end = as_of or _today()
    ids = list(_account_ids(conn) if account_ids is None else account_ids)
    span = history_span(conn, ids, symbol, end) if years is None else max(1, int(years))
    last = _dt.date.fromisoformat(end)
    first = _dt.date.fromisoformat(years_before(end, span))
    days = (last - first).days
    n = max(2, int(points))
    out = []
    for i in range(n):
        day = first + _dt.timedelta(days=int(round(days * i / (n - 1))))
        iso = day.isoformat()
        out.append((iso, _value_at(conn, ids, symbol, iso)))
    return _trim_leading_zeros(out)


def current_mix(conn, account_ids=None, as_of: Optional[str] = None) -> dict:
    """Today's actual asset-class weights for a scope, summing to 1.

    Straight off ``portfolio.allocation().by_class`` -- the dashboard does not
    classify anything itself. An empty or unpriced scope is all cash, which is
    ladder level 0 and the honest answer for "no risk taken yet"."""
    end = as_of or _today()
    ids = list(_account_ids(conn) if account_ids is None else account_ids)
    alloc = portfolio.allocation(conn, account_ids=ids, as_of=end)
    weights = {s.key: float(s.value) for s in alloc.by_class if s.value > 0}
    if not weights:
        return forecast.normalize_weights({"cash": 1.0})
    return forecast.normalize_weights(weights)


def current_risk(conn, account_ids=None, as_of: Optional[str] = None) -> float:
    """Where the thermometer's needle sits by default: the ladder level whose
    volatility matches the scope's actual mix (design 4.5's inverse map, which
    lives in :func:`forecast.risk_for_mix` and is clamped to the ladder)."""
    return forecast.risk_for_mix(current_mix(conn, account_ids, as_of))


def mix_caption(weights) -> str:
    """``{class: weight}`` -> ``60% stocks / 24% bonds / 16% cash``.

    Percentages are whole numbers because the slider is a tenth of a level and
    nobody reads 23.7% bonds as different from 24%."""
    w = forecast.normalize_weights(weights)
    buckets = (
        ("stocks", w.get("domestic_stock", 0.0) + w.get("intl_stock", 0.0)),
        ("bonds", w.get("bond", 0.0)),
        ("cash", w.get("cash", 0.0)),
        ("other", w.get("real_estate", 0.0) + w.get("other", 0.0)),
    )
    parts = []
    for name, value in buckets:
        pct = Decimal(str(value * 100.0)).quantize(Decimal("1"),
                                                   rounding=ROUND_HALF_UP)
        if pct > 0:
            parts.append(f"{int(pct)}% {name}")
    return " / ".join(parts) if parts else "no holdings"


def projection_fan(start_cents: int, annual_contribution_cents: int,
                   risk: float, years: int, *, inflation=None) -> list:
    """The closed-form percentile fan for one constant mix.

    Every number in it comes from :mod:`mammon.forecast`: the risk level picks
    the mix (4.5), the mix gives ``(mu, sigma)``, and ``forecast.fan`` does the
    moment recursion and the lognormal percentiles (5.3). No maths here, and
    no simulation anywhere."""
    mix = forecast.mix_for_risk(risk)
    mu, sigma = forecast.portfolio_moments(mix)
    return forecast.fan(int(start_cents), int(annual_contribution_cents),
                        mu, sigma, int(years), inflation=inflation)


def chart_colors(pal=None) -> dict:
    """Every color the two hole charts draw WITH, from the active palette.

    Reported: "the plots have poor contrast in both dark and light mode". The
    series colors were fixed hex literals picked against a white page, so on
    the dark page they were a dim blue on near-black; the faint gray baseline
    outline was close to invisible in either. Each one is now a SEMANTIC palette
    name (``blue`` for the value line and the measured fan, ``negative`` for the
    What If fan, ``muted`` for the outline behind it), resolved from
    :func:`charts._active_palette` at DRAW time -- so both themes get the hue
    that theme already uses for that meaning, and a theme change repaints them.

    The axes CHROME (ticks, spines, tick labels) is deliberately not here: it
    keeps ``charts.py``'s rule of theming only in dark mode, because
    matplotlib's light defaults are already the highest-contrast thing on a
    white page."""
    pal = charts._active_palette() if pal is None else pal
    return {
        "history": pal["blue"],
        "baseline": pal["blue"],
        "what_if": pal["negative"],
        "outline": pal["muted"],
        "text": pal["text"],
    }


# ---------------------------------------------------------------------------
# the two charts (design 2.3)
# ---------------------------------------------------------------------------
class _HoleCanvas(FigureCanvasQTAgg):
    """Shared chrome for the two charts inside the ring's hole.

    They are their own canvases rather than ``ui/charts.py`` classes because
    they draw into a ~200px square with no titles, no frame and a transparent
    background: everything the shared canvases exist to provide is exactly
    what has to be stripped off here. What they DO take from ``charts.py`` is
    every color rule -- the palette, the chrome recoloring and the grid --
    so the hole charts cannot drift from the app's other charts (the fan adds
    a small frameless legend of its own, because an unlabelled band left the
    user guessing whether the shading meant percentiles or sigmas)."""

    MIN_HEIGHT = 44

    def __init__(self, parent=None):
        fig = Figure(figsize=(3.0, 1.3), dpi=72)
        fig.patch.set_alpha(0.0)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(self.MIN_HEIGHT)

    def _new_axes(self):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.patch.set_alpha(0.0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=TICK_FONT_SIZE, length=2, pad=1)
        self.figure.subplots_adjust(left=PLOT_AXES_LEFT, right=PLOT_AXES_RIGHT,
                                    top=0.96, bottom=0.26)
        return ax

    def _grid(self, ax) -> None:
        """The gridlines the user asked for, on both axes, behind the data.

        Drawn through :func:`charts._theme_grid` so their weight and color are
        the SAME rule the Net Worth chart follows -- one grid look in the app,
        and the color comes from the active palette rather than a literal."""
        charts._theme_grid(ax, charts._chart_palette(), axis="both")

    def _theme(self, ax) -> None:
        """Color the chrome from the palette that is active RIGHT NOW.

        Called at the END of every render, never at construction: the page is
        rebuilt on a theme change (``ui/widgets.py`` marks it stale), and a
        color captured when the widget was created would keep matplotlib's
        near-black defaults on the dark palette -- ticks, tick labels and axis
        labels invisible against a dark page, which is exactly what was
        reported. The palette and the recoloring rule are IMPORTED from
        :mod:`mammon.ui.charts` so this family cannot drift from the shared
        charts."""
        pal = charts._chart_palette()
        charts._theme_axes_chrome(ax, pal)
        if pal is None:                     # light mode: matplotlib's defaults
            return
        # Chrome the shared helper does not own, because the shared charts do
        # not label their axes: the two axis labels and the x grid.
        for label in (ax.xaxis.label, ax.yaxis.label):
            label.set_color(pal["text"])
        for gridline in ax.get_xgridlines():
            gridline.set_color(pal["line"])
        # The figure and the axes stay transparent on purpose -- the themed page
        # behind them is the background, so there is nothing to fill.

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
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: f"${v:,.0f}"))


class ValueHistoryCanvas(_HoleCanvas):
    """The upper chart: one line, the subject's value over the period, drawn in
    the wedge color when a filter is active."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points: list = []
        self._color: Optional[str] = None      # None = "the theme's own blue"
        self.render()

    def set_series(self, points, color: Optional[str] = None) -> None:
        """``color=None`` means "no wedge is selected": the line goes back to
        the theme's blue. It must ASSIGN rather than keep the old value, or
        clearing the ring filter would leave the line painted in the color of
        the wedge that is no longer selected."""
        self._points = list(points or [])
        self._color = color or None
        self.render()

    def points(self) -> list:
        return list(self._points)

    def color(self) -> str:
        """The line's color: the selected wedge's, or -- with no filter -- the
        active theme's accent blue, resolved now rather than at construction so
        a theme change cannot leave a light-mode blue on a dark page."""
        return self._color or chart_colors()["history"]

    def render(self) -> None:
        if not self._points:
            self._empty(HISTORY_EMPTY_TEXT)
            return
        ax = self._new_axes()
        color = self.color()
        xs = list(range(len(self._points)))
        ys = [c / 100.0 for _iso, c in self._points]
        ax.plot(xs, ys, color=color, linewidth=1.8, zorder=3)
        ax.fill_between(xs, ys, min(0.0, min(ys)), color=color,
                        alpha=0.20, linewidth=0, zorder=2)
        ax.set_xticks([xs[0], xs[-1]])
        ax.set_xticklabels([fmt_date(self._points[0][0]),
                            fmt_date(self._points[-1][0])],
                           fontsize=AXIS_LABEL_FONT_SIZE)
        self._dollars(ax)
        ax.margins(x=0.02)
        self._grid(ax)
        self._theme(ax)
        self.draw_idle()


class ProjectionFanCanvas(_HoleCanvas):
    """The lower chart: the percentile fan, never a single line.

    A What If fan is drawn over the measured one, with the measured one kept as
    a faint outline behind it, because the question the user is asking is "how
    much does this change things" -- which is unanswerable if the thing being
    changed disappears."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._baseline: list = []
        self._what_if: Optional[list] = None
        self.render()

    def set_fans(self, baseline, what_if=None) -> None:
        self._baseline = list(baseline or [])
        self._what_if = list(what_if) if what_if else None
        self.render()

    def baseline(self) -> list:
        return list(self._baseline)

    def what_if(self) -> Optional[list]:
        return None if self._what_if is None else list(self._what_if)

    def has_what_if(self) -> bool:
        return bool(self._what_if)

    @staticmethod
    def _band(points, attr) -> list:
        return [getattr(p, attr) / 100.0 for p in points]

    def _draw_fan(self, ax, points, color) -> None:
        """The three bands, each LABELLED with the percentiles it actually is.

        The labels feed the on-plot legend: the user could not tell whether the
        shading was standard deviations or percentiles, and design 5.3 draws the
        5/25/50/75/95 percentiles of a fitted lognormal -- so naming them "1
        sigma" would be wrong, not merely vague."""
        xs = [p.year for p in points]
        ax.fill_between(xs, self._band(points, "p05"), self._band(points, "p95"),
                        color=color, alpha=0.20, linewidth=0, zorder=2,
                        label=FAN_OUTER_LABEL)
        ax.fill_between(xs, self._band(points, "p25"), self._band(points, "p75"),
                        color=color, alpha=0.38, linewidth=0, zorder=3,
                        label=FAN_INNER_LABEL)
        ax.plot(xs, self._band(points, "p50"), color=color, linewidth=1.8,
                zorder=4, label=FAN_MEDIAN_LABEL)

    def _draw_outline(self, ax, points, color) -> None:
        xs = [p.year for p in points]
        for i, attr in enumerate(("p05", "p50", "p95")):
            ax.plot(xs, self._band(points, attr), color=color, linewidth=1.0,
                    linestyle="--", alpha=0.85, zorder=5,
                    label=FAN_BASELINE_LABEL if i == 0 else None)

    def _legend(self, ax) -> None:
        """Name the bands ON the plot. No frame: the figure is transparent over
        a themed page, so a legend box would be a white rectangle in dark mode.
        The text takes the dark palette's color the way the tick labels do."""
        handles, labels = ax.get_legend_handles_labels()
        if not handles:
            return
        leg = ax.legend(handles, labels, loc="upper left",
                        fontsize=LEGEND_FONT_SIZE, frameon=False,
                        handlelength=1.3, handletextpad=0.5, labelspacing=0.2,
                        borderpad=0.0, borderaxespad=0.3)
        leg.set_zorder(6)
        pal = charts._chart_palette()
        if pal is not None:
            for text in leg.get_texts():
                text.set_color(pal["text"])

    def render(self) -> None:
        if not self._baseline:
            self._empty(PROJECTION_EMPTY_TEXT)
            return
        colors = chart_colors()
        ax = self._new_axes()
        if self._what_if:
            self._draw_outline(ax, self._baseline, colors["outline"])
            self._draw_fan(ax, self._what_if, colors["what_if"])
        else:
            self._draw_fan(ax, self._baseline, colors["baseline"])
        ax.set_xlabel("")
        ax.xaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: "" if v <= 0 else f"{v:,.0f}y"))
        self._dollars(ax)
        ax.margins(x=0.01)
        self._grid(ax)
        self._legend(ax)
        self._theme(ax)
        self.draw_idle()


class _PeriodChart(QWidget):
    """A canvas plus its own small period combo. The two selectors are
    independent (design 2.3): each chart owns one, there is no shared control.

    The combo is CREATED here but deliberately NOT added to this layout. The
    chart lives in the ring's hole, and the ring is now painted in front of the
    hole, so a control inside the hole would be covered by the band; the page
    hands both combos to :meth:`RingArea.set_selectors`, which reparents them
    outside the hole -- the top chart's above it, the projection's below it
    (reported). Ownership, entries and signals stay exactly where they were, so
    everything that talks to ``chart.period`` is unaffected."""

    OBJECT_NAME = "periodChart"
    COMBO_NAME = "periodCombo"
    #: Which side of the hole this chart's selector belongs on.
    SELECTOR_SLOT = "top"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName(self.OBJECT_NAME)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        self.period = QComboBox(self)
        self.period.setObjectName(self.COMBO_NAME)
        self.period.setMaximumWidth(96)
        for value, label in self._entries():
            self.period.addItem(label, value)
        self.canvas = self._make_canvas()
        lay.addWidget(self.canvas, 1)
        self.period.currentIndexChanged.connect(self._on_period)

    # -- subclass hooks -----------------------------------------------------
    def _entries(self) -> tuple:
        raise NotImplementedError

    def _make_canvas(self):
        raise NotImplementedError

    def _emit(self) -> None:
        raise NotImplementedError

    # -- the selector -------------------------------------------------------
    def _on_period(self, *_args) -> None:
        self._emit()

    def years(self):
        return self.period.itemData(self.period.currentIndex())

    def set_years(self, years) -> None:
        """Select the entry for ``years``. Hand-rolled rather than
        ``findData``: "Max" is stored as None, which never matches through
        QVariant."""
        for i in range(self.period.count()):
            if self.period.itemData(i) == years:
                self.period.setCurrentIndex(i)
                return
        raise ValueError(f"no such period: {years!r}")

    def period_labels(self) -> list:
        return [self.period.itemText(i) for i in range(self.period.count())]

    def period_values(self) -> list:
        return [self.period.itemData(i) for i in range(self.period.count())]


class ValueHistoryChart(_PeriodChart):
    """Upper half of the hole: the value chart and its 1..10 years/Max combo."""

    OBJECT_NAME = "historyChart"
    COMBO_NAME = "historyPeriod"
    SELECTOR_SLOT = "top"

    periodChanged = pyqtSignal(object)      # years, or None for Max

    def _entries(self):
        return HISTORY_PERIODS

    def _make_canvas(self):
        return ValueHistoryCanvas(self)

    def _emit(self):
        self.periodChanged.emit(self.years())

    def set_series(self, points, color: Optional[str] = None) -> None:
        self.canvas.set_series(points, color)

    def points(self) -> list:
        return self.canvas.points()


class ProjectionChart(_PeriodChart):
    """Lower half of the hole: the fan and its 5..50 year horizon combo."""

    OBJECT_NAME = "projectionChart"
    COMBO_NAME = "projectionHorizon"
    SELECTOR_SLOT = "bottom"

    horizonChanged = pyqtSignal(int)

    def _entries(self):
        return tuple((y, f"{y} years") for y in PROJECTION_HORIZONS)

    def _make_canvas(self):
        return ProjectionFanCanvas(self)

    def _emit(self):
        self.horizonChanged.emit(int(self.years()))

    def set_fans(self, baseline, what_if=None) -> None:
        self.canvas.set_fans(baseline, what_if)

    def has_what_if(self) -> bool:
        return self.canvas.has_what_if()


# ---------------------------------------------------------------------------
# the thermometer and What If (design 2.5)
# ---------------------------------------------------------------------------
class ThermometerBar(QAbstractSlider):
    """The risk column AND its handle in one widget: blue (all cash) at the
    bottom to red (all stocks) at the top, with an oval handle painted on the
    color itself at the level currently named.

    It used to be a painted column with a native ``QSlider`` laid out BESIDE
    it, which read as a cross rather than a thermometer (reported: "The square
    slider on the thermometer makes it look like a cross. Why can't the slider
    be an oval on the colored bar itself?"). Merging the two is not only
    cosmetic: with the handle drawn from this widget's own geometry there is
    exactly one mapping between a y coordinate and a risk level, so the thing
    the user grabs and the thing the gradient shows can never drift apart the
    way two stacked widgets with different margins silently did.

    It derives from :class:`QAbstractSlider` rather than reimplementing a value
    model: the range, the single/page steps, the keyboard handling (arrows,
    PageUp/PageDown, Home/End) and the ``valueChanged`` signal the page already
    listens to all come from Qt, so only the painting and the mouse mapping are
    ours. ``QAbstractSlider`` is NOT a ``QSlider``, which is what keeps the old
    native widget from creeping back in unnoticed.

    Handle colors come from the palette (Base filled, WindowText outlined) so
    the oval reads against both the blue end and the red end in light AND dark
    mode; hardcoding white here made it vanish on the cold end of a light
    theme."""

    #: Wide enough that the oval is a grab target rather than a hairline. The
    #: column was 14px when it only had to be looked at.
    BAR_WIDTH = 26
    #: Height of the oval. Also the dead zone at each end of the column: the
    #: handle's CENTER travels, so half of it overhangs no further than the top
    #: and bottom rungs.
    HANDLE_HEIGHT = 16

    def __init__(self, parent=None):
        # QAbstractSlider takes only a parent -- unlike QSlider, the
        # orientation is a property, and passing it positionally is a TypeError.
        super().__init__(parent)
        self.setOrientation(Qt.Vertical)
        self.setObjectName("riskSlider")
        self.setFixedWidth(self.BAR_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)

    # -- geometry -----------------------------------------------------------
    def _travel(self) -> float:
        """Pixels the handle's center can move: the column minus the oval."""
        return float(max(1, self.height() - self.HANDLE_HEIGHT))

    def _fraction(self) -> float:
        span = self.maximum() - self.minimum()
        if span <= 0:
            return 0.0
        return (self.value() - self.minimum()) / float(span)

    def handle_center_y(self) -> float:
        """Where the oval sits now. Bottom of the column is the minimum."""
        return self.HANDLE_HEIGHT / 2.0 + (1.0 - self._fraction()) * self._travel()

    def value_at(self, y: float) -> int:
        """The level a click at ``y`` names, clamped to the range."""
        frac = 1.0 - (float(y) - self.HANDLE_HEIGHT / 2.0) / self._travel()
        frac = max(0.0, min(1.0, frac))
        span = self.maximum() - self.minimum()
        return int(round(self.minimum() + frac * span))

    # -- paint --------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        grad = QLinearGradient(0.0, float(self.height()), 0.0, 0.0)
        grad.setColorAt(0.0, QColor(THERMO_COLD))
        grad.setColorAt(1.0, QColor(THERMO_HOT))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(0, 0, self.width(), self.height(), 6, 6)

        pal = self.palette()
        group = QPalette.Normal if self.isEnabled() else QPalette.Disabled
        fill = pal.color(group, QPalette.Base)
        edge = pal.color(group, QPalette.WindowText)
        if not self.isEnabled():
            fill.setAlpha(170)
            edge.setAlpha(170)
        cy = self.handle_center_y()
        rect = QRectF(2.0, cy - self.HANDLE_HEIGHT / 2.0,
                      float(self.width()) - 4.0, float(self.HANDLE_HEIGHT))
        p.setPen(QPen(edge, 2.0))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(rect, self.HANDLE_HEIGHT / 2.0,
                          self.HANDLE_HEIGHT / 2.0)
        p.end()

    # -- mouse --------------------------------------------------------------
    def mousePressEvent(self, event):
        if not self.isEnabled() or event.button() != Qt.LeftButton:
            event.ignore()
            return
        self.setSliderDown(True)
        self.setValue(self.value_at(event.pos().y()))
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.isEnabled() or not self.isSliderDown():
            event.ignore()
            return
        self.setValue(self.value_at(event.pos().y()))
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.isSliderDown():
            self.setSliderDown(False)
        event.accept()

    def sliderChange(self, change):
        super().sliderChange(change)
        self.update()


class Thermometer(QWidget):
    """The risk selector: one gradient bar over the 11-rung ladder, its oval
    handle painted on the color, with the mix it currently names written
    underneath.

    The handle is a tenth-of-a-level grid because design 4.5 makes the ladder
    continuous -- between rungs the WEIGHTS interpolate and the moments are
    recomputed from them, so the caption and the maths can never disagree.

    There is no separate slider widget: :class:`ThermometerBar` IS the control
    (see its docstring), and this class is only the bar plus its caption."""

    STEPS = RISK_SLIDER_STEPS

    riskChanged = pyqtSignal(float)

    def __init__(self, parent=None, *, risk: float = 0.0):
        super().__init__(parent)
        self.setObjectName("thermometer")
        self._updating = False
        self._editable = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        column = QHBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addStretch(1)
        self.bar = ThermometerBar(self)
        self.bar.setRange(0, int(forecast.MAX_RISK_LEVEL * self.STEPS))
        self.bar.setSingleStep(1)
        self.bar.setPageStep(self.STEPS)
        self.bar.setEnabled(False)          # read-only until What If is on
        self.bar.valueChanged.connect(self._on_value)
        column.addWidget(self.bar, 0)
        column.addStretch(1)
        outer.addLayout(column, 1)

        self.caption = QLabel(self)
        self.caption.setObjectName("mixCaption")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        outer.addWidget(self.caption, 0)

        # Deliberately below the 90 this carried while the band was a stretch
        # layout: the page now sizes the thermometer at two thirds of an even
        # share (:data:`THERMOMETER_HEIGHT_SCALE`), and a floor above that would
        # quietly cancel the rule instead of honouring it.
        self.setMinimumHeight(THERMOMETER_MIN_HEIGHT)
        self.set_risk(risk)

    # -- the level ----------------------------------------------------------
    def risk(self) -> float:
        return self.bar.value() / float(self.STEPS)

    def set_risk(self, level: float, *, emit: bool = False) -> None:
        """Move the needle. Silent by default: this is the page pushing the
        measured mix down, and a redraw loop is not a user action."""
        value = int(round(float(level) * self.STEPS))
        value = max(self.bar.minimum(), min(self.bar.maximum(), value))
        self._updating = not emit
        try:
            self.bar.setValue(value)
        finally:
            self._updating = False
        self._sync_caption()

    def mix(self) -> dict:
        return forecast.mix_for_risk(self.risk())

    def caption_text(self) -> str:
        return self.caption.text()

    def is_editable(self) -> bool:
        return self._editable

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        self.bar.setEnabled(self._editable)

    def _on_value(self, _value) -> None:
        self._sync_caption()
        if not self._updating:
            self.riskChanged.emit(self.risk())

    def _sync_caption(self) -> None:
        caption = mix_caption(self.mix())
        self.caption.setText(caption)
        self.setToolTip(f"Risk level {self.risk():.1f} of "
                        f"{forecast.MAX_RISK_LEVEL}: {caption}")


#: Dynamic property carrying "is this toggle currently ON", as the string
#: "on"/"off" so a QSS attribute selector can match it. A bool property matches
#: too in Qt5, but only against the literal "true"; the strings say what they
#: mean and survive a repolish unambiguously.
TOGGLE_ACTIVE_PROP = "active"


def _on_text_for(accent: str) -> str:
    """Legible label color on top of ``accent``, chosen by the accent's own
    lightness. The two themes' accents sit on opposite sides of this line -- the
    light theme's is a deep blue (white text), the dark theme's a pale one (near
    black) -- so a hardcoded white would have made the dark-mode highlight
    white-on-pale-blue, which is exactly the low-contrast complaint this styling
    exists to answer."""
    return "#ffffff" if QColor(accent).lightness() < 140 else "#10141a"


def toggle_qss(theme: str | None = None) -> str:
    """The stylesheet that makes a checkable QPushButton LOOK checked.

    It has to be spelled out here because the app's own QSS (``ui/style.py``,
    ``build_qss``) gives QPushButton a background, a border and hover/pressed
    fills but NO ``:checked`` rule -- and once a stylesheet paints a button, Qt
    stops drawing the native style's sunken checked chrome entirely. The result
    was a mode pair and a What If button that looked identical on and off
    (reported). Fill, label color and a bold weight all move together, so the
    state is readable without relying on color alone.

    ``theme`` names a theme for inspection ('light'/'dark'); the default follows
    the ACTIVE accent, so a user's accent override styles the toggle too."""
    accent = style.accent_color() if theme is None else style.palette_for(theme)["blue"]
    on_text = _on_text_for(accent)
    c = QColor(accent)
    hover = (c.lighter(115) if on_text == "#ffffff" else c.darker(112)).name()
    return f"""
QPushButton[{TOGGLE_ACTIVE_PROP}="on"] {{
    background: {accent};
    color: {on_text};
    border: 1px solid {accent};
    font-weight: bold;
}}
QPushButton[{TOGGLE_ACTIVE_PROP}="on"]:hover {{ background: {hover}; border-color: {hover}; }}
QPushButton[{TOGGLE_ACTIVE_PROP}="on"]:pressed {{ background: {hover}; }}
"""


def set_toggle_active(button, on: bool) -> None:
    """Repaint ``button`` in (or out of) its highlighted state."""
    button.setProperty(TOGGLE_ACTIVE_PROP, "on" if on else "off")
    button.style().unpolish(button)
    button.style().polish(button)
    button.update()


def is_toggle_highlighted(button) -> bool:
    """Whether ``button`` is currently drawn highlighted."""
    return button.property(TOGGLE_ACTIVE_PROP) == "on"


def install_toggle_highlight(button) -> None:
    """Make a checkable QPushButton show its checked state, from then on.

    The highlight is driven off the button's OWN ``toggled`` signal rather than
    off the call sites, so it follows the state whatever moved it: a click, a
    programmatic ``set_mode``/``set_what_if``, a refresh, or a QButtonGroup
    silently unchecking the other mode. Wiring each caller instead is how a
    checked-looking button and the actual mode drift apart."""
    button.setStyleSheet(toggle_qss())
    button.toggled.connect(lambda on, b=button: set_toggle_active(b, on))
    set_toggle_active(button, button.isChecked())


def restyle_toggle(button) -> None:
    """Re-read the theme for ``button``'s highlight (no-op if unchanged).

    Only called on a style/palette change, and only writes when the stylesheet
    actually differs, so it cannot loop against the StyleChange it handles."""
    qss = toggle_qss()
    if button.styleSheet() != qss:
        button.setStyleSheet(qss)
    set_toggle_active(button, button.isChecked())


class WhatIfConnectors(QWidget):
    """Two labelled arrows from the What If button to the controls it turns on.

    What If is a MODE: while it is on, the inflow arrows and the thermometer
    stop reporting what was measured and start accepting what the user wants to
    try. Nothing on screen said so -- the button lit up, and the two controls it
    had just changed the meaning of were elsewhere in the band. These connectors
    are that sentence, drawn: one arrow up to the arrows labelled "change
    contributions", one down to the thermometer labelled "change asset mix".

    Pure decoration, so it is mouse-transparent. That attribute is safe HERE and
    is not elsewhere on this page: Qt skips a mouse-transparent widget's whole
    SUBTREE when picking a mouse receiver, which is what made the band's own
    controls dead when it was tried there -- but this overlay has no children,
    and everything under it (the arrows, the thermometer, What If itself) has to
    keep its clicks.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("whatIfConnectors")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._legs = []                      # [(from_y, to_y, label)]
        self.hide()

    def set_legs(self, legs) -> None:
        """``[(start_y, end_y, label)]`` in THIS widget's coordinates."""
        self._legs = list(legs)
        self.update()

    def paintEvent(self, event):             # noqa: N802 (Qt's name)
        if not self._legs:
            return
        pal = charts._active_palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(pal["highlight"]), CONNECTOR_WIDTH)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        font = _scaled_font(self.font(), CONNECTOR_FONT_SCALE)
        p.setFont(font)
        metrics = QFontMetrics(font)
        for start_y, end_y, label in self._legs:
            x = CONNECTOR_LINE_X
            p.setPen(pen)
            p.drawLine(x, int(start_y), x, int(end_y))
            # Arrowhead at the FAR end, pointing the way the meaning travels.
            step = CONNECTOR_HEAD if end_y > start_y else -CONNECTOR_HEAD
            head = QPolygonF([QPointF(x, end_y),
                              QPointF(x - CONNECTOR_HEAD * 0.7, end_y - step),
                              QPointF(x + CONNECTOR_HEAD * 0.7, end_y - step)])
            p.setBrush(QBrush(QColor(pal["highlight"])))
            p.setPen(Qt.NoPen)
            p.drawPolygon(head)
            p.setPen(QPen(QColor(pal["text"])))
            text_x = x + CONNECTOR_HEAD + 4
            box = QRect(text_x, int(min(start_y, end_y)),
                        max(1, self.width() - text_x - 2),
                        max(1, int(abs(end_y - start_y))))
            p.drawText(box, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap,
                       metrics.elidedText(label, Qt.ElideRight,
                                          max(1, box.width() * 2)))
        p.end()


class WhatIfBar(QWidget):
    """The What If toggle, its Reset and the name of the scope it is acting on,
    on the center line between the arrows and the thermometer -- the two things
    it makes editable.

    The bar is ALWAYS enabled. It used to disable itself whenever a ring wedge
    was selected, on the theory that a what-if was a statement about the whole
    portfolio; that made the one question the control exists to answer
    unaskable (reported: "being able to change the inflow is meaningless as a
    tiny inflow in a small account can't move the needle vs a large total").
    The projection is scoped to the selection instead, which is why the bar
    carries a scope label: a fan drawn for one small account and misread as the
    portfolio's would be worse than no fan at all, so the scope is stated in
    words directly under the button rather than left to be inferred from which
    wedge happens to be pulled out."""

    toggled = pyqtSignal(bool)
    resetRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("whatIfBar")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        row = QWidget(self)
        row.setObjectName("whatIfRow")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(4)
        self.button = QPushButton("What If", row)
        self.button.setObjectName("whatIfToggle")
        self.button.setCheckable(True)
        self.button.setToolTip(WHAT_IF_TOOLTIP)
        self.button.toggled.connect(self._on_toggled)
        # What If is a MODE, not a command: while it is on, the arrows and the
        # thermometer mean something different, so the button has to say so.
        install_toggle_highlight(self.button)
        self.reset = QPushButton("Reset", row)
        self.reset.setObjectName("whatIfReset")
        self.reset.setToolTip("Put the measured inflows and the measured mix back")
        self.reset.setEnabled(False)
        self.reset.clicked.connect(lambda _checked=False: self.resetRequested.emit())
        row_lay.addWidget(self.button, 1)
        row_lay.addWidget(self.reset, 0)
        lay.addWidget(row)

        self._subject = WHAT_IF_ALL_SUBJECT
        self.scope = QLabel(self)
        self.scope.setObjectName("whatIfScope")
        self.scope.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        font = QFont(self.scope.font())
        font.setPointSizeF(max(6.0, font.pointSizeF() - 1.0))
        font.setItalic(True)
        self.scope.setFont(font)
        lay.addWidget(self.scope)
        self._sync_scope_label()

    def _on_toggled(self, on: bool) -> None:
        self.reset.setEnabled(bool(on))
        self.toggled.emit(bool(on))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # getattr: Qt delivers StyleChange during construction, before the row
        # exists.
        button = getattr(self, "button", None)
        if button is not None and event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            restyle_toggle(button)

    def is_active(self) -> bool:
        return self.button.isChecked()

    def set_active(self, on: bool) -> None:
        self.button.setChecked(bool(on))

    def is_available(self) -> bool:
        """Kept because callers ask; nothing ever answers False any more."""
        return self.button.isEnabled()

    # -- the scope ----------------------------------------------------------
    def scope_subject(self) -> str:
        """The scope's full name, whatever the label had room to draw."""
        return self._subject

    def scope_text(self) -> str:
        return self.scope.text()

    def set_scope(self, subject: str) -> None:
        """Name what the What If fan is about. ``subject`` is whatever the
        center line is about -- an account name, a ticker, or
        :data:`WHAT_IF_ALL_SUBJECT`."""
        self._subject = str(subject or WHAT_IF_ALL_SUBJECT)
        self._sync_scope_label()

    def _sync_scope_label(self) -> None:
        full = WHAT_IF_SCOPE_PREFIX + self._subject
        # The band is a fixed-width column, so a long account name has to be
        # elided here rather than being allowed to widen the bar's size hint.
        width = self.width() or LEFT_BAND_WIDTH
        self.scope.setText(QFontMetrics(self.scope.font()).elidedText(
            full, Qt.ElideRight, max(40, width)))
        self.scope.setToolTip(full)

    def resizeEvent(self, event):    # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        self._sync_scope_label()


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
def _frame(name: str, parent=None, *, minimum=(0, 0)) -> QFrame:
    """A reserved, empty region. Named so a later task fills it by name rather
    than by counting children."""
    f = QFrame(parent)
    f.setObjectName(name)
    f.setFrameShape(QFrame.NoFrame)
    f.setMinimumSize(*minimum)
    return f


def corner_colors(pal=None) -> dict:
    """Every color the corner launchers' graphics are drawn with.

    The same rule as :func:`chart_colors`, and for the same reported reason
    ("poor contrast in both dark and light mode"): every color is a SEMANTIC
    palette name resolved at PAINT time, never a fixed hex literal. It matters
    more here than it does for the charts -- a hand-painted glyph has no
    matplotlib default to fall back on, so a hex picked against a white page is
    simply invisible on the dark one.

    The category hues come from :func:`charts.wedge_colors`, the SAME list the
    ring hands its wedges. That is deliberate: the two pies in the Rebalancing
    glyph are then recognisably the ring's own colors rather than a second
    palette a later theme would have to be taught about.
    """
    pal = charts._active_palette() if pal is None else pal
    return {
        "title": pal["text"],
        "muted": pal["muted"],
        "line": pal["line"],
        "accent": pal["blue"],
        "negative": pal["negative"],
        "wedges": charts.wedge_colors([str(i) for i in range(CORNER_WEDGE_COUNT)]),
    }


class CornerButton(QPushButton):
    """A corner launcher: a prominent title over a themed graphic.

    It word-wraps its caption, which a plain ``QPushButton`` will not do: the
    corner boxes are near-square offcuts of the ring's bounding box (see
    :meth:`RingArea.corner_rects`), so "Capital Gains and Taxes" on one line is
    clipped to "Capital Gai..." at any realistic window width. The frame is still
    drawn by the style -- only the contents are ours -- so the button keeps the
    platform's hover, focus and pressed appearance, and it is still an ordinary
    ``QPushButton`` for the mouse, for focus and for ``clicked``.

    Reported: four identical gray boxes of small text gave no clue which was
    which, so each now paints a picture of what it OPENS (``CORNER_GLYPHS``)
    under a title deliberately larger and bolder than the body font. The layout
    is shared -- ``CORNER_PAD`` margin, title on top, graphic in what is left --
    because the four are seen together and any per-corner tuning would show.

    Everything is painted with ``QPainter`` from ``corner_colors()``: no image
    files, so nothing can go stale against the theme, and a graphic drawn from
    palette names is legible in dark and light alike. The title never gives up
    room to the graphic -- it steps down through ``CORNER_TITLE_SCALES`` until
    its wrapped block fits, and the graphic is dropped entirely when the box is
    too small to show one (``CORNER_ART_MIN``).
    """

    def __init__(self, text: str, parent=None, *, glyph: str = ""):
        super().__init__(text, parent)
        self.glyph = glyph

    # -- layout -------------------------------------------------------------
    def _title_font(self, width: int, max_height: int):
        """``(font, height)`` for the caption wrapped into ``width``.

        Largest size first, stopping at the first that fits inside
        ``max_height``. The candidate list is finite, so this cannot loop; if
        even the smallest overflows the block is simply capped, which is the
        old behavior rather than a new failure."""
        font = QFont(self.font())
        font.setBold(True)
        base = font.pointSizeF()
        flags = Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap
        height = max_height
        for scale in CORNER_TITLE_SCALES:
            if base > 0:
                font.setPointSizeF(max(6.0, base * scale))
            metrics = QFontMetrics(font)
            height = metrics.boundingRect(0, 0, width, 0, flags,
                                          self.text()).height()
            if height <= max_height or base <= 0:
                break
        return font, min(height, max_height)

    def paintEvent(self, event):       # noqa: N802 (Qt's name)
        opt = QStyleOptionButton()
        # initFrom, not the protected initStyleOption: this needs only the
        # palette, font and enabled/focus state, and building the option here
        # keeps the class working on any PyQt build.
        opt.initFrom(self)
        opt.rect = self.rect()
        opt.state |= (QStyle.State_Sunken if self.isDown()
                      else QStyle.State_Raised)
        p = QStylePainter(self)
        self._paint_tile(p)

        inner = self.rect().adjusted(CORNER_PAD, CORNER_PAD,
                                     -CORNER_PAD, -CORNER_PAD)
        if inner.width() <= 0 or inner.height() <= 0:
            return
        col = corner_colors()
        cap = max(1, int(inner.height() * CORNER_TITLE_MAX_FRACTION))
        font, title_h = self._title_font(inner.width(), cap)
        p.setFont(font)
        p.setPen(QColor(col["title"]))
        p.drawText(QRect(inner.left(), inner.top(), inner.width(), title_h),
                   Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self.text())

        art = inner.adjusted(0, title_h + CORNER_PAD, 0, 0)
        if art.width() < CORNER_ART_MIN or art.height() < CORNER_ART_MIN:
            return
        painter = getattr(self, "_paint_" + self.glyph, None) if self.glyph else None
        if painter is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        painter(p, QRectF(art), col)
        p.restore()

    def _paint_tile(self, p) -> None:
        """The tile's own rounded, bordered face.

        Replaces ``CE_PushButtonBevel``. The platform bevel was fine when these
        filled the corner offcut, but at button size it reads as a sunken panel
        and its 2-3px radius is invisible; reported as "add a border around them
        and round the corners a bit more". Fill, border and the pressed shade
        all come from the ACTIVE palette's button names, so the tiles follow a
        theme switch exactly as the rest of the page does.
        """
        pal = charts._active_palette()
        fill = pal["btn_pressed"] if self.isDown() else (
            pal["btn_hover"] if self.underMouse() else pal["btn_bg"])
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        # Inset by half the pen so the stroke lands INSIDE the widget: a pen
        # centered on the rect's edge is clipped to half its width and the
        # border looks thinner on two sides than on the other two.
        half = CORNER_BORDER / 2.0
        box = QRectF(self.rect()).adjusted(half, half, -half, -half)
        p.setPen(QPen(QColor(pal["btn_border"]), CORNER_BORDER))
        p.setBrush(QBrush(QColor(fill)))
        p.drawRoundedRect(box, CORNER_RADIUS, CORNER_RADIUS)
        p.restore()

    # -- the shared drawing primitives --------------------------------------
    @staticmethod
    def _pie(p, box: QRectF, fractions, wedges) -> None:
        """A filled pie of ``fractions`` (summing to 1) in ``wedges``' colors,
        starting at twelve o'clock and going clockwise -- the direction the ring
        itself reads in."""
        p.setPen(Qt.NoPen)
        start = 90.0
        for i, frac in enumerate(fractions):
            span = -360.0 * float(frac)
            p.setBrush(QColor(wedges[i % len(wedges)]))
            p.drawPie(box, int(round(start * 16)), int(round(span * 16)))
            start += span

    @staticmethod
    def _arrow(p, x0: float, x1: float, y: float, size: float, color) -> None:
        """A left-to-right arrow along ``y``, its head ``size`` px long."""
        head = min(size, (x1 - x0) * 0.6)
        if head <= 1.0:
            return
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        shaft = max(1.0, head * 0.30)
        p.drawRect(QRectF(x0, y - shaft / 2.0, (x1 - x0) - head, shaft))
        p.drawPolygon(QPolygonF([
            QPointF(x1, y),
            QPointF(x1 - head, y - head * 0.55),
            QPointF(x1 - head, y + head * 0.55),
        ]))

    # -- one glyph per corner -----------------------------------------------
    def _paint_rebalance(self, p, rect: QRectF, col) -> None:
        """Two pies, the same colors in different proportions, an arrow between
        them: today's drifted mix on the left, the target on the right -- which
        is exactly the pair the Rebalancing dialog puts side by side."""
        gap = max(8.0, rect.width() * 0.16)
        size = min(rect.height(), (rect.width() - gap) / 2.0)
        if size < 10.0:
            return
        top = rect.top() + (rect.height() - size) / 2.0
        left = QRectF(rect.left(), top, size, size)
        right = QRectF(rect.right() - size, top, size, size)
        self._pie(p, left, CORNER_PIE_DRIFTED, col["wedges"])
        self._pie(p, right, CORNER_PIE_TARGET, col["wedges"])
        self._arrow(p, left.right() + 1.0, right.left() - 1.0,
                    top + size / 2.0, min(gap * 0.8, size * 0.5), col["accent"])

    def _paint_tax(self, p, rect: QRectF, col) -> None:
        """Two gains of the same size, one with a big bite taken out of it and
        one with a small one: the short-term lot and the long-term lot the
        Capital Gains report exists to tell apart."""
        # Two bars, kept near each other and centered: pushed out to the box's
        # edges they read as two unrelated figures instead of one comparison,
        # which is the entire point of the glyph.
        bar_w = rect.width() * 0.30
        gap = rect.width() * 0.16
        x0 = rect.left() + (rect.width() - (2.0 * bar_w + gap)) / 2.0
        base = rect.bottom() - 1.0
        total = rect.height() - 2.0
        p.setPen(Qt.NoPen)
        for i, taxed in enumerate((0.50, 0.15)):
            x = x0 + i * (bar_w + gap)
            bite = total * taxed
            p.setBrush(QColor(col["wedges"][2]))        # the ring's green: kept
            p.drawRect(QRectF(x, base - (total - bite), bar_w, total - bite))
            p.setBrush(QColor(col["negative"]))         # what the tax takes
            p.drawRect(QRectF(x, base - total, bar_w, bite))
        p.setPen(QPen(QColor(col["line"]), 1))
        p.drawLine(QPointF(rect.left(), base + 1.0),
                   QPointF(rect.right(), base + 1.0))

    def _paint_performance(self, p, rect: QRectF, col) -> None:
        """A rising series with an arrow across it -- gain, income and annual
        return per holding, which is what the Performance Report ranks."""
        n = CORNER_WEDGE_COUNT
        gap = rect.width() / (n * 4.0)
        bar_w = (rect.width() - gap * (n - 1)) / n
        base = rect.bottom() - 1.0
        total = rect.height() - 2.0
        p.setPen(Qt.NoPen)
        for i, frac in enumerate((0.30, 0.48, 0.66, 0.90)):
            p.setBrush(QColor(col["wedges"][i % len(col["wedges"])]))
            p.drawRect(QRectF(rect.left() + i * (bar_w + gap),
                              base - total * frac, bar_w, total * frac))
        p.setPen(QPen(QColor(col["line"]), 1))
        p.drawLine(QPointF(rect.left(), base + 1.0),
                   QPointF(rect.right(), base + 1.0))
        # The trend arrow rides OVER the bars, so it is drawn in the text color
        # rather than the accent: that is the one hue guaranteed to contrast
        # with the page AND with every wedge color under it, in both themes.
        p.save()
        p.translate(rect.center())
        p.rotate(-28.0)
        span = min(rect.width(), rect.height() * 2.2) * 0.9
        # A head sized off the box height alone swallows the bars at realistic
        # corner sizes (rendered and looked at): the trend is an annotation over
        # the series, so it is kept to roughly one bar's width.
        self._arrow(p, -span / 2.0, span / 2.0, 0.0,
                    min(rect.height() * 0.26, span * 0.18), col["title"])
        p.restore()

    def _paint_categories(self, p, rect: QRectF, col) -> None:
        """A legend: a colored swatch against a named class, three times over.
        Setting asset categories is naming those colors, and the dialog this
        opens is a table of exactly these rows."""
        rows = 3
        gap = rect.height() / (rows * 3.0)
        row_h = (rect.height() - gap * (rows - 1)) / rows
        swatch = min(row_h, rect.width() * 0.24)
        rule_h = max(2.0, swatch * 0.34)
        p.setPen(Qt.NoPen)
        for i in range(rows):
            y = rect.top() + i * (row_h + gap)
            p.setBrush(QColor(col["wedges"][i % len(col["wedges"])]))
            p.drawRect(QRectF(rect.left(), y + (row_h - swatch) / 2.0,
                              swatch, swatch))
            # Ragged rule lengths, so the row reads as a NAME beside the swatch
            # rather than as a bar chart of three equal values.
            width = (rect.width() - swatch - gap) * (1.0, 0.72, 0.88)[i]
            p.setBrush(QColor(col["muted"]))
            p.drawRect(QRectF(rect.left() + swatch + gap,
                              y + (row_h - rule_h) / 2.0, max(2.0, width),
                              rule_h))


def ring_slices(conn, mode: str, as_of: Optional[str] = None, *,
                account_ids=None) -> list:
    """``[(key, label, cents)]`` for the ring in ``mode``.

    accounts: one slice per investment account, valued by
    ``portfolio.account_valuation`` -- which dispatches on the account's KIND,
    so a crypto wallet is valued by the crypto engine rather than by the
    brokerage one (calling the latter on a wallet reports its own balance as
    large NEGATIVE cash, and the account then vanishes from the ring through
    the ``total <= 0`` guard below). securities: one slice per security
    across all of them, via ``portfolio.allocation`` (which is where the
    options exclusion of SRD 5.8e-9 already lives, and which dispatches the
    same way).

    ``account_ids`` is the page's account scope (the gear); ``None`` means
    every investment account. It bounds BOTH modes -- narrowing the accounts
    narrows the securities the ring can show, because a security is only ever
    held in an account."""
    if mode not in RING_MODES:
        raise ValueError(f"unknown ring mode {mode!r}; one of {RING_MODES}")
    end = as_of or _today()
    ids = _account_ids(conn) if account_ids is None else list(account_ids)
    if mode == MODE_ACCOUNTS:
        out = []
        for account_id in ids:
            total = portfolio.account_valuation(conn, account_id, end).total
            if total <= 0:
                continue
            row = conn.execute("SELECT name FROM accounts WHERE id=?",
                               (account_id,)).fetchone()
            out.append((str(account_id), (row["name"] if row else str(account_id)),
                        total))
        return out
    alloc = portfolio.allocation(conn, account_ids=ids, as_of=end)
    out = [(s.key, s.label, s.value) for s in alloc.by_security if s.value > 0]
    # Cash last, so it reads as the remainder rather than as a holding, and only
    # when there is some: an account swept to zero should not draw a wedge.
    cash = _scope_cash(conn, ids, end)
    if cash > 0:
        out.append((CASH_KEY, CASH_LABEL, cash))
    return out


class InvestmentDashboardPage(QWidget):
    """The dashboard page. Built against a connection, refreshed through
    ``mark_stale`` / ``refresh_if_stale`` the way every other panel that the
    main window parks in a stack is (``ui/projection_dialogs.py``)."""

    filterChanged = pyqtSignal(object)      # ("account", id) / ("security", sym) / None

    def __init__(self, conn, parent=None, *, as_of: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self.as_of = as_of or _today()
        self._mode = MODE_ACCOUNTS
        self._filter = None
        self._stale = False
        # The gear's account selection; None means "every investment account",
        # the same contract ``ReportFilterBar.selected_account_ids`` uses. Set
        # before _build, which builds the gear and then refreshes on it.
        self._account_scope = None
        self.arrows: list = []
        self.arrow_widgets: list = []
        self.placeholders: dict = {}
        self.corner_buttons: dict = {}
        # Modeless report windows opened from the corners. They are kept only so
        # they are not garbage-collected the instant the launcher returns.
        self._report_windows: list = []
        # What If lives entirely here: measured values are what the database
        # says, what-if values are what the user is currently imagining, and
        # nothing ever moves from the second set into the first (design 2.5).
        self._what_if = False
        self._measured_risk = 0.0
        self._measured_inflows: dict = {}
        self._what_if_inflows: dict = {}
        self._build()
        self.refresh()

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        # One row, no rows above or below: the ring area is the full height of
        # the page and the corner launchers ride on top of it as overlays.
        #
        # The band is NOT a cell of this layout. Its right edge has to sit on
        # the ring's left tangent, and the tangent is a function of the ring
        # area's width -- which, if the band were a cell, would be a function of
        # the band. That circle is why the band is an overlay child placed by
        # :meth:`_layout_left_band`; the layout only reserves its column as a
        # left margin, so the ring area's geometry never depends on the band.
        root = QHBoxLayout(self)
        root.setContentsMargins(
            LEFT_BAND_WIDTH + BAND_RING_GAP + BAND_SPACING, 0, 0, 0)
        root.setSpacing(BAND_SPACING)
        self._build_left_band()
        root.addWidget(self._build_ring_area(), 1)
        self._build_corners()
        self._build_gear()
        # Open on the scope this ledger was last given (reported: "so that I
        # don't have to keep excluding the same accounts every time"). Set
        # directly rather than through set_account_scope, which exists to react
        # to a CHANGE -- it clears the wedge filter and marks the page stale,
        # and there is nothing yet to clear or redraw at construction.
        remembered = prefs.dashboard_account_scope(self._db_path())
        if remembered is not None:
            allowed = set(_account_ids(self.conn))
            self._account_scope = [int(a) for a in remembered if int(a) in allowed]
        # The reported stack, bottom to top: band, then the ring area (whose own
        # children are the corner launchers under the ring). The ring area is
        # masked to the union of its children, so raising it over the band costs
        # the band no clicks except where a launcher really covers a block --
        # see :class:`RingArea`.
        self.ring_area.raise_()

    def _build_corners(self) -> None:
        """The four launchers, as overlays in the ring area's corners.

        ``set_corners`` reparents and RAISES them over the ring canvas, which is
        painted across the whole area -- an un-raised button would be both
        invisible and unhittable. ``placeholders`` keeps its name and its keys:
        it is the dict of what sits in each reserved slot, and that is now a
        button rather than an empty frame."""
        for name in CORNER_NAMES:
            btn = CornerButton(CORNER_LABELS[name], self.ring_area,
                               glyph=CORNER_GLYPHS[name])
            btn.setObjectName(name)
            btn.setToolTip(CORNER_TOOLTIPS[name])
            # Bound late through getattr so a subclass -- or a test -- that
            # overrides one of the open_* methods is the one that runs.
            method = CORNER_ACTIONS[name]
            btn.clicked.connect(lambda _c, m=method: getattr(self, m)())
            self.placeholders[name] = btn
        self.corner_buttons = dict(self.placeholders)
        self.ring_area.set_corners(dict(self.placeholders))

    # -- the corner launchers ----------------------------------------------
    # Each corner is one small public method, so the wiring (which button calls
    # what) and the launch (what that opens) can be asserted separately. The two
    # seams below are where a window is actually shown; a test overrides THOSE,
    # because a QDialog.exec_()'d under the offscreen platform never returns.

    def _open_report(self, spec):
        """Open the shared report window on ``spec``. Modeless, so the window is
        retained in a list -- an unreferenced one is garbage-collected the moment
        this returns, and a single attribute would evict the previous report when
        a second corner is clicked (the same reasoning as
        ``widgets._open_report_window``)."""
        from mammon.ui.report_window import ReportWindow
        win = ReportWindow(self.conn, parent=self, spec=spec)
        self._report_windows.append(win)
        win.show()
        return win

    def _exec_dialog(self, dlg):
        """Run a modal launcher dialog. The ONE place this page enters a modal
        loop, and overridable for exactly that reason."""
        return dlg.exec_()

    def open_capital_gains(self):
        """Top left: the Capital Gains and Taxes report -- which lots are
        long-term, which are short, when each short lot turns long, and the tax
        consequence of selling before it does."""
        from mammon.ui.report_window import CAPITAL_GAINS_SPEC
        return self._open_report(CAPITAL_GAINS_SPEC)

    def open_performance_report(self):
        """Top right: the existing Investment Performance report, through the
        same shared window the Reports menu opens it in."""
        from mammon.ui.report_window import INVESTMENT_PERFORMANCE_SPEC
        return self._open_report(INVESTMENT_PERFORMANCE_SPEC)

    def open_asset_categories(self):
        """Bottom left: the Asset Allocation report.

        Points at ``ui/asset_allocation``, not the older ``AllocationDialog``.
        This corner exists so a user can GIVE a holding its asset mix, and the
        old window could only ever display one: ``set_mixture`` had no caller
        but the yfinance fetch, so a 401(k) fund with no public ticker could not
        be described at all. The old window keeps the property scopes and stays
        reachable from the register's gear.

        The page's own account scope is passed through, so the report covers
        what the dashboard is showing rather than re-deciding it.
        """
        from mammon.ui.asset_allocation import AssetAllocationWindow
        dlg = AssetAllocationWindow(self.conn, parent=self,
                                    account_ids=self.account_scope(),
                                    as_of=self.as_of)
        # A mix changed here moves every figure on the page behind it.
        dlg.changed.connect(self.refresh)
        return self._exec_dialog(dlg)

    def open_rebalancing(self):
        """Bottom right: Target & Drift -- the target asset mix and how far the
        real one has strayed from it."""
        from mammon.ui.rebalance_dialog import RebalanceDialog
        return self._exec_dialog(RebalanceDialog(self.conn, parent=self))

    def _build_left_band(self) -> QWidget:
        """The band has NO layout: :meth:`_layout_left_band` places its four
        blocks, because two of them align with widgets that live inside the
        hole rather than with anything in the band."""
        band = QWidget(self)
        band.setObjectName("leftBand")
        band.setFixedWidth(LEFT_BAND_WIDTH)
        # The band overlaps the ring area's left corner launchers, and the ring
        # area is raised over it, so that overlap already resolves in the
        # launchers' favour by z-order alone. It must NOT be made
        # mouse-transparent to arrange that: Qt skips a mouse-transparent
        # widget's whole subtree when picking a mouse receiver, so the attribute
        # here made the arrows, the mode buttons, What If and the thermometer
        # unclickable. Nothing sits under the band that needs its clicks.

        # Top: the arrows, centered on the value-history chart.
        self.arrow_box = QWidget(band)
        self.arrow_box.setObjectName("arrowBand")
        self.arrow_layout = QVBoxLayout(self.arrow_box)
        self.arrow_layout.setContentsMargins(0, 0, 0, 0)
        self.arrow_layout.setSpacing(6)

        # The mode toggle is NOT a band block any more -- it is handed to the
        # ring area and placed inside the ring near the top (reported). Built
        # parented to the page so it has an owner until set_mode_row adopts it.
        self.mode_row = QWidget(self)
        self.mode_row.setObjectName("modeRow")
        mode_lay = QHBoxLayout(self.mode_row)
        mode_lay.setContentsMargins(0, 0, 0, 0)
        mode_lay.setSpacing(4)
        self.mode_buttons = {}
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for mode, text in ((MODE_ACCOUNTS, "Accounts"), (MODE_SECURITIES, "Securities")):
            btn = QPushButton(text, self.mode_row)
            btn.setCheckable(True)
            btn.setChecked(mode == self._mode)
            btn.clicked.connect(lambda _c, m=mode: self.set_mode(m))
            self.mode_group.addButton(btn)
            mode_lay.addWidget(btn, 1)
            self.mode_buttons[mode] = btn
            # The pair is a two-state switch, so which one is live has to be
            # visible; checkable alone buys nothing under the app stylesheet.
            install_toggle_highlight(btn)

        self.what_if_bar = WhatIfBar(band)
        # Drawn OVER the band's blocks, so it is a sibling placed after them
        # rather than a child of any one of them; mouse-transparent, so the
        # arrows and the thermometer it crosses keep their clicks.
        self.connectors = WhatIfConnectors(band)
        # The connectors say what the MODE turned on, so they follow the toggle
        # rather than any redraw: a refresh does not change whether What If is
        # on, and a toggle does not go through one.
        self.what_if_bar.toggled.connect(lambda _on: self._layout_connectors())
        self.what_if_bar.toggled.connect(self._on_what_if_toggled)
        self.what_if_bar.resetRequested.connect(self.reset_what_if)

        # Bottom: the thermometer, centered on the projection fan.
        self.thermometer = Thermometer(band)
        self.thermometer.riskChanged.connect(self._on_risk_changed)

        self.left_band = band
        return band

    def _build_ring_area(self) -> QWidget:
        self.ring = RingCanvas([])
        self.ring.sliceClicked.connect(self._on_slice_clicked)

        hole = QWidget()
        hole.setObjectName("ringHole")
        hole_lay = QVBoxLayout(hole)
        hole_lay.setContentsMargins(0, 0, 0, 0)
        # No spacing: the gap the center line needs is the reserved strip below,
        # and a layout gap on either side of it only adds to that (reported:
        # "the space between the top plot and the centerline text can be
        # reduced").
        hole_lay.setSpacing(0)
        self.history_chart = ValueHistoryChart(hole)
        self.history_chart.set_years(DEFAULT_HISTORY_YEARS)
        self.history_chart.periodChanged.connect(self._on_history_period)
        self.projection_chart = ProjectionChart(hole)
        self.projection_chart.set_years(DEFAULT_PROJECTION_YEARS)
        self.projection_chart.horizonChanged.connect(self._on_projection_horizon)
        # The center line is NOT in this layout any more -- it is an overlay on
        # the ring area, so it can run the full width of the circle instead of
        # being clipped to the plots' rectangle (reported). What stays here is a
        # blank strip of its height, keeping the two plots (both stretch 1) off
        # the midline the overlay sits on.
        self.center_gap = QWidget(hole)
        self.center_gap.setObjectName("centerGap")
        self.center_gap.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # Below the gap, a second blank strip of TWICE the lift. Both charts
        # stretch equally, so the free height splits evenly between them and the
        # fixed pair sits in the middle: adding ``s`` below the gap moves the
        # gap's center up by exactly ``s / 2``. That is how the reserved strip
        # tracks RingArea.center_lift() without either of them hardcoding the
        # other's number.
        self.center_lift_spacer = QWidget(hole)
        self.center_lift_spacer.setObjectName("centerLiftSpacer")
        self.center_lift_spacer.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        hole_lay.addWidget(self.history_chart, 1)
        hole_lay.addWidget(self.center_gap, 0)
        hole_lay.addWidget(self.center_lift_spacer, 0)
        hole_lay.addWidget(self.projection_chart, 1)
        self.hole = hole

        self.ring_area = RingArea(self.ring, hole, self)
        # The selectors leave the hole: above the top chart, below the bottom
        # one, where the ring -- painted in front of the hole -- cannot cover
        # them (reported).
        # Each selector travels inside a header that also NAMES its plot: the
        # top one names the current scope, the bottom one says what a fan of
        # future values is and disclaims it (5.4).
        self.history_header = PlotHeader(self.history_chart.period,
                                         title=performance_title(""))
        self.projection_header = PlotHeader(self.projection_chart.period,
                                            title=PROJECTION_TITLE,
                                            note=PROJECTION_DISCLAIMER)
        self.ring_area.set_selectors(top=self.history_header,
                                     bottom=self.projection_header)
        self.center = CenterLineWidget(parent=self.ring_area)
        self.ring_area.set_center(self.center)
        self._sync_center_gap()
        return self.ring_area

    def _sync_center_gap(self) -> None:
        """Reserve exactly the overlaid center line's height in the hole.

        Called after every ``set_line``: the line's height follows its font and
        its content, and a stale reservation either lets the top plot run under
        the text or leaves a band of dead air under it."""
        gap = getattr(self, "center_gap", None)
        if gap is None:
            return
        gap.setFixedHeight(max(1, self.center.sizeHint().height()))
        spacer = getattr(self, "center_lift_spacer", None)
        if spacer is not None:
            # Twice the lift: see the comment where the spacer is built.
            spacer.setFixedHeight(max(0, 2 * self.ring_area.center_lift()))

    def _build_gear(self) -> None:
        """The accounts gear, at the top just left of the Performance Report
        launcher (reported).

        It is the app's EXISTING customization control -- ``CustomizeDialog``
        plus ``customize_button`` from ``ui/report_filters.py``, the same pair
        every report window uses -- with the category picker switched off, so
        the accounts it offers and the way it remembers them are the ones the
        user already knows. Its selection becomes this page's account scope.

        The picker is restricted to ``ledger.INVESTMENT_LIKE_TYPES`` (crypto
        counts), so it opens with only the investment accounts offered and all
        of them ticked (reported: "default the customization picker to have only
        selected the investment accounts"). It listed the WHOLE roster before,
        all ticked, and :meth:`set_account_scope` then silently dropped every
        non-investment id -- checkboxes that could not do anything."""
        from mammon.ui.report_filters import (
            CustomizeDialog, customize_button, set_account_picker_ids)
        start = years_before(self.as_of, MAX_HISTORY_YEARS)
        self.customize_dialog = CustomizeDialog(
            self.conn, start, self.as_of, show_accounts=True,
            account_types=ledger.INVESTMENT_LIKE_TYPES, parent=self)
        # Pre-tick whatever this ledger was last scoped to, so the gear opens
        # showing the user's own selection rather than everything.
        saved = prefs.dashboard_account_scope(self._db_path())
        if saved is not None and self.customize_dialog.filters.account_list is not None:
            set_account_picker_ids(self.customize_dialog.filters.account_list, saved)
        self.customize_dialog.applied.connect(self._on_accounts_customized)
        self.gear = customize_button(self.customize_dialog, parent=self.ring_area)
        self.gear.setObjectName("accountGear")
        self.gear.setToolTip("Choose which accounts this dashboard covers")
        self.ring_area.set_gear(self.gear)
        self.ring_area.set_mode_row(self.mode_row)

    def _on_accounts_customized(self) -> None:
        """Adopt the gear's account selection, remember it, and redraw on it."""
        chosen = self.customize_dialog.filters.selected_account_ids()
        self.set_account_scope(chosen)
        # Store what the page RESOLVED, not the raw picker output: set_account_scope
        # drops non-investment ids, and saving the unfiltered list would restore a
        # scope the page cannot honor.
        prefs.set_dashboard_account_scope(self._db_path(), self.account_scope())

    def _db_path(self) -> str:
        """This connection's file, for keying the remembered scope. Empty for an
        in-memory ledger, which simply shares one key -- there is no file to tell
        two of them apart, and tests pass their own QSettings anyway."""
        try:
            for _seq, name, filename in self.conn.execute("PRAGMA database_list"):
                if name == "main" and filename:
                    return str(filename)
        except Exception:
            pass
        return ""

    def account_scope(self) -> Optional[list]:
        """The gear's account selection, or None for "every investment
        account" -- the same None-means-no-filter contract ``ReportFilterBar``
        uses."""
        return None if self._account_scope is None else list(self._account_scope)

    def set_account_scope(self, account_ids) -> None:
        """Narrow the whole page -- ring, plots, center line and inflows -- to
        these accounts. ``None`` restores every investment account.

        Non-investment ids are dropped rather than trusted: the gear lists every
        account in the ledger, and valuing a checking account as a holding is
        not a thing this page can do."""
        if account_ids is None:
            self._account_scope = None
        else:
            allowed = set(_account_ids(self.conn))
            self._account_scope = [int(a) for a in account_ids if int(a) in allowed]
        # A filter chosen before the scope changed may no longer be drawable --
        # an account outside the new scope, or a security now held in none of
        # the scoped accounts. Nothing is cleared by hand here: the refresh
        # rebuilds the slices and :meth:`_sync_filter_to_ring` then takes the
        # ring's word for what is selected, which covers the security case the
        # hand-written clearing here never did.
        self.refresh()

    # -- hand placement -----------------------------------------------------
    def thermometer_slot_height(self) -> int:
        """How tall the thermometer is drawn: :data:`THERMOMETER_HEIGHT_SCALE`
        of the share an even split of the band below the arrows would give it.

        Expressed as a ratio rather than a pixel count so the rule survives a
        resize: it was two thirds of that share, and is now three quarters of
        two thirds -- one half (reported: "the thermometer can be reduced in
        height to 3/4 its current height"). The floor came down by the same
        three quarters, because a floor above the scaled height silently wins
        the argument on a short page."""
        band_h = self.left_band.height() or self.height()
        furniture = (self.mode_row.sizeHint().height()
                     + self.what_if_bar.sizeHint().height()
                     + 3 * BAND_SPACING)
        slot = max(0, band_h - furniture) // 2
        return max(THERMOMETER_MIN_HEIGHT, int(round(slot * THERMOMETER_HEIGHT_SCALE)))

    def _canvas_center_y(self, canvas) -> Optional[int]:
        """Vertical center of a hole canvas in the band's coordinates, or None
        while the widget tree has no real geometry yet."""
        if canvas is None or not canvas.height():
            return None
        top_left = canvas.mapTo(self, canvas.rect().topLeft())
        return top_left.y() + canvas.height() // 2

    def band_tangent_x(self) -> Optional[int]:
        """Page x of the ring's left outer tangent -- where the band's right
        edge belongs (reported: "I want them against the vertical line tangent
        to the left edge of the ring"). None before the ring area has geometry."""
        area = getattr(self, "ring_area", None)
        if area is None or not area.width():
            return None
        return area.mapTo(self, QPoint(0, 0)).x() + area.left_tangent_x()

    def _band_top_limit(self, band, band_top: int) -> int:
        """The first y in the band's own coordinates that is not underneath a
        raised ring-area overlay.

        The ring area is a LATER sibling raised over the band, so anything it
        draws in the band's column wins the mouse even though the band's own
        children paint there. The top corner launchers are the ones that reach
        that far left. Reported as "the inflow arrow edit is no longer
        working": on a wide window the arrow stack starts high enough that its
        first arrow sat inside ``cornerTopLeft``, so the editor was visible,
        enabled, and every click on it opened the corner's panel instead."""
        limit = 0
        area = getattr(self, "ring_area", None)
        if area is None or not area.width():
            return limit
        origin = area.mapTo(self, QPoint(0, 0))
        band_rect = QRect(band.mapTo(self, QPoint(0, 0)), band.size())
        for name, rect in area.corner_rects().items():
            if not str(name).startswith("cornerTop"):
                continue
            x, y, w, h = rect
            overlay = QRect(origin.x() + x, origin.y() + y, w, h)
            if not overlay.intersects(band_rect):
                continue
            limit = max(limit, overlay.bottom() + 1 - band_top + BAND_SPACING)
        return max(0, limit)

    def _layout_left_band(self) -> None:
        """Place the band on the ring's tangent, and its blocks against the
        *plots* rather than against the band.

        The arrows center on the value-history canvas and the thermometer on
        the projection fan; the mode row and What If fill the gap between them.
        A stretch layout cannot do this: it only knows the band's own height,
        and the canvases sit inside the hole inside the ring area.

        Nothing here may leave an arrow underneath another control. The band is
        placed by hand, so an overlap is invisible on screen -- the arrow still
        paints -- and shows up only as a click that goes somewhere else. Both
        ways that happened are defended against below: the top corner launcher
        raised over the band's column, and the arrows being clamped up into the
        mode buttons when the stack is taller than the gap."""
        band = getattr(self, "left_band", None)
        if band is None:
            return
        tangent = self.band_tangent_x()
        if tangent is not None:
            # Right-aligned to the tangent still, but held off it by
            # BAND_RING_GAP (reported: "the arrow and thermometer need a spacer
            # between them and the ring, maybe 50 pixels"), and clamped to the
            # page so a very narrow window cannot slide it off the left edge.
            band.setGeometry(max(0, tangent - BAND_RING_GAP - LEFT_BAND_WIDTH),
                             0, LEFT_BAND_WIDTH, self.height())
        if not band.height():
            return
        width = band.width()
        band_top = band.mapTo(self, band.rect().topLeft()).y()

        arrow_h = max(self.arrow_box.sizeHint().height(), 0)
        what_h = self.what_if_bar.sizeHint().height()
        therm_h = self.thermometer_slot_height()

        top_center = self._canvas_center_y(getattr(self.history_chart, "canvas", None))
        bot_center = self._canvas_center_y(getattr(self.projection_chart, "canvas", None))
        if top_center is None or bot_center is None:
            # No hole geometry yet (first show): fall back to an even split so
            # the band is never left stacked on top of itself at 0,0.
            top_center = band_top + band.height() // 4
            bot_center = band_top + 3 * band.height() // 4

        arrow_y = top_center - band_top - arrow_h // 2
        therm_y = bot_center - band_top - therm_h // 2

        # What If sits HALFWAY between the arrows and the thermometer
        # (reported), which is what its two connectors point at. With the mode
        # switch moved into the ring it is the only thing in that gap, so it can
        # be centered in it rather than stacked against one end.
        block_h = what_h
        gap_top = arrow_y + arrow_h
        gap_bottom = therm_y
        block_y = gap_top + (gap_bottom - gap_top - block_h) // 2
        if block_y < arrow_y + arrow_h + BAND_SPACING:
            arrow_y = block_y - BAND_SPACING - arrow_h
        # The floor is not 0: it is below whatever the ring area has raised over
        # this column. Clamping to 0 put the top arrow under the corner
        # launcher, which then ate its clicks.
        arrow_y = max(self._band_top_limit(band, band_top), arrow_y)

        # ...and if that push down (or a stack too tall for the gap in the
        # first place) would now run the arrows into the mode buttons, the
        # furniture below gives way instead. The arrows are the control that
        # has to stay clickable; a shortened thermometer is only smaller.
        floor = arrow_y + arrow_h + BAND_SPACING
        if block_y < floor:
            block_y = floor
            therm_y = max(therm_y, block_y + block_h + BAND_SPACING)
            therm_h = max(0, min(therm_h, band.height() - therm_y))

        self.arrow_box.setGeometry(0, arrow_y, width, arrow_h)
        self.what_if_bar.setGeometry(0, block_y, width, what_h)
        self.thermometer.setGeometry(0, therm_y, width, therm_h)
        self._layout_connectors()

    def _layout_connectors(self) -> None:
        """Place and aim the two What If connectors.

        Called at the END of :meth:`_layout_left_band`, once the blocks it
        points at have their geometry -- the legs are measured FROM those
        widgets, so computing them any earlier would aim at the previous frame.
        """
        overlay = getattr(self, "connectors", None)
        if overlay is None:
            return
        band = self.left_band
        overlay.setGeometry(0, 0, band.width(), band.height())
        on = self.what_if_bar.button.isChecked()
        overlay.setVisible(on)
        if not on:
            overlay.set_legs([])
            return
        button = self.what_if_bar.button
        top = button.mapTo(band, button.rect().topLeft()).y()
        bottom = top + button.height()
        legs = []
        arrows = self.arrow_box
        if arrows.height() > 0:
            legs.append((top - BAND_SPACING,
                         arrows.geometry().bottom() + BAND_SPACING,
                         CONNECTOR_TO_ARROWS))
        thermo = self.thermometer
        if thermo.height() > 0:
            legs.append((bottom + BAND_SPACING,
                         thermo.geometry().top() - BAND_SPACING,
                         CONNECTOR_TO_THERMOMETER))
        overlay.set_legs(legs)
        overlay.raise_()

    def _schedule_band_layout(self) -> None:
        """Re-place the band once Qt has settled the ring's geometry. Deferred
        because the canvases' own sizes are what we measure against, and inside
        a resize they are still the previous frame's."""
        QTimer.singleShot(0, self._layout_left_band)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_left_band()
        self._schedule_band_layout()

    def changeEvent(self, event) -> None:
        """Re-theme the mode highlight when the app's look changes under us.

        A live light/dark switch re-applies the application stylesheet, and the
        mode buttons carry their own -- which would otherwise keep the old
        theme's accent and go unreadable against the new surfaces."""
        super().changeEvent(event)
        # getattr: Qt delivers StyleChange during construction, before the band
        # is built.
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            for btn in getattr(self, "mode_buttons", {}).values():
                restyle_toggle(btn)
            # The center block's accent and heading gray come from the active
            # palette, and nothing else would rebuild it on a theme switch --
            # it would keep one theme's amber on the other theme's background.
            center = getattr(self, "center", None)
            if center is not None:
                center.restyle()

    # -- state --------------------------------------------------------------
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Switch the ring between accounts and securities. The filter does not
        survive the switch -- an account id means nothing in securities mode."""
        if mode not in RING_MODES:
            raise ValueError(f"unknown ring mode {mode!r}; one of {RING_MODES}")
        if mode == self._mode:
            return
        self._mode = mode
        for m, btn in self.mode_buttons.items():
            btn.setChecked(m == mode)
        self._filter = None
        self.ring._selected = None
        self.refresh()
        self.filterChanged.emit(self._filter)

    def filter(self):
        """``("account", id)``, ``("security", symbol)``, or None for the whole
        portfolio."""
        return self._filter

    def filter_subject(self) -> str:
        """What the center line is currently about, as a human reads it."""
        if self._filter is None:
            return WHAT_IF_ALL_SUBJECT
        kind, key = self._filter
        if kind == "account":
            row = self.conn.execute("SELECT name FROM accounts WHERE id=?",
                                    (key,)).fetchone()
            return row["name"] if row else str(key)
        # The cash wedge's key is a sentinel, never a ticker; show its label.
        return CASH_LABEL if key == CASH_KEY else str(key)

    def _on_slice_clicked(self, key) -> None:
        if key is None:
            self._filter = None
        elif self._mode == MODE_ACCOUNTS:
            self._filter = ("account", int(key))
        else:
            self._filter = ("security", str(key))
        self._refresh_center()
        self._refresh_charts()
        self.filterChanged.emit(self._filter)

    def select_slice(self, key) -> None:
        """Programmatic equivalent of clicking the wedge for ``key``."""
        self.ring.pick(key)

    def clear_filter(self) -> None:
        """Back to the whole portfolio. Everything scoped -- the center line,
        both charts and the What If fan -- widens with it."""
        if self._filter is None:
            return
        self.ring.pick(None)              # emits sliceClicked -> _on_slice_clicked

    def _sync_filter_to_ring(self) -> bool:
        """Make ``self._filter`` a statement ABOUT THE RING, and say whether it
        changed.

        The two used to be set independently and could drift: a refresh whose
        new slice set no longer contains the selected key makes
        :meth:`RingCanvas.set_slices` drop ``ring._selected``, and a gear scope
        that excludes the selected account cleared only an ACCOUNT filter, never
        a security one. Either way the page went on scoping its charts -- and
        now its What If fan -- to something no wedge was showing as selected,
        which is a projection the user has no way to read. Deriving the filter
        from the ring after every refresh makes that state unreachable rather
        than merely unlikely."""
        selected = self.ring.selected()
        if selected is None:
            new = None
        elif self._mode == MODE_ACCOUNTS:
            new = ("account", int(selected))
        else:
            new = ("security", str(selected))
        changed = new != self._filter
        self._filter = new
        return changed

    # -- refresh ------------------------------------------------------------
    def refresh(self) -> None:
        self.ring.set_slices(ring_slices(self.conn, self._mode, self.as_of,
                                         account_ids=self.account_scope()))
        filter_changed = self._sync_filter_to_ring()
        # The ring's mask depends on whether it drew any wedges at all, so it is
        # recomputed whenever the slices change -- not just on resize.
        self.ring_area.relayout()
        self._refresh_arrows()
        self._refresh_center()
        self._refresh_charts()
        self._stale = False
        # The arrow block's height is however many arrows there are, so its
        # center line moves whenever they are rebuilt.
        self._layout_left_band()
        self._schedule_band_layout()
        if filter_changed:
            self.filterChanged.emit(self._filter)

    def _refresh_center(self) -> None:
        if self._filter is None:
            line = center_line(self.conn, self.as_of,
                               account_ids=self.account_scope())
        else:
            kind, key = self._filter
            if kind == "account":
                line = center_line(self.conn, self.as_of, account_ids=[int(key)],
                                   subject=self.filter_subject())
            else:
                line = center_line(self.conn, self.as_of,
                                   account_ids=self.account_scope(),
                                   symbol=str(key),
                                   subject=self.filter_subject())
        self.center.set_line(line)
        # The top plot's title says the same thing the center line does, so it
        # is refreshed from here rather than from the callers: every path that
        # changes the scope -- a wedge click, a gear scope, a mode switch, a
        # plain refresh -- already comes through this method, and a title left
        # behind would be naming a curve that is no longer drawn.
        self._refresh_titles()
        # The overlay is hand-placed, so a line that changed height has to be
        # re-reserved and re-placed; a layout would have done this itself.
        self._sync_center_gap()
        self.ring_area.relayout()

    def _refresh_titles(self) -> None:
        """Point the top plot's title at whatever the page is now scoped to."""
        self.history_header.set_title(performance_title(self.filter_subject()))

    def _refresh_arrows(self) -> None:
        while self.arrow_layout.count():
            item = self.arrow_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.arrows = inflow_arrows(self.conn, self.as_of,
                                    account_ids=self.account_scope())
        self._measured_inflows = {a.account_id: int(a.total) for a in self.arrows}
        self.arrow_widgets = []
        for arrow in self.arrows:
            w = InflowArrowWidget(arrow, self.ring.color_for(arrow.account_id),
                                  self.arrow_box)
            if arrow.account_id in self._what_if_inflows:
                w.set_amount(self._what_if_inflows[arrow.account_id])
            w.set_editable(self._arrow_editable(arrow.account_id))
            w.amountChanged.connect(
                lambda cents, key=arrow.account_id: self._on_arrow_amount(key, cents))
            self.arrow_layout.addWidget(w)
            self.arrow_widgets.append(w)
        self.arrow_layout.addStretch(1)

    # -- the two charts in the hole -----------------------------------------
    def _scope_ids(self) -> list:
        """The account ids the charts are about: the gear's scope, narrowed
        further by the ring filter when a wedge is selected."""
        base = self.account_scope()
        if base is None:
            base = _account_ids(self.conn)
        if self._filter is not None and self._filter[0] == "account":
            picked = int(self._filter[1])
            if picked in base:
                return [picked]
        return list(base)

    def _scope_symbol(self) -> Optional[str]:
        if self._filter is not None and self._filter[0] == "security":
            return str(self._filter[1])
        return None

    def _wedge_color(self) -> Optional[str]:
        """The selected wedge's color, so the value line reads as that wedge.
        ``None`` with no filter: the chart then uses the theme's own blue."""
        if self._filter is None:
            return None
        return self.ring.color_for(str(self._filter[1]))

    def _refresh_charts(self) -> None:
        self._sync_what_if_scope()
        # Selecting a wedge re-scopes What If live rather than cancelling it.
        # Editability no longer moves with the selection (see
        # :meth:`_arrow_editable`), but the arrow widgets may have been rebuilt
        # since, so they are re-synced from the one rule here.
        self._sync_arrow_editability()
        self._refresh_history()
        self._refresh_measured()
        self._refresh_projection()

    def _refresh_history(self) -> None:
        series = value_series(self.conn, self.history_chart.years(),
                              as_of=self.as_of, account_ids=self._scope_ids(),
                              symbol=self._scope_symbol())
        self.history_chart.set_series(series, self._wedge_color())

    def _refresh_measured(self) -> None:
        """Re-measure the mix for the CURRENT scope, and put the needle where
        reality is.

        The measurement itself always runs: it is what the baseline fan is
        drawn from, so selecting a wedge while What If is on has to re-measure
        or the baseline would still be the portfolio's while the What If fan is
        the account's -- two curves on the same axes answering different
        questions. Only the NEEDLE is left alone while What If is on: there the
        slider belongs to the user, and a refresh underneath must not snatch it
        back."""
        self._measured_risk = current_risk(self.conn, self._scope_ids(), self.as_of)
        if self._what_if:
            return
        self.thermometer.set_risk(self._measured_risk)

    def _scoped_inflows(self) -> dict:
        """The measured inflows that belong to the CURRENT selection.

        Reported: selecting a wedge for an account with no inflow arrow still
        grew its projection as if the whole portfolio's contributions landed in
        it. The yearly inflow is measured per ACCOUNT (design 3 -- the arrows
        show exactly which accounts have one), so a fan drawn for one account
        may only carry that account's own stream, and an account without an
        arrow must project ZERO contributions. The portfolio view (no filter)
        keeps the sum, because there every arrow is in scope.

        A SECURITY selection also projects zero. An inflow arrives in an
        account, not in one holding: charging an account's whole contribution to
        a single security would inflate that security's fan with money that
        mostly buys something else, and splitting it by the security's share of
        the account would invent a contribution policy the user never stated.
        Zero understates rather than invents."""
        if self._filter is None:
            return dict(self._measured_inflows)
        kind, key = self._filter
        if kind == "account" and int(key) in self._measured_inflows:
            return {int(key): self._measured_inflows[int(key)]}
        return {}

    def _measured_contribution(self) -> int:
        return sum(self._scoped_inflows().values())

    def _what_if_contribution(self) -> int:
        return sum(self._what_if_inflows.get(key, amount)
                   for key, amount in self._scoped_inflows().items())

    def _refresh_projection(self) -> None:
        """Both fans, always on the SAME scope.

        Starting value, measured contribution, measured mix and the What If
        contribution are each read through the current selection, so with a
        wedge picked the whole picture is that account's (or that security's).
        That is the point of scoped What If: against a portfolio total a
        realistic change to one account's inflow is invisible, and the user
        asked to see the change where it actually lands. The two fans must
        never be scoped differently -- a portfolio baseline under an account's
        What If reads as a catastrophe rather than a comparison -- which is why
        ``start`` is computed once here and ``_measured_risk`` is re-measured
        for the scope on every refresh."""
        start = _value_at(self.conn, self._scope_ids(), self._scope_symbol(),
                          self.as_of)
        years = self.projection_chart.years()
        baseline = projection_fan(start, self._measured_contribution(),
                                  self._measured_risk, years)
        what_if = None
        if self._what_if:
            what_if = projection_fan(start, self._what_if_contribution(),
                                     self.thermometer.risk(), years)
        self.projection_chart.set_fans(baseline, what_if)

    # -- What If -------------------------------------------------------------
    def _sync_what_if_scope(self) -> None:
        """Keep the bar's scope label on whatever the ring currently says the
        page is about. What If is never disabled and never cleared by a
        selection: the projection follows the selection instead, and this label
        is what stops a scoped fan from being read as the portfolio's."""
        self.what_if_bar.set_scope(self.filter_subject())

    def what_if_active(self) -> bool:
        return self._what_if

    def what_if_available(self) -> bool:
        """Always True. Kept as a method because the window and the tests ask;
        the disable path is gone."""
        return self.what_if_bar.is_available()

    def _arrow_editable(self, account_id: int) -> bool:
        """Every arrow that is DRAWN is editable while What If is on.

        Reported: "The inflow arrow edit is no longer working." This used to
        gate on :meth:`_scoped_inflows` as well, reasoning that an edit which
        could not move the fan on screen should not be taken. The reasoning was
        sound and the behavior was wrong: selecting any wedge silently turned
        every OTHER account's arrow read-only, and a security wedge turned all
        of them read-only at once -- arrows still painted, still sitting there,
        just inert, with nothing on screen saying why. The user reads that as a
        broken control, not as a scope rule.

        The rule now is the other way round: an arrow that is drawn is
        editable, and it is the PAGE's job to make the edit land somewhere
        visible (:meth:`_focus_inflow`). Arrows are only drawn for accounts the
        gear already kept, so "drawn" is the whole test."""
        return self._what_if

    def set_what_if(self, on: bool) -> None:
        """Programmatic equivalent of pressing the toggle."""
        self.what_if_bar.set_active(bool(on))

    def _on_what_if_toggled(self, on: bool) -> None:
        on = bool(on)
        self._what_if = on
        self._sync_arrow_editability()
        self.thermometer.set_editable(on)
        if not on:
            self._reset_what_if_values()
        self._refresh_projection()

    def _sync_arrow_editability(self) -> None:
        for w in self.arrow_widgets:
            w.set_editable(self._arrow_editable(w.account_id()))

    def reset_what_if(self) -> None:
        """Measured inflows and the measured mix, back the way they are."""
        self._reset_what_if_values()
        self._refresh_projection()

    def _reset_what_if_values(self) -> None:
        self._what_if_inflows = {}
        for w in self.arrow_widgets:
            w.set_amount(self._measured_inflows.get(w.account_id(), 0))
        self.thermometer.set_risk(self._measured_risk)

    def _focus_inflow(self, account_id: int) -> bool:
        """Re-scope the page so an edit to ``account_id``'s arrow is VISIBLE;
        returns whether the selection moved.

        The old gate refused edits outside the selection. Refusing was the
        defect, so the page moves instead: it selects that account's own wedge
        (its fan is then exactly the one the arrow feeds), or, when there is no
        such wedge to select -- securities mode, or an account the ring did not
        draw -- widens back to the whole portfolio, where every arrow counts.
        Either way the user sees the number they just typed do something.

        Safe to call from an arrow's ``editingFinished``: the selection path
        (:meth:`_on_slice_clicked`) refreshes the center and the charts but
        deliberately does NOT rebuild the arrow widgets, so the widget still
        mid-signal is not deleted underneath Qt."""
        account_id = int(account_id)
        if account_id in self._scoped_inflows():
            return False
        if self._mode == MODE_ACCOUNTS and str(account_id) in self.ring.keys():
            self.ring.pick(str(account_id))
            return True
        if self._filter is not None:
            self.clear_filter()
            return True
        return False

    def _on_arrow_amount(self, account_id: int, cents: int) -> None:
        self._what_if_inflows[int(account_id)] = int(cents)
        # The re-scope refreshes the projection on its way through; refreshing
        # here as well would just draw the same fans twice.
        if not self._focus_inflow(account_id):
            self._refresh_projection()

    def _on_risk_changed(self, _level: float) -> None:
        self._refresh_projection()

    def _on_history_period(self, _years) -> None:
        self._refresh_history()

    def _on_projection_horizon(self, _years: int) -> None:
        self._refresh_projection()

    # -- test/host seams ------------------------------------------------------
    def center_text(self) -> str:
        return self.center.text()

    def arrow_accounts(self) -> list:
        return [a.account_id for a in self.arrows]

    # -- the staleness contract (ui/projection_dialogs.py:581) ---------------
    def mark_stale(self) -> None:
        """A write somewhere else changed what this page is a picture of:
        recompute now if the page is on screen, otherwise the next time it is
        shown."""
        self._stale = True
        if self.isVisible():
            self.refresh_if_stale()

    def refresh_if_stale(self) -> bool:
        """Recompute iff a write invalidated the page since the last draw;
        returns whether it did."""
        if not self._stale:
            return False
        self.refresh()
        return True

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_if_stale()
        self._layout_left_band()
        self._schedule_band_layout()
