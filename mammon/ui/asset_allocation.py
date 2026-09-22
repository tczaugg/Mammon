"""The Asset Allocation report: what each account is made of (SRD 5.8g).

This replaces the pie-first view for the INVESTMENT side of the portfolio, and
it exists because the old one could not answer the question people actually
bring to it. A pie shows one grouping at a time and cannot compare two things,
so "is my 401(k) more aggressive than my taxable account?" -- the question that
decides what you buy next -- had no answer on screen. Parallel stacked bars over
a shared class-to-color scale do answer it, and the same colors carry down into
each account's securities, so a fund's contribution to its account is legible in
place rather than by cross-referencing a legend.

What it shows and what it deliberately does not
-----------------------------------------------
INVESTMENTS ONLY. Property, vehicles and other owned assets are out of scope
(reported): the dashboard's ring already excludes them, ``forecast`` has no
(mu, sigma) for them, so they can never join a projection, and their presence
here bought nothing but a wedge. ``portfolio.allocation`` still scopes wider for
the older window, which keeps that view for anyone who wants it.

TRIANGLES, NOT SILENCE. A holding with no mixture and no class lands in
``unclassified``, which the old view reported as one more wedge -- leaving the
user to work out which holding poisoned it. Every row that is itself undefined
is marked, and so is every row a rollup ABOVE one: an account's bar and the
portfolio total both carry the mark when anything under them is unallocated. The
rule is the one this codebase already follows for excluded options and position
discrepancies: state what is missing rather than quietly averaging it in.

Where the numbers come from
---------------------------
Nothing is computed here. ``portfolio.allocation`` does the splitting (per
security by its mixture, per account by its own), ``security_mix`` owns the
mixtures, and this module arranges what they return. That is the same rule the
Investment Dashboard follows, and it is why a change to how a mixture splits
shows up in the dashboard's ring, the rebalancer and this report at once.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PyQt5.QtCore import Qt, QRectF, pyqtSignal
from PyQt5.QtGui import (QBrush, QColor, QFont, QFontMetrics, QIcon, QPainter,
                         QPen, QPixmap, QPolygonF)
from PyQt5.QtCore import QPointF
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mammon import investments, ledger, portfolio, security_mix
from mammon.ui import charts

#: The class every unallocated dollar lands in. Not an asset class -- the
#: ABSENCE of one, which is why it is marked rather than colored like the rest.
UNCLASSIFIED = "unclassified"

#: Columns of the tree.
COL_NAME, COL_VALUE, COL_PCT, COL_MIX, COL_BAR = range(5)
HEADERS = ["Account / security", "Value", "%", "Mix", "Composition"]

#: Bar geometry. The bar is a cell widget, so it is sized by the column.
BAR_HEIGHT = 16
BAR_RADIUS = 3
#: A segment thinner than this gets no separator line -- at one pixel the line
#: IS the segment, and a row of hairlines reads as a striped bar rather than as
#: an allocation.
BAR_MIN_SEPARATOR = 3
#: A segment narrower than its own label plus this padding is left BLANK rather
#: than labelled: a percentage clipped to "4" or spilling over its neighbour is
#: worse than none, and the tooltip carries every figure anyway.
BAR_LABEL_PADDING = 8

#: The warning mark. A filled triangle, drawn rather than an emoji or an image:
#: it has to take the theme's color and scale with the row's font.
TRIANGLE_SIZE = 12

#: The legend's swatch, the gap to its label, the gap to the next entry, and
#: the height of one wrapped row.
LEGEND_SWATCH = 11
LEGEND_TEXT_GAP = 5
LEGEND_ITEM_GAP = 18
LEGEND_ROW_HEIGHT = 20


def class_colors(pal=None) -> dict:
    """``{asset_class: '#rrggbb'}`` -- ONE definition, so a class is the same
    color in every bar, in every account, on every row.

    Drawn from ``charts.wedge_colors`` over ``ASSET_CLASSES`` in their declared
    order, which is what makes it stable: the color of "bond" does not move when
    an account stops holding real estate. ``unclassified`` is deliberately NOT
    in the palette: it is drawn in the theme's muted gray, because giving the
    absence of an answer a category color makes it look like an answer.
    """
    labels = list(portfolio.ASSET_CLASSES)
    # wedge_colors returns a LIST parallel to its labels, not a mapping.
    return dict(zip(labels, charts.wedge_colors(labels, palette=pal)))


def _on_segment_text(background: str) -> str:
    """Black or white over a segment, by that segment's own lightness.

    The class palette runs from a pale gold to a mid blue, so one fixed ink
    would be unreadable on half of it. Relative luminance with the usual 0.5
    threshold, which is the same call :func:`_on_text_for` makes for the
    dashboard's toggle buttons.
    """
    value = background.lstrip("#")
    if len(value) != 6:
        return "#000000"
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if luminance > 0.55 else "#ffffff"


def unclassified_color(pal=None) -> str:
    """The muted gray an unallocated slice is drawn in."""
    palette = charts._active_palette() if pal is None else pal
    return palette["muted"]


def warning_color(pal=None) -> str:
    """Amber, from the palette added for the dashboard's center block, so the
    'you have not finished' mark is the same color as the page's other
    attention-seeking figure rather than a second invented one."""
    palette = charts._active_palette() if pal is None else pal
    return palette["highlight"]


# ---------------------------------------------------------------------------
# the pieces of the picture
# ---------------------------------------------------------------------------
class WarningTriangle(QWidget):
    """An amber triangle: this row, or something under it, is unallocated."""

    def __init__(self, tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setFixedSize(TRIANGLE_SIZE + 4, TRIANGLE_SIZE + 4)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        if tooltip:
            self.setToolTip(tooltip)

    def paintEvent(self, event):        # noqa: N802 (Qt's name)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        _paint_triangle(p, min(self.width(), self.height()))
        p.end()


def warning_pixmap(size: int = TRIANGLE_SIZE + 4):
    """The triangle as a pixmap, for use as a row icon.

    Painted rather than loaded: it takes the theme's amber, so it cannot go
    stale against a light/dark switch the way an image file would.
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    _paint_triangle(p, size)
    p.end()
    return pix


def _paint_triangle(p, size: int) -> None:
    """The mark itself, shared by the widget and the pixmap so the two cannot
    drift apart."""
    w = h = size - 4
    x = y = 2.0
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(warning_color())))
    p.drawPolygon(QPolygonF([QPointF(x + w / 2.0, y),
                             QPointF(x + w, y + h),
                             QPointF(x, y + h)]))
    pal = charts._active_palette()
    p.setPen(QPen(QColor(pal["window"]), 1.6))
    p.drawLine(QPointF(x + w / 2.0, y + h * 0.42),
               QPointF(x + w / 2.0, y + h * 0.70))
    p.drawPoint(QPointF(x + w / 2.0, y + h * 0.85))


class ClassBar(QWidget):
    """One horizontal stacked bar: a scope's value split across asset classes.

    Segments are ordered by ``ASSET_CLASSES``, never by size, so two accounts'
    bars can be read against each other -- bonds are in the same place in both.
    That is the entire reason this is a bar and not a pie.
    """

    def __init__(self, weights: Optional[dict] = None, parent=None, *,
                 show_labels: bool = False, height: int = BAR_HEIGHT):
        super().__init__(parent)
        self._weights: dict = dict(weights or {})
        #: Write each share INSIDE its segment where it fits (reported: "with
        #: the percentages in the bars or via tooltip for small bars"). Off by
        #: default: the tree's per-row bars are too short for type, and the
        #: tooltip already says everything.
        self._show_labels = bool(show_labels)
        self._height = int(height)
        self.setMinimumHeight(self._height + 4)
        self.setToolTip(self.describe())

    def set_weights(self, weights: dict) -> None:
        self._weights = dict(weights or {})
        self.setToolTip(self.describe())
        self.update()

    def weights(self) -> dict:
        return dict(self._weights)

    def order(self) -> list:
        """The classes present, in declared order, with ``unclassified`` last --
        it is the remainder, and putting it anywhere else implies it is a class."""
        present = [c for c in portfolio.ASSET_CLASSES if self._weights.get(c)]
        if self._weights.get(UNCLASSIFIED):
            present.append(UNCLASSIFIED)
        return present

    def describe(self) -> str:
        total = sum(self._weights.values())
        if total <= 0:
            return ""
        parts = []
        for cls in self.order():
            share = 100.0 * float(self._weights[cls]) / float(total)
            label = (portfolio.ASSET_CLASS_LABELS.get(cls, cls)
                     if cls != UNCLASSIFIED else "Unallocated")
            parts.append(f"{label} {share:.1f}%")
        return "   ".join(parts)

    def paintEvent(self, event):        # noqa: N802 (Qt's name)
        total = sum(self._weights.values())
        if total <= 0:
            return
        colors = class_colors()
        gray = unclassified_color()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        height = self._height
        y = (self.height() - height) / 2.0
        width = float(self.width())
        x = 0.0
        pal = charts._active_palette()
        metrics = QFontMetrics(self.font())
        for cls in self.order():
            share = float(self._weights[cls]) / float(total)
            seg = width * share
            color = gray if cls == UNCLASSIFIED else colors.get(cls, gray)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(color)))
            p.drawRect(QRectF(x, y, seg, height))
            if seg >= BAR_MIN_SEPARATOR and x > 0:
                p.setPen(QPen(QColor(pal["window"]), 1))
                p.drawLine(QPointF(x, y), QPointF(x, y + height))
            if self._show_labels:
                label = f"{share * 100:.0f}%"
                if metrics.horizontalAdvance(label) + BAR_LABEL_PADDING <= seg:
                    p.setPen(QPen(QColor(_on_segment_text(color))))
                    p.drawText(QRectF(x, y, seg, height),
                               Qt.AlignCenter, label)
            x += seg
        p.end()


class ClassLegend(QWidget):
    """What the colors in the Composition bars mean (reported).

    Every bar on the page shares one class-to-color map, so ONE legend explains
    all of them -- which is the other half of why the colors are shared. It
    shows only the classes actually present, because a legend listing classes
    nobody holds is a legend the eye learns to skip.

    Swatch and label per class, laid out in a row that wraps by hand: Qt has no
    flow layout, and a single row silently clips its tail at a narrow window
    rather than telling anyone.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._classes: list = []
        self._rows = 1
        self.setMinimumHeight(LEGEND_ROW_HEIGHT)

    def set_classes(self, classes) -> None:
        """``classes`` in draw order; ``unclassified`` may be among them."""
        self._classes = [c for c in classes]
        self.updateGeometry()
        self.update()

    def classes(self) -> list:
        return list(self._classes)

    def _entries(self) -> list:
        """``[(label, color)]`` -- what is actually drawn."""
        colors = class_colors()
        gray = unclassified_color()
        out = []
        for cls in self._classes:
            if cls == UNCLASSIFIED:
                out.append(("Unallocated", gray))
            else:
                out.append((portfolio.ASSET_CLASS_LABELS.get(cls, cls),
                            colors.get(cls, gray)))
        return out

    def _layout(self, width: int) -> list:
        """``[(x, y, label, color)]`` for the current width, wrapping as needed."""
        metrics = QFontMetrics(self.font())
        placed, x, y = [], 0, 0
        for label, color in self._entries():
            item_w = (LEGEND_SWATCH + LEGEND_TEXT_GAP
                      + metrics.horizontalAdvance(label) + LEGEND_ITEM_GAP)
            if x and x + item_w > max(1, width):
                x, y = 0, y + LEGEND_ROW_HEIGHT
            placed.append((x, y, label, color))
            x += item_w
        self._rows = (y // LEGEND_ROW_HEIGHT) + 1
        return placed

    def sizeHint(self):
        from PyQt5.QtCore import QSize
        self._layout(max(1, self.width()))
        return QSize(1, self._rows * LEGEND_ROW_HEIGHT)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # A wrap changes the height this needs, and nothing else would ask.
        self.updateGeometry()

    def paintEvent(self, event):        # noqa: N802 (Qt's name)
        if not self._classes:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = charts._active_palette()
        metrics = QFontMetrics(self.font())
        for x, y, label, color in self._layout(self.width()):
            top = y + (LEGEND_ROW_HEIGHT - LEGEND_SWATCH) / 2.0
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(color)))
            p.drawRoundedRect(QRectF(x, top, LEGEND_SWATCH, LEGEND_SWATCH), 2, 2)
            p.setPen(QPen(QColor(pal["text"])))
            p.drawText(int(x + LEGEND_SWATCH + LEGEND_TEXT_GAP),
                       int(y + (LEGEND_ROW_HEIGHT + metrics.ascent()) / 2.0 - 1),
                       label)
        p.end()


class MixEditor(QDialog):
    """Type a security's or an account's class mixture by hand.

    THE missing capability: ``security_mix.set_mixture`` has existed since
    mixtures did, and until now only ``fetch_mixtures`` ever called it. A 401(k)
    fund has no public ticker, so the fetch cannot help it and there was no
    other way in -- which is why those funds could not be described at all.

    Weights are entered as percentages and normalized on accept, so 60/30/5 is
    taken as the ratio it plainly is rather than refused for summing to 95. All
    zeros CLEARS the mixture, which is how a security goes back to its single
    class.
    """

    def __init__(self, subject: str, weights: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Asset mix - {subject}")
        self._spins: dict = {}
        outer = QVBoxLayout(self)
        intro = QLabel(
            "Percentages of this holding's value. They are scaled to 100 when "
            "you save, so relative sizes are enough. Leave every box at zero to "
            "clear the mix.", self)
        intro.setWordWrap(True)
        outer.addWidget(intro)
        form = QFormLayout()
        current = dict(weights or {})
        for cls in portfolio.ASSET_CLASSES:
            spin = QDoubleSpinBox(self)
            spin.setRange(0.0, 100.0)
            spin.setDecimals(2)
            spin.setSuffix(" %")
            spin.setValue(float(current.get(cls, 0) or 0))
            spin.valueChanged.connect(self._retotal)
            self._spins[cls] = spin
            form.addRow(portfolio.ASSET_CLASS_LABELS.get(cls, cls), spin)
        outer.addLayout(form)
        self.total_label = QLabel("", self)
        outer.addWidget(self.total_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
                                   parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._retotal()

    def _retotal(self) -> None:
        total = sum(s.value() for s in self._spins.values())
        if total <= 0:
            self.total_label.setText("Total 0% - saving clears the mix.")
        else:
            self.total_label.setText(f"Total {total:.2f}% - scaled to 100 on save.")

    def weights(self) -> dict:
        """What was typed, zeros dropped. ``{}`` means clear."""
        return {cls: Decimal(str(spin.value()))
                for cls, spin in self._spins.items() if spin.value() > 0}


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------
class AssetAllocationWindow(QDialog):
    """Accounts, expandable into their securities, each with its composition.

    The tree is the spine because that is where the decision is made: a user
    fixes an unallocated fund by opening the account they hold it in. The bars
    are the answer, and the triangles say where the answer is still incomplete.
    """

    changed = pyqtSignal()

    def __init__(self, conn, parent=None, *, account_ids=None,
                 as_of: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self.account_ids = None if account_ids is None else [int(a) for a in account_ids]
        self.as_of = as_of
        self.setWindowTitle("Asset Allocation")
        self.resize(900, 620)
        outer = QVBoxLayout(self)

        self.summary = QLabel("", self)
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)

        self.total_bar = ClassBar(parent=self)
        outer.addWidget(self.total_bar)
        self.legend = ClassLegend(self)
        outer.addWidget(self.legend)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(len(HEADERS))
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self.edit_item(item))
        header = self.tree.header()
        # Explicit widths, not ResizeToContents: security_mix.describe() of a
        # four-class fund is a long string, and sized to its contents the Mix
        # column took 584px of an 876px viewport and left the bars 104. The user
        # can still drag any of them.
        for col, width in ((COL_NAME, 240), (COL_VALUE, 100),
                           (COL_PCT, 60), (COL_MIX, 170)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            header.resizeSection(col, width)
        # The BAR takes what is left, not the name: the bars are the answer this
        # report exists to give, and a stretched name column pushed them off the
        # right edge behind a horizontal scrollbar.
        header.setSectionResizeMode(COL_BAR, QHeaderView.Stretch)
        header.setStretchLastSection(True)
        outer.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.edit_button = QPushButton("Set mix...", self)
        self.edit_button.setToolTip(
            "Give the selected account or security its asset mix. "
            "Double-clicking a row does the same.")
        self.edit_button.clicked.connect(self.edit_selected)
        row.addWidget(self.edit_button)
        self.clear_button = QPushButton("Clear mix", self)
        self.clear_button.clicked.connect(self.clear_selected)
        row.addWidget(self.clear_button)
        row.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        close.rejected.connect(self.reject)
        row.addWidget(close)
        outer.addLayout(row)

        self.reload()

    # -- data ---------------------------------------------------------------
    def allocation(self):
        """Investments only -- see the module docstring."""
        return portfolio.allocation(self.conn, account_ids=self.account_ids,
                                    as_of=self.as_of, scope="investments")

    def _security_weights(self, symbol: str, value: int) -> tuple:
        """``(weights, defined)`` for one holding.

        ``defined`` is False when the holding has neither a mixture nor a single
        class, which is exactly when its value lands in ``unclassified``.
        """
        mix = security_mix.get_mixture(self.conn, symbol)
        if mix:
            weights = security_mix.split_value(value, mix)
            return weights, UNCLASSIFIED not in weights
        row = portfolio.get_security(self.conn, symbol)
        cls = (row["asset_class"] if row is not None else None) or ""
        if cls:
            return {cls: value}, True
        return {UNCLASSIFIED: value}, False

    def _account_rows(self) -> list:
        """``[(account_id, name, total, [(symbol, value, weights, defined)], cash)]``
        for the scope, largest account first."""
        alloc = self.allocation()
        as_of = alloc.as_of
        out = []
        for slice_ in alloc.by_account:
            aid = int(slice_.key)
            val = portfolio.account_valuation(self.conn, aid, as_of)
            holdings = []
            for h in val.holdings:
                if h.price is None or investments.is_option(self.conn, h.symbol):
                    continue
                weights, defined = self._security_weights(h.symbol,
                                                          int(h.market_value))
                holdings.append((h.symbol, int(h.market_value), weights, defined))
            out.append((aid, slice_.label, int(slice_.value), holdings,
                        int(val.cash)))
        return out

    def _account_cash_weights(self, aid: int, cash: int) -> tuple:
        """How an account's idle cash is allocated, and whether that was STATED.

        Cash falling to the `cash` class by default is not a gap -- it is the
        right answer for a sweep balance and always has been -- so this reports
        ``defined`` True. Only a mixture or an explicit class changes where it
        goes; nothing here can be unallocated.
        """
        if not cash:
            return {}, True
        mix = security_mix.get_account_mixture(self.conn, aid)
        if mix:
            return security_mix.split_value(cash, mix), True
        acct = ledger.get_account(self.conn, aid)
        # Mirror allocation() exactly: an EXPLICIT class on the account takes
        # the balance, and silence means cash. Not account_asset_class(), whose
        # fallback for a non-cash-shaped account is `unclassified` -- correct
        # for a house, wrong for a brokerage sweep, and it would light a warning
        # triangle on every account nobody had classified by hand.
        stated = None
        if acct is not None and "asset_class" in acct.keys():
            stated = (acct["asset_class"] or "").strip() or None
        return {stated or "cash": cash}, True

    # -- rendering ----------------------------------------------------------
    def reload(self) -> None:
        self.tree.clear()
        rows = self._account_rows()
        grand: dict = {}
        any_undefined = False
        total_value = sum(r[2] for r in rows)
        for aid, name, acct_total, holdings, cash in rows:
            acct_weights: dict = {}
            acct_undefined = False
            cash_weights, _ = self._account_cash_weights(aid, cash)
            for cls, part in cash_weights.items():
                acct_weights[cls] = acct_weights.get(cls, 0) + part
            for _sym, _val, weights, defined in holdings:
                if not defined:
                    acct_undefined = True
                for cls, part in weights.items():
                    acct_weights[cls] = acct_weights.get(cls, 0) + part
            any_undefined = any_undefined or acct_undefined

            item = QTreeWidgetItem(self.tree)
            item.setData(COL_NAME, Qt.UserRole, ("account", aid))
            item.setText(COL_NAME, name)
            item.setText(COL_VALUE, _money(acct_total))
            item.setTextAlignment(COL_VALUE, Qt.AlignRight | Qt.AlignVCenter)
            item.setText(COL_PCT, _pct(acct_total, total_value))
            item.setTextAlignment(COL_PCT, Qt.AlignRight | Qt.AlignVCenter)
            own = security_mix.get_account_mixture(self.conn, aid)
            item.setText(COL_MIX, security_mix.describe(own) if own else "")
            font = QFont(item.font(COL_NAME))
            font.setBold(True)
            item.setFont(COL_NAME, font)
            bar = ClassBar(acct_weights, self.tree)
            self.tree.setItemWidget(item, COL_BAR, bar)
            if acct_undefined:
                self._mark(item, "Something in this account has no asset mix, "
                                 "so part of its composition is unallocated.")
            for cls, part in acct_weights.items():
                grand[cls] = grand.get(cls, 0) + part

            if cash:
                kid = QTreeWidgetItem(item)
                kid.setData(COL_NAME, Qt.UserRole, ("cash", aid))
                kid.setText(COL_NAME, "Cash")
                kid.setText(COL_VALUE, _money(cash))
                kid.setTextAlignment(COL_VALUE, Qt.AlignRight | Qt.AlignVCenter)
                kid.setText(COL_PCT, _pct(cash, acct_total))
                kid.setTextAlignment(COL_PCT, Qt.AlignRight | Qt.AlignVCenter)
                kid.setText(COL_MIX, security_mix.describe(own) if own else "")
                self.tree.setItemWidget(kid, COL_BAR,
                                        ClassBar(cash_weights, self.tree))

            for symbol, value, weights, defined in sorted(
                    holdings, key=lambda h: -h[1]):
                kid = QTreeWidgetItem(item)
                kid.setData(COL_NAME, Qt.UserRole, ("security", symbol))
                kid.setText(COL_NAME, symbol)
                kid.setText(COL_VALUE, _money(value))
                kid.setTextAlignment(COL_VALUE, Qt.AlignRight | Qt.AlignVCenter)
                kid.setText(COL_PCT, _pct(value, acct_total))
                kid.setTextAlignment(COL_PCT, Qt.AlignRight | Qt.AlignVCenter)
                mix = security_mix.get_mixture(self.conn, symbol)
                kid.setText(COL_MIX, security_mix.describe(mix) if mix else "")
                self.tree.setItemWidget(kid, COL_BAR, ClassBar(weights, self.tree))
                if not defined:
                    self._mark(kid, "This holding has no asset mix and no asset "
                                    "class, so its value is unallocated.")
            item.setExpanded(True)

        self.total_bar.set_weights(grand)
        # The legend explains every bar on the page, so it lists what the TOTAL
        # holds -- the union of the accounts', in the same order they draw in.
        self.legend.set_classes(self.total_bar.order())
        unallocated = grand.get(UNCLASSIFIED, 0)
        text = f"Investments {_money(total_value)}"
        if unallocated:
            text += (f"   -   {_money(unallocated)} unallocated "
                     f"({100.0 * unallocated / total_value:.1f}%)"
                     if total_value else "")
        self.summary.setText(text)
        self._undefined = any_undefined or bool(unallocated)

    def _mark(self, item, tooltip: str) -> None:
        """Put the triangle on a row and say why.

        An ICON on the name column, not a widget in a column of its own. Qt
        draws the tree's branch indicator in column 0, so a warning widget there
        was clipped by the expander; an icon sits after it, beside the name it
        qualifies, and collides with nothing.
        """
        item.setIcon(COL_NAME, QIcon(warning_pixmap()))
        item.setToolTip(COL_NAME, tooltip)

    def has_unallocated(self) -> bool:
        """Whether anything in scope is still unallocated -- what the triangles
        on the rollups are saying."""
        return bool(getattr(self, "_undefined", False))

    # -- editing ------------------------------------------------------------
    def selected_subject(self):
        """``(kind, key)`` for the selected row, or None."""
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(COL_NAME, Qt.UserRole)

    def edit_selected(self) -> None:
        items = self.tree.selectedItems()
        if items:
            self.edit_item(items[0])

    def edit_item(self, item) -> None:
        subject = item.data(COL_NAME, Qt.UserRole)
        if subject is None:
            return
        kind, key = subject
        if kind == "security":
            current = security_mix.get_mixture(self.conn, key)
            title = key
        else:
            aid = int(key)
            current = security_mix.get_account_mixture(self.conn, aid)
            row = ledger.get_account(self.conn, aid)
            title = row["name"] if row is not None else str(aid)
        editor = self._make_editor(title, current)
        if self._run_editor(editor) != QDialog.Accepted:
            return
        weights = editor.weights()
        if kind == "security":
            security_mix.set_mixture(self.conn, key, weights, source="manual")
        else:
            security_mix.set_account_mixture(self.conn, int(key), weights,
                                             source="manual")
        self.reload()
        self.changed.emit()

    def clear_selected(self) -> None:
        subject = self.selected_subject()
        if subject is None:
            return
        kind, key = subject
        if kind == "security":
            security_mix.set_mixture(self.conn, key, {})
        else:
            security_mix.clear_account_mixture(self.conn, int(key))
        self.reload()
        self.changed.emit()

    # -- seams (a modal exec_() never returns under the offscreen platform) --
    def _make_editor(self, subject: str, weights: dict) -> MixEditor:
        return MixEditor(subject, weights, parent=self)

    def _run_editor(self, editor) -> int:
        return editor.exec_()

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)


def _money(cents: int) -> str:
    from mammon.ui.models import fmt_money
    return fmt_money(int(cents))


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}" if whole else ""
