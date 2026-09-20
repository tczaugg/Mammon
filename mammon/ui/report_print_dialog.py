"""Print setup for the custom-report drill-down -- the Qt half of
:mod:`mammon.ui.report_print`.

Everything that DECIDES anything lives next door and is pure. This module only
collects four choices and shows what they do:

* **orientation** -- a drill-down is wide, so landscape is often the whole fix;
* **font and size** -- the other whole fix, and the one that trades legibility
  for width;
* **which column gives way** when neither is enough. That is the manual override,
  and it exists because auto-fit cannot know whether the user is reading the
  hierarchy or the payees. Amount and Date are not on the list: the numbers
  always show.
* **rules** -- hairlines between rows, off for a cleaner page.

The PREVIEW is the point of the window. It is the real fitted output --
``report_print.print_rows`` over the real rows -- not a mock-up, so a user can
see a payee about to be cut and pick the other column instead of discovering it
on paper.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFontComboBox, QFormLayout, QLabel, QPlainTextEdit, QVBoxLayout,
)

from mammon.ui import report_print
from mammon.ui.report_print import PrintSettings

#: What the truncation combo offers, as (label, key). Amount and Date are
#: deliberately absent -- see the module docstring.
TRUNCATE_CHOICES = (
    ("Payee / Memo", "payee"),
    ("Line item / Tag / Category", "hierarchy"),
)

#: How many rows the preview shows. Enough to reach a transaction leaf on a
#: normal report, short enough to repaint on every keystroke.
PREVIEW_ROWS = 24


class PrintSetupDialog(QDialog):
    """Collect :class:`~mammon.ui.report_print.PrintSettings` for one printout.

    ``rows`` are the window's VISIBLE drill rows -- the same ones that will
    print -- so the preview cannot disagree with the page."""

    def __init__(self, rows, settings=None, parent=None, printer=None):
        super().__init__(parent)
        self.setWindowTitle("Print Setup")
        self.rows = list(rows or ())
        self.printer = printer
        base = settings or PrintSettings()
        self.resize(720, 560)

        form = QFormLayout()
        self.orientation = QComboBox()
        self.orientation.addItem("Portrait", False)
        self.orientation.addItem("Landscape", True)
        self.orientation.setCurrentIndex(1 if base.landscape else 0)

        self.font_box = QFontComboBox()
        self.font_box.setCurrentFont(QFont(base.font_family))
        self.size_box = QDoubleSpinBox()
        self.size_box.setRange(5.0, 18.0)
        self.size_box.setSingleStep(0.5)
        self.size_box.setDecimals(1)
        self.size_box.setValue(float(base.font_size))

        self.truncate = QComboBox()
        for label, key in TRUNCATE_CHOICES:
            self.truncate.addItem(label, key)
        idx = self.truncate.findData(base.truncate)
        self.truncate.setCurrentIndex(max(idx, 0))

        self.grid = QCheckBox("Rule between rows")
        self.grid.setChecked(bool(base.show_grid))

        form.addRow("Orientation:", self.orientation)
        form.addRow("Font:", self.font_box)
        form.addRow("Size:", self.size_box)
        form.addRow("Truncate first:", self.truncate)
        form.addRow("", self.grid)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        self.fit_label = QLabel("")
        self.fit_label.setWordWrap(True)
        lay.addWidget(self.fit_label)
        lay.addWidget(QLabel("Preview:"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        # Monospace on purpose: the fit is computed in CHARACTERS, so a
        # proportional preview would show a different page than it measured.
        self.preview.setFont(QFont("Consolas", 8))
        self.preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.preview, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Print…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        for widget, signal in ((self.orientation, "currentIndexChanged"),
                               (self.font_box, "currentFontChanged"),
                               (self.size_box, "valueChanged"),
                               (self.truncate, "currentIndexChanged"),
                               (self.grid, "toggled")):
            getattr(widget, signal).connect(self.refresh_preview)
        self.refresh_preview()

    # -- the choices ---------------------------------------------------------
    def settings(self) -> PrintSettings:
        """What is on screen, with the page width measured for it."""
        chosen = PrintSettings(
            landscape=bool(self.orientation.currentData()),
            font_family=self.font_box.currentFont().family(),
            font_size=float(self.size_box.value()),
            truncate=str(self.truncate.currentData()),
            show_grid=bool(self.grid.isChecked()),
        )
        return chosen.with_width(self.measure_width(chosen))

    def measure_width(self, settings: PrintSettings) -> int:
        """The page budget in characters. Overridable, and measured against a
        REAL printer when one was handed in -- the default page is only a
        stand-in for a setup opened with no printer to ask."""
        printer = self.printer
        if printer is None:
            from PyQt5.QtPrintSupport import QPrinter
            printer = QPrinter(QPrinter.HighResolution)
        from PyQt5.QtPrintSupport import QPrinter as _QPrinter
        printer.setOrientation(_QPrinter.Landscape if settings.landscape
                               else _QPrinter.Portrait)
        return report_print.page_width_chars(settings, printer)

    # -- the preview ---------------------------------------------------------
    def refresh_preview(self) -> None:
        settings = self.settings()
        widths = report_print.fit_columns(self.rows, settings)
        natural = report_print.natural_widths(self.rows)
        cut = [report_print.COLUMNS[i] for i in range(len(widths))
               if widths[i] < natural[i]]
        self.fit_label.setText(
            "%d characters across the page. Columns: %s.%s"
            % (settings.width_chars,
               ", ".join("%s %d" % (report_print.COLUMNS[i], widths[i])
                         for i in range(len(widths))),
               ("  Truncated: " + ", ".join(cut)) if cut else
               "  Everything fits."))
        self.preview.setPlainText(self.preview_text())

    def preview_text(self) -> str:
        """The fitted rows as monospaced text -- the real output, clipped."""
        settings = self.settings()
        widths = report_print.fit_columns(self.rows, settings)
        lines = []
        header = [report_print.COLUMNS[i][:widths[i]] for i in range(4)]
        lines.append(_lay_out(header, widths))
        lines.append(_lay_out(["-" * w for w in widths], widths))
        for cells, _bold, excluded in report_print.print_rows(
                self.rows, settings)[:PREVIEW_ROWS]:
            text = _lay_out(cells, widths)
            lines.append(text + ("   (excluded)" if excluded else ""))
        if len(self.rows) > PREVIEW_ROWS:
            lines.append("... and %d more rows" % (len(self.rows) - PREVIEW_ROWS))
        return "\n".join(lines)


def _lay_out(cells, widths) -> str:
    """One monospaced line: every column padded to its fitted width, the money
    column right-aligned the way it prints."""
    out = []
    for i, text in enumerate(cells):
        width = widths[i]
        if i == report_print.AMOUNT:
            out.append(str(text).rjust(width))
        else:
            out.append(str(text).ljust(width))
    return (" " * report_print.GUTTER).join(out).rstrip()
