"""
m2_ocr.py — Module 2: OCR Engine
==================================

Reads cleaned 300 DPI PNGs from     data/output_cleaned/  (via _manifest.jsonl)
Writes hOCR + ALTO XML to           data/output_ocr/
Writes DZI tile pyramids to         data/output_dzi/
Appends manifest entries to both output folders.

Processing pipeline per page
─────────────────────────────
  1. Run Tesseract → hOCR   (or Kraken → ALTO directly)
  2. Convert hOCR → ALTO v3 XML  (if using Tesseract)
  3. Generate DZI tile pyramid with pyvips
  4. Append entries to both manifests

Usage
─────
  python scripts/m2_ocr.py
  python scripts/m2_ocr.py --engine kraken --model path/to/model.mlmodel
  python scripts/m2_ocr.py --force
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

# ── importability when run directly ────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (
    append_manifest,
    load_config,
    page_stem,
    processed_set,
    read_manifest,
    setup_logging,
)

log = setup_logging(module_name="m2_ocr")


# ─────────────────────────────────────────────────────────────────────────────
# Step 1a — Tesseract OCR → hOCR
# ─────────────────────────────────────────────────────────────────────────────

def run_tesseract(
    img_path: Path,
    out_stem: Path,
    languages: list[str],
    dpi: int,
    oem: int,
    psm: int,
) -> Path:
    """
    Run Tesseract on *img_path* and write an hOCR file.

    Tesseract flags
    ───────────────
    --oem 1     LSTM neural-net engine (best accuracy for modern + historical fonts)
    --psm 6     Assume a single uniform block of text.
                Use --psm 3 for multi-column layouts or --psm 4 for single-column
                variable-size text (common in old parish registers).
    -l pol+…    Load multiple language models; Tesseract picks the best per word.

    The hOCR output is an XHTML file where every recognised word is wrapped in:
        <span class="ocrx_word" title="bbox x0 y0 x1 y1; x_wconf 92">word</span>
    """
    lang_str = "+".join(languages)
    hocr_path = out_stem.with_suffix(".hocr")

    # pytesseract is the preferred path; fall back to subprocess CLI if unavailable
    try:
        import pytesseract
        hocr_bytes = pytesseract.image_to_pdf_or_hocr(
            str(img_path),
            lang=lang_str,
            extension="hocr",
            config=f"--dpi {dpi} --oem {oem} --psm {psm}",
        )
        hocr_path.write_bytes(hocr_bytes)
    except ImportError:
        log.warning("pytesseract not installed; falling back to Tesseract CLI.")
        cmd = [
            "tesseract", str(img_path), str(out_stem),
            "-l", lang_str,
            "--dpi", str(dpi),
            "--oem", str(oem),
            "--psm", str(psm),
            "hocr",
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        # Tesseract CLI appends .hocr automatically
        hocr_path = out_stem.with_suffix("").with_suffix(".hocr")

    return hocr_path


# ─────────────────────────────────────────────────────────────────────────────
# Step 1b — Kraken OCR → ALTO XML  (alternative to Tesseract)
# ─────────────────────────────────────────────────────────────────────────────

def run_kraken(img_path: Path, out_stem: Path, model_path: str) -> Path:
    """
    Run Kraken (better for non-standard scripts and historical/handwritten docs).

    Kraken produces ALTO XML natively, so we skip the hOCR→ALTO conversion step.
    Requires a trained .mlmodel file; community models are available at:
        https://zenodo.org/communities/ocr_models
    """
    alto_path = out_stem.with_suffix(".alto.xml")
    cmd = [
        "kraken",
        "-i", str(img_path), str(alto_path),
        "segment", "-bl",
        "ocr", "-m", model_path,
    ]
    log.info("    Running Kraken …")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Kraken failed: {result.stderr}")
    return alto_path


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Convert hOCR → ALTO v3 XML
# ─────────────────────────────────────────────────────────────────────────────

def hocr_to_alto(hocr_path: Path, alto_path: Path) -> None:
    """
    Convert a Tesseract hOCR file to ALTO v3 XML.

    Preferred: use the `hocr-to-alto` CLI tool (pip install hocr-to-alto).
    Fallback: our own lightweight parser that covers the common cases.

    hOCR → ALTO field mapping
    ─────────────────────────
    ocrx_word  bbox "x0 y0 x1 y1"  →  String HPOS=x0 VPOS=y0 WIDTH=x1-x0 HEIGHT=y1-y0
    x_wconf NN  (0-100)             →  WC = NN/100   (ALTO stores 0.0-1.0)
    """
    # Try the dedicated library first
    try:
        result = subprocess.run(
            ["hocr2alto", str(hocr_path), "-o", str(alto_path)],
            capture_output=True, text=True, check=True
        )
        return
    except (subprocess.CalledProcessError, FileNotFoundError):
        log.debug("hocr2alto CLI unavailable; using built-in converter.")

    # Built-in minimal hOCR → ALTO converter
    _builtin_hocr_to_alto(hocr_path, alto_path)


def _builtin_hocr_to_alto(hocr_path: Path, alto_path: Path) -> None:
    """
    Minimal hOCR → ALTO v3 converter.

    Parses the key structural elements (pages, content areas, lines, words)
    and produces valid ALTO XML.  Does not attempt to convert every possible
    hOCR attribute — use hocr-to-alto for full compliance.
    """
    ALTO_NS = "http://www.loc.gov/standards/alto/ns-v3#"

    # Parse hOCR with lxml (falls back to ElementTree if lxml unavailable)
    try:
        from lxml import etree as _ET
        tree = _ET.parse(str(hocr_path))
        root = tree.getroot()
    except ImportError:
        tree = ET.parse(str(hocr_path))
        root = tree.getroot()

    def _get_bbox(title: str) -> tuple[int, int, int, int] | None:
        """Extract bbox (x0 y0 x1 y1) from an hOCR title attribute."""
        m = re.search(r"bbox\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", title or "")
        if not m:
            return None
        return tuple(int(v) for v in m.groups())  # type: ignore[return-value]

    def _get_wconf(title: str) -> float:
        m = re.search(r"x_wconf\s+(\d+)", title or "")
        return float(m.group(1)) / 100.0 if m else 0.0

    def _find_all(node, cls: str):
        """Find all descendant elements with a given hOCR class."""
        # Works for both lxml and ElementTree
        results = []
        for el in node.iter():
            c = el.get("class", "")
            if c == cls or cls in c.split():
                results.append(el)
        return results

    # Locate the page element
    pages_el = _find_all(root, "ocr_page")
    if not pages_el:
        log.warning("No ocr_page found in %s", hocr_path)
        pages_el = [root]

    # Build ALTO document
    alto_root = ET.Element("alto")
    alto_root.set("xmlns", ALTO_NS)
    layout = ET.SubElement(alto_root, "Layout")

    for pg_idx, pg_el in enumerate(pages_el, start=1):
        pg_title = pg_el.get("title", "")
        pg_bbox = _get_bbox(pg_title) or (0, 0, 2480, 3508)
        page_w = pg_bbox[2]
        page_h = pg_bbox[3]

        page_node = ET.SubElement(layout, "Page")
        page_node.set("ID", f"p{pg_idx:03d}")
        page_node.set("WIDTH", str(page_w))
        page_node.set("HEIGHT", str(page_h))
        page_node.set("PHYSICAL_IMG_NR", str(pg_idx))

        ps_node = ET.SubElement(page_node, "PrintSpace")
        ps_node.set("HPOS", "0")
        ps_node.set("VPOS", "0")
        ps_node.set("WIDTH", str(page_w))
        ps_node.set("HEIGHT", str(page_h))

        tb_node = ET.SubElement(ps_node, "TextBlock")
        tb_node.set("ID", "tb_1")

        for line_idx, line_el in enumerate(_find_all(pg_el, "ocr_line"), start=1):
            line_title = line_el.get("title", "")
            line_bbox = _get_bbox(line_title)
            if not line_bbox:
                continue
            lx0, ly0, lx1, ly1 = line_bbox

            tl_node = ET.SubElement(tb_node, "TextLine")
            tl_node.set("ID", f"tl_{line_idx}")
            tl_node.set("HPOS", str(lx0))
            tl_node.set("VPOS", str(ly0))
            tl_node.set("WIDTH", str(lx1 - lx0))
            tl_node.set("HEIGHT", str(ly1 - ly0))

            for word_idx, word_el in enumerate(_find_all(line_el, "ocrx_word"), start=1):
                word_title = word_el.get("title", "")
                word_bbox = _get_bbox(word_title)
                if not word_bbox:
                    continue
                wx0, wy0, wx1, wy1 = word_bbox

                # Get text content (lxml uses .text_content(), ET uses .itertext())
                try:
                    content = word_el.text_content().strip()
                except AttributeError:
                    content = "".join(word_el.itertext()).strip()

                if not content:
                    continue

                str_node = ET.SubElement(tl_node, "String")
                str_node.set("ID", f"s_{line_idx}_{word_idx}")
                str_node.set("CONTENT", content)
                str_node.set("HPOS", str(wx0))
                str_node.set("VPOS", str(wy0))
                str_node.set("WIDTH", str(wx1 - wx0))
                str_node.set("HEIGHT", str(wy1 - wy0))
                str_node.set("WC", f"{_get_wconf(word_title):.2f}")

    tree_out = ET.ElementTree(alto_root)
    ET.indent(tree_out, space="  ")
    tree_out.write(str(alto_path), encoding="unicode", xml_declaration=True)


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Generate DZI tile pyramid with pyvips
# ─────────────────────────────────────────────────────────────────────────────

def generate_dzi(img_path: Path, out_stem: Path, tile_size: int, overlap: int) -> tuple[Path, int]:
    """
    Build a Deep Zoom Image (DZI) tile pyramid from *img_path*.

    How DZI works
    ─────────────
    A DZI is a multi-resolution tile pyramid.  The original image is
    divided into a grid of tiles (default 254×254 px), and successively
    downsampled versions fill in lower zoom levels.

    Number of zoom levels for an image of size W × H:

        L = ⌈log₂(max(W, H))⌉ + 1

    At level L-1 (deepest), tiles come from the full-resolution image.
    At level L-2, the image is halved in each dimension, etc.
    At level 0, the entire image fits in a single pixel.

    At each level l, the tile count is:
        cols = ⌈W_l / tile_size⌉    where W_l = ⌈W / 2^(L-1-l)⌉

    Tile overlap (default 1 px) means adjacent tiles share one pixel on
    each shared edge — this prevents seam artefacts at high zoom.

    pyvips is used because it is dramatically faster than PIL/OpenCV for
    large images (streams processing; never loads the full image into RAM).

    Returns (dzi_descriptor_path, zoom_levels)
    """
    try:
        import pyvips
    except ImportError:
        raise RuntimeError("pyvips not installed. Run: pip install pyvips")

    import math

    image = pyvips.Image.new_from_file(str(img_path), access="sequential")
    w, h = image.width, image.height
    zoom_levels = math.ceil(math.log2(max(w, h))) + 1

    dzi_path = out_stem.with_suffix(".dzi")
    # pyvips appends .dzi and creates _files/ directory automatically
    image.dzsave(
        str(out_stem),
        tile_size=tile_size,
        overlap=overlap,
        suffix=".png",
    )

    log.debug("    DZI: %dx%d px → %d zoom levels", w, h, zoom_levels)
    return dzi_path, zoom_levels


# ─────────────────────────────────────────────────────────────────────────────
# OCR stats helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_ocr_stats(alto_path: Path) -> dict:
    """
    Parse mean confidence and word count from an ALTO v3 XML file.
    Returns {"mean_confidence": float, "word_count": int}.
    """
    try:
        from lxml import etree
        tree = etree.parse(str(alto_path))
        strings = tree.findall(".//{http://www.loc.gov/standards/alto/ns-v3#}String")
    except ImportError:
        tree = ET.parse(str(alto_path))
        strings = list(tree.iter("String"))

    if not strings:
        return {"mean_confidence": 0.0, "word_count": 0}

    confs = [float(s.get("WC", "0")) for s in strings]
    return {
        "mean_confidence": round(sum(confs) / len(confs) * 100, 1),
        "word_count": len(strings),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main processing logic
# ─────────────────────────────────────────────────────────────────────────────

def process_page(
    entry: dict,
    cleaned_dir: Path,
    ocr_dir: Path,
    dzi_dir: Path,
    cfg: dict,
    engine: str,
    kraken_model: str | None,
    ocr_done: set,
    dzi_done: set,
    force: bool,
) -> None:
    """Process a single page entry from the cleaned manifest."""
    doc_id = entry["doc_id"]
    page = entry["page"]
    img_path = cleaned_dir / entry["filename"]

    if not img_path.exists():
        log.warning("Image not found: %s, skipping.", img_path)
        return

    stem = page_stem(doc_id, page)

    # ── OCR ────────────────────────────────────────────────────────────────────
    if force or (doc_id, page) not in ocr_done:
        log.info("  OCR  page %03d  (%s)", page, engine)
        t0 = time.perf_counter()

        ocr_stem = ocr_dir / stem
        hocr_path = ocr_stem.with_suffix(".hocr")
        alto_path = ocr_stem.with_suffix(".alto.xml")
        languages_used: list[str] = []

        if engine == "kraken":
            if not kraken_model:
                raise ValueError("--model required when using Kraken engine.")
            alto_path = run_kraken(img_path, ocr_stem, kraken_model)
            languages_used = ["kraken"]
        else:
            # Tesseract
            ocr_cfg = cfg.get("ocr", {})
            langs = cfg.get("ocr_languages", ["pol"])
            run_tesseract(
                img_path,
                ocr_stem,
                languages=langs,
                dpi=entry.get("dpi", 300),
                oem=ocr_cfg.get("oem", 1),
                psm=ocr_cfg.get("psm", 6),
            )
            hocr_to_alto(hocr_path, alto_path)
            languages_used = langs

        stats = parse_ocr_stats(alto_path)
        elapsed_ocr = round(time.perf_counter() - t0, 2)

        append_manifest(ocr_dir, {
            "doc_id": doc_id,
            "page": page,
            "hocr_file": hocr_path.name if hocr_path.exists() else None,
            "alto_file": alto_path.name,
            "ocr_engine": engine,
            "languages": languages_used,
            "mean_confidence": stats["mean_confidence"],
            "word_count": stats["word_count"],
            "processing_time_s": elapsed_ocr,
            "status": "ready",
        })
        log.info("    ✓ OCR  → %s  (%.2fs, %d words, conf=%.1f%%)",
                 alto_path.name, elapsed_ocr, stats["word_count"], stats["mean_confidence"])
    else:
        log.debug("  OCR  page %03d already done, skipping.", page)

    # ── DZI ────────────────────────────────────────────────────────────────────
    if force or (doc_id, page) not in dzi_done:
        log.info("  DZI  page %03d", page)
        t0 = time.perf_counter()

        dzi_cfg = cfg.get("dzi", {})
        dzi_stem = dzi_dir / stem
        try:
            dzi_file, zoom_levels = generate_dzi(
                img_path,
                dzi_stem,
                tile_size=dzi_cfg.get("tile_size", 254),
                overlap=dzi_cfg.get("overlap", 1),
            )
        except Exception as exc:
            log.error("    DZI generation failed: %s", exc)
            return

        elapsed_dzi = round(time.perf_counter() - t0, 2)
        img_w = entry.get("width_px", 0)
        img_h = entry.get("height_px", 0)

        append_manifest(dzi_dir, {
            "doc_id": doc_id,
            "page": page,
            "dzi_file": dzi_file.name,
            "tile_dir": f"{stem}_files/",
            "image_width": img_w,
            "image_height": img_h,
            "zoom_levels": zoom_levels,
            "processing_time_s": elapsed_dzi,
            "status": "ready",
        })
        log.info("    ✓ DZI → %s  (%.2fs, %d levels)", dzi_file.name, elapsed_dzi, zoom_levels)
    else:
        log.debug("  DZI  page %03d already done, skipping.", page)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Module 2 — Run OCR and generate DZI tiles."
    )
    parser.add_argument("--engine", choices=["tesseract", "kraken"], default=None,
                        help="OCR engine override (default: from config.yaml).")
    parser.add_argument("--model", type=str, default=None,
                        help="Kraken .mlmodel path (required when --engine=kraken).")
    parser.add_argument("--force", action="store_true",
                        help="Reprocess pages already in the manifests.")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    root = Path(args.config).parent if args.config else Path(__file__).resolve().parent.parent

    engine = args.engine or cfg.get("ocr_engine", "tesseract")

    cleaned_dir = root / cfg["paths"]["output_cleaned"]
    ocr_dir = root / cfg["paths"]["output_ocr"]
    dzi_dir = root / cfg["paths"]["output_dzi"]
    ocr_dir.mkdir(parents=True, exist_ok=True)
    dzi_dir.mkdir(parents=True, exist_ok=True)

    # Load already-processed sets once
    ocr_done = processed_set(ocr_dir)
    dzi_done = processed_set(dzi_dir)

    entries = list(read_manifest(cleaned_dir))
    if not entries:
        log.info("No entries found in cleaned manifest. Run m1_preprocess.py first.")
        return

    log.info("Found %d pages in cleaned manifest. Engine: %s", len(entries), engine)

    for entry in entries:
        process_page(
            entry, cleaned_dir, ocr_dir, dzi_dir,
            cfg, engine, args.model,
            ocr_done, dzi_done, args.force,
        )

    log.info("Module 2 complete.")


if __name__ == "__main__":
    main()
