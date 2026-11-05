#!/bin/sh
# Finder-friendly alias for the single native macOS startup path.
ROOT=$(CDPATH= cd "$(dirname "$0")" && pwd)
exec /bin/sh "$ROOT/scripts/start_macos.command" "$@"
