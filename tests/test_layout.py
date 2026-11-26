import pytest

from passport_studio import (MM_PER_INCH, PAPER_PRESETS, PHOTO_PRESETS,
                             auto_crop_box, compute_grid, effective_dpi,
                             fit_box, parse_dimensions, plan_pages)


# ---- parse_dimensions -------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("35x45", (35, 45)),
    ("35x45mm", (35, 45)),
    ("3.5x4.5cm", (35, 45)),
    ("2x2in", (2 * MM_PER_INCH, 2 * MM_PER_INCH)),
    ("35×45", (35, 45)),
    ("india passport 35x45", (35, 45)),   # preset, case-insensitive
])
def test_parse_dimensions(text, expected):
    assert parse_dimensions(text, PHOTO_PRESETS) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["35", "axb", "0x45", "-1x4"])
def test_parse_dimensions_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_dimensions(bad, PHOTO_PRESETS)


# ---- compute_grid -----------------------------------------------------------

def _cfg(**kw):
    cfg = dict(dpi=300, paper_mm=PAPER_PRESETS["A4 210x297"], photo_mm=(35, 45),
               margin_mm=5, gap_mm=2)
    cfg.update(kw)
    return cfg


def test_a4_fits_5_by_6_indian_passport_photos():
    g = compute_grid(_cfg())
    assert (g["cols"], g["rows"]) == (5, 6)
    assert g["page"] == (2480, 3508)          # A4 at 300 dpi


def test_grid_count_does_not_depend_on_dpi():
    hi, lo = compute_grid(_cfg(dpi=600)), compute_grid(_cfg(dpi=40))
    assert (hi["cols"], hi["rows"]) == (lo["cols"], lo["rows"])


def test_landscape_swaps_paper():
    g = compute_grid(_cfg(landscape=True))
    assert g["page"][0] > g["page"][1]


def test_photo_larger_than_paper_returns_none():
    assert compute_grid(_cfg(paper_mm=(30, 30))) is None


# ---- plan_pages -------------------------------------------------------------

def test_rows_mode_gives_each_photo_whole_rows():
    pages = plan_pages([1, 2], cols=3, rows=6, mode="rows")
    assert len(pages) == 1
    slots = pages[0]
    assert [s for s in slots if s[2] == 0] == [(0, 0, 0), (0, 1, 0), (0, 2, 0)]
    assert {s[0] for s in slots if s[2] == 1} == {1, 2}


def test_copies_mode_flows_across_rows():
    (page,) = plan_pages([4], cols=3, rows=6, mode="copies")
    assert page == [(0, 0, 0), (0, 1, 0), (0, 2, 0), (1, 0, 0)]


def test_overflow_adds_pages():
    pages = plan_pages([5], cols=2, rows=2, mode="copies")
    assert [len(p) for p in pages] == [4, 1]


def test_zero_quantity_photos_are_skipped():
    assert plan_pages([0, 0], cols=3, rows=3) == []


# ---- cropping ---------------------------------------------------------------

def test_auto_crop_without_face_is_centred_and_correct_aspect():
    x, y, w, h = auto_crop_box(1000, 1000, aspect=35 / 45)
    assert w / h == pytest.approx(35 / 45)
    assert x == pytest.approx((1000 - w) / 2)
    assert h == 1000


def test_auto_crop_with_face_stays_inside_image():
    x, y, w, h = auto_crop_box(800, 1200, 35 / 45, face=(700, 50, 200, 200))
    assert 0 <= x and x + w <= 800 and 0 <= y and y + h <= 1200


def test_fit_box_clamps_into_image():
    x, y, w, h = fit_box([900, 900, 5000, 0], 1000, 1000, 35 / 45)
    assert w / h == pytest.approx(35 / 45)
    assert 0 <= x <= 1000 - w and 0 <= y <= 1000 - h


def test_effective_dpi():
    # A 413x531 px crop printed at 35x45 mm is ~300 dpi.
    assert effective_dpi([0, 0, 413, 531], (35, 45)) == pytest.approx(299.7, abs=0.5)
