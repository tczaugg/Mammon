"""Tag Manager -- the offscreen dialog that projects the ledger tag verbs.

Mirrors test_category_manager.py: the public verbs (rename / set_color /
clear_color / delete) mutate through ``mammon.ledger`` and reload the list, the
destructive delete confirms through the ``QMessageBox.question`` seam, and
``changed`` fires after any mutation so the owner refreshes open registers and the
By Tag report. Synthetic data only -- no PII.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QMessageBox

from mammon import db, ledger
from mammon.ui.tags_dialog import TagsDialog
from mammon.tests import fresh_db


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "tagmgr.db")
    yield c
    c.close()


def _acct(conn):
    return ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)


def test_reload_lists_all_tags_with_color_and_usage(qapp, conn):
    acct = _acct(conn)
    v = ledger.tag_id(conn, "Vacation")
    ledger.set_tag_color(conn, v, "#112233")
    ledger.tag_id(conn, "Spare")                    # never applied
    t = ledger.add_transaction(conn, acct, "2026-03-01", -50_00, payee="X")
    ledger.set_tags(conn, t, "Vacation")
    w = TagsDialog(conn)
    assert {w._tags[tid]["name"] for tid in w._tags} == {"Vacation", "Spare"}
    assert w._items[v].text(w.COLOR) == "#112233"
    assert w._items[v].text(w.USED) == "1"
    assert not w._items[v].icon(w.NAME).isNull()    # colored -> swatch shown


def test_widget_rename_updates_list_and_signals(qapp, conn):
    tid = ledger.tag_id(conn, "vac")
    w = TagsDialog(conn)
    fired = []
    w.changed.connect(lambda: fired.append(1))
    w.rename_tag(tid, "Vacation")
    assert w._tags[tid]["name"] == "Vacation"
    assert fired == [1]


def test_widget_rename_collision_raises(qapp, conn):
    a = ledger.tag_id(conn, "Home")
    ledger.tag_id(conn, "Work")
    w = TagsDialog(conn)
    with pytest.raises(ValueError):
        w.rename_tag(a, "Work")


def test_widget_set_and_clear_color(qapp, conn):
    tid = ledger.tag_id(conn, "Vacation")
    w = TagsDialog(conn)
    w.set_color(tid, "#3b6ea5")
    assert w._tags[tid]["color"] == "#3b6ea5"
    assert not w._items[tid].icon(w.NAME).isNull()  # swatch appears
    w.clear_color(tid)
    assert w._tags[tid]["color"] is None
    assert w._items[tid].icon(w.NAME).isNull()      # swatch gone


def test_clear_button_tracks_selection_color(qapp, conn):
    tid = ledger.tag_id(conn, "Plain")
    w = TagsDialog(conn)
    w.tree.setCurrentItem(w._items[tid])
    assert w.clear_btn.isEnabled() is False         # no color yet
    w.set_color(tid, "#112233")
    w.tree.setCurrentItem(w._items[tid])
    assert w.clear_btn.isEnabled() is True


def test_widget_delete_confirm_yes(qapp, conn, monkeypatch):
    acct = _acct(conn)
    tid = ledger.tag_id(conn, "Doomed")
    t = ledger.add_transaction(conn, acct, "2026-03-01", -50_00, payee="X")
    ledger.set_tags(conn, t, "Doomed")
    w = TagsDialog(conn)
    w.tree.setCurrentItem(w._items[tid])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w._on_delete()
    assert tid not in w._tags
    assert ledger.get_tag(conn, tid) is None
    assert ledger.get_tags(conn, t) == []


def test_widget_delete_confirm_no_keeps_tag(qapp, conn, monkeypatch):
    tid = ledger.tag_id(conn, "Keep")
    w = TagsDialog(conn)
    w.tree.setCurrentItem(w._items[tid])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    w._on_delete()
    assert tid in w._tags                            # declined -> still present
