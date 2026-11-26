#!/usr/bin/env python3
"""
passport_studio.py — build a print-ready PDF sheet of passport photos from one
or several photos, with a live preview and an interactive crop.

    python passport_studio.py                       # GUI
    python passport_studio.py a.jpg b.jpg           # GUI, photos preloaded
    python passport_studio.py --cli a.jpg b.jpg     # text-prompt mode

Each photo gets its own crop and its own share of the sheet — say one row of
the first, two rows of the second, one row of the third. If the total spills
past a single sheet, extra pages are added to the same PDF.

Requires : Pillow          ->  pip install pillow
Optional : opencv-python   ->  pip install opencv-python   (auto face detection)
           pillow-heif     ->  pip install pillow-heif      (iPhone .heic files)
Tkinter ships with Python on Windows/macOS; on Linux: sudo apt install python3-tk
"""

import argparse
import math
import os
import queue
import sys
import threading

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

try:                                    # iPhone HEIC support, if installed
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

BUILD = "2026-09-01-g"
_UNSET = object()
MM_PER_INCH = 25.4

PHOTO_PRESETS = {                        # width x height in mm
    "India passport 35x45": (35, 45),
    "Indian stamp 20x25": (20, 25),
    "US passport 51x51": (50.8, 50.8),
    "Schengen/UK 35x45": (35, 45),
    "China visa 33x48": (33, 48),
    "Custom 30x40": (30, 40),
}

PAPER_PRESETS = {
    "A4 210x297": (210, 297),
    "A5 148x210": (148, 210),
    "Letter 216x279": (215.9, 279.4),
    "4x6 photo paper": (101.6, 152.4),
    "5x7 photo paper": (127, 177.8),
}

IMAGE_EXTS = ("jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff", "heic")


# --------------------------------------------------------------------------
#  Core: sizing and cropping
# --------------------------------------------------------------------------

def parse_dimensions(text, presets, what="size"):
    """Accept a preset label, or '35x45', '35x45mm', '2x2in', '10x15cm'."""
    raw = text.strip()
    if raw in presets:
        return presets[raw]
    key = raw.lower()
    for label, dims in presets.items():
        if key == label.lower():
            return dims

    unit = "mm"
    if key.endswith('"') or key.endswith("in"):
        unit, key = "in", key.rstrip('"').removesuffix("in")
    elif key.endswith("cm"):
        unit, key = "cm", key.removesuffix("cm")
    elif key.endswith("mm"):
        key = key.removesuffix("mm")

    parts = key.replace("*", "x").replace("×", "x").split("x")
    if len(parts) != 2:
        raise ValueError(f"Could not read {what}: {text!r}")
    w, h = float(parts[0]), float(parts[1])
    factor = {"mm": 1.0, "cm": 10.0, "in": MM_PER_INCH}[unit]
    if w <= 0 or h <= 0:
        raise ValueError(f"{what} must be positive")
    return w * factor, h * factor


def load_image(path):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)           # honour phone rotation
    return img.convert("RGB")


def detect_face(img):
    """Return (x, y, w, h) of the largest face, or None. Needs opencv-python."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        small = img.copy()
        small.thumbnail((900, 900))
        scale = img.width / small.width
        gray = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2GRAY)
        gray = cv2.equalizeHist(gray)
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(60, 60))
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return (x * scale, y * scale, w * scale, h * scale)
    except Exception:
        return None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def auto_crop_box(img_w, img_h, aspect, face=None):
    """Best-guess crop rectangle [x, y, w, h] in source pixels. aspect = w/h."""
    if face:
        fx, fy, fw, fh = face
        # Aim for the face filling about half the frame height, eyes above
        # centre — close to most passport specifications.
        h = fh / 0.50
        w = h * aspect
        if w > img_w:
            w, h = img_w, img_w / aspect
        if h > img_h:
            h, w = img_h, img_h * aspect
        cx = fx + fw / 2
        cy = fy + fh / 2 + 0.12 * h
    else:
        w = min(img_w, img_h * aspect)
        h = w / aspect
        cx, cy = img_w / 2, img_h / 2
    x = clamp(cx - w / 2, 0, img_w - w)
    y = clamp(cy - h / 2, 0, img_h - h)
    return [x, y, w, h]


def fit_box(box, img_w, img_h, aspect):
    """Force a box to the right aspect ratio and keep it inside the image."""
    x, y, w, h = box
    w = clamp(w, 24, img_w)
    h = w / aspect
    if h > img_h:
        h = img_h
        w = h * aspect
    x = clamp(x, 0, img_w - w)
    y = clamp(y, 0, img_h - h)
    return [x, y, w, h]


def effective_dpi(box, photo_mm):
    """Real print resolution given how much of the source the crop uses."""
    return min(box[2] / (photo_mm[0] / MM_PER_INCH),
               box[3] / (photo_mm[1] / MM_PER_INCH))


BG_PRESETS = {
    "Keep original": None,
    "White": (255, 255, 255),
    "Off-white": (246, 246, 246),
    "Light grey": (220, 220, 220),
    "Light blue": (198, 219, 240),
    "Blue": (120, 170, 225),
    "Light red": (240, 200, 200),
    "Red": (215, 90, 90),
    "Cream": (250, 243, 224),
    "Light green": (205, 232, 205),
}

# Short labels for the photo list column
BG_SHORT = {None: "—"}
for _label, _rgb in BG_PRESETS.items():
    if _rgb is not None:
        BG_SHORT[_rgb] = _label


def _mask_plausible(alpha):
    """Reject masks that keep everything or almost nothing."""
    try:
        import numpy as np
        small = np.asarray(alpha.resize((90, 120)))
        fg = float((small > 128).mean())
        return 0.10 < fg < 0.93, fg
    except Exception:
        return True, -1.0


def _clean_mask(mask_arr):
    """Keep the largest blob, close gaps, fill holes. mask_arr is 0/255 uint8."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return mask_arr
    m = cv2.medianBlur(mask_arr, 5)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, labels, stats, _ = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
    if n > 1:
        biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        m = np.where(labels == biggest, 255, 0).astype(np.uint8)
    holes = m.copy()
    hh, ww = holes.shape
    ff = np.zeros((hh + 2, ww + 2), np.uint8)
    cv2.floodFill(holes, ff, (0, 0), 255)
    return (m | cv2.bitwise_not(holes)).astype(np.uint8)


def _grabcut_alpha(img, face=None):
    """Cutout via OpenCV GrabCut. Returns an 'L' mask or None."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        work = img.copy()
        work.thumbnail((700, 700))               # GrabCut is slow at full size
        arr = np.array(work)[:, :, ::-1].copy()  # RGB -> BGR
        h, w = arr.shape[:2]

        # The rectangle must leave a real border of definite background outside
        # it, otherwise GrabCut has nothing to learn from and marks the whole
        # frame as foreground — which looks exactly like "nothing happened".
        bx, by = int(w * 0.08), int(h * 0.04)
        if face:
            sx, sy = work.width / img.width, work.height / img.height
            fx, fy, fw, fh = face[0] * sx, face[1] * sy, face[2] * sx, face[3] * sy
            x0 = int(clamp(fx - 1.1 * fw, bx, w * 0.35))
            x1 = int(clamp(fx + fw + 1.1 * fw, w * 0.65, w - bx))
            y0 = int(clamp(fy - 0.85 * fh, by, h * 0.30))
        else:
            x0, x1, y0 = bx, w - bx, by
        rect = (x0, y0, max(10, x1 - x0), max(10, h - by - y0))

        mask = np.zeros((h, w), np.uint8)
        bgd = np.zeros((1, 65), np.float64)
        fgd = np.zeros((1, 65), np.float64)
        cv2.grabCut(arr, mask, rect, bgd, fgd, 6, cv2.GC_INIT_WITH_RECT)
        alpha = np.where((mask == 2) | (mask == 0), 0, 255).astype(np.uint8)
        alpha = _clean_mask(alpha)
        return Image.fromarray(alpha, "L").resize(img.size, Image.BILINEAR)
    except Exception:
        return None


def _colorkey_alpha(img, tol=42):
    """
    Cutout by colour distance from the border. Fast and reliable when the
    subject was shot against a reasonably plain wall, useless otherwise.
    """
    try:
        import numpy as np
    except Exception:
        return None
    try:
        work = img.copy()
        work.thumbnail((700, 700))
        a = np.asarray(work).astype(np.int16)
        h, w, _ = a.shape
        e = max(6, int(min(h, w) * 0.02))
        edges = np.concatenate([a[:e].reshape(-1, 3), a[-e:].reshape(-1, 3),
                                a[:, :e].reshape(-1, 3), a[:, -e:].reshape(-1, 3)])
        bg = np.median(edges, axis=0)
        spread = float(np.median(np.abs(edges - bg).sum(axis=1)))
        if spread > 90:                  # background is not plain enough
            return None
        dist = np.sqrt(((a - bg) ** 2).sum(axis=2))
        alpha = (dist > tol).astype(np.uint8) * 255
        alpha = _clean_mask(alpha)
        return Image.fromarray(alpha, "L").resize(img.size, Image.BILINEAR)
    except Exception:
        return None


REMBG_WORKER = r"""
import sys
from PIL import Image
src, dst = sys.argv[1], sys.argv[2]
from rembg import remove
out = remove(Image.open(src).convert("RGBA"))
out.getchannel("A").save(dst)
"""


def _rembg_alpha(img, timeout=300):
    """
    Run rembg in a SEPARATE PROCESS and return (alpha, note).

    rembg drags in pymatting -> numba -> llvmlite. On some Python versions
    that stack aborts the whole process at the C level (SIGABRT), which no
    try/except can catch. Isolating it in a subprocess means a crash costs us
    one failed exit code instead of the entire application.
    """
    import importlib.util
    import subprocess
    import tempfile

    # find_spec only LOOKS for the module — it never executes it. A plain
    # `import rembg` here would run numba/llvmlite in our own process, which
    # is exactly the thing that can abort the interpreter.
    try:
        if importlib.util.find_spec("rembg") is None:
            return None, "rembg not installed"
    except Exception:
        return None, "rembg not installed"

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.png")
        dst = os.path.join(td, "out.png")
        script = os.path.join(td, "rembg_worker.py")
        work = img
        if max(img.size) > 2000:               # bound memory and runtime
            work = img.copy()
            work.thumbnail((2000, 2000))
        work.save(src)
        with open(script, "w") as fh:
            fh.write(REMBG_WORKER)
        try:
            r = subprocess.run([sys.executable, script, src, dst],
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"rembg timed out after {timeout}s"
        except Exception as e:
            return None, f"could not start rembg ({type(e).__name__})"

        if r.returncode != 0 or not os.path.exists(dst):
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            last = err.splitlines()[-1] if err else ""
            if r.returncode < 0:
                return None, (f"rembg crashed the interpreter (signal "
                              f"{-r.returncode}) — see the notes on numba/"
                              f"llvmlite below")
            if "onnxruntime" in err:
                return None, 'rembg has no backend — pip install "rembg[cpu]"'
            return None, f"rembg exited {r.returncode}: {last[:160]}"

        try:
            alpha = Image.open(dst).convert("L")
            alpha.load()
        except Exception as e:
            return None, f"rembg output unreadable ({type(e).__name__})"
        if alpha.size != img.size:
            alpha = alpha.resize(img.size, Image.BILINEAR)
        return alpha, "rembg"


def cutout_alpha(img, face=None):
    """
    Separate subject from background.

    Returns (alpha 'L' mask or None, description of what happened). Tries
    rembg first (a segmentation model built for this, much better around
    hair), then GrabCut, then plain-background colour keying. Each result is
    sanity-checked, because a mask that keeps the entire frame is worse than
    an honest failure — it silently looks like the feature did nothing.
    """
    notes = []
    a, note = _rembg_alpha(img)
    if a is not None:
        ok, fg = _mask_plausible(a)
        if ok:
            return a, "rembg"
        notes.append(f"rembg gave an implausible mask ({fg:.0%} kept)")
    else:
        notes.append(note)

    a = _grabcut_alpha(img, face)
    if a is not None:
        ok, fg = _mask_plausible(a)
        if ok:
            return a, "OpenCV GrabCut"
        notes.append(f"GrabCut kept {fg:.0%} of the frame")
    else:
        notes.append("GrabCut unavailable")

    a = _colorkey_alpha(img)
    if a is not None:
        ok, fg = _mask_plausible(a)
        if ok:
            return a, "colour key"
        notes.append(f"colour key kept {fg:.0%} of the frame")
    else:
        notes.append("background too busy for colour keying")

    return None, "; ".join(notes)


DEFAULT_ADJ = {"brightness": 1.0, "contrast": 1.0, "saturation": 1.0,
               "sharpness": 1.0, "warmth": 0.0, "gray": False, "auto": False}

ADJ_LIMITS = {"brightness": (0.4, 1.8, 0.05), "contrast": (0.4, 1.8, 0.05),
              "saturation": (0.0, 2.0, 0.05), "sharpness": (0.0, 3.0, 0.10),
              "warmth": (-30.0, 30.0, 1.0)}

ADJ_PRESETS = {
    "None": {},
    "Auto levels": {"auto": True},
    "Brighten": {"brightness": 1.12, "contrast": 1.05},
    "Soft skin": {"brightness": 1.06, "sharpness": 0.75, "saturation": 0.95},
    "Punchy": {"contrast": 1.20, "saturation": 1.15, "sharpness": 1.30},
    "Warm": {"warmth": 10.0},
    "Cool": {"warmth": -10.0},
    "Document B&W": {"gray": True, "contrast": 1.15},
}


def auto_levels(img):
    """
    Stretch brightness only, leaving colour alone.

    PIL's autocontrast stretches R, G and B independently, so any colour cast
    in the original gets amplified — which is why a warm wall turns green.
    Doing it on the luma channel of YCbCr avoids that entirely.
    """
    y, cb, cr = img.convert("YCbCr").split()
    y = ImageOps.autocontrast(y, cutoff=1)
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB")


def apply_adjustments(img, adj):
    """Apply the tone/colour adjustments. Runs BEFORE the background is
    composited, so a replaced background keeps exactly the colour chosen."""
    out = img
    if adj.get("auto"):
        out = auto_levels(out)
    if adj.get("gray"):
        out = ImageOps.grayscale(out).convert("RGB")
    w = float(adj.get("warmth", 0))
    if abs(w) > 0.5:                      # shift red up and blue down, or vice versa
        f = 1.0 + w / 100.0
        r, g, b = out.split()
        r = r.point(lambda v: min(255, int(v * f)))
        b = b.point(lambda v: min(255, int(v / f)))
        out = Image.merge("RGB", (r, g, b))
    for key, cls in (("brightness", ImageEnhance.Brightness),
                     ("contrast", ImageEnhance.Contrast),
                     ("saturation", ImageEnhance.Color),
                     ("sharpness", ImageEnhance.Sharpness)):
        v = float(adj.get(key, 1.0))
        if abs(v - 1.0) > 1e-3:
            out = cls(out).enhance(v)
    return out


class Photo:
    """One source image plus its crop and how much of the sheet it gets."""

    def __init__(self, path, img, aspect):
        self.path = path
        self.img = img
        self.face = detect_face(img)
        self.box = auto_crop_box(img.width, img.height, aspect, self.face)
        self.qty = 1                     # rows, or copies, depending on mode
        self.alpha = None                # cutout mask, once computed
        self.cut_note = ""               # which method worked, or why none did
        self.adj = dict(DEFAULT_ADJ)     # per-photo tone/colour adjustments
        self.bg = None                   # per-photo background colour
        self.feather = 2                 # per-photo cutout edge softness
        self._cache = None               # (key, composited image)

    @property
    def name(self):
        return os.path.basename(self.path)

    def refit(self, aspect):
        self.box = fit_box(self.box, self.img.width, self.img.height, aspect)

    def adjusted(self):
        return self.adj != DEFAULT_ADJ

    def display(self, bg_color=_UNSET, feather=_UNSET):
        """The image as it should appear: adjustments applied, then the
        background swapped if one was chosen and a cutout exists.

        With no arguments it uses this photo's own background and softness,
        so different photos on one sheet can have different backgrounds."""
        if bg_color is _UNSET:
            bg_color = self.bg
        if feather is _UNSET:
            feather = self.feather
        key = (bg_color, feather, tuple(sorted(self.adj.items())))
        if self._cache and self._cache[0] == key:
            return self._cache[1]

        out = apply_adjustments(self.img, self.adj) if self.adjusted() else self.img
        if bg_color is not None and self.alpha is not None:
            a = self.alpha
            if feather:
                a = a.filter(ImageFilter.GaussianBlur(feather))
            out = Image.composite(out, Image.new("RGB", out.size, bg_color), a)
        self._cache = (key, out)
        return out


# --------------------------------------------------------------------------
#  Core: sheet layout
# --------------------------------------------------------------------------

def compute_grid(cfg):
    """Pixel geometry of the sheet. Returns None if the photo cannot fit."""
    dpi = cfg["dpi"]
    px = lambda v_mm: int(round(v_mm / MM_PER_INCH * dpi))

    paper_w, paper_h = cfg["paper_mm"]
    if cfg.get("landscape"):
        paper_w, paper_h = paper_h, paper_w
    photo_w, photo_h = cfg["photo_mm"]

    page_w, page_h = px(paper_w), px(paper_h)
    cell_w, cell_h = px(photo_w), px(photo_h)
    margin, gap = px(cfg["margin_mm"]), px(cfg["gap_mm"])

    # How many fit is worked out in millimetres, NOT in pixels. Doing it in
    # pixels makes the answer depend on the DPI you happen to render at, so a
    # low-resolution preview could disagree with the printed sheet.
    usable_w_mm = paper_w - 2 * cfg["margin_mm"]
    usable_h_mm = paper_h - 2 * cfg["margin_mm"]
    if photo_w > usable_w_mm or photo_h > usable_h_mm:
        return None

    eps = 1e-9
    gap_mm = cfg["gap_mm"]
    cols = int((usable_w_mm + gap_mm + eps) // (photo_w + gap_mm))
    rows = int((usable_h_mm + gap_mm + eps) // (photo_h + gap_mm))
    return dict(page=(page_w, page_h), cell=(cell_w, cell_h), gap=gap,
                cols=max(1, cols), rows=max(1, rows), dpi=dpi)


def plan_pages(quantities, cols, rows, mode="rows"):
    """
    Decide where every photo goes.

    mode 'rows'   : each photo takes whole rows, starting on a fresh row.
    mode 'copies' : each photo takes that many individual slots, flowing on.

    Returns a list of pages; each page is a list of (row, col, photo_index).
    """
    pages, page = [], []
    r = c = 0

    def new_page():
        nonlocal pages, page, r, c
        if page:
            pages.append(page)
        page, r, c = [], 0, 0

    for idx, qty in enumerate(quantities):
        qty = max(0, int(qty))
        if qty == 0:
            continue
        if mode == "rows":
            if c != 0:                       # finish any half-filled row first
                c = 0
                r += 1
            for _ in range(qty):
                if r >= rows:
                    new_page()
                for cc in range(cols):
                    page.append((r, cc, idx))
                r += 1
            c = 0
        else:
            for _ in range(qty):
                if r >= rows:
                    new_page()
                page.append((r, c, idx))
                c += 1
                if c >= cols:
                    c = 0
                    r += 1
    if page:
        pages.append(page)
    return pages


def render_pages(photos, cfg, dpi=None):
    """Render every sheet. Returns (list of PIL images, info dict)."""
    grid = compute_grid(dict(cfg, dpi=dpi or cfg["dpi"]))
    if grid is None or not photos:
        return [], None

    cols, rows = grid["cols"], grid["rows"]
    cell_w, cell_h = grid["cell"]
    gap = grid["gap"]
    page_w, page_h = grid["page"]
    marks = cfg.get("marks", "border")
    # Guides need real thickness or the printer drops them, and they sit just
    # OUTSIDE the photo so they land on white paper instead of on the image.
    line = max(1, int(round(0.18 / MM_PER_INCH * grid["dpi"])))
    tick = int(round(3.5 / MM_PER_INCH * grid["dpi"]))
    grey = (110, 110, 110)

    plan = plan_pages([p.qty for p in photos], cols, rows, cfg.get("mode", "rows"))
    if not plan:
        return [], dict(grid, pages=0, total=0, plan=[])

    # Crop and resize each photo once, not once per slot
    cells = []
    for p in photos:
        x, y, w, h = p.box
        src = p.display()                 # each photo's own background/adjustments
        cells.append(src.crop((int(x), int(y), int(x + w), int(y + h)))
                        .resize((cell_w, cell_h), Image.LANCZOS))

    sheets = []
    for slots in plan:
        used_rows = max(s[0] for s in slots) + 1
        used_cols = max(s[1] for s in slots) + 1
        grid_w = used_cols * cell_w + (used_cols - 1) * gap
        grid_h = used_rows * cell_h + (used_rows - 1) * gap
        ox = (page_w - grid_w) // 2
        oy = (page_h - grid_h) // 2

        sheet = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(sheet)
        for r, c, idx in slots:
            x = ox + c * (cell_w + gap)
            y = oy + r * (cell_h + gap)
            sheet.paste(cells[idx], (x, y))
            if marks == "border":
                o = line                      # sit in the gutter, not on the photo
                draw.rectangle([x - o, y - o, x + cell_w - 1 + o, y + cell_h - 1 + o],
                               outline=grey, width=line)
            elif marks == "corners":
                o = line
                for px_, py_, dx, dy in ((x - o, y - o, -1, -1),
                                         (x + cell_w + o, y - o, 1, -1),
                                         (x - o, y + cell_h + o, -1, 1),
                                         (x + cell_w + o, y + cell_h + o, 1, 1)):
                    draw.line([px_, py_, px_ + dx * tick, py_], fill=grey, width=line)
                    draw.line([px_, py_, px_, py_ + dy * tick], fill=grey, width=line)
        sheets.append(sheet)

    info = dict(grid, pages=len(sheets), total=sum(len(s) for s in plan), plan=plan)
    return sheets, info


def save_pdf(photos, cfg, path):
    sheets, info = render_pages(photos, cfg)
    if not sheets:
        raise ValueError("Nothing to print — check the photo size and quantities.")
    sheets[0].save(path, "PDF", save_all=True, append_images=sheets[1:],
                   resolution=cfg["dpi"], quality=95)
    return info


# --------------------------------------------------------------------------
#  GUI
# --------------------------------------------------------------------------

def run_gui(initial_paths=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from PIL import ImageTk

    CROP_W, CROP_H = 380, 430
    SHEET_W, SHEET_H = 290, 470

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"Passport Photo Sheet  —  build {BUILD}")
            self.geometry("1300x820")
            self.minsize(1040, 620)

            # ttk's default Treeview row height is a fixed pixel value that does
            # not follow the system font, so rows overlap on large-font or HiDPI
            # setups. Derive it from the actual font metrics instead.
            from tkinter import font as tkfont
            f = tkfont.nametofont("TkDefaultFont")
            style = ttk.Style(self)
            style.configure("Treeview",
                            rowheight=max(22, f.metrics("linespace") + 8))

            self.photos = []
            self.sel = None                  # index into self.photos
            self._preview_job = None
            self._drag = None
            self._disp = None
            self._page = 0
            self._sheets = []
            self._custom_bg = (255, 255, 255)
            self._cutting = False
            self._cut_warned = False
            self._cut_queue = queue.Queue()
            self._cut_thread = None

            pics = os.path.join(os.path.expanduser("~"), "Pictures")
            self.last_dir = pics if os.path.isdir(pics) else os.getcwd()

            self._build_ui()
            if initial_paths:
                self.add_images(initial_paths)
            else:
                self.status.set("Add one or more photos to begin.")

        # ---------------- layout ----------------
        def _build_ui(self):
            root = ttk.Frame(self, padding=10)
            root.pack(fill="both", expand=True)

            body = ttk.Frame(root)
            body.pack(fill="both", expand=True)

            # --- crop pane ---
            left = ttk.LabelFrame(body, text="Crop  ·  drag to move, scroll to zoom",
                                  padding=8)
            left.pack(side="left", fill="both", expand=True)
            self.crop_canvas = tk.Canvas(left, width=CROP_W, height=CROP_H,
                                         bg="#2b2b2b", highlightthickness=0)
            self.crop_canvas.pack(fill="both", expand=True)
            self.crop_canvas.bind("<ButtonPress-1>", self.on_press)
            self.crop_canvas.bind("<B1-Motion>", self.on_drag)
            self.crop_canvas.bind("<MouseWheel>", self.on_wheel)            # Win/macOS
            self.crop_canvas.bind("<Button-4>", lambda e: self.zoom(0.94))  # Linux
            self.crop_canvas.bind("<Button-5>", lambda e: self.zoom(1.06))
            self.crop_canvas.bind("<Configure>", lambda e: self.draw_crop())

            cb = ttk.Frame(left)
            cb.pack(fill="x", pady=(8, 0))
            ttk.Button(cb, text="Auto fit", command=self.auto_fit).pack(side="left")
            ttk.Button(cb, text="Detect face",
                       command=self.face_fit).pack(side="left", padx=6)
            ttk.Button(cb, text="−", width=3,
                       command=lambda: self.zoom(1.08)).pack(side="left")
            ttk.Button(cb, text="+", width=3,
                       command=lambda: self.zoom(0.92)).pack(side="left", padx=(4, 8))
            self.guides = tk.BooleanVar(value=True)
            ttk.Checkbutton(cb, text="Eye guide", variable=self.guides,
                            command=self.draw_crop).pack(side="left")
            ttk.Button(cb, text="Apply crop to all",
                       command=self.apply_crop_all).pack(side="right")

            # --- middle column: photo list + settings + adjustments ---
            # There is more here than fits on a short screen, so the column
            # scrolls instead of the window growing past the display.
            mid_outer = ttk.Frame(body)
            mid_outer.pack(side="left", fill="y", padx=10)
            mid_canvas = tk.Canvas(mid_outer, width=380, highlightthickness=0,
                                   bg=self.cget("bg"))
            mid_sb = ttk.Scrollbar(mid_outer, orient="vertical",
                                   command=mid_canvas.yview)
            mid_canvas.configure(yscrollcommand=mid_sb.set)
            mid_canvas.pack(side="left", fill="y", expand=True)
            mid_sb.pack(side="right", fill="y")

            mid = ttk.Frame(mid_canvas)
            mid_win = mid_canvas.create_window((0, 0), window=mid, anchor="nw")

            def _mid_resize(_=None):
                # Widen the column to whatever the widgets actually need at the
                # user's font size, instead of a hard-coded pixel width that
                # silently clips labels on large-font or HiDPI displays.
                mid_canvas.configure(scrollregion=mid_canvas.bbox("all"))
                want = mid.winfo_reqwidth()
                if want > 10 and want != int(mid_canvas.cget("width")):
                    mid_canvas.configure(width=min(want, 560))
            mid.bind("<Configure>", _mid_resize)
            mid_canvas.bind(
                "<Configure>",
                lambda e: mid_canvas.itemconfigure(
                    mid_win, width=max(e.width, mid.winfo_reqwidth())))

            def _mid_wheel(delta):
                mid_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
            for w in (mid_canvas, mid):
                w.bind("<MouseWheel>", lambda e: _mid_wheel(e.delta))
                w.bind("<Button-4>", lambda e: _mid_wheel(1))
                w.bind("<Button-5>", lambda e: _mid_wheel(-1))

            self._build_list(mid)
            self._build_settings(mid)
            self._build_background(mid)
            self._build_adjust(mid)

            # --- sheet preview ---
            right = ttk.LabelFrame(body, text="Sheet preview", padding=8)
            right.pack(side="left", fill="both", expand=True)
            self.sheet_canvas = tk.Canvas(right, width=SHEET_W, height=SHEET_H,
                                          bg="#dedede", highlightthickness=0)
            self.sheet_canvas.pack(fill="both", expand=True)
            self.sheet_canvas.bind("<Configure>", lambda e: self.schedule())

            nav = ttk.Frame(right)
            nav.pack(fill="x", pady=(8, 0))
            self.prev_btn = ttk.Button(nav, text="◀", width=3,
                                       command=lambda: self.flip(-1))
            self.prev_btn.pack(side="left")
            self.page_lbl = ttk.Label(nav, text="", anchor="center")
            self.page_lbl.pack(side="left", fill="x", expand=True)
            self.next_btn = ttk.Button(nav, text="▶", width=3,
                                       command=lambda: self.flip(1))
            self.next_btn.pack(side="right")
            self.layout_lbl = ttk.Label(right, text="", justify="center",
                                        wraplength=250, anchor="center")
            self.layout_lbl.pack(pady=(6, 0))

            foot = ttk.Frame(root)
            foot.pack(fill="x", pady=(10, 0))
            self.status = tk.StringVar()
            ttk.Label(foot, textvariable=self.status,
                      foreground="#444").pack(side="left")
            ttk.Button(foot, text="Save PDF…", command=self.save).pack(side="right")

        def _build_list(self, parent):
            box = ttk.LabelFrame(parent, text="Photos on the sheet", padding=8)
            box.pack(fill="x")

            self.tree = ttk.Treeview(box, columns=("qty", "bg"), height=6,
                                     selectmode="browse")
            self.tree.heading("#0", text="Photo")
            self.tree.heading("qty", text="Rows")
            self.tree.heading("bg", text="BG")
            self.tree.column("#0", width=150, stretch=True)
            self.tree.column("qty", width=48, anchor="center", stretch=False)
            self.tree.column("bg", width=96, anchor="center", stretch=False)
            self.tree.pack(fill="x")
            self.tree.bind("<<TreeviewSelect>>", self.on_select)

            b1 = ttk.Frame(box)
            b1.pack(fill="x", pady=(6, 0))
            ttk.Button(b1, text="Add…", command=self.pick_images).pack(side="left")
            ttk.Button(b1, text="Remove",
                       command=self.remove_sel).pack(side="left", padx=4)
            ttk.Button(b1, text="↑", width=3,
                       command=lambda: self.move(-1)).pack(side="left")
            ttk.Button(b1, text="↓", width=3,
                       command=lambda: self.move(1)).pack(side="left", padx=(4, 0))

            # Own row: with large system fonts this label will not fit beside
            # the buttons above, and packing silently clips whatever overflows.
            b1b = ttk.Frame(box)
            b1b.pack(fill="x", pady=(4, 0))
            ttk.Button(b1b, text="Share sheet evenly between photos",
                       command=self.share_evenly).pack(fill="x")

            b2 = ttk.Frame(box)
            b2.pack(fill="x", pady=(8, 0))
            self.qty_lbl = ttk.Label(b2, text="Rows for this photo")
            self.qty_lbl.pack(side="left")
            self.v_qty = tk.IntVar(value=1)
            self.qty_spin = ttk.Spinbox(b2, from_=0, to=99, width=5,
                                        textvariable=self.v_qty,
                                        command=self.on_qty)
            self.qty_spin.pack(side="right")
            self.v_qty.trace_add("write", lambda *a: self.on_qty())

            b3 = ttk.Frame(box)
            b3.pack(fill="x", pady=(6, 0))
            ttk.Label(b3, text="Count by").pack(side="left")
            self.v_mode = tk.StringVar(value="rows")
            m = ttk.Combobox(b3, textvariable=self.v_mode, width=10, state="readonly",
                             values=["rows", "copies"])
            m.pack(side="right")
            m.bind("<<ComboboxSelected>>", lambda e: self.on_mode())

        def _build_settings(self, parent):
            p = ttk.LabelFrame(parent, text="Sheet settings", padding=10)
            p.pack(fill="x", pady=(10, 0))
            r = 0

            def row(label, widget):
                nonlocal r
                ttk.Label(p, text=label).grid(row=r, column=0, sticky="w", pady=3)
                widget.grid(row=r, column=1, sticky="ew", pady=3, padx=(8, 0))
                r += 1

            self.v_photo = tk.StringVar(value="India passport 35x45")
            c1 = ttk.Combobox(p, textvariable=self.v_photo, width=19,
                              values=list(PHOTO_PRESETS))
            for ev in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
                c1.bind(ev, lambda e: self.on_photo_size())
            row("Photo size", c1)

            self.v_paper = tk.StringVar(value="A4 210x297")
            c2 = ttk.Combobox(p, textvariable=self.v_paper, width=19,
                              values=list(PAPER_PRESETS))
            for ev in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
                c2.bind(ev, lambda e: self.schedule())
            row("Paper", c2)

            self.v_land = tk.BooleanVar(value=False)
            row("", ttk.Checkbutton(p, text="Landscape", variable=self.v_land,
                                    command=self.schedule))

            self.v_margin = tk.DoubleVar(value=8.0)
            row("Margin (mm)", ttk.Spinbox(p, from_=0, to=40, increment=0.5, width=8,
                                           textvariable=self.v_margin,
                                           command=self.schedule))
            self.v_gap = tk.DoubleVar(value=3.0)
            row("Gap (mm)", ttk.Spinbox(p, from_=0, to=20, increment=0.5, width=8,
                                        textvariable=self.v_gap,
                                        command=self.schedule))

            self.v_marks = tk.StringVar(value="border")
            c3 = ttk.Combobox(p, textvariable=self.v_marks, width=10, state="readonly",
                              values=["border", "corners", "none"])
            c3.bind("<<ComboboxSelected>>", lambda e: self.schedule())
            row("Cut guides", c3)

            self.v_dpi = tk.StringVar(value="300")
            c4 = ttk.Combobox(p, textvariable=self.v_dpi, width=10, state="readonly",
                              values=["150", "300", "600"])
            c4.bind("<<ComboboxSelected>>", lambda e: self.schedule())
            row("Print DPI", c4)

            for v in (self.v_margin, self.v_gap):
                v.trace_add("write", lambda *a: self.schedule())

            self.quality_lbl = ttk.Label(p, text="", wraplength=230, foreground="#a00")
            self.quality_lbl.grid(row=r, column=0, columnspan=2, sticky="w",
                                  pady=(6, 0))
            p.columnconfigure(1, weight=1)

        def _build_background(self, parent):
            box = ttk.LabelFrame(parent, text="Background — selected photo",
                                 padding=10)
            box.pack(fill="x", pady=(10, 0))
            box.columnconfigure(1, weight=1)

            ttk.Label(box, text="Colour").grid(row=0, column=0, sticky="w", pady=3)
            self.v_bg = tk.StringVar(value="Keep original")
            c5 = ttk.Combobox(box, textvariable=self.v_bg, width=16, state="readonly",
                              values=list(BG_PRESETS) + ["Custom colour…"])
            c5.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=3)
            c5.bind("<<ComboboxSelected>>", lambda e: self.on_bg())

            ttk.Label(box, text="Edge softness").grid(row=1, column=0, sticky="w",
                                                      pady=3)
            self.v_feather = tk.IntVar(value=2)
            ttk.Spinbox(box, from_=0, to=8, width=6, textvariable=self.v_feather,
                        command=self.on_feather).grid(row=1, column=1, sticky="w",
                                                      padx=(8, 0), pady=3)

            btns = ttk.Frame(box)
            btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            self.recut_btn = ttk.Button(btns, text="Redo cutout",
                                        command=self.redo_cutout, state="disabled")
            self.recut_btn.pack(side="left")
            ttk.Button(btns, text="Apply to all",
                       command=self.apply_bg_all).pack(side="right")

            self.bg_lbl = ttk.Label(box, text="Background: keeping original",
                                    wraplength=240, foreground="#555")
            self.bg_lbl.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

            self.bg_bar = ttk.Progressbar(box, mode="determinate", length=200)
            self.bg_bar.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
            self.bg_bar.grid_remove()

        def _build_adjust(self, parent):
            box = ttk.LabelFrame(parent, text="Image adjustments", padding=10)
            box.pack(fill="x", pady=(10, 0))
            box.columnconfigure(1, weight=1)

            ttk.Label(box, text="Preset").grid(row=0, column=0, sticky="w", pady=(0, 4))
            self.v_preset = tk.StringVar(value="None")
            pc = ttk.Combobox(box, textvariable=self.v_preset, width=14,
                              state="readonly", values=list(ADJ_PRESETS))
            pc.grid(row=0, column=1, columnspan=4, sticky="ew", padx=(6, 0),
                    pady=(0, 4))
            pc.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())

            self.adj_vars = {}
            rows = (("brightness", "Brightness"), ("contrast", "Contrast"),
                    ("saturation", "Saturation"), ("sharpness", "Sharpness"),
                    ("warmth", "Warmth"))
            for idx, (key, label) in enumerate(rows):
                i = idx + 1
                lo, hi, step = ADJ_LIMITS[key]
                default = DEFAULT_ADJ[key]
                ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", pady=2)
                var = tk.DoubleVar(value=default)
                self.adj_vars[key] = var
                sc = ttk.Scale(box, from_=lo, to=hi, variable=var, length=104,
                               command=lambda _v, k=key: self.on_adjust(k))
                sc.grid(row=i, column=1, sticky="ew", padx=6, pady=2)
                ttk.Button(box, text="−", width=2,
                           command=lambda k=key: self.nudge(k, -1)).grid(
                               row=i, column=2, padx=(0, 2))
                val = ttk.Label(box, text=self._fmt_adj(key, default), width=5,
                                anchor="center", foreground="#666")
                val.grid(row=i, column=3)
                ttk.Button(box, text="+", width=2,
                           command=lambda k=key: self.nudge(k, +1)).grid(
                               row=i, column=4, padx=(2, 0))
                self.adj_vars[key + "_lbl"] = val

            n = len(rows) + 1
            self.v_gray = tk.BooleanVar(value=False)
            ttk.Checkbutton(box, text="Black & white", variable=self.v_gray,
                            command=lambda: self.on_adjust("gray")).grid(
                                row=n, column=0, columnspan=5, sticky="w", pady=(6, 0))
            self.v_auto = tk.BooleanVar(value=False)
            ttk.Checkbutton(box, text="Auto levels (brightness only)",
                            variable=self.v_auto,
                            command=lambda: self.on_adjust("auto")).grid(
                                row=n + 1, column=0, columnspan=5, sticky="w")

            btns = ttk.Frame(box)
            btns.grid(row=n + 2, column=0, columnspan=5, sticky="ew", pady=(8, 0))
            ttk.Button(btns, text="Reset", command=self.reset_adjust).pack(side="left")
            ttk.Button(btns, text="Apply to all",
                       command=self.apply_adjust_all).pack(side="right")

        @staticmethod
        def _fmt_adj(key, value):
            return f"{value:+.0f}" if key == "warmth" else f"{value:.2f}"

        def load_adjust(self, photo):
            """Show the selected photo's adjustments in the sliders."""
            self._loading_adj = True
            try:
                for key, var in self.adj_vars.items():
                    if key.endswith("_lbl"):
                        continue
                    var.set(photo.adj[key])
                    self.adj_vars[key + "_lbl"].config(
                        text=self._fmt_adj(key, photo.adj[key]))
                self.v_gray.set(photo.adj["gray"])
                self.v_auto.set(photo.adj["auto"])
            finally:
                self._loading_adj = False

        def on_adjust(self, key):
            if getattr(self, "_loading_adj", False):
                return
            p = self.current()
            if not p:
                return
            if key == "gray":
                p.adj["gray"] = bool(self.v_gray.get())
            elif key == "auto":
                p.adj["auto"] = bool(self.v_auto.get())
            else:
                v = float(self.adj_vars[key].get())
                p.adj[key] = v
                self.adj_vars[key + "_lbl"].config(text=self._fmt_adj(key, v))
            p._cache = None
            self.schedule()

        def nudge(self, key, direction):
            """Step one slider by exactly one increment."""
            p = self.current()
            if not p:
                return
            lo, hi, step = ADJ_LIMITS[key]
            value = clamp(round(p.adj[key] + direction * step, 4), lo, hi)
            p.adj[key] = value
            p._cache = None
            self._loading_adj = True
            try:
                self.adj_vars[key].set(value)
            finally:
                self._loading_adj = False
            self.adj_vars[key + "_lbl"].config(text=self._fmt_adj(key, value))
            self.schedule()

        def apply_preset(self):
            p = self.current()
            if not p:
                return
            p.adj = dict(DEFAULT_ADJ)
            p.adj.update(ADJ_PRESETS.get(self.v_preset.get(), {}))
            p._cache = None
            self.load_adjust(p)
            self.schedule()

        def reset_adjust(self):
            p = self.current()
            if not p:
                return
            p.adj = dict(DEFAULT_ADJ)
            p._cache = None
            self.v_preset.set("None")
            self.load_adjust(p)
            self.schedule()

        def apply_adjust_all(self):
            p = self.current()
            if not p:
                return
            for other in self.photos:
                other.adj = dict(p.adj)
                other._cache = None
            self.status.set("Adjustments copied to every photo.")
            self.schedule()

        # ---------------- settings helpers ----------------
        def photo_mm(self):
            try:
                return parse_dimensions(self.v_photo.get(), PHOTO_PRESETS, "photo size")
            except Exception:
                return PHOTO_PRESETS["India passport 35x45"]

        def paper_mm(self):
            try:
                return parse_dimensions(self.v_paper.get(), PAPER_PRESETS, "paper size")
            except Exception:
                return PAPER_PRESETS["A4 210x297"]

        def num(self, var, default):
            try:
                return float(var.get())
            except Exception:
                return default

        def aspect(self):
            w, h = self.photo_mm()
            return w / h

        def cfg(self, dpi=None):
            return dict(photo_mm=self.photo_mm(), paper_mm=self.paper_mm(),
                        margin_mm=self.num(self.v_margin, 8),
                        gap_mm=self.num(self.v_gap, 3),
                        marks=self.v_marks.get(), landscape=self.v_land.get(),
                        mode=self.v_mode.get(), dpi=dpi or int(self.v_dpi.get()))

        def current(self):
            if self.sel is None or self.sel >= len(self.photos):
                return None
            return self.photos[self.sel]

        # ---------------- photo list ----------------
        def pick_images(self):
            pattern = " ".join(f"*.{e} *.{e.upper()}" for e in IMAGE_EXTS)
            paths = filedialog.askopenfilenames(
                title="Choose one or more photos",
                initialdir=self.last_dir,
                filetypes=[("Images", pattern), ("All files", "*.*")])
            if paths:
                self.add_images(paths)

        def add_images(self, paths):
            added = 0
            for path in paths:
                path = os.path.expanduser(path)
                if not os.path.isfile(path):
                    continue
                try:
                    img = load_image(path)
                except Exception as e:
                    messagebox.showerror("Could not open", f"{path}\n\n{e}")
                    continue
                self.photos.append(Photo(path, img, self.aspect()))
                self.last_dir = os.path.dirname(os.path.abspath(path)) or self.last_dir
                added += 1
            if added:
                self.refresh_list(select=len(self.photos) - 1)
                if any(ph.bg is not None for ph in self.photos):
                    self.start_cutout()
                any_face = any(p.face for p in self.photos)
                self.status.set("Face detected — check each crop." if any_face
                                else "Drag to reposition, scroll to zoom.")
                self.schedule()

        def refresh_list(self, select=None):
            self.tree.delete(*self.tree.get_children())
            for i, p in enumerate(self.photos):
                self.tree.insert("", "end", iid=str(i), text=p.name,
                                 values=(p.qty, BG_SHORT.get(p.bg, "custom")))
            if self.photos:
                idx = select if select is not None else (self.sel or 0)
                idx = clamp(idx, 0, len(self.photos) - 1)
                self.tree.selection_set(str(idx))
                self.tree.see(str(idx))
            else:
                self.sel = None
                self.draw_crop()

        def on_select(self, _=None):
            picked = self.tree.selection()
            if not picked:
                return
            self.sel = int(picked[0])
            p = self.current()
            if p:
                self.v_qty.set(p.qty)
                self.load_adjust(p)
                self.load_bg(p)
            self.draw_crop()
            self.update_quality()

        def on_qty(self, *_):
            p = self.current()
            if not p:
                return
            try:
                p.qty = max(0, int(self.v_qty.get()))
            except Exception:
                return
            self.tree.item(str(self.sel),
                           values=(p.qty, BG_SHORT.get(p.bg, "custom")))
            self.schedule()

        def on_mode(self):
            rows_mode = self.v_mode.get() == "rows"
            self.tree.heading("qty", text="Rows" if rows_mode else "Copies")
            self.qty_lbl.config(text="Rows for this photo" if rows_mode
                                else "Copies of this photo")
            self.schedule()

        def remove_sel(self):
            if self.sel is None:
                return
            del self.photos[self.sel]
            self.refresh_list(select=min(self.sel, len(self.photos) - 1))
            self.schedule()

        def move(self, delta):
            if self.sel is None:
                return
            j = self.sel + delta
            if 0 <= j < len(self.photos):
                self.photos[self.sel], self.photos[j] = \
                    self.photos[j], self.photos[self.sel]
                self.refresh_list(select=j)
                self.schedule()

        def share_evenly(self):
            """Split the sheet as evenly as possible between the loaded photos."""
            if not self.photos:
                return
            grid = compute_grid(self.cfg())
            if grid is None:
                return
            total = grid["rows"] if self.v_mode.get() == "rows" \
                else grid["rows"] * grid["cols"]
            n = len(self.photos)
            base, extra = divmod(total, n)
            for i, p in enumerate(self.photos):
                p.qty = max(1, base + (1 if i < extra else 0))
            self.refresh_list(select=self.sel)
            self.schedule()

        # ---------------- background ----------------
        def bg_color(self):
            label = self.v_bg.get()
            if label == "Custom colour…":
                return self._custom_bg
            return BG_PRESETS.get(label)

        def load_bg(self, photo):
            """Show the selected photo's background settings in the panel."""
            self._loading_bg = True
            try:
                label = next((k for k, v in BG_PRESETS.items() if v == photo.bg), None)
                if label is None and photo.bg is not None:
                    label, self._custom_bg = "Custom colour…", photo.bg
                self.v_bg.set(label or "Keep original")
                self.v_feather.set(photo.feather)
                self.recut_btn.state(["!disabled"] if photo.bg else ["disabled"])
                self.bg_lbl.config(
                    text=("Background: keeping original" if photo.bg is None else
                          f"Background: {photo.cut_note or 'not applied yet'}"),
                    foreground="#555" if photo.bg is None or photo.alpha else "#a00")
            finally:
                self._loading_bg = False

        def on_bg(self):
            """Background is per photo, so this only touches the selected one."""
            if getattr(self, "_loading_bg", False):
                return
            p = self.current()
            if not p:
                return
            if self.v_bg.get() == "Custom colour…":
                from tkinter import colorchooser
                rgb, _ = colorchooser.askcolor(color="#ffffff",
                                               title="Background colour")
                if rgb:
                    self._custom_bg = tuple(int(v) for v in rgb)
                else:
                    self.v_bg.set("Keep original")
            p.bg = self.bg_color()
            p._cache = None
            self.recut_btn.state(["!disabled"] if p.bg else ["disabled"])
            self.tree.item(str(self.sel),
                           values=(p.qty, BG_SHORT.get(p.bg, "custom")))
            if p.bg:
                self.start_cutout()
            else:
                self.bg_lbl.config(text="Background: keeping original",
                                   foreground="#555")
                self.schedule()

        def on_feather(self):
            p = self.current()
            if not p or getattr(self, "_loading_bg", False):
                return
            p.feather = int(self.num(self.v_feather, 2))
            p._cache = None
            self.schedule()

        def apply_bg_all(self):
            """Copy this photo's background choice to every photo."""
            p = self.current()
            if not p:
                return
            for other in self.photos:
                other.bg = p.bg
                other.feather = p.feather
                other._cache = None
            self.refresh_list(select=self.sel)
            if p.bg:
                self.start_cutout()
            else:
                self.schedule()

        def redo_cutout(self):
            p = self.current()
            targets = [p] if p else []
            for ph in targets:
                ph.alpha = None
                ph._cache = None
            self._cut_warned = False
            self.start_cutout()

        def start_cutout(self):
            """
            Separate subject from background on a worker thread.

            Tkinter is not thread-safe: a worker must never call widget methods
            (not even .after). The thread only posts results to a queue, and the
            main thread polls that queue and does all the UI work.
            """
            todo = [ph for ph in self.photos
                    if ph.bg is not None and ph.alpha is None]
            if not todo or self._cutting:
                self.draw_crop()
                self.schedule()
                return
            self._cutting = True
            self._cut_warned = False        # warn again on each fresh attempt
            self._cut_queue = queue.Queue()
            n = len(todo)
            self.bg_lbl.config(text=f"Background: working… (0/{n})",
                               foreground="#a60")
            self.bg_bar.grid()
            if n > 1:
                # Known number of photos: show real progress across them.
                self.bg_bar.config(mode="determinate", maximum=n, value=0)
            else:
                # One photo and no progress signal from inside the model, so
                # animate rather than fake a percentage.
                self.bg_bar.config(mode="indeterminate")
                self.bg_bar.start(14)
            self.status.set(f"Separating subject from background… (0/{n}) "
                            f"— this can take a moment")

            def work(q=self._cut_queue, jobs=todo, total=n):
                for i, ph in enumerate(jobs, 1):
                    try:
                        alpha, note = cutout_alpha(ph.img, ph.face)
                    except BaseException as e:      # incl. SystemExit from libs
                        alpha, note = None, f"{type(e).__name__}: {e}"
                    print(f"[cutout] {ph.name}: "
                          f"{'OK via ' + note if alpha is not None else 'FAILED — ' + note}",
                          file=sys.stderr, flush=True)
                    q.put((ph, alpha, note, i, total))

            self._cut_thread = threading.Thread(target=work, daemon=True)
            self._cut_thread.start()
            self.after(120, self._poll_cutout)      # scheduled from main thread

        def _poll_cutout(self):
            finished = False
            try:
                while True:
                    photo, alpha, note, i, n = self._cut_queue.get_nowait()
                    photo.alpha = alpha
                    photo.cut_note = note
                    photo._cache = None
                    self.bg_lbl.config(text=f"Background: working… ({i}/{n})",
                                       foreground="#a60")
                    if str(self.bg_bar.cget("mode")) == "determinate":
                        self.bg_bar.config(value=i)
                    if alpha is None and not self._cut_warned:
                        self._cut_warned = True
                        messagebox.showwarning(
                            "Background not replaced",
                            f"Could not separate {photo.name} from its "
                            f"background.\n\nWhy: {note}\n\n"
                            "The reliable fix is rembg:\n    pip install rembg\n\n"
                            "then press 'Redo cutout'.")
                    if i == n:
                        finished = True
            except queue.Empty:
                pass

            # Also stop if the worker died before reporting everything
            if not finished and self._cut_thread is not None \
                    and not self._cut_thread.is_alive() \
                    and self._cut_queue.empty():
                finished = True

            if finished:
                self._cutting = False
                self._cut_thread = None
                try:
                    self.bg_bar.stop()
                except Exception:
                    pass
                self.bg_bar.grid_remove()
                want = [ph for ph in self.photos if ph.bg is not None]
                done = [ph for ph in want if ph.alpha is not None]
                if done:
                    self.bg_lbl.config(
                        text=f"Background: applied to {len(done)}/{len(want)} "
                             f"via {done[-1].cut_note}",
                        foreground="#070")
                elif want:
                    why = next((ph.cut_note for ph in want if ph.cut_note),
                               "unknown reason")
                    self.bg_lbl.config(text=f"Background FAILED: {why}",
                                       foreground="#a00")
                self.draw_crop()
                self.schedule()
            else:
                self.after(120, self._poll_cutout)

        # ---------------- crop interaction ----------------
        def on_photo_size(self):
            a = self.aspect()
            for p in self.photos:
                p.refit(a)
            self.draw_crop()
            self.schedule()

        def auto_fit(self):
            p = self.current()
            if not p:
                return
            p.box = auto_crop_box(p.img.width, p.img.height, self.aspect(), p.face)
            self.draw_crop()
            self.schedule()

        def face_fit(self):
            p = self.current()
            if not p:
                return
            p.face = detect_face(p.img)
            if not p.face:
                messagebox.showinfo(
                    "No face found",
                    "Could not detect a face automatically.\n\n"
                    "Install opencv-python for this feature, or set the crop by hand.")
                return
            self.auto_fit()

        def apply_crop_all(self):
            """Copy the selected photo's crop framing to the others, proportionally."""
            src = self.current()
            if not src or len(self.photos) < 2:
                return
            fx = src.box[0] / src.img.width
            fy = src.box[1] / src.img.height
            fw = src.box[2] / src.img.width
            for p in self.photos:
                if p is src:
                    continue
                w = fw * p.img.width
                p.box = fit_box([fx * p.img.width, fy * p.img.height, w, 0],
                                p.img.width, p.img.height, self.aspect())
            self.status.set("Crop framing copied to the other photos.")
            self.schedule()

        def canvas_geometry(self, p):
            cw = max(self.crop_canvas.winfo_width(), 50)
            ch = max(self.crop_canvas.winfo_height(), 50)
            scale = min(cw / p.img.width, ch / p.img.height)
            dw, dh = p.img.width * scale, p.img.height * scale
            return scale, (cw - dw) / 2, (ch - dh) / 2, dw, dh

        def shown_image(self, p):
            """What the crop pane should display: this photo's own settings."""
            return p.display()

        def draw_crop(self):
            c = self.crop_canvas
            c.delete("all")
            p = self.current()
            if not p:
                c.create_text(max(c.winfo_width(), 100) // 2,
                              max(c.winfo_height(), 100) // 2,
                              fill="#888", text="No photo selected")
                return
            scale, ox, oy, dw, dh = self.canvas_geometry(p)
            self._disp = (scale, ox, oy)
            disp = self.shown_image(p).resize((max(1, int(dw)), max(1, int(dh))),
                                              Image.BILINEAR)
            self._photo_ref = ImageTk.PhotoImage(disp)
            c.create_image(ox, oy, image=self._photo_ref, anchor="nw")

            x, y, w, h = p.box
            X, Y = ox + x * scale, oy + y * scale
            W, H = w * scale, h * scale
            for rect in ((ox, oy, ox + dw, Y), (ox, Y + H, ox + dw, oy + dh),
                         (ox, Y, X, Y + H), (X + W, Y, ox + dw, Y + H)):
                c.create_rectangle(*rect, fill="#000", stipple="gray50", outline="")
            c.create_rectangle(X, Y, X + W, Y + H, outline="#ffcc00", width=2)
            if self.guides.get():
                eye = Y + H * 0.45
                c.create_line(X, eye, X + W, eye, fill="#ffcc00", dash=(4, 3))
                c.create_text(X + 4, eye - 8, text="eyes", anchor="w",
                              fill="#ffcc00", font=("TkDefaultFont", 8))
                chin = Y + H * 0.86
                c.create_line(X, chin, X + W, chin, fill="#ffcc00", dash=(2, 4))

        def on_press(self, e):
            p = self.current()
            if p:
                self._drag = (e.x, e.y, p.box[0], p.box[1])

        def on_drag(self, e):
            p = self.current()
            if not (p and self._drag and self._disp):
                return
            sx, sy, bx, by = self._drag
            scale = self._disp[0]
            p.box[0] = bx + (e.x - sx) / scale
            p.box[1] = by + (e.y - sy) / scale
            p.refit(self.aspect())
            self.draw_crop()
            self.schedule()

        def on_wheel(self, e):
            self.zoom(0.92 if e.delta > 0 else 1.08)

        def zoom(self, factor):
            p = self.current()
            if not p:
                return
            x, y, w, h = p.box
            cx, cy = x + w / 2, y + h / 2
            w *= factor
            a = self.aspect()
            p.box = fit_box([cx - w / 2, cy - (w / a) / 2, w, 0],
                            p.img.width, p.img.height, a)
            self.draw_crop()
            self.schedule()

        def update_quality(self):
            p = self.current()
            if not p:
                self.quality_lbl.config(text="")
                return
            eff = effective_dpi(p.box, self.photo_mm())
            self.quality_lbl.config(
                text="" if eff >= 200 else
                f"{p.name}: only ~{eff:.0f} DPI at this crop. Zoom out or use a "
                f"larger photo for a sharp print.")
            self.status.set(f"{p.name}  ·  crop {int(p.box[2])}×{int(p.box[3])} px  "
                            f"·  ~{eff:.0f} DPI at print size")

        # ---------------- preview ----------------
        def schedule(self, *_):
            if self._preview_job:
                self.after_cancel(self._preview_job)
            self._preview_job = self.after(160, self._refresh)

        def _refresh(self):
            self._preview_job = None
            self.draw_crop()
            self.draw_sheet()

        def flip(self, delta):
            if self._sheets:
                self._page = clamp(self._page + delta, 0, len(self._sheets) - 1)
                self.show_page()

        def draw_sheet(self):
            self._preview_job = None
            c = self.sheet_canvas
            cw, ch = max(c.winfo_width(), 100), max(c.winfo_height(), 100)
            if not self.photos:
                c.delete("all")
                c.create_text(cw // 2, ch // 2, text="Preview", fill="#999")
                self.page_lbl.config(text="")
                self.layout_lbl.config(text="")
                self._sheets = []
                return

            paper_w, paper_h = self.paper_mm()
            if self.v_land.get():
                paper_w, paper_h = paper_h, paper_w
            prev_dpi = max(24, min(150, min(cw / (paper_w / MM_PER_INCH),
                                            ch / (paper_h / MM_PER_INCH))))
            self._sheets, info = render_pages(self.photos, self.cfg(),
                                              dpi=int(prev_dpi))
            if not self._sheets:
                c.delete("all")
                c.create_text(cw // 2, ch // 2, fill="#a00",
                              text="Doesn't fit — check size or quantities")
                self.layout_lbl.config(text="")
                self.page_lbl.config(text="")
                return

            self._info = info
            self._page = clamp(self._page, 0, len(self._sheets) - 1)
            self.show_page()
            self.update_quality()

        def show_page(self):
            c = self.sheet_canvas
            c.delete("all")
            cw, ch = max(c.winfo_width(), 100), max(c.winfo_height(), 100)
            page = self._sheets[self._page]
            self._sheet_ref = ImageTk.PhotoImage(page)
            x, y = (cw - page.width) // 2, (ch - page.height) // 2
            c.create_rectangle(x + 3, y + 3, x + page.width + 3, y + page.height + 3,
                               fill="#b8b8b8", outline="")
            c.create_image(x, y, image=self._sheet_ref, anchor="nw")
            c.create_rectangle(x, y, x + page.width, y + page.height, outline="#999")

            n = len(self._sheets)
            self.page_lbl.config(text=f"Page {self._page + 1} of {n}")
            self.prev_btn.state(["!disabled"] if self._page > 0 else ["disabled"])
            self.next_btn.state(["!disabled"] if self._page < n - 1 else ["disabled"])

            info = self._info
            pw, ph = self.photo_mm()
            self.layout_lbl.config(
                text=f"{info['cols']} × {info['rows']} per sheet · "
                     f"{info['total']} photos\n{pw:g} × {ph:g} mm each")

        # ---------------- save ----------------
        def save(self):
            if not self.photos:
                messagebox.showinfo("Nothing to save", "Add a photo first.")
                return
            first = os.path.splitext(self.photos[0].name)[0]
            default = f"{first}_sheet.pdf" if len(self.photos) == 1 \
                else "passport_sheet.pdf"
            out = filedialog.asksaveasfilename(
                defaultextension=".pdf", initialfile=default,
                initialdir=self.last_dir, filetypes=[("PDF", "*.pdf")])
            if not out:
                return
            try:
                info = save_pdf(self.photos, self.cfg(), out)
            except Exception as e:
                messagebox.showerror("Could not save", str(e))
                return
            self.status.set(f"Saved {os.path.basename(out)}")
            pages = info["pages"]
            messagebox.showinfo(
                "Saved",
                f"{info['total']} photos across {pages} "
                f"{'page' if pages == 1 else 'pages'} written to:\n{out}\n\n"
                "When printing, choose 'Actual size' or 100% scale — "
                "not 'Fit to page'.")

    App().mainloop()


# --------------------------------------------------------------------------
#  Text-prompt mode
# --------------------------------------------------------------------------

def run_cli(paths=None):
    def ask(prompt, default):
        try:
            got = input(f"{prompt} [{default}]: ").strip()
        except EOFError:
            print()
            return str(default)
        return got or str(default)

    def ask_num(prompt, default, cast=float):
        while True:
            try:
                return cast(ask(prompt, default))
            except ValueError:
                print("  Please enter a number.")

    def ask_size(heading, presets, prompt, what):
        print(f"\n{heading}")
        labels = list(presets)
        for i, label in enumerate(labels, 1):
            w, h = presets[label]
            print(f"  {i}. {label}  ({w:g}×{h:g} mm)")
        while True:
            choice = ask(prompt, "1")
            if choice.isdigit() and 1 <= int(choice) <= len(labels):
                return presets[labels[int(choice) - 1]]
            try:
                return parse_dimensions(choice, presets, what)
            except ValueError:
                print("  Pick a number from the list, or type something like 35x45.")

    print("\n--- Passport photo sheet ---\n")

    paths = list(paths or [])
    while True:
        if not paths:
            first = os.path.expanduser(
                input("Path to your photo: ").strip().strip('"').strip("'"))
            if os.path.isfile(first):
                paths.append(first)
            else:
                print("  No file at that path, try again.")
                continue
        more = ask("Add another photo? (path, or blank to continue)", "")
        if not more:
            break
        more = os.path.expanduser(more.strip('"').strip("'"))
        if os.path.isfile(more):
            paths.append(more)
        else:
            print("  No file at that path.")

    photo_mm = ask_size("Photo size presets:", PHOTO_PRESETS,
                        "Pick a number, or type a size like 35x45 / 2x2in",
                        "photo size")
    paper_mm = ask_size("Paper presets:", PAPER_PRESETS,
                        "Pick a number, or type a size like 210x297", "paper size")

    margin = ask_num("\nPage margin in mm", 8)
    gap = ask_num("Gap between photos in mm", 3)
    marks = ask("Cut guides (border / corners / none)", "border").lower()
    if marks not in ("border", "corners", "none"):
        marks = "border"
    dpi = ask_num("Print DPI", 300, int)
    mode = ask("Count each photo by (rows / copies)", "rows").lower()
    if mode not in ("rows", "copies"):
        mode = "rows"

    bg_labels = {"keep": None, "white": (255, 255, 255), "grey": (220, 220, 220),
                 "gray": (220, 220, 220), "blue": (198, 219, 240)}
    bg_in = ask("Background (keep / white / grey / blue)", "keep").lower()
    bg_color = bg_labels.get(bg_in)
    feather = ask_num("Edge softness in px", 2, int) if bg_color else 0

    cfg = dict(photo_mm=photo_mm, paper_mm=paper_mm, margin_mm=margin, gap_mm=gap,
               marks=marks, landscape=False, mode=mode, dpi=dpi,
               bg_color=bg_color, feather=feather)
    grid = compute_grid(cfg)
    if grid is None:
        sys.exit("The photo is larger than the printable area — reduce the margin.")
    print(f"\nThis sheet holds {grid['cols']} across × {grid['rows']} down "
          f"({grid['cols'] * grid['rows']} photos per page).")

    aspect = photo_mm[0] / photo_mm[1]
    photos = []
    for path in paths:
        img = load_image(path)
        p = Photo(path, img, aspect)
        print(f"\n{p.name}  ({img.width}×{img.height} px)"
              f"{'  — face detected' if p.face else ''}")
        p.qty = ask_num(f"  How many {mode}", 1, int)
        anchor = ask("  Crop from (center / top / bottom)", "center").lower()
        zoom = ask_num("  Zoom (100 = auto, 120 = tighter, 80 = wider)", 100) or 100
        if zoom != 100:
            cx = p.box[0] + p.box[2] / 2
            cy = p.box[1] + p.box[3] / 2
            w = p.box[2] * 100.0 / zoom
            p.box = fit_box([cx - w / 2, cy - (w / aspect) / 2, w, 0],
                            img.width, img.height, aspect)
        if anchor == "top":
            p.box[1] = 0
        elif anchor == "bottom":
            p.box[1] = img.height - p.box[3]
        p.refit(aspect)
        eff = effective_dpi(p.box, photo_mm)
        if eff < 200:
            print(f"  Warning: only ~{eff:.0f} DPI at this crop; may print soft.")
        if bg_color:
            p.bg, p.feather = bg_color, feather
            print("  Separating subject from background…", end=" ", flush=True)
            p.alpha, note = cutout_alpha(p.img, p.face)
            print(f"done via {note}." if p.alpha is not None
                  else f"FAILED ({note}). Try: pip install rembg")
        photos.append(p)

    sheets, info = render_pages(photos, cfg)
    if not sheets:
        sys.exit("Nothing to print — every quantity was zero.")

    stem = os.path.splitext(paths[0])[0]
    preview = stem + "_preview.png"
    sheets[0].resize((sheets[0].width // 4, sheets[0].height // 4)).save(preview)
    print(f"\nPreview of page 1 written to {preview} — check it before printing.")

    out = ask("Output PDF path", stem + "_sheet.pdf")
    save_pdf(photos, cfg, out)
    print(f"\nSaved {out}")
    print(f"  {info['total']} photos across {info['pages']} page(s), "
          f"{photo_mm[0]:g}×{photo_mm[1]:g} mm each, {dpi} DPI")
    print("  Print at 'Actual size' / 100% scale — not 'Fit to page'.\n")


def diagnose(paths):
    """Report exactly what the background remover can and cannot do here."""
    print(f"\n--- Background removal diagnostics (build {BUILD}) ---\n")
    import importlib.metadata as md
    import importlib.util

    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    for mod in ("PIL", "cv2", "numpy"):
        try:
            m = __import__(mod)
            print(f"{mod:<12}: {getattr(m, '__version__', 'installed')}")
        except Exception as e:
            print(f"{mod:<12}: NOT AVAILABLE ({type(e).__name__})")

    # These are read from package metadata, never imported here: importing
    # rembg in this process can abort the interpreter outright.
    for dist, mod in (("rembg", "rembg"), ("onnxruntime", "onnxruntime"),
                      ("numba", "numba"), ("llvmlite", "llvmlite")):
        try:
            print(f"{dist:<12}: {md.version(dist)}  (not imported here)")
        except Exception:
            present = importlib.util.find_spec(mod) is not None
            print(f"{dist:<12}: {'present' if present else 'NOT INSTALLED'}")

    if not paths:
        print("\nPass a photo to test it: --diagnose photo.jpg")
        return

    for path in paths:
        path = os.path.expanduser(path)      # tolerate a quoted '~/...'
        if not os.path.isfile(path):
            print(f"\n{path}: no such file")
            hint = os.path.dirname(path) or "."
            if os.path.isdir(hint):
                near = [f for f in sorted(os.listdir(hint))
                        if f.lower().endswith(IMAGE_EXTS)][:8]
                if near:
                    print("  images in that folder: " + ", ".join(near))
            continue
        img = load_image(path)
        face = detect_face(img)
        print(f"\n{os.path.basename(path)}  {img.width}x{img.height}")
        print(f"  face detected : {'yes ' + str(tuple(int(v) for v in face)) if face else 'no'}")

        for name, fn in (("rembg", None),
                         ("grabcut", lambda: _grabcut_alpha(img, face)),
                         ("colourkey", lambda: _colorkey_alpha(img))):
            if name == "rembg":
                a, why = _rembg_alpha(img)
                if a is None:
                    print(f"  {name:<14}: {why}")
                    continue
            else:
                a = fn()
            if a is None:
                print(f"  {name:<14}: returned nothing")
                continue
            ok, fg = _mask_plausible(a)
            verdict = "OK" if ok else "REJECTED (keeps too much/little)"
            print(f"  {name:<14}: keeps {fg:.0%} of frame -> {verdict}")
            out = os.path.splitext(path)[0] + f"_mask_{name}.png"
            a.save(out)
            print(f"                   mask written to {out}")

        alpha, note = cutout_alpha(img, face)
        print(f"  CHOSEN        : {note if alpha is not None else 'NONE — ' + note}")
        if alpha is not None:
            p = Photo(path, img, 35 / 45)
            p.alpha = alpha
            prev = os.path.splitext(path)[0] + "_bg_preview.png"
            p.display((198, 219, 240), 2).save(prev)
            print(f"  preview       : {prev}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="*", help="photos to load")
    ap.add_argument("--cli", action="store_true", help="text prompts instead of the GUI")
    ap.add_argument("--diagnose", action="store_true",
                    help="report why background removal is or isn't working")
    args = ap.parse_args()

    print(f"passport_studio build {BUILD}", file=sys.stderr)
    if args.diagnose:
        diagnose(args.images)
        return
    if args.cli:
        run_cli(args.images)
        return
    try:
        import tkinter  # noqa: F401
    except Exception:
        print("Tkinter is not available, falling back to text mode.")
        print("  Linux: sudo apt install python3-tk\n")
        run_cli(args.images)
        return
    run_gui(args.images)


if __name__ == "__main__":
    main()
