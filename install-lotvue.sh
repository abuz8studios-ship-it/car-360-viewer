#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_APP="$SCRIPT_DIR/LotVue360.app"
TARGET_ROOT="${HOME}/Applications"
TARGET_APP="$TARGET_ROOT/LotVue360.app"
DATA_DIR="${HOME}/Library/Application Support/LotVue360"
OPEN_APP=1

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-open) OPEN_APP=0 ;;
        --applications)
            TARGET_ROOT="/Applications"
            TARGET_APP="$TARGET_ROOT/LotVue360.app" ;;
        --help|-h)
            echo "Usage: bash install-lotvue.sh [--no-open] [--applications]"
            echo "Default installs for your user in ~/Applications (no admin needed)."
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ ! -d "$SOURCE_APP" ]; then
    echo "LotVue360.app is missing beside this installer. Extract the complete release ZIP first." >&2
    exit 1
fi
if pgrep -x LotVue360 >/dev/null 2>&1; then
    echo "Quit LotVue360 before installing or updating it." >&2
    exit 1
fi
mkdir -p "$TARGET_ROOT" "$DATA_DIR"
STAGING_APP="$TARGET_ROOT/.LotVue360-update-$$.app"
rm -rf "$STAGING_APP"
ditto "$SOURCE_APP" "$STAGING_APP"
if [ -e "$TARGET_APP" ]; then
    rm -rf "$TARGET_APP"
fi
mv "$STAGING_APP" "$TARGET_APP"

printf 'Installed: %s\n' "$TARGET_APP"
printf 'User data: %s (separate from the app bundle)\n' "$DATA_DIR"
echo 'The release is not Developer ID signed or notarized. If Gatekeeper blocks first launch,'
printf 'use Finder to open %s, then right-click LotVue360.app > Open and confirm the macOS dialog.\n' "$TARGET_APP"
if [ "$OPEN_APP" -eq 1 ]; then
    open "$TARGET_APP" || {
        echo "Could not open the app automatically. Open it from Finder: $TARGET_APP" >&2
        exit 1
    }
fi
