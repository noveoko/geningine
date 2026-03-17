#!/usr/bin/env bash
# utils/dump_for_llm_code_context.sh
# Run from anywhere — always operates on the project root (one level up from this script).
#
# Usage:
#   bash utils/dump_for_llm_code_context.sh
#   bash utils/dump_for_llm_code_context.sh > context.txt
#   bash utils/dump_for_llm_code_context.sh | xclip -selection clipboard
#   bash utils/dump_for_llm_code_context.sh | pbcopy

set -euo pipefail

# ── Resolve project root regardless of where the script is called from ────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Extensions to include ─────────────────────────────────────────────────────
INCLUDE_EXTS="py sh js jsx css yaml yml json md txt"

# ── Directories to skip ───────────────────────────────────────────────────────
SKIP_DIRS="node_modules __pycache__ .git .pytest_cache .venv venv dist build data"

# ── Files to skip by name ─────────────────────────────────────────────────────
SKIP_FILES="package-lock.json yarn.lock"

# ── Build find -name args ─────────────────────────────────────────────────────
ext_args=()
first=true
for ext in $INCLUDE_EXTS; do
    if $first; then ext_args+=( -name "*.${ext}" ); first=false
    else             ext_args+=( -o -name "*.${ext}" )
    fi
done

# ── Build find prune args ─────────────────────────────────────────────────────
prune_args=()
for d in $SKIP_DIRS; do
    prune_args+=( -path "*/${d}" -o -path "*/${d}/*" )
done

# ── Collect and filter files ──────────────────────────────────────────────────
mapfile -t FILES < <(
    find "$ROOT" \
        \( "${prune_args[@]}" \) -prune \
        -o \( "${ext_args[@]}" \) -print \
    | sort
)

filtered=()
for f in "${FILES[@]}"; do
    base=$(basename "$f")
    skip=false
    for sf in $SKIP_FILES; do [ "$base" = "$sf" ] && skip=true && break; done
    $skip || filtered+=("$f")
done

# ── Output ────────────────────────────────────────────────────────────────────
total=${#filtered[@]}
echo "# PROJECT SNAPSHOT"
echo "# Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "# Root: ${ROOT}"
echo "# Files: ${total}"
echo ""

for f in "${filtered[@]}"; do
    clean="${f#${ROOT}/}"
    lines=$(wc -l < "$f")
    size=$(wc -c < "$f")
    echo "================================================================"
    echo "FILE: ${clean}  (${lines} lines, ${size} bytes)"
    echo "================================================================"
    cat "$f"
    echo ""
done

echo "================================================================"
echo "# END OF SNAPSHOT — ${total} files"