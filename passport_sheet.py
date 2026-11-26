#!/usr/bin/env python3
"""
passport_sheet.py — turn one photo into a print-ready PDF sheet of passport photos.

Usage:
    python passport_sheet.py photo.jpg
    python passport_sheet.py photo.jpg -o sheet.pdf --size 35x45 --count 8
    python passport_sheet.py photo.jpg --size 2x2in --sheet 4x6in --marks corners

Only dependency: Pillow  ->  pip install pillow
"""
import argparse
import math
import os
import sys

from PIL import Image, ImageDraw, ImageOps

MM_PER_INCH = 25.4

# Common photo sizes (width x height in mm)
PHOTO_PRESETS = {
    "india": (35, 45),        # Indian passport / visa / most Indian forms
    "passport": (35, 45),     # ICAO / UK / EU / AU standard
    "us": (50.8, 50.8),       # US passport & visa (2 x 2 in)
    "schengen": (35, 45),
    "stamp": (20, 25),        # small "stamp size" photo used in India
}

# Common paper sizes (width x height in mm)
PAPER_PRESETS = {
    "a4": (210, 297),
    "a5": (148, 210),
    "letter": (215.9, 279.4),
    "4x6": (101.6, 152.4),    # standard photo-lab print
    "5x7": (127, 177.8),
    "6x4": (152.4, 101.6),
}


def parse_dimensions(text, presets, what):
    """Accept '35x45', '35x45mm', '2x2in', or a preset name. Returns (w_mm, h_mm)."""
    key = text.strip().lower()
    if key in presets:
        return presets[key]

    unit = "mm"
    if key.endswith("in") or key.endswith('"'):
        unit = "in"
        key = key.rstrip('"').removesuffix("in")
    elif key.endswith("cm"):
        unit = "cm"
        key = key.removesuffix("cm")
    elif key.endswith("mm"):
        key = key.removesuffix("mm")

    parts = key.replace("*", "x").replace("X", "x").split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"Could not read {what} {text!r}. Use e.g. 35x45, 2x2in, or one of: "
            + ", ".join(sorted(presets))
        )
    try:
        w, h = float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"Could not read {what} {text!r}.")

    factor = {"mm": 1.0, "cm": 10.0, "in": MM_PER_INCH}[unit]
    return w * factor, h * factor


def fit_photo(img, target_px, mode, anchor):
    """Resize the source image to exactly target_px = (w, h)."""
    tw, th = target_px
    if mode == "stretch":
        return img.resize((tw, th), Image.LANCZOS)

    if mode == "fit":  # letterbox: whole photo kept, white bars added
        out = Image.new("RGB", (tw, th), "white")
        copy = img.copy()
        copy.thumbnail((tw, th), Image.LANCZOS)
        out.paste(copy, ((tw - copy.width) // 2, (th - copy.height) // 2))
        return out

    # default: "crop" — fill the frame, trim the overflow
    src_ratio = img.width / img.height
    dst_ratio = tw / th
    if src_ratio > dst_ratio:                       # source too wide -> trim sides
        new_w = int(round(img.height * dst_ratio))
        left = (img.width - new_w) // 2             # always centred horizontally
        box = (left, 0, left + new_w, img.height)
    else:                                           # source too tall -> trim top/bottom
        new_h = int(round(img.width / dst_ratio))
        if anchor == "top":
            top = 0
        elif anchor == "bottom":
            top = img.height - new_h
        else:
            top = (img.height - new_h) // 2
        box = (0, top, img.width, top + new_h)
    return img.crop(box).resize((tw, th), Image.LANCZOS)


def build_sheet(args):
    if not os.path.isfile(args.image):
        sys.exit(f"Error: no such file: {args.image}")

    src = Image.open(args.image)
    src = ImageOps.exif_transpose(src)          # honour phone rotation data
    if src.mode != "RGB":
        src = src.convert("RGB")

    photo_w_mm, photo_h_mm = args.size
    paper_w_mm, paper_h_mm = args.sheet
    if args.landscape:
        paper_w_mm, paper_h_mm = paper_h_mm, paper_w_mm

    dpi = args.dpi
    mm = lambda v: int(round(v / MM_PER_INCH * dpi))   # mm -> pixels

    page_w, page_h = mm(paper_w_mm), mm(paper_h_mm)
    cell_w, cell_h = mm(photo_w_mm), mm(photo_h_mm)
    margin, gap = mm(args.margin), mm(args.gap)

    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin
    if cell_w > usable_w or cell_h > usable_h:
        sys.exit("Error: photo is bigger than the printable area. Reduce --margin or --size.")

    cols = max(1, (usable_w + gap) // (cell_w + gap))
    rows = max(1, (usable_h + gap) // (cell_h + gap))
    if args.cols:
        cols = min(cols, args.cols)
    if args.rows:
        rows = min(rows, args.rows)

    capacity = cols * rows
    count = capacity if args.count in (None, 0) else min(args.count, capacity)
    if args.count and args.count > capacity:
        print(f"Note: only {capacity} photos fit on this sheet; making {capacity}.")

    # Warn if the source image is too low-res for a crisp print
    effective_dpi = min(src.width / (photo_w_mm / MM_PER_INCH),
                        src.height / (photo_h_mm / MM_PER_INCH))
    if effective_dpi < 200:
        print(f"Warning: source image is low resolution (~{effective_dpi:.0f} DPI at this "
              f"photo size). It may look soft when printed. 300+ DPI is ideal.")

    photo = fit_photo(src, (cell_w, cell_h), args.fill, args.anchor)

    # If we are not filling the sheet, only use the rows we actually need
    rows = min(rows, math.ceil(count / cols))
    cols = min(cols, count)

    # Centre the whole grid inside the page
    grid_w = cols * cell_w + (cols - 1) * gap
    grid_h = rows * cell_h + (rows - 1) * gap
    x0 = (page_w - grid_w) // 2
    y0 = (page_h - grid_h) // 2

    page = Image.new("RGB", (page_w, page_h), "white")
    draw = ImageDraw.Draw(page)
    line = max(1, int(round(dpi / 300)))          # ~1px at 300dpi, scales with dpi
    grey = (170, 170, 170)
    tick = mm(3)

    placed = 0
    for r in range(rows):
        for c in range(cols):
            if placed >= count:
                break
            x = x0 + c * (cell_w + gap)
            y = y0 + r * (cell_h + gap)
            page.paste(photo, (x, y))

            if args.marks == "border":
                draw.rectangle([x, y, x + cell_w - 1, y + cell_h - 1],
                               outline=grey, width=line)
            elif args.marks == "corners":
                for cx, cy, dx, dy in ((x, y, -1, -1), (x + cell_w, y, 1, -1),
                                       (x, y + cell_h, -1, 1), (x + cell_w, y + cell_h, 1, 1)):
                    draw.line([cx + dx * gap // 4, cy, cx + dx * tick, cy], fill=grey, width=line)
                    draw.line([cx, cy + dy * gap // 4, cx, cy + dy * tick], fill=grey, width=line)
            placed += 1

    out = args.output or os.path.splitext(args.image)[0] + "_passport_sheet.pdf"
    page.save(out, "PDF", resolution=dpi, quality=95)

    print(f"Saved: {out}")
    print(f"  Sheet      : {paper_w_mm:g} x {paper_h_mm:g} mm")
    print(f"  Photo size : {photo_w_mm:g} x {photo_h_mm:g} mm")
    print(f"  Layout     : {cols} x {rows} grid, {placed} photos, {args.gap:g} mm gap, "
          f"{args.margin:g} mm margin, {dpi} DPI")
    return out


def main():
    p = argparse.ArgumentParser(
        description="Make a print-ready PDF sheet of passport-size photos from one image.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Photo presets: " + ", ".join(sorted(PHOTO_PRESETS))
               + "\nPaper presets: " + ", ".join(sorted(PAPER_PRESETS)),
    )
    p.add_argument("image", help="source photo (jpg, png, heic-converted, ...)")
    p.add_argument("-o", "--output", help="output PDF path")
    p.add_argument("--size", default="35x45",
                   type=lambda s: parse_dimensions(s, PHOTO_PRESETS, "photo size"),
                   help="photo size, e.g. 35x45, 2x2in, or a preset (default: 35x45 mm)")
    p.add_argument("--sheet", default="a4",
                   type=lambda s: parse_dimensions(s, PAPER_PRESETS, "paper size"),
                   help="paper size, e.g. a4, letter, 4x6, 100x150 (default: a4)")
    p.add_argument("--landscape", action="store_true", help="rotate the paper")
    p.add_argument("--margin", type=float, default=8.0, help="page margin in mm (default: 8)")
    p.add_argument("--gap", type=float, default=3.0,
                   help="space between photos in mm, cutting room (default: 3)")
    p.add_argument("--count", type=int, default=None,
                   help="how many photos to place (default: fill the sheet)")
    p.add_argument("--cols", type=int, help="cap the number of columns")
    p.add_argument("--rows", type=int, help="cap the number of rows")
    p.add_argument("--dpi", type=int, default=300, help="print resolution (default: 300)")
    p.add_argument("--fill", choices=["crop", "fit", "stretch"], default="crop",
                   help="crop = fill frame and trim; fit = keep whole photo with white bars")
    p.add_argument("--anchor", choices=["center", "top", "bottom"], default="center",
                   help="which part to keep when cropping vertically (default: center)")
    p.add_argument("--marks", choices=["border", "corners", "none"], default="border",
                   help="cutting guides (default: border)")
    args = p.parse_args()
    build_sheet(args)


if __name__ == "__main__":
    main()
