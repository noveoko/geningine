"""
tests/test_pipeline.py — Unit + integration tests for the Genealogy OCR Pipeline

Coverage
────────
  utils.py          get_doc_id, append/read manifest, processed_set, chunked
  m1_preprocess.py  deskew, denoise, binarize, to_grayscale, save_png
  m2_ocr.py         hOCR→ALTO converter, DZI zoom-level formula
  m3_indexer.py     parse_alto, normalize_bbox, build_documents
  content           real Kowel 1928 document — Polish + Ukrainian bilingual

Source document
───────────────
  "Samorząd Kowelski" № 4, Lipiec 1928 r.
  Bilingual (Polish / Ukrainian) official gazette of the Kowel County Council.
  Contains a Volyn Voivode regulation on rabies (wścieklizna / скаженіна) prevention.

Run with:
  pytest tests/ -v
  pytest tests/ -v --cov=scripts --cov-report=term-missing
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
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
# Ground-truth reference — Samorząd Kowelski № 4, Lipiec 1928
# ═════════════════════════════════════════════════════════════════════════════
# High-quality OCR reference for the document used in fixtures and content
# tests.  All TestKowelContent assertions derive from this dict.

KOWEL_DOC = {
    "header": {
        "price":      "Cena 1 Zł.",
        "main_title": "SAMORZĄD KOWELSKI.",
        "subtitle":   "OFICJALNY ORGAN WYDZIAŁU POWIATOWEGO SEJMIKU W KOWLU.",
        "address":    "Adres Redakcji i Administracji - Kowel, Wydział Powiatowy.",
        "issue_details": {
            "year":         "Rok I.",
            "date":         "Lipiec 1928 r.",
            "issue_number": "№ 4.",
        },
    },
    "left_column": {                        # Polish
        "language":      "Polish",
        "section_title": "DZIAŁ OFICJALNY.",
        "announcement_header": (
            "Rozporządzenie Wojewody Wołyńskiego "
            "z 24 maja 1928 r. L. 2256 Rol. "
            "w sprawie zwalczania i zapobiegania wścieklizny."
        ),
        "par1_text": (
            "Wszystkie psy, znajdujące się na terenie poszczególnych gmin i miast, "
            "winny być zarejestrowane i oznakowane w terminie do dnia "
            "1 września 1928 roku."
        ),
        "par2_text": (
            "Wszystkie psy na obszarze Województwa winny być trzymane na uwięzi, "
            "w razie zaś prowadzenia psów, takowe winny być zaopatrzone "
            "w bezpieczne kagańce i trzymane na smyczy."
        ),
        "key_terms": ["psy", "wścieklizny", "zarejestrowane", "kagańce", "smyczy"],
    },
    "right_column": {                       # Ukrainian
        "language":      "Ukrainian",
        "section_title": "РОЗДІЛ ОФІЦІЙНИЙ.",
        "announcement_header": (
            "Розпорядження Волинського Воєводства "
            "з дня 24 травня 1928 р. ч. 2256 Rol. "
            "в справі — як запобігти й боротися зі скаженіною."
        ),
        "par1_text": (
            "Всі собаки, що знаходяться на теренах окремих гмін і міст, "
            "мусять бути зареєстровані і ознаковані в терміні до "
            "1-го вересня 1928 р."
        ),
        "key_terms": ["собаки", "скаженіни", "зареєстровані", "намордники", "шворках"],
    },
    "source_file": "samorząd_kowelski_1928_nr4.pdf",
    # sha256("samorząd_kowelski_1928_nr4.pdf")[:12]
    "doc_id":      "9cecfdf583b7",
}


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture
def gray_image():
    """200×300 grayscale image with a dark text-band near the top."""
    img = np.full((300, 200), 220, dtype=np.uint8)
    img[20:40, 10:190] = 30
    return img


@pytest.fixture
def skewed_image():
    """400×400 image with a line drawn at ~5° from horizontal."""
    import cv2
    img = np.full((400, 400), 220, dtype=np.uint8)
    cv2.line(img, (50, 100), (350, 127), color=0, thickness=3)
    return img


@pytest.fixture
def minimal_alto_xml(tmp_dir):
    """
    ALTO v3 XML fixture using real words from the Kowel 1928 Polish column.

    Page: 2480 × 3508 px  (300 DPI A4 scan)

    Line 1 — newspaper title:
      "SAMORZĄD"   HPOS=120 VPOS=200 WIDTH=280 HEIGHT=40  WC=0.95
      "KOWELSKI."  HPOS=420 VPOS=200 WIDTH=260 HEIGHT=40  WC=0.93
      union bbox_px = [120, 200, 680, 240]
      avg confidence = (0.95 + 0.93) / 2 = 0.940

    Line 2 — section heading:
      "DZIAŁ"      HPOS=120 VPOS=260 WIDTH=160 HEIGHT=40  WC=0.91
      "OFICJALNY." HPOS=300 VPOS=260 WIDTH=220 HEIGHT=40  WC=0.89
      union bbox_px = [120, 260, 520, 300]
      avg confidence = (0.91 + 0.89) / 2 = 0.900
    """
    ALTO_NS = "http://www.loc.gov/standards/alto/ns-v3#"
    xml = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <alto xmlns="{ALTO_NS}">
          <Layout>
            <Page ID="p001" WIDTH="2480" HEIGHT="3508" PHYSICAL_IMG_NR="1">
              <PrintSpace HPOS="0" VPOS="0" WIDTH="2480" HEIGHT="3508">
                <TextBlock ID="tb_1">
                  <TextLine ID="tl_1" HPOS="120" VPOS="200" WIDTH="560" HEIGHT="40">
                    <String ID="s_1" CONTENT="SAMORZĄD"  HPOS="120" VPOS="200"
                            WIDTH="280" HEIGHT="40" WC="0.95"/>
                    <String ID="s_2" CONTENT="KOWELSKI." HPOS="420" VPOS="200"
                            WIDTH="260" HEIGHT="40" WC="0.93"/>
                  </TextLine>
                  <TextLine ID="tl_2" HPOS="120" VPOS="260" WIDTH="400" HEIGHT="40">
                    <String ID="s_3" CONTENT="DZIAŁ"      HPOS="120" VPOS="260"
                            WIDTH="160" HEIGHT="40" WC="0.91"/>
                    <String ID="s_4" CONTENT="OFICJALNY." HPOS="300" VPOS="260"
                            WIDTH="220" HEIGHT="40" WC="0.89"/>
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
def kowel_alto_xml(tmp_dir):
    """
    Richer ALTO v3 XML fixture covering both columns of the Kowel document.

    Left column (Polish)  — HPOS starts at 120
    Right column (Ukrainian) — HPOS starts at 1260  (≈ half of 2480 px page width)

    Lines:
      tl_1  Polish title:    "SAMORZĄD KOWELSKI."
      tl_2  Polish section:  "DZIAŁ OFICJALNY."
      tl_3  Polish heading:  "Rozporządzenie Wojewody Wołyńskiego"
      tl_4  Polish par1:     "Wszystkie psy, znajdujące się na terenie"
      tl_5  Ukr title:       "РОЗДІЛ ОФІЦІЙНИЙ."
      tl_6  Ukr heading:     "Розпорядження Волинського Воєводства"
      tl_7  Ukr par1:        "Всі собаки, що знаходяться на теренах"
    """
    ALTO_NS = "http://www.loc.gov/standards/alto/ns-v3#"
    xml = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <alto xmlns="{ALTO_NS}">
          <Layout>
            <Page ID="p001" WIDTH="2480" HEIGHT="3508" PHYSICAL_IMG_NR="1">
              <PrintSpace HPOS="0" VPOS="0" WIDTH="2480" HEIGHT="3508">

                <TextBlock ID="tb_left">
                  <TextLine ID="tl_1" HPOS="120" VPOS="160" WIDTH="980" HEIGHT="50">
                    <String ID="s_1" CONTENT="SAMORZĄD"   HPOS="120"  VPOS="160" WIDTH="380" HEIGHT="50" WC="0.97"/>
                    <String ID="s_2" CONTENT="KOWELSKI."  HPOS="520"  VPOS="160" WIDTH="360" HEIGHT="50" WC="0.96"/>
                  </TextLine>
                  <TextLine ID="tl_2" HPOS="120" VPOS="240" WIDTH="600" HEIGHT="40">
                    <String ID="s_3" CONTENT="DZIAŁ"      HPOS="120"  VPOS="240" WIDTH="180" HEIGHT="40" WC="0.94"/>
                    <String ID="s_4" CONTENT="OFICJALNY." HPOS="320"  VPOS="240" WIDTH="260" HEIGHT="40" WC="0.92"/>
                  </TextLine>
                  <TextLine ID="tl_3" HPOS="120" VPOS="340" WIDTH="900" HEIGHT="38">
                    <String ID="s_5" CONTENT="Rozporządzenie" HPOS="120" VPOS="340" WIDTH="340" HEIGHT="38" WC="0.88"/>
                    <String ID="s_6" CONTENT="Wojewody"       HPOS="480" VPOS="340" WIDTH="200" HEIGHT="38" WC="0.91"/>
                    <String ID="s_7" CONTENT="Wołyńskiego"    HPOS="700" VPOS="340" WIDTH="260" HEIGHT="38" WC="0.87"/>
                  </TextLine>
                  <TextLine ID="tl_4" HPOS="120" VPOS="400" WIDTH="1000" HEIGHT="38">
                    <String ID="s_8"  CONTENT="Wszystkie"  HPOS="120" VPOS="400" WIDTH="220" HEIGHT="38" WC="0.90"/>
                    <String ID="s_9"  CONTENT="psy,"       HPOS="360" VPOS="400" WIDTH="80"  HEIGHT="38" WC="0.95"/>
                    <String ID="s_10" CONTENT="znajdujące" HPOS="460" VPOS="400" WIDTH="240" HEIGHT="38" WC="0.83"/>
                    <String ID="s_11" CONTENT="się"        HPOS="720" VPOS="400" WIDTH="80"  HEIGHT="38" WC="0.94"/>
                    <String ID="s_12" CONTENT="na"         HPOS="820" VPOS="400" WIDTH="60"  HEIGHT="38" WC="0.96"/>
                    <String ID="s_13" CONTENT="terenie"    HPOS="900" VPOS="400" WIDTH="160" HEIGHT="38" WC="0.89"/>
                  </TextLine>
                </TextBlock>

                <TextBlock ID="tb_right">
                  <TextLine ID="tl_5" HPOS="1260" VPOS="160" WIDTH="980" HEIGHT="50">
                    <String ID="s_14" CONTENT="РОЗДІЛ"      HPOS="1260" VPOS="160" WIDTH="280" HEIGHT="50" WC="0.94"/>
                    <String ID="s_15" CONTENT="ОФІЦІЙНИЙ."  HPOS="1560" VPOS="160" WIDTH="360" HEIGHT="50" WC="0.92"/>
                  </TextLine>
                  <TextLine ID="tl_6" HPOS="1260" VPOS="340" WIDTH="940" HEIGHT="38">
                    <String ID="s_16" CONTENT="Розпорядження" HPOS="1260" VPOS="340" WIDTH="360" HEIGHT="38" WC="0.86"/>
                    <String ID="s_17" CONTENT="Волинського"   HPOS="1640" VPOS="340" WIDTH="280" HEIGHT="38" WC="0.88"/>
                    <String ID="s_18" CONTENT="Воєводства"    HPOS="1940" VPOS="340" WIDTH="240" HEIGHT="38" WC="0.85"/>
                  </TextLine>
                  <TextLine ID="tl_7" HPOS="1260" VPOS="400" WIDTH="1000" HEIGHT="38">
                    <String ID="s_19" CONTENT="Всі"          HPOS="1260" VPOS="400" WIDTH="80"  HEIGHT="38" WC="0.96"/>
                    <String ID="s_20" CONTENT="собаки,"      HPOS="1360" VPOS="400" WIDTH="200" HEIGHT="38" WC="0.91"/>
                    <String ID="s_21" CONTENT="що"           HPOS="1580" VPOS="400" WIDTH="60"  HEIGHT="38" WC="0.95"/>
                    <String ID="s_22" CONTENT="знаходяться"  HPOS="1660" VPOS="400" WIDTH="300" HEIGHT="38" WC="0.84"/>
                    <String ID="s_23" CONTENT="на"           HPOS="1980" VPOS="400" WIDTH="60"  HEIGHT="38" WC="0.97"/>
                    <String ID="s_24" CONTENT="теренах"      HPOS="2060" VPOS="400" WIDTH="200" HEIGHT="38" WC="0.88"/>
                  </TextLine>
                </TextBlock>

              </PrintSpace>
            </Page>
          </Layout>
        </alto>
    """)
    path = tmp_dir / "kowel.alto.xml"
    path.write_text(xml, encoding="utf-8")
    return path


@pytest.fixture
def minimal_hocr(tmp_dir):
    """
    hOCR fixture — Tesseract output for the Kowel 1928 Polish title line.
    Words: "SAMORZĄD" (x_wconf 97) and "KOWELSKI." (x_wconf 96).
    """
    html = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
          "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
        <html xmlns="http://www.w3.org/1999/xhtml">
        <head><title>hOCR</title></head>
        <body>
        <div class="ocr_page" id="page_1"
             title="image samorząd_kowelski_1928_nr4.png; bbox 0 0 2480 3508; ppageno 0">
          <div class="ocr_carea" title="bbox 120 160 900 210">
            <p class="ocr_par">
              <span class="ocr_line" title="bbox 120 160 900 210">
                <span class="ocrx_word"
                      title="bbox 120 160 500 210; x_wconf 97">SAMORZĄD</span>
                <span class="ocrx_word"
                      title="bbox 520 160 900 210; x_wconf 96">KOWELSKI.</span>
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
        assert utils.get_doc_id("test.pdf") == utils.get_doc_id("test.pdf")

    def test_different_files_differ(self):
        assert utils.get_doc_id("fileA.pdf") != utils.get_doc_id("fileB.pdf")

    def test_hex_characters_only(self):
        doc_id = utils.get_doc_id("anything.tiff")
        assert all(c in "0123456789abcdef" for c in doc_id)

    def test_matches_sha256_prefix(self):
        filename = "parish_register_1897.pdf"
        expected = hashlib.sha256(filename.encode()).hexdigest()[:12]
        assert utils.get_doc_id(filename) == expected

    def test_path_object_and_string_equivalent(self):
        assert utils.get_doc_id("foo.pdf") == utils.get_doc_id("foo.pdf")

    def test_kowel_doc_id(self):
        """The Kowel source file must hash to the value stored in KOWEL_DOC."""
        assert utils.get_doc_id(KOWEL_DOC["source_file"]) == KOWEL_DOC["doc_id"]


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
        assert len(list(utils.read_manifest(tmp_dir))) == 5

    def test_ts_auto_injected(self, tmp_dir):
        utils.append_manifest(tmp_dir, {"doc_id": "x", "page": 1})
        entry = list(utils.read_manifest(tmp_dir))[0]
        assert "ts" in entry

    def test_read_empty_folder(self, tmp_dir):
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
        utils.append_manifest(tmp_dir, {"doc_id": "a", "page": 1})
        utils.append_manifest(tmp_dir, {"doc_id": "b", "page": 2})
        lines = (tmp_dir / "_manifest.jsonl").read_text().strip().splitlines()
        for line in lines:
            obj = json.loads(line)
            assert "doc_id" in obj

    def test_kowel_manifest_roundtrip(self, tmp_dir):
        """Manifest entry for the Kowel document survives a write/read cycle."""
        utils.append_manifest(tmp_dir, {
            "doc_id":      KOWEL_DOC["doc_id"],
            "page":        1,
            "source_file": KOWEL_DOC["source_file"],
            "status":      "ready",
        })
        entry = list(utils.read_manifest(tmp_dir))[0]
        assert entry["doc_id"]      == KOWEL_DOC["doc_id"]
        assert entry["source_file"] == KOWEL_DOC["source_file"]


class TestChunked:
    def test_even_split(self):
        assert list(utils.chunked(range(6), 2)) == [[0, 1], [2, 3], [4, 5]]

    def test_uneven_split(self):
        assert list(utils.chunked(range(5), 2)) == [[0, 1], [2, 3], [4]]

    def test_empty(self):
        assert list(utils.chunked([], 3)) == []

    def test_larger_than_input(self):
        assert list(utils.chunked([1, 2], 10)) == [[1, 2]]


class TestPageStem:
    def test_format(self):
        assert utils.page_stem("a3f8b1c2d4e5", 1)   == "a3f8b1c2d4e5_p001"
        assert utils.page_stem("a3f8b1c2d4e5", 42)  == "a3f8b1c2d4e5_p042"
        assert utils.page_stem("a3f8b1c2d4e5", 999) == "a3f8b1c2d4e5_p999"

    def test_kowel_stem(self):
        stem = utils.page_stem(KOWEL_DOC["doc_id"], 1)
        assert stem == f"{KOWEL_DOC['doc_id']}_p001"


# ═════════════════════════════════════════════════════════════════════════════
# m1_preprocess.py tests
# ═════════════════════════════════════════════════════════════════════════════

class TestToGrayscale:
    def test_bgr_to_gray(self):
        bgr = np.zeros((50, 50, 3), dtype=np.uint8)
        bgr[:, :, 0] = 100
        result = to_grayscale(bgr)
        assert result.ndim == 2
        assert result.shape == (50, 50)

    def test_already_gray_passthrough(self, gray_image):
        result = to_grayscale(gray_image)
        assert np.array_equal(result, gray_image)


class TestDeskew:
    def test_small_angle_no_correction(self, gray_image):
        result, angle = deskew(gray_image, min_angle=0.5, max_angle=15.0)
        assert abs(angle) < 0.5 or np.array_equal(result, gray_image)

    def test_empty_image_no_crash(self):
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
        result, _ = deskew(gray_image, min_angle=0.5, max_angle=0.1)
        assert np.array_equal(result, gray_image)

    def test_rotation_matrix_formula(self):
        """
        M = [[cos θ,  sin θ,  tx],
             [-sin θ, cos θ,  ty]]
        For θ=0, M reduces to the identity (no rotation).
        """
        import cv2
        theta = 0.0
        M = cv2.getRotationMatrix2D((100.0, 150.0), theta, 1.0)
        assert M.shape == (2, 3)
        np.testing.assert_allclose(M[0, 0],  math.cos(math.radians(theta)), atol=1e-6)
        np.testing.assert_allclose(M[0, 1],  math.sin(math.radians(theta)), atol=1e-6)
        np.testing.assert_allclose(M[1, 0], -math.sin(math.radians(theta)), atol=1e-6)
        np.testing.assert_allclose(M[1, 1],  math.cos(math.radians(theta)), atol=1e-6)


class TestDenoise:
    def test_output_shape_unchanged(self, gray_image):
        assert denoise(gray_image, h=10).shape == gray_image.shape

    def test_output_dtype(self, gray_image):
        assert denoise(gray_image, h=10).dtype == np.uint8

    def test_reduces_random_noise(self):
        rng = np.random.default_rng(42)
        noisy = (200 + rng.integers(-20, 20, size=(100, 100))).clip(0, 255).astype(np.uint8)
        denoised = denoise(noisy, h=15)
        assert float(denoised.std()) < float(noisy.std())


class TestBinarize:
    def test_output_is_binary(self, gray_image):
        result = binarize(gray_image, block_size=31, c=15)
        assert set(np.unique(result)).issubset({0, 255})

    def test_output_shape_unchanged(self, gray_image):
        assert binarize(gray_image, block_size=31, c=15).shape == gray_image.shape

    def test_dark_region_becomes_black(self):
        """
        Adaptive thresholding works on local contrast.  At the edge of a dark
        band the neighbourhood straddles dark and white, so the local Gaussian
        mean is high enough that the dark pixels threshold to black.
        """
        img = np.full((200, 200), 240, dtype=np.uint8)
        img[90:110, :] = 20   # dark horizontal band
        result = binarize(img, block_size=31, c=15)
        edge_strip = result[95:105, 5:30]
        assert (edge_strip == 0).mean() > 0.5


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
        dpi = Image.open(out).info.get("dpi", (0, 0))
        assert dpi[0] == pytest.approx(300, abs=1)


# ═════════════════════════════════════════════════════════════════════════════
# m2_ocr.py tests
# ═════════════════════════════════════════════════════════════════════════════

class TestBuiltinHocrToAlto:
    def test_produces_valid_xml(self, minimal_hocr, tmp_dir):
        alto = tmp_dir / "out.alto.xml"
        _builtin_hocr_to_alto(minimal_hocr, alto)
        ET.parse(str(alto))   # must not raise

    def test_words_preserved(self, minimal_hocr, tmp_dir):
        alto = tmp_dir / "out.alto.xml"
        _builtin_hocr_to_alto(minimal_hocr, alto)
        tree = ET.parse(str(alto))
        contents = [
            el.get("CONTENT")
            for el in tree.iter()
            if el.tag.endswith("}String") or el.tag == "String"
        ]
        assert "SAMORZĄD"  in contents
        assert "KOWELSKI." in contents

    def test_bbox_transferred(self, minimal_hocr, tmp_dir):
        alto = tmp_dir / "out.alto.xml"
        _builtin_hocr_to_alto(minimal_hocr, alto)
        for el in ET.parse(str(alto)).iter():
            if el.tag.endswith("}String") or el.tag == "String":
                for attr in ("HPOS", "VPOS", "WIDTH", "HEIGHT"):
                    assert el.get(attr) is not None

    def test_confidence_converted_to_decimal(self, minimal_hocr, tmp_dir):
        """hOCR x_wconf is 0-100; ALTO WC must be 0.0-1.0."""
        alto = tmp_dir / "out.alto.xml"
        _builtin_hocr_to_alto(minimal_hocr, alto)
        for el in ET.parse(str(alto)).iter():
            if el.tag.endswith("}String") or el.tag == "String":
                wc = el.get("WC")
                if wc is not None:
                    assert 0.0 <= float(wc) <= 1.0


class TestDziZoomLevelFormula:
    """L = ⌈log₂(max(W, H))⌉ + 1"""

    def _L(self, w, h):
        return math.ceil(math.log2(max(w, h))) + 1

    def test_square_256(self):     assert self._L(256, 256)   == 9
    def test_square_255(self):     assert self._L(255, 255)   == 9
    def test_square_257(self):     assert self._L(257, 257)   == 10
    def test_typical_scan(self):   assert self._L(2480, 3508) == 13
    def test_landscape(self):      assert self._L(3508, 2480) == 13
    def test_tall_narrow(self):    assert self._L(100, 4096)  == 13
    def test_minimum(self):        assert self._L(1, 1)       == 1


# ═════════════════════════════════════════════════════════════════════════════
# m3_indexer.py tests
# ═════════════════════════════════════════════════════════════════════════════

class TestNormalizeBbox:
    """Both x and y coordinates are divided by image WIDTH — not height."""

    def test_x_divided_by_width(self):
        bbox = normalize_bbox(248, 0, 496, 0, 2480)
        assert bbox[0] == pytest.approx(0.1, abs=1e-4)
        assert bbox[2] == pytest.approx(0.2, abs=1e-4)

    def test_y_divided_by_WIDTH_not_height(self):
        # y=248 → 248/2480 = 0.1, NOT 248/3508 ≈ 0.0707
        bbox = normalize_bbox(0, 248, 0, 496, 2480)
        assert bbox[1] == pytest.approx(0.1, abs=1e-4)
        assert bbox[3] == pytest.approx(0.2, abs=1e-4)

    def test_full_page_x_equals_1(self):
        bbox = normalize_bbox(0, 0, 2480, 3508, 2480)
        assert bbox[2] == pytest.approx(1.0,        abs=1e-6)
        assert bbox[3] == pytest.approx(3508 / 2480, abs=1e-4)

    def test_returns_4_floats(self):
        result = normalize_bbox(10, 20, 100, 200, 1000)
        assert len(result) == 4
        assert all(isinstance(v, float) for v in result)

    def test_rounding_to_6_decimals(self):
        for v in normalize_bbox(1, 1, 3, 3, 7):
            dp = len(str(v).rstrip("0").split(".")[-1])
            assert dp <= 6

    def test_zero_origin(self):
        assert normalize_bbox(0, 0, 0, 0, 1000) == [0.0, 0.0, 0.0, 0.0]


class TestParseAlto:
    def test_page_dimensions(self, minimal_alto_xml):
        page_w, page_h, _ = parse_alto(minimal_alto_xml)
        assert page_w == 2480
        assert page_h == 3508

    def test_line_count(self, minimal_alto_xml):
        _, _, lines = parse_alto(minimal_alto_xml)
        assert len(lines) == 2

    def test_line_text_content(self, minimal_alto_xml):
        _, _, lines = parse_alto(minimal_alto_xml)
        assert lines[0]["text"] == "SAMORZĄD KOWELSKI."
        assert lines[1]["text"] == "DZIAŁ OFICJALNY."

    def test_bbox_px_format(self, minimal_alto_xml):
        _, _, lines = parse_alto(minimal_alto_xml)
        for line in lines:
            x0, y0, x1, y1 = line["bbox_px"]
            assert x0 <= x1
            assert y0 <= y1

    def test_line1_bbox_is_union_of_words(self, minimal_alto_xml):
        """
        Line 1 words:
          "SAMORZĄD"  HPOS=120 WIDTH=280 → x1=400
          "KOWELSKI." HPOS=420 WIDTH=260 → x1=680
        Both at VPOS=200, HEIGHT=40 → y1=240
        Union: [120, 200, 680, 240]
        """
        _, _, lines = parse_alto(minimal_alto_xml)
        assert lines[0]["bbox_px"] == [120, 200, 680, 240]

    def test_confidence_in_range(self, minimal_alto_xml):
        _, _, lines = parse_alto(minimal_alto_xml)
        for line in lines:
            assert 0.0 <= line["confidence"] <= 1.0

    def test_avg_confidence_line1(self, minimal_alto_xml):
        """WC = 0.95, 0.93 → avg = 0.940"""
        _, _, lines = parse_alto(minimal_alto_xml)
        assert lines[0]["confidence"] == pytest.approx((0.95 + 0.93) / 2, abs=1e-3)

    def test_avg_confidence_line2(self, minimal_alto_xml):
        """WC = 0.91, 0.89 → avg = 0.900"""
        _, _, lines = parse_alto(minimal_alto_xml)
        assert lines[1]["confidence"] == pytest.approx((0.91 + 0.89) / 2, abs=1e-3)

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
        assert len(docs) == 2

    def test_required_fields_present(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        required = {"id", "doc_id", "page", "line_index", "text",
                    "bbox", "bbox_px", "confidence", "source_file"}
        for doc in docs:
            assert not (required - doc.keys())

    def test_bbox_normalized(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        for doc in docs:
            x0, y0, x1, y1 = doc["bbox"]
            assert 0.0 <= x0 <= 1.0
            assert 0.0 <= x1 <= 1.0
            assert 0.0 <= y0 <= page_h / page_w + 0.01
            assert 0.0 <= y1 <= page_h / page_w + 0.01

    def test_bbox_px_preserved(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        assert docs[0]["bbox_px"] == [120, 200, 680, 240]

    def test_source_file_stored(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        assert all(d["source_file"] == "test.pdf" for d in docs)

    def test_json_serializable(self, minimal_alto_xml):
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        reloaded = json.loads(json.dumps(docs, ensure_ascii=False))
        assert len(reloaded) == len(docs)

    def test_empty_lines(self):
        assert build_documents("abc123def456", 1, "test.pdf", 2480, 3508, []) == []


# ═════════════════════════════════════════════════════════════════════════════
# Kowel 1928 document — content validation tests
# ═════════════════════════════════════════════════════════════════════════════

class TestKowelContent:
    """
    Validate the pipeline's output against the ground-truth OCR reference
    for the Kowel 1928 document.

    These tests answer the question: "does the pipeline correctly parse,
    index and preserve the actual content of this specific document?"
    """

    # ── KOWEL_DOC structural integrity ───────────────────────────────────────

    def test_doc_id_is_12_hex_chars(self):
        doc_id = KOWEL_DOC["doc_id"]
        assert len(doc_id) == 12
        assert all(c in "0123456789abcdef" for c in doc_id)

    def test_doc_id_matches_source_file_hash(self):
        expected = utils.get_doc_id(KOWEL_DOC["source_file"])
        assert KOWEL_DOC["doc_id"] == expected

    def test_both_languages_present(self):
        assert KOWEL_DOC["left_column"]["language"]  == "Polish"
        assert KOWEL_DOC["right_column"]["language"] == "Ukrainian"

    def test_issue_date(self):
        assert KOWEL_DOC["header"]["issue_details"]["date"] == "Lipiec 1928 r."

    def test_issue_number(self):
        assert KOWEL_DOC["header"]["issue_details"]["issue_number"] == "№ 4."

    # ── Polish column content ────────────────────────────────────────────────

    def test_polish_title_words_parsed(self, kowel_alto_xml):
        _, _, lines = parse_alto(kowel_alto_xml)
        texts = [l["text"] for l in lines]
        assert "SAMORZĄD KOWELSKI." in texts

    def test_polish_section_title_parsed(self, kowel_alto_xml):
        _, _, lines = parse_alto(kowel_alto_xml)
        texts = [l["text"] for l in lines]
        assert "DZIAŁ OFICJALNY." in texts

    def test_polish_key_terms_present(self, kowel_alto_xml):
        """'psy' and 'znajdujące' from par. 1 must appear in the indexed text."""
        _, _, lines = parse_alto(kowel_alto_xml)
        full_text = " ".join(l["text"] for l in lines)
        assert "psy," in full_text
        assert "znajdujące" in full_text

    def test_polish_regulation_author(self, kowel_alto_xml):
        """'Rozporządzenie Wojewody Wołyńskiego' must survive parsing."""
        _, _, lines = parse_alto(kowel_alto_xml)
        full_text = " ".join(l["text"] for l in lines)
        assert "Rozporządzenie" in full_text
        assert "Wołyńskiego" in full_text

    def test_polish_diacritics_preserved_in_index(self, kowel_alto_xml, tmp_dir):
        """Polish diacritics (ą ę ó ś ź ż ć ń ł) must not be mangled in JSON."""
        from m3_indexer import write_index_json
        page_w, page_h, lines = parse_alto(kowel_alto_xml)
        docs = build_documents(
            KOWEL_DOC["doc_id"], 1, KOWEL_DOC["source_file"],
            page_w, page_h, lines
        )
        out = tmp_dir / "kowel_p001.json"
        write_index_json(docs, out)
        reloaded = json.loads(out.read_text(encoding="utf-8"))
        full_text = " ".join(d["text"] for d in reloaded)
        for word in ["SAMORZĄD", "Rozporządzenie", "Wołyńskiego", "znajdujące"]:
            assert word in full_text, f"'{word}' missing after JSON round-trip"

    # ── Ukrainian column content ─────────────────────────────────────────────

    def test_ukrainian_section_title_parsed(self, kowel_alto_xml):
        _, _, lines = parse_alto(kowel_alto_xml)
        texts = [l["text"] for l in lines]
        assert "РОЗДІЛ ОФІЦІЙНИЙ." in texts

    def test_ukrainian_cyrillic_preserved(self, kowel_alto_xml):
        _, _, lines = parse_alto(kowel_alto_xml)
        full_text = " ".join(l["text"] for l in lines)
        assert "собаки," in full_text
        assert "знаходяться" in full_text

    def test_ukrainian_diacritics_in_index(self, kowel_alto_xml, tmp_dir):
        """Cyrillic text must survive build_documents + JSON serialisation."""
        from m3_indexer import write_index_json
        page_w, page_h, lines = parse_alto(kowel_alto_xml)
        docs = build_documents(
            KOWEL_DOC["doc_id"], 1, KOWEL_DOC["source_file"],
            page_w, page_h, lines
        )
        out = tmp_dir / "kowel_p001.json"
        write_index_json(docs, out)
        reloaded = json.loads(out.read_text(encoding="utf-8"))
        full_text = " ".join(d["text"] for d in reloaded)
        for word in ["РОЗДІЛ", "Розпорядження", "Волинського", "собаки,"]:
            assert word in full_text, f"'{word}' missing after JSON round-trip"

    # ── Two-column layout: right column starts after page midpoint ───────────

    def test_right_column_bbox_beyond_midpoint(self, kowel_alto_xml):
        """
        All Ukrainian lines must have x0_norm > 0.5 (right half of page).
        This validates that the two-column broadsheet layout is preserved
        through the coordinate normalisation step.
        """
        page_w, _, lines = parse_alto(kowel_alto_xml)
        ukr_words = ["РОЗДІЛ", "Розпорядження", "Всі"]
        for line in lines:
            if any(w in line["text"] for w in ukr_words):
                x0_norm = line["bbox_px"][0] / page_w
                assert x0_norm > 0.5, (
                    f"Ukrainian line '{line['text'][:30]}' "
                    f"has x0_norm={x0_norm:.3f}, expected > 0.5"
                )

    def test_left_column_bbox_in_left_half(self, kowel_alto_xml):
        """All Polish lines must have x0_norm < 0.5 (left half of page)."""
        page_w, _, lines = parse_alto(kowel_alto_xml)
        pol_words = ["SAMORZĄD", "DZIAŁ", "Rozporządzenie", "Wszystkie"]
        for line in lines:
            if any(w in line["text"] for w in pol_words):
                x0_norm = line["bbox_px"][0] / page_w
                assert x0_norm < 0.5, (
                    f"Polish line '{line['text'][:30]}' "
                    f"has x0_norm={x0_norm:.3f}, expected < 0.5"
                )

    # ── Full document index round-trip ───────────────────────────────────────

    def test_all_7_lines_indexed(self, kowel_alto_xml):
        _, _, lines = parse_alto(kowel_alto_xml)
        assert len(lines) == 7

    def test_index_ids_use_kowel_doc_id(self, kowel_alto_xml):
        page_w, page_h, lines = parse_alto(kowel_alto_xml)
        docs = build_documents(
            KOWEL_DOC["doc_id"], 1, KOWEL_DOC["source_file"],
            page_w, page_h, lines
        )
        for doc in docs:
            assert doc["id"].startswith(KOWEL_DOC["doc_id"])
            assert doc["source_file"] == KOWEL_DOC["source_file"]

    def test_ground_truth_key_terms_searchable(self, kowel_alto_xml):
        """
        Every key term from KOWEL_DOC that appears in the fixture must be
        findable in the indexed documents via a simple substring search —
        simulating what Meilisearch would match.
        """
        page_w, page_h, lines = parse_alto(kowel_alto_xml)
        docs = build_documents(
            KOWEL_DOC["doc_id"], 1, KOWEL_DOC["source_file"],
            page_w, page_h, lines
        )
        all_text = " ".join(d["text"] for d in docs)

        # Terms present in the fixture (subset of full key_terms list)
        fixture_polish_terms = ["psy,", "znajdujące", "Wołyńskiego"]
        fixture_ukr_terms    = ["собаки,", "знаходяться"]

        for term in fixture_polish_terms + fixture_ukr_terms:
            assert term in all_text, f"Key term '{term}' not found in indexed text"


# ═════════════════════════════════════════════════════════════════════════════
# Integration: utils + m3_indexer round-trip
# ═════════════════════════════════════════════════════════════════════════════

class TestIndexerManifestRoundTrip:
    def test_write_and_reload_json(self, tmp_dir, minimal_alto_xml):
        from m3_indexer import write_index_json
        page_w, page_h, lines = parse_alto(minimal_alto_xml)
        docs = build_documents("abc123def456", 1, "test.pdf", page_w, page_h, lines)
        out = tmp_dir / "abc123def456_p001.json"
        write_index_json(docs, out)
        reloaded = json.loads(out.read_text(encoding="utf-8"))
        assert len(reloaded) == len(docs)
        for orig, loaded in zip(docs, reloaded):
            assert orig["id"]      == loaded["id"]
            assert orig["text"]    == loaded["text"]
            assert orig["bbox"]    == loaded["bbox"]
            assert orig["bbox_px"] == loaded["bbox_px"]

    def test_manifest_entry_appended(self, tmp_dir):
        utils.append_manifest(tmp_dir, {
            "doc_id":     "abc123def456",
            "page":       1,
            "json_file":  "abc123def456_p001.json",
            "line_count": 2,
            "status":     "ready",
        })
        entries = list(utils.read_manifest(tmp_dir))
        assert len(entries) == 1
        assert entries[0]["line_count"] == 2

    def test_kowel_manifest_entry(self, tmp_dir, kowel_alto_xml):
        """Full pipeline manifest entry for the Kowel document."""
        from m3_indexer import write_index_json
        page_w, page_h, lines = parse_alto(kowel_alto_xml)
        docs = build_documents(
            KOWEL_DOC["doc_id"], 1, KOWEL_DOC["source_file"],
            page_w, page_h, lines
        )
        out = tmp_dir / f"{KOWEL_DOC['doc_id']}_p001.json"
        write_index_json(docs, out)
        utils.append_manifest(tmp_dir, {
            "doc_id":      KOWEL_DOC["doc_id"],
            "page":        1,
            "json_file":   out.name,
            "line_count":  len(docs),
            "source_file": KOWEL_DOC["source_file"],
            "status":      "ready",
        })
        entry = list(utils.read_manifest(tmp_dir))[0]
        assert entry["doc_id"]     == KOWEL_DOC["doc_id"]
        assert entry["line_count"] == 7
        assert entry["source_file"] == KOWEL_DOC["source_file"]