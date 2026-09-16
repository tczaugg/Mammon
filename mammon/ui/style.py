"""classic-desktop-flavored theme for the Mammon desktop UI.

A single QSS builder plus a helper that also sets the application font. Kept
separate from the widgets so the look can be tuned in one place. Colors and the
narrow blue account bar / alternating register shading mimic the classic desktop ledger.

Two THEMES are supported: the default LIGHT palette (the original classic look)
and a DARK palette (dark background, light foreground). apply_theme() resolves
the palette from the user's Display Preferences, so switching themes recolors the
whole app live; the LIGHT palette is byte-for-byte the original look, so nothing
changes out of the box.
"""
from __future__ import annotations

import sys

# Palette (approximate the classic desktop ledger) -- the LIGHT theme's named constants,
# kept as module-level names for backward compatibility (tests + callers read
# style.BLUE / style.RED / style.ALT_ROW).
BLUE = "#1f5fa8"        # account names, net worth
SLATE = "#33475b"       # titles, group headers, column header text
RED = "#c0392b"         # negative balances
ALT_ROW = "#f4f6f9"     # alternating register row shading
HEADER_BG = "#eef1f5"   # column header + net-worth strip
BAR_BG = "#f5f7fa"      # account bar background
LINE = "#d4d9e0"        # separators / borders
SELECT = "#cfe0f2"      # selection highlight

# The default register font, PER PLATFORM. The look this UI is after is the
# small system sans-serif of a classic desktop ledger -- which is a different
# face and a different size on each OS. Hardcoding "Segoe UI" at 9pt gave macOS
# an unavailable font at a size two points below anything native, and Linux
# whatever the fontconfig fallback happened to be. Each entry is that platform's
# own UI font at its own conventional size; the user can still override both
# under Settings > Display Preferences.
if sys.platform == "darwin":
    DEFAULT_FONT_FAMILY = ".AppleSystemUIFont"
    DEFAULT_FONT_SIZE = 13
elif sys.platform.startswith("win"):
    DEFAULT_FONT_FAMILY = "Segoe UI"
    DEFAULT_FONT_SIZE = 9
else:                                    # Linux/BSD: the near-universal default
    DEFAULT_FONT_FAMILY = "DejaVu Sans"
    DEFAULT_FONT_SIZE = 10

DEFAULT_THEME = "light"

# ---- THEME PALETTES --------------------------------------------------------
# Every color the QSS needs, keyed by a semantic name. LIGHT maps each key to
# the ORIGINAL literal so the light stylesheet is unchanged; DARK is a parallel
# dark-background / light-foreground palette. build_qss() reads a palette dict,
# so a new theme is just another dict.
LIGHT = {
    "window": "#ffffff",         # window / blanket widget background
    "surface": "#ffffff",        # table / menu / group-box background
    "header_bg": HEADER_BG,      # column headers, title box, net-worth strip
    "bar_bg": BAR_BG,            # account bar body
    "line": LINE,                # borders / separators
    "text": SLATE,               # titles, headers, menu text (slate)
    "muted": "#8a94a0",          # register subtitle
    "disabled": "#9aa2ab",       # disabled menu items
    "select": SELECT,            # selection fill
    "select_text": "#000000",    # selected-row text
    "blue": BLUE,                # account names / accent
    "menu_sel_bg": BLUE,         # drop-down highlighted item fill
    "menu_sel_text": "#ffffff",  # drop-down highlighted item text
    "menubar_sel_bg": SELECT,    # menu-BAR highlighted item fill
    "menubar_sel_text": SLATE,   # menu-BAR highlighted item text
    "grid": "#e6e9ee",           # table gridlines
    "hdr_border_r": "#dfe3e8",   # header section right border
    "hdr_border_b": "#cfd4db",   # header section bottom border
    "alt_row": ALT_ROW,          # DEFAULT alternate-row shading
    "negative": RED,             # DEFAULT negative-amount color
    "balance_text": "#222222",   # positive account-row balance text
    "cell_text": None,           # explicit table item-text color; None = keep the
                                 # widget palette default (light look unchanged)
    "branch_indicator": "#5b636e",  # tree expand/collapse arrows; 6.1:1 on white
    "btn_bg": "#f2f4f7",
    "btn_border": "#c7cdd6",
    "btn_hover": "#e7ebf1",
    "btn_pressed": "#d9e0ea",
    "extra": "",                 # theme-specific trailing QSS (none for light)
}

# DARK: dark surfaces, light text, a bright negative red legible on dark, and a
# lighter blue for account names. Backgrounds step window < bar < surface <
# alt_row < header so the shading and boxes stay visible.
DARK = {
    "window": "#1e1f22",
    "surface": "#24262a",
    "header_bg": "#2f3237",
    "bar_bg": "#202124",
    "line": "#3a3d42",
    "text": "#e3e5e8",
    "muted": "#9aa2ab",
    "disabled": "#6b7178",
    "select": "#3d5168",
    "select_text": "#ffffff",
    "blue": "#6fb1ff",
    "menu_sel_bg": "#3d5168",
    "menu_sel_text": "#ffffff",
    "menubar_sel_bg": "#3a3d42",
    "menubar_sel_text": "#e3e5e8",
    "grid": "#34363b",
    "hdr_border_r": "#3a3d42",
    "hdr_border_b": "#3a3d42",
    "base_row": "#2b2d31",       # the unshaded row; AlternateBase sits above it
    "alt_row": "#3b3e43",        # must stay LIGHTER than the base row (#2b2d31):
                                 # they were the same value, so alternate-row
                                 # shading was invisible in dark mode.
    "negative": "#ff6b6b",
    "balance_text": "#e3e5e8",
    "cell_text": "#e3e5e8",      # dark: table item text must be explicitly light,
                                 # or delegates fall back to a near-black palette
                                 # default and the register text is illegible
    "branch_indicator": "#c5cad2",  # tree expand/collapse arrows: the platform
                                    # style draws these near-black, so on the
                                    # dark surfaces they vanished (reported).
                                    # 8.4:1 on the tree background (#2b2d31).
    "btn_bg": "#35383e",
    "btn_border": "#45484e",
    "btn_hover": "#3f4247",
    "btn_pressed": "#2c2f34",
    # Dark needs explicit light text on the blanket widgets and dark backgrounds
    # on the standard input widgets (which carry no color rule in the light look).
    "extra": """
QWidget { color: #e3e5e8; }
QToolTip { color: #e3e5e8; background: #2b2d31; border: 1px solid #3a3d42; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit,
QTextEdit, QAbstractItemView {
    background: #2b2d31; color: #e3e5e8; border: 1px solid #3a3d42;
    selection-background-color: #3d5168; selection-color: #ffffff;
}
QComboBox QAbstractItemView {
    background: #2b2d31; color: #e3e5e8;
    selection-background-color: #3d5168; selection-color: #ffffff;
}
QCheckBox, QRadioButton, QGroupBox { color: #e3e5e8; }
QTableWidget { background: #24262a; color: #e3e5e8; }
/* A stylesheet-backgrounded QPushButton draws its LABEL from the palette's
   ButtonText unless 'color' is given here; without it dark-mode button text is
   dark-on-dark (reported). Spell it out for buttons and dialog boxes. */
QPushButton { color: #e3e5e8; }
QPushButton:disabled { color: #6b7178; }
QDialogButtonBox QPushButton { color: #e3e5e8; }
/* Tabs, for the same reason as the buttons above but worse. With no QTabBar
   rule the native Windows style paints the tab in its own LIGHT chrome while
   the label is drawn from the palette -- which dark mode has just made light --
   so the Holdings tabs came out light-on-light and unreadable (reported).
   Naming the tab here moves it onto stylesheet rendering, where both the
   background and the text are ours. */
QTabWidget::pane { border: 1px solid #3a3d42; background: #24262a; top: -1px; }
QTabBar::tab {
    background: #2b2d31; color: #9aa2ab;
    border: 1px solid #3a3d42; border-bottom: none;
    border-top-left-radius: 3px; border-top-right-radius: 3px;
    padding: 5px 12px; margin-right: 2px;
}
QTabBar::tab:selected { background: #35383e; color: #e3e5e8; }
QTabBar::tab:hover { background: #3f4247; color: #e3e5e8; }
QTabBar::tab:disabled { color: #6b7178; }
""",
}

_THEMES = {"light": LIGHT, "dark": DARK}


def palette_for(theme: str | None) -> dict:
    """The palette dict for a theme name ('light' or 'dark'); unknown -> light."""
    return _THEMES.get((theme or DEFAULT_THEME), LIGHT)


# ---- ACTIVE display state (user-overridable, applied live) -----------------
# apply_theme() copies the resolved theme + user overrides into this dict; models
# and widgets read the accessors below so a preference change recolors every
# negative amount, the row shading, and the account-bar text WITHOUT any change
# to mammon.ledger. It initializes to the exact current (light) look, so the
# defaults reproduce today's appearance identically until the user opts in.
_active = {
    "theme": DEFAULT_THEME,
    "palette": LIGHT,
    "font_family": DEFAULT_FONT_FAMILY,
    "font_size": DEFAULT_FONT_SIZE,
    "alt_row_color": ALT_ROW,
    "negative_color": RED,
    "row_shading": True,
}

# The application's palette BEFORE any theming (captured once, lazily, on the
# first apply_theme). Light restores it exactly, so the light look is unchanged;
# dark swaps in a full dark palette. A QSS alone leaks: widgets that draw text
# from the PALETTE rather than a QSS 'color' rule (button labels, in-cell editors,
# QTableWidget items, dialog fields) render dark-on-dark otherwise (reported).
_default_palette = None


def theme() -> str:
    """The ACTIVE theme name ('light' or 'dark')."""
    return _active["theme"]


def negative_color() -> str:
    """The ACTIVE negative-amount color (register/account-bar reds)."""
    return _active["negative_color"]


def alt_row_color() -> str:
    """The ACTIVE alternate-row shading color."""
    return _active["alt_row_color"]


def row_shading() -> bool:
    """Whether alternating-row shading is on."""
    return _active["row_shading"]


def text_color() -> str:
    """The ACTIVE primary text color (headings / group totals)."""
    return _active["palette"]["text"]


def accent_color() -> str:
    """The ACTIVE accent color (account names / positive net worth)."""
    return _active["palette"]["blue"]


def balance_text_color() -> str:
    """The ACTIVE positive account-row balance text color."""
    return _active["palette"]["balance_text"]


def cell_text_color():
    """The explicit table item-text color for the ACTIVE theme, or None to keep
    the widget's palette default. Light mode returns None (so the register's body
    text renders exactly as it does today); dark mode returns a light color so
    QTableView items -- which delegates paint from the palette, NOT the QSS color
    rule -- stay legible on the dark background."""
    return _active["palette"].get("cell_text")


def muted_color() -> str:
    """The ACTIVE muted text color (two-line classification / subtitles)."""
    return _active["palette"]["muted"]


def line_color() -> str:
    """The ACTIVE border/separator color."""
    return _active["palette"]["line"]


def header_bg_color() -> str:
    """The ACTIVE header/section background color."""
    return _active["palette"]["header_bg"]


def branch_indicator_color(palette: dict | None = None) -> str:
    """The tree expand/collapse arrow color for a palette (ACTIVE theme by
    default). Every tree in the app -- the Itemize by Category drill-down, the
    report category picker, the categories and tags dialogs -- gets its arrows
    from this one value."""
    p = palette or _active["palette"]
    return p.get("branch_indicator") or p["text"]


def _branch_qss(color: str) -> str:
    """The QTreeView::branch block for one theme, or '' if the arrow images
    cannot be generated (read-only install), in which case Qt keeps drawing its
    own native arrows exactly as it did before.

    The four selectors are the standard closed/open x has-siblings/not pairs;
    leaf rows are deliberately left unstyled so Qt still draws the branch lines.
    Note that a drawable ::branch rule REPLACES the native glyph rather than
    recoloring it, which is why each rule carries an image (see
    mammon.ui.branch_icons)."""
    from . import branch_icons

    closed = branch_icons.arrow_path("closed", color)
    opened = branch_icons.arrow_path("open", color)
    if closed is None or opened is None:
        return ""
    return """
/* ---- tree expand/collapse indicators ----
   Theme-driven ('branch_indicator' in the palette): the native glyphs are drawn
   by the platform style in near-black and were invisible in dark mode. */
QTreeView::branch:has-children:!has-siblings:closed,
QTreeView::branch:closed:has-children:has-siblings {
    border-image: none;
    image: url("%s");
}
QTreeView::branch:open:has-children:!has-siblings,
QTreeView::branch:open:has-children:has-siblings {
    border-image: none;
    image: url("%s");
}
""" % (closed.as_posix(), opened.as_posix())


def build_qss(palette: dict | None = None, alt_row: str | None = None) -> str:
    """The classic-desktop QSS for a given theme PALETTE, with the alternate-row
    shading color injected (the one theme value the user can pick independently).
    Omit both for the default light look."""
    p = palette or LIGHT
    if alt_row is None:
        alt_row = p["alt_row"]
    window = p["window"]; surface = p["surface"]; header_bg = p["header_bg"]
    bar_bg = p["bar_bg"]; line = p["line"]; text = p["text"]; muted = p["muted"]
    disabled = p["disabled"]; select = p["select"]; select_text = p["select_text"]
    blue = p["blue"]; menu_sel_bg = p["menu_sel_bg"]; menu_sel_text = p["menu_sel_text"]
    negative = p["negative"]
    menubar_sel_bg = p["menubar_sel_bg"]; menubar_sel_text = p["menubar_sel_text"]
    grid = p["grid"]; hdr_border_r = p["hdr_border_r"]; hdr_border_b = p["hdr_border_b"]
    btn_bg = p["btn_bg"]; btn_border = p["btn_border"]
    btn_hover = p["btn_hover"]; btn_pressed = p["btn_pressed"]
    branch = _branch_qss(branch_indicator_color(p))
    return f"""
QMainWindow, QWidget {{ background: {window}; }}

/* ---- menus ----
   The blank global 'QWidget {{ background }}' rule above also paints the menus,
   so WITHOUT the explicit selected-item colors below a highlighted item drew its
   text in (near) the same color as its background and vanished on hover (the user's
   report). Give the menu bar and drop-downs an explicit high-contrast
   selection: a filled highlight with contrasting text. */
QMenuBar {{ background: {header_bg}; border-bottom: 1px solid {line}; }}
QMenuBar::item {{ background: transparent; color: {text}; padding: 4px 10px; }}
QMenuBar::item:selected, QMenuBar::item:pressed {{
    background: {menubar_sel_bg}; color: {menubar_sel_text}; border-radius: 3px;
}}
QMenu {{ background: {surface}; color: {text}; border: 1px solid {line}; }}
QMenu::item {{ background: transparent; color: {text}; padding: 5px 26px 5px 22px; }}
QMenu::item:selected {{ background: {menu_sel_bg}; color: {menu_sel_text}; }}
QMenu::item:disabled {{ color: {disabled}; }}
QMenu::separator {{ height: 1px; background: {line}; margin: 4px 8px; }}

/* ---- left account bar: boxed category sections ---- */
QScrollArea#acctScroll {{ border: none; border-right: 1px solid {line}; }}
QWidget#acctBody {{ background: {bar_bg}; }}

QFrame#acctGroup {{
    background: {surface};
    border: 1px solid {line};
    border-radius: 4px;
}}
QWidget#acctGroupHeader {{
    background: {header_bg};
    border-bottom: 1px solid {line};
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}}
QLabel#acctGroupTitle {{ color: {text}; font-weight: bold; }}

QFrame#acctRow {{ background: transparent; border: none; }}
QFrame#acctRow[selected="true"] {{ background: {select}; }}
QFrame#acctRow QLabel#acctName {{ color: {blue}; }}
QFrame#acctRow QLabel#acctPending {{ color: {negative}; font-weight: bold; }}

/* ---- net worth strip: amount aligned to the balance column ---- */
QWidget#netWorth {{
    border-top: 1px solid {line};
    border-right: 1px solid {line};
    background: {header_bg};
}}

/* ---- register header ----
   The account title sits in a bordered box that spans the register's width and
   whose top aligns with the top of the left account bar's first group box
   (by request). */
QFrame#registerTitleBox {{
    background: {header_bg};
    border: 1px solid {line};
    border-radius: 4px;
}}
QLabel#registerTitle {{ color: {text}; font-size: 16pt; font-weight: 600; padding: 2px 6px; background: transparent; border: none; }}
QLabel#registerSub {{ color: {muted}; font-size: 8pt; padding: 0 2px 4px 2px; }}

/* ---- register table ---- */
QTableView {{
    background: {surface};
    alternate-background-color: {alt_row};
    gridline-color: {grid};
    selection-background-color: {select};
    selection-color: {select_text};
}}
QHeaderView::section {{
    background: {header_bg};
    color: {text};
    padding: 4px 6px;
    border: none;
    border-right: 1px solid {hdr_border_r};
    border-bottom: 1px solid {hdr_border_b};
}}
QTableView QTableCornerButton::section {{ background: {header_bg}; border: none; }}

/* ---- reconcile workspace: two titled panes (debits left, credits right) ---- */
QFrame#reconcilePane {{ border: 1px solid {line}; border-radius: 4px; }}
QLabel#reconcilePaneTitle {{
    background: {header_bg};
    color: {text};
    font-weight: bold;
    padding: 5px 8px;
    border-bottom: 1px solid {line};
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}}

/* ---- buttons / toolbar ---- */
QPushButton {{
    background: {btn_bg};
    border: 1px solid {btn_border};
    border-radius: 3px;
    padding: 3px 10px;
}}
QPushButton:hover {{ background: {btn_hover}; }}
QPushButton:pressed {{ background: {btn_pressed}; }}
{branch}
{p["extra"]}"""


# The default themed QSS (used when no user preferences are supplied). Kept as a
# module constant so callers/tests can inspect the stylesheet directly.
QUICKEN_QSS = build_qss()


def _build_dark_palette():
    """A full dark QPalette from the DARK theme dict, so every widget that reads
    the palette (button labels, in-cell editors, QTableWidget items, dialog
    fields, disabled text) gets legible dark-mode colors -- not just the widgets
    the QSS explicitly names."""
    from PyQt5.QtGui import QColor, QPalette
    p = QPalette()
    window = QColor(DARK["window"]); text = QColor(DARK["text"])
    base = QColor(DARK["base_row"]); alt = QColor(DARK["alt_row"])
    button = QColor(DARK["btn_bg"]); disabled = QColor(DARK["disabled"])
    highlight = QColor(DARK["select"]); highlight_text = QColor("#ffffff")
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, alt)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, button)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor("#ffffff"))
    p.setColor(QPalette.ToolTipBase, base)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.PlaceholderText, QColor(DARK["muted"]))
    p.setColor(QPalette.Highlight, highlight)
    p.setColor(QPalette.HighlightedText, highlight_text)
    p.setColor(QPalette.Link, QColor(DARK["blue"]))
    p.setColor(QPalette.LinkVisited, QColor(DARK["blue"]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    p.setColor(QPalette.Disabled, QPalette.Highlight, QColor("#33363b"))
    return p


def apply_theme(app, values=None) -> None:
    """Set the classic font, stylesheet, and palette on a QApplication.

    ``values`` is an optional Display-Preferences dict (see mammon.ui.prefs): its
    theme ('light'/'dark'), font family/size, alternate-row color, negative-amount
    color, and row-shading flag override the defaults and become the ACTIVE
    display state that models/widgets read live. Omit it for the default (current)
    light look, which it reproduces exactly.

    The FIRST call captures the app's original (light) palette; light mode always
    restores that exact palette (so the light look is unchanged), and dark mode
    installs a full dark palette so palette-driven widgets (buttons, in-cell
    editors, split-line fields) are legible, not just the QSS-named ones."""
    from PyQt5.QtGui import QFont, QPalette
    global _default_palette
    if _default_palette is None:
        _default_palette = QPalette(app.palette())  # capture BEFORE any theming
    if values:
        theme_name = values.get("theme") or DEFAULT_THEME
        palette = palette_for(theme_name)
        _active["theme"] = theme_name if theme_name in _THEMES else DEFAULT_THEME
        _active["palette"] = palette
        _active["font_family"] = values.get("font_family") or DEFAULT_FONT_FAMILY
        _active["font_size"] = int(values.get("font_size") or DEFAULT_FONT_SIZE)
        _active["alt_row_color"] = values.get("alt_row_color") or palette["alt_row"]
        _active["negative_color"] = values.get("negative_color") or palette["negative"]
        _active["row_shading"] = bool(values.get("row_shading", True))
    else:
        # No preferences supplied -> the default (current) light classic look.
        _active.update({
            "theme": DEFAULT_THEME,
            "palette": LIGHT,
            "font_family": DEFAULT_FONT_FAMILY,
            "font_size": DEFAULT_FONT_SIZE,
            "alt_row_color": ALT_ROW,
            "negative_color": RED,
            "row_shading": True,
        })
    font = QFont(_active["font_family"], _active["font_size"])
    font.setStyleHint(QFont.SansSerif)
    app.setFont(font)
    # Palette first, then stylesheet: dark installs a full dark palette so
    # palette-driven widgets are legible; light restores the captured default so
    # the light look is byte-for-byte unchanged.
    app.setPalette(_build_dark_palette() if _active["theme"] == "dark"
                   else _default_palette)
    app.setStyleSheet(build_qss(_active["palette"], _active["alt_row_color"]))
