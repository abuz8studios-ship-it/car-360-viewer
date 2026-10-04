#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: Uninstall-LotVue360.sh [--prefix DIR] [--remove-data] [--yes]

Removes the per-user LotVue360 program and desktop-menu entry. User data is
kept by default. --remove-data permanently deletes the local database, secret,
settings, uploads, and logs; it requires an explicit confirmation (or --yes).
EOF
}

: "${HOME:?HOME must be set for a per-user uninstall}"
INSTALL_PREFIX=${LOTVUE_INSTALL_PREFIX:-"$HOME/.local/opt"}
REMOVE_DATA=0
CONFIRM=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "--prefix requires a directory" >&2
                exit 2
            fi
            INSTALL_PREFIX=$2
            shift ;;
        --remove-data) REMOVE_DATA=1 ;;
        --yes) CONFIRM=1 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$(uname -s)" != "Linux" ]; then
    echo "This uninstaller is for Linux only." >&2
    exit 1
fi
case "$INSTALL_PREFIX" in
    /*) ;;
    *) INSTALL_PREFIX="$PWD/$INSTALL_PREFIX" ;;
esac
TARGET_APP="$INSTALL_PREFIX/LotVue360"
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
case "$DATA_HOME" in
    /*) ;;
    *) echo "XDG_DATA_HOME must be an absolute path." >&2; exit 1 ;;
esac
DESKTOP_FILE="$DATA_HOME/applications/lotvue360.desktop"
DATA_DIR="$DATA_HOME/LotVue360"

if [ ! -d "$TARGET_APP" ]; then
    echo "LotVue360 is not installed at $TARGET_APP." >&2
    exit 1
fi
if command -v pgrep >/dev/null 2>&1 && pgrep -x LotVue360 >/dev/null 2>&1; then
    echo "Quit LotVue360 before uninstalling it." >&2
    exit 1
fi
if [ "$REMOVE_DATA" -eq 1 ] && [ "$CONFIRM" -ne 1 ]; then
    printf 'Permanently delete all LotVue360 user data at %s? Type yes to continue: ' "$DATA_DIR"
    IFS= read -r answer
    if [ "$answer" != "yes" ]; then
        echo "User data was kept."
        exit 0
    fi
fi

rm -rf "$TARGET_APP"
if [ -f "$DESKTOP_FILE" ] && grep -Fqx 'X-LotVue360-Managed=true' "$DESKTOP_FILE"; then
    rm -f "$DESKTOP_FILE"
fi
if [ "$REMOVE_DATA" -eq 1 ]; then
    rm -rf "$DATA_DIR"
    echo "Removed LotVue360 and its local user data."
else
    printf 'Removed LotVue360. User data was kept at %s.\n' "$DATA_DIR"
fi
