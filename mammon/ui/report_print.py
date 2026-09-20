"""Printing the custom-report drill-down: fitting it to a page, and the setup
the user gets to control.

`mammon.ui.printing` already renders HTML to a printer or a PDF, and this module
does not duplicate any of that -- it builds the HTML for ONE report and answers
the question that report raises and the flat ones do not: **a drill-down is as
wide as its deepest open branch**, so it does not fit a page by default and
cannot be made to fit by shrinking type alone.

Four rules decide the layout, and the first is the one everything else bends to.

**The numbers always show.** An amount is why the page exists; a truncated
figure is worse than no page, because `1,234.5` reads as a number. The Amount
column is measured from its content and never gives way, and Date is treated the
same -- it is narrow, fixed-width, and a half-printed date is not a date.

**What gives way is the user's choice.** Only two columns can absorb a shortfall:
the hierarchy column (whose width is the deepest open branch plus its label) and
Payee / Memo. Auto-fit spends whatever is left on them in proportion to what they
actually need, and when that is not enough the column named in
:attr:`PrintSettings.truncate` is cut first, to an ellipsis. Cutting the one the
user did not pick would silently destroy the thing he was printing the report to
read.

**The page prints the tree AS IT STANDS.** Expansion state is the user's
statement of what he wants on paper -- he has already opened the three lines he
is checking and closed the sixty he is not -- so the window hands over exactly
its visible rows and this module adds nothing and drops nothing. There is no
"print all levels" option because there is already a control for that, and it is
the tree.

**Fitting is arithmetic on CHARACTERS, not on pixels.** The caller measures the
page once through Qt (`QFontMetrics` over the chosen font, against the printer's
usable width) and passes the budget in; everything here is pure, so the fitting
rules are testable without a printer, a font, or a window.
"""
from __future__ import annotations

import html as _html
from dataclasses import dataclass, replace

#: The columns, in order. Index 0 is the hierarchy, the last is the money.
COLUMNS = ("Line item / Tag / Category", "Date", "Payee / Memo", "Amount")
HIERARCHY, DATE, PAYEE, AMOUNT = range(4)

#: The two columns that may be truncated, by the key the setup dialog stores.
TRUNCATABLE = {"hierarchy": HIERARCHY, "payee": PAYEE}

#: Never cut a flexible column below this: a column narrower than this says
#: nothing at all, and the user is better served by cutting the other one.
MIN_FLEXIBLE = 8

#: Spaces between columns when the fit is computed. HTML padding does the real
#: spacing; this keeps the budget honest about it.
GUTTER = 2

ELLIPSIS = "…"


@dataclass(frozen=True)
class PrintSettings:
    """What the user chose in print setup.

    ``width_chars`` is the page's usable width measured in characters of the
    chosen font -- the one number that has to come from Qt, computed once by
    :func:`page_width_chars` so everything below it stays pure."""

    landscape: bool = False
    font_family: str = "Segoe UI"
    font_size: float = 9.0
    truncate: str = "payee"
    width_chars: int = 110
    show_grid: bool = True

    def with_width(self, width_chars: int) -> "PrintSettings":
        return replace(self, width_chars=int(width_chars))


def page_width_chars(settings: PrintSettings, printer) -> int:
    """How many characters of ``settings``' font fit across ``printer``'s page.

    The only Qt in the fitting path, and it is a measurement rather than a
    decision. ``averageCharWidth`` is the right measure here precisely because it
    is an average: the text is proportional, so an exact fit is not knowable
    without laying it out, and the alternative -- the widest glyph -- would leave
    a third of the page empty on every ordinary report."""
    from PyQt5.QtGui import QFont, QFontMetricsF

    font = QFont(settings.font_family)
    font.setPointSizeF(float(settings.font_size))
    # Metrics measured against the PRINTER, and the page taken in the printer's
    # own device units, so both are in one coordinate system. Measuring the font
    # on its own gives SCREEN pixels while ``pageRect(Point)`` gives points, and
    # mixing the two reported a US Letter page as 45 characters wide -- every
    # report truncated to a third of the paper, which is exactly the failure
    # this function exists to prevent.
    metrics = QFontMetricsF(font, printer)
    char = metrics.averageCharWidth() or 1.0
    return max(20, int(printer.pageRect().width() / char))


def natural_widths(rows) -> list:
    """The width each column would take uncut, in characters.

    The hierarchy column is measured WITH its indent, because the indent is what
    makes a drill-down wide -- measuring the labels alone would report a page
    that fits and then print one that does not."""
    widths = [len(c) for c in COLUMNS]
    for row in rows:
        cells = _row_cells(row)
        for i, text in enumerate(cells):
            widths[i] = max(widths[i], len(text))
    return widths


def fit_columns(rows, settings: PrintSettings) -> list:
    """The printed width of each column, in characters.

    Amount and Date keep their natural width always. What remains is offered to
    the hierarchy and Payee columns; if they both fit, they get what they need
    and the page is simply narrower than the paper. If they do not, the column
    named by ``settings.truncate`` is cut first -- down to :data:`MIN_FLEXIBLE`
    at the hardest -- and only if that is still not enough does the other one
    give way. Nothing ever returns a negative or zero width."""
    natural = natural_widths(rows)
    fixed = natural[DATE] + natural[AMOUNT] + GUTTER * (len(COLUMNS) - 1)
    budget = max(MIN_FLEXIBLE * 2, int(settings.width_chars) - fixed)
    want = natural[HIERARCHY] + natural[PAYEE]
    if want <= budget:
        return list(natural)

    first = TRUNCATABLE.get(settings.truncate, PAYEE)
    second = PAYEE if first == HIERARCHY else HIERARCHY
    out = list(natural)
    over = want - budget
    # Cut the chosen column first, no further than MIN_FLEXIBLE.
    give = min(over, max(0, out[first] - MIN_FLEXIBLE))
    out[first] -= give
    over -= give
    if over > 0:
        out[second] = max(MIN_FLEXIBLE, out[second] - over)
    return out


def _row_cells(row) -> list:
    """One row's four display strings, the hierarchy column carrying its indent.

    Indent is spaces rather than markup so the width arithmetic and the printed
    page agree by construction -- a CSS margin would be invisible to
    :func:`natural_widths` and then appear on paper."""
    cells = list(row.cells)
    cells[HIERARCHY] = ("  " * int(row.depth)) + cells[HIERARCHY]
    return cells


def _clip(text: str, width: int) -> str:
    """``text`` cut to ``width`` characters, ending in an ellipsis when cut."""
    if width <= 0 or len(text) <= width:
        return text
    if width <= 1:
        return ELLIPSIS
    return text[:width - 1].rstrip() + ELLIPSIS


def print_rows(rows, settings: PrintSettings) -> list:
    """The rows as they will PRINT: ``(cells, bold, excluded)`` with every cell
    already clipped to its fitted width.

    Separate from the HTML so a test can assert the fitting without parsing
    markup, and so the setup dialog can show a true preview rather than a
    guess."""
    widths = fit_columns(rows, settings)
    out = []
    for row in rows:
        cells = [_clip(text, widths[i])
                 for i, text in enumerate(_row_cells(row))]
        out.append((cells, bool(row.bold), bool(getattr(row, "excluded", False))))
    return out


def drill_print_html(rows, title: str, settings: PrintSettings,
                     subtitle: str = "") -> str:
    """The drill-down as a printable HTML document.

    White page, black text, whatever the app's theme -- the same rule the other
    report exports follow, because a report saved from dark mode must not print
    dark-on-dark. An excluded row keeps its strike-through, since that is the
    whole of what distinguishes it from a line that counted."""
    widths = fit_columns(rows, settings)
    total = sum(widths) or 1
    head = []
    for i, name in enumerate(COLUMNS):
        align = "right" if i == AMOUNT else "left"
        pct = 100.0 * widths[i] / total
        head.append(f'<th align="{align}" width="{pct:.1f}%">'
                    f'{_esc(_clip(name, widths[i]))}</th>')
    body = []
    for cells, bold, excluded in print_rows(rows, settings):
        tds = []
        for i, text in enumerate(cells):
            align = "right" if i == AMOUNT else "left"
            # Leading indent must survive HTML's whitespace collapsing, or every
            # row prints flush left and the hierarchy is gone.
            shown = _esc(text).replace("  ", "&nbsp;&nbsp;")
            if bold:
                shown = f"<b>{shown}</b>"
            if excluded:
                shown = f"<s>{shown}</s>"
            tds.append(f'<td align="{align}">{shown}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")

    grid = ("th, td { border-bottom: 1px solid #ccc; padding: 1px 4px; }"
            if settings.show_grid else
            "th, td { padding: 1px 4px; }")
    sub = f"<div class='sub'>{_esc(subtitle)}</div>" if subtitle else ""
    return f"""<html><head><meta charset="utf-8"><title>{_esc(title)}</title><style>
      body {{ font-family: '{_esc(settings.font_family)}', Arial, sans-serif;
              font-size: {float(settings.font_size):.1f}pt;
              background: #ffffff; color: #000000; }}
      h2 {{ margin: 0 0 2px 0; font-size: {float(settings.font_size) + 3:.1f}pt; }}
      .sub {{ margin: 0 0 8px 0; color: #333333; }}
      table {{ border-collapse: collapse; width: 100%; }}
      {grid}
      th {{ border-bottom: 1px solid #333; text-align: left; }}
    </style></head><body>
      <h2>{_esc(title)}</h2>{sub}
      <table><tr>{''.join(head)}</tr>{''.join(body)}</table>
    </body></html>"""


def _esc(value) -> str:
    return _html.escape("" if value is None else str(value))


# ---------------------------------------------------------------------------
# Print setup
# ---------------------------------------------------------------------------

class PrintSetupDialog:
    """Deferred import shim -- see :mod:`mammon.ui.report_print_dialog`.

    The pure fitting rules above must stay importable without Qt, which is what
    lets the tests exercise them headless and at speed; the dialog lives next
    door and is imported only when someone opens it."""

    def __new__(cls, *args, **kwargs):
        from mammon.ui.report_print_dialog import PrintSetupDialog as Real
        return Real(*args, **kwargs)
