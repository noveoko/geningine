# Genealogy OCR Pipeline — Technical Specification

**Version:** 1.0  
**Author:** Systems Architect  
**Architecture:** Folder-as-a-Queue (each module is a standalone script)  
**Stack:** Python 3.11+, OpenCV, Tesseract 5 / Kraken, libvips, Meilisearch, React, OpenSeadragon

---

## Global Conventions

### Directory Layout

```
project_root/
├── data/
│   ├── input_scans/          ← Drop raw files here (Module 1 reads)
│   ├── output_cleaned/       ← Module 1 writes, Module 2 reads
│   ├── output_ocr/           ← Module 2 writes, Module 3 reads
│   ├── output_dzi/           ← Module 2 writes, Module 4 reads (served statically)
│   └── output_index/         ← Module 3 writes (loaded into Meilisearch)
├── scripts/
│   ├── m1_preprocess.py
│   ├── m2_ocr.py
│   ├── m3_indexer.py
│   └── m4_serve.sh           (or a small dev-server wrapper)
├── frontend/                 ← React app (Module 4)
└── config.yaml               ← Shared settings (DPI, languages, Meilisearch URL)
```

### Shared Config (`config.yaml`)

```yaml
target_dpi: 300
ocr_languages: ["pol", "rus", "deu", "lat"]   # Tesseract language codes
ocr_engine: "tesseract"                         # or "kraken"
meilisearch:
  url: "http://127.0.0.1:7700"
  api_key: ""                                   # empty for local dev
  index_name: "genealogy_pages"
dzi:
  tile_size: 254
  overlap: 1
  format: "png"
```

### File Naming Convention

Every file moving through the pipeline carries a deterministic **document ID** derived from its original filename:

```
doc_id = sha256(original_filename)[:12]
```

A multi-page PDF `parish_register_1897.pdf` with 40 pages produces:

```
output_cleaned/a3f8b1c2d4e5_p001.png
output_cleaned/a3f8b1c2d4e5_p002.png
...
output_cleaned/a3f8b1c2d4e5_p040.png
```

This ensures every downstream artifact (OCR XML, DZI folder, index JSON) can be traced back to the source file unambiguously.

### Manifest Pattern

Every output folder contains a `_manifest.jsonl` file — one JSON object per line, appended atomically. This is the queue signal: downstream modules scan this file to discover new work.

```jsonl
{"doc_id": "a3f8b1c2d4e5", "page": 1, "filename": "a3f8b1c2d4e5_p001.png", "status": "ready", "ts": "2026-03-16T10:30:00Z"}
```

---

## Module 1 — Pre-processor

**Purpose:** Convert raw scans (PDF, TIFF, JPEG, PNG) into normalized, cleaned, 300 DPI grayscale PNGs suitable for OCR.

### 1.1 Input / Output

| Direction | Folder | File Types | Notes |
|-----------|--------|------------|-------|
| **Input** | `data/input_scans/` | `.pdf`, `.tiff`, `.tif`, `.jpg`, `.jpeg`, `.png` | Drop files here; script watches or runs as batch |
| **Output** | `data/output_cleaned/` | `.png` (grayscale, 300 DPI) | One PNG per page. `_manifest.jsonl` appended per file. |

### 1.2 Processing Logic — Step by Step

**Step 1: Detect file type and extract pages.**

For PDFs, use `pdf2image` (which wraps Poppler's `pdftoppm`):

```bash
# Equivalent CLI (for reference):
pdftoppm -png -r 300 input.pdf output_prefix
```

```python
# Python:
from pdf2image import convert_from_path
pages = convert_from_path("input.pdf", dpi=300, fmt="png", grayscale=True)
```

For TIFF/JPEG/PNG, load directly with OpenCV. If the image has no embedded DPI metadata, assume 300 DPI. If DPI is below 300, upscale using Lanczos interpolation — this is critical because Tesseract's accuracy drops significantly below 300 DPI.

**Step 2: Convert to grayscale.**

```python
import cv2
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
```

**Step 3: Deskew.**

Deskew corrects rotated scans. The idea: detect lines of text (which should be horizontal), measure the angle they make with the true horizontal, and rotate the image to cancel that angle.

```python
# Find all coordinates of non-white pixels
coords = np.column_stack(np.where(gray < 128))
# Fit a minimum-area bounding rectangle around those points
angle = cv2.minAreaRect(coords)[-1]
# OpenCV returns angles in [-90, 0); adjust to get actual skew
if angle < -45:
    angle = -(90 + angle)
else:
    angle = -angle
# Only correct if skew is significant (> 0.5 degrees) but not extreme
if 0.5 < abs(angle) < 15.0:
    (h, w) = gray.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    gray = cv2.warpAffine(gray, M, (w, h),
                          flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)
```

The math behind `getRotationMatrix2D`: it produces a 2×3 affine matrix

$$
M = \begin{bmatrix} \cos\theta & \sin\theta & t_x \\ -\sin\theta & \cos\theta & t_y \end{bmatrix}
$$

where $\theta$ is the rotation angle and $(t_x, t_y)$ are translation offsets that keep the center of rotation at the image center. `warpAffine` applies this matrix to every pixel coordinate $(x, y)$ to compute its new position $(x', y') = M \cdot [x, y, 1]^T$.

**Step 4: Denoise.**

Use a non-local means denoising filter, which is effective for scanned documents because it preserves sharp text edges while removing scanner noise:

```python
denoised = cv2.fastNlMeansDenoising(gray, h=10, templateWindowSize=7, searchWindowSize=21)
```

The parameter `h` controls filter strength — 10 is a good default for 300 DPI scans. Higher values blur text.

**Step 5: Adaptive thresholding (optional, controlled by config).**

For documents with uneven lighting (e.g., book gutters, yellowed paper), adaptive thresholding converts the image to pure black-and-white by computing a local threshold for each pixel neighborhood:

```python
binary = cv2.adaptiveThreshold(
    denoised, 255,
    cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
    cv2.THRESH_BINARY,
    blockSize=31,  # size of neighborhood (must be odd)
    C=15           # constant subtracted from mean
)
```

The math: for each pixel at position $(x, y)$, the algorithm computes a threshold $T(x,y)$ as the Gaussian-weighted mean of pixel values in a $31 \times 31$ neighborhood minus constant $C = 15$. If $\text{pixel}(x,y) > T(x,y)$, output is white (255); otherwise black (0). This handles uneven illumination far better than a single global threshold.

**Step 6: Save as 300 DPI PNG.**

```bash
# ImageMagick (for setting DPI metadata reliably):
convert cleaned.png -density 300 -units PixelsPerInch output.png
```

```python
# Or with Pillow:
from PIL import Image
img_pil = Image.fromarray(result)
img_pil.save(output_path, dpi=(300, 300))
```

**Step 7: Append to manifest.**

### 1.3 Data Contract — Manifest Entry

Each processed page produces one line in `output_cleaned/_manifest.jsonl`:

```json
{
  "doc_id": "a3f8b1c2d4e5",
  "page": 1,
  "filename": "a3f8b1c2d4e5_p001.png",
  "source_file": "parish_register_1897.pdf",
  "width_px": 2480,
  "height_px": 3508,
  "dpi": 300,
  "deskew_angle_deg": 1.3,
  "binarized": false,
  "processing_time_s": 2.4,
  "ts": "2026-03-16T10:30:00Z"
}
```

**Required fields:** `doc_id`, `page`, `filename`, `width_px`, `height_px`, `dpi`.  
**Optional fields:** `deskew_angle_deg`, `binarized`, `processing_time_s`.

### 1.4 LLM Context Snippet

> **Module 1 (Pre-processor)** reads raw scans (PDF/TIFF/JPEG/PNG) from `data/input_scans/`, extracts pages with `pdf2image` at 300 DPI, applies OpenCV grayscale conversion + deskew (`minAreaRect` → `warpAffine`) + `fastNlMeansDenoising`, and saves each page as a grayscale 300 DPI PNG to `data/output_cleaned/{doc_id}_p{NNN}.png`. The `doc_id` is `sha256(original_filename)[:12]`. It appends one JSONL line per page to `output_cleaned/_manifest.jsonl` with fields: `doc_id`, `page`, `filename`, `source_file`, `width_px`, `height_px`, `dpi`, and `ts`.

---

## Module 2 — OCR Engine

**Purpose:** Run OCR on cleaned PNGs to produce word-level bounding box data (hOCR + ALTO XML), and simultaneously generate Deep Zoom Image (DZI) tiles for the frontend viewer.

### 2.1 Input / Output

| Direction | Folder | File Types | Notes |
|-----------|--------|------------|-------|
| **Input** | `data/output_cleaned/` | `.png` (300 DPI grayscale) | Reads `_manifest.jsonl` for new work |
| **Output 1** | `data/output_ocr/` | `.hocr` (XHTML), `.alto.xml` (ALTO v3) | One pair per page |
| **Output 2** | `data/output_dzi/` | `.dzi` + `_files/` tile directory | One DZI set per page |

### 2.2 Processing Logic — Step by Step

**Step 1: Run Tesseract to produce hOCR.**

hOCR is an XHTML-based format where every recognized word is wrapped in a `<span>` tag with a `title` attribute containing its bounding box (`bbox x0 y0 x1 y1`) and confidence (`x_wconf NN`).

```bash
# CLI:
tesseract input.png output -l pol+rus+deu+lat --dpi 300 hocr

# Produces: output.hocr
```

```python
# Python (pytesseract):
import pytesseract
hocr_bytes = pytesseract.image_to_pdf_or_hocr(
    "input.png",
    lang="pol+rus+deu+lat",
    extension="hocr",
    config="--dpi 300 --oem 1 --psm 6"
)
with open("output.hocr", "wb") as f:
    f.write(hocr_bytes)
```

Tesseract flags explained:
- `--oem 1`: Use the LSTM neural net engine (best accuracy).
- `--psm 6`: Assume a single uniform block of text. For multi-column layouts (common in old registers), use `--psm 3` (fully automatic) or `--psm 4` (single column of variable-size text).
- `-l pol+rus+deu+lat`: Load Polish, Russian, German, and Latin trained data models. Tesseract will try each and pick the best match per word.

**Step 1b (Alternative): Run Kraken for historical/handwritten documents.**

Kraken is better for non-standard scripts, historical fonts, and partially handwritten documents. It produces ALTO XML natively.

```bash
kraken -i input.png output.alto.xml segment -bl ocr -m historical_polish.mlmodel
```

If using Kraken, skip the hOCR-to-ALTO conversion in Step 2.

**Step 2: Convert hOCR to ALTO XML.**

If using Tesseract, convert hOCR to ALTO XML so Module 3 has a single canonical format. Use the `hocr-to-alto` Python library:

```bash
pip install hocr-to-alto
hocr2alto output.hocr -o output.alto.xml
```

Alternatively, keep both formats — Module 3 can parse either. But having ALTO as the canonical format simplifies the indexer.

**Step 3: Generate DZI tiles with libvips.**

Deep Zoom Images (DZI) are a tile pyramid. The original image is divided into a grid of small tiles (typically 254×254 px), and lower-resolution versions are generated at each zoom level. This lets the frontend load only the visible tiles at the current zoom, enabling smooth panning and zooming of very large scans.

The math: for an image of size $W \times H$, the number of zoom levels is:

$$
L = \lceil \log_2(\max(W, H)) \rceil + 1
$$

At level $L-1$ (the deepest), tiles are taken from the original resolution. At level $L-2$, the image is halved in each dimension, and so on. Level 0 is a single pixel. At each level $l$, the number of tiles horizontally is $\lceil W_l / T \rceil$ where $W_l = \lceil W / 2^{(L-1-l)} \rceil$ and $T$ is the tile size (254).

```bash
# CLI (fastest — highly recommended):
vips dzsave input.png output_dzi --tile-size 254 --overlap 1 --suffix .png
# Produces: output_dzi.dzi (XML descriptor) + output_dzi_files/ (tile folders)
```

```python
# Python:
import pyvips
image = pyvips.Image.new_from_file("input.png")
image.dzsave("output_dzi", tile_size=254, overlap=1, suffix=".png")
```

The output structure looks like:

```
output_dzi.dzi                  ← XML descriptor (tells OpenSeadragon the dimensions)
output_dzi_files/
├── 0/
│   └── 0_0.png                ← 1px thumbnail
├── 1/
│   └── 0_0.png
├── ...
└── 14/                        ← Full resolution tiles
    ├── 0_0.png
    ├── 0_1.png
    ├── 1_0.png
    └── ...
```

**Step 4: Append to manifests.**

Write to both `output_ocr/_manifest.jsonl` and `output_dzi/_manifest.jsonl`.

### 2.3 Data Contract

**hOCR structure (key fragment):**

```xml
<div class="ocr_page" title="bbox 0 0 2480 3508; image input.png; ppageno 0">
  <div class="ocr_carea" title="bbox 120 200 2360 3400">
    <p class="ocr_par" title="bbox 120 200 2360 280">
      <span class="ocr_line" title="bbox 120 200 2360 240">
        <span class="ocrx_word" title="bbox 120 200 280 240; x_wconf 92">Kowalski</span>
        <span class="ocrx_word" title="bbox 300 200 420 240; x_wconf 87">Jan</span>
      </span>
    </p>
  </div>
</div>
```

**ALTO XML structure (key fragment):**

```xml
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="p001" WIDTH="2480" HEIGHT="3508" PHYSICAL_IMG_NR="1">
      <PrintSpace>
        <TextBlock ID="tb_1">
          <TextLine ID="tl_1" HPOS="120" VPOS="200" WIDTH="2240" HEIGHT="40">
            <String ID="s_1" CONTENT="Kowalski" HPOS="120" VPOS="200"
                    WIDTH="160" HEIGHT="40" WC="0.92"/>
            <String ID="s_2" CONTENT="Jan" HPOS="300" VPOS="200"
                    WIDTH="120" HEIGHT="40" WC="0.87"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>
```

**ALTO bounding box convention:** `HPOS` = horizontal position from left edge (x0), `VPOS` = vertical position from top edge (y0), `WIDTH` and `HEIGHT` define the rectangle. So the bounding box in `[x0, y0, x1, y1]` format is `[HPOS, VPOS, HPOS+WIDTH, VPOS+HEIGHT]`.

**DZI descriptor (`output_dzi.dzi`):**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Image xmlns="http://schemas.microsoft.com/deepzoom/2008"
       Format="png" Overlap="1" TileSize="254">
  <Size Width="2480" Height="3508"/>
</Image>
```

**OCR manifest entry (`output_ocr/_manifest.jsonl`):**

```json
{
  "doc_id": "a3f8b1c2d4e5",
  "page": 1,
  "hocr_file": "a3f8b1c2d4e5_p001.hocr",
  "alto_file": "a3f8b1c2d4e5_p001.alto.xml",
  "ocr_engine": "tesseract",
  "languages": ["pol", "rus"],
  "mean_confidence": 84.3,
  "word_count": 312,
  "ts": "2026-03-16T10:35:00Z"
}
```

**DZI manifest entry (`output_dzi/_manifest.jsonl`):**

```json
{
  "doc_id": "a3f8b1c2d4e5",
  "page": 1,
  "dzi_file": "a3f8b1c2d4e5_p001.dzi",
  "tile_dir": "a3f8b1c2d4e5_p001_files/",
  "image_width": 2480,
  "image_height": 3508,
  "zoom_levels": 13,
  "ts": "2026-03-16T10:35:00Z"
}
```

### 2.4 LLM Context Snippet

> **Module 2 (OCR Engine)** reads cleaned 300 DPI PNGs from `data/output_cleaned/` and produces two outputs: (1) hOCR via `pytesseract` with `--oem 1 --psm 6 -l pol+rus+deu+lat`, converted to ALTO v3 XML with `hocr-to-alto`, written to `data/output_ocr/{doc_id}_p{NNN}.hocr` and `.alto.xml`; (2) Deep Zoom tiles via `pyvips.Image.dzsave()` with tile_size=254 and overlap=1, written to `data/output_dzi/{doc_id}_p{NNN}.dzi` and `{doc_id}_p{NNN}_files/`. Each output folder has its own `_manifest.jsonl`; the OCR manifest includes `mean_confidence` and `word_count`, the DZI manifest includes `image_width`, `image_height`, and `zoom_levels`.

---

## Module 3 — The Indexer

**Purpose:** Parse ALTO XML (or hOCR), extract every word with its bounding box, compute normalized coordinates, and produce Meilisearch-ready JSON documents.

### 3.1 Input / Output

| Direction | Folder | File Types | Notes |
|-----------|--------|------------|-------|
| **Input** | `data/output_ocr/` | `.alto.xml` (preferred) or `.hocr` | Reads `_manifest.jsonl` |
| **Output** | `data/output_index/` | `.json` (Meilisearch batch format) | One JSON file per page, plus a batch-upload script |

### 3.2 Processing Logic — Step by Step

**Step 1: Parse ALTO XML and extract word-level data.**

```python
from lxml import etree

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v3#"

tree = etree.parse("input.alto.xml")
page = tree.find(f".//{{{ALTO_NS}}}Page")
page_w = int(page.get("WIDTH"))    # e.g. 2480
page_h = int(page.get("HEIGHT"))   # e.g. 3508

words = []
for string_el in tree.iter(f"{{{ALTO_NS}}}String"):
    words.append({
        "content": string_el.get("CONTENT"),
        "hpos": int(string_el.get("HPOS")),
        "vpos": int(string_el.get("VPOS")),
        "width": int(string_el.get("WIDTH")),
        "height": int(string_el.get("HEIGHT")),
        "confidence": float(string_el.get("WC", "0.0")),
    })
```

**Step 2: Compute normalized bounding boxes.**

The frontend (OpenSeadragon) works in viewport coordinates where the image spans $[0, 1]$ horizontally and $[0, H/W]$ vertically. We normalize pixel bounding boxes to this coordinate space so the overlay math is resolution-independent.

Given a word at pixel coordinates $(x_0, y_0, x_1, y_1)$ on an image of size $W \times H$:

$$
x_{0,\text{norm}} = \frac{x_0}{W}, \quad y_{0,\text{norm}} = \frac{y_0}{W}, \quad x_{1,\text{norm}} = \frac{x_1}{W}, \quad y_{1,\text{norm}} = \frac{y_1}{W}
$$

Note: **both axes are divided by $W$ (the image width), not $H$**. This is because OpenSeadragon's viewport coordinate system uses the image width as the unit length (width = 1.0), and the height scales proportionally as $H/W$. This is a common source of bugs — if you normalize $y$ by $H$, your overlays will be squashed or stretched.

```python
def normalize_bbox(hpos, vpos, width, height, page_w):
    """Convert pixel bbox to OpenSeadragon viewport coordinates.
    
    The key insight: OpenSeadragon normalizes BOTH axes by the image width.
    So x ranges [0, 1] and y ranges [0, page_h/page_w].
    """
    x0 = hpos / page_w
    y0 = vpos / page_w          # ← divided by WIDTH, not HEIGHT
    x1 = (hpos + width) / page_w
    y1 = (vpos + height) / page_w
    return [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]
```

**Step 3: Assemble line-level index documents.**

Individual words are too granular for useful search. Instead, group words into **lines** and index each line as a document. The line's bounding box is the union of all its word bboxes:

$$
\text{line\_bbox} = \left[\min(x_{0,i}),\ \min(y_{0,i}),\ \max(x_{1,i}),\ \max(y_{1,i})\right]
$$

where $i$ ranges over all words in the line.

```python
# Group words by TextLine parent
for line_el in tree.iter(f"{{{ALTO_NS}}}TextLine"):
    line_words = line_el.findall(f"{{{ALTO_NS}}}String")
    text = " ".join(w.get("CONTENT") for w in line_words)
    
    bboxes = [normalize_bbox(
        int(w.get("HPOS")), int(w.get("VPOS")),
        int(w.get("WIDTH")), int(w.get("HEIGHT")),
        page_w
    ) for w in line_words]
    
    line_bbox = [
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    ]
    
    avg_confidence = sum(
        float(w.get("WC", "0")) for w in line_words
    ) / max(len(line_words), 1)
```

**Step 4: Write Meilisearch JSON.**

```python
import json

documents = []
for idx, line_data in enumerate(lines):
    documents.append({
        "id": f"{doc_id}_p{page:03d}_l{idx:04d}",
        "doc_id": doc_id,
        "page": page,
        "line_index": idx,
        "text": line_data["text"],
        "bbox": line_data["bbox"],       # [x0, y0, x1, y1] normalized
        "bbox_px": line_data["bbox_px"], # [x0, y0, x1, y1] original pixels
        "confidence": line_data["confidence"],
        "source_file": source_file,
    })

with open(f"output_index/{doc_id}_p{page:03d}.json", "w") as f:
    json.dump(documents, f, ensure_ascii=False)
```

**Step 5: Batch upload to Meilisearch.**

```python
import meilisearch

client = meilisearch.Client("http://127.0.0.1:7700")
index = client.index("genealogy_pages")

# Configure searchable and filterable attributes (run once)
index.update_settings({
    "searchableAttributes": ["text"],
    "filterableAttributes": ["doc_id", "page", "confidence"],
    "sortableAttributes": ["page", "line_index", "confidence"],
    "typoTolerance": {
        "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 7}
    }
})

# Upload in batches of 1000
for batch in chunked(documents, 1000):
    index.add_documents(batch)
```

### 3.3 Data Contract — Index Document Schema

Each line in the Meilisearch index follows this schema:

```json
{
  "id": "a3f8b1c2d4e5_p001_l0042",
  "doc_id": "a3f8b1c2d4e5",
  "page": 1,
  "line_index": 42,
  "text": "Kowalski Jan lat 35 wyznanie rzym-kat",
  "bbox": [0.048387, 0.057005, 0.951613, 0.068404],
  "bbox_px": [120, 200, 2360, 240],
  "confidence": 0.89,
  "source_file": "parish_register_1897.pdf"
}
```

**Field definitions:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | `string` | **Primary key.** Format: `{doc_id}_p{page}_l{line_index}`. |
| `doc_id` | `string` | Links back to the source document across all modules. |
| `page` | `int` | 1-indexed page number within the source document. |
| `line_index` | `int` | 0-indexed line number within the page. |
| `text` | `string` | **Searchable.** Full text of the OCR line. |
| `bbox` | `float[4]` | `[x0, y0, x1, y1]` in OpenSeadragon viewport coords (both axes / image width). |
| `bbox_px` | `int[4]` | `[x0, y0, x1, y1]` in original pixel coordinates. |
| `confidence` | `float` | Average OCR confidence for the line (0.0–1.0). |
| `source_file` | `string` | Original filename for display in the UI. |

### 3.4 LLM Context Snippet

> **Module 3 (Indexer)** reads ALTO v3 XML files from `data/output_ocr/`, parses them with `lxml`, and groups `<String>` elements by their parent `<TextLine>` to produce line-level search documents. Each document contains the concatenated line text, a pixel bounding box `bbox_px`, and a normalized bounding box `bbox` where **both x and y are divided by the page pixel width** (OpenSeadragon's viewport convention). It writes one JSON array per page to `data/output_index/{doc_id}_p{NNN}.json` and batch-uploads to Meilisearch (index name `genealogy_pages`) with `text` as the only searchable attribute and `doc_id`, `page`, `confidence` as filterable attributes.

---

## Module 4 — The Frontend

**Purpose:** A React single-page application that combines Meilisearch instant search with an OpenSeadragon deep zoom viewer, drawing bounding box overlays on search hits.

### 4.1 Input / Output

| Direction | Source | Format | Notes |
|-----------|--------|--------|-------|
| **Input (search)** | Meilisearch HTTP API | JSON (search results) | `http://127.0.0.1:7700` |
| **Input (tiles)** | Static file server | DZI tiles | Served from `data/output_dzi/` |
| **Output** | Browser | Interactive UI | No server-side rendering required |

### 4.2 Architecture — Component Tree

```
<App>
├── <SearchBar />              ← Meilisearch instant-search input
├── <ResultsList />            ← Paginated search hits with snippets
│   └── <ResultItem />         ← Single hit (text, page, confidence badge)
└── <ViewerPanel />
    ├── <OpenSeadragonViewer /> ← Deep zoom canvas
    └── <BoundingBoxOverlay /> ← SVG overlay synced to viewport
```

### 4.3 Processing Logic — Key Integration Points

**Point 1: Meilisearch client setup.**

```bash
npm install meilisearch
```

```javascript
import { MeiliSearch } from "meilisearch";

const client = new MeiliSearch({ host: "http://127.0.0.1:7700" });
const index = client.index("genealogy_pages");

// Search with highlighting:
const results = await index.search("Kowalski", {
  attributesToHighlight: ["text"],
  highlightPreTag: "<mark>",
  highlightPostTag: "</mark>",
  limit: 20,
  filter: ["confidence > 0.5"],
});
// results.hits[0] → { id, doc_id, page, text, bbox, bbox_px, ... }
```

**Point 2: OpenSeadragon viewer initialization.**

```bash
npm install openseadragon
```

```javascript
import OpenSeadragon from "openseadragon";

const viewer = OpenSeadragon({
  id: "osd-viewer",
  prefixUrl: "/openseadragon/images/",  // nav button icons
  tileSources: `/dzi/${docId}_p${page}.dzi`,
  showNavigator: true,
  navigatorPosition: "BOTTOM_RIGHT",
  minZoomLevel: 0.5,
  maxZoomLevel: 10,
  gestureSettingsMouse: { clickToZoom: false },
});
```

When the user clicks a search result, swap the tile source:

```javascript
function navigateToResult(hit) {
  const dziUrl = `/dzi/${hit.doc_id}_p${String(hit.page).padStart(3, "0")}.dzi`;
  viewer.open(dziUrl);
  // After the image loads, draw the overlay and pan to it:
  viewer.addOnceHandler("open", () => {
    drawHighlight(hit.bbox);
    panToRegion(hit.bbox);
  });
}
```

**Point 3: Bounding box overlay — the critical integration.**

OpenSeadragon supports SVG overlays that stay in sync with pan/zoom. The `bbox` array from Meilisearch is already in viewport coordinates (both axes divided by image width), so the overlay math is:

```javascript
function drawHighlight(bbox) {
  // bbox = [x0, y0, x1, y1] in viewport coordinates
  const [x0, y0, x1, y1] = bbox;

  // Create an OpenSeadragon.Rect in viewport coordinates
  const rect = new OpenSeadragon.Rect(x0, y0, x1 - x0, y1 - y0);

  // Create a DOM element for the overlay
  const highlightEl = document.createElement("div");
  highlightEl.className = "search-highlight";
  // Style: semi-transparent yellow box with a border
  highlightEl.style.cssText = `
    background: rgba(255, 220, 0, 0.25);
    border: 2px solid rgba(255, 180, 0, 0.8);
    pointer-events: none;
  `;

  // Add overlay — OpenSeadragon handles all zoom/pan transforms
  viewer.addOverlay({
    element: highlightEl,
    location: rect,
  });
}

function panToRegion(bbox) {
  const [x0, y0, x1, y1] = bbox;
  // Add 20% padding around the hit for context
  const padX = (x1 - x0) * 0.2;
  const padY = (y1 - y0) * 0.2;
  const region = new OpenSeadragon.Rect(
    x0 - padX,
    y0 - padY,
    (x1 - x0) + 2 * padX,
    (y1 - y0) + 2 * padY
  );
  viewer.viewport.fitBounds(region);
}

function clearHighlights() {
  viewer.clearOverlays();
}
```

**Point 4: Static file serving.**

For local development, serve the DZI tiles with any static file server:

```bash
# Python's built-in server:
cd data/output_dzi && python -m http.server 8001

# Or with the React dev server's proxy (vite.config.js):
export default {
  server: {
    proxy: {
      "/dzi": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/dzi/, ""),
      },
    },
  },
};
```

For production, point Nginx or Caddy at the `output_dzi/` folder.

### 4.4 Data Contract — Search API Request/Response

**Request (from frontend to Meilisearch):**

```json
{
  "q": "Kowalski Jan",
  "attributesToHighlight": ["text"],
  "highlightPreTag": "<mark>",
  "highlightPostTag": "</mark>",
  "limit": 20,
  "offset": 0,
  "filter": ["confidence > 0.5"]
}
```

**Response (Meilisearch → frontend):**

```json
{
  "hits": [
    {
      "id": "a3f8b1c2d4e5_p001_l0042",
      "doc_id": "a3f8b1c2d4e5",
      "page": 1,
      "line_index": 42,
      "text": "Kowalski Jan lat 35 wyznanie rzym-kat",
      "bbox": [0.048387, 0.057005, 0.951613, 0.068404],
      "bbox_px": [120, 200, 2360, 240],
      "confidence": 0.89,
      "source_file": "parish_register_1897.pdf",
      "_formatted": {
        "text": "<mark>Kowalski</mark> <mark>Jan</mark> lat 35 wyznanie rzym-kat"
      }
    }
  ],
  "estimatedTotalHits": 7,
  "processingTimeMs": 2,
  "query": "Kowalski Jan"
}
```

The frontend uses `hit.bbox` directly for overlay rendering (already in viewport coordinates) and `hit._formatted.text` for displaying highlighted snippets in the results list.

### 4.5 LLM Context Snippet

> **Module 4 (Frontend)** is a React SPA with two panels: a Meilisearch-powered search bar + results list (using the `meilisearch` JS client, index `genealogy_pages`), and an OpenSeadragon deep zoom viewer loading `.dzi` files from a static server at `/dzi/{doc_id}_p{NNN}.dzi`. When a user clicks a search result, the viewer opens the corresponding page's DZI, creates a `<div>` overlay with `viewer.addOverlay()` positioned by the hit's `bbox` field (which is already in OpenSeadragon viewport coordinates — both axes normalized by image width), and pans to the region with `viewer.viewport.fitBounds()` with 20% padding.

---

## Appendix A — Dependency Checklist

```bash
# System packages (Ubuntu/Debian)
sudo apt install -y \
  tesseract-ocr tesseract-ocr-pol tesseract-ocr-rus tesseract-ocr-deu \
  libvips-tools \
  poppler-utils \
  imagemagick

# Python packages
pip install \
  opencv-python-headless \
  pdf2image \
  pytesseract \
  Pillow \
  lxml \
  pyvips \
  meilisearch \
  pyyaml \
  hocr-to-alto

# Meilisearch (local binary — no Docker needed)
curl -L https://install.meilisearch.com | sh
./meilisearch --master-key="" --http-addr="127.0.0.1:7700"

# Node.js (frontend)
cd frontend && npm install meilisearch openseadragon react react-dom
```

**Estimated cost:** $0. All tools are open-source. The only potential cost is Kraken model training data or commercial Tesseract language packs, which are not needed for the base pipeline.

## Appendix B — Processing Order Cheatsheet

```
1. Drop files into  data/input_scans/
2. Run:  python scripts/m1_preprocess.py
3. Run:  python scripts/m2_ocr.py
4. Run:  python scripts/m3_indexer.py
5. Start Meilisearch:  ./meilisearch
6. Start tile server:  cd data/output_dzi && python -m http.server 8001
7. Start frontend:  cd frontend && npm run dev
```

Each step is idempotent — re-running a module skips files already present in its output manifest.

## Appendix C — Bounding Box Coordinate Systems (Quick Reference)

| Context | Coordinate System | x range | y range | Example |
|---------|-------------------|---------|---------|---------|
| ALTO XML / hOCR | Pixel (top-left origin) | `[0, page_width]` | `[0, page_height]` | `(120, 200, 280, 240)` |
| Meilisearch `bbox_px` | Pixel (top-left origin) | `[0, page_width]` | `[0, page_height]` | `[120, 200, 280, 240]` |
| Meilisearch `bbox` | OSD viewport (both axes / width) | `[0, 1.0]` | `[0, H/W]` | `[0.0484, 0.0570, 0.1129, 0.0684]` |
| OpenSeadragon overlay | OSD viewport | `[0, 1.0]` | `[0, H/W]` | Same as `bbox` — use directly |