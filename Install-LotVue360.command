#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
bash ./install-lotvue.sh "$@"
status=$?
if [ "${LOTVUE_INSTALLER_NO_PAUSE:-0}" != "1" ]; then
    printf '\nPress Return to close this window.'
    read -r _
fi
exit "$status"
