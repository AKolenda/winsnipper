#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
UUID='winsnipper@winsnipper.local'
DATA_DIR="$HOME/.local/share/winsnipper"
EXTENSION_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --help|-h) printf 'Usage: %s [--dry-run]\n' "$0"; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

if "$DRY_RUN"; then
    printf '%s\n' \
        "Would disable $UUID and remove it from enabled-extensions." \
        "Would remove $EXTENSION_DIR and $DATA_DIR/editor/." \
        "Would remove $HOME/.local/bin/winsnipper-editor." \
        "Would remove io.github.winsnipper.Editor.desktop and io.github.winsnipper.Editor.service." \
        "Would restore original keybindings from $DATA_DIR/keybinding-backup.json, then delete the backup." \
        'Would run update-desktop-database if available. Saved screenshots are retained.'
    exit 0
fi

if ! command -v python3 >/dev/null 2>&1 || ! command -v gsettings >/dev/null 2>&1; then
    printf 'python3 and gsettings are required to safely restore keybindings.\n' >&2
    exit 1
fi
HELPER="$SCRIPT_DIR/tools/keybindings.py"
if [[ ! -f "$HELPER" ]]; then
    HELPER="$DATA_DIR/tools/keybindings.py"
fi
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions disable "$UUID" || true
fi
python3 "$HELPER" disable-extension
# Restore before removing files so a failed restoration keeps the helper/backup.
python3 "$HELPER" --backup "$DATA_DIR/keybinding-backup.json" restore
rm -rf -- "$EXTENSION_DIR" "$DATA_DIR/editor" "$DATA_DIR/tools"
rm -f -- "$HOME/.local/bin/winsnipper-editor" \
    "$HOME/.local/share/applications/io.github.winsnipper.Editor.desktop" \
    "$HOME/.local/share/dbus-1/services/io.github.winsnipper.Editor.service"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" || true
fi
printf '%s\n' 'WinSnipper uninstalled; original keybindings restored. Saved screenshots are retained.'
