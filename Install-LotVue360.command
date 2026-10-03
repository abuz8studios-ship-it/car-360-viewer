#!/bin/bash
cd "$(dirname "$0")"
bash ./install-lotvue.sh
status=$?
printf '\nPress Return to close this window.'
read -r _
exit "$status"
