"""Mammon's application icon: a pyramid of ten gold bars.

Ten bars in four courses -- 4, 3, 2, 1 -- each course nesting on the one below.
Drawn geometrically rather than illustrated, because the smallest size that
matters is 16x16 in a taskbar: at that scale only a silhouette survives, and a
symmetrical pyramid keeps its shape where a detailed subject turns to mush.

Everything is built at 2048 and downsampled with LANCZOS, which is what keeps
the diagonals clean at 32px and below.
"""
import pathlib
import sys

from PIL import Image, ImageDraw

S = 2048                      # supersampled working canvas
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "mammon" / "ui" / "icons"
DOCS = ROOT / "docs" / "images"

BG         = (36, 48, 61)     # deep slate; gold reads hot against it
BG_EDGE    = (28, 38, 49)
GOLD_TOP   = (245, 206, 88)   # lit top face
GOLD_FRONT = (214, 160, 36)   # front face
GOLD_SIDE  = (166, 120, 20)   # shadowed right face
GOLD_EDGE  = (140, 100, 14)   # seam between touching bars
SHEEN      = (255, 228, 146)  # single specular streak

BW, BH = 196, 88              # front-face width and height, in 1024-space
TAPER = 13                    # ingots are wider at the base than at the top
DX, DY = 34, 26               # depth of the top face: right, and up
GAP = 12                      # between bars in a course


def px(v):
    return v * S / 1024


def poly(d, pts, fill, outline=None, width=0):
    pts = [(px(x), px(y)) for x, y in pts]
    if outline is not None and width:
        d.polygon(pts, fill=fill, outline=outline, width=int(px(width)))
    else:
        d.polygon(pts, fill=fill)


def bar(d, x0, y0):
    """One ingot: top, right and front faces. ``(x0, y0)`` is the top-left of
    the FRONT face; the top face recedes up and to the right."""
    x1 = x0 + BW
    poly(d, [(x0 + TAPER, y0), (x1 - TAPER, y0),
             (x1 - TAPER + DX, y0 - DY), (x0 + TAPER + DX, y0 - DY)], GOLD_TOP)
    poly(d, [(x1 - TAPER, y0), (x1 - TAPER + DX, y0 - DY),
             (x1 + DX, y0 + BH - DY), (x1, y0 + BH)], GOLD_SIDE)
    # Trapezoid, wider at the base -- what makes it an ingot and not a brick.
    poly(d, [(x0 + TAPER, y0), (x1 - TAPER, y0), (x1, y0 + BH), (x0, y0 + BH)],
         GOLD_FRONT, outline=GOLD_EDGE, width=3)


def build() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=px(190), fill=BG)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=px(190), outline=BG_EDGE,
                        width=int(px(8)))

    # Courses bottom-up, so each covers the top faces of the one beneath; bars
    # left-to-right within a course, so each front face covers its neighbour's
    # right face. Painted in any other order the stack falls apart into
    # floating slabs.
    courses = (4, 3, 2, 1)
    base_y = 700
    for row, count in enumerate(courses):
        width = count * BW + (count - 1) * GAP
        x = (1024 - width) / 2 - DX / 2
        y = base_y - row * (BH + 3)
        for i in range(count):
            bar(d, x + i * (BW + GAP), y)

    # One streak on the capstone. A single highlight reads as metal; several
    # read as clutter.
    top_y = base_y - (len(courses) - 1) * (BH + 3)
    cx = (1024 - BW) / 2 - DX / 2
    poly(d, [(cx + 46, top_y - 4), (cx + 126, top_y - 4),
             (cx + 126 + DX * 0.6, top_y - DY * 0.6),
             (cx + 46 + DX * 0.6, top_y - DY * 0.6)], SHEEN)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    master = build()

    master.resize((1024, 1024), Image.LANCZOS).save(OUT / "mammon.png")
    master.resize((512, 512), Image.LANCZOS).save(DOCS / "icon.png")

    # Every size Windows asks for, in one .ico. Downsampling each from the 2048
    # master beats letting the .ico writer derive them.
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [master.resize((n, n), Image.LANCZOS) for n in sizes]
    frames[-1].save(OUT / "mammon.ico", format="ICO",
                    sizes=[(n, n) for n in sizes], append_images=frames[:-1])

    for n in (16, 24, 32, 48, 64, 128, 256, 512):      # Linux hicolor sizes
        master.resize((n, n), Image.LANCZOS).save(OUT / f"mammon-{n}.png")

    if "--previews" in sys.argv:
        # Magnified nearest-neighbour blowups of the small sizes, for judging
        # what actually survives in a taskbar. Working aids, not assets, so they
        # are opt-in and land outside the committed set.
        out = ROOT / "build" / "icon-previews"
        out.mkdir(parents=True, exist_ok=True)
        for n in (16, 32, 48):
            master.resize((n, n), Image.LANCZOS).resize(
                (n * 8, n * 8), Image.NEAREST).save(out / f"preview-{n}.png")
        print("previews ->", out)
    print("wrote", OUT / "mammon.ico", "+ PNG set")


if __name__ == "__main__":
    main()
