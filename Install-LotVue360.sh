#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: Install-LotVue360.sh [--no-open] [--prefix DIR]

Installs the self-contained Linux x86_64 app for the current user (no sudo),
adds a desktop-menu entry, and opens Dealer Studio unless --no-open is passed.
The database, settings, uploads, and local secrets stay in the user's XDG data
folder, separate from the installed program files.
EOF
}

: "${HOME:?HOME must be set for a per-user install}"
INSTALL_PREFIX=${LOTVUE_INSTALL_PREFIX:-"$HOME/.local/opt"}
OPEN_APP=1

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-open) OPEN_APP=0 ;;
        --prefix)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "--prefix requires a directory" >&2
                exit 2
            fi
            INSTALL_PREFIX=$2
            shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Linux only." >&2
    exit 1
fi
case "$(uname -m)" in
    x86_64|amd64|AMD64) ;;
    *) echo "This release supports Linux x86_64 only; found $(uname -m)." >&2; exit 1 ;;
esac

DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
case "$DATA_HOME" in
    /*) ;;
    *) echo "XDG_DATA_HOME must be an absolute path." >&2; exit 1 ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_APP=$SCRIPT_DIR
SOURCE_LAUNCHER="$SOURCE_APP/LotVue360"
SOURCE_APP_PAYLOAD="$SOURCE_APP/app"
if [ ! -x "$SOURCE_LAUNCHER" ] || [ ! -f "$SOURCE_APP_PAYLOAD/main.py" ] || [ ! -f "$SOURCE_APP_PAYLOAD/.env.example" ]; then
    echo "The complete LotVue360 Linux release must be extracted beside this installer." >&2
    exit 1
fi

APPLICATIONS_DIR="$DATA_HOME/applications"
DESKTOP_FILE="$APPLICATIONS_DIR/lotvue360.desktop"
if [ -e "$DESKTOP_FILE" ] && ! grep -Fqx 'X-LotVue360-Managed=true' "$DESKTOP_FILE"; then
    echo "Refusing to overwrite an unrelated desktop entry: $DESKTOP_FILE" >&2
    exit 1
fi
if command -v pgrep >/dev/null 2>&1 && pgrep -x LotVue360 >/dev/null 2>&1; then
    echo "Quit LotVue360 before installing or updating it." >&2
    exit 1
fi

mkdir -p "$INSTALL_PREFIX" "$APPLICATIONS_DIR"
INSTALL_PREFIX=$(CDPATH= cd -- "$INSTALL_PREFIX" && pwd)
TARGET_APP="$INSTALL_PREFIX/LotVue360"
if [ "$SOURCE_APP" = "$TARGET_APP" ]; then
    echo "Run the installer from the extracted release folder, not the installed app folder." >&2
    exit 1
fi

EXEC_PATH="$TARGET_APP/LotVue360"
ICON_PATH="$TARGET_APP/app/static/desktop/lotvue.png"
# Exec uses desktop-entry quoting, not shell quoting. Escape backslashes,
# quotes, and percent field-code markers before wrapping the path in quotes.
DESKTOP_EXEC=$(printf '%s' "$EXEC_PATH" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/%/%%/g')
DESKTOP_ICON=$(printf '%s' "$ICON_PATH" | sed -e 's/\\/\\\\/g')
DESKTOP_TEMP="$APPLICATIONS_DIR/.lotvue360.desktop.$$"
cat > "$DESKTOP_TEMP" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=LotVue360
Comment=Local dealership vehicle-photo studio
Exec="$DESKTOP_EXEC"
Icon=$DESKTOP_ICON
Terminal=false
Categories=Graphics;Office;
StartupNotify=true
X-LotVue360-Managed=true
EOF
chmod 644 "$DESKTOP_TEMP"

STAGING_PARENT=$(mktemp -d "$INSTALL_PREFIX/.lotvue360-install.XXXXXX")
BACKUP_PARENT=$(mktemp -d "$INSTALL_PREFIX/.lotvue360-backup.XXXXXX")
STAGING_APP="$STAGING_PARENT/LotVue360"
TARGET_MOVED=0
TARGET_ACTIVATED=0
INSTALL_COMPLETE=0
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    rm -rf "$STAGING_PARENT" || true
    rm -f "$DESKTOP_TEMP" || true
    if [ "$INSTALL_COMPLETE" -ne 1 ]; then
        if [ "$TARGET_ACTIVATED" -eq 1 ]; then
            rm -rf "$TARGET_APP" || true
        fi
        if [ "$TARGET_MOVED" -eq 1 ] && [ -e "$BACKUP_PARENT/LotVue360" ]; then
            mv "$BACKUP_PARENT/LotVue360" "$TARGET_APP" || true
        fi
    fi
    rm -rf "$BACKUP_PARENT" || true
    exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$STAGING_APP"
cp -a "$SOURCE_APP/." "$STAGING_APP/"
if [ -e "$TARGET_APP" ]; then
    mv "$TARGET_APP" "$BACKUP_PARENT/LotVue360"
    TARGET_MOVED=1
fi
if ! mv "$STAGING_APP" "$TARGET_APP"; then
    echo "Could not activate the new install; the previous app will be restored." >&2
    exit 1
fi
TARGET_ACTIVATED=1
if ! mv -f "$DESKTOP_TEMP" "$DESKTOP_FILE"; then
    echo "Could not create the desktop-menu entry; the previous install will be restored." >&2
    exit 1
fi
INSTALL_COMPLETE=1

printf 'Installed: %s\n' "$TARGET_APP"
printf 'User data: %s/LotVue360 (separate from the app files)\n' "$DATA_HOME"
printf 'App menu entry: %s\n' "$DESKTOP_FILE"
if [ "$OPEN_APP" -eq 1 ]; then
    nohup "$TARGET_APP/LotVue360" >/dev/null 2>&1 </dev/null &
    echo "Dealer Studio is starting in your browser."
else
    echo "Run $TARGET_APP/LotVue360 or select LotVue360 from your desktop app menu."
fi
