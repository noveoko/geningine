#!/usr/bin/env python3
"""
migrate.py — One-shot migration script.

Run from the project root (the directory that contains app/ and harvester_app/):

    python migrate.py
    python migrate.py --dry-run   # preview every action, write nothing

What this script does
---------------------
  1. app/scripts/m0_harvest.py   ← copy from harvester_app/m0_harvest.py
  2. app/data/config/harvest_queue.json
                                 ← copy from harvester_app/harvest_queue.json
  3. app/config.yml              ← merge harvest: + paths: sections in, then
     rename → app/config.yaml      rename to .yaml (utils.py hardcodes config.yaml)
  4. requirements.txt (root)     ← merge harvester_app/requirements.txt,
                                    deduplicating lines
  5. harvester_app/              ← remove entirely (all targets already copied)

Nothing outside these five operations is touched.
"""

import argparse
import shutil
import sys
from pathlib import Path

# yaml is needed only for the config merge step; everything else is stdlib.
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required.  Run:  pip install pyyaml")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _log(dry_run: bool, msg: str) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"{prefix}{msg}")


def _copy(src, dst, dry_run):
    _log(dry_run, f"COPY     {src}  →  {dst}")
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _rename(src, dst, dry_run):
    _log(dry_run, f"RENAME   {src}  →  {dst}")
    if not dry_run:
        src.rename(dst)


def _remove_tree(path, dry_run):
    _log(dry_run, f"REMOVE   {path}/")
    if not dry_run:
        shutil.rmtree(path)


# ──────────────────────────────────────────────────────────────────────────────
# Step 3 — config merge
# ──────────────────────────────────────────────────────────────────────────────

# These are the sections M0 needs.  We inject them only if they are absent from
# the existing config — we never overwrite keys the user has already set.
HARVEST_DEFAULTS = {
    "harvest": {
        "rate_limit_delay_s": 2.0,
        "max_retries": 5,
        "retry_wait_min_s": 4,
        "retry_wait_max_s": 60,
        "preferred_format": {
            "archive_org": ["Text PDF", "Single Page Processed JP2 ZIP"],
            "polona":       ["pdf", "iiif_jpg"],
            "dlibra":       ["pdf", "djvu"],
            "szwa":         ["pdf", "jpg"],
        },
        "djvu_convert": True,
        "szwa_api_key": "",
    },
    "paths": {
        "input_scans":    "data/input_scans",
        "output_cleaned": "data/output_cleaned",
        "output_ocr":     "data/output_ocr",
        "output_dzi":     "data/output_dzi",
        "output_index":   "data/output_index",
    },
}


def _merge_config(existing_yml, dry_run):
    """
    Load the existing app/config.yml, inject any missing top-level sections
    from HARVEST_DEFAULTS, write back to app/config.yaml, then remove
    app/config.yml.

    We do a shallow merge at the top level: if a key already exists (e.g.
    'paths') we leave it entirely untouched so we never clobber M1/M2/M3
    settings the user has already configured.
    """
    with open(existing_yml, encoding="utf-8") as fh:
        current: dict = yaml.safe_load(fh) or {}

    added: list[str] = []
    for section, defaults in HARVEST_DEFAULTS.items():
        if section not in current:
            current[section] = defaults
            added.append(section)

    target_yaml = existing_yml.with_suffix(".yaml")  # app/config.yaml

    if added:
        _log(dry_run, f"MERGE    {existing_yml}  +sections: {added}  →  {target_yaml}")
    else:
        _log(dry_run, f"MERGE    {existing_yml}  (all sections already present)  →  {target_yaml}")

    if not dry_run:
        with open(target_yaml, "w", encoding="utf-8") as fh:
            yaml.dump(current, fh, allow_unicode=True, default_flow_style=False, sort_keys=False)
        existing_yml.unlink()
        _log(dry_run, f"REMOVE   {existing_yml}  (superseded by {target_yaml.name})")


# ──────────────────────────────────────────────────────────────────────────────
# Step 4 — requirements merge
# ──────────────────────────────────────────────────────────────────────────────

def _merge_requirements(root_req, harvester_req, dry_run):
    """
    Merge harvester_app/requirements.txt into the root requirements.txt.

    Lines that are already present (exact match after stripping whitespace and
    lowercasing) are skipped.  Comment lines and blank lines from the source
    are also skipped to keep the root file tidy.
    """
    existing_lines = root_req.read_text(encoding="utf-8").splitlines() if root_req.exists() else []
    existing_normalised = {l.strip().lower() for l in existing_lines if l.strip() and not l.startswith("#")}

    new_lines: list[str] = []
    for line in harvester_req.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower() not in existing_normalised:
            new_lines.append(stripped)

    if not new_lines:
        _log(dry_run, f"MERGE    {harvester_req}  →  {root_req}  (nothing new to add)")
        return

    _log(dry_run, f"MERGE    {harvester_req}  →  {root_req}  (+{len(new_lines)} line(s): {new_lines})")
    if not dry_run:
        with open(root_req, "a", encoding="utf-8") as fh:
            fh.write("\n# --- harvester (m0) ---\n")
            for line in new_lines:
                fh.write(line + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ──────────────────────────────────────────────────────────────────────────────

def _check_sources(root):
    """Return a list of error messages for any missing source paths."""
    errors = []
    required = [
        root / "harvester_app" / "m0_harvest.py",
        root / "harvester_app" / "harvest_queue.json",
        root / "harvester_app" / "requirements.txt",
        root / "app" / "config.yml",
        root / "app" / "scripts" / "utils.py",
    ]
    for p in required:
        if not p.exists():
            errors.append(f"  missing: {p.relative_to(root)}")
    return errors


def _check_conflicts(root):
    """Warn about anything that would be overwritten."""
    warnings = []
    targets = [
        root / "app" / "scripts" / "m0_harvest.py",
        root / "app" / "data" / "config" / "harvest_queue.json",
        root / "app" / "config.yaml",
    ]
    for p in targets:
        if p.exists():
            warnings.append(f"  exists (will overwrite): {p.relative_to(root)}")
    return warnings


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def migrate(root, dry_run):
    print(f"\nProject root: {root}")
    print(f"Mode:         {'DRY-RUN' if dry_run else 'LIVE'}\n")

    # ── Pre-flight checks ──────────────────────────────────────────────────
    errors = _check_sources(root)
    if errors:
        print("ERROR: Required source files not found:")
        print("\n".join(errors))
        print("\nAre you running this from the project root?")
        sys.exit(1)

    conflicts = _check_conflicts(root)
    if conflicts and not dry_run:
        print("WARNING: The following files will be overwritten:")
        print("\n".join(conflicts))
        answer = input("\nContinue? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    print("─" * 60)

    # ── Step 1: m0_harvest.py ─────────────────────────────────────────────
    print("\nStep 1: Copy m0_harvest.py into app/scripts/")
    _copy(
        root / "harvester_app" / "m0_harvest.py",
        root / "app" / "scripts" / "m0_harvest.py",
        dry_run,
    )

    # ── Step 2: harvest_queue.json ────────────────────────────────────────
    print("\nStep 2: Copy harvest_queue.json into app/data/config/")
    _copy(
        root / "harvester_app" / "harvest_queue.json",
        root / "app" / "data" / "config" / "harvest_queue.json",
        dry_run,
    )

    # ── Step 3: config merge + rename .yml → .yaml ────────────────────────
    print("\nStep 3: Merge harvest/paths sections into app/config.yml → app/config.yaml")
    _merge_config(root / "app" / "config.yml", dry_run)

    # ── Step 4: requirements merge ────────────────────────────────────────
    print("\nStep 4: Merge harvester requirements into root requirements.txt")
    _merge_requirements(
        root / "requirements.txt",
        root / "harvester_app" / "requirements.txt",
        dry_run,
    )

    # ── Step 5: remove harvester_app/ ────────────────────────────────────
    print("\nStep 5: Remove harvester_app/")
    _remove_tree(root / "harvester_app", dry_run)

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    if dry_run:
        print("DRY-RUN complete.  Re-run without --dry-run to apply.")
    else:
        print("Migration complete.  Final structure:")
        print("  app/scripts/m0_harvest.py         ✓")
        print("  app/data/config/harvest_queue.json ✓")
        print("  app/config.yaml                   ✓  (was config.yml)")
        print("  requirements.txt                  ✓  (merged)")
        print("  harvester_app/                    ✓  (removed)")
        print()
        print("Next steps:")
        print("  pip install -r requirements.txt")
        print("  python app/scripts/m0_harvest.py --dry-run")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate harvester_app/ files into the app/ pipeline structure."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview every action without writing or deleting anything.",
    )
    parser.add_argument(
        "--root", metavar="PATH", default=None,
        help="Project root directory (default: directory containing this script).",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent
    migrate(root, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
