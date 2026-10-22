#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="${1:-$project_dir/rollback-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$backup_dir"

for relative in config.json .state data/learned.jsonl; do
  source_path="$project_dir/$relative"
  if [[ -e "$source_path" ]]; then
    destination="$backup_dir/$relative"
    mkdir -p "$(dirname "$destination")"
    mv "$source_path" "$destination"
    echo "moved: $relative"
  fi
done

echo "rollback-backed-up: $backup_dir"
