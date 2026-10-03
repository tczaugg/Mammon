"""Tag Manager -- list every tag and rename / set-or-clear its color / delete it.

A THIN projection over the tag domain in :mod:`mammon.ledger`: every write goes
through a ledger verb (:func:`ledger.rename_tag`, :func:`ledger.set_tag_color`,
:func:`ledger.delete_tag`), so this widget holds no SQL and no invariant logic of
its own -- it gathers a choice and reloads. Mirrors the Category Manager
(:mod:`mammon.ui.categories_dialog`): a ``QDialog`` taking the live connection,
opened from the Tools menu, emitting ``changed`` after any mutation so the owner
refreshes open registers (whose Tag cells may have changed color or spelling) and
the By Tag report.

A tag's color is its IDENTITY color, shown here as a swatch and reused by the
register cell, the split dialog and the By Tag report (see
:func:`ledger.tag_colors`).

Headless-safe (see the offscreen-modal hazard in CLAUDE.md): the public verbs
(``rename_tag`` / ``set_color`` / ``clear_color`` / ``delete_tag``) take ids and
strings and open no modal, so the offscreen tests drive them directly. Only the
private ``_on_*`` button handlers gather input through dialogs, and the destructive
one confirms through the ``QMessageBox.question`` seam that tests patch. The color
picker is a button handler (never a delegate's ``setModelData``), so it opens no
nested event loop during editor teardown -- the heap hazard CLAUDE.md warns about.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QColorDialog, QDialog, QHBoxLayout, QInputDialog, QLabel,
    QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from mammon import ledger
from mammon.ui.swatch import color_square_icon

_DEFAULT_COLOR = "#3b6ea5"


class TagsDialog(QDialog):
    """The tag list with Rename / Set Color / Clear Color / Delete. ``changed``
    fires after any mutation so the owner can refresh registers and the By Tag
    report."""

    changed = pyqtSignal()

    NAME, COLOR, USED = range(3)

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Tag Manager")
        self.resize(460, 420)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Tag", "Color", "Used by"])
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setRootIsDecorated(False)
        self.tree.header().setStretchLastSection(True)
        self.tree.currentItemChanged.connect(lambda *_: self._sync_buttons())

        self.rename_btn = QPushButton("Rename…")
        self.rename_btn.clicked.connect(self._on_rename)
        self.color_btn = QPushButton("Set Color…")
        self.color_btn.clicked.connect(self._on_set_color)
        self.clear_btn = QPushButton("Clear Color")
        self.clear_btn.clicked.connect(self._on_clear_color)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.clicked.connect(self._on_delete)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        for b in (self.rename_btn, self.color_btn, self.clear_btn, self.delete_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)

        hint = QLabel(
            "Rename a tag, give it a color, or delete it. A tag's color is its "
            "identity: the same swatch shows in the register, the split dialog and "
            "the By Tag report. Deleting a tag removes it from every transaction "
            "and split leg that carried it.")
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.tree)
        layout.addLayout(btn_row)

        self._tags: dict = {}
        self._items: dict = {}
        self._reload()

    # ---- data ------------------------------------------------------------
    def _reload(self):
        keep = self._selected_id()
        self.tree.clear()
        self._tags = {}
        self._items = {}
        for t in ledger.list_tags(self.conn):
            item = QTreeWidgetItem(self.tree)
            item.setText(self.NAME, t["name"])
            item.setText(self.COLOR, t["color"] or "")
            item.setText(self.USED, str(t["usage"]))
            item.setData(self.NAME, Qt.UserRole, t["id"])
            if t["color"]:
                item.setIcon(self.NAME, color_square_icon(t["color"]))
            self._tags[t["id"]] = dict(t)
            self._items[t["id"]] = item
        if keep is not None and keep in self._items:
            self.tree.setCurrentItem(self._items[keep])
        self._sync_buttons()

    def _selected_id(self) -> Optional[int]:
        item = self.tree.currentItem()
        if item is None:
            return None
        val = item.data(self.NAME, Qt.UserRole)
        return int(val) if val is not None else None

    def _selected(self) -> Optional[dict]:
        tid = self._selected_id()
        return self._tags.get(tid) if tid is not None else None

    def _sync_buttons(self):
        sel = self._selected()
        has = sel is not None
        for b in (self.rename_btn, self.color_btn, self.delete_btn):
            b.setEnabled(has)
        self.clear_btn.setEnabled(has and bool(sel.get("color")))

    def _confirm(self, title: str, text: str) -> bool:
        """Yes/No confirmation through the ``QMessageBox.question`` seam that the
        offscreen tests patch, so a destructive action never blocks headless."""
        return QMessageBox.question(
            self, title, text, QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No) == QMessageBox.Yes

    # ---- public verbs (no modals: exercised directly by tests) -----------
    def rename_tag(self, tag_id: int, new_name: str) -> None:
        """Rename in place (keeps the id); raises ``ValueError`` on a collision."""
        ledger.rename_tag(self.conn, tag_id, new_name)
        self._reload()
        self.changed.emit()

    def set_color(self, tag_id: int, color: str) -> None:
        """Set a tag's identity color (``#RRGGBB``); raises on a bad hex string."""
        ledger.set_tag_color(self.conn, tag_id, color)
        self._reload()
        self.changed.emit()

    def clear_color(self, tag_id: int) -> None:
        """Clear a tag's color back to "not chosen"."""
        ledger.set_tag_color(self.conn, tag_id, None)
        self._reload()
        self.changed.emit()

    def delete_tag(self, tag_id: int) -> None:
        """Delete the tag, removing it from every transaction and split leg."""
        ledger.delete_tag(self.conn, tag_id)
        self._reload()
        self.changed.emit()

    # ---- button handlers (gather input, then call a public verb) ---------
    def _on_rename(self):
        tag = self._selected()
        if tag is None:
            return
        new, ok = QInputDialog.getText(
            self, "Rename Tag", "New name:", text=tag["name"])
        if not ok:
            return
        try:
            self.rename_tag(tag["id"], new.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Rename Tag", str(e))

    def _on_set_color(self):
        tag = self._selected()
        if tag is None:
            return
        initial = QColor(tag["color"]) if tag.get("color") else QColor(_DEFAULT_COLOR)
        chosen = QColorDialog.getColor(initial, self, "Tag Color")
        if not chosen.isValid():
            return
        try:
            self.set_color(tag["id"], chosen.name())
        except ValueError as e:
            QMessageBox.warning(self, "Set Color", str(e))

    def _on_clear_color(self):
        tag = self._selected()
        if tag is None or not tag.get("color"):
            return
        self.clear_color(tag["id"])

    def _on_delete(self):
        tag = self._selected()
        if tag is None:
            return
        usage = tag.get("usage", 0)
        if usage:
            msg = ("Delete the tag '%s'? It will be removed from %d "
                   "transaction(s) / split leg(s)." % (tag["name"], usage))
        else:
            msg = "Delete the tag '%s'?" % tag["name"]
        if not self._confirm("Delete Tag", msg):
            return
        self.delete_tag(tag["id"])
