#!/usr/bin/env bash
# Downloads SEC filings and runs the full pipeline (bronze -> silver -> gold
# -> anomaly log -> Tableau export). Run from the project root:
#   ./scripts/run_pipeline.sh
set -euo pipefail

if [ -z "${EDGAR_CONTACT_EMAIL:-}" ]; then
    echo "Set EDGAR_CONTACT_EMAIL to a real contact email before running." >&2
    echo 'Example: export EDGAR_CONTACT_EMAIL="you@example.com"' >&2
    exit 1
fi

export PYTHONPATH=.
PYTHON="${PYTHON:-python3}"

echo "== Step 1/2: Resolve CIKs and download SEC Financial Statement Data Sets =="
"$PYTHON" -m src.download.edgar_downloader

echo "== Step 2/2: Spark pipeline =="
"$PYTHON" -m src.pipeline.run

echo "Done. Draft commentary next, e.g.:"
echo "  $PYTHON -m src.cli draft --ticker INTC --quarter 2024Q2"
