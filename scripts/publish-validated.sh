#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/publish-validated.sh [--dry-run|--publish]

  --dry-run  Validate and display the exact publication allow-list (default).
  --publish  Validate, commit only READY files, and push only origin/main.
EOF
}

mode="${1:---dry-run}"
if (( $# > 1 )); then
  usage >&2
  exit 64
fi

case "$mode" in
  --dry-run)
    echo "SAFE/DRY-RUN: validation and publication allow-list check only"
    echo "No files will be staged, committed, or pushed."
    exec python3 "$repo_root/scripts/validation-gate.py" publish-dry-run
    ;;
  --publish)
    echo "CONTROLLED PUBLISH: validation, exact READY staging, commit, and origin/main push"
    exec python3 "$repo_root/scripts/validation-gate.py" publish
    ;;
  -h|--help)
    usage
    ;;
  *)
    echo "Unknown option: $mode" >&2
    usage >&2
    exit 64
    ;;
esac
