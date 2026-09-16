"""Regressions for two reported chart defects (SRD 5.8d chart-readability).

Defect 7 -- the category pies (Spending by Category / Income by Category, and
the shared Asset Allocation pie) drew from a 10-colour palette that wrapped with
``i % len``, so an 11th division reused -- and became indistinguishable from --
the ``Other`` wedge's colour. The fix (``ui.charts.wedge_colors`` +
``_PIE_PALETTE``) carries enough distinct colours for the worst realistic pie
(~19 wedges: every real category ~5% with an ``Other`` >=10%) and PINS ``Other``
to the palette's stable final colour regardless of the division count. Every
wedge stays identifiable: slivers too small for an inline label get a hover
tooltip naming the category, its share of the whole and its dollar amount.

Defect 8 -- the Net Worth Over Time line chart had barely-visible horizontal
grid lines and NO vertical ones. The fix enables BOTH axes at a crisp
weight/opacity (matching the app's financial calendar table grid), with the
vertical lines aligned to the x-axis ticks.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from matplotlib.colors import to_rgba

from mammon.reports.charts import NetWorthPoint, NetWorthSeries
from mammon.ui import charts


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# --- Defect 7: palette breadth and a stable 'Other' colour -------------------
def test_pie_palette_has_at_least_19_distinct_colours():
    """The worst realistic pie has ~19 divisions (18 real categories near 5%
    apiece plus an ``Other`` >=10%); the palette must supply that many distinct
    colours so no two wedges share one."""
    assert len(set(charts._PIE_PALETTE)) >= 19


def test_other_is_pinned_to_the_last_palette_slot_at_every_count():
    """``Other`` always takes the palette's FINAL entry, whatever its position in
    the label list and however many real categories precede it -- so it is a
    stable, recognizable colour rather than wherever the slice order landed."""
    other_colour = charts._PIE_PALETTE[-1]

    # Position-independent: 'Other' need not be last in the label list.
    assert charts.wedge_colors(["A", "Other", "B"])[1] == other_colour

    # Count-independent: from 1 up through the worst case, 'Other' keeps the last
    # slot and never collides with any real category's colour.
    for n_real in range(1, 19):
        labels = [f"Cat{i}" for i in range(n_real)] + ["Other"]
        colours = charts.wedge_colors(labels)
        assert colours[-1] == other_colour
        real_colours = colours[:-1]
        assert other_colour not in real_colours, f"'Other' collided at n={n_real}"


def test_worst_case_19_divisions_are_all_distinct():
    """18 real categories + ``Other`` -> 19 wedges, every colour different."""
    labels = [f"Cat{i}" for i in range(18)] + ["Other"]
    colours = charts.wedge_colors(labels)
    assert len(colours) == 19
    assert len(set(colours)) == 19
    assert colours[-1] == charts._PIE_PALETTE[-1]


def _rolled_up_rows():
    """Rows that force ``group_small_slices`` to produce a real ``Other`` wedge:
    one large slice, three mid slices, and a tail of tiny ones that roll up past
    the 10% target."""
    return ([("Big", 60000)]
            + [(f"Mid{i}", 1000) for i in range(3)]
            + [(f"Tiny{i}", 1000) for i in range(7)])


def test_slices_pie_paints_other_wedge_the_final_palette_colour(qapp):
    canvas = charts.SlicesPieCanvas("Spending by Category", _rolled_up_rows())
    assert canvas.has_group()          # the tail actually rolled into 'Other'
    by_label = dict(canvas._wedges)
    assert "Other" in by_label
    assert by_label["Other"].get_facecolor() == to_rgba(charts._PIE_PALETTE[-1])
    # No real wedge shares the 'Other' colour.
    for label, wedge in canvas._wedges:
        if label != "Other":
            assert wedge.get_facecolor() != to_rgba(charts._PIE_PALETTE[-1])


def test_every_wedge_has_a_tooltip_even_the_unlabelled_slivers(qapp):
    canvas = charts.SlicesPieCanvas("Spending by Category", _rolled_up_rows())
    drawn = [lab for lab, _ in canvas.drawn_slices()]
    # A tooltip for every wedge, including the ones too small to carry a label.
    assert set(canvas._tooltips) == set(drawn)
    hidden = [lab for lab in drawn if lab not in set(canvas.visible_labels())]
    assert hidden, "expected some slivers with suppressed inline labels"
    for lab in hidden:
        text = canvas._tooltips[lab]
        assert lab in text          # names the category
        assert "%" in text          # its share of the whole
        assert "$" in text          # its dollar amount


# --- Defect 8: Net Worth grid on both axes -----------------------------------
def _net_worth_series():
    return NetWorthSeries(
        start="2026-01-01", end="2026-06-30",
        points=[NetWorthPoint("2026-01-31", 1000_00),
                NetWorthPoint("2026-02-28", 1200_00),
                NetWorthPoint("2026-03-31", 900_00),
                NetWorthPoint("2026-04-30", 1500_00),
                NetWorthPoint("2026-05-31", 1700_00),
                NetWorthPoint("2026-06-30", 2100_00)])


def test_net_worth_enables_both_axis_gridlines(qapp):
    """Horizontal AND vertical grid lines are drawn (defect 8 added the vertical
    ones), and both are visible rather than off."""
    canvas = charts.NetWorthCanvas(_net_worth_series())
    ax = canvas.figure.axes[0]

    xgl = ax.get_xgridlines()
    ygl = ax.get_ygridlines()
    assert xgl and all(g.get_visible() for g in xgl), "no vertical grid lines"
    assert ygl and all(g.get_visible() for g in ygl), "no horizontal grid lines"


def test_net_worth_gridlines_use_the_stronger_calendar_weight(qapp):
    """The lines are drawn at the crisp weight/opacity constants (stronger than
    matplotlib's washed-out 0.25 default), matching the financial calendar."""
    canvas = charts.NetWorthCanvas(_net_worth_series())
    ax = canvas.figure.axes[0]
    for g in ax.get_xgridlines() + ax.get_ygridlines():
        assert g.get_alpha() == charts._GRID_ALPHA
        assert g.get_linewidth() == charts._GRID_LINEWIDTH
    assert charts._GRID_ALPHA > 0.25   # stronger than the old default
