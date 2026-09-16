"""Lightweight, persisted UI preferences (QSettings-backed).

Remembers the register's DISPLAY PREFERENCES -- the default one-line/two-line
view, the register font (family + point size), alternating-row shading on/off
and its color, and the negative-amount color -- so the Settings > Display
Preferences choices survive across runs. Everything writes to QSettings (an INI
file / the platform's user scope) -- NEVER to the ledger database, so there is
no schema change and no write into the user's mammon.db.

The defaults are pulled from :mod:`mammon.ui.style` so the out-of-the-box values
reproduce the current appearance EXACTLY; nothing changes unless the user opts
in. :func:`display_prefs` returns the whole set as a dict for
:func:`mammon.ui.style.apply_theme`.

Theme-sensitive vs. global preferences
--------------------------------------
Some display settings mean different things in light vs. dark mode and MUST be
remembered PER THEME, or a pick made in one theme silently overwrites the other:
the register's alternate-row shade and the negative-amount color are legible
only against their own background, so each theme keeps its own slot
(``display/theme_colors/<theme>/<role>``) and :func:`alt_row_color` /
:func:`negative_color` read the slot for the CURRENTLY active theme. A
pre-namespacing single-key value (the original global ``display/alt_row_color`` /
``display/negative_color``) is migrated into the active theme's slot on first
read -- an existing user loses nothing, and the pick lands in the theme it was
chosen under rather than leaking into the other. Everything else -- font
family/size, date format, row-shading on/off, the two-line default -- means the
same thing in both themes and stays global.
"""
from __future__ import annotations

from PyQt5.QtCore import QSettings

from mammon.ui import style

_ORG = "Mammon"
_APP = "Mammon"

_TWO_LINE_KEY = "register/two_line"
_ACCOUNT_VIEW_PREFIX = "register/view_mode/"
_SOUND_KEY = "register/accepted_sound"
_THEME_KEY = "display/theme"
_FONT_FAMILY_KEY = "display/font_family"
_FONT_SIZE_KEY = "display/font_size"
_ROW_SHADING_KEY = "display/row_shading"
# The two names below are the PRE-NAMESPACING single keys (one value shared by
# both themes). They are no longer written -- kept only as the migration source
# read once by _read_theme_color, then removed. The live per-theme slots live
# under _theme_color_key(role, theme), a group disjoint from these leaf keys so
# QSettings never sees a key and a group sharing a path.
_ALT_ROW_COLOR_KEY = "display/alt_row_color"        # legacy (migrated on read)
_NEGATIVE_COLOR_KEY = "display/negative_color"       # legacy (migrated on read)
_DATE_FORMAT_KEY = "display/date_format"


def _theme_color_key(role: str, theme_name: str) -> str:
    """QSettings key for a theme-sensitive color slot. ``role`` is 'alt_row' or
    'negative'; ``theme_name`` is 'light' or 'dark'. Each theme keeps its own
    value so a pick in one theme never bleeds into the other."""
    return f"display/theme_colors/{theme_name}/{role}"

# Defaults reproduce today's look exactly (single source of truth = style.py).
DEFAULT_TWO_LINE = False
DEFAULT_THEME = style.DEFAULT_THEME            # "light"
DEFAULT_FONT_FAMILY = style.DEFAULT_FONT_FAMILY
DEFAULT_FONT_SIZE = style.DEFAULT_FONT_SIZE
DEFAULT_ROW_SHADING = True
# The alt-row / negative colors DEFAULT to the ACTIVE theme's palette value (so
# dark mode gets dark-appropriate colors out of the box); an explicit user pick
# always overrides, and is remembered per theme (see _read_theme_color). The
# light theme's values are the original #f4f6f9 / #c0392b; these DEFAULT_* names
# stay light-valued because they are the "Restore Defaults" target and the empty
# fallback only.
DEFAULT_ALT_ROW_COLOR = style.ALT_ROW
DEFAULT_NEGATIVE_COLOR = style.RED

# DISPLAY-only date format (parsing/storage stay ISO YYYY-MM-DD). The offered
# choices are the tokens fmt_date understands; the first is the default.
DATE_FORMATS = ("MM/DD/YYYY", "DD/MM/YYYY", "YYYY-MM-DD")
DEFAULT_DATE_FORMAT = "MM/DD/YYYY"


def _settings(settings: QSettings | None = None) -> QSettings:
    """The shared QSettings store (IniFormat/UserScope so tests can redirect it
    with ``QSettings.setPath``). Pass an explicit store to override (testing)."""
    if settings is not None:
        return settings
    return QSettings(QSettings.IniFormat, QSettings.UserScope, _ORG, _APP)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value, default: str) -> str:
    s = "" if value is None else str(value).strip()
    return s or default


# ---- default one/two-line view ---------------------------------------------
def two_line_default(settings: QSettings | None = None) -> bool:
    """Whether the register should open in two-line mode (default: False)."""
    return _as_bool(_settings(settings).value(_TWO_LINE_KEY, DEFAULT_TWO_LINE))


def set_two_line_default(on: bool, settings: QSettings | None = None) -> None:
    """Persist the register's default one-line/two-line view."""
    s = _settings(settings)
    s.setValue(_TWO_LINE_KEY, bool(on))
    s.sync()


# ---- per-account one/two-line override --------------------------------------
# Which layout suits a register depends on the ACCOUNT: a card whose rows carry
# long statement descriptions and a category wants two lines, while a checking
# account entered by hand reads fine in one. A single global switch forces the
# same answer everywhere, so the account's own choice is stored beside it and
# the global setting becomes the default for accounts that have not chosen.
def account_view_mode(account_id, settings: QSettings | None = None) -> str:
    """'one' or 'two' for this account -- its own override if it has one, else
    the global default."""
    s = _settings(settings)
    stored = s.value(f"{_ACCOUNT_VIEW_PREFIX}{int(account_id)}", None)
    if stored in ("one", "two"):
        return stored
    return "two" if two_line_default(settings) else "one"


def set_account_view_mode(account_id, mode: str,
                          settings: QSettings | None = None) -> None:
    """Store this account's own one/two-line choice. ``mode`` of None or "" clears
    the override, putting the account back on the global default."""
    s = _settings(settings)
    key = f"{_ACCOUNT_VIEW_PREFIX}{int(account_id)}"
    if mode in ("one", "two"):
        s.setValue(key, mode)
    else:
        s.remove(key)
    s.sync()


def has_account_view_mode(account_id, settings: QSettings | None = None) -> bool:
    """True when this account has its own override (so a change to the global
    default must leave it alone)."""
    stored = _settings(settings).value(
        f"{_ACCOUNT_VIEW_PREFIX}{int(account_id)}", None)
    return stored in ("one", "two")


# ---- transaction-accepted sound ---------------------------------------------
DEFAULT_SOUND = True


def sound_enabled(settings: QSettings | None = None) -> bool:
    """Whether to play the ka-ching when a transaction is saved (default: on)."""
    return _as_bool(_settings(settings).value(_SOUND_KEY, DEFAULT_SOUND))


def set_sound_enabled(on: bool, settings: QSettings | None = None) -> None:
    s = _settings(settings)
    s.setValue(_SOUND_KEY, bool(on))
    s.sync()


# ---- theme (light / dark) ---------------------------------------------------
def theme(settings: QSettings | None = None) -> str:
    """The register theme, 'light' (default) or 'dark'."""
    t = _as_str(_settings(settings).value(_THEME_KEY, DEFAULT_THEME), DEFAULT_THEME)
    return t if t in ("light", "dark") else DEFAULT_THEME


def set_theme(name: str, settings: QSettings | None = None) -> None:
    s = _settings(settings)
    s.setValue(_THEME_KEY, name if name in ("light", "dark") else DEFAULT_THEME)
    s.sync()


# ---- date display format ----------------------------------------------------
def date_format(settings: QSettings | None = None) -> str:
    """The user-chosen date DISPLAY format, one of :data:`DATE_FORMATS`
    (default ``MM/DD/YYYY``). A stored value outside the offered set falls back
    to the default so a hand-edited INI can never break rendering."""
    f = _as_str(_settings(settings).value(_DATE_FORMAT_KEY, DEFAULT_DATE_FORMAT),
                DEFAULT_DATE_FORMAT)
    return f if f in DATE_FORMATS else DEFAULT_DATE_FORMAT


def set_date_format(fmt: str, settings: QSettings | None = None) -> None:
    s = _settings(settings)
    s.setValue(_DATE_FORMAT_KEY, fmt if fmt in DATE_FORMATS else DEFAULT_DATE_FORMAT)
    s.sync()


# ---- register font ----------------------------------------------------------
def font_family(settings: QSettings | None = None) -> str:
    return _as_str(_settings(settings).value(_FONT_FAMILY_KEY, DEFAULT_FONT_FAMILY),
                   DEFAULT_FONT_FAMILY)


def font_size(settings: QSettings | None = None) -> int:
    return _as_int(_settings(settings).value(_FONT_SIZE_KEY, DEFAULT_FONT_SIZE),
                   DEFAULT_FONT_SIZE)


# ---- row shading + colors ---------------------------------------------------
def row_shading(settings: QSettings | None = None) -> bool:
    return _as_bool(_settings(settings).value(_ROW_SHADING_KEY, DEFAULT_ROW_SHADING),
                    DEFAULT_ROW_SHADING)


def _read_theme_color(role: str, legacy_key: str, s: QSettings,
                      theme_name: str | None = None) -> str:
    """The theme-sensitive color for ``role`` ('alt_row'/'negative'), read from
    the requested theme's own slot (``theme_name``; default = the currently
    active theme). Falls back to that theme's palette value. On first read, a
    pre-namespacing single-key value is migrated into the ACTIVE theme's slot and
    the legacy key dropped -- so an existing user's pick is never lost and cannot
    bleed into the other theme (the legacy value was chosen under whatever theme
    is active now, so only the active theme may claim it)."""
    active = theme(s)
    target = theme_name if theme_name in ("light", "dark") else active
    slot = _theme_color_key(role, target)
    stored = s.value(slot, None)
    if stored is not None and str(stored).strip():
        return str(stored).strip()
    if target == active:
        legacy = s.value(legacy_key, None)
        if legacy is not None and str(legacy).strip():
            val = str(legacy).strip()
            s.setValue(slot, val)
            s.remove(legacy_key)
            s.sync()
            return val
    return style.palette_for(target)[role]     # role is exactly the palette key


def alt_row_color(settings: QSettings | None = None,
                  theme_name: str | None = None) -> str:
    """The alternate-row shade for the active theme (or ``theme_name`` if given).
    Each theme remembers its own pick; unset falls back to that theme's palette
    (light #f4f6f9 / dark tint). Pass ``theme_name`` to read another theme's slot
    without switching -- the Display Preferences dialog uses this so toggling its
    theme combo surfaces what that theme will actually paint."""
    return _read_theme_color("alt_row", _ALT_ROW_COLOR_KEY, _settings(settings),
                             theme_name)


def negative_color(settings: QSettings | None = None,
                   theme_name: str | None = None) -> str:
    """The negative-amount color for the active theme (or ``theme_name`` if
    given). Per theme, unset falls back to that theme's palette value (light
    #c0392b / dark bright red, legible on dark)."""
    return _read_theme_color("negative", _NEGATIVE_COLOR_KEY, _settings(settings),
                             theme_name)


# ---- whole-set read / write -------------------------------------------------
def display_prefs(settings: QSettings | None = None) -> dict:
    """Every display preference as one dict -- fed to
    :func:`mammon.ui.style.apply_theme` and the Display Preferences dialog."""
    s = _settings(settings)
    return {
        "theme": theme(s),
        "font_family": font_family(s),
        "font_size": font_size(s),
        "row_shading": row_shading(s),
        "alt_row_color": alt_row_color(s),
        "negative_color": negative_color(s),
        "two_line": two_line_default(s),
        "date_format": date_format(s),
    }


def set_display_prefs(values: dict, settings: QSettings | None = None) -> None:
    """Persist a Display Preferences dict (only the keys present are written)."""
    s = _settings(settings)
    # A color pick belongs to the theme it was chosen UNDER. When this same call
    # also changes 'theme', the Display Preferences dialog has already reseeded
    # the swatches to the new theme, so the colors describe that new theme --
    # write them to its slot. Otherwise they belong to the current active theme.
    if "theme" in values:
        name = values["theme"]
        color_theme = name if name in ("light", "dark") else DEFAULT_THEME
        s.setValue(_THEME_KEY, color_theme)
    else:
        color_theme = theme(s)
    if "font_family" in values:
        s.setValue(_FONT_FAMILY_KEY, _as_str(values["font_family"], DEFAULT_FONT_FAMILY))
    if "font_size" in values:
        s.setValue(_FONT_SIZE_KEY, _as_int(values["font_size"], DEFAULT_FONT_SIZE))
    if "row_shading" in values:
        s.setValue(_ROW_SHADING_KEY, bool(values["row_shading"]))
    if "alt_row_color" in values:
        s.setValue(_theme_color_key("alt_row", color_theme),
                   _as_str(values["alt_row_color"], DEFAULT_ALT_ROW_COLOR))
    if "negative_color" in values:
        s.setValue(_theme_color_key("negative", color_theme),
                   _as_str(values["negative_color"], DEFAULT_NEGATIVE_COLOR))
    if "two_line" in values:
        s.setValue(_TWO_LINE_KEY, bool(values["two_line"]))
        if "sound" in values:
            s.setValue(_SOUND_KEY, bool(values["sound"]))
    if "date_format" in values:
        fmt = values["date_format"]
        s.setValue(_DATE_FORMAT_KEY, fmt if fmt in DATE_FORMATS else DEFAULT_DATE_FORMAT)
    s.sync()


# ---------------------------------------------------------------------------
# Import-review visibility, remembered PER ACCOUNT.
#
# Accepting a review row used to make it vanish permanently, even though the row
# was kept in the database forever -- retained and invisible at the same time,
# which is the worst of both. The panel can now show actioned rows greyed out,
# and how much to show is a per-account habit: an account fed by one clean
# monthly download wants them hidden, one that needs constant correction wants
# the history. So the choice is persisted per account rather than globally, and
# survives restarts -- being interrupted mid-review is exactly when you need it.
# ---------------------------------------------------------------------------
_REVIEW_VIS_KEY = "review/visibility"          # + "/<account_id>"

VIS_PENDING = "pending"     # hide anything already accepted or discarded
VIS_BATCH = "batch"         # + actioned rows of the imports holding pending work
VIS_ALL = "all"             # + every retained import
VIS_MODES = (VIS_PENDING, VIS_BATCH, VIS_ALL)
DEFAULT_REVIEW_VIS = VIS_BATCH


def review_visibility(account_id, settings: QSettings | None = None) -> str:
    """How much of ``account_id``'s review list to show (see VIS_* above)."""
    s = _settings(settings)
    raw = s.value(f"{_REVIEW_VIS_KEY}/{int(account_id)}", DEFAULT_REVIEW_VIS)
    mode = _as_str(raw, DEFAULT_REVIEW_VIS)
    return mode if mode in VIS_MODES else DEFAULT_REVIEW_VIS


def set_review_visibility(account_id, mode: str,
                          settings: QSettings | None = None) -> None:
    """Remember the review visibility for ``account_id``."""
    s = _settings(settings)
    s.setValue(f"{_REVIEW_VIS_KEY}/{int(account_id)}",
               mode if mode in VIS_MODES else DEFAULT_REVIEW_VIS)
    s.sync()


# ---------------------------------------------------------------------------
# register columns the user has hidden (the column chooser on the register's
# header). Global rather than per account: which columns a person wants is a
# habit, not an account property. Stored as column NAMES (RegisterModel
# .HEADERS), never indexes, so a future header reorder cannot hide the wrong
# column. Columns the register itself hides (two-line mode collapses
# Category/Memo/Tag, a loan register drops Num/Tag) are layered on top.
# ---------------------------------------------------------------------------
_HIDDEN_COLS_KEY = "register/hidden_columns"


def _hidden_cols_key(scope: str) -> str:
    # ``scope`` names WHICH register's chooser. The cash register keeps the
    # historical "register/..." key so an existing install's hidden columns
    # survive; the investment register (parity item) uses its own scope so its
    # different column set never collides with the cash names.
    return _HIDDEN_COLS_KEY if scope == "register" else f"{scope}/hidden_columns"


def hidden_columns(settings: QSettings | None = None, *,
                   scope: str = "register") -> list[str]:
    """Names of the register columns the user chose to hide (may be empty).
    ``scope`` selects the register ("register" for cash, "investment_register"
    for the investment register)."""
    s = _settings(settings)
    raw = s.value(_hidden_cols_key(scope), "")
    if isinstance(raw, (list, tuple)):
        names = [str(x) for x in raw]
    else:
        names = _as_str(raw, "").split(",")
    return [n.strip() for n in names if n and n.strip()]


def set_hidden_columns(names, settings: QSettings | None = None, *,
                       scope: str = "register") -> None:
    """Persist the hidden register columns for ``scope`` (deduplicated, order
    kept)."""
    s = _settings(settings)
    clean = [str(n).strip() for n in names if str(n).strip()]
    s.setValue(_hidden_cols_key(scope), ",".join(dict.fromkeys(clean)))
    s.sync()


# ---------------------------------------------------------------------------
# scheduled payments: pre-enter due ones when the app starts (roadmap item 5).
# On by default -- a reminder that has to be generated by hand is one that
# gets missed -- and switchable from the Scheduled Payments manager. A
# remind-only definition (auto_enter off) is never pre-entered regardless.
# ---------------------------------------------------------------------------
_AUTO_ENTER_KEY = "scheduled/auto_enter_on_launch"
DEFAULT_AUTO_ENTER = True

# ---------------------------------------------------------------------------
# projection / calendar: the account slots (up to five) the month is summed
# over. A display choice, so it lives here and the calendar opens on the same
# accounts next time. Ids that no longer exist are dropped when read back.
# ---------------------------------------------------------------------------
_PROJECTION_SLOTS_KEY = "projection/slots"


def projection_slots(settings: QSettings | None = None) -> list[int]:
    """Account ids in the calendar's slots, in slot order (may be empty)."""
    s = _settings(settings)
    raw = s.value(_PROJECTION_SLOTS_KEY, "")
    parts = [str(x) for x in raw] if isinstance(raw, (list, tuple)) else \
        _as_str(raw, "").split(",")
    out: list[int] = []
    for p in parts:
        p = p.strip()
        if p.lstrip("-").isdigit():
            out.append(int(p))
    return out


def set_projection_slots(ids, settings: QSettings | None = None) -> None:
    """Persist the calendar's account slots (deduplicated, order kept)."""
    s = _settings(settings)
    clean = [str(int(i)) for i in ids if i is not None]
    s.setValue(_PROJECTION_SLOTS_KEY, ",".join(dict.fromkeys(clean)))
    s.sync()


# ---------------------------------------------------------------------------
# allocation: which accounts the Asset Allocation window covers. A view choice
# (the numbers themselves are all in the ledger), so it lives here and the
# window opens on the same scope next time.
# ---------------------------------------------------------------------------
_ALLOCATION_SCOPE_KEY = "allocation/scope"
DEFAULT_ALLOCATION_SCOPE = "everything"


def allocation_scope(settings: QSettings | None = None) -> str:
    """The saved Asset Allocation scope, one of
    ``portfolio.ALLOCATION_SCOPES``; the default counts everything owned,
    property included."""
    from mammon.portfolio import ALLOCATION_SCOPES
    v = _as_str(_settings(settings).value(_ALLOCATION_SCOPE_KEY,
                                          DEFAULT_ALLOCATION_SCOPE),
                DEFAULT_ALLOCATION_SCOPE)
    return v if v in ALLOCATION_SCOPES else DEFAULT_ALLOCATION_SCOPE


def set_allocation_scope(scope: str, settings: QSettings | None = None) -> None:
    s = _settings(settings)
    s.setValue(_ALLOCATION_SCOPE_KEY, str(scope))
    s.sync()


# ---------------------------------------------------------------------------
# money market: does a sweep count as CASH or as a SECURITY? Both answers are
# defensible -- a sweep is a fund you hold shares of, and it is also the
# account's spendable balance -- so it is a view choice, not a fact about the
# ledger. OFF by default, deliberately: ON would change the cash-vs-securities
# split of every existing file the first time it opened, with nothing on
# screen to say why. Totals never move either way (SRD 5.8e-2c).
# ---------------------------------------------------------------------------
_MONEY_MARKET_AS_CASH_KEY = "allocation/money_market_as_cash"
DEFAULT_MONEY_MARKET_AS_CASH = False


def money_market_as_cash(settings: QSettings | None = None) -> bool:
    """True to roll money-market holdings up as cash rather than securities."""
    return _as_bool(_settings(settings).value(_MONEY_MARKET_AS_CASH_KEY,
                                              DEFAULT_MONEY_MARKET_AS_CASH),
                    DEFAULT_MONEY_MARKET_AS_CASH)


def set_money_market_as_cash(on: bool, settings: QSettings | None = None) -> None:
    s = _settings(settings)
    s.setValue(_MONEY_MARKET_AS_CASH_KEY, bool(on))
    s.sync()


def auto_enter_on_launch(settings: QSettings | None = None) -> bool:
    return _as_bool(_settings(settings).value(_AUTO_ENTER_KEY, DEFAULT_AUTO_ENTER),
                    DEFAULT_AUTO_ENTER)


def set_auto_enter_on_launch(on: bool, settings: QSettings | None = None) -> None:
    s = _settings(settings)
    s.setValue(_AUTO_ENTER_KEY, bool(on))
    s.sync()
