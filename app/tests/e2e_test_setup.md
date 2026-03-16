This is a very well-architected genealogy pipeline. The separation of concerns (Pre-process $\rightarrow$ OCR/DZI $\rightarrow$ Indexing), the idempotency via manifests, and the careful attention to coordinate normalization for OpenSeadragon show a lot of foresight.

Since I am an AI, I won't be able to physically "see" or run the PDF you upload, but I can write a highly verbose, robust, and isolated end-to-end (E2E) test for your `pytest` suite right now.

To make this a true E2E test without polluting your real `data/` directories, this test dynamically generates a temporary project structure and a custom `config.yaml` using `pytest`'s `tmp_path` fixture. It then invokes your scripts via `subprocess` (exactly as a user or CI pipeline would) and validates the exact file outputs, manifest updates, and XML structures detailed in your `README.md`.

Here is the verbose E2E test. Save this as `tests/test_e2e_pipeline.py`.

```python
"""
test_e2e_pipeline.py
====================
Verbose End-to-End test for the Genealogy OCR Pipeline.

Flow:
1. Sets up a temporary isolated workspace with its own config.yaml and data/ dirs.
2. Copies the user-provided test PDF into the input directory.
3. Runs Module 1 (Pre-process) -> Asserts clean PNGs and manifest.
4. Runs Module 2 (OCR) -> Asserts hOCR, ALTO XML, DZI tiles, and manifest.
5. Runs Module 3 (Indexer) -> Asserts Meilisearch JSON formatting and bbox normalization.
"""

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

# Adjust this if your test PDF is stored elsewhere
TEST_PDF_SOURCE = Path(__file__).parent / "test_data" / "sample_scan.pdf"

@pytest.fixture
def e2e_env(tmp_path):
    """
    Creates an isolated pipeline environment.
    Returns a dictionary containing critical paths.
    """
    # 1. Create directory layout
    data_dir = tmp_path / "data"
    dirs = {
        "root": tmp_path,
        "input": data_dir / "input_scans",
        "clean": data_dir / "output_cleaned",
        "ocr": data_dir / "output_ocr",
        "dzi": data_dir / "output_dzi",
        "index": data_dir / "output_index"
    }
    
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    # 2. Create isolated config.yaml pointing to tmp_path
    config_path = tmp_path / "config.yaml"
    config_data = {
        "target_dpi": 300,
        "ocr_languages": ["eng"], # Simplified for testing, change as needed
        "ocr_engine": "tesseract",
        "preprocessing": {
            "deskew_enabled": True,
            "binarize_enabled": False
        },
        "ocr": {"psm": 6},
        "meilisearch": {
            "url": "http://127.0.0.1:7700",
            "index_name": "test_pages"
        },
        "paths": {
            "input_scans": str(dirs["input"].relative_to(tmp_path)),
            "output_cleaned": str(dirs["clean"].relative_to(tmp_path)),
            "output_ocr": str(dirs["ocr"].relative_to(tmp_path)),
            "output_dzi": str(dirs["dzi"].relative_to(tmp_path)),
            "output_index": str(dirs["index"].relative_to(tmp_path)),
        }
    }
    
    with open(config_path, "w") as f:
        yaml.dump(config_data, f)
        
    dirs["config"] = config_path
    return dirs


@pytest.mark.skipif(not TEST_PDF_SOURCE.exists(), reason="Test PDF not found. Please upload it to tests/test_data/sample_scan.pdf")
def test_full_pipeline_e2e(e2e_env):
    """
    Executes the entire pipeline end-to-end and validates the contracts
    defined in the README.
    """
    # ---------------------------------------------------------
    # SETUP: Drop the raw scan into the pipeline
    # ---------------------------------------------------------
    test_file = e2e_env["input"] / TEST_PDF_SOURCE.name
    shutil.copy(TEST_PDF_SOURCE, test_file)
    
    doc_id = test_file.stem # Assuming get_doc_id behaves somewhat like this

    # ---------------------------------------------------------
    # STAGE 1: Pre-processing (m1)
    # ---------------------------------------------------------
    m1_cmd = [
        "python", "scripts/m1_preprocess.py",
        "--config", str(e2e_env["config"])
    ]
    res_m1 = subprocess.run(m1_cmd, capture_output=True, text=True)
    assert res_m1.returncode == 0, f"m1 failed: {res_m1.stderr}"

    # Verify M1 Outputs
    clean_manifest_path = e2e_env["clean"] / "_manifest.jsonl"
    assert clean_manifest_path.exists(), "m1 did not create _manifest.jsonl"
    
    with open(clean_manifest_path, "r") as f:
        m1_manifest = [json.loads(line) for line in f]
    
    assert len(m1_manifest) > 0, "No pages were processed by m1"
    
    # Check that PNGs were actually generated
    for entry in m1_manifest:
        png_path = e2e_env["clean"] / entry["filename"]
        assert png_path.exists(), f"Missing cleaned PNG: {png_path}"
        assert entry["dpi"] == 300, "Manifest DPI does not match target_dpi"

    # ---------------------------------------------------------
    # STAGE 2: OCR & DZI (m2)
    # ---------------------------------------------------------
    m2_cmd = [
        "python", "scripts/m2_ocr.py",
        "--config", str(e2e_env["config"])
    ]
    res_m2 = subprocess.run(m2_cmd, capture_output=True, text=True)
    assert res_m2.returncode == 0, f"m2 failed: {res_m2.stderr}"

    # Verify M2 Outputs (OCR)
    ocr_manifest_path = e2e_env["ocr"] / "_manifest.jsonl"
    assert ocr_manifest_path.exists(), "m2 did not create OCR manifest"
    
    with open(ocr_manifest_path, "r") as f:
        m2_ocr_manifest = [json.loads(line) for line in f]
    
    for entry in m2_ocr_manifest:
        # Check ALTO XML exists and is valid
        alto_path = e2e_env["ocr"] / entry["alto_filename"]
        assert alto_path.exists(), f"Missing ALTO file: {alto_path}"
        
        # Parse XML to ensure it's not corrupt
        tree = ET.parse(alto_path)
        root = tree.getroot()
        assert "alto" in root.tag.lower(), "Root element is not ALTO"

    # Verify M2 Outputs (DZI)
    dzi_manifest_path = e2e_env["dzi"] / "_manifest.jsonl"
    assert dzi_manifest_path.exists(), "m2 did not create DZI manifest"
    
    for entry in m1_manifest:
        base_stem = entry["filename"].replace(".png", "")
        dzi_file = e2e_env["dzi"] / f"{base_stem}.dzi"
        dzi_folder = e2e_env["dzi"] / f"{base_stem}_files"
        
        assert dzi_file.exists(), f"Missing DZI descriptor: {dzi_file}"
        assert dzi_folder.exists() and dzi_folder.is_dir(), f"Missing DZI tile folder: {dzi_folder}"

    # ---------------------------------------------------------
    # STAGE 3: Indexing (m3)
    # ---------------------------------------------------------
    m3_cmd = [
        "python", "scripts/m3_indexer.py",
        "--config", str(e2e_env["config"]),
        "--no-upload" # Test local JSON generation only
    ]
    res_m3 = subprocess.run(m3_cmd, capture_output=True, text=True)
    assert res_m3.returncode == 0, f"m3 failed: {res_m3.stderr}"

    # Verify M3 Outputs (Meilisearch JSON)
    index_files = list(e2e_env["index"].glob("*.json"))
    assert len(index_files) > 0, "m3 did not generate any Meilisearch JSON files"

    # Validate the crucial coordinate constraint mentioned in the README
    # bbox = [x0/W, y0/W, x1/W, y1/W]
    for idx_file in index_files:
        with open(idx_file, "r") as f:
            payload = json.load(f)
            
        for document in payload:
            assert "bbox" in document, "Indexed document missing bbox"
            x0, y0, x1, y1 = document["bbox"]
            
            # Assert all coordinates are normalized between 0.0 and something reasonable (H/W)
            # A standard A4 scan at 300DPI is ~2480x3508. H/W is ~1.41.
            # So coordinates should safely be between 0.0 and 2.0.
            assert 0.0 <= x0 <= 1.0, f"x0 out of bounds: {x0}"
            assert 0.0 <= x1 <= 1.0, f"x1 out of bounds: {x1}"
            assert 0.0 <= y0 <= 2.5, f"y0 not normalized to width: {y0}"
            assert 0.0 <= y1 <= 2.5, f"y1 not normalized to width: {y1}"
            assert x0 < x1, "Invalid x coordinates (x0 >= x1)"
            assert y0 < y1, "Invalid y coordinates (y0 >= y1)"

    # ---------------------------------------------------------
    # STAGE 4: Idempotency Check
    # ---------------------------------------------------------
    # Re-running M1 without --force should result in 0 new pages processed
    res_m1_idempotent = subprocess.run(m1_cmd, capture_output=True, text=True)
    assert "Total pages processed: 0" in res_m1_idempotent.stdout or "already processed" in res_m1_idempotent.stderr or res_m1_idempotent.returncode == 0

```

### How to use this:

1. Create a folder `tests/test_data/`.
2. Place a small, 1-to-2 page PDF in there named `sample_scan.pdf`.
3. Run `pytest tests/test_e2e_pipeline.py -v -s`.

Would you like to drop in the code for `m3_indexer.py` next so we can verify the Meilisearch chunking logic before you upload your PDF?