"""
m1_preprocess.py — Module 1: Pre-processor
===========================================

Reads raw scans (PDF, TIFF, JPEG, PNG) from  data/input_scans/
Writes cleaned 300 DPI grayscale PNGs to     data/output_cleaned/
Appends one JSONL line per page to           data/output_cleaned/_manifest.jsonl

Processing pipeline per page
─────────────────────────────
  1. Detect file type → extract page(s) as NumPy arrays at target DPI
  2. Convert to grayscale
  3. Deskew  (minAreaRect angle → warpAffine)
  4. Denoise (fastNlMeansDenoising)
  5. Optional: adaptive threshold (Gaussian, for uneven lighting)
  6. Save as 300 DPI PNG
  7. Append manifest entry

Usage
─────
  python scripts/m1_preprocess.py                       # process all new files
  python scripts/m1_preprocess.py --file my_scan.pdf    # single file
  python scripts/m1_preprocess.py --force               # reprocess everything
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# ── make sure utils is importable when running the script directly ─────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (
    already_processed,
    append_manifest,
    get_doc_id,
    load_config,
    page_stem,
    processed_set,
    setup_logging,
)

log = setup_logging(module_name="m1_preprocess")


# ─────────────────────────────────────────────────────────────────────────────
# Step helpers
# ─────────────────────────────────────────────────────────────────────────────

def extract_pages(file_path: Path, target_dpi: int) -> list[np.ndarray]:
    """
    Step 1 — Extract all pages from *file_path* as BGR NumPy arrays.

    Supports: .pdf, .tiff/.tif, .jpg/.jpeg, .png

    For PDFs: pdf2image wraps Poppler's pdftoppm, converting each page at
              *target_dpi* before handing us a list of PIL Images.
    For images: OpenCV loads directly.  If the image DPI metadata is below
                *target_dpi*, we upscale using Lanczos interpolation — this
                is critical because Tesseract accuracy degrades below 300 DPI.
    """
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        try:
            from pdf2image import convert_from_path
        except ImportError:
            raise RuntimeError("pdf2image not installed. Run: pip install pdf2image")
        log.info("  Extracting PDF pages at %d DPI …", target_dpi)
        pil_pages = convert_from_path(str(file_path), dpi=target_dpi, fmt="png")
        return [cv2.cvtColor(np.array(p), cv2.COLOR_RGB2BGR) for p in pil_pages]

    elif suffix in {".tiff", ".tif"}:
        # TIFF can contain multiple frames (pages)
        ret, frames = [], True
        cap = cv2.VideoCapture(str(file_path))  # won't work for multi-frame TIFF
        # Use Pillow instead, which handles multi-frame TIFF natively
        pil_img = Image.open(file_path)
        pages: list[np.ndarray] = []
        try:
            while True:
                frame = np.array(pil_img.convert("RGB"))
                pages.append(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                pil_img.seek(pil_img.tell() + 1)
        except EOFError:
            pass
        return pages

    else:  # .jpg, .jpeg, .png, etc.
        img = cv2.imread(str(file_path))
        if img is None:
            raise ValueError(f"OpenCV could not read: {file_path}")

        # Check embedded DPI metadata via Pillow and upscale if needed
        pil_img = Image.open(file_path)
        dpi_info = pil_img.info.get("dpi", (target_dpi, target_dpi))
        actual_dpi = dpi_info[0] if dpi_info else target_dpi

        if actual_dpi < target_dpi:
            scale = target_dpi / actual_dpi
            log.warning(
                "  Image DPI (%d) below target (%d); upscaling ×%.2f with Lanczos …",
                actual_dpi, target_dpi, scale
            )
            h, w = img.shape[:2]
            new_w, new_h = int(w * scale), int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

        return [img]


def to_grayscale(img: np.ndarray) -> np.ndarray:
    """Step 2 — Convert BGR image to single-channel grayscale."""
    if len(img.shape) == 2:
        return img  # already grayscale
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def deskew(gray: np.ndarray, min_angle: float, max_angle: float) -> tuple[np.ndarray, float]:
    """
    Step 3 — Correct rotation of a scanned page.

    Algorithm
    ---------
    1. Find all dark pixel coordinates  (text = dark on light background).
    2. Fit the minimum-area bounding rectangle around those points.
       cv2.minAreaRect() returns (center, (width, height), angle) where
       *angle* is in the range [-90, 0).
    3. Adjust the raw angle to get the true skew angle:
         if angle < -45°  →  skew = -(90 + angle)   [nearly vertical]
         else             →  skew = -angle            [nearly horizontal]
    4. Build a 2×3 rotation matrix M centred on the image midpoint:

           ⎡ cos θ   sin θ   tx ⎤
       M = ⎣-sin θ   cos θ   ty ⎦

       where (tx, ty) keep the centre of rotation at (w/2, h/2).
    5. Apply M to every pixel with warpAffine (bilinear interpolation,
       border replicated to avoid black edges).

    Returns
    -------
    (corrected_image, detected_angle_deg)
    If the angle is outside [min_angle, max_angle], returns the original
    image unchanged with angle=0.0.
    """
    # Dark pixels only (text)
    coords = np.column_stack(np.where(gray < 128))

    if coords.size == 0:
        log.debug("    Deskew: no dark pixels found, skipping.")
        return gray, 0.0

    # minAreaRect expects float32 points in (x, y) order; np.where gives (row=y, col=x)
    pts = coords[:, ::-1].astype(np.float32)
    raw_angle = cv2.minAreaRect(pts)[-1]  # in [-90, 0)

    # Convert to true skew angle
    angle = -(90 + raw_angle) if raw_angle < -45 else -raw_angle

    if not (min_angle < abs(angle) < max_angle):
        log.debug("    Deskew: angle %.2f° outside correction range, skipping.", angle)
        return gray, 0.0

    log.debug("    Deskew: correcting %.2f°", angle)
    (h, w) = gray.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    corrected = cv2.warpAffine(
        gray, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE
    )
    return corrected, round(float(angle), 2)


def denoise(gray: np.ndarray, h: int = 10) -> np.ndarray:
    """
    Step 4 — Non-local means denoising.

    NLM works by comparing small neighbourhoods (templateWindowSize=7×7)
    across a larger search window (searchWindowSize=21×21) and averaging
    pixels whose surroundings look similar.  This preserves sharp text edges
    while removing scanner grain.  Parameter *h* controls filter strength:
    10 is safe for 300 DPI scans; go lower if letters look blurry.
    """
    return cv2.fastNlMeansDenoising(
        gray, h=h, templateWindowSize=7, searchWindowSize=21
    )


def binarize(gray: np.ndarray, block_size: int = 31, c: int = 15) -> np.ndarray:
    """
    Step 5 — Adaptive Gaussian thresholding (optional).

    For each pixel at position (x, y), the threshold T(x,y) is computed as:

        T(x,y) = GaussianWeightedMean(neighbourhood of size block_size) − C

    Pixel output:
        255 (white)  if pixel(x,y) > T(x,y)
          0 (black)  otherwise

    This handles uneven illumination (e.g. book gutters, yellowed paper) far
    better than a single global threshold.  *block_size* must be an odd integer;
    larger values smooth over bigger illumination gradients.
    """
    return cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=block_size,
        C=c
    )


def save_png(img: np.ndarray, out_path: Path, dpi: int = 300) -> None:
    """Step 6 — Save grayscale image as a PNG with correct DPI metadata."""
    pil_img = Image.fromarray(img)
    pil_img.save(str(out_path), dpi=(dpi, dpi))


# ─────────────────────────────────────────────────────────────────────────────
# Main processing logic
# ─────────────────────────────────────────────────────────────────────────────

def process_file(
    file_path: Path,
    cfg: dict,
    out_dir: Path,
    force: bool = False,
) -> int:
    """
    Process all pages of a single input file.

    Returns the number of pages successfully written.
    """
    doc_id = get_doc_id(file_path.name)
    target_dpi = cfg["target_dpi"]
    pre = cfg.get("preprocessing", {})

    log.info("Processing '%s'  →  doc_id=%s", file_path.name, doc_id)

    # Load the processed set once (faster than per-page manifest scan)
    done = processed_set(out_dir)

    try:
        pages = extract_pages(file_path, target_dpi)
    except Exception as exc:
        log.error("  Failed to extract pages: %s", exc)
        return 0

    written = 0
    for page_num, img in enumerate(pages, start=1):
        if not force and (doc_id, page_num) in done:
            log.debug("  Page %d already processed, skipping.", page_num)
            continue

        t_start = time.perf_counter()

        # Step 2: grayscale
        gray = to_grayscale(img)

        # Step 3: deskew
        angle = 0.0
        if pre.get("deskew_enabled", True):
            gray, angle = deskew(
                gray,
                min_angle=pre.get("deskew_min_angle_deg", 0.5),
                max_angle=pre.get("deskew_max_angle_deg", 15.0),
            )

        # Step 4: denoise
        if pre.get("denoise_enabled", True):
            gray = denoise(gray, h=pre.get("denoise_h", 10))

        # Step 5: binarize (optional)
        binarized = False
        if pre.get("binarize_enabled", False):
            gray = binarize(
                gray,
                block_size=pre.get("binarize_block_size", 31),
                c=pre.get("binarize_c", 15),
            )
            binarized = True

        # Step 6: save
        stem = page_stem(doc_id, page_num)
        out_path = out_dir / f"{stem}.png"
        save_png(gray, out_path, dpi=target_dpi)

        h_px, w_px = gray.shape[:2]
        elapsed = round(time.perf_counter() - t_start, 2)

        # Step 7: manifest
        append_manifest(out_dir, {
            "doc_id": doc_id,
            "page": page_num,
            "filename": out_path.name,
            "source_file": file_path.name,
            "width_px": w_px,
            "height_px": h_px,
            "dpi": target_dpi,
            "deskew_angle_deg": angle,
            "binarized": binarized,
            "processing_time_s": elapsed,
            "status": "ready",
        })

        log.info("  ✓ Page %03d → %s  (%.2fs, skew=%.1f°)", page_num, out_path.name, elapsed, angle)
        written += 1

    return written


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Module 1 — Pre-process raw scans into cleaned 300 DPI PNGs."
    )
    parser.add_argument(
        "--file", "-f", type=str, default=None,
        help="Process a single file only (path relative to project root or absolute)."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Reprocess files even if they already appear in the manifest."
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to config.yaml (auto-detected if omitted)."
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    root = Path(args.config).parent if args.config else Path(__file__).resolve().parent.parent

    in_dir = root / cfg["paths"]["input_scans"]
    out_dir = root / cfg["paths"]["output_cleaned"]
    out_dir.mkdir(parents=True, exist_ok=True)

    supported = {".pdf", ".tiff", ".tif", ".jpg", ".jpeg", ".png"}

    if args.file:
        candidates = [Path(args.file)]
    else:
        candidates = [p for p in sorted(in_dir.iterdir()) if p.suffix.lower() in supported]

    if not candidates:
        log.info("No input files found in %s", in_dir)
        return

    total_pages = 0
    for file_path in candidates:
        if not file_path.exists():
            log.error("File not found: %s", file_path)
            continue
        total_pages += process_file(file_path, cfg, out_dir, force=args.force)

    log.info("Done. Total pages processed: %d", total_pages)


if __name__ == "__main__":
    main()
