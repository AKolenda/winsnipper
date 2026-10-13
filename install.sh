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
    printf '%s\n' "Would install WinSnipper for GNOME under $DATA_HOME." \
        "Would install the launcher in $HOME/.local/bin and desktop/D-Bus entries." \
        'Would check Python 3, PyGObject, cairo, GTK 4 and GNOME Shell >= 45.' \
        "Would preserve $DATA_DIR/keybinding-backup.json and take over Super+Shift+S."
    exit 0
fi
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import cairo, gi; gi.require_version("Gtk", "4.0"); gi.require_foreign("cairo"); from gi.repository import Gtk' 2>/dev/null; then
    printf '%s\n' 'Python 3, PyGObject, cairo and GTK 4 are required.' \
        'Install dependencies with: sudo apt install python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-4.0' >&2
    exit 1
fi
python3 "$SCRIPT_DIR/tools/install.py" check
if ! command -v gnome-shell >/dev/null 2>&1; then
    printf 'GNOME Shell 45 or newer is required for installation.\n' >&2
    exit 1
fi
SHELL_VERSION=$(gnome-shell --version)
if [[ ! "$SHELL_VERSION" =~ ([0-9]+)(\.|$) ]] || (( BASH_REMATCH[1] < 45 )); then
    printf 'GNOME Shell 45 or newer is required (found: %s).\n' "$SHELL_VERSION" >&2
    exit 1
fi
for dependency in gsettings glib-compile-schemas; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Required command is missing: %s\n' "$dependency" >&2; exit 1
    fi
done
python3 "$SCRIPT_DIR/tools/install.py" install --root "$SCRIPT_DIR"
# Only take over shortcuts after all application files are successfully installed.
python3 "$SCRIPT_DIR/tools/keybindings.py" --backup "$DATA_DIR/keybinding-backup.json" disable
python3 "$SCRIPT_DIR/tools/keybindings.py" enable-extension
if command -v gnome-extensions >/dev/null 2>&1; then gnome-extensions enable "$UUID" || true; fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DATA_HOME/applications" || true
fi
printf '%s\n' 'WinSnipper installed. Log out and back in once, then press Super+Shift+S.'
