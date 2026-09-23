"""Tests for printing the custom-report drill-down (mammon/ui/report_print.py).

The fitting rules are pure arithmetic on characters, so they are asserted here
without a printer, a font or a window. The one rule everything else bends to:
**the numbers always show** -- an amount is why the page exists, and a clipped
one still reads as a number, which is worse than no page at all.

All data is synthetic.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon.ui.custom_report_window import DrillRow
from mammon.ui.report_print import (
    AMOUNT, DATE, HIERARCHY, MIN_FLEXIBLE, PAYEE, PrintSettings,
    drill_print_html, fit_columns, natural_widths, print_rows,
)


def _row(depth, label, date="", payee="", amount="1,000.00", **kw):
    return DrillRow(depth, kw.pop("kind", "txn"),
                    [label, date, payee, amount], 100000, **kw)


WIDE = [
    _row(0, "Schedule E:Rents received", kind="item", bold=True),
    _row(1, "7344 Muirfield", kind="tag"),
    _row(2, "Rental:Rent", kind="category"),
    _row(3, "", "04/06/2026", "Bart Domegan and Company — April rent",
         "1,550.00"),
]


def test_natural_widths_include_the_indent():
    """The indent is what makes a drill-down wide; measuring the labels alone
    would report a page that fits and then print one that does not."""
    label = "Rental:Rent and Utilities"      # long enough to clear the header
    shallower = natural_widths([_row(2, label)])[HIERARCHY]
    deeper = natural_widths([_row(3, label)])[HIERARCHY]
    assert shallower == len(label) + 4        # two spaces per level
    assert deeper == len(label) + 6
    assert natural_widths(WIDE)[PAYEE] == len(
        "Bart Domegan and Company — April rent")


def test_everything_fits_on_a_wide_page():
    widths = fit_columns(WIDE, PrintSettings(width_chars=200))
    assert widths == natural_widths(WIDE)


def test_the_amount_column_never_gives_way():
    """The rule the others bend to."""
    natural = natural_widths(WIDE)
    for width in (200, 60, 40, 25):
        widths = fit_columns(WIDE, PrintSettings(width_chars=width))
        assert widths[AMOUNT] == natural[AMOUNT], width


def test_the_date_column_never_gives_way():
    natural = natural_widths(WIDE)
    for width in (200, 60, 40, 25):
        widths = fit_columns(WIDE, PrintSettings(width_chars=width))
        assert widths[DATE] == natural[DATE], width


def test_the_chosen_column_is_cut_first():
    natural = natural_widths(WIDE)
    # Wide enough that ONE column giving way is enough; see the next test for
    # what happens when it is not.
    narrow = PrintSettings(width_chars=80, truncate="payee")
    widths = fit_columns(WIDE, narrow)
    assert widths[PAYEE] < natural[PAYEE]
    assert widths[HIERARCHY] == natural[HIERARCHY]

    other = PrintSettings(width_chars=80, truncate="hierarchy")
    widths = fit_columns(WIDE, other)
    assert widths[HIERARCHY] < natural[HIERARCHY]
    assert widths[PAYEE] == natural[PAYEE]


def test_the_other_column_gives_way_only_when_the_first_bottoms_out():
    widths = fit_columns(WIDE, PrintSettings(width_chars=30, truncate="payee"))
    assert widths[PAYEE] == MIN_FLEXIBLE
    assert widths[HIERARCHY] < natural_widths(WIDE)[HIERARCHY]
    assert widths[HIERARCHY] >= MIN_FLEXIBLE


def test_no_column_is_ever_zero_or_negative():
    for width in (200, 80, 40, 20, 1):
        widths = fit_columns(WIDE, PrintSettings(width_chars=width))
        assert all(w > 0 for w in widths), (width, widths)


def test_a_clipped_cell_ends_in_an_ellipsis_and_the_amount_does_not():
    rows = print_rows(WIDE, PrintSettings(width_chars=80, truncate="payee"))
    leaf = rows[-1][0]
    assert leaf[PAYEE].endswith("…")
    assert leaf[AMOUNT] == "1,550.00"          # whole, always


def test_the_html_carries_the_font_and_the_strike_through():
    struck = [_row(3, "", "04/06/2026", "Refunded", "10.00", excluded=True)]
    html = drill_print_html(struck, "R", PrintSettings(font_family="Courier New",
                                                       font_size=7.5))
    assert "Courier New" in html and "7.5pt" in html
    assert "<s>" in html                        # an excluded row is still struck
    assert "#ffffff" in html and "#000000" in html   # never dark-on-dark


def test_the_html_bolds_an_item_row_and_keeps_its_indent():
    html = drill_print_html(WIDE, "R", PrintSettings(width_chars=200))
    assert "<b>" in html
    assert "&nbsp;&nbsp;" in html               # the indent survives HTML


def test_an_empty_report_still_builds_a_page():
    html = drill_print_html([], "R", PrintSettings())
    assert "<table>" in html and "Amount" in html
