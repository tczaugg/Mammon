"""Printing for Mammon: build a classic printable register (and spending
report) as HTML, and render that HTML to a printer or a PDF file.

The HTML builders are pure and unit-testable without a running GUI; only the
``render_*`` helpers touch Qt (QTextDocument + QPrinter), and they work
headless (an offscreen QApplication is enough to emit a PDF), so the whole
feature is exercised in tests without a physical printer or a modal dialog.
"""
from __future__ import annotations

import html as _html

from mammon.ui.models import fmt_cents, fmt_date, fmt_money

# Register columns in Quicken order (Clr sits between Payment and Deposit).
_REGISTER_HEADERS = ["Date", "Num", "Payee", "Category", "Memo",
                     "Payment", "Clr", "Deposit", "Balance"]
_MONEY_COLS = {"Payment", "Deposit", "Balance"}


def _esc(value) -> str:
    return _html.escape("" if value is None else str(value))


def _clr_glyph(row) -> str:
    if row.get("reconciled"):
        return "R"
    if row.get("cleared"):
        return "c"
    return ""


def register_html(account_name: str, rows, ending_balance: int,
                  subtitle: str | None = None) -> str:
    """A printable HTML document for one account's register.

    ``rows`` are the dicts returned by ``ledger.register_rows`` (date, num,
    payee, category_label, tag, memo, amount cents, cleared, reconciled,
    balance cents). Amounts fan into Payment/Deposit exactly as the on-screen
    register shows them, money is right-aligned, and a bold Ending Balance line
    closes the page -- so a printout mirrors the ledger."""
    head = ["<tr>"]
    for h in _REGISTER_HEADERS:
        align = "right" if h in _MONEY_COLS else "left"
        head.append(f'<th align="{align}">{_esc(h)}</th>')
    head.append("</tr>")

    body = []
    for r in rows:
        amt = int(r["amount"])
        cells = {
            "Date": fmt_date(r["date"]),
            "Num": _esc(r.get("num")),
            "Payee": _esc(r.get("payee")),
            "Category": _esc(r.get("category_label")),
            "Memo": _esc(r.get("memo")),
            "Payment": fmt_cents(-amt) if amt < 0 else "",
            "Clr": _clr_glyph(r),
            "Deposit": fmt_cents(amt) if amt > 0 else "",
            "Balance": fmt_cents(r["balance"]),
        }
        body.append("<tr>")
        for h in _REGISTER_HEADERS:
            align = "right" if h in _MONEY_COLS else \
                ("center" if h == "Clr" else "left")
            body.append(f'<td align="{align}">{cells[h]}</td>')
        body.append("</tr>")

    sub = f"<div class='sub'>{_esc(subtitle)}</div>" if subtitle else ""
    return f"""<html><head><style>
      /* A printout is always a white page with black text, regardless of the
         app's active theme -- a register printed from dark mode must be legible
         on paper, not dark-on-dark. */
      body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 9pt;
              background: #ffffff; color: #000000; }}
      h2 {{ margin: 0 0 2px 0; }}
      .sub {{ color: #555; margin-bottom: 8px; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid #ccc; padding: 2px 6px; }}
      th {{ border-bottom: 1px solid #333; }}
      .total {{ margin-top: 10px; font-weight: bold; text-align: right; }}
    </style></head><body>
      <h2>{_esc(account_name)}</h2>{sub}
      <table>{''.join(head)}{''.join(body)}</table>
      <div class="total">Ending Balance: {fmt_money(ending_balance)}</div>
    </body></html>"""


def render_html_to_pdf(html_str: str, pdf_path: str,
                       title: str = "Mammon") -> str:
    """Render an HTML document to a PDF file. Returns the path. Requires a
    QApplication to exist (offscreen is fine); used by 'Print to PDF' and by
    the tests, so the print path is verified without a physical printer."""
    from PyQt5.QtGui import QTextDocument
    from PyQt5.QtPrintSupport import QPrinter

    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(pdf_path)
    printer.setDocName(title)
    doc = QTextDocument()
    doc.setHtml(html_str)
    doc.print_(printer)
    return pdf_path


def render_html_to_printer(html_str: str, printer) -> None:
    """Render an HTML document to an already-configured QPrinter (the one a
    QPrintDialog hands back). Kept tiny so the dialog wiring stays in the UI."""
    from PyQt5.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setHtml(html_str)
    doc.print_(printer)
