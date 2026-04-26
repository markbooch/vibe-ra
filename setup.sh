#!/usr/bin/env bash
# Apply the vibera ExternalControl port patch to a local OpenRA checkout.
#
# Usage:   ./setup.sh /path/to/OpenRA
#
# The patch moves OpenRA's ExternalControl trait from the upstream
# default 7777 to 7778, which is what vibera defaults to. After applying
# you still need to follow OpenRA's own build instructions.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 /path/to/OpenRA" >&2
    exit 64
fi

OPENRA_DIR="$1"
PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="$PATCH_DIR/patches/openra-port.patch"

if [[ ! -d "$OPENRA_DIR/mods/ra" ]]; then
    echo "error: $OPENRA_DIR does not look like an OpenRA checkout (missing mods/ra)" >&2
    exit 65
fi
if [[ ! -f "$PATCH_FILE" ]]; then
    echo "error: cannot find $PATCH_FILE" >&2
    exit 66
fi

cd "$OPENRA_DIR"
echo "applying ExternalControl port patch (7777 -> 7778) in $OPENRA_DIR ..."
git apply --check "$PATCH_FILE" 2>/dev/null || {
    # Already applied?
    if grep -q "Port: 7778" mods/ra/rules/world.yaml; then
        echo "patch already applied, nothing to do."
        exit 0
    fi
    echo "error: patch does not apply cleanly. Inspect mods/ra/rules/world.yaml manually." >&2
    exit 1
}
git apply "$PATCH_FILE"

echo "done. Build OpenRA per upstream docs, then run:"
echo "    mono OpenRA.dll Game.Mod=ra"
