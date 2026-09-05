"""Category Manager -- view the category tree and add / rename / reparent /
merge / delete categories.

A THIN projection over the category domain in :mod:`mammon.ledger`: every write
goes through a ledger function (``create_category``, ``rename_category``,
``reparent_category``, ``merge_category``, ``delete_category``), so this widget
holds no SQL and no invariant logic of its own -- it only gathers a choice and
reloads. That split matters most for the destructive verbs. *Merge* re-points
every transaction, split, learned rule, budget line and scheduled payment onto
the survivor, and *delete* refuses to orphan in-use transactions -- it demands a
replacement and reassigns them first. Both rules live in the domain layer, so
they hold no matter who calls them, and this dialog cannot break them by
accident.

Mirrors the Budgets / Rules Manager dialogs: a ``QDialog`` taking the live
connection, opened from the Tools menu, emitting ``changed`` after any mutation
so the owner refreshes open registers whose category labels may have moved.

Headless-safe (see the offscreen-modal hazard in CLAUDE.md): the public verbs
(``add_category``/``rename_category``/``reparent_category``/``merge_into``/
``delete_category``) take ids and strings and never open a modal, so the
offscreen tests drive them directly. Only the private ``_on_*`` button handlers
gather input through dialogs, and every destructive one confirms through the
``QMessageBox.question`` seam that tests patch.

The Type column surfaces the classic income/expense classification derived in
:mod:`mammon.category_types` so the same split is visible here.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)

from mammon import category_types, ledger

_TOP_LEVEL = "(top level)"


class ReplacementCategoryDialog(QDialog):
    """Ask which category should adopt the transactions of a category being
    deleted. The combo is editable: pick an existing category from the list or
    type a new ``Parent:Child`` name to create one."""

    def __init__(self, parent=None, *, doomed_path: str = "",
                 choices: Optional[list[str]] = None, usage: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Reassign Transactions")
        form = QFormLayout()
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.addItems(choices or [])
        self.combo.setCurrentText("")
        form.addRow("Move transactions to:", self.combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "'%s' is used by %d transaction(s). Choose an existing category or "
            "type a new name to reassign them to before it is deleted."
            % (doomed_path, usage))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self):
        if not self.combo.currentText().strip():
            QMessageBox.warning(
                self, "Reassign Transactions",
                "A replacement category is required.")
            return
        self.accept()

    def value(self) -> str:
        return self.combo.currentText().strip()


class CategoriesDialog(QDialog):
    """The category tree with Add / Rename / Reparent / Merge / Delete. ``changed``
    fires after any mutation so the owner can refresh registers and pickers."""

    changed = pyqtSignal()

    PATH, TYPE, USED = range(3)

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Category Manager")
        self.resize(560, 460)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Category", "Type", "Used by"])
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setRootIsDecorated(True)
        self.tree.header().setStretchLastSection(True)
        self.tree.currentItemChanged.connect(lambda *_: self._sync_buttons())

        self.add_btn = QPushButton("Add…")
        self.add_btn.clicked.connect(self._on_add)
        self.rename_btn = QPushButton("Rename…")
        self.rename_btn.clicked.connect(self._on_rename)
        self.reparent_btn = QPushButton("Reparent…")
        self.reparent_btn.clicked.connect(self._on_reparent)
        self.merge_btn = QPushButton("Merge…")
        self.merge_btn.clicked.connect(self._on_merge)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.clicked.connect(self._on_delete)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        for b in (self.add_btn, self.rename_btn, self.reparent_btn,
                  self.merge_btn, self.delete_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)

        hint = QLabel(
            "Add, rename, reparent, merge or delete categories. Merging moves "
            "every transaction, rule and budget line onto the target and removes "
            "the source; deleting a category still in use asks for a replacement "
            "first, so no transaction is ever orphaned.")
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.tree)
        layout.addLayout(btn_row)

        self._nodes: dict = {}
        self._items: dict = {}
        self._reload()

    # ---- data ------------------------------------------------------------
    def _reload(self):
        keep = self._selected_id()
        self.tree.clear()
        self._nodes = {}
        self._items = {}
        self._types = category_types.classify_categories(self.conn)
        self._add_children(None, self.tree.invisibleRootItem())
        self.tree.expandAll()
        if keep is not None and keep in self._items:
            self.tree.setCurrentItem(self._items[keep])
        self._sync_buttons()

    def _add_children(self, parent_id, parent_item):
        for c in ledger.category_children(self.conn, parent_id, include_hidden=True):
            item = QTreeWidgetItem(parent_item)
            item.setText(self.PATH, c["name"])
            item.setText(self.TYPE, self._types.get(c["id"], "expense"))
            item.setText(
                self.USED, str(ledger.count_category_usage(self.conn, c["id"])))
            item.setData(self.PATH, Qt.UserRole, c["id"])
            self._nodes[c["id"]] = {
                "id": c["id"], "name": c["name"], "parent_id": parent_id,
                "path": ledger.category_path(self.conn, c["id"])}
            self._items[c["id"]] = item
            self._add_children(c["id"], item)

    def _selected_id(self) -> Optional[int]:
        item = self.tree.currentItem()
        if item is None:
            return None
        val = item.data(self.PATH, Qt.UserRole)
        return int(val) if val is not None else None

    def _selected(self) -> Optional[dict]:
        cid = self._selected_id()
        return self._nodes.get(cid) if cid is not None else None

    def _others(self, cat_id) -> list[dict]:
        """Every category except ``cat_id`` as ``{'id', 'path'}``, path-sorted --
        the candidate pool for a reparent target or a merge/delete destination."""
        return [c for c in ledger.list_categories(self.conn, include_hidden=True)
                if c["id"] != cat_id]

    def _sync_buttons(self):
        has = self._selected() is not None
        for b in (self.rename_btn, self.reparent_btn, self.merge_btn,
                  self.delete_btn):
            b.setEnabled(has)

    def _confirm(self, title: str, text: str) -> bool:
        """Yes/No confirmation through the ``QMessageBox.question`` seam that the
        offscreen tests patch, so a destructive action never blocks headless."""
        return QMessageBox.question(
            self, title, text, QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No) == QMessageBox.Yes

    # ---- public verbs (no modals: exercised directly by tests) -----------
    def add_category(self, path: str) -> Optional[int]:
        """Get-or-create a category from a ``Parent:Child`` path; reload + signal."""
        cid = ledger.create_category(self.conn, path)
        self._reload()
        self.changed.emit()
        return cid

    def rename_category(self, cat_id: int, new_name: str) -> None:
        """Rename in place (keeps the id); raises ``ValueError`` on a collision."""
        ledger.rename_category(self.conn, cat_id, new_name)
        self._reload()
        self.changed.emit()

    def reparent_category(self, cat_id: int, new_parent_id: Optional[int]) -> None:
        """Move under a new parent (``None`` = top level); raises on a cycle."""
        ledger.reparent_category(self.conn, cat_id, new_parent_id)
        self._reload()
        self.changed.emit()

    def merge_into(self, from_id: int, to_id: int) -> None:
        """Merge ``from_id`` into ``to_id`` (repoints everything, deletes source)."""
        ledger.merge_category(self.conn, from_id, to_id)
        self._reload()
        self.changed.emit()

    def delete_category(self, cat_id: int,
                        replacement_id: Optional[int] = None) -> None:
        """Delete ``cat_id``, reassigning its rows to ``replacement_id`` first."""
        ledger.delete_category(self.conn, cat_id, replacement_id=replacement_id)
        self._reload()
        self.changed.emit()

    # ---- button handlers (gather input, then call a public verb) ---------
    def _on_add(self):
        path, ok = QInputDialog.getText(
            self, "Add Category",
            "New category (use Parent:Child for a sub-category):")
        if not ok or not path.strip():
            return
        self.add_category(path.strip())

    def _on_rename(self):
        cat = self._selected()
        if cat is None:
            return
        new, ok = QInputDialog.getText(
            self, "Rename Category", "New name:", text=cat["name"])
        if not ok:
            return
        try:
            self.rename_category(cat["id"], new.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Rename Category", str(e))

    def _on_reparent(self):
        cat = self._selected()
        if cat is None:
            return
        others = self._others(cat["id"])
        options = [_TOP_LEVEL] + [c["path"] for c in others]
        choice, ok = QInputDialog.getItem(
            self, "Reparent Category",
            "New parent for '%s':" % cat["path"], options, 0, False)
        if not ok or not choice:
            return
        new_parent = None if choice == _TOP_LEVEL else next(
            (c["id"] for c in others if c["path"] == choice), None)
        try:
            self.reparent_category(cat["id"], new_parent)
        except ValueError as e:
            QMessageBox.warning(self, "Reparent Category", str(e))

    def _on_merge(self):
        cat = self._selected()
        if cat is None:
            return
        others = self._others(cat["id"])
        if not others:
            QMessageBox.information(
                self, "Merge Category", "There is no other category to merge into.")
            return
        paths = [c["path"] for c in others]
        target, ok = QInputDialog.getItem(
            self, "Merge Category",
            "Merge '%s' into:" % cat["path"], paths, 0, False)
        if not ok or not target:
            return
        to_id = next((c["id"] for c in others if c["path"] == target), None)
        if to_id is None:
            return
        if not self._confirm(
                "Merge Category",
                "Merge '%s' into '%s'? Every transaction, rule and budget line "
                "moves onto '%s' and '%s' is removed."
                % (cat["path"], target, target, cat["path"])):
            return
        try:
            self.merge_into(cat["id"], to_id)
        except ValueError as e:
            QMessageBox.warning(self, "Merge Category", str(e))

    def _on_delete(self):
        cat = self._selected()
        if cat is None:
            return
        usage = ledger.count_category_usage(self.conn, cat["id"])
        if usage == 0:
            if not self._confirm(
                    "Delete Category", "Delete the category '%s'?" % cat["path"]):
                return
            self.delete_category(cat["id"])
            return

        # In use -> demand a replacement and reassign before deleting, so those
        # transactions are never orphaned onto NULL.
        choices = [c["path"] for c in self._others(cat["id"])]
        dlg = ReplacementCategoryDialog(
            self, doomed_path=cat["path"], choices=choices, usage=usage)
        if dlg.exec_() != QDialog.Accepted:
            return
        replacement = ledger.create_category(self.conn, dlg.value())
        if replacement is None or replacement == cat["id"]:
            QMessageBox.warning(
                self, "Delete Category",
                "Pick a different category to reassign transactions to.")
            return
        self.delete_category(cat["id"], replacement_id=replacement)
