"""Tree expand/collapse indicators must be VISIBLE in every theme.

The user's report: in the Itemize by Category report the '>' and the
upside-down caret "can't be seen well in dark mode... this might affect other
reports too". It did: Qt draws the branch glyphs natively, in colors the
platform style picks, so the dark theme never had a say and they came out
near-black on a near-black background.

These tests measure VISIBILITY, not the presence of a string: the WCAG contrast
ratio of the arrow color against every background the theme paints behind a
tree, and -- for dark mode -- the actual rendered pixels of a real tree.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.ui import style
from mammon.tests import fresh_db

# The four standard branch states Qt distinguishes. All of them must be styled,
# or a tree's first/last row keeps the invisible native glyph.
FOUR_STATES = [
    "QTreeView::branch:has-children:!has-siblings:closed",
    "QTreeView::branch:closed:has-children:has-siblings",
    "QTreeView::branch:open:has-children:!has-siblings",
    "QTreeView::branch:open:has-children:has-siblings",
]

# Every palette key that can end up painted behind a tree row.
BACKGROUND_KEYS = ("window", "surface", "header_bg", "bar_bg", "alt_row",
                   "base_row")

MIN_CONTRAST = 4.5  # WCAG AA for non-text/large-text UI components


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "branches.db")
    yield c
    c.close()


# ---- WCAG contrast, computed here so the test measures what the user sees ----

def _linear(channel: float) -> float:
    return (channel / 12.92 if channel <= 0.03928
            else ((channel + 0.055) / 1.055) ** 2.4)


def _luminance(color: str) -> float:
    h = color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b))


def contrast_ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_helper_matches_known_wcag_values():
    # Sanity-check the yardstick before trusting it: black on white is 21:1,
    # and a color against itself is 1:1.
    assert round(contrast_ratio("#000000", "#ffffff"), 1) == 21.0
    assert round(contrast_ratio("#777777", "#777777"), 2) == 1.0


# ---- the rules exist, for both themes ---------------------------------------

@pytest.mark.parametrize("name,palette", [("light", style.LIGHT),
                                          ("dark", style.DARK)])
def test_build_qss_styles_all_four_branch_states(name, palette):
    qss = style.build_qss(palette)
    for selector in FOUR_STATES:
        assert selector in qss, name + " QSS is missing " + selector


@pytest.mark.parametrize("name,palette", [("light", style.LIGHT),
                                          ("dark", style.DARK)])
def test_branch_indicator_is_legible_on_every_theme_background(name, palette):
    """The acceptance criterion: the arrow must actually be seeable."""
    arrow = palette["branch_indicator"]
    for key in BACKGROUND_KEYS:
        bg = palette.get(key)
        if not bg:
            continue
        ratio = contrast_ratio(arrow, bg)
        assert ratio >= MIN_CONTRAST, (
            "%s theme: branch arrow %s on %s (%s) is only %.2f:1"
            % (name, arrow, key, bg, ratio))


# ---- the color comes from the theme, never a literal in the QSS -------------

@pytest.mark.parametrize("name,palette", [("light", style.LIGHT),
                                          ("dark", style.DARK)])
def test_branch_color_is_the_theme_dict_value(qapp, name, palette):
    """The generated arrow carries exactly the palette's color, and swapping the
    palette value swaps the QSS -- i.e. nothing is hardcoded in build_qss()."""
    from PyQt5.QtGui import QColor, QImage

    from mammon.ui import branch_icons

    wanted = palette["branch_indicator"]
    qss = style.build_qss(palette)
    assert wanted.lstrip("#") in qss  # the arrow images are keyed by the color

    # The pixels of the generated arrow are that exact color.
    for shape in ("closed", "open"):
        path = branch_icons.arrow_path(shape, wanted)
        assert path is not None and str(path.as_posix()) in qss
        img = QImage(str(path))
        assert not img.isNull(), "Qt cannot load the generated arrow " + str(path)
        opaque = [QColor(img.pixel(x, y)).name()
                  for y in range(img.height()) for x in range(img.width())
                  if QColor(img.pixelColor(x, y)).alpha() == 255]
        assert opaque, shape + " arrow rendered nothing"
        assert set(opaque) == {wanted.lower()}

    # Change the palette value and the stylesheet follows it.
    recolored = dict(palette)
    recolored["branch_indicator"] = "#ff00ff"
    other = style.build_qss(recolored)
    assert "ff00ff" in other
    assert wanted.lstrip("#") not in other


# ---- the rendered pixels: the user's actual complaint ------------------------

def test_dark_tree_actually_paints_visible_arrows(qapp):
    """Render a real tree under the dark theme and find the arrow pixels.

    A ``QTreeView::branch`` rule that Qt cannot resolve (a missing image, an
    unsupported url form) fails SILENTLY -- and worse, a drawable branch rule
    suppresses the native glyph, so a broken rule leaves no arrow at all. Only
    looking at the pixels catches that."""
    from PyQt5.QtGui import QColor, QImage, QPainter
    from PyQt5.QtWidgets import QTreeWidget, QTreeWidgetItem

    saved_qss = qapp.styleSheet()
    saved_palette = qapp.palette()
    try:
        style.apply_theme(qapp, {"theme": "dark"})
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        for n in range(2):
            top = QTreeWidgetItem(["parent %d" % n])
            tree.addTopLevelItem(top)
            top.addChild(QTreeWidgetItem(["child"]))
        tree.expandItem(tree.topLevelItem(0))   # one open arrow, one closed
        tree.resize(300, 160)
        tree.show()
        qapp.processEvents()

        shot = QImage(300, 160, QImage.Format_ARGB32)
        shot.fill(0)
        painter = QPainter(shot)
        tree.render(painter)
        painter.end()

        wanted = style.DARK["branch_indicator"].lower()
        hits = [(x, y) for y in range(shot.height()) for x in range(shot.width())
                if QColor(shot.pixel(x, y)).name() == wanted]
        assert len(hits) >= 8, "no visible expand/collapse arrows were painted"
        # Two separate glyphs (the open one and the closed one), both inside the
        # first indentation column.
        rows = sorted({y for _x, y in hits})
        assert max(x for x, _y in hits) < tree.indentation()
        assert rows[-1] - rows[0] > 10, "only one arrow was drawn"
        tree.close()
    finally:
        qapp.setStyleSheet(saved_qss)
        qapp.setPalette(saved_palette)
        style.apply_theme(qapp)  # back to the default light look


# ---- the real report the user reported --------------------------------------

def test_itemize_report_tree_is_covered_by_the_global_rule(qapp, conn):
    """The Itemize by Category drill-down sets no stylesheet of its own, so it
    inherits the application rule -- which is the whole point of fixing this
    globally rather than in one report."""
    from mammon.ui.report_window import ITEMIZE_SPEC, ReportWindow

    a = ledger.create_account(conn, "Checking", "checking")
    fuel = ledger.resolve_category(conn, "Auto & Transport:Fuel")
    ledger.add_transaction(conn, a, "2026-01-05", -100_00, category_id=fuel,
                           payee="Fuel Stop")

    saved_qss = qapp.styleSheet()
    saved_palette = qapp.palette()
    win = None
    try:
        style.apply_theme(qapp, {"theme": "dark"})
        win = ReportWindow(conn, spec=ITEMIZE_SPEC)
        assert win.tree is not None
        # No local stylesheet shadows the global one...
        assert win.tree.styleSheet() == ""
        # ...and the application sheet the tree resolves against carries the
        # theme-driven branch rules.
        from PyQt5.QtWidgets import QApplication

        effective = QApplication.instance().styleSheet()
        for selector in FOUR_STATES:
            assert selector in effective
        assert style.DARK["branch_indicator"].lstrip("#") in effective
    finally:
        if win is not None:
            win.close()
        qapp.setStyleSheet(saved_qss)
        qapp.setPalette(saved_palette)
        style.apply_theme(qapp)
