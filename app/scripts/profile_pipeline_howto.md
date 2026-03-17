Drop it in `app/scripts/` and run it:

```bash
# Basic run — profiles all M1 steps on page 1
uv run app/scripts/profile_pipeline.py \
  --pdf app/data/input_scans/archive_org_sanfranciscotele1904paci_0.pdf

# Stable averages over 3 repeats + include Tesseract
uv run app/scripts/profile_pipeline.py \
  --pdf app/data/input_scans/archive_org_sanfranciscotele1904paci_0.pdf \
  --repeat 3 --ocr

# Deep cProfile call graph (install snakeviz for a browser UI)
uv run app/scripts/profile_pipeline.py \
  --pdf app/data/input_scans/archive_org_sanfranciscotele1904paci_0.pdf \
  --cprofile
# then: snakeviz profile_output/4_Denoise.prof
```

The report looks like this (colours in terminal):

```
================================================================================
  M1 Pipeline Profiling Report
  PDF   : archive_org_sanfranciscotele1904paci_0.pdf
  Page  : 1
  Reps  : 1
  Total : 4.821s
================================================================================

  Step                                             Mean     Min     Max  Mem MB  Share
  ─────────────────────────────────────────────    ──────   ──────  ────── ───────  ──────────────────────────
  1. PDF → BGR image (pdf2image/Poppler)          0.843s  0.843s  0.843s   48.2    ████░░░░░░░░░░░░░░░░░░░░  17.5%
  2. BGR → grayscale                              0.004s  0.004s  0.004s    3.1    ░░░░░░░░░░░░░░░░░░░░░░░░   0.1%
  3. Deskew (minAreaRect + warpAffine)            0.312s  0.312s  0.312s   12.4    ██░░░░░░░░░░░░░░░░░░░░░░   6.5%
  4. Denoise (fastNlMeansDenoising)               3.401s  3.401s  3.401s  124.7    ████████████████░░░░░░░░  70.5%
  5. Binarize (adaptiveThreshold) ← DISABLED      0.089s  0.089s  0.089s    8.2    ░░░░░░░░░░░░░░░░░░░░░░░░   1.8%
  6. Save PNG (Pillow)                            0.172s  0.172s  0.172s   22.1    █░░░░░░░░░░░░░░░░░░░░░░░   3.6%

  Observations & suggestions:

    • Denoise is the bottleneck (>40% of time). Consider:
        – Reducing denoise_h in config.yaml (10→5) for a 2–3× speedup with minimal quality loss.
        – The searchWindowSize=21 parameter dominates cost: NLM is O(w²·h) in search window size.
          Dropping to searchWindowSize=15 cuts cost by ~(15/21)² ≈ 51%.
```

Three things the report gives you that `tqdm` alone can't:

- **Per-step memory delta** via `tracemalloc` — useful for spotting if denoise is blowing up RAM on large pages
- **Min/max across repeats** — if max >> min that means the OS is doing something (disk flush, GC) and a single-run time is misleading
- **Automatic recommendations** keyed to whichever step dominates — the denoise advice in particular (reducing `searchWindowSize` from 21→15 gives roughly a 2× speedup because NLM cost scales quadratically with search window size)