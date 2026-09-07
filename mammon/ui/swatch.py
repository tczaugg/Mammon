"""Tiny colored-square swatches for tag colors -- pure Qt, no data access.

A tag's color (a ``#RRGGBB`` string, read from :func:`mammon.ledger.tag_colors`)
is surfaced as a small filled square: an icon in the Tag Manager list and the By
Tag report table, and a hand-painted rect in the register / split delegates.
Centralised here so every surface draws the same chip, and so ``ui/`` keeps the
one colour-to-pixel step in one place with no SQL and no money logic near it.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap


def color_square_pixmap(color: str, size: int = 12) -> QPixmap:
    """A ``size`` x ``size`` pixmap filled with ``color`` (a ``#RRGGBB`` string),
    outlined with a faint border so a light swatch stays visible on a light row."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setBrush(QColor(color))
    painter.setPen(QColor(0, 0, 0, 60))
    painter.drawRect(0, 0, size - 1, size - 1)
    painter.end()
    return pix


def color_square_icon(color: str, size: int = 12) -> QIcon:
    """A :class:`QIcon` of a filled square in ``color``, for list / table items."""
    return QIcon(color_square_pixmap(color, size))
