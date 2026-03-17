# Genealogy OCR Pipeline

Full pipeline for converting raw scans of historical records (parish registers,
directories, vital records) into a searchable, deep-zoom web archive.

```
PDF / TIFF / JPEG / PNG
         │
         ▼
  [Module 1] m1_preprocess.py
  Deskew · Denoise · Binarize → 300 DPI grayscale PNGs
         │
         ▼
  [Module 2] m2_ocr.py
  Tesseract / Kraken → hOCR + ALTO XML
  pyvips             → DZI tile pyramids
         │
         ▼
  [Module 3] m3_indexer.py
  Parse ALTO → normalize bboxes → Meilisearch JSON + upload
         │
         ▼
  [Module 4] React frontend
  Meilisearch instant-search + OpenSeadragon deep zoom viewer
  with bounding-box overlays on search hits
```

## Quick start

### 1. System dependencies

```bash
# Ubuntu / Debian / WSL
sudo apt install -y \
  tesseract-ocr tesseract-ocr-pol tesseract-ocr-rus tesseract-ocr-deu tesseract-ocr-lat \
  libvips-tools \
  poppler-utils \
  imagemagick

# Meilisearch (single binary, no Docker needed)
curl -L https://install.meilisearch.com | sh
```

### 2. Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Frontend dependencies

```bash
cd frontend && npm install
```

### 4. Run the pipeline

```bash
# Drop your scans here:
cp my_parish_register.pdf data/input_scans/

# Step through the pipeline:
python scripts/m1_preprocess.py   # clean + deskew
python scripts/m2_ocr.py          # OCR + DZI tiles
python scripts/m3_indexer.py      # index into Meilisearch

# Start all services:
bash scripts/m4_serve.sh
# → Frontend:  http://127.0.0.1:5173
# → Search:    http://127.0.0.1:7700
# → Tiles:     http://127.0.0.1:8001
```

### 5. Run tests

```bash
pytest tests/ -v
pytest tests/ -v --cov=scripts --cov-report=term-missing
```

---

## Directory layout

```
project_root/
├── config.yaml                  ← DPI, languages, Meilisearch URL, engine
├── requirements.txt
├── pytest.ini
├── data/
│   ├── input_scans/             ← DROP RAW FILES HERE
│   ├── output_cleaned/          ← 300 DPI grayscale PNGs + _manifest.jsonl
│   ├── output_ocr/              ← .hocr + .alto.xml + _manifest.jsonl
│   ├── output_dzi/              ← .dzi + _files/ tiles + _manifest.jsonl
│   └── output_index/            ← Meilisearch JSON batches
├── scripts/
│   ├── utils.py                 ← Shared: doc_id, manifest, chunked
│   ├── m1_preprocess.py         ← Module 1: Pre-processor
│   ├── m2_ocr.py                ← Module 2: OCR + DZI
│   ├── m3_indexer.py            ← Module 3: Indexer
│   └── m4_serve.sh              ← Module 4: Start services
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── searchClient.js
│   │   └── components/
│   │       ├── SearchBar.jsx
│   │       ├── ResultsList.jsx
│   │       ├── ResultItem.jsx
│   │       └── OpenSeadragonViewer.jsx
│   ├── index.html
│   ├── vite.config.js
│   └── package.json
└── tests/
    └── test_pipeline.py         ← 70 unit + integration tests
```

---

## Module details

### Module 1 — Pre-processor (`m1_preprocess.py`)

| Flag | Default | Description |
|------|---------|-------------|
| `--file` | all files | Process a single file |
| `--force` | off | Reprocess already-done pages |
| `--config` | auto | Path to `config.yaml` |

Processing steps per page:
1. **Extract** — `pdf2image` for PDFs, OpenCV + Pillow for images; upscales to 300 DPI if needed
2. **Grayscale** — `cv2.cvtColor(BGR → GRAY)`
3. **Deskew** — `cv2.minAreaRect` → angle → `cv2.warpAffine` with rotation matrix M
4. **Denoise** — `cv2.fastNlMeansDenoising` (h=10)
5. **Binarize** (optional) — adaptive Gaussian threshold (blockSize=31, C=15)
6. **Save** — 300 DPI PNG via Pillow

### Module 2 — OCR Engine (`m2_ocr.py`)

| Flag | Default | Description |
|------|---------|-------------|
| `--engine` | from config | `tesseract` or `kraken` |
| `--model` | — | Kraken `.mlmodel` path (required for Kraken) |
| `--force` | off | Reprocess already-done pages |

Tesseract flags: `--oem 1` (LSTM), `--psm 6` (uniform block), `-l pol+rus+deu+lat`.
For multi-column layouts (common in directories) change `psm` to `3` or `4` in `config.yaml`.

DZI zoom levels: $L = \lceil\log_2(\max(W,H))\rceil + 1$. A 2480×3508 scan produces 13 levels.

### Module 3 — Indexer (`m3_indexer.py`)

| Flag | Default | Description |
|------|---------|-------------|
| `--no-upload` | off | Write JSON only, skip Meilisearch |
| `--force` | off | Re-index already-done pages |

**Coordinate convention** (critical): OpenSeadragon normalises BOTH axes by the image WIDTH.
So `bbox = [x0/W, y0/W, x1/W, y1/W]` — *not* `y/H`. This is enforced in `normalize_bbox()`
and tested in `TestNormalizeBbox::test_y_coords_also_divided_by_WIDTH_not_height`.

### Module 4 — Frontend (`frontend/`)

React SPA. Left panel: Meilisearch instant-search with highlighted snippets and
confidence badges. Right panel: OpenSeadragon deep zoom viewer. Clicking a result
loads the page's DZI, draws a pulsing yellow overlay at the hit's `bbox`, and pans
to that region with 20% padding.

---

## Coordinate systems quick reference

| Context | x range | y range | Note |
|---------|---------|---------|------|
| ALTO XML pixel | `[0, page_width]` | `[0, page_height]` | `HPOS`, `VPOS` |
| `bbox_px` in index | `[0, page_width]` | `[0, page_height]` | Raw pixels |
| `bbox` in index | `[0, 1.0]` | `[0, H/W]` | Both axes ÷ **width** |
| OSD viewport | `[0, 1.0]` | `[0, H/W]` | Use `bbox` directly |

---

## Configuration (`config.yaml`)

```yaml
target_dpi: 300
ocr_languages: ["pol", "rus", "deu", "lat"]
ocr_engine: "tesseract"          # or "kraken"

preprocessing:
  deskew_enabled: true
  binarize_enabled: false        # turn on for yellowed/uneven pages

ocr:
  psm: 6                         # change to 3 for multi-column layouts

meilisearch:
  url: "http://127.0.0.1:7700"
  index_name: "genealogy_pages"
```

---

## Idempotency

Every module checks the output folder's `_manifest.jsonl` before processing.
Re-running any script skips pages that already have a manifest entry.
Use `--force` to override.
