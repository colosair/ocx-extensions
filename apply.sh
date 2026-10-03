#!/usr/bin/env bash
# Bootstrap this machine: picker policy on top of OpenCodex's live catalog + quota skill.
# Safe to re-run: every step is idempotent and user-disabled models are never re-enabled.
# The steps live in scripts/apply.py, so the Windows host-native fallback can run them too.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v ocx >/dev/null 2>&1 || {
  echo "OpenCodex (ocx) is required but was not found." >&2
  echo "Please install OpenCodex first, then re-run this script." >&2
  exit 1
}

# First Python 3 that actually runs. On Windows this skips the Microsoft Store alias stub.
py=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; sys.exit(sys.version_info[0] != 3)' >/dev/null 2>&1; then
    py="$(command -v "$candidate")"
    break
  fi
done
[ -n "$py" ] || { echo "python3 is required." >&2; exit 1; }

exec "$py" "$repo_dir/scripts/apply.py" "$@"
