#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

python3 -m compileall -q mars_deimos run.py tests
python3 -m unittest discover -s tests -v
python3 run.py bank-stats

if [[ "${1:-}" == "--online" ]]; then
  python3 run.py probe
fi

echo "verify-ok"
