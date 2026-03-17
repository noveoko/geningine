#!/usr/bin/env python3
"""
profile_pipeline.py — Step-level profiling report for the M1 preprocessing pipeline.

Runs every processing step on a single PDF page in isolation, measures wall-clock
time and peak memory for each, then prints a ranked breakdown so you can see
exactly where time is being spent.

Optionally also profiles the M2 OCR step (requires Tesseract installed).

Usage
-----
  # Profile M1 steps only (fastest)
  python scripts/profile_pipeline.py --pdf app/data/input_scans/my_scan.pdf

  # Include M2 OCR timing (slow — adds a full Tesseract run)
  python scripts/profile_pipeline.py --pdf app/data/input_scans/my_scan.pdf --ocr

  # Choose which page to profile (default: page 1)
  python scripts/profile_pipeline.py --pdf app/data/input_scans/my_scan.pdf --page 3

  # Repeat each step N times for stable averages (default: 1)
  python scripts/profile_pipeline.py --pdf app/data/input_scans/my_scan.pdf --repeat 3

  # Write a full cProfile call graph to disk for deeper inspection
  python scripts/profile_pipeline.py --pdf app/data/input_scans/my_scan.pdf --cprofile

Output
------
  Prints a formatted table to stdout.
  With --cprofile also writes profile_output/<step>.prof files (open with snakeviz).
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

# ── Bootstrap path so we can import app/scripts/utils.py ──────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from utils import load_config
except ImportError:
    # Fallback: just load yaml directly so the script works standalone
    import yaml
    def load_config(p=None):
        path = p or SCRIPTS_DIR.parent / "config.yaml"
        return yaml.safe_load(open(path))

# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    name: str
    wall_s: float          # wall-clock seconds (mean over repeats)
    wall_s_min: float      # fastest run
    wall_s_max: float      # slowest run
    peak_mb: float         # peak memory delta in MB
    repeats: int
    cprofile_stats: pstats.Stats | None = field(default=None, repr=False)
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Core measurement helper
# ─────────────────────────────────────────────────────────────────────────────

def measure(
    name: str,
    fn: Callable,
    repeats: int = 1,
    do_cprofile: bool = False,
) -> StepResult:
    """
    Run *fn* up to *repeats* times, recording wall-clock time and peak memory.

    How tracemalloc works
    ---------------------
    tracemalloc hooks into Python's memory allocator.  We call:
        tracemalloc.start()   — begin recording allocations
        fn()                  — the work
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()    — stop recording, free bookkeeping memory

    *peak* is the highest allocation delta above the baseline since start(),
    in bytes.  We convert to MB.  Note: this measures Python-managed heap only;
    native OpenCV/NumPy allocations may appear partially or not at all depending
    on the allocator.  It's a useful signal, not a precise total RSS figure.

    cProfile
    --------
    We wrap the *last* repeat in cProfile so the timing data reflects a warm
    run (caches, branch predictor) rather than a cold start.
    """
    times: list[float] = []

    try:
        for i in range(repeats):
            use_cprofile = do_cprofile and (i == repeats - 1)

            tracemalloc.start()
            t0 = time.perf_counter()

            if use_cprofile:
                profiler = cProfile.Profile()
                profiler.enable()

            fn()

            if use_cprofile:
                profiler.disable()

            elapsed = time.perf_counter() - t0
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            times.append(elapsed)

        stats = None
        if do_cprofile:
            stream = io.StringIO()
            ps = pstats.Stats(profiler, stream=stream).sort_stats("cumulative")
            stats = ps

        return StepResult(
            name=name,
            wall_s=sum(times) / len(times),
            wall_s_min=min(times),
            wall_s_max=max(times),
            peak_mb=peak_bytes / 1_048_576,
            repeats=repeats,
            cprofile_stats=stats,
        )

    except Exception as exc:
        return StepResult(
            name=name,
            wall_s=0.0, wall_s_min=0.0, wall_s_max=0.0,
            peak_mb=0.0, repeats=repeats,
            error=str(exc),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Individual step wrappers
# ─────────────────────────────────────────────────────────────────────────────

def profile_m1_steps(
    pdf_path: Path,
    page_index: int,
    cfg: dict,
    repeats: int,
    do_cprofile: bool,
    include_ocr: bool,
) -> list[StepResult]:
    """
    Run and time each M1 step in sequence.

    We keep intermediate results in variables so each step receives the
    realistic input it would see in production (e.g. deskew gets a grayscale
    image, not the original BGR).
    """
    import cv2
    from PIL import Image

    pre = cfg.get("preprocessing", {})
    target_dpi = cfg.get("target_dpi", 300)

    results: list[StepResult] = []

    # ── Step 1: PDF → page image ──────────────────────────────────────────
    print(f"  Profiling: PDF extraction (page {page_index}) …")
    pages_holder: list = []

    def _extract():
        try:
            from pdf2image import convert_from_path
        except ImportError:
            raise RuntimeError("pdf2image not installed: pip install pdf2image")
        pil_pages = convert_from_path(str(pdf_path), dpi=target_dpi, fmt="png",
                                       first_page=page_index, last_page=page_index)
        pages_holder.clear()
        pages_holder.extend(
            cv2.cvtColor(np.array(p), cv2.COLOR_RGB2BGR) for p in pil_pages
        )

    results.append(measure("1. PDF → BGR image (pdf2image/Poppler)", _extract, repeats, do_cprofile))

    if not pages_holder:
        print("  ERROR: could not extract page — aborting further steps.")
        return results

    img_bgr = pages_holder[0]
    print(f"     Page dimensions: {img_bgr.shape[1]}×{img_bgr.shape[0]} px")

    # ── Step 2: BGR → grayscale ───────────────────────────────────────────
    print("  Profiling: grayscale conversion …")
    gray_holder: list = []

    def _gray():
        g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if len(img_bgr.shape) == 3 else img_bgr
        gray_holder.clear()
        gray_holder.append(g)

    results.append(measure("2. BGR → grayscale", _gray, repeats, do_cprofile))
    gray = gray_holder[0]

    # ── Step 3: deskew ────────────────────────────────────────────────────
    print("  Profiling: deskew …")
    deskew_holder: list = []

    def _deskew():
        coords = np.column_stack(np.where(gray < 128))
        if coords.size == 0:
            deskew_holder.append((gray, 0.0))
            return
        pts = coords[:, ::-1].astype(np.float32)
        raw_angle = cv2.minAreaRect(pts)[-1]
        angle = -(90 + raw_angle) if raw_angle < -45 else -raw_angle
        min_a = pre.get("deskew_min_angle_deg", 0.5)
        max_a = pre.get("deskew_max_angle_deg", 15.0)
        if not (min_a < abs(angle) < max_a):
            deskew_holder.append((gray, 0.0))
            return
        h, w = gray.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
        corrected = cv2.warpAffine(gray, M, (w, h),
                                   flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REPLICATE)
        deskew_holder.append((corrected, round(float(angle), 2)))

    results.append(measure("3. Deskew (minAreaRect + warpAffine)", _deskew, repeats, do_cprofile))
    deskewed, angle = deskew_holder[0]
    print(f"     Detected skew angle: {angle:+.2f}°")

    # ── Step 4: denoise ───────────────────────────────────────────────────
    print("  Profiling: denoise (this is usually the slowest M1 step) …")
    denoised_holder: list = []

    def _denoise():
        h_strength = pre.get("denoise_h", 10)
        result = cv2.fastNlMeansDenoising(
            deskewed, h=h_strength, templateWindowSize=7, searchWindowSize=21
        )
        denoised_holder.clear()
        denoised_holder.append(result)

    results.append(measure("4. Denoise (fastNlMeansDenoising)", _denoise, repeats, do_cprofile))
    denoised = denoised_holder[0]

    # ── Step 5: binarize (even if disabled in config, we profile it) ──────
    print("  Profiling: adaptive threshold (binarize) …")

    def _binarize():
        cv2.adaptiveThreshold(
            denoised, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=pre.get("binarize_block_size", 31),
            C=pre.get("binarize_c", 15),
        )

    r = measure("5. Binarize (adaptiveThreshold) [optional]", _binarize, repeats, do_cprofile)
    if not pre.get("binarize_enabled", False):
        r.name += "  ← DISABLED in config"
    results.append(r)

    # ── Step 6: save PNG ──────────────────────────────────────────────────
    print("  Profiling: PNG save …")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp_png = Path(tf.name)

    def _save():
        from PIL import Image as PILImage
        PILImage.fromarray(denoised).save(str(tmp_png), dpi=(target_dpi, target_dpi))

    results.append(measure("6. Save PNG (Pillow)", _save, repeats, do_cprofile))

    png_size_kb = tmp_png.stat().st_size / 1024
    print(f"     Output PNG size: {png_size_kb:.0f} KB")
    tmp_png.unlink(missing_ok=True)

    # ── Step 7 (optional): OCR with Tesseract ─────────────────────────────
    if include_ocr:
        print("  Profiling: Tesseract OCR (pytesseract) …")

        # Re-save to a real tmp file so tesseract can read it
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            ocr_png = Path(tf.name)
        from PIL import Image as PILImage
        PILImage.fromarray(denoised).save(str(ocr_png), dpi=(target_dpi, target_dpi))

        def _ocr():
            try:
                import pytesseract
                lang = cfg.get("ocr", {}).get("language", "pol")
                pytesseract.image_to_string(str(ocr_png), lang=lang)
            except ImportError:
                raise RuntimeError("pytesseract not installed: pip install pytesseract")

        results.append(measure("7. OCR (Tesseract via pytesseract)", _ocr, repeats, do_cprofile))
        ocr_png.unlink(missing_ok=True)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Report renderer
# ─────────────────────────────────────────────────────────────────────────────

def _bar(fraction: float, width: int = 24) -> str:
    """
    ASCII progress bar representing *fraction* (0.0–1.0) of the total time.

    Uses block characters (█ ▓ ▒ ░) for a compact visual at any terminal width.
    """
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def render_report(
    results: list[StepResult],
    pdf_path: Path,
    page_index: int,
    repeats: int,
    cprofile_dir: Path | None,
) -> None:
    """Print the formatted timing report to stdout."""

    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    GREEN  = "\033[92m"
    CYAN   = "\033[96m"
    DIM    = "\033[2m"

    def colour_time(s: float, total: float) -> str:
        pct = s / total if total > 0 else 0
        if pct >= 0.5:
            c = RED
        elif pct >= 0.2:
            c = YELLOW
        else:
            c = GREEN
        return f"{c}{s:.3f}s{RESET}"

    ok      = [r for r in results if not r.error]
    errored = [r for r in results if r.error]
    total_s = sum(r.wall_s for r in ok)

    W = 80
    print()
    print(BOLD + "=" * W + RESET)
    print(BOLD + "  M1 Pipeline Profiling Report" + RESET)
    print(f"  PDF   : {pdf_path.name}")
    print(f"  Page  : {page_index}")
    print(f"  Reps  : {repeats}  (times shown are means)")
    print(f"  Total : {BOLD}{total_s:.3f}s{RESET}")
    print("=" * W)
    print()

    # ── Step-level table ──────────────────────────────────────────────────
    col_w = 46
    print(f"  {'Step':<{col_w}}  {'Mean':>7}  {'Min':>7}  {'Max':>7}  {'Mem MB':>7}  {'Share'}")
    print(f"  {'-'*col_w}  {'-------':>7}  {'-------':>7}  {'-------':>7}  {'-------':>7}  {'-'*26}")

    for r in ok:
        frac    = r.wall_s / total_s if total_s > 0 else 0
        bar     = _bar(frac)
        pct     = frac * 100
        c_time  = colour_time(r.wall_s, total_s)
        # Colour the percentage
        if pct >= 50:
            c_pct = RED
        elif pct >= 20:
            c_pct = YELLOW
        else:
            c_pct = GREEN
        # Truncate long step names
        name = r.name if len(r.name) <= col_w else r.name[:col_w - 1] + "…"
        print(
            f"  {name:<{col_w}}  "
            f"{c_time:>7}  "
            f"{DIM}{r.wall_s_min:.3f}s{RESET}  "
            f"{DIM}{r.wall_s_max:.3f}s{RESET}  "
            f"{r.peak_mb:>6.1f}  "
            f"  {bar} {c_pct}{pct:5.1f}%{RESET}"
        )

    for r in errored:
        print(f"  {RED}✗ {r.name:<{col_w-2}}  ERROR: {r.error}{RESET}")

    # ── Ranked summary (slowest first) ────────────────────────────────────
    print()
    print(BOLD + "  Ranked by mean time (slowest first):" + RESET)
    for i, r in enumerate(sorted(ok, key=lambda x: x.wall_s, reverse=True), 1):
        frac = r.wall_s / total_s if total_s > 0 else 0
        print(f"    {i}. {r.name:<48}  {r.wall_s:.3f}s  ({frac*100:.1f}%)")

    # ── Recommendations ───────────────────────────────────────────────────
    print()
    print(BOLD + "  Observations & suggestions:" + RESET)
    suggestions: list[str] = []

    slowest = max(ok, key=lambda r: r.wall_s) if ok else None
    if slowest:
        frac = slowest.wall_s / total_s
        if "Denoise" in slowest.name and frac > 0.4:
            suggestions.append(
                "• Denoise is the bottleneck (>40% of time).  Consider:\n"
                "    – Reducing denoise_h in config.yaml (10→5) for a 2–3× speedup with minimal quality loss.\n"
                "    – Disabling it entirely (denoise_enabled: false) for a quick quality-vs-speed test.\n"
                "    – The searchWindowSize=21 parameter dominates cost: NLM is O(w²·h) in search window size.\n"
                "      Dropping to searchWindowSize=15 cuts cost by ~(15/21)² ≈ 51%."
            )
        if "PDF" in slowest.name and frac > 0.4:
            suggestions.append(
                "• PDF extraction (Poppler) is the bottleneck.\n"
                "    – This is largely unavoidable for high-DPI rasterisation.\n"
                "    – Consider caching extracted PNGs so re-runs skip this step."
            )
        if "OCR" in slowest.name and frac > 0.5:
            suggestions.append(
                "• Tesseract OCR dominates total time.\n"
                "    – Enable OMP_THREAD_LIMIT=4 (or more) to let Tesseract use multiple cores.\n"
                "    – tesseract --oem 1 (LSTM only) is faster than --oem 3 (default combined).\n"
                "    – Consider Kraken or PaddleOCR for historical Polish scripts."
            )

    # Memory spikes
    high_mem = [r for r in ok if r.peak_mb > 200]
    if high_mem:
        for r in high_mem:
            suggestions.append(
                f"• {r.name} allocates >{r.peak_mb:.0f} MB peak.\n"
                "    – Process one page at a time (already the case in M1) rather than all pages.\n"
                "    – Ensure intermediate arrays are del'd after each step if memory is tight."
            )

    if not suggestions:
        suggestions.append("• No obvious single bottleneck — time is distributed across steps.")

    for s in suggestions:
        print(f"\n    {s}")

    # ── cProfile detail ───────────────────────────────────────────────────
    if cprofile_dir:
        print()
        print(BOLD + f"  cProfile call graphs written to: {cprofile_dir}/" + RESET)
        print(f"  {DIM}View with:  pip install snakeviz && snakeviz {cprofile_dir}/<step>.prof{RESET}")
        cprofile_dir.mkdir(parents=True, exist_ok=True)

        for r in ok:
            if r.cprofile_stats:
                # Sanitise step name to a valid filename
                fname = r.name.split(".")[0].strip().replace(" ", "_").replace("/", "_")
                prof_path = cprofile_dir / f"{fname}.prof"
                # Re-dump the profiler data
                r.cprofile_stats.dump_stats(str(prof_path))

                # Also print the top-10 functions inline
                print()
                print(f"  {CYAN}── {r.name} — top 10 by cumulative time ──{RESET}")
                stream = io.StringIO()
                ps = pstats.Stats(r.cprofile_stats.stats if hasattr(r.cprofile_stats, 'stats') else r.cprofile_stats,
                                  stream=stream)
                r.cprofile_stats.stream = stream
                r.cprofile_stats.sort_stats("cumulative").print_stats(10)
                print(stream.getvalue())

    print("=" * W)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile M1 preprocessing steps on a single PDF page.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  python scripts/profile_pipeline.py --pdf app/data/input_scans/archive_org_sanfranciscotele1904paci_0.pdf
  python scripts/profile_pipeline.py --pdf my_scan.pdf --ocr --repeat 3
  python scripts/profile_pipeline.py --pdf my_scan.pdf --cprofile
""",
    )
    parser.add_argument("--pdf",     required=True, metavar="PATH",
                        help="Path to the PDF to profile.")
    parser.add_argument("--page",    type=int, default=1, metavar="N",
                        help="Page number to extract and profile (default: 1).")
    parser.add_argument("--repeat",  type=int, default=1, metavar="N",
                        help="Repeat each step N times; report mean/min/max (default: 1).")
    parser.add_argument("--ocr",     action="store_true",
                        help="Also profile the Tesseract OCR step (requires pytesseract).")
    parser.add_argument("--cprofile", action="store_true",
                        help="Run cProfile on each step and write .prof files.")
    parser.add_argument("--config",  metavar="PATH", default=None,
                        help="Path to config.yaml (auto-detected if omitted).")
    parser.add_argument("--out-dir", metavar="PATH", default=None,
                        help="Directory for .prof files (default: profile_output/).")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"ERROR: PDF not found: {pdf_path}")
        sys.exit(1)

    cfg = load_config(args.config)

    cprofile_dir = None
    if args.cprofile:
        cprofile_dir = Path(args.out_dir or "profile_output").resolve()

    print(f"\nProfiling: {pdf_path.name}  (page {args.page}, {args.repeat} repeat(s))")
    print("Running steps...\n")

    results = profile_m1_steps(
        pdf_path=pdf_path,
        page_index=args.page,
        cfg=cfg,
        repeats=args.repeat,
        do_cprofile=args.cprofile,
        include_ocr=args.ocr,
    )

    render_report(
        results=results,
        pdf_path=pdf_path,
        page_index=args.page,
        repeats=args.repeat,
        cprofile_dir=cprofile_dir,
    )


if __name__ == "__main__":
    main()