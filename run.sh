#!/usr/bin/env bash
# Run the TAIEX Drawdown Atlas dashboard locally.
#
# Usage:
#   ./run.sh              Fetch latest data, then serve at http://localhost:3001
#   ./run.sh --no-fetch   Skip fetching, serve existing data/taiex-daily.json as-is
#   ./run.sh --port 9000  Serve on a custom port
#
# Data is cached in data/raw/, so a first-time fetch takes ~110 minutes but
# subsequent runs only fetch new trading days (usually seconds).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PORT=3001
DO_FETCH=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-fetch) DO_FETCH=0; shift ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ "$DO_FETCH" -eq 1 ]]; then
  echo "Fetching TAIEX data (incremental; cached responses are reused)..."
  python3 fetch_data.py
fi

echo "Serving dashboard at http://localhost:${PORT}"
exec python3 -m http.server "$PORT"
