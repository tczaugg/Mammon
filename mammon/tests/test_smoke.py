"""The ONE test that runs no matter what changed: does the app come up and show data?

Every other test file in this suite is local -- it probes one module, and its
result is a foregone conclusion unless that module changed. This file is the
exception, and it exists because the failure it catches is the only one that is
genuinely global: the window not building, or building empty.

So it asserts through the DISPLAY, never through the domain. Reading balances
back out of `ledger` would prove the arithmetic and prove nothing about whether
a user sees it; these assertions go through the same models the widgets paint
from (`AccountBar.model`, `RegisterModel.data`), because "the GUI runs and
displays data" is the whole requirement.

Keep it that way: one test, no mocks on the display path, and no growth. A file
that has to run on every change is the most expensive file in the repo.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db
from mammon.tests import fresh_db


@pytest.fixture
def qapp():
    from PyQt5.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def test_the_app_runs_and_displays_data(qapp, tmp_path):
    from PyQt5.QtCore import Qt
    from mammon.app import sample_data
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import MainWindow

    conn = fresh_db(tmp_path / "smoke.db")
    sample_data(conn)
    win = MainWindow(conn)
    try:
        # 1. The window built, with its menus.
        titles = {a.text().replace("&", "") for a in win.menuBar().actions()}
        assert {"File", "Reports"} <= titles

        # 2. The account bar LISTS the accounts (an empty bar is the bug this
        #    catches: a window that builds and shows nothing).
        assert win.accounts.model.rowCount() >= 3

        # 3. A register OPENS and shows rows.
        account_id = int(win.accounts.model.account_id_at(0))
        win.open_register(account_id)
        reg = win._registers.get(account_id)
        assert reg is not None, "opening an account produced no register"
        assert reg.model.rowCount() >= 1, "the register displayed no rows"

        # 4. The rows carry RENDERED values, read through the display role the
        #    view actually paints from: the date, both money columns of a classic
        #    register (payment and deposit are separate columns, not one signed
        #    amount) and the running balance. A register full of blank cells
        #    satisfies a row count and nothing else.
        shown = [
            str(reg.model.data(reg.model.index(row, col), Qt.DisplayRole) or "")
            for row in range(min(reg.model.rowCount(), 5))
            for col in (RegisterModel.DATE, RegisterModel.PAYMENT,
                        RegisterModel.DEPOSIT, RegisterModel.BALANCE)
        ]
        assert any(text.strip() for text in shown), "every displayed cell was blank"
        assert any(any(ch.isdigit() for ch in text) for text in shown), \
            "no displayed cell contained a number"
    finally:
        win.close()
        conn.close()
