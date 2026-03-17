"""
m3_indexer.py — Module 3: Indexer
===================================

Reads ALTO v3 XML from          data/output_ocr/   (via _manifest.jsonl)
Writes Meilisearch JSON to      data/output_index/
Uploads documents to            Meilisearch  (index: genealogy_pages)

Processing pipeline per page
─────────────────────────────
  1. Parse ALTO XML → extract <String> elements grouped by <TextLine>
  2. Compute normalized bounding boxes (both axes ÷ page width — OSD convention)
  3. Assemble line-level index documents
  4. Write JSON to output_index/
  5. Batch-upload to Meilisearch (1000 docs per request)

Coordinate system note
──────────────────────
OpenSeadragon (OSD) normalises BOTH x and y by the image WIDTH (not height).
So the viewport x-range is [0, 1] and y-range is [0, H/W].
If you normalise y by H you will get squashed or stretched overlays.
This is the most common source of overlay bugs — see normalize_bbox() below.

Usage
─────
  python scripts/m3_indexer.py
  python scripts/m3_indexer.py --no-upload   # parse + write JSON but skip Meilisearch
  python scripts/m3_indexer.py --force
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# ── importability when run directly ────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (
    append_manifest,
    chunked,
    load_config,
    page_stem,
    processed_set,
    read_manifest,
    setup_logging,
)

log = setup_logging(module_name="m3_indexer")

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v3#"


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Parse ALTO XML
# ─────────────────────────────────────────────────────────────────────────────

def parse_alto(alto_path: Path) -> tuple[int, int, list[dict]]:
    """
    Parse an ALTO v3 XML file and return (page_width, page_height, lines).

    Each element of *lines* is:
        {
            "text":       str,           # space-joined word contents
            "words":      list[dict],    # raw word dicts (content, hpos, vpos, …)
            "bbox_px":    [x0, y0, x1, y1],  # pixel coords of the line
            "confidence": float,         # average word confidence 0–1
        }

    ALTO coordinate system (top-left origin):
        HPOS   = horizontal position from left edge  → x0
        VPOS   = vertical position from top edge     → y0
        WIDTH  → x1 = HPOS + WIDTH
        HEIGHT → y1 = VPOS + HEIGHT

    We use lxml when available for speed; fall back to stdlib ElementTree.
    """
    try:
        from lxml import etree
        tree = etree.parse(str(alto_path))
        page_el = tree.find(f".//{{{ALTO_NS}}}Page")
    except ImportError:
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(alto_path))
        page_el = tree.find(f".//{{{ALTO_NS}}}Page")

    if page_el is None:
        raise ValueError(f"No <Page> element found in {alto_path}")

    page_w = int(page_el.get("WIDTH", 2480))
    page_h = int(page_el.get("HEIGHT", 3508))

    lines: list[dict] = []

    try:
        line_iter = tree.iter(f"{{{ALTO_NS}}}TextLine")
    except AttributeError:
        import xml.etree.ElementTree as ET
        line_iter = tree.iter(f"{{{ALTO_NS}}}TextLine")

    for line_el in line_iter:
        try:
            string_els = list(line_el.iter(f"{{{ALTO_NS}}}String"))
        except Exception:
            continue

        if not string_els:
            continue

        words: list[dict] = []
        for s in string_els:
            content = s.get("CONTENT", "").strip()
            if not content:
                continue
            try:
                hpos   = int(s.get("HPOS",   "0"))
                vpos   = int(s.get("VPOS",   "0"))
                width  = int(s.get("WIDTH",  "0"))
                height = int(s.get("HEIGHT", "0"))
                wc     = float(s.get("WC",   "0"))
            except (TypeError, ValueError):
                continue

            words.append({
                "content":  content,
                "hpos":     hpos,
                "vpos":     vpos,
                "width":    width,
                "height":   height,
                "confidence": wc,
            })

        if not words:
            continue

        # Line bounding box = union of all word boxes
        x0 = min(w["hpos"] for w in words)
        y0 = min(w["vpos"] for w in words)
        x1 = max(w["hpos"] + w["width"]  for w in words)
        y1 = max(w["vpos"] + w["height"] for w in words)

        avg_conf = sum(w["confidence"] for w in words) / len(words)

        lines.append({
            "text":       " ".join(w["content"] for w in words),
            "words":      words,
            "bbox_px":    [x0, y0, x1, y1],
            "confidence": round(avg_conf, 4),
        })

    return page_w, page_h, lines


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Normalize bounding boxes to OSD viewport coordinates
# ─────────────────────────────────────────────────────────────────────────────

def normalize_bbox(
    x0: int, y0: int, x1: int, y1: int, page_w: int
) -> list[float]:
    """
    Convert a pixel bounding box to OpenSeadragon viewport coordinates.

    OpenSeadragon coordinate system
    ────────────────────────────────
    OSD defines its viewport so that the image WIDTH = 1.0 exactly.
    The image height therefore equals H/W in viewport units.

    CRITICAL: Both x AND y coordinates are divided by the image WIDTH (not height).
    This is counter-intuitive but intentional — OSD uses the width as the
    universal unit for both axes so that aspect ratio is always preserved.

    Given pixel box [x0, y0, x1, y1] on an image of width W:

        x0_norm = x0 / W
        y0_norm = y0 / W     ← divided by WIDTH, not HEIGHT
        x1_norm = x1 / W
        y1_norm = y1 / W     ← divided by WIDTH, not HEIGHT

    If you mistakenly divide y by H:
        - portrait images: y coordinates appear compressed
        - landscape images: y coordinates appear stretched
    """
    w = float(page_w)
    return [
        round(x0 / w, 6),
        round(y0 / w, 6),   # ← divide by WIDTH
        round(x1 / w, 6),
        round(y1 / w, 6),   # ← divide by WIDTH
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Assemble index documents
# ─────────────────────────────────────────────────────────────────────────────

def build_documents(
    doc_id: str,
    page: int,
    source_file: str,
    page_w: int,
    page_h: int,
    lines: list[dict],
) -> list[dict[str, Any]]:
    """
    Build a list of Meilisearch index documents from parsed ALTO lines.

    Document schema
    ───────────────
    id          Primary key.  Format: {doc_id}_p{page:03d}_l{line:04d}
    doc_id      Links back to source document across all pipeline modules.
    page        1-indexed page number.
    line_index  0-indexed line number within the page.
    text        Searchable: full text of the OCR line.
    bbox        [x0, y0, x1, y1] in OSD viewport coordinates (both ÷ page_w).
    bbox_px     [x0, y0, x1, y1] in original pixel coordinates.
    confidence  Average OCR confidence for the line (0.0–1.0).
    source_file Original filename for display in the UI.
    """
    docs: list[dict] = []
    for idx, line in enumerate(lines):
        bpx = line["bbox_px"]
        bbox_norm = normalize_bbox(bpx[0], bpx[1], bpx[2], bpx[3], page_w)

        docs.append({
            "id":          f"{doc_id}_p{page:03d}_l{idx:04d}",
            "doc_id":      doc_id,
            "page":        page,
            "line_index":  idx,
            "text":        line["text"],
            "bbox":        bbox_norm,
            "bbox_px":     bpx,
            "confidence":  line["confidence"],
            "source_file": source_file,
        })
    return docs


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Write JSON
# ─────────────────────────────────────────────────────────────────────────────

def write_index_json(documents: list[dict], out_path: Path) -> None:
    """Serialize the document list to a JSON file (UTF-8, no ASCII escaping)."""
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(documents, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Upload to Meilisearch
# ─────────────────────────────────────────────────────────────────────────────

def configure_index(index) -> None:
    """
    Set Meilisearch index settings (run once; safe to re-run — idempotent).

    searchableAttributes   Only 'text' is full-text searched.
    filterableAttributes   Allows filter: "confidence > 0.5" or "doc_id = X".
    sortableAttributes     Enables ORDER BY in queries.
    typoTolerance          Historical OCR produces many typos; we tolerate up to
                           2 typos for words ≥7 chars, 1 typo for words ≥4 chars.
    """
    index.update_settings({
        "searchableAttributes": ["text"],
        "filterableAttributes": ["doc_id", "page", "confidence"],
        "sortableAttributes": ["page", "line_index", "confidence"],
        "typoTolerance": {
            "enabled": True,
            "minWordSizeForTypos": {
                "oneTypo":  4,
                "twoTypos": 7,
            },
        },
    })


def upload_to_meilisearch(
    documents: list[dict],
    ms_url: str,
    ms_key: str,
    index_name: str,
    batch_size: int,
) -> None:
    """
    Upload documents to Meilisearch in batches.

    Meilisearch is an open-source, self-hosted full-text search engine.
    It accepts documents as JSON arrays.  We split into batches to avoid
    hitting the HTTP request size limit.
    """
    try:
        import meilisearch
    except ImportError:
        raise RuntimeError("meilisearch not installed. Run: pip install meilisearch")

    client = meilisearch.Client(ms_url, ms_key or None)
    index = client.index(index_name)

    # Configure settings (idempotent)
    configure_index(index)

    total = 0
    for batch in chunked(documents, batch_size):
        task = index.add_documents(batch)
        total += len(batch)
        log.debug("    Uploaded batch of %d (task uid=%s)", len(batch), task.task_uid)

    log.info("    Uploaded %d documents to Meilisearch index '%s'", total, index_name)


# ─────────────────────────────────────────────────────────────────────────────
# Main processing logic
# ─────────────────────────────────────────────────────────────────────────────

def process_page(
    ocr_entry: dict,
    cleaned_manifest_index: dict[tuple[str, int], dict],
    ocr_dir: Path,
    index_dir: Path,
    cfg: dict,
    no_upload: bool,
    done: set,
    force: bool,
) -> list[dict]:
    """Process one page entry from the OCR manifest.  Returns document list."""
    doc_id = ocr_entry["doc_id"]
    page   = ocr_entry["page"]

    if not force and (doc_id, page) in done:
        log.debug("  Page %03d already indexed, skipping.", page)
        return []

    alto_file = ocr_entry.get("alto_file")
    if not alto_file:
        log.warning("  No alto_file for %s page %d, skipping.", doc_id, page)
        return []

    alto_path = ocr_dir / alto_file
    if not alto_path.exists():
        log.warning("  ALTO file not found: %s", alto_path)
        return []

    # Get source filename from the cleaned manifest (for display in UI)
    cleaned_entry = cleaned_manifest_index.get((doc_id, page), {})
    source_file = cleaned_entry.get("source_file", "unknown")

    log.info("  Indexing page %03d  (%s)", page, alto_path.name)
    t0 = time.perf_counter()

    try:
        page_w, page_h, lines = parse_alto(alto_path)
    except Exception as exc:
        log.error("    Failed to parse ALTO: %s", exc)
        return []

    documents = build_documents(doc_id, page, source_file, page_w, page_h, lines)

    # Write JSON
    stem = page_stem(doc_id, page)
    json_path = index_dir / f"{stem}.json"
    write_index_json(documents, json_path)

    elapsed = round(time.perf_counter() - t0, 2)
    log.info("    ✓ %d lines → %s  (%.2fs)", len(documents), json_path.name, elapsed)

    return documents


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Module 3 — Parse ALTO XML and index into Meilisearch."
    )
    parser.add_argument("--no-upload", action="store_true",
                        help="Parse and write JSON but skip Meilisearch upload.")
    parser.add_argument("--force", action="store_true",
                        help="Re-index pages already in the index manifest.")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    root = Path(args.config).parent if args.config else Path(__file__).resolve().parent.parent

    ocr_dir   = root / cfg["paths"]["output_ocr"]
    index_dir = root / cfg["paths"]["output_index"]
    cleaned_dir = root / cfg["paths"]["output_cleaned"]
    index_dir.mkdir(parents=True, exist_ok=True)

    ms_cfg    = cfg.get("meilisearch", {})
    ms_url    = ms_cfg.get("url",        "http://127.0.0.1:7700")
    ms_key    = ms_cfg.get("api_key",    "")
    idx_name  = ms_cfg.get("index_name", "genealogy_pages")
    batch_sz  = ms_cfg.get("batch_size", 1000)

    # Build a fast lookup: (doc_id, page) → cleaned manifest entry
    cleaned_index: dict[tuple[str, int], dict] = {
        (e["doc_id"], e["page"]): e
        for e in read_manifest(cleaned_dir)
    }

    ocr_entries = list(read_manifest(ocr_dir))
    if not ocr_entries:
        log.info("No OCR entries found. Run m2_ocr.py first.")
        return

    done = processed_set(index_dir)
    all_docs: list[dict] = []

    for entry in ocr_entries:
        docs = process_page(
            entry, cleaned_index,
            ocr_dir, index_dir,
            cfg, args.no_upload, done, args.force,
        )
        all_docs.extend(docs)

    if all_docs and not args.no_upload:
        log.info("Uploading %d documents to Meilisearch …", len(all_docs))
        try:
            upload_to_meilisearch(all_docs, ms_url, ms_key, idx_name, batch_sz)
        except Exception as exc:
            log.error("Meilisearch upload failed: %s", exc)
            log.info("JSON files are saved in %s — you can retry the upload later.", index_dir)
    elif args.no_upload:
        log.info("--no-upload set. Skipped Meilisearch. JSON in %s", index_dir)

    log.info("Module 3 complete.")


if __name__ == "__main__":
    main()
