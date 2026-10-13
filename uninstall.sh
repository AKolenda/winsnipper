#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
UUID='winsnipper@winsnipper.local'
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DATA_DIR="$DATA_HOME/winsnipper"
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --help|-h) printf 'Usage: %s [--dry-run]\n' "$0"; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done
if "$DRY_RUN"; then
    printf '%s\n' "Would disable $UUID, restore unchanged keybindings, and remove WinSnipper program files." \
        "Would preserve unavailable keybinding records in $DATA_DIR/keybinding-backup.json." \
        'Saved screenshots are retained.'
    exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf 'python3 is required to safely uninstall WinSnipper.\n' >&2; exit 1
fi
HELPER_DIR="$SCRIPT_DIR/tools"
if [[ ! -f "$HELPER_DIR/install.py" ]]; then HELPER_DIR="$DATA_DIR/tools"; fi
python3 "$HELPER_DIR/install.py" check
if command -v gsettings >/dev/null 2>&1; then
    if command -v gnome-extensions >/dev/null 2>&1; then gnome-extensions disable "$UUID" || true; fi
    python3 "$HELPER_DIR/keybindings.py" disable-extension
    python3 "$HELPER_DIR/keybindings.py" --backup "$DATA_DIR/keybinding-backup.json" restore
elif [[ -e "$DATA_DIR/keybinding-backup.json" ]]; then
    printf 'gsettings is required to restore saved keybindings; program files were retained.\n' >&2; exit 1
fi
python3 "$HELPER_DIR/install.py" uninstall
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database "$DATA_HOME/applications" || true; fi
printf '%s\n' 'WinSnipper uninstalled. Saved screenshots are retained.'
