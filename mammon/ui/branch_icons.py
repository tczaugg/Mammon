"""Theme-colored expand/collapse arrows for every tree in the app.

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

#: Subsamples per axis when rasterizing the triangle. Cheap anti-aliasing: a
#: hard-edged 16px triangle looks visibly jagged next to Qt's own arrows.
_SUBSAMPLES = 4

#: The two shapes, as triangles on the SIZE x SIZE canvas.
#: "closed" points right (collapsed, '>'), "open" points down (expanded, 'v').
_SHAPES = {
    "closed": ((5.5, 3.0), (5.5, 13.0), (11.0, 8.0)),
    "open": ((3.0, 5.5), (13.0, 5.5), (8.0, 11.0)),
}


def _rgb(color: str) -> tuple[int, int, int]:
    """'#rrggbb' -> (r, g, b)."""
    h = color.lstrip("#")
    if len(h) != 6:
        raise ValueError("branch indicator color must be #rrggbb, got " + color)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _coverage(px: int, py: int, tri) -> float:
    """Fraction of pixel (px, py) covered by the triangle, by supersampling."""
    (ax, ay), (bx, by), (cx, cy) = tri
    hits = 0
    step = 1.0 / _SUBSAMPLES
    for i in range(_SUBSAMPLES):
        x = px + (i + 0.5) * step
        for j in range(_SUBSAMPLES):
            y = py + (j + 0.5) * step
            # Same-side test: all three edge cross-products share a sign.
            d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
            d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
            d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
            neg = d1 < 0 or d2 < 0 or d3 < 0
            pos = d1 > 0 or d2 > 0 or d3 > 0
            if not (neg and pos):
                hits += 1
    return hits / float(_SUBSAMPLES * _SUBSAMPLES)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def render_png(shape: str, color: str, size: int = SIZE) -> bytes:
    """The PNG bytes for one arrow: an 8-bit RGBA image, transparent except for
    the anti-aliased triangle drawn in ``color``."""
    tri = _SHAPES[shape]
    if size != SIZE:  # scale the shape with the canvas
        k = size / float(SIZE)
        tri = tuple((x * k, y * k) for x, y in tri)
    r, g, b = _rgb(color)
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            a = int(round(_coverage(x, y, tri) * 255))
            row += bytes((r, g, b, a))
        rows.append(b"\x00" + bytes(row))  # filter type 0 (None)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + _chunk(b"IEND", b""))


def arrow_path(shape: str, color: str, size: int = SIZE) -> Path | None:
    """Path to the on-disk PNG for (shape, color), generating it if missing.

    The color is part of the FILENAME, so switching themes never reuses a stale
    arrow and two themes coexist happily. Returns None when the cache directory
    cannot be written (a read-only install): the caller then omits the branch
    rules entirely and Qt keeps drawing its native arrows, which is exactly the
    behavior that existed before this module."""
    from mammon import paths

    name = "branch-%s-%s-%d.png" % (shape, color.lstrip("#").lower(), size)
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
