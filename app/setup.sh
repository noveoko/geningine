#!/usr/bin/env bash
# setup.sh — run this once inside the folder containing all project files.
# Creates the directory structure and moves every file to its correct location.

set -euo pipefail

echo "=== Genealogy OCR Pipeline — project setup ==="
echo "Working directory: $(pwd)"
echo ""

# ── 1. Create directories ─────────────────────────────────────────────────────
echo "Creating directories..."
mkdir -p data/input_scans
mkdir -p data/output_cleaned
mkdir -p data/output_ocr
mkdir -p data/output_dzi
mkdir -p data/output_index
mkdir -p scripts
mkdir -p frontend/src/components
mkdir -p tests

# ── 2. Move Python scripts ────────────────────────────────────────────────────
echo "Moving scripts..."
for f in utils.py m1_preprocess.py m2_ocr.py m3_indexer.py m4_serve.sh; do
    [ -f "$f" ] && mv "$f" scripts/ && echo "  scripts/$f" || true
done
chmod +x scripts/m4_serve.sh 2>/dev/null || true

# ── 3. Move frontend root files ───────────────────────────────────────────────
echo "Moving frontend files..."
for f in index.html package.json vite.config.js; do
    [ -f "$f" ] && mv "$f" frontend/ && echo "  frontend/$f" || true
done

# ── 4. Move frontend/src files ────────────────────────────────────────────────
for f in App.jsx main.jsx index.css searchClient.js; do
    [ -f "$f" ] && mv "$f" frontend/src/ && echo "  frontend/src/$f" || true
done

# ── 5. Move React components ──────────────────────────────────────────────────
for f in OpenSeadragonViewer.jsx ResultItem.jsx ResultsList.jsx SearchBar.jsx; do
    [ -f "$f" ] && mv "$f" frontend/src/components/ && echo "  frontend/src/components/$f" || true
done

# ── 6. Move test files ────────────────────────────────────────────────────────
echo "Moving tests..."
for f in test_pipeline.py; do
    [ -f "$f" ] && mv "$f" tests/ && echo "  tests/$f" || true
done
touch tests/__init__.py

# ── 7. Verify nothing is missing ─────────────────────────────────────────────
echo ""
echo "Verifying structure..."
MISSING=0
check() {
    if [ -f "$1" ]; then
        echo "  [OK] $1"
    else
        echo "  [!!] MISSING: $1"
        MISSING=$((MISSING + 1))
    fi
}

check config.yaml
check requirements.txt
check pytest.ini
check README.md
check scripts/utils.py
check scripts/m1_preprocess.py
check scripts/m2_ocr.py
check scripts/m3_indexer.py
check scripts/m4_serve.sh
check frontend/index.html
check frontend/package.json
check frontend/vite.config.js
check frontend/src/App.jsx
check frontend/src/main.jsx
check frontend/src/index.css
check frontend/src/searchClient.js
check frontend/src/components/OpenSeadragonViewer.jsx
check frontend/src/components/ResultItem.jsx
check frontend/src/components/ResultsList.jsx
check frontend/src/components/SearchBar.jsx
check tests/test_pipeline.py

echo ""
if [ "$MISSING" -eq 0 ]; then
    echo "All files in place."
else
    echo "$MISSING file(s) missing — check the list above."
fi

# ── 8. Print next steps ───────────────────────────────────────────────────────
echo ""
echo "=== Next steps ==="
echo "  pip install -r requirements.txt"
echo "  cd frontend && npm install && cd .."
echo "  python -m pytest tests/ -v"
echo ""
echo "  # Then drop scans into data/input_scans/ and run:"
echo "  python scripts/m1_preprocess.py"
echo "  python scripts/m2_ocr.py"
echo "  python scripts/m3_indexer.py"
echo "  bash scripts/m4_serve.sh"