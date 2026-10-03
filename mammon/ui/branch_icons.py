"""Theme-colored vector icons, rasterized to tiny PNGs: tree branch arrows, and
the over-budget mark the Financial Calendar puts on an overspending expense.

WHY THIS EXISTS
Qt draws a QTreeView's branch indicator natively, in colors the platform style
picks -- and the platform style has no idea Mammon is in dark mode, so the
'>' and 'v' glyphs came out near-black on the dark background and effectively
disappeared (reported against the Itemize by Category report, and true of every
other tree). The only way to recolor them app-wide is a
``QTreeView::branch`` rule in the global stylesheet.

WHY IMAGE FILES
Once ANY drawable ``QTreeView::branch`` rule exists, QStyleSheetStyle stops
delegating to the native style and paints the rule itself -- background, border
and ``image``. So a rule that merely names a color would ERASE the arrow rather
than recolor it: the glyph has to come from an image. Qt stylesheets resolve
``url()`` through QFile, so ``data:`` URIs do NOT work, and the repository is
public and deliberately carries no binary assets. The arrows are therefore
GENERATED here, as tiny PNGs written once per (shape, color) into
``paths.cache_dir()``, with a pure-Python encoder (zlib + struct) so generation
needs neither a QApplication nor an image library and can run at import time,
before Qt is up.

The color is never a literal in here: callers pass the active theme's
``branch_indicator`` value, so a palette edit is the only place a tree arrow's
color is decided.

WHY ARBITRARY POLYGONS
The same three constraints -- no binary assets, no ``data:`` URI, color decided
per theme -- apply to any small mark the UI needs, so the rasterizer takes a
POLYGON rather than a triangle. The over-budget moneybag (SRD 5.10e) is a
fourteen-vertex silhouette with a concave neck, which is why the fill rule is a
crossing-number test over the edge list rather than the triangle same-side test
this started as. That rewrite is pinned byte-for-byte on the two arrows by
``test_style_tree_branches.test_render_png_matches_the_legacy_rasterizer``: no
supersample point lands exactly on an edge of either triangle, so the two rules
cannot disagree there, and a theme's arrows never silently shift.

Why a SHAPE and not a color: the calendar already spends red on scheduled
money out, amber on predicted money out, green and blue on money in, and an
amber triangle means missing or conflicting data everywhere else in the app.
An over-budget expense needed a mark that collides with none of that, and it
must not be an emoji character -- fonts off Windows do not carry one.
"""
from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

#: Canvas edge in device-independent pixels. The branch cell is one indentation
#: wide (20px by default), so a 16px canvas sits inside it with a little air;
#: the QSS ``image`` property centers it without scaling.
SIZE = 16

#: Subsamples per axis when rasterizing a shape. Cheap anti-aliasing: a
#: hard-edged 16px triangle looks visibly jagged next to Qt's own arrows.
#: Do not change it: the arrows' bytes are pinned against the old rasterizer,
#: and the proof that the two fill rules agree rests on where these samples fall.
_SUBSAMPLES = 4

#: The shapes, as closed polygons on the SIZE x SIZE canvas, vertices in order.
#: "closed" points right (collapsed, '>'), "open" points down (expanded, 'v').
#: "moneybag" is the over-budget mark: a flat tied mouth, a pinched neck, a
#: round body. Checked as an alpha map down to 12px before it was adopted --
#: a broken piggybank, the other candidate, is mush at that size, while the
#: bag keeps a recognizable silhouette.
_SHAPES = {
    "closed": ((5.5, 3.0), (5.5, 13.0), (11.0, 8.0)),
    "open": ((3.0, 5.5), (13.0, 5.5), (8.0, 11.0)),
    "moneybag": (
        (4.6, 1.6), (11.4, 1.6),        # the tie, a flat band across the mouth
        (9.4, 3.6),                     # right of the neck, pinching in
        (12.0, 5.3), (13.6, 8.4),       # shoulder flaring back out
        (13.4, 11.4), (11.6, 13.6),
        (9.4, 14.6), (6.6, 14.6),       # the base
        (4.4, 13.6), (2.6, 11.4),
        (2.4, 8.4), (4.0, 5.3),
        (6.6, 3.6),                     # left of the neck
    ),
}


def _rgb(color: str) -> tuple[int, int, int]:
    """'#rrggbb' -> (r, g, b)."""
    h = color.lstrip("#")
    if len(h) != 6:
        raise ValueError("branch indicator color must be #rrggbb, got " + color)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _inside(x: float, y: float, poly) -> bool:
    """Crossing-number point-in-polygon: is (x, y) inside ``poly``?

    Count the edges a ray going left from the point crosses; odd means inside.
    Horizontal edges contribute nothing (both endpoints fail the half-open
    ``>`` straddle test), and the half-open test also stops a vertex shared by
    two edges from being counted twice. Unlike a triangle same-side test this
    is correct for a CONCAVE outline, which the moneybag's neck is.
    """
    hit = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
        j = i
    return hit


def _coverage(px: int, py: int, poly) -> float:
    """Fraction of pixel (px, py) covered by ``poly``, by supersampling."""
    hits = 0
    step = 1.0 / _SUBSAMPLES
    for i in range(_SUBSAMPLES):
        x = px + (i + 0.5) * step
        for j in range(_SUBSAMPLES):
            y = py + (j + 0.5) * step
            if _inside(x, y, poly):
                hits += 1
    return hits / float(_SUBSAMPLES * _SUBSAMPLES)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def render_png(shape: str, color: str, size: int = SIZE) -> bytes:
    """The PNG bytes for one shape: an 8-bit RGBA image, transparent except for
    the anti-aliased polygon drawn in ``color``."""
    poly = _SHAPES[shape]
    if size != SIZE:  # scale the shape with the canvas
        k = size / float(SIZE)
        poly = tuple((x * k, y * k) for x, y in poly)
    r, g, b = _rgb(color)
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            a = int(round(_coverage(x, y, poly) * 255))
            row += bytes((r, g, b, a))
        rows.append(b"\x00" + bytes(row))  # filter type 0 (None)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + _chunk(b"IEND", b""))


def icon_path(shape: str, color: str, size: int = SIZE, *,
              prefix: str = "icon") -> Path | None:
    """Path to the on-disk PNG for (shape, color, size), generating it if missing.

    The color is part of the FILENAME, so switching themes never reuses a stale
    icon and two themes coexist happily. Returns None when the cache directory
    cannot be written (a read-only install): the caller then omits the branch
    rules entirely and Qt keeps drawing its native arrows, which is exactly the
    behavior that existed before this module -- and the calendar drops the mark
    to a text token rather than showing a broken image.

    ``prefix`` namespaces the cache file so two shapes that happen to share a
    name never collide; ``arrow_path`` keeps the original ``branch-`` prefix,
    so an existing cache directory stays valid.
    """
    from mammon import paths

    name = "%s-%s-%s-%d.png" % (prefix, shape, color.lstrip("#").lower(), size)
    try:
        cache = paths.cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / name
        if target.exists() and target.stat().st_size > 0:
            return target
        data = render_png(shape, color, size)
        # Unique temp name + os.replace: the whole test suite runs under
        # pytest-xdist, so a dozen processes can import the theme at once and
        # a half-written PNG would be a maddening intermittent failure.
        tmp = cache / ("%s.%d.tmp" % (name, os.getpid()))
        tmp.write_bytes(data)
        os.replace(str(tmp), str(target))
        return target
    except OSError:
        return None


def arrow_path(shape: str, color: str, size: int = SIZE) -> Path | None:
    """The tree branch indicator for (shape, color): :func:`icon_path` under the
    ``branch-`` cache prefix this module shipped with."""
    return icon_path(shape, color, size, prefix="branch")
