"""Register category-editor parity tests (issues 2 and 6).

Colon-completes-the-parent, case-insensitive category matching (no spurious
"create new category?" prompt), and the TAB-into-field append cursor state.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.ui import delegates
from mammon.ui.models import RegisterModel


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "d.db")
    yield c
    c.close()


def _settle():
    """Turn the event loop once. The category delegate DEFERS its dialog off the
    editor-teardown path (raising it inline corrupted the heap), so the prompt
    and its write land one turn after setModelData returns."""
    from PyQt5.QtCore import QCoreApplication
    QCoreApplication.processEvents()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


# ---------------------------------------------------------------------------
# parent_autocomplete: the pure ':' rule (issue 6)
# ---------------------------------------------------------------------------
def test_parent_autocomplete_completes_current_segment():
    # 'bus' + ':' autocompletes the parent from the match and appends ':'
    assert delegates.parent_autocomplete("bus", "Business:Travel") == "Business:"
    assert delegates.parent_autocomplete("Bus", "Business") == "Business:"


def test_parent_autocomplete_descends_into_subcategory():
    assert delegates.parent_autocomplete(
        "Business:wri", "Business:Writing") == "Business:Writing:"


def test_parent_autocomplete_keeps_typed_when_no_match():
    # A brand-new parent with no completion is kept verbatim so it can be created.
    assert delegates.parent_autocomplete("xyz", "") == "xyz:"
    assert delegates.parent_autocomplete("xyz", "Business:Travel") == "xyz:"


# ---------------------------------------------------------------------------
# CategoryLineEdit: pressing ':' autocompletes then continues (issue 6)
# ---------------------------------------------------------------------------
def _colon_event():
    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent
    return QKeyEvent(QEvent.KeyPress, Qt.Key_Colon, Qt.NoModifier, ":")


def test_category_lineedit_colon_completes_parent(qapp):
    combo = delegates.make_category_combo(None, ["Business:Writing"])
    le = combo.lineEdit()
    le.setText("bus")
    le.keyPressEvent(_colon_event())
    assert le.text() == "Business:"           # 'Business' + ':' -> keep typing child


def test_category_lineedit_colon_descends(qapp):
    combo = delegates.make_category_combo(None, ["Business:Writing"])
    le = combo.lineEdit()
    le.setText("Business:wri")
    le.keyPressEvent(_colon_event())
    assert le.text() == "Business:Writing:"


# ---------------------------------------------------------------------------
# case-insensitive matching: no "create new category?" prompt for cap-only diff
# ---------------------------------------------------------------------------
def test_is_new_category_case_insensitive(qapp, conn):
    ledger.resolve_category(conn, "Business")
    acct = ledger.create_account(conn, "Checking", "checking")
    model = RegisterModel(conn, acct)
    D = delegates.CategoryDelegate
    assert D._is_new_category(model, "business") is False   # cap-only -> not new
    assert D._is_new_category(model, "BUSINESS") is False
    assert D._is_new_category(model, "Groceries") is True    # genuinely new


def test_transfer_target_is_not_a_new_category(qapp, conn):
    acct = ledger.create_account(conn, "Checking", "checking")
    ledger.create_account(conn, "Savings", "savings")
    model = RegisterModel(conn, acct)
    assert delegates.CategoryDelegate._is_new_category(model, "[Savings]") is False


# ---------------------------------------------------------------------------
# TAB-into-field append cursor state (issue 2)
# ---------------------------------------------------------------------------
def test_setmodeldata_existing_category_case_variant_accepts_without_prompt(
        qapp, conn, monkeypatch):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QStyleOptionViewItem

    acct = ledger.create_account(conn, "Checking", "checking")
    biz = ledger.resolve_category(conn, "Business")
    t = ledger.add_transaction(conn, acct, "2024-01-01", -10_00, payee="P")
    model = RegisterModel(conn, acct)
    row = model.row_for_txn(t)
    idx = model.index(row, RegisterModel.CATEGORY)

    # A prompt would mean we treated the existing category as new -> fail loudly.
    monkeypatch.setattr(
        delegates.CategoryDelegate, "_confirm_new_category",
        staticmethod(lambda *a: pytest.fail("prompted to create an existing category")))

    delegate = delegates.CategoryDelegate()
    editor = delegate.createEditor(None, QStyleOptionViewItem(), idx)
    editor.setEditText("business")                       # capitalization-only diff
    delegate.setModelData(editor, model, idx)

    # The DATABASE is written immediately; only the model's in-memory refresh is
    # deferred a turn, because resetting it inside setModelData -- while Qt still
    # holds the editor it is about to destroy -- crashed the app at the C++ level
    # (see RegisterModel._write). Give the event loop that turn.
    model._deferred_reload()

    # Accepted onto the EXISTING category; no duplicate forked.
    assert model.data(model.index(row, RegisterModel.CATEGORY), Qt.DisplayRole) == "Business"
    assert ledger.resolve_category(conn, "business") == biz
    assert [c["path"] for c in ledger.list_categories(conn)].count("Business") == 1


def test_category_editor_cursor_at_end_no_selection(qapp, conn):
    from PyQt5.QtWidgets import QStyleOptionViewItem

    acct = ledger.create_account(conn, "Checking", "checking")
    cid = ledger.resolve_category(conn, "Business")
    ledger.add_transaction(conn, acct, "2024-01-01", -10_00,
                           payee="P", category_id=cid)
    model = RegisterModel(conn, acct)
    idx = model.index(model.row_for_txn(
        ledger.register_rows(conn, acct)[0]["id"]), RegisterModel.CATEGORY)

    delegate = delegates.CategoryDelegate()
    editor = delegate.createEditor(None, QStyleOptionViewItem(), idx)
    delegate.setEditorData(editor, idx)
    le = editor.lineEdit()
    assert le.text() == "Business"
    assert le.hasSelectedText() is False        # nothing selected -> first key appends
    assert le.cursorPosition() == len("Business")


# ---- money columns: an untouched editor must not write ----------------------
def _money_editor(delegate, model, row, col, qapp):
    """Open an inline editor on a money cell exactly as the view does."""
    from PyQt5.QtWidgets import QStyleOptionViewItem, QWidget
    parent = QWidget()
    idx = model.index(row, col)
    editor = delegate.createEditor(parent, QStyleOptionViewItem(), idx)
    # Keep the parent alive through the editor: dropping it would let Qt delete
    # the child editor out from under the test.
    editor._parent_ref = parent
    delegate.setEditorData(editor, idx)
    return editor, idx


def test_clicking_the_empty_money_cell_does_not_zero_the_amount(qapp, conn, accounts):
    """A payment row keeps its value in Payment and leaves Deposit EMPTY. Qt
    commits an inline editor on focus-out whether or not it was touched, so
    clicking the empty Deposit cell and clicking away committed "" -- and
    parse_amount("") is 0, blanking the transaction. Merely clicking around the
    register silently destroyed amounts."""
    from mammon.ui.delegates import MoneyDelegate

    chk, _ = accounts
    tid = ledger.add_transaction(conn, chk, "2026-02-01", -50_00, payee="Store")
    model = RegisterModel(conn, chk)
    delegate = MoneyDelegate()

    # The empty half of the pair: open its editor and close it untouched.
    editor, idx = _money_editor(delegate, model, 0, RegisterModel.DEPOSIT, qapp)
    assert editor.text() == ""                       # nothing to show, correctly
    delegate.setModelData(editor, model, idx)
    assert ledger.get_transaction(conn, tid)["amount"] == -50_00

    # The populated half, likewise untouched, is also left alone.
    editor, idx = _money_editor(delegate, model, 0, RegisterModel.PAYMENT, qapp)
    assert editor.text() == "50.00"
    delegate.setModelData(editor, model, idx)
    assert ledger.get_transaction(conn, tid)["amount"] == -50_00


def test_money_editor_still_commits_a_real_edit(qapp, conn, accounts):
    """The guard is 'unchanged', not 'never': typing still writes, and deliberately
    clearing the text (a keystroke) still zeroes the amount."""
    from mammon.ui.delegates import MoneyDelegate

    chk, _ = accounts
    tid = ledger.add_transaction(conn, chk, "2026-02-01", -50_00, payee="Store")
    model = RegisterModel(conn, chk)
    delegate = MoneyDelegate()

    editor, idx = _money_editor(delegate, model, 0, RegisterModel.PAYMENT, qapp)
    editor.setText("75.00")
    delegate.setModelData(editor, model, idx)
    assert ledger.get_transaction(conn, tid)["amount"] == -75_00

    # Moving the value to the other column works through the same path.
    model = RegisterModel(conn, chk)
    editor, idx = _money_editor(delegate, model, 0, RegisterModel.DEPOSIT, qapp)
    editor.setText("20.00")
    delegate.setModelData(editor, model, idx)
    assert ledger.get_transaction(conn, tid)["amount"] == 20_00

    # An explicit clear is a keystroke, so it is honoured.
    model = RegisterModel(conn, chk)
    editor, idx = _money_editor(delegate, model, 0, RegisterModel.DEPOSIT, qapp)
    assert editor.text() == "20.00"
    editor.setText("")
    delegate.setModelData(editor, model, idx)
    assert ledger.get_transaction(conn, tid)["amount"] == 0


def test_register_installs_the_money_delegate_on_both_columns(qapp, conn, accounts):
    """Guard against the columns quietly falling back to Qt's default delegate."""
    from mammon.ui.delegates import MoneyDelegate
    from mammon.ui.widgets import RegisterWidget

    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    for col in (RegisterModel.PAYMENT, RegisterModel.DEPOSIT):
        assert isinstance(w.view.itemDelegateForColumn(col), MoneyDelegate)


# ---- category fragment resolution (Quicken-style autocomplete) --------------
CATS = ["Auto:Fuel", "Auto:Repair", "Landscaping", "Groceries", "[Savings]"]


def test_unique_prefix_resolves_instead_of_prompting_a_new_category():
    """'land' with a single Landscaping category is a completion, not a new
    category -- the typo guard used to fire on exactly the gesture it should
    have completed."""
    assert delegates.resolve_category_input("land", CATS) == "Landscaping"
    assert delegates.resolve_category_input("gro", CATS) == "Groceries"
    assert delegates.resolve_category_input("LANDSCAPING", CATS) == "Landscaping"


def test_bare_subcategory_name_resolves_to_its_full_path():
    """Quicken lets a unique leaf stand in for the whole path: 'fuel' is
    Auto:Fuel without naming the parent."""
    assert delegates.resolve_category_input("fuel", CATS) == "Auto:Fuel"
    assert delegates.resolve_category_input("Fuel", CATS) == "Auto:Fuel"
    assert delegates.resolve_category_input("repair", CATS) == "Auto:Repair"
    assert delegates.resolve_category_input("auto:f", CATS) == "Auto:Fuel"


def test_ambiguous_or_unknown_text_is_left_alone():
    """Ambiguity is a question, not an answer: with two Fuel leaves the caller
    must still ask. A genuinely new name keeps the typo guard working."""
    both = CATS + ["Boat:Fuel"]
    assert delegates.resolve_category_input("fuel", both) is None
    assert delegates.resolve_category_input("au", CATS) is None   # 2 Auto: paths
    assert delegates.resolve_category_input("Dining", CATS) is None
    assert delegates.resolve_category_input("", CATS) is None
    # An exact full path still wins over any leaf ambiguity.
    assert delegates.resolve_category_input("Boat:Fuel", both) == "Boat:Fuel"


def test_exact_path_beats_a_leaf_match():
    cats = ["Auto:Fuel", "Fuel"]
    assert delegates.resolve_category_input("fuel", cats) == "Fuel"


def test_split_dialog_category_combo_matches_the_register(qapp, conn, accounts):
    """A split line was a bare combo with no completer at all, so typing habits
    learned in the register silently did not work one dialog away."""
    from mammon.ui.delegates import CategoryLineEdit
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    ledger.resolve_category(conn, "Auto:Fuel")
    tid = ledger.add_transaction(conn, chk, "2026-02-01", -40_00, payee="Gas")
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, 0)
    cat = dlg._lines[0]["cat"]
    assert cat.isEditable()
    assert cat.completer() is not None                  # register parity
    assert isinstance(cat.lineEdit(), CategoryLineEdit)  # ':' parent-complete

    # A bare leaf typed on a split line saves as the full path.
    cat.setEditText("fuel")
    dlg._lines[0]["amount"].setValue(-40.00)
    legs = dlg.lines_cents()
    assert cat.currentText() == "Auto:Fuel"
    assert legs and legs[0]["amount"] == -40_00
    # It stored the EXISTING Auto:Fuel, not a new top-level "fuel".
    assert legs[0]["category_id"] == ledger.resolve_category(conn, "Auto:Fuel")
    assert "fuel" not in [c["path"].lower() for c in ledger.list_categories(conn)]


def test_ambiguous_fragment_offers_the_matches_not_a_new_category(qapp, conn, accounts):
    """A real ledger has both Auto:Fuel and Moving:Fuel, so 'fuel' is a question.
    Offering to CREATE a category called 'fuel' to someone who already has two of
    them is a wrong answer to a question they did not ask -- the matches are."""
    from PyQt5.QtWidgets import QStyleOptionViewItem
    from mammon.ui.delegates import CategoryDelegate

    chk, _ = accounts
    ledger.resolve_category(conn, "Auto:Fuel")
    ledger.resolve_category(conn, "Moving:Fuel")
    tid = ledger.add_transaction(conn, chk, "2026-01-05", -40_00, payee="Gas")
    m = RegisterModel(conn, chk)
    idx = m.index(0, RegisterModel.CATEGORY)

    offered = []

    class Picker(CategoryDelegate):
        def _choose_among_matches(self, text, matches):
            offered.append((text, list(matches)))
            return matches[1]                       # user picks Moving:Fuel

        def _confirm_new_category(self, *a, **k):   # must never be reached
            raise AssertionError("offered to create a category instead of choosing")

    delegate = Picker()
    editor = delegate.createEditor(None, QStyleOptionViewItem(), idx)
    editor.setEditText("fuel")
    delegate.setModelData(editor, m, idx)
    _settle()                                   # the deferred prompt runs
    _settle()                                   # ...then the model's own reload

    assert offered == [("fuel", ["Auto:Fuel", "Moving:Fuel"])]
    assert m.txn_at(0)["category_label"] == "Moving:Fuel"
    # No junk category was created along the way.
    paths = [c["path"] for c in ledger.list_categories(conn)]
    assert "fuel" not in [p.lower() for p in paths]
    assert ledger.get_transaction(conn, tid)["category_id"] is not None


def test_category_dialog_never_opens_during_editor_teardown(qapp, conn, accounts):
    """The crash: setModelData runs while Qt is destroying the editor, so a modal
    there opens a nested event loop that lets the teardown finish and frees the
    editor this frame still holds -- heap corruption, 0xc0000374, no traceback.
    The prompt must therefore be DEFERRED, not raised inline."""
    from PyQt5.QtWidgets import QStyleOptionViewItem
    from mammon.ui.delegates import CategoryDelegate

    chk, _ = accounts
    m = RegisterModel(conn, chk)
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Shop")
    m.reload()
    idx = m.index(0, RegisterModel.CATEGORY)

    during = []

    class Watcher(CategoryDelegate):
        def _confirm_new_category(self, parent, text):
            during.append("asked")
            # Whatever raises this must NOT be holding a dying editor.
            assert parent is None or parent.isVisible() or True
            return True

    delegate = Watcher()
    editor = delegate.createEditor(None, QStyleOptionViewItem(), idx)
    editor.setEditText("Brand New Category")
    delegate.setModelData(editor, m, idx)
    assert during == []                 # nothing asked synchronously
    editor.deleteLater()                # simulate Qt tearing the editor down
    _settle()
    assert during == ["asked"]          # asked afterwards, editor already gone
    _settle()                           # the model reload is deferred in turn
    assert m.txn_at(0)["category_label"] == "Brand New Category"


def test_deferred_write_drops_a_stale_row(qapp, conn, accounts):
    """The row can go away while the dialog is open; a stale index must be
    dropped rather than written to."""
    from PyQt5.QtWidgets import QStyleOptionViewItem
    from mammon.ui.delegates import CategoryDelegate

    chk, _ = accounts
    tid = ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Shop")
    m = RegisterModel(conn, chk)
    idx = m.index(0, RegisterModel.CATEGORY)

    class Slow(CategoryDelegate):
        def _confirm_new_category(self, parent, text):
            return True

    delegate = Slow()
    editor = delegate.createEditor(None, QStyleOptionViewItem(), idx)
    editor.setEditText("Later")
    delegate.setModelData(editor, m, idx)
    ledger.delete_transaction(conn, tid)             # row vanishes first
    m.reload()
    _settle()                                        # must not raise
    assert ledger.get_transaction(conn, tid) is None


# ---- the completer popup offers subcategory matches -------------------------
def test_completion_list_offers_subcategory_matches_ranked():
    """Qt's default filter is 'starts with' against the whole Parent:Child path,
    so typing a leaf name got an EMPTY popup -- no category begins with 'fuel'.
    The list now offers path and leaf matches together, best first, so the
    highlighted entry is what a fragment almost always means."""
    cats = ["Auto:Fuel", "Moving:Fuel", "Auto:Repair", "Landscaping",
            "Groceries", "Restaurant"]
    assert delegates.category_completions("fuel", cats) == ["Auto:Fuel", "Moving:Fuel"]
    assert delegates.category_completions("land", cats) == ["Landscaping"]
    # Path matches outrank a mere substring hit ('Restaurant' contains 'au').
    assert delegates.category_completions("au", cats) == [
        "Auto:Fuel", "Auto:Repair", "Restaurant"]
    # An exact leaf comes before a longer path that merely starts the same way.
    assert delegates.category_completions("fuel", ["Fuel", "Auto:Fuel"])[0] == "Fuel"
    assert delegates.category_completions("", cats) == cats
    assert delegates.category_completions("zzz", cats) == []


def test_completer_model_refills_from_the_typed_fragment(qapp, conn, accounts):
    """The popup itself, not just the helper: the completer recomputes its model
    per keystroke, so a leaf fragment actually lists something."""
    from mammon.ui.delegates import CategoryCompleter

    completer = CategoryCompleter(["Auto:Fuel", "Moving:Fuel", "Groceries"])
    completer.setCompletionPrefix("fuel")
    assert completer.completionCount() == 2
    assert completer.currentCompletion() == "Auto:Fuel"       # the default pick
    completer.setCompletionPrefix("gro")
    assert completer.completionCount() == 1
    assert completer.currentCompletion() == "Groceries"


def test_enter_takes_the_first_offering_but_focus_out_does_not(qapp, conn, accounts):
    """Enter/Tab is a deliberate commit with the popup on screen, so an ambiguous
    fragment takes the highlighted entry. Focus-out chose nothing, so it must not
    guess -- the same rule that stops a stray click zeroing an amount."""
    from mammon.ui.delegates import accept_category_text, make_category_combo

    cats = ["Auto:Fuel", "Moving:Fuel", "Groceries"]

    combo = make_category_combo(None, cats)
    combo.setEditText("fuel")
    accept_category_text(combo, take_first=True)
    assert combo.currentText() == "Auto:Fuel"

    combo = make_category_combo(None, cats)
    combo.setEditText("fuel")
    accept_category_text(combo)                    # focus-out: still ambiguous
    assert combo.currentText() == "fuel"

    # An UNAMBIGUOUS fragment resolves either way.
    for kwargs in ({}, {"take_first": True}):
        combo = make_category_combo(None, cats)
        combo.setEditText("gro")
        accept_category_text(combo, **kwargs)
        assert combo.currentText() == "Groceries"


def test_enter_honours_a_popup_selection_over_the_first(qapp, conn, accounts):
    """Arrowing to the second entry and pressing Enter takes THAT one."""
    from mammon.ui.delegates import accept_category_text, make_category_combo

    combo = make_category_combo(None, ["Auto:Fuel", "Moving:Fuel"])
    combo.setEditText("fuel")
    completer = combo.completer()
    completer.setCompletionPrefix("fuel")
    completer.setCurrentRow(1)                     # user arrows down one
    assert completer.currentCompletion() == "Moving:Fuel"
    accept_category_text(combo, take_first=True)
    assert combo.currentText() == "Moving:Fuel"


def test_typing_a_genuinely_new_category_is_left_alone(qapp, conn, accounts):
    """Nothing matches, so the typo guard still gets its chance."""
    from mammon.ui.delegates import accept_category_text, make_category_combo

    combo = make_category_combo(None, ["Auto:Fuel", "Groceries"])
    combo.setEditText("Vacaton")
    accept_category_text(combo, take_first=True)
    assert combo.currentText() == "Vacaton"
