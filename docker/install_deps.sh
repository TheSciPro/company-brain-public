#!/bin/sh
# Install the project from pyproject.toml with the extras named in $1 (comma-separated, default: graph).
# GraphRAG-SDK is not re-pinned here: whatever git commit pyproject.toml names is what pip installs.
# If torch is in that closure, install the CPU wheel first so a later install does not fetch the CUDA build.
set -eu
cd "$(dirname "$0")/.."
extras=${1:-graph}
target=".[$extras]"
pip install --upgrade pip
plan=$(mktemp)
if ! pip install --dry-run --ignore-installed "$target" >"$plan" 2>&1; then
  cat "$plan" >&2
  exit 1
fi
if grep -Eq '^(Collecting torch|Would install torch)' "$plan"; then
  echo "torch is a dependency; installing the CPU wheel"
  pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
else
  echo "torch is not a dependency; skipping the CPU wheel"
fi
rm -f "$plan"
pip install --no-cache-dir --upgrade-strategy only-if-needed "$target"
