"""Item delegates for the register: a calendar date picker, a category combo
that also lists '[Account]' transfer targets, and the Payee delegate that
renders Quicken's two-line row (classification on an indented second line)."""
from __future__ import annotations

from PyQt5.QtCore import (QDate, QEvent, QPersistentModelIndex, QRect, QSize,
                          QStringListModel, Qt, QTimer)
from PyQt5.QtGui import QColor, QFont, QFontMetrics
from PyQt5.QtWidgets import (
    QComboBox, QCompleter, QDateEdit, QDoubleSpinBox, QHeaderView,
    QInputDialog, QLineEdit, QMessageBox, QStyledItemDelegate, QWidget,
)

from mammon.ui import style
from mammon.ui.models import RegisterModel


class NoWheelComboBox(QComboBox):
    """A combo that IGNORES mouse-wheel scrolling while collapsed.

    Hovering the wheel over a register/split category (or action) combo used to
    silently change its value -- dangerous while scrolling through split rows.
    Ignoring the wheel event lets it propagate to the parent view, so the
    register scrolls instead. The dropdown popup is a SEPARATE widget, so
    scrolling an OPEN dropdown still works; click/keyboard selection is
    unaffected."""

    def wheelEvent(self, event):  # noqa: N802 - Qt override name
        event.ignore()


def _accept_active_completion(editor) -> None:
    """Commit an editable combo's active completion into its text before a
    Tab-out reads ``currentText()``.

    In ``PopupCompletion`` mode a highlighted popup entry (or the top match for
    what was typed) is NOT copied into the line edit until the user presses
    Enter or clicks -- pressing Tab just dismisses the popup, so the *picked*
    category/transfer account is lost and the partial text is committed instead.
    On Tab we therefore accept the current completion first, but only when it
    genuinely completes what was typed (so a freshly typed new 'Parent:Child'
    path with no completion is left untouched)."""
    if isinstance(editor, QComboBox):
        current, put = editor.currentText, editor.setEditText
    elif isinstance(editor, QLineEdit) and not isinstance(editor.parent(), QComboBox):
        # A bare line edit with a completer: the payee cell's QuickFill editor.
        # (A combo's INNER line edit is left to the combo branch above.)
        current, put = editor.text, editor.setText
    else:
        return
    completer = editor.completer()
    if completer is None:
        return
    typed = current().strip()
    if not typed:
        return
    completion = completer.currentCompletion()
    if (completion and completion != current()
            and completion.lower().startswith(typed.lower())):
        put(completion)


def parent_autocomplete(typed: str, completion: str) -> str:
    """Text to show after ':' is pressed in a category field (feature parity).

    Typing a colon AUTOCOMPLETES the segment being typed from the completer's best
    full-path ``completion`` and appends the ':' so typing continues into the
    subcategory:  ``('bus', 'Business:Travel') -> 'Business:'`` and
    ``('Business:wri', 'Business:Writing') -> 'Business:Writing:'``. With no usable
    completion the typed segment is kept verbatim (``('xyz', '') -> 'xyz:'``), so
    a brand-new parent can still be created."""
    parts = typed.split(":")
    level = len(parts) - 1                     # index of the segment being typed
    comp_parts = completion.split(":") if completion else []
    if (completion and completion.lower().startswith(typed.lower())
            and len(comp_parts) > level):
        seg = comp_parts[level]                # autocomplete this level from the match
    else:
        seg = parts[level]                     # nothing to complete -> keep as typed
    return ":".join(parts[:level] + [seg]) + ":"


class CategoryLineEdit(QLineEdit):
    """The category combo's line edit: pressing ':' autocompletes the current
    parent segment then inserts the ':' so typing flows into the subcategory
    (see :func:`parent_autocomplete`). Every other key behaves normally."""

    def __init__(self, combo, parent=None):
        super().__init__(parent)
        self._combo = combo

    def focusInEvent(self, event):  # noqa: N802 - Qt override name
        # Qt gives a keystroke/Tab-opened editor focus via a programmatic
        # setFocus(), and QLineEdit's default focus-in SELECTS ALL its text, so the
        # first key would REPLACE the field. Cancel that: leave the cursor at the
        # end with no selection so the first key APPENDS. The click-to-edit path
        # re-selects explicitly afterwards (RegisterWidget._edit_cell), so
        # clicking a category still replaces on first key.
        super().focusInEvent(event)
        self.deselect()
        self.end(False)

    def keyPressEvent(self, event):  # noqa: N802 - Qt override name
        if (event.text() == ":"
                and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier))):
            comp = ""
            completer = self._combo.completer() if self._combo is not None else None
            if completer is not None:
                completer.setCompletionPrefix(self.text())
                comp = completer.currentCompletion()
            self.setText(parent_autocomplete(self.text(), comp))
            self.end(False)                    # cursor after the ':' to keep typing
            return
        super().keyPressEvent(event)


def make_date_edit(parent=None, iso: str = "", *, blank_ok: bool = False):
    """The application's date entry widget: typeable AND calendar-pickable, shown
    in the user's chosen format.

    Every date field in the app comes from here so one preference governs them
    all. Before this each site hardcoded its own answer -- the register editor
    said MM/dd/yyyy while the report filters, the scheduled-payments dialog and
    the reconcile setup all said yyyy-MM-dd, and three more dialogs took dates as
    free text that only accepted ISO. Choosing DD/MM/YYYY changed the register
    and nothing else.

    A QDateEdit gives both entry routes at once (type over the fields, or drop
    the calendar) and cannot hold a non-date, so nothing downstream has to defend
    against one. ``blank_ok`` allows an empty value via a sentinel minimum date,
    for the few optional dates.
    """
    from mammon.ui.models import qt_date_format
    edit = QDateEdit(parent) if parent is not None else QDateEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat(qt_date_format())
    if blank_ok:
        # Qt has no empty state, so the minimum doubles as "unset" and renders
        # as such. Callers read it back through date_edit_iso().
        edit.setMinimumDate(QDate(1752, 9, 14))
        edit.setSpecialValueText(" ")
    d = QDate.fromString((iso or "").strip(), "yyyy-MM-dd")
    edit.setDate(d if d.isValid() else QDate.currentDate())
    return edit


def date_edit_iso(edit) -> str:
    """A date editor's value as ISO ``YYYY-MM-DD`` -- "" for a blank_ok editor
    left unset. Storage, the ledger validators and download scripts all speak
    ISO, so the conversion happens here rather than at each call site."""
    d = edit.date()
    if not d.isValid() or (edit.specialValueText() and d == edit.minimumDate()):
        return ""
    return d.toString("yyyy-MM-dd")


def refresh_date_format(root) -> int:
    """Re-apply the current date-format preference to every date editor under
    ``root``. Long-lived dialogs and filter bars are built once, so changing the
    preference has to reach the widgets already on screen. Returns how many were
    updated."""
    from mammon.ui.models import qt_date_format
    fmt = qt_date_format()
    edits = root.findChildren(QDateEdit)
    for e in edits:
        e.setDisplayFormat(fmt)
    return len(edits)


def _choices_for(model, row=None):
    """Category picker choices from ``model``, ranked for ``row`` when it can be.

    Older/other models expose a no-argument ``category_choices``; asking them for
    a ranked list must not break the editor, so the row argument is dropped on a
    TypeError rather than guarded by isinstance checks.
    """
    if not hasattr(model, "category_choices"):
        return []
    if row is None:
        return model.category_choices()
    try:
        return model.category_choices(row)
    except TypeError:
        return model.category_choices()


def make_category_combo(parent, choices):
    """Build the register's editable category combo, shared by the one-line
    :class:`CategoryDelegate` and the two-line :class:`PayeeTwoLineDelegate`.

    A blank first item, the live category/transfer choices, a case-insensitive
    popup completer, and a :class:`CategoryLineEdit` for the ':' parent-complete
    gesture."""
    combo = NoWheelComboBox(parent)
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    combo.setLineEdit(CategoryLineEdit(combo, combo))
    combo.addItem("")
    combo.addItems(list(choices))
    # Matches on the path AND on the subcategory name, so typing a leaf finds it.
    combo.setCompleter(CategoryCompleter(list(choices), combo))
    return combo


def resolve_category_input(typed: str, choices):
    """Resolve what the user typed to an EXISTING category path, or None.

    Quicken completes a category from any unambiguous fragment, and typing an
    unambiguous fragment is the normal way to enter one. Without this, "land"
    with a single Landscaping category was treated as a brand-new category and
    prompted "Create new category 'land'?" -- the typo guard firing on the exact
    gesture it should have completed.

    Resolution order, each step requiring a UNIQUE winner:

    1. the full path, exactly (case-insensitive)
    2. the LEAF name, exactly -- "fuel" -> "Auto:Fuel", so a subcategory can be
       reached without naming its parent
    3. the full path by prefix -- "land" -> "Landscaping"
    4. the leaf name by prefix -- "fu" -> "Auto:Fuel"

    Ambiguity returns None rather than guessing: with both Auto:Fuel and
    Boat:Fuel, "fuel" is a question, not an answer, and the caller falls back to
    asking. None also covers a genuinely new category, which is what keeps the
    typo guard working for the case it was built for.
    """
    matches = category_matches(typed, choices)
    return matches[0] if len(matches) == 1 else None


def category_completions(typed: str, choices) -> list:
    """Everything worth OFFERING for a fragment, best first.

    Where :func:`category_matches` returns a single tier (it has to decide
    whether the fragment is unambiguous), this returns the ordered UNION across
    tiers, because a dropdown is not making a decision -- it is showing the user
    what their fragment could mean so they can make one. Typing "fuel" lists
    Auto:Fuel and Moving:Fuel rather than nothing, which is what a completer
    keyed only on the start of the full path did: no category path BEGINS with
    "fuel", so the popup stayed empty and the subcategory was unreachable by its
    own name.

    Order is what makes "just press Enter" work: the best tier comes first, so
    the highlighted entry is the one a user typing a fragment almost always
    means. An empty fragment offers everything.
    """
    text = (typed or "").strip()
    opts = [c for c in choices if c]
    if not text:
        return opts
    low = text.lower()
    leaf = lambda c: c.rsplit(":", 1)[-1]
    ordered = []
    for tier in (
        [c for c in opts if c.lower() == low],
        [c for c in opts if leaf(c).lower() == low],
        [c for c in opts if c.lower().startswith(low)],
        [c for c in opts if leaf(c).lower().startswith(low)],
        [c for c in opts if low in c.lower()],
    ):
        ordered.extend(tier)
    return list(dict.fromkeys(ordered))


class CategoryCompleter(QCompleter):
    """A completer that also matches a category by its SUBCATEGORY name.

    Qt's default filter is "starts with", tested against the whole
    ``Parent:Child`` path, so a user typing a leaf name got an empty popup --
    nothing begins with "fuel". Recomputing the completion model from
    :func:`category_completions` on each keystroke offers path matches and leaf
    matches together, ranked, so the fragment resolves through the UI the user is
    already looking at instead of through a dialog after the fact.

    ``splitPath`` is the hook Qt calls with the current text; returning [""]
    after refilling the model means "offer everything in it".
    """

    def __init__(self, choices, parent=None):
        self._choices = [c for c in choices if c]
        self._list = QStringListModel(list(self._choices), parent)
        super().__init__(self._list, parent)
        self.setCompletionMode(QCompleter.PopupCompletion)
        self.setCaseSensitivity(Qt.CaseInsensitive)

    def set_choices(self, choices) -> None:
        self._choices = [c for c in choices if c]

    def splitPath(self, path):          # noqa: N802 - Qt override name
        self._list.setStringList(category_completions(path, self._choices))
        return [""]


def payee_completions(typed: str, choices) -> list:
    """Payees to offer for a typed fragment: names that START with it first, in
    the order given (most recently used first), then names that merely contain
    it. Empty for an empty fragment, so the popup never opens on a blank cell."""
    text = (typed or "").strip().lower()
    if not text:
        return []
    opts = [c for c in choices if c]
    starts = [c for c in opts if c.lower().startswith(text)]
    contains = [c for c in opts if text in c.lower() and not c.lower().startswith(text)]
    return list(dict.fromkeys(starts + contains))


class PayeeCompleter(QCompleter):
    """QuickFill's payee completer. Ranks by recency (the order
    :func:`ledger.list_payees` returns) and also matches INSIDE the name, so
    ``costco`` finds ``COSTCO WHSE #0001`` and ``Costco Gas`` alike -- bank
    descriptors rarely begin with the word a person remembers. Same
    refill-on-keystroke mechanism as :class:`CategoryCompleter`."""

    def __init__(self, choices, parent=None):
        self._choices = [c for c in choices if c]
        self._list = QStringListModel(list(self._choices), parent)
        super().__init__(self._list, parent)
        self.setCompletionMode(QCompleter.PopupCompletion)
        self.setCaseSensitivity(Qt.CaseInsensitive)

    def set_choices(self, choices) -> None:
        self._choices = [c for c in choices if c]

    def splitPath(self, path):          # noqa: N802 - Qt override name
        self._list.setStringList(payee_completions(path, self._choices))
        return [""]


def category_matches(typed: str, choices) -> list:
    """Every existing category the typed fragment could mean, best tier first.

    Returns [] for a fragment that matches nothing (a genuinely new category),
    one entry when it is unambiguous, and several when the user has to choose --
    "fuel" is both Auto:Fuel and Moving:Fuel in a real ledger. That third case is
    why this exists separately from :func:`resolve_category_input`: offering
    "create a new category called fuel" to someone who has two of them is a
    wrong answer to a question they did not ask.
    """
    text = (typed or "").strip()
    if not text:
        return []
    opts = [c for c in choices if c]
    low = text.lower()
    leaf = lambda c: c.rsplit(":", 1)[-1]
    for tier in (
        [c for c in opts if c.lower() == low],
        [c for c in opts if leaf(c).lower() == low],
        [c for c in opts if c.lower().startswith(low)],
        [c for c in opts if leaf(c).lower().startswith(low)],
    ):
        uniq = list(dict.fromkeys(tier))
        if uniq:
            return uniq
    return []


def combo_choices(combo) -> list:
    """The category/transfer paths an editable combo was populated with."""
    return [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i)]


def accept_category_text(editor, *, take_first: bool = False) -> None:
    """Replace an editable category combo's text with the category it names.

    ``take_first`` is the difference between a DELIBERATE commit and an
    incidental one. On Tab/Enter the user is looking at the popup and means the
    highlighted entry, so an ambiguous fragment takes the first offering --
    typing "fuel" and pressing Enter gets Auto:Fuel. On focus-out nobody chose
    anything, so only an UNAMBIGUOUS fragment is accepted; guessing there would
    categorize a transaction because a click landed elsewhere, the same way the
    money columns used to zero an amount (see MoneyDelegate).
    """
    if not isinstance(editor, QComboBox):
        return
    text = editor.currentText().strip()
    if not text:
        return
    choices = combo_choices(editor)
    if take_first:
        offered = category_completions(text, choices)
        completer = editor.completer()
        # Honour what the popup is showing: the entry the user arrowed to when it
        # is still one of the current offerings, otherwise the top-ranked one.
        picked = completer.currentCompletion() if completer is not None else ""
        if picked not in offered:
            picked = offered[0] if offered else ""
        if picked and picked != editor.currentText():
            editor.setEditText(picked)
        if picked:
            return
    hit = resolve_category_input(text, choices)
    if hit is not None and hit != editor.currentText():
        editor.setEditText(hit)


def select_editor_for_append(editor) -> None:
    """Put a fresh combo/line-edit editor's cursor at the END with no selection,
    so the FIRST key typed after TAB-ing into the field APPENDS instead of
    replacing the whole value. The click path re-selects all afterwards
    (RegisterWidget._edit_cell), so click-to-edit still replaces on first key."""
    le = editor.lineEdit() if isinstance(editor, QComboBox) else editor
    if isinstance(le, QLineEdit):
        le.deselect()
        le.end(False)


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """A spin box that IGNORES mouse-wheel scrolling, so scrolling over a split
    row's amount field can no longer silently change the amount. Typing and the
    up/down arrows still work."""

    def wheelEvent(self, event):  # noqa: N802 - Qt override name
        event.ignore()

# Category / Memo / Tag take these fractions of the classification strip; Tag
# gets the remainder so the three boxes ALWAYS fill the full width edge-to-edge.
_CAT_FRACTION = 0.46
_MEMO_FRACTION = 0.32


def classification_zones(rect):
    """Split a rectangle into three CONTIGUOUS, edge-to-edge boxes -- Category,
    Memo, Tag -- that together fill its full width. Shared by the two-line Payee
    delegate (each row's second line) and the two-line header, so the header
    boxes sit directly above the row boxes and the whole strip fills the Payee
    column (by request)."""
    w = rect.width()
    cat_w = int(w * _CAT_FRACTION)
    memo_w = int(w * _MEMO_FRACTION)
    cat = QRect(rect.left(), rect.top(), cat_w, rect.height())
    memo = QRect(rect.left() + cat_w, rect.top(), memo_w, rect.height())
    tag = QRect(rect.left() + cat_w + memo_w, rect.top(),
                w - cat_w - memo_w, rect.height())
    return {"category": cat, "memo": memo, "tag": tag}


class DateDelegate(QStyledItemDelegate):
    """Edit a date cell with a calendar popup, in the user's chosen date format.
    The cell DISPLAYS that format while the model stores/edits ISO YYYY-MM-DD, so
    the editor reads EditRole and writes a QDate back."""

    def createEditor(self, parent, option, index):
        return make_date_edit(parent)

    def setEditorData(self, editor, index):
        txt = str(index.data(Qt.EditRole) or "")
        d = QDate.fromString(txt, "yyyy-MM-dd")
        editor.setDate(d if d.isValid() else QDate.currentDate())

    def setModelData(self, editor, model, index):
        model.setData(index, editor.date(), Qt.EditRole)


class MoneyDelegate(QStyledItemDelegate):
    """Payment/Deposit editor that writes only when the text actually CHANGED.

    Qt commits an inline editor on focus-out whether or not the user touched it.
    For a text column that is harmless -- it rewrites the same string. For the
    money pair it destroyed data: a payment row carries its value in Payment and
    leaves Deposit EMPTY, so clicking the empty Deposit cell opened an editor
    seeded with "", and clicking anywhere else committed that "" back.
    ``parse_amount("")`` is 0, so the transaction's amount became zero -- after
    which BOTH money cells render empty and the amount looks simply gone. Merely
    clicking around a register silently blanked transactions.

    The rule this restores: without a keystroke, the value does not change. An
    untouched editor writes nothing at all, which also spares the row a pointless
    update and the balance checkpoints a pointless recompute. Clearing an amount
    on purpose still works -- deleting the text is a keystroke, so the text
    differs and the write goes through.

    The original is kept as a Qt property rather than a Python attribute so it
    cannot be lost to a re-wrapped editor instance.
    """

    _ORIGINAL = "mammonOriginalText"

    def setEditorData(self, editor, index):
        super().setEditorData(editor, index)
        if isinstance(editor, QLineEdit):
            editor.setProperty(self._ORIGINAL, editor.text())

    def setModelData(self, editor, model, index):
        if isinstance(editor, QLineEdit):
            original = editor.property(self._ORIGINAL)
            # `original` is None only if setEditorData never ran; then fall
            # through and commit, preserving the old behaviour rather than
            # silently swallowing a real edit.
            if original is not None and editor.text() == original:
                return
        super().setModelData(editor, model, index)


class CategoryDelegate(QStyledItemDelegate):
    """Editable combo of category paths plus '[Account]' transfer targets.

    Choices are pulled live from the model (``category_choices``) so a newly
    created account/category shows up without rebuilding the delegate. The combo
    is editable, so typing a new 'Parent:Child' path creates it on commit."""

    def createEditor(self, parent, option, index):
        model = index.model()
        # Pass the row so the model can promote this payee's own categories to
        # the top of the list (see RegisterModel.category_choices).
        choices = _choices_for(model, index.row())
        return make_category_combo(parent, choices)

    def setEditorData(self, editor, index):
        txt = str(index.data(Qt.DisplayRole) or "")
        i = editor.findText(txt)
        if i >= 0:
            editor.setCurrentIndex(i)
        else:
            editor.setEditText(txt)
        # Cursor to the end (no selection) so a TAB-into-field first keystroke
        # APPENDS; the click path re-selects all afterwards.
        select_editor_for_append(editor)

    def setModelData(self, editor, model, index):
        # An unambiguous fragment names an existing category, so resolve it
        # BEFORE the typo guard runs -- otherwise typing "land" for the sole
        # Landscaping category asked to create a category called "land".
        accept_category_text(editor)
        text = editor.currentText().strip()
        if text and self._is_new_category(model, text):
            # ANY dialog here has to be DEFERRED. setModelData runs while Qt is
            # tearing the editor down (_commit_open_editor emits commitData then
            # closeEditor), and a modal opens a nested event loop that lets that
            # teardown finish -- destroying the editor this frame still holds.
            # Parenting the box to the editor made it certain. The result was a
            # silent heap corruption, 0xc0000374, with no Python traceback:
            # exactly the failure RegisterModel._write defers reloads to avoid.
            self._defer_resolution(model, index, text)
            return
        model.setData(index, editor.currentText(), Qt.EditRole)

    def _defer_resolution(self, model, index, text) -> None:
        """Ask about ``text`` on the NEXT event-loop turn, once the editor is
        gone, then write through a persistent index."""
        persistent = QPersistentModelIndex(index)
        QTimer.singleShot(
            0, lambda: self._resolve_unknown(model, persistent, text))

    def _resolve_unknown(self, model, persistent, text) -> None:
        """Off the teardown path: either pick among existing matches or confirm
        creating a new category, then write. A stale index (the row went away
        while the dialog was open) is dropped rather than written to."""
        if not persistent.isValid():
            return
        index = model.index(persistent.row(), persistent.column())
        choices = model.category_choices() if hasattr(model, "category_choices") else []
        matches = category_matches(text, choices)
        if len(matches) > 1:
            chosen = self._choose_among_matches(text, matches)
        elif matches:
            chosen = matches[0]
        else:
            chosen = text if self._confirm_new_category(self._dialog_parent(), text) else None
        if chosen:
            model.setData(index, chosen, Qt.EditRole)

    def _dialog_parent(self):
        """The view this delegate serves -- a live widget, unlike the editor Qt
        is in the middle of destroying."""
        parent = self.parent()
        return parent if isinstance(parent, QWidget) else None

    def _choose_among_matches(self, text, matches):
        """Ask which of several existing categories the fragment meant. Returns
        the chosen path, or None if the user cancelled. Overridable so headless
        tests never open a modal (see CLAUDE.md's headless-modal hazard)."""
        chosen, ok = QInputDialog.getItem(
            self._dialog_parent(), "Which category?",
            f'"{text}" matches more than one category:',
            matches, 0, False)
        return chosen if ok else None

    @staticmethod
    def _is_new_category(model, text) -> bool:
        """True when ``text`` names a category that does not yet exist (so
        committing it would CREATE one). Transfer targets ('[Account]') and
        already-known category paths are not new."""
        if hasattr(model, "transfer_target") and model.transfer_target(text) is not None:
            return False
        choices = model.category_choices() if hasattr(model, "category_choices") else []
        existing = {c.lower() for c in choices
                    if not (c.startswith("[") and c.endswith("]"))}
        return text.lower() not in existing

    @staticmethod
    def _confirm_new_category(parent, text) -> bool:  # noqa: D401 - see caller
        """Ask before creating a brand-new category (typo guard). Returns True to
        proceed with creation, False to cancel and keep the field unchanged."""
        reply = QMessageBox.question(
            parent, "Create new category?",
            f'Create new category "{text}"?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        return reply == QMessageBox.Yes

    def eventFilter(self, editor, event):
        # Tab/Backtab OR Enter/Return out of the combo must SAVE the picked
        # category/transfer account, not the half-typed prefix behind the
        # completer popup. Enter used to revert the value unless the user first
        # clicked another cell (commit-on-Enter bug), so it now accepts the
        # active completion just like Tab before the base class commits + closes.
        if (event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Tab, Qt.Key_Backtab,
                                    Qt.Key_Enter, Qt.Key_Return)):
            _accept_active_completion(editor)
            # ...then take what the popup is offering. Enter/Tab is a deliberate
            # commit with the choices on screen, so a fragment matching several
            # categories resolves to the highlighted one rather than raising a
            # dialog about text the user can already see disambiguated.
            accept_category_text(editor, take_first=True)
        return super().eventFilter(editor, event)


class PayeeTwoLineDelegate(QStyledItemDelegate):
    """The Payee-column delegate for Quicken's TWO-LINE register.

    Line 1 top-aligns the payee. Line 2, indented under the payee, holds the
    transaction's classification split into three aligned, elided zones --
    Category | Memo | Tag -- and each zone is GENUINELY EDITABLE IN PLACE:
    clicking a zone opens the matching editor (an editable category combo, or a
    line edit for memo/tag) and the commit is routed to THAT underlying column,
    not the payee. This fixes the user's three complaints with the Task-23 layout:
    (a) the header now labels both lines (RegisterModel.headerData), (b) line 2
    is real editing rather than painted text that fell through to the payee, and
    (c) the fields sit in three BOXED, edge-to-edge zones that together fill the
    Payee column (matching boxes in the header), instead of sprawling. In
    one-line mode it is the plain default delegate, so the toggle is a no-op
    there."""

    # Box border + muted classification text read the ACTIVE theme palette at
    # paint time (style.line_color()/muted_color()), so line 2 follows dark mode.
    TEXT_PAD = 4                # px inset for text inside each box

    # Which underlying model column each line-2 zone reads/writes.
    _COL = {"category": RegisterModel.CATEGORY,
            "memo": RegisterModel.MEMO,
            "tag": RegisterModel.TAG}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.two_line = False
        # The line-2 field the NEXT editor targets (set from the click position
        # in editorEvent). None means the payee (line 1) -- the default.
        self._active_field = None

    def set_two_line(self, on: bool) -> None:
        self.two_line = bool(on)

    # ---- geometry (also used by tests for the boxed-zone alignment) -------
    def _line_rects(self, rect):
        """Split a payee cell into its top (payee) and bottom (classification)
        halves. The classification strip spans the FULL width of the payee cell
        so its three boxes fill the row over the extent of the Payee field."""
        half = rect.height() // 2
        top = QRect(rect.left(), rect.top(), rect.width(), half)
        bottom = QRect(rect.left(), rect.top() + half,
                       rect.width(), rect.height() - half)
        return top, bottom

    def second_line_rects(self, rect):
        """The three line-2 field boxes (category, memo, tag) -- contiguous and
        edge-to-edge across the full width of the payee cell (matching the
        header boxes above them)."""
        _, bottom = self._line_rects(rect)
        return classification_zones(bottom)

    def field_at(self, rect, pos):
        """The line-2 field under a point, or None when the point is on line 1
        (the payee). Drives which editor a click opens."""
        if not self.two_line:
            return None
        _, bottom = self._line_rects(rect)
        if not bottom.contains(pos):
            return None
        for name, r in self.second_line_rects(rect).items():
            if r.contains(pos):
                return name
        return "category"          # a gap between zones -> nearest is category

    # ---- painting --------------------------------------------------------
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if self.two_line:
            # keep the payee on the UPPER line so the second line has room below
            option.displayAlignment = Qt.AlignLeft | Qt.AlignTop

    def sizeHint(self, option, index):
        sh = super().sizeHint(option, index)
        if self.two_line:
            return QSize(sh.width(), sh.height() * 2 + 8)
        return sh

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        model = index.model()
        if not self.two_line or model is None:
            return
        # The blank quick-entry row has no transaction, so no second line.
        if hasattr(model, "is_blank_row") and model.is_blank_row(index.row()):
            return
        font = QFont(option.font)
        pt = font.pointSizeF()
        if pt > 0:
            font.setPointSizeF(max(7.0, pt - 1.0))
        fm = QFontMetrics(font)
        painter.save()
        painter.setFont(font)
        for field, r in self.second_line_rects(option.rect).items():
            if r.width() <= 0:
                continue
            # a box around every field (filled or not) -- the user's request
            painter.setPen(QColor(style.line_color()))
            painter.drawRect(r.adjusted(0, 0, -1, -1))
            text = str(index.sibling(index.row(), self._COL[field]).data(Qt.DisplayRole) or "")
            if field == "tag" and text:
                text = f"#{text}"
            if not text:
                continue
            inner = r.adjusted(self.TEXT_PAD, 0, -self.TEXT_PAD, 0)
            painter.setPen(QColor(style.muted_color()))
            painter.drawText(inner, int(Qt.AlignLeft | Qt.AlignVCenter),
                             fm.elidedText(text, Qt.ElideRight, max(0, inner.width())))
        painter.restore()

    # ---- editing (genuine, routed to the right line-2 field) -------------
    def _classification_locked(self, index) -> bool:
        """A split's category is '--Split--' (managed in the split dialog) and a
        split-transfer / one-sided mirror leg can't be re-pointed inline, so
        line-2 category editing is suppressed for them. A PLAIN two-sided
        transfer's category names the linked account and CAN be re-pointed here
        (picking another '[Account]' rewires the mirror). Memo/tag stay editable
        regardless."""
        model = index.model()
        txn = model.txn_at(index.row()) if hasattr(model, "txn_at") else None
        if not txn:
            return True
        if txn.get("is_split"):
            return True
        if txn["transfer_account_id"] is not None:
            return txn["transfer_pair_id"] is None  # one-sided leg: not retargetable
        return False

    def editorEvent(self, event, model, option, index):
        if self.two_line and event.type() in (
                QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            if hasattr(model, "is_blank_row") and model.is_blank_row(index.row()):
                self._active_field = None
            else:
                self._active_field = self.field_at(option.rect, event.pos())
        return super().editorEvent(event, model, option, index)

    def createEditor(self, parent, option, index):
        field = self._active_field if self.two_line else None
        if field == "category":
            if self._classification_locked(index):
                return None            # transfers/splits are not inline-recategorized
            model = index.model()
            choices = _choices_for(model, index.row())
            return make_category_combo(parent, choices)
        if field in ("memo", "tag"):
            return QLineEdit(parent)
        # Payee (line 1): a typeable dropdown seeded with the rename tree's
        # candidate payees when editing a pending review row (pick one or type a
        # brand-new name). Falls back to a plain line edit otherwise.
        combo = self._payee_combo(parent, index)
        if combo is not None:
            return combo
        # QuickFill: every other payee edit gets a completer over the payees the
        # register has used, most recent first (RegisterModel.payee_choices).
        editor = QLineEdit(parent)
        model = index.model()
        choices = model.payee_choices() if hasattr(model, "payee_choices") else []
        if choices:
            editor.setCompleter(PayeeCompleter(choices, editor))
        return editor

    def _payee_combo(self, parent, index):
        """Editable combo of suggested payees for the pending row, or ``None``.

        Returns a :class:`NoWheelComboBox` pre-loaded with the rename tree's
        dropdown candidates (``MappedRow.payee_candidates``) so an unsure
        suggestion becomes a typeable pick list; returns ``None`` (=> a plain
        line edit) for ordinary rows or when there is nothing to offer.
        """
        model = index.model()
        if not (hasattr(model, "is_pending_row") and model.is_pending_row(index.row())):
            return None
        entry = model.pending_entry() if hasattr(model, "pending_entry") else None
        cands = list(getattr(entry.mapped, "payee_candidates", []) or []) if entry else []
        cands = [c for c in cands if c]
        if not cands:
            return None
        combo = NoWheelComboBox(parent)
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        combo.addItems(cands)
        completer = combo.completer()
        if completer is not None:
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
        return combo

    def setEditorData(self, editor, index):
        field = self._active_field if self.two_line else None
        if field in self._COL:
            txt = str(index.sibling(index.row(), self._COL[field]).data(Qt.EditRole) or "")
            if isinstance(editor, QComboBox):
                i = editor.findText(txt)
                if i >= 0:
                    editor.setCurrentIndex(i)
                else:
                    editor.setEditText(txt)
            elif isinstance(editor, QLineEdit):
                editor.setText(txt)
            # First key after TAB-into-field appends, not replaces (issue parity
            # with the one-line CategoryDelegate).
            select_editor_for_append(editor)
            return
        if isinstance(editor, QComboBox):
            # Payee typeable combo: seed it with the cell's current payee text.
            txt = str(index.data(Qt.EditRole) or "")
            i = editor.findText(txt)
            if i >= 0:
                editor.setCurrentIndex(i)
            else:
                editor.setEditText(txt)
            return
        super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        field = self._active_field if self.two_line else None
        if field in self._COL:
            if isinstance(editor, QComboBox):
                # Same fragment resolution as the one-line CategoryDelegate, so
                # the two register layouts accept identical typing.
                if field == "category":
                    accept_category_text(editor)
                text = editor.currentText()
            elif isinstance(editor, QLineEdit):
                text = editor.text()
            else:
                text = ""
            model.setData(index.sibling(index.row(), self._COL[field]), text, Qt.EditRole)
            self._active_field = None
            return
        if isinstance(editor, QComboBox):
            # Payee typeable combo commits straight to the payee cell.
            _accept_active_completion(editor)
            model.setData(index, editor.currentText().strip(), Qt.EditRole)
            return
        super().setModelData(editor, model, index)

    def eventFilter(self, editor, event):
        # Same Tab-out fix as CategoryDelegate for the line-2 category combo:
        # accept the picked completion before the value is committed.
        if (event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)):
            _accept_active_completion(editor)
        return super().eventFilter(editor, event)

    def updateEditorGeometry(self, editor, option, index):
        if self.two_line:
            if self._active_field in self._COL:
                editor.setGeometry(self.second_line_rects(option.rect)[self._active_field])
                return
            top, _ = self._line_rects(option.rect)   # payee edits stay on line 1
            editor.setGeometry(top)
            return
        super().updateEditorGeometry(editor, option, index)

    def destroyEditor(self, editor, index):
        # Reset so a later keyboard edit (no mouse position) defaults to the
        # payee even if the previous editor was cancelled on a line-2 field.
        self._active_field = None
        super().destroyEditor(editor, index)


class TwoLineHeaderView(QHeaderView):
    """The register's horizontal header. In two-line mode it custom-paints the
    Payee section as two rows: 'Payee' on top, and three BOXED labels --
    Category | Memo | Tag -- beneath, filling the column width and aligned
    directly above each row's second-line boxes (by request). Every other
    section, and all of one-line mode, uses the default themed painting."""

    LABELS = {"category": "Category", "memo": "Memo", "tag": "Tag"}

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.two_line = False
        self.payee_section = RegisterModel.PAYEE
        self.setSectionsClickable(True)

    def set_two_line(self, on: bool) -> None:
        self.two_line = bool(on)
        self.viewport().update()

    def classification_boxes(self, section_rect):
        """The three header field boxes for a Payee section rect -- the SAME
        geometry the delegate uses for a row's second line, so they line up."""
        half = section_rect.height() // 2
        bottom = QRect(section_rect.left(), section_rect.top() + half,
                       section_rect.width(), section_rect.height() - half)
        return classification_zones(bottom)

    def paintSection(self, painter, rect, logicalIndex):
        if not (self.two_line and logicalIndex == self.payee_section):
            super().paintSection(painter, rect, logicalIndex)
            return
        painter.save()
        # themed background + the section separators the QSS would have drawn;
        # colors read the ACTIVE theme palette so the header follows dark mode.
        header_bg = QColor(style.header_bg_color())
        line = QColor(style.line_color())
        text = QColor(style.text_color())
        painter.fillRect(rect, header_bg)
        painter.setPen(line)
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.drawLine(rect.topRight(), rect.bottomRight())
        # top row: the 'Payee' label
        half = rect.height() // 2
        top = QRect(rect.left(), rect.top(), rect.width(), half)
        painter.setPen(text)
        painter.drawText(top.adjusted(6, 0, -4, 0),
                         int(Qt.AlignLeft | Qt.AlignVCenter), "Payee")
        # bottom row: three boxed classification labels
        small = QFont(self.font())
        pt = small.pointSizeF()
        if pt > 0:
            small.setPointSizeF(max(7.0, pt - 1.0))
        painter.setFont(small)
        for field, r in self.classification_boxes(rect).items():
            if r.width() <= 0:
                continue
            painter.setPen(line)
            painter.drawRect(r.adjusted(0, 0, -1, -1))
            painter.setPen(text)
            painter.drawText(r.adjusted(4, 0, -3, 0),
                             int(Qt.AlignLeft | Qt.AlignVCenter), self.LABELS[field])
        painter.restore()
