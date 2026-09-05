"""Asking for the database password: unlocking, setting, changing, removing.

Mammon holds no password anywhere. The user types it when the ledger is opened, the
derived key lives in the running process for that session only, and nothing about it
is written to QSettings, the database, or any file. Close the app and the key is gone.
That is the same rule the download side follows -- see CLAUDE.md, "Configuration" --
and it is why there is no "remember my password" here.

**A plaintext ledger is never prompted for.** Callers ask
:func:`mammon.encryption.is_encrypted` first, which reads the file header, so the
default experience is completely unchanged: no password, no dialog, no difference.

The two dialogs are separate because they answer different questions:

* :func:`ask_password` -- "what is the password for this file?", asked when a file
  turns out to be encrypted. It loops until the password works or the user gives up,
  bounded by ``attempts`` so an unattended run cannot spin forever.
* :class:`SetPasswordDialog` -- "what should the password become?", which covers
  setting one on a plaintext ledger, changing an existing one, and removing it (leave
  the new password blank). One dialog, because they are one decision.
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QVBoxLayout)

from mammon import db as db_mod
from mammon import encryption, sqldriver

MAX_ATTEMPTS = 3


def ask_password(parent, path, *, attempts: int = MAX_ATTEMPTS,
                 title: str = "Password required") -> Optional[str]:
    """Prompt until ``path`` opens, and return the working password.

    ``None`` means the user cancelled, or ran out of attempts -- callers must treat
    that as "do not open this file", never as "open it without a key".

    The loop is bounded rather than infinite on purpose: this runs at startup, and
    anything that can run unattended has to terminate on its own (CLAUDE.md).
    """
    import os
    name = os.path.basename(str(path))
    prompt = f"{name} is encrypted.\nEnter its password:"
    for remaining in range(attempts, 0, -1):
        text, ok = QInputDialog.getText(parent, title, prompt, QLineEdit.Password)
        if not ok:
            return None
        if not text:
            prompt = f"{name} is encrypted.\nA password is required:"
            continue
        try:
            conn = db_mod.connect(str(path), text)
        except sqldriver.DatabaseError:
            if remaining == 1:
                QMessageBox.critical(
                    parent, title,
                    "That password did not work, and there are no attempts left.\n\n"
                    "The file is not damaged -- reopen it to try again.")
                return None
            prompt = (f"That password did not open {name}.\n"
                      f"Try again ({remaining - 1} left):")
            continue
        conn.close()                    # opened only to prove the password
        return text
    return None


class SetPasswordDialog(QDialog):
    """Set, change, or remove the password on a database file.

    Leaving the new password blank REMOVES encryption, which is the honest way to
    expose it: the field's own emptiness is what "no encryption" means everywhere
    else in this design, so it should mean that here too rather than hiding behind a
    separate checkbox the user has to find.
    """

    def __init__(self, parent=None, *, encrypted: bool = False):
        super().__init__(parent)
        self.encrypted = encrypted
        self.setWindowTitle("Change Database Password" if encrypted
                            else "Set Database Password")

        outer = QVBoxLayout(self)
        blurb = QLabel(
            "Changing the password rewrites the database file.\nA backup is taken "
            "first, and the original is kept until the new file is verified."
            if encrypted else
            "Setting a password encrypts the whole database file: every account,\n"
            "transaction and price, plus the backups taken from it.\n\n"
            "There is no recovery. If you forget it, the data is gone.")
        blurb.setWordWrap(True)
        outer.addWidget(blurb)

        form = QFormLayout()
        self.current = QLineEdit()
        self.current.setEchoMode(QLineEdit.Password)
        if encrypted:
            form.addRow("Current password", self.current)
        self.new_pw = QLineEdit()
        self.new_pw.setEchoMode(QLineEdit.Password)
        form.addRow("New password", self.new_pw)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.Password)
        form.addRow("Confirm", self.confirm)
        outer.addLayout(form)

        hint = QLabel("Leave the new password blank to remove encryption."
                      if encrypted else
                      "Leave blank to cancel without encrypting.")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

    def _accept(self) -> None:
        """Validate before closing, so a typo is caught here and not by a rewrite."""
        if self.new_pw.text() != self.confirm.text():
            QMessageBox.warning(self, self.windowTitle(),
                                "The new password and its confirmation differ.")
            return
        if self.encrypted and not self.current.text():
            QMessageBox.warning(self, self.windowTitle(),
                                "The current password is required.")
            return
        self.accept()

    def values(self) -> tuple:
        """``(current, new)``. An empty ``new`` means "remove encryption"."""
        return self.current.text(), self.new_pw.text()


def describe(path) -> str:
    """One line for a status/menu label: whether this file is encrypted."""
    if not encryption.available():
        return "Encryption unavailable (pip install mammon[encryption])"
    return "Encrypted" if encryption.is_encrypted(path) else "Not encrypted"
