#!/usr/bin/env bash
#
# One-shot install for Playwright + the Chromium binary it drives.
# Run this *after* `pip install -e .` (or after installing the wheel).

set -euo pipefail

echo "Installing Playwright Chromium runtime..."
python -m playwright install chromium

echo "Done. You can now run:"
echo "    python -m immobiliare_export --config ricerca.yml"
