"""Rasterize the ``.ans`` card art to transparent-background PNGs.

Each ``src/wifit3/ui/assets/cards/*.ans`` is a small grid (<=20x10) of terminal
cells: truecolor SGR (``38;2;r;g;b`` fg / ``48;2;r;g;b`` bg) plus the block glyphs
space / full / half (upper/lower/left/right) / shade (light/medium/dark). We render
each cell's block geometry to pixels, flood-fill the pure-black canvas to transparent
from the borders (so interior black like eyes or screw-holes survives), then autocrop.
A make+model label (``_LABELS``) is drawn underneath, font auto-shrunk so a long name
never widens the card (keeps every card's on-page footprint uniform).

    uv run python scripts/ui/render_cardart.py                # all cards -> assets/cardart/
    uv run python scripts/ui/render_cardart.py card-pau06     # one card, by stem or path
    uv run python scripts/ui/render_cardart.py --scale 24     # bigger square-pixel size
    uv run python scripts/ui/render_cardart.py --stack stack-mt7921au card-awus036axml card-pau0f
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CARDS_DIR = os.path.join(_ROOT, "src", "wifit3", "ui", "assets", "cards")
_OUT_DIR = os.path.join(_ROOT, "assets", "cardart")

_SGR = re.compile(r"\x1b\[([0-9;]*)m")

# Neutral gray reads on both GitHub light and dark themes (baked-in text can't be theme-aware).
_LABEL_RGB = (150, 150, 150)

# stem (sans ``card-``) -> make+model shown under the art. Falls back to the upper-cased stem.
_LABELS = {
    # ALFA
    "awus036h":    "ALFA AWUS036H",
    "awus036nh":   "ALFA AWUS036NH",
    "awus036nha":  "ALFA AWUS036NHA",
    "awus036ach":  "ALFA AWUS036ACH",
    "awus036achm": "ALFA AWUS036ACHM",
    "awus036acm":  "ALFA AWUS036ACM",
    "awus036acs":  "ALFA AWUS036ACS",
    "awus036axml": "ALFA AWUS036AXML",
    "awus1900":    "ALFA AWUS1900",

    # Panda
    "pau06":     "Panda PAU06",
    "pau09n600": "Panda PAU09 N600",
    "pau0f":     "Panda PAU0F",
    "pau0b":     "Panda PAU0B",

    # TP-Link
    "tpwn722nv23":     "TP-Link TL-WN722N",
    "archert2u":       "TP-Link Archer T2U",
    "archert2uplus":   "TP-Link Archer T2U Plus",
    "archert2unano":   "TP-Link Archer T2U Nano",
    "archert3u":       "TP-Link Archer T3U",
    "archert3uplus":   "TP-Link Archer T3U Plus",
    "archert4u":       "TP-Link Archer T4U V3",
    "archert4uplus":   "TP-Link Archer T4U Plus",
    "archertx20uplus": "TP-Link Archer TX20U Plus",

    "asusbe93":        "ASUS USB-BE93",
    "auscomer600":     "Auscoumer 600",
    "dlinkdwa126":     "D-Link DWA-126",
    "buffalonintendo": "Buffalo Nintendo Wi-Fi",
    "lotekoo150":      "LOTEKOO 150",
    "netgeara9000":    "Netgear A9000",
}


_STACKS = {
    "stack-ar9271":    ["card-awus036nha", "card-tpwn722nv23", "card-dlinkdwa126"],
    "stack-mt7921au":  ["card-awus036axml", "card-pau0f"],
    "stack-rtl8821au": ["card-awus036acs", "card-archert2uplus", "card-archert2u"],
}


def _label_for(stem: str) -> str:
    key = stem[5:] if stem.startswith("card-") else stem
    return _LABELS.get(key, key.upper())


def _parse_grid(text: str) -> list[list[tuple[tuple[int, int, int], tuple[int, int, int], str]]]:
    """Parse the SGR stream into rows of (fg, bg, char) cells."""
    fg = (255, 255, 255)
    bg = (0, 0, 0)
    grid: list[list] = []
    for line in text.split("\n"):
        row: list = []
        pos = 0
        for m in _SGR.finditer(line):
            for ch in line[pos:m.start()]:
                row.append((fg, bg, ch))
            pos = m.end()
            params = [int(p) for p in m.group(1).split(";") if p != ""] or [0]
            i = 0
            while i < len(params):
                p = params[i]
                if p == 0:
                    fg, bg = (255, 255, 255), (0, 0, 0)
                    i += 1
                elif p == 38 and params[i + 1:i + 2] == [2]:
                    fg = (params[i + 2], params[i + 3], params[i + 4])
                    i += 5
                elif p == 48 and params[i + 1:i + 2] == [2]:
                    bg = (params[i + 2], params[i + 3], params[i + 4])
                    i += 5
                else:
                    i += 1
        for ch in line[pos:]:
            row.append((fg, bg, ch))
        grid.append(row)
    while grid and not grid[-1]:
        grid.pop()
    return grid


def _blend(fg, bg, a):
    return tuple(round(b + (f - b) * a) for f, b in zip(fg, bg))


def _fill(px, W, x0, y0, w, h, color):
    row = bytes((*color, 255)) * w
    for y in range(y0, y0 + h):
        o = (y * W + x0) * 4
        px[o:o + w * 4] = row


def _render(grid, s: int) -> tuple[bytearray, int, int]:
    """Rasterize the cell grid. ``s`` = square-pixel size; cell = s wide x 2s tall."""
    cw, ch = s, s * 2
    cols = max((len(r) for r in grid), default=0)
    rows = len(grid)
    W, H = cols * cw, rows * ch
    px = bytearray(W * H * 4)  # RGBA, all (0,0,0,0)
    for ry, row in enumerate(grid):
        for cx, (fg, bg, glyph) in enumerate(row):
            x0, y0 = cx * cw, ry * ch
            if glyph == " ":
                _fill(px, W, x0, y0, cw, ch, bg)
            elif glyph == "█":              # full block
                _fill(px, W, x0, y0, cw, ch, fg)
            elif glyph == "▀":              # upper half
                _fill(px, W, x0, y0, cw, ch, bg)
                _fill(px, W, x0, y0, cw, s, fg)
            elif glyph == "▄":              # lower half
                _fill(px, W, x0, y0, cw, ch, bg)
                _fill(px, W, x0, y0 + s, cw, s, fg)
            elif glyph == "▌":              # left half
                _fill(px, W, x0, y0, cw, ch, bg)
                _fill(px, W, x0, y0, cw // 2, ch, fg)
            elif glyph == "▐":              # right half
                _fill(px, W, x0, y0, cw, ch, bg)
                _fill(px, W, x0 + cw // 2, y0, cw - cw // 2, ch, fg)
            elif glyph == "░":              # light shade
                _fill(px, W, x0, y0, cw, ch, _blend(fg, bg, 0.25))
            elif glyph == "▒":              # medium shade
                _fill(px, W, x0, y0, cw, ch, _blend(fg, bg, 0.50))
            elif glyph == "▓":              # dark shade
                _fill(px, W, x0, y0, cw, ch, _blend(fg, bg, 0.75))
            else:
                _fill(px, W, x0, y0, cw, ch, bg)
    return px, W, H


def _key_black_from_border(px, W, H):
    """Flood-fill pure-black opaque pixels reachable from any edge -> transparent."""
    def is_black_opaque(o):
        return px[o + 3] == 255 and px[o] == 0 and px[o + 1] == 0 and px[o + 2] == 0

    stack = []
    for x in range(W):
        stack.extend([(x, 0), (x, H - 1)])
    for y in range(H):
        stack.extend([(0, y), (W - 1, y)])
    while stack:
        x, y = stack.pop()
        if not (0 <= x < W and 0 <= y < H):
            continue
        o = (y * W + x) * 4
        if not is_black_opaque(o):
            continue
        px[o + 3] = 0
        stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])


def _autocrop(px, W, H):
    minx, miny, maxx, maxy = W, H, -1, -1
    for y in range(H):
        for x in range(W):
            if px[(y * W + x) * 4 + 3] != 0:
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
    if maxx < 0:
        return px, W, H
    nw, nh = maxx - minx + 1, maxy - miny + 1
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        src = ((y + miny) * W + minx) * 4
        out[y * nw * 4:(y + 1) * nw * 4] = px[src:src + nw * 4]
    return out, nw, nh


def rasterize(ans_path: str, scale: int) -> Image.Image:
    """Read one ``.ans`` -> autocropped, black-canvas-transparent RGBA image."""
    grid = _parse_grid(open(ans_path, encoding="utf-8").read())
    px, W, H = _render(grid, scale)
    _key_black_from_border(px, W, H)
    px, W, H = _autocrop(px, W, H)
    return Image.frombytes("RGBA", (W, H), bytes(px))


def _fit_font(draw: ImageDraw.ImageDraw, label: str, max_w: int, base: int, floor: int = 13):
    """Largest default font (<= base) whose ``label`` fits ``max_w``, down to ``floor``."""
    for size in range(base, floor, -1):
        font = ImageFont.load_default(size=size)
        if draw.textlength(label, font=font) <= max_w:
            return font
    return ImageFont.load_default(size=floor)


def _with_label(card: Image.Image, label: str, scale: int) -> Image.Image:
    """Return ``card`` with ``label`` centered beneath it (transparent margins)."""
    pad = 4
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    font = _fit_font(probe, label, max(card.width - 2 * pad, 40), base=round(scale * 1.6))
    tw = round(probe.textlength(label, font=font))
    ascent, descent = font.getmetrics()
    gap = round(scale * 0.5)
    W = max(card.width, tw + 2 * pad)
    H = card.height + gap + ascent + descent + pad
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(card, ((W - card.width) // 2, 0))
    ImageDraw.Draw(out).text(((W - tw) // 2, card.height + gap), label, font=font, fill=(*_LABEL_RGB, 255))
    return out


def _labeled(ans_path: str, scale: int) -> Image.Image:
    stem = os.path.splitext(os.path.basename(ans_path))[0]
    return _with_label(rasterize(ans_path, scale), _label_for(stem), scale)


def _stack_vertical(paths: list[str], scale: int, gap: int) -> Image.Image:
    """Stack each labeled card top-to-bottom, centered, ``gap`` px apart."""
    cards = [_labeled(p, scale) for p in paths]
    W = max(c.width for c in cards)
    H = sum(c.height for c in cards) + gap * (len(cards) - 1)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    y = 0
    for c in cards:
        out.alpha_composite(c, ((W - c.width) // 2, y))
        y += c.height + gap
    return out


def convert(ans_path: str, out_dir: str, scale: int) -> tuple[str, int, int]:
    img = _labeled(ans_path, scale)
    stem = os.path.splitext(os.path.basename(ans_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, stem + ".png")
    img.save(out)
    return out, img.width, img.height


def _resolve(names: list[str]) -> list[str]:
    if not names:
        return sorted(glob.glob(os.path.join(_CARDS_DIR, "*.ans")))
    paths = []
    for n in names:
        if os.path.isfile(n):
            paths.append(n)
        else:
            stem = n if n.endswith(".ans") else n + ".ans"
            paths.append(os.path.join(_CARDS_DIR, stem))
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cards", nargs="*", help="card stem(s) or path(s); default: all")
    ap.add_argument("--scale", type=int, default=16, help="square-pixel size in px (default 16)")
    ap.add_argument("--out", default=_OUT_DIR, help="output dir (default assets/cardart)")
    ap.add_argument("--stack", metavar="NAME", help="stack the given cards vertically into NAME.png")
    ap.add_argument("--gap", type=int, default=28, help="gap (px) between stacked cards (default 28)")
    args = ap.parse_args(argv)
    if args.stack:
        paths = _resolve(args.cards)
        img = _stack_vertical(paths, args.scale, args.gap)
        os.makedirs(args.out, exist_ok=True)
        out = os.path.join(args.out, args.stack + ".png")
        img.save(out)
        print(f"stack {[os.path.basename(p) for p in paths]} -> {os.path.relpath(out, _ROOT)}  ({img.width}x{img.height})")
        return
    for p in _resolve(args.cards):
        if not os.path.isfile(p):
            print(f"skip (missing): {p}", file=sys.stderr)
            continue
        out, w, h = convert(p, args.out, args.scale)
        print(f"{os.path.basename(p):28s} -> {os.path.relpath(out, _ROOT)}  ({w}x{h})")
    if not args.cards:                                  # a full run refreshes the stacks too
        for name, stems in _STACKS.items():
            img = _stack_vertical(_resolve(stems), args.scale, args.gap)
            out = os.path.join(args.out, name + ".png")
            img.save(out)
            print(f"{name:28s} -> {os.path.relpath(out, _ROOT)}  ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
