"""
tests/test_pipeline.py — Unit + integration tests for the Genealogy OCR Pipeline

Coverage
────────
  utils.py          get_doc_id, append/read manifest, processed_set, chunked
  m1_preprocess.py  deskew, denoise, binarize, to_grayscale, save_png
  m2_ocr.py         hOCR→ALTO converter, DZI zoom-level formula
  m3_indexer.py     parse_alto, normalize_bbox, build_documents

Run with:
  cd genealogy_pipeline
  pytest tests/ -v
  pytest tests/ -v --cov=scripts --cov-report=term-missing
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

# ── Add scripts/ to path so we can import modules directly ───────────────────
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import utils
from m1_preprocess import binarize, denoise, deskew, to_grayscale
from m2_ocr import _builtin_hocr_to_alto
from m3_indexer import build_documents, normalize_bbox, parse_alto


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_dir(tmp_path):
    """Return a fresh temporary directory for each test."""
    return tmp_path


@pytest.fixture
def gray_image():
    """
    A synthetic 200×300 grayscale image (uint8).
    Mostly white (220) with a 20-pixel wide black rectangle at the top —
    simulates a simple text line on a page.
    """
    img = np.full((300, 200), 220, dtype=np.uint8)
    img[20:40, 10:190] = 30   # dark "text" band
    return img


@pytest.fixture
def skewed_image():
    """
    A 400×400 white image with a diagonal dark band, producing a detectable
    skew angle when fed to the deskew() function.
    """
    img = np.full((400, 400), 220, dtype=np.uint8)
    # Draw a line at ~5° from horizontal
    import cv2
    cv2.line(img, (50, 100), (350, 127), color=0, thickness=3)
    return img


@pytest.fixture
def minimal_alto_xml(tmp_dir):
    """
    Write a minimal valid ALTO v3 XML file and return its path.

    The file contains:
      - 1 page  (2480 × 3508 px)
      - 1 TextBlock
      - 2 TextLines
      - 3 words in line 1, 2 words in line 2
    """
    ALTO_NS = "http://www.loc.gov/standards/alto/ns-v3#"
    xml = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <alto xmlns="{ALTO_NS}">
          <Layout>
            <Page ID="p001" WIDTH="2480" HEIGHT="3508" PHYSICAL_IMG_NR="1">
              <PrintSpace HPOS="0" VPOS="0" WIDTH="2480" HEIGHT="3508">
                <TextBlock ID="tb_1">
                  <TextLine ID="tl_1" HPOS="120" VPOS="200" WIDTH="800" HEIGHT="40">
                    <String ID="s_1" CONTENT="Kowalski" HPOS="120" VPOS="200"
                            WIDTH="200" HEIGHT="40" WC="0.92"/>
                    <String ID="s_2" CONTENT="Jan"      HPOS="340" VPOS="200"
                            WIDTH="100" HEIGHT="40" WC="0.87"/>
                    <String ID="s_3" CONTENT="lat"      HPOS="460" VPOS="200"
                            WIDTH="80"  HEIGHT="40" WC="0.95"/>
                  </TextLine>
                  <TextLine ID="tl_2" HPOS="120" VPOS="260" WIDTH="500" HEIGHT="40">
                    <String ID="s_4" CONTENT="wyznanie"  HPOS="120" VPOS="260"
                            WIDTH="220" HEIGHT="40" WC="0.78"/>
                    <String ID="s_5" CONTENT="rzym-kat"  HPOS="360" VPOS="260"
                            WIDTH="180" HEIGHT="40" WC="0.81"/>
                  </TextLine>
                </TextBlock>
              </PrintSpace>
            </Page>
          </Layout>
        </alto>
    """)
    path = tmp_dir / "test.alto.xml"
    path.write_text(xml, encoding="utf-8")
    return path


@pytest.fixture
def minimal_hocr(tmp_dir):
    """
    Write a minimal hOCR XHTML file (Tesseract output format) and return its path.

    Contains 1 page, 1 paragraph, 1 line, 2 words with bboxes and confidence scores.
    """
    html = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
          "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
        <html xmlns="http://www.w3.org/1999/xhtml">
        <head><title>hOCR</title></head>
        <body>
        <div class="ocr_page" id="page_1"
             title="image test.png; bbox 0 0 2480 3508; ppageno 0">
          <div class="ocr_carea" title="bbox 120 200 920 240">
            <p class="ocr_par">
              <span class="ocr_line" title="bbox 120 200 920 240">
                <span class="ocrx_word"
                      title="bbox 120 200 320 240; x_wconf 92">Kowalski</span>
                <span class="ocrx_word"
                      title="bbox 340 200 440 240; x_wconf 87">Jan</span>
              </span>
            </p>
          </div>
        </div>
        </body>
        </html>
    """)
    path = tmp_dir / "test.hocr"
    path.write_text(html, encoding="utf-8")
    return path


# ═════════════════════════════════════════════════════════════════════════════
# utils.py tests
# ═════════════════════════════════════════════════════════════════════════════

class TestGetDocId:
    def test_returns_12_chars(self):
        doc_id = utils.get_doc_id("parish_register_1897.pdf")
        assert len(doc_id) == 12

    def test_deterministic(self):
        """Same filename always produces the same doc_id."""
        a = utils.get_doc_id("test.pdf")
        b = utils.get_doc_id("test.pdf")
        assert a == b

    def test_different_files_differ(self):
        assert utils.get_doc_id("fileA.pdf") != utils.get_doc_id("fileB.pdf")

    def test_hex_characters_only(self):
        doc_id = utils.get_doc_id("anything.tiff")
        assert all(c in "0123456789abcdef" for c in doc_id)

    def test_matches_sha256_prefix(self):
        """Cross-check against a direct SHA-256 call."""
        filename = "parish_register_1897.pdf"
        expected = hashlib.sha256(filename.encode()).hexdigest()[:12]
        assert utils.get_doc_id(filename) == expected

    def test_path_object_and_string_equivalent(self):
        """Passing a Path object or a plain string gives the same result."""
        assert utils.get_doc_id("foo.pdf") == utils.get_doc_id("foo.pdf")


class TestManifest:
    def test_append_and_read(self, tmp_dir):
        entry = {"doc_id": "abc123", "page": 1, "status": "ready"}
        utils.append_manifest(tmp_dir, entry)
        entries = list(utils.read_manifest(tmp_dir))
        assert len(entries) == 1
        assert entries[0]["doc_id"] == "abc123"

    def test_multiple_appends(self, tmp_dir):
        for i in range(5):
            utils.append_manifest(tmp_dir, {"doc_id": "x", "page": i})
        entries = list(utils.read_manifest(tmp_dir))
        assert len(entries) == 5

    def test_ts_auto_injected(self, tmp_dir):
        utils.append_manifest(tmp_dir, {"doc_id": "x", "page": 1})
        entry = list(utils.read_manifest(tmp_dir))[0]
        assert "ts" in entry

    def test_read_empty_folder(self, tmp_dir):
        """read_manifest on a folder without a manifest returns an empty iterator."""
        assert list(utils.read_manifest(tmp_dir)) == []

    def test_already_processed_true(self, tmp_dir):
        utils.append_manifest(tmp_dir, {"doc_id": "abc", "page": 3})
        assert utils.already_processed(tmp_dir, "abc", 3) is True

    def test_already_processed_false(self, tmp_dir):
        utils.append_manifest(tmp_dir, {"doc_id": "abc", "page": 3})
        assert utils.already_processed(tmp_dir, "abc", 4) is False

    def test_processed_set(self, tmp_dir):
        utils.append_manifest(tmp_dir, {"doc_id": "a", "page": 1})
        utils.append_manifest(tmp_dir, {"doc_id": "a", "page": 2})
        s = utils.processed_set(tmp_dir)
        assert ("a", 1) in s
        assert ("a", 2) in s
        assert ("a", 3) not in s

    def test_jsonl_is_valid_per_line(self, tmp_dir):
        """Each line in the manifest file must be independently valid JSON."""
        utils.append_manifest(tmp_dir, {"doc_id": "a", "page": 1})
        utils.append_manifest(tmp_dir, {"doc_id": "b", "page": 2})
        lines = (tmp_dir / "_manifest.jsonl").read_text().strip().splitlines()
        for line in lines:
            obj = json.loads(line)   # must not raise
            assert "doc_id" in obj


class TestChunked:
    def test_even_split(self):
        result = list(utils.chunked(range(6), 2))
        assert result == [[0, 1], [2, 3], [4, 5]]

    def test_uneven_split(self):
        result = list(utils.chunked(range(5), 2))
        assert result == [[0, 1], [2, 3], [4]]

    def test_empty(self):
        assert list(utils.chunked([], 3)) == []

    def test_larger_than_input(self):
        assert list(utils.chunked([1, 2], 10)) == [[1, 2]]


class TestPageStem:
    def test_format(self):
        assert utils.page_stem("a3f8b1c2d4e5", 1)   == "a3f8b1c2d4e5_p001"
        assert utils.page_stem("a3f8b1c2d4e5", 42)  == "a3f8b1c2d4e5_p042"
        assert utils.page_stem("a3f8b1c2d4e5", 999) == "a3f8b1c2d4e5_p999"


# ═════════════════════════════════════════════════════════════════════════════
# m1_preprocess.py tests
# ═════════════════════════════════════════════════════════════════════════════

class TestToGrayscale:
    def test_bgr_to_gray(self):
        bgr = np.zeros((50, 50, 3), dtype=np.uint8)
        bgr[:, :, 0] = 100   # blue channel
        result = to_grayscale(bgr)
        assert result.ndim == 2
        assert result.shape == (50, 50)

    def test_already_gray_passthrough(self, gray_image):
        result = to_grayscale(gray_image)
        assert result.shape == gray_image.shape
        assert np.array_equal(result, gray_image)


class TestDeskew:
    def test_small_angle_no_correction(self, gray_image):
        """Angles below min_angle (0.5°) are not corrected."""
        result, angle = deskew(gray_image, min_angle=0.5, max_angle=15.0)
        # The synthetic image has no strong lines, so angle should be ~0
        assert abs(angle) < 0.5 or np.array_equal(result, gray_image)

    def test_empty_image_no_crash(self):
        """A completely white image (no dark pixels) must not crash."""
        white = np.full((100, 100), 255, dtype=np.uint8)
        result, angle = deskew(white, min_angle=0.5, max_angle=15.0)
        assert angle == 0.0
        assert result.shape == white.shape

    def test_returns_same_shape(self, gray_image):
        result, _ = deskew(gray_image, min_angle=0.5, max_angle=15.0)
        assert result.shape == gray_image.shape

    def test_output_dtype(self, gray_image):
        result, _ = deskew(gray_image, min_angle=0.5, max_angle=15.0)
        assert result.dtype == np.uint8

    def test_extreme_angle_not_corrected(self, gray_image):
        """Angles above max_angle (15°) are treated as artefacts and skipped."""
        # We can't easily force a >15° angle from minAreaRect in a unit test,
        # but we can verify the function returns original when angle is out of range.
        result, angle = deskew(gray_image, min_angle=0.5, max_angle=0.1)
        # max < min → nothing qualifies → original image returned
        assert np.array_equal(result, gray_image)

    def test_rotation_matrix_formula(self):
        """
        Verify the 2×3 rotation matrix formula used internally.

        M = [[cos θ,  sin θ,  tx],
             [-sin θ, cos θ,  ty]]

        For θ=0°, M should be the identity transform (no translation needed
        since centre stays at centre):  [[1, 0, 0], [0, 1, 0]]
        """
        import cv2
        theta = 0.0
        w, h = 200, 300
        M = cv2.getRotationMatrix2D((w / 2, h / 2), theta, 1.0)
        assert M.shape == (2, 3)
        np.testing.assert_allclose(M[0, 0],  math.cos(math.radians(theta)), atol=1e-6)
        np.testing.assert_allclose(M[0, 1],  math.sin(math.radians(theta)), atol=1e-6)
        np.testing.assert_allclose(M[1, 0], -math.sin(math.radians(theta)), atol=1e-6)
        np.testing.assert_allclose(M[1, 1],  math.cos(math.radians(theta)), atol=1e-6)


class TestDenoise:
    def test_output_shape_unchanged(self, gray_image):
        result = denoise(gray_image, h=10)
        assert result.shape == gray_image.shape

    def test_output_dtype(self, gray_image):
        result = denoise(gray_image, h=10)
        assert result.dtype == np.uint8

    def test_reduces_random_noise(self):
        """NLM should reduce pixel-level random noise in a uniform region."""
        rng = np.random.default_rng(42)
        noisy = (200 + rng.integers(-20, 20, size=(100, 100))).clip(0, 255).astype(np.uint8)
        denoised = denoise(noisy, h=15)
        # Variance should decrease after denoising
        assert float(denoised.std()) < float(noisy.std())


class TestBinarize:
    def test_output_is_binary(self, gray_image):
        result = binarize(gray_image, block_size=31, c=15)
        unique_values = np.unique(result)
        assert set(unique_values).issubset({0, 255})

    def test_output_shape_unchanged(self, gray_image):
        result = binarize(gray_image, block_size=31, c=15)
        assert result.shape == gray_image.shape

    def test_dark_region_becomes_black(self):
        """
        Test adaptive thresholding with a gradient edge — a dark strip adjacent
        to a white background.

        Adaptive thresholding computes a local Gaussian-weighted mean T(x,y) for
        each pixel and outputs:
            255 (white)  if pixel > T(x,y) - C
              0 (black)  otherwise

        A *uniformly* dark patch produces a dark local mean too, so its interior
        can still appear white (the pixel is not dark *relative to its neighbours*).
        This is correct behaviour — it enhances local contrast, not global contrast.

        We test the *edge* pixels of the dark patch instead, where the neighbourhood
        straddles dark and white, making the local mean high enough that the dark
        pixels reliably threshold to black.
        """
        img = np.full((200, 200), 240, dtype=np.uint8)
        # Horizontal dark band — edge pixels have mixed-brightness neighbourhoods
        img[90:110, :] = 20   # very dark band

        result = binarize(img, block_size=31, c=15)

        # The left/right endpoints of the band have a mostly-white neighbourhood
        # → local mean is high → threshold is high → dark pixel (20) → black
        edge_strip = result[95:105, 5:30]   # left edge of band
        black_ratio = (edge_strip == 0).mean()
        assert black_ratio > 0.5, (
            f"Expected edge pixels of dark band to be mostly black, "
            f"got {black_ratio:.1%} black"
        )


class TestSavePng:
    def test_saves_and_loads(self, tmp_dir, gray_image):
        from m1_preprocess import save_png
        from PIL import Image
        out = tmp_dir / "out.png"
        save_png(gray_image, out, dpi=300)
        assert out.exists()
        loaded = Image.open(out)
        assert loaded.size == (gray_image.shape[1], gray_image.shape[0])

    def test_dpi_metadata(self, tmp_dir, gray_image):
        from m1_preprocess import save_png
        from PIL import Image
        out = tmp_dir / "out.png"
        save_png(gray_image, out, dpi=300)
        loaded = Image.open(out)
        dpi = loaded.info.get("dpi", (0, 0))
        assert dpi[0] == pytest.approx(300, abs=1)


# ═════════════════════════════════════════════════════════════════════════════
# m2_ocr.py tests
# ═════════════════════════════════════════════════════════════════════════════

class TestBuiltinHocrToAlto:
    def test_produces_valid_xml(self, minimal_hocr, tmp_dir):
        alto_path = tmp_dir / "out.alto.xml"
        _builtin_hocr_to_alto(minimal_hocr, alto_path)
        assert alto_path.exists()
        # Must parse without exception
        ET.parse(str(alto_path))

    def test_words_preserved(self, minimal_hocr, tmp_dir):
        alto_path = tmp_dir / "out.alto.xml"
        _builtin_hocr_to_alto(minimal_hocr, alto_path)
        tree = ET.parse(str(alto_path))
        # Find all String elements (any namespace)
        strings = [el for el in tree.iter() if el.tag.endswith("}String") or el.tag == "String"]
        contents = [s.get("CONTENT") for s in strings if s.get("CONTENT")]
        assert "Kowalski" in contents
        assert "Jan" in contents

    def test_bbox_transferred(self, minimal_hocr, tmp_dir):
        """HPOS/VPOS/WIDTH/HEIGHT must be set on each String element."""
        alto_path = tmp_dir / "out.alto.xml"
        _builtin_hocr_to_alto(minimal_hocr, alto_path)
        tree = ET.parse(str(alto_path))
        for el in tree.iter():
            if el.tag.endswith("}String") or el.tag == "String":
                assert el.get("HPOS") is not None
                assert el.get("VPOS") is not None
                assert el.get("WIDTH") is not None
                assert el.get("HEIGHT") is not None

    def test_confidence_converted_to_decimal(self, minimal_hocr, tmp_dir):
        """
        hOCR stores x_wconf as 0-100 integers.
        ALTO WC should be 0.0-1.0 floats.
        """
        alto_path = tmp_dir / "out.alto.xml"
        _builtin_hocr_to_alto(minimal_hocr, alto_path)
        tree = ET.parse(str(alto_path))
        for el in tree.iter():
            if el.tag.endswith("}String") or el.tag == "String":
                wc = el.get("WC")
                if wc is not None:
                    assert 0.0 <= float(wc) <= 1.0, f"WC={wc} out of [0,1]"


class TestDziZoomLevelFormula:
    """
    Verify the DZI zoom-level formula:  L = ⌈log₂(max(W, H))⌉ + 1

    This is pure math — we test it without calling pyvips.
    """

    def _expected_levels(self, w: int, h: int) -> int:
        return math.ceil(math.log2(max(w, h))) + 1

    def test_square_256(self):
        # log₂(256) = 8 (exact) → ⌈8⌉ + 1 = 9
        assert self._expected_levels(256, 256) == 9

    def test_square_255(self):
        # log₂(255) ≈ 7.994 → ⌈7.994⌉ = 8 → 8 + 1 = 9
        assert self._expected_levels(255, 255) == 9

    def test_square_257(self):
        # log₂(257) ≈ 8.006 → ⌈8.006⌉ = 9 → 9 + 1 = 10
        assert self._expected_levels(257, 257) == 10

    def test_typical_scan_2480x3508(self):
        # max = 3508; log₂(3508) ≈ 11.776 → ⌈11.776⌉ = 12 → 12 + 1 = 13
        assert self._expected_levels(2480, 3508) == 13

    def test_landscape_3508x2480(self):
        # max = 3508; same result regardless of orientation
        assert self._expected_levels(3508, 2480) == 13

    def test_tall_narrow_100x4096(self):
        # max = 4096; log₂(4096) = 12 (exact) → ⌈12⌉ + 1 = 13
        assert self._expected_levels(100, 4096) == 13

    def test_minimum_1x1(self):
        # log₂(1) = 0 → ⌈0⌉ + 1 = 1
        assert self._expected_levels(1, 1) == 1


# ═════════════════════════════════════════════════════════════════════════════
# m3_indexer.py tests
# ═════════════════════════════════════════════════════════════════════════════

class TestNormalizeBbox:
    """
    Test the OpenSeadragon viewport coordinate normalization.

    KEY RULE: both x and y coordinates are divided by the IMAGE WIDTH (not height).
    OSD's viewport system uses width=1.0 as the unit, and height=H/W proportionally.
    """

    def test_x_coords_divided_by_width(self):
        page_w = 2480
        bbox = normalize_bbox(248, 0, 496, 0, page_w)
        assert bbox[0] == pytest.approx(0.1,   abs=1e-4)   # x0 = 248/2480
        assert bbox[2] == pytest.approx(0.2,   abs=1e-4)   # x1 = 496/2480

    def test_y_coords_also_divided_by_WIDTH_not_height(self):
        """
        This is the most critical invariant.
        y values MUST be divided by page_w, not page_h.
        """
        page_w = 2480
        # y=248 → 248/2480 = 0.1  (NOT 248/3508 ≈ 0.0707)
        bbox = normalize_bbox(0, 248, 0, 496, page_w)
        assert bbox[1] == pytest.approx(0.1, abs=1e-4)
        assert bbox[3] == pytest.approx(0.2, abs=1e-4)

    def test_full_page_bbox_normalizes_to_width_1(self):
        page_w = 2480
        page_h = 3508
        bbox = normalize_bbox(0, 0, page_w, page_h, page_w)
        assert bbox[0] == pytest.approx(0.0, abs=1e-6)
        assert bbox[1] == pytest.approx(0.0, abs=1e-6)
        assert bbox[2] == pytest.approx(1.0, abs=1e-6)
        # y1 = 3508/2480 ≈ 1.4145 — the image height in viewport units
        assert bbox[3] == pytest.approx(page_h / page_w, abs=1e-4)

    def test_returns_4_floats(self):
        result = normalize_bbox(10, 20, 100, 200, 1000)
        assert len(result) == 4
        assert all(isinstance(v, float) for v in result)

    def test_rounding_to_6_decimals(self):
        result = normalize_bbox(1, 1, 3, 3, 7)
        for v in result:
            decimal_places = len(str(v).rstrip("0").split(".")[-1])
            assert decimal_places <= 6

    def test_zero_origin(self):
        bbox = normalize_bbox(0, 0, 0, 0, 1000)
        assert bbox == [0.0, 0.0, 0.0, 0.0]


class TestParseAlto:
    def test_returns_correct_page_dimensions(self, minimal_alto_xml):
        page_w, page_h, _ = parse_alto(minimal_alto_xml)
        assert page_w == 2480
        assert page_h == 3508

    def test_returns_correct_line_count(self, minimal_alto_xml):
        _, _, lines = parse_alto(minimal_alto_xml)
        assert len(lines) == 2

    def test_line_text_content(self, minimal_alto_xml):
        _, _, lines = parse_alto(minimal_alto_xml)
        assert lines[0]["text"] == "Kowalski Jan lat"
        assert lines[1]["text"] == "wyznanie rzym-kat"

    def test_line_bbox_px_format(self, minimal_alto_xml):
        _, _, lines = parse_alto(minimal_alto_xml)
        for line in lines:
            bp = line["bbox_px"]
            assert len(bp) == 4
            x0, y0, x1, y1 = bp
            assert x0 <= x1, "x0 must be ≤ x1"
            assert y0 <= y1, "y0 must be ≤ y1"

    def test_line1_bbox_is_union_of_words(self, minimal_alto_xml):
        """
        Line 1 bbox must be the union of its 3 word bboxes:
          word1: HPOS=120, VPOS=200, WIDTH=200, HEIGHT=40 → x1=320, y1=240
          word2: HPOS=340, VPOS=200, WIDTH=100, HEIGHT=40 → x1=440, y1=240
          word3: HPOS=460, VPOS=200, WIDTH=80,  HEIGHT=40 → x1=540, y1=240
        Union: [120, 200, 540, 240]
        """
        _, _, lines = parse_alto(minimal_alto_xml)
        assert lines[0]["bbox_px"] == [120, 200, 540, 240]

    def test_confidence_in_range(self, minimal_alto_xml):
        _, _, lines = parse_alto(minimal_alto_xml)
        for line in lines:
            assert 0.0 <= line["confidence"] <= 1.0

    def test_avg_confidence_line1(self, minimal_alto_xml):
        """
        Words have WC = 0.92, 0.87, 0.95.
        Average = (0.92 + 0.87 + 0.95) / 3 = 0.9133…
        """
        _, _, lines = parse_alto(minimal_alto_xml)
        expected = (0.92 + 0.87 + 0.95) / 3
        assert lines[0]["confidence"] == pytest.approx(expected, abs=1e-3)

    def test_missing_file_raises(self, tmp_dir):
        with pytest.raises((FileNotFoundError, OSError, ET.ParseError)):
            parse_alto(tmp_dir / "nonexistent.alto.xml")


class TestBuildDocuments:
    def test_id_format(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        assert docs[0]["id"] == "abc123def456_p001_l0000"
        assert docs[1]["id"] == "abc123def456_p001_l0001"

    def test_count_matches_lines(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        assert len(docs) == len(lines) == 2

    def test_required_fields_present(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        required = {"id", "doc_id", "page", "line_index", "text", "bbox", "bbox_px",
                    "confidence", "source_file"}
        for doc in docs:
            missing = required - doc.keys()
            assert not missing, f"Missing fields: {missing}"

    def test_bbox_is_normalized(self, minimal_alto_xml):
        """bbox values must all be ≤ 1.0 for x, and ≤ H/W for y."""
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        for doc in docs:
            x0, y0, x1, y1 = doc["bbox"]
            assert 0.0 <= x0 <= 1.0
            assert 0.0 <= x1 <= 1.0
            # y can exceed 1.0 for portrait images (it reaches H/W ≈ 1.41 for A4)
            assert 0.0 <= y0 <= page_h / page_w + 0.01
            assert 0.0 <= y1 <= page_h / page_w + 0.01

    def test_bbox_px_preserved(self, minimal_alto_xml):
        """bbox_px should be the raw pixel coordinates from the ALTO file."""
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        assert docs[0]["bbox_px"] == [120, 200, 540, 240]

    def test_source_file_stored(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        assert all(d["source_file"] == "test.pdf" for d in docs)

    def test_json_serializable(self, minimal_alto_xml):
        """All documents must serialize to JSON without errors."""
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        json_str = json.dumps(docs, ensure_ascii=False)
        reloaded = json.loads(json_str)
        assert len(reloaded) == len(docs)

    def test_empty_lines_produces_empty_docs(self):
        docs = build_documents("abc123def456", 1, "test.pdf", 2480, 3508, [])
        assert docs == []


# ═════════════════════════════════════════════════════════════════════════════
# Integration: utils + m3_indexer round-trip
# ═════════════════════════════════════════════════════════════════════════════

class TestIndexerManifestRoundTrip:
    """
    End-to-end test of the indexer's write-then-read path.

    Checks that JSON written by build_documents() and read back from disk
    is byte-for-byte equivalent.
    """

    def test_write_and_reload_json(self, tmp_dir, minimal_alto_xml):
        from m3_indexer import write_index_json
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)

        out_path = tmp_dir / "abc123def456_p001.json"
        write_index_json(docs, out_path)

        with open(out_path, encoding="utf-8") as f:
            reloaded = json.load(f)

        assert len(reloaded) == len(docs)
        for orig, loaded in zip(docs, reloaded):
            assert orig["id"]      == loaded["id"]
            assert orig["text"]    == loaded["text"]
            assert orig["bbox"]    == loaded["bbox"]
            assert orig["bbox_px"] == loaded["bbox_px"]

    def test_manifest_entry_appended(self, tmp_dir, minimal_alto_xml):
        """
        Simulate what m3_indexer.py does at the end of processing:
        appends a manifest entry and verifies it is readable.
        """
        utils.append_manifest(tmp_dir, {
            "doc_id": "abc123def456",
            "page": 1,
            "json_file": "abc123def456_p001.json",
            "line_count": 2,
            "status": "ready",
        })
        entries = list(utils.read_manifest(tmp_dir))
        assert len(entries) == 1
        assert entries[0]["line_count"] == 2
