#!/usr/bin/env python3
"""
m0_harvest.py — Module 0: Archive PDF & Scan Download Pipeline
===============================================================

Reads  data/config/harvest_queue.json
Writes downloaded files to  data/input_scans/          (Module 1's input folder)
Appends one JSONL line per download to  data/input_scans/_manifest.jsonl

Source systems supported
------------------------
  archive_org   internetarchive Python library
  polona        JSON metadata API + IIIF image fallback
  dlibra        BeautifulSoup HTML scrape + optional DjVu → PDF conversion
  szwa          Szukaj w Archiwach REST JSON API

All HTTP calls use tenacity exponential backoff (parameters from config.yaml).
Files are written to .tmp then atomically renamed — Module 1 never sees a
partial download.

Usage
-----
  python scripts/m0_harvest.py                      # process new targets only
  python scripts/m0_harvest.py --source polona      # one source only
  python scripts/m0_harvest.py --force              # re-download everything
  python scripts/m0_harvest.py --dry-run            # preview without writing
  python scripts/m0_harvest.py --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ---------------------------------------------------------------------------
# Path bootstrap — identical pattern to m1_preprocess.py
# ---------------------------------------------------------------------------
# __file__ = app/scripts/m0_harvest.py
# parent   = app/scripts/
# sys.path gets app/scripts/ so `from utils import ...` resolves correctly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import (       # noqa: E402
    append_manifest,
    get_doc_id,
    load_config,
    processed_set,
    setup_logging,
)

log = setup_logging("m0_harvest")


# =============================================================================
# Retry-aware HTTP GET
# =============================================================================

def _make_retry_get(cfg: dict):
    """
    Build a requests.get wrapper with tenacity retry/backoff drawn from
    config.yaml so retry behaviour is configurable without touching code.

    Exponential backoff formula after the k-th failure:

        wait = min(retry_wait_max_s,  retry_wait_min_s × 2^(k-1))

    Example with min=4, max=60:
        failure 1 → wait  4 s
        failure 2 → wait  8 s
        failure 3 → wait 16 s
        failure 4 → wait 32 s
        failure 5 → wait 60 s  (capped)
    """
    h = cfg.get("harvest", {})

    @retry(
        retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
        wait=wait_exponential(
            multiplier=1,
            min=h.get("retry_wait_min_s", 4),
            max=h.get("retry_wait_max_s", 60),
        ),
        stop=stop_after_attempt(h.get("max_retries", 5)),
        reraise=True,
    )
    def _get(url: str, **kwargs) -> requests.Response:
        return requests.get(url, timeout=30, **kwargs)

    return _get


# =============================================================================
# File helpers
# =============================================================================

def _stream_to_tmp(response: requests.Response, final_path: Path) -> None:
    """
    Write a streaming HTTP response to disk using a .tmp guard.

    We write to <name>.pdf.tmp first, then atomically rename to <name>.pdf
    only when the download is 100% complete.  This prevents Module 1 from
    picking up a partial file if it is watching the folder.

    The rename is atomic on POSIX (single syscall) and safe on Windows NTFS
    (MoveFile replaces atomically when src/dst are on the same volume).

    On failure the .tmp file is cleaned up so it never litters the folder.
    """
    tmp = final_path.with_suffix(final_path.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            for chunk in response.iter_content(chunk_size=8192):
                fh.write(chunk)
        tmp.rename(final_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _md5(path: Path) -> str:
    """
    MD5 hex digest of a file, read in 64 KB blocks.

    Used as an integrity / dedup signal in the manifest — not for security,
    so MD5's cryptographic weaknesses don't matter here.
    """
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _fetch_direct(
    url: str,
    slug: str,
    out_dir: Path,
    _get: Any,
    ext: str = ".pdf",
    filename: str | None = None,
) -> tuple[Path, str]:
    """Download *url* to out_dir via .tmp guard. Returns (final_path, url)."""
    if filename is None:
        filename = f"{slug}{ext}"
    final_path = out_dir / filename
    r = _get(url, stream=True)
    r.raise_for_status()
    _stream_to_tmp(r, final_path)
    return final_path, url


# =============================================================================
# DjVu → PDF conversion
# =============================================================================

def _djvu_to_pdf(djvu_path: Path) -> Path:
    """
    Convert DjVu → PDF with the djvulibre command-line tool ``ddjvu``.

    System dependency (not pip-installable):
        Ubuntu/Debian:  sudo apt install djvulibre-bin
        Arch:           sudo pacman -S djvulibre

    The original .djvu is removed after successful conversion to save space.
    Returns the Path of the resulting .pdf.
    """
    pdf_path = djvu_path.with_suffix(".pdf")
    log.info("Converting DjVu → PDF: %s", djvu_path.name)
    try:
        subprocess.run(
            ["ddjvu", "-format=pdf", str(djvu_path), str(pdf_path)],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ddjvu not found — install djvulibre-bin: sudo apt install djvulibre-bin"
        )
    djvu_path.unlink()
    log.info("Converted → %s", pdf_path.name)
    return pdf_path


# =============================================================================
# IIIF image-sequence downloader  (Polona fallback)
# =============================================================================

def _download_iiif_sequence(
    manifest_url: str,
    item_id: str,
    out_dir: Path,
    cfg: dict,
    _get: Any,
) -> list[Path]:
    """
    Download every page of an IIIF Presentation Manifest as individual JPEGs.

    IIIF Presentation Manifest structure (v2, simplified):

        manifest
          └─ sequences[0]
               └─ canvases[i]
                    └─ images[0]
                         └─ resource["@id"]   ← image URL

    We rewrite each URL to request the full-resolution default JPEG:
        {base_url}/{identifier}/full/full/0/default.jpg

    Files are named:  polona_{item_id}_{page:03d}.jpg
    """
    delay = cfg.get("harvest", {}).get("rate_limit_delay_s", 2.0)
    r = _get(manifest_url)
    r.raise_for_status()
    iiif = r.json()

    canvases = iiif.get("sequences", [{}])[0].get("canvases", [])
    if not canvases:
        raise ValueError(f"No canvases in IIIF manifest: {manifest_url}")

    paths: list[Path] = []
    for idx, canvas in enumerate(canvases, start=1):
        resource = (canvas.get("images") or [{}])[0].get("resource", {})
        img_url  = resource.get("@id", "")
        if not img_url:
            log.warning("Canvas %d: no image URL — skipping", idx)
            continue

        # Normalise to full-resolution JPEG regardless of what the server sent
        base     = img_url.split("/full/")[0]
        full_url = f"{base}/full/full/0/default.jpg"

        filename   = f"polona_{item_id}_{idx:03d}.jpg"
        final_path = out_dir / filename
        r_img = _get(full_url, stream=True)
        r_img.raise_for_status()
        _stream_to_tmp(r_img, final_path)
        paths.append(final_path)
        log.info("  IIIF page %d/%d → %s", idx, len(canvases), filename)
        time.sleep(delay)

    return paths


# =============================================================================
# Source fetchers
# =============================================================================

# ---------------------------------------------------------------------------
# Archive.org
# ---------------------------------------------------------------------------

def fetch_archive_org(
    target: dict,
    cfg: dict,
    out_dir: Path,
    _get: Any,          # unused — ia library handles HTTP, included for uniform signature
    dry_run: bool = False,
) -> tuple[str, str, int | None]:
    """
    Fetch one item from Archive.org via the ``internetarchive`` Python library.

    The function tries each format listed under
    config.harvest.preferred_format.archive_org in priority order, stopping
    at the first format it finds in the remote item.

    ``internetarchive`` is an optional heavy dependency so it is imported
    inside the fetcher — the rest of the pipeline works without it installed.
    """
    item_id    = target["id"]
    filename   = f"archive_org_{item_id}.pdf"
    final_path = out_dir / filename
    h_cfg      = cfg.get("harvest", {})
    formats    = h_cfg.get("preferred_format", {}).get(
        "archive_org", ["Text PDF", "Single Page Processed JP2 ZIP"]
    )

    if dry_run:
        log.info("[DRY-RUN] archive_org  id=%s  →  %s", item_id, filename)
        return filename, "", None

    try:
        import internetarchive as ia
    except ImportError:
        raise ImportError("Run: pip install internetarchive")

    item  = ia.get_item(item_id)
    title = item.metadata.get("title", "")

    for fmt in formats:
        matching = [f for f in item.files if f.get("format") == fmt]
        if not matching:
            continue
        chosen = matching[0]
        log.info("archive_org  id=%s  format=%s  remote_file=%s", item_id, fmt, chosen["name"])

        ia.download(item_id, files=[chosen["name"]], destdir=str(out_dir), no_directory=True)

        # ia.download preserves the original filename; rename to our canonical slug
        downloaded = out_dir / chosen["name"]
        if downloaded.resolve() != final_path.resolve():
            downloaded.rename(final_path)

        return filename, title, None

    raise RuntimeError(
        f"archive_org '{item_id}': none of the preferred formats found: {formats}"
    )


# ---------------------------------------------------------------------------
# Polona.pl
# ---------------------------------------------------------------------------

POLONA_API = "https://api.polona.pl/api/entities/{id}"
POLONA_PDF = "https://polona.pl/api/entities/{id}/download/"


def fetch_polona(
    target: dict,
    cfg: dict,
    out_dir: Path,
    _get: Any,
    dry_run: bool = False,
) -> tuple[str, str, int | None]:
    """
    Fetch one item from Polona.pl (Polish National Library digital portal).

    Strategy
    --------
    1. Call the JSON metadata API for title and IIIF manifest URL.
    2. Try the bundled PDF endpoint (preferred — one file, easier for OCR).
    3. If the PDF endpoint returns non-200, fall back to IIIF page-by-page.

    Why prefer the PDF?
    -------------------
    A single PDF is faster, smaller, and much easier for the OCR module to
    handle than a directory of hundreds of individual JPEGs.
    """
    item_id    = target["id"]
    filename   = f"polona_{item_id}.pdf"
    final_path = out_dir / filename

    if dry_run:
        log.info("[DRY-RUN] polona  id=%s  →  %s", item_id, filename)
        return filename, "", None

    meta = _get(POLONA_API.format(id=item_id))
    meta.raise_for_status()
    meta  = meta.json()
    title = meta.get("title", "")

    r = _get(POLONA_PDF.format(id=item_id), stream=True, allow_redirects=True)
    if r.status_code == 200:
        _stream_to_tmp(r, final_path)
        log.info("polona  ✓  %s", filename)
        return filename, title, None

    iiif = meta.get("iiif_manifest_url") or meta.get("iiif_manifest")
    if iiif:
        log.info("polona  PDF → HTTP %d; falling back to IIIF", r.status_code)
        pages = _download_iiif_sequence(iiif, item_id, out_dir, cfg, _get)
        return (pages[0].name if pages else filename), title, len(pages)

    raise RuntimeError(
        f"polona '{item_id}': PDF returned HTTP {r.status_code} "
        "and no IIIF manifest in metadata"
    )


# ---------------------------------------------------------------------------
# dLibra / FBC (Polish Digital Library consortium)
# ---------------------------------------------------------------------------

def fetch_dlibra(
    target: dict,
    cfg: dict,
    out_dir: Path,
    _get: Any,
    dry_run: bool = False,
) -> tuple[str, str, int | None]:
    """
    Fetch one item from a dLibra-powered digital library (e.g. WBC Poznań).

    dLibra's download URL is not part of the OAI-PMH metadata — it only
    appears in the rendered HTML.  We fetch the publication page and scan
    all <a href> tags for a direct PDF or DjVu link with BeautifulSoup.

    DjVu files are converted to PDF with djvulibre if harvest.djvu_convert
    is True in config.yaml.
    """
    page_url = target.get("url", "")
    if not page_url:
        raise ValueError("dlibra target must include a 'url' field")

    # Slug from the numeric trailing segment of the URL, e.g.
    # https://www.wbc.poznan.pl/publication/12345  →  dlibra_12345
    url_id = page_url.rstrip("/").split("/")[-1]
    slug   = f"dlibra_{url_id}"

    if dry_run:
        log.info("[DRY-RUN] dlibra  url=%s  →  %s.pdf", page_url, slug)
        return f"{slug}.pdf", "", None

    r = _get(page_url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    for link in soup.find_all("a", href=True):
        href: str = link["href"]
        # Resolve relative URLs against the page's origin
        if href.startswith("/"):
            parsed = urlparse(page_url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"

        if href.lower().endswith(".pdf"):
            log.info("dlibra  ✓  PDF: %s", href)
            final_path, _ = _fetch_direct(href, slug, out_dir, _get)
            return final_path.name, "", None

        if href.lower().endswith(".djvu"):
            log.info("dlibra  DjVu: %s", href)
            djvu_path, _ = _fetch_direct(href, slug, out_dir, _get, ext=".djvu")
            if cfg.get("harvest", {}).get("djvu_convert", False):
                pdf_path = _djvu_to_pdf(djvu_path)
                return pdf_path.name, "", None
            return djvu_path.name, "", None

    raise RuntimeError(
        f"dlibra page '{page_url}': no direct PDF or DjVu download link found in HTML"
    )


# ---------------------------------------------------------------------------
# Szukaj w Archiwach (SzWA) — Polish state archives portal
# ---------------------------------------------------------------------------

SZWA_API_BASE = "https://www.szukajwarchiwach.gov.pl/api/"


def fetch_szwa(
    target: dict,
    cfg: dict,
    out_dir: Path,
    _get: Any,
    dry_run: bool = False,
) -> tuple[str, str, int | None]:
    """
    Fetch a scan from Szukaj w Archiwach (Search in Archives).

    The SzWA REST API is queried with ``zespol`` (fond/series number) and
    ``jednostka`` (archival unit number) from the queue entry.  The first
    result that carries a ``skan_url`` field is downloaded.
    """
    h_cfg     = cfg.get("harvest", {})
    zespol    = str(target.get("zespol", ""))
    jednostka = str(target.get("jednostka", ""))
    slug      = f"szwa_{zespol}_{jednostka}"

    if dry_run:
        log.info("[DRY-RUN] szwa  zespol=%s  jednostka=%s  →  %s.pdf", zespol, jednostka, slug)
        return f"{slug}.pdf", "", None

    params: dict[str, str] = {"zespol": zespol, "jednostka": jednostka}
    api_key = h_cfg.get("szwa_api_key", "")
    if api_key:
        params["key"] = api_key

    r = _get(f"{SZWA_API_BASE}jednostki", params=params)
    r.raise_for_status()
    results = r.json().get("results", [])

    if not results:
        raise RuntimeError(f"szwa: no results for zespol={zespol}, jednostka={jednostka}")

    for item in results:
        scan_url = item.get("skan_url")
        if not scan_url:
            continue
        filename = f"{slug}.pdf"
        final_path, _ = _fetch_direct(scan_url, slug, out_dir, _get, filename=filename)
        return final_path.name, item.get("tytul", ""), None

    raise RuntimeError(
        f"szwa: results found for zespol={zespol}, jednostka={jednostka} "
        "but none had a 'skan_url' field"
    )


# =============================================================================
# Fetcher dispatch table
# =============================================================================

FETCHERS: dict[str, Any] = {
    "archive_org": fetch_archive_org,
    "polona":      fetch_polona,
    "dlibra":      fetch_dlibra,
    "szwa":        fetch_szwa,
}


# =============================================================================
# Canonical source URL (for manifest provenance field)
# =============================================================================

def _source_url(target: dict) -> str:
    source = target.get("source", "")
    if source == "archive_org":
        return f"https://archive.org/details/{target.get('id', '')}"
    if source == "polona":
        return f"https://polona.pl/item/{target.get('id', '')}"
    if source == "dlibra":
        return target.get("url", "")
    if source == "szwa":
        return (
            f"https://www.szukajwarchiwach.gov.pl/zasob/jednostki"
            f"?zespol={target.get('zespol','')}&jednostka={target.get('jednostka','')}"
        )
    return ""


# =============================================================================
# Predicted filename before download  (needed for idempotency check)
# =============================================================================

def _expected_filename(target: dict) -> str:
    """
    Compute the canonical output filename for a queue entry without downloading.

    This mirrors the naming convention used inside each fetcher so we can check
    the manifest for idempotency *before* making any HTTP requests.
    """
    source = target.get("source", "")
    if source == "archive_org":
        return f"archive_org_{target['id']}.pdf"
    if source == "polona":
        return f"polona_{target['id']}.pdf"
    if source == "dlibra":
        url_id = target.get("url", "").rstrip("/").split("/")[-1]
        return f"dlibra_{url_id}.pdf"
    if source == "szwa":
        return f"szwa_{target['zespol']}_{target['jednostka']}.pdf"
    return f"unknown_{target.get('id', 'item')}.pdf"


# =============================================================================
# Main orchestrator
# =============================================================================

def run(
    cfg: dict,
    queue: list[dict],
    out_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    source_filter: str | None = None,
) -> None:
    """
    Iterate the harvest queue and dispatch each entry to its fetcher.

    Parameters
    ----------
    cfg           : Loaded config dict.
    queue         : Parsed harvest_queue.json.
    out_dir       : data/input_scans/ — Module 1's input directory.
    force         : Re-download even if already in the manifest.
    dry_run       : Log what would happen; write nothing.
    source_filter : If given, skip entries whose source doesn't match.
    """
    # Create the output directory *before* any manifest reads so
    # processed_set() doesn't crash on a fresh run.
    out_dir.mkdir(parents=True, exist_ok=True)

    done  = processed_set(out_dir)   # set of (doc_id, page=1) already downloaded
    delay = cfg.get("harvest", {}).get("rate_limit_delay_s", 2.0)
    _get  = _make_retry_get(cfg)

    total  = len(queue)
    errors = 0

    for idx, target in enumerate(queue, start=1):
        source = target.get("source", "")

        # ── 1. Source filter ──────────────────────────────────────────────
        if source_filter and source != source_filter:
            continue

        # ── 2. Validate source ────────────────────────────────────────────
        if source not in FETCHERS:
            log.error(
                "[%d/%d] Unknown source '%s'. Valid: %s",
                idx, total, source, list(FETCHERS),
            )
            errors += 1
            continue

        # ── 3. Idempotency check ──────────────────────────────────────────
        # We derive the expected filename deterministically so we can check
        # the manifest without any network calls.
        expected  = _expected_filename(target)
        doc_id    = get_doc_id(expected)
        note      = target.get("note", "")

        if not force and (doc_id, 1) in done:
            log.info(
                "[%d/%d] %-12s  %s  already in manifest — skipping  (--force to re-download)",
                idx, total, source, expected,
            )
            continue

        log.info(
            "[%d/%d] %-12s  %s%s",
            idx, total, source, expected, f"  ({note})" if note else "",
        )

        # ── 4. Fetch ──────────────────────────────────────────────────────
        try:
            filename, title, page_count = FETCHERS[source](
                target, cfg, out_dir, _get, dry_run=dry_run
            )
        except RetryError as exc:
            log.error("[%d/%d] %-12s  FAILED (max retries): %s", idx, total, source, exc)
            errors += 1
            continue
        except Exception as exc:
            log.error("[%d/%d] %-12s  FAILED: %s", idx, total, source, exc)
            errors += 1
            continue

        if dry_run:
            continue

        # ── 5. Append manifest ────────────────────────────────────────────
        # append_manifest (from utils.py) writes directly — out_dir already
        # exists because we called mkdir above.
        final_path = out_dir / filename
        append_manifest(out_dir, {
            "doc_id":        get_doc_id(filename),
            "page":          1,                  # sentinel: "file downloaded"
            "filename":      filename,
            "source_system": source,
            "source_id":     target.get("id", ""),
            "original_url":  _source_url(target),
            "title":         title,
            "file_type":     "pdf" if filename.endswith(".pdf") else "jpg",
            "page_count":    page_count,
            "checksum_md5":  _md5(final_path) if final_path.exists() else "",
            "status":        "ready",
        })
        log.info("           manifest ✓  doc_id=%s", get_doc_id(filename))

        # ── 6. Rate-limit pause ───────────────────────────────────────────
        if idx < total:
            time.sleep(delay)

    log.info("Done. %d target(s) in queue, %d error(s).", total, errors)
    if errors:
        sys.exit(1)


# =============================================================================
# CLI
# =============================================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="m0_harvest.py",
        description="Module 0 — Archive PDF & Scan Download Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  python scripts/m0_harvest.py                       # normal run
  python scripts/m0_harvest.py --source polona       # one source only
  python scripts/m0_harvest.py --force               # re-download everything
  python scripts/m0_harvest.py --dry-run             # preview only
  python scripts/m0_harvest.py --config /path/to/config.yaml
""",
    )
    parser.add_argument("--force",    action="store_true",
                        help="Re-download files already in the manifest.")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print what would be fetched without writing files.")
    parser.add_argument("--source",   metavar="SRC", default=None,
                        help=f"Process only this source. One of: {list(FETCHERS)}")
    parser.add_argument("--config",   metavar="PATH", default=None,
                        help="Path to config.yaml (auto-detected if omitted).")
    parser.add_argument("--queue",    metavar="PATH", default=None,
                        help="Path to harvest_queue.json (default: data/config/harvest_queue.json).")
    parser.add_argument("--out-dir",  metavar="PATH", default=None,
                        help="Override output directory (default: from config paths.input_scans).")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    cfg = load_config(args.config)

    # Derive the project root the same way M1 does: one level above scripts/
    root = (
        Path(args.config).parent
        if args.config
        else Path(__file__).resolve().parent.parent
    )

    # Queue file
    queue_path = (
        Path(args.queue)
        if args.queue
        else root / "data" / "config" / "harvest_queue.json"
    )
    if not queue_path.exists():
        log.error("Harvest queue not found: %s", queue_path)
        sys.exit(1)
    with open(queue_path, encoding="utf-8") as fh:
        queue: list[dict] = json.load(fh)
    log.info("Loaded %d target(s) from %s", len(queue), queue_path)

    # Output directory — uses the same config key as Module 1
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else root / cfg["paths"]["input_scans"]
    )

    if args.dry_run:
        log.info("*** DRY-RUN — no files will be written ***")

    run(cfg, queue, out_dir, force=args.force, dry_run=args.dry_run, source_filter=args.source)


if __name__ == "__main__":
    main()