"""Password prompting: startup, opening a file, restoring a backup, changing it.

Every test here drives the prompt through the ``_ask_db_password`` seam rather than a
real dialog. That is not merely convenient: a ``QDialog`` exec_()-ed under the
offscreen platform blocks forever, so a test that opened the real one would hang the
suite rather than fail it (CLAUDE.md, "Headless-modal hazard").

The property under test throughout is the same one: **a plaintext ledger must never
prompt**, and an encrypted one must never open without the password.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from mammon import backup, db, encryption, ledger, sqldriver
from mammon.ui import widgets

needs_cipher = pytest.mark.skipif(
    not encryption.available(),
    reason="needs the sqlcipher3 driver (pip install mammon[encryption])")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _ledger(path, payee="Northwind Market") -> str:
    conn = db.init_db(str(path))
    acct = ledger.create_account(conn, "Checking", "checking", opening_balance=1_000_00)
    ledger.add_transaction(conn, acct, "2026-01-05", -25_00, payee=payee)
    conn.commit()
    conn.close()
    return str(path)


class _Window(widgets.MainWindow):
    """MainWindow with the password prompt replaced by a scripted answer."""

    scripted = None
    asked = None

    def _ask_db_password(self, path):
        type(self).asked = str(path)
        return self.scripted


# --- the silent default ---------------------------------------------------

def test_a_plaintext_ledger_is_never_asked_for_a_password(qapp, tmp_path):
    """The whole design rests on this: with no encryption there is no dialog, no
    delay, and nothing different from before the feature existed."""
    p = _ledger(tmp_path / "plain.db")
    _Window.scripted, _Window.asked = "should not be used", None
    win = _Window(db.init_db(p), db_path=p)
    try:
        win.open_database(p)
        assert _Window.asked is None
        assert win.db_key is None
    finally:
        win.close()


# --- opening an encrypted file -------------------------------------------

@needs_cipher
def test_opening_an_encrypted_ledger_prompts_and_opens_it(qapp, tmp_path):
    plain = _ledger(tmp_path / "l.db")
    enc = encryption.encrypt_database(plain, "pw")
    win = _Window(db.init_db(plain), db_path=plain)
    try:
        _Window.scripted, _Window.asked = "pw", None
        win.open_database(str(enc))
        assert _Window.asked == str(enc)
        assert win.db_key == "pw"
        assert win.conn.execute(
            "SELECT payee FROM transactions WHERE payee IS NOT NULL"
        ).fetchone()["payee"] == "Northwind Market"
    finally:
        win.close()


@needs_cipher
def test_cancelling_the_prompt_leaves_the_window_where_it_was(qapp, tmp_path):
    """Cancel must mean "do not open", never "open it unkeyed"."""
    plain = _ledger(tmp_path / "l.db")
    enc = encryption.encrypt_database(plain, "pw")
    win = _Window(db.init_db(plain), db_path=plain)
    try:
        _Window.scripted = None                 # user pressed Cancel
        win.open_database(str(enc))
        assert win.db_path == plain             # unchanged
        assert win.db_key is None
    finally:
        win.close()


@needs_cipher
def test_a_supplied_key_skips_the_prompt(qapp, tmp_path):
    """Startup and the password-change flow pass the key they already hold; asking
    again for a password the app just used would be noise."""
    plain = _ledger(tmp_path / "l.db")
    enc = encryption.encrypt_database(plain, "pw")
    win = _Window(db.init_db(plain), db_path=plain)
    try:
        _Window.scripted, _Window.asked = "wrong", None
        win.open_database(str(enc), "pw")
        assert _Window.asked is None
        assert win.db_key == "pw"
    finally:
        win.close()


# --- backups of an encrypted ledger --------------------------------------

@needs_cipher
def test_backups_of_an_encrypted_ledger_are_encrypted(qapp, tmp_path, monkeypatch):
    """A snapshot is the copy most likely to leave the machine. It must not be the
    only unprotected one -- and without the key the backup API refuses outright,
    which the auto-backup would swallow, silently leaving no backups at all."""
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", tmp_path / "backups")
    plain = _ledger(tmp_path / "l.db")
    enc = encryption.encrypt_database(plain, "pw")
    win = _Window(db.init_db(str(enc), "pw"), db_path=str(enc), db_key="pw")
    try:
        # _start_autobackup seeds the change fingerprint from the state at open, so
        # an idle session writes nothing at all. Edit something first, or the tick
        # correctly skips and this test proves only that the guard works.
        ledger.add_transaction(win.conn, 1, "2026-02-01", -10_00, payee="Alder Cafe")
        win.conn.commit()
        win._autobackup_tick()
        made = list((tmp_path / "backups").rglob("*.bak")) + \
            list((tmp_path / "backups").rglob("*.delta"))
        assert made, "the auto-backup wrote nothing for an encrypted ledger"
        assert all(encryption.is_encrypted(m) for m in made if m.suffix == ".bak")
        assert not any(b"Northwind Market" in m.read_bytes() for m in made)
    finally:
        win.close()


@needs_cipher
def test_a_restored_encrypted_snapshot_still_opens(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", tmp_path / "backups")
    plain = _ledger(tmp_path / "l.db")
    enc = encryption.encrypt_database(plain, "pw")
    conn = db.init_db(str(enc), "pw")
    snap = backup.create_backup(conn, str(enc), tag=backup.MANUAL_TAG, key="pw")
    conn.close()
    out = backup.restore_backup(snap, tmp_path / "restored.db")
    assert encryption.is_encrypted(out)
    restored = db.connect(str(out), "pw")
    try:
        assert restored.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert restored.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    finally:
        restored.close()


# --- the wrong password ---------------------------------------------------

@needs_cipher
def test_the_wrong_password_raises_rather_than_opening_something(tmp_path):
    plain = _ledger(tmp_path / "l.db")
    enc = encryption.encrypt_database(plain, "right")
    with pytest.raises(sqldriver.DatabaseError):
        db.connect(str(enc), "wrong")


@needs_cipher
def test_a_password_with_an_apostrophe_round_trips(tmp_path):
    """PRAGMA key cannot be parameterised, so the key is quoted by hand."""
    plain = _ledger(tmp_path / "l.db")
    pw = "don't guess 'this'"
    enc = encryption.encrypt_database(plain, pw)
    conn = db.connect(str(enc), pw)
    try:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    finally:
        conn.close()


# --- backups that predate encryption --------------------------------------

@needs_cipher
def test_plaintext_snapshots_are_found_including_deltas(tmp_path, monkeypatch):
    """Enabling encryption does nothing for the copies already on disk. Deltas
    count too: a delta of a plaintext baseline holds plaintext pages, whatever it
    wraps them in, and it carries no SQLite header of its own to check."""
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", tmp_path / "backups")
    p = _ledger(tmp_path / "l.db", payee="SECRETPAYEE")
    conn = db.init_db(p)
    backup.create_backup(conn, p, tag=backup.MANUAL_TAG)
    backup.create_backup(conn, p, tag=backup.AUTO_TAG, incremental=True)
    ledger.add_transaction(conn, 1, "2026-03-01", -1_00, payee="Alder Cafe")
    conn.commit()
    backup.create_backup(conn, p, tag=backup.AUTO_TAG, incremental=True)
    conn.close()

    stale = backup.plaintext_snapshots(p)
    assert len(stale) == 3
    assert any(s.suffix == ".delta" for s in stale), "a delta was missed"
    assert any(b"SECRETPAYEE" in s.read_bytes() for s in stale if s.suffix == ".bak")


@needs_cipher
def test_snapshots_of_an_encrypted_ledger_are_not_reported_as_plaintext(
        tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", tmp_path / "backups")
    plain = _ledger(tmp_path / "l.db")
    enc = encryption.encrypt_database(plain, "pw")
    conn = db.init_db(str(enc), "pw")
    backup.create_backup(conn, str(enc), tag=backup.MANUAL_TAG, key="pw")
    conn.close()
    assert backup.plaintext_snapshots(str(enc)) == []


@needs_cipher
def test_the_user_is_offered_the_chance_to_clear_them(qapp, tmp_path, monkeypatch):
    """Offered, not done: for the next few minutes those snapshots are the only
    way back in if the new password was mistyped."""
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", tmp_path / "backups")
    p = _ledger(tmp_path / "l.db")
    conn = db.init_db(p)
    backup.create_backup(conn, p, tag=backup.MANUAL_TAG)
    conn.close()

    win = _Window(db.init_db(p), db_path=p)
    try:
        asked = []
        monkeypatch.setattr(
            widgets.QMessageBox, "question",
            staticmethod(lambda *a, **k: asked.append(a[2]) or widgets.QMessageBox.No))
        monkeypatch.setattr(widgets.QMessageBox, "information",
                            staticmethod(lambda *a, **k: None))
        win._offer_to_clear_plaintext_backups()
        assert asked, "the user was never told about the plaintext snapshots"
        assert "unencrypted" in asked[0]
        assert backup.plaintext_snapshots(p), "answering No must keep them"

        asked.clear()
        monkeypatch.setattr(
            widgets.QMessageBox, "question",
            staticmethod(lambda *a, **k: asked.append(a[2]) or widgets.QMessageBox.Yes))
        win._offer_to_clear_plaintext_backups()
        assert backup.plaintext_snapshots(p) == [], "answering Yes must delete them"
    finally:
        win.close()
