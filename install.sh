#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
UUID='winsnipper@winsnipper.local'
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DATA_DIR="$DATA_HOME/winsnipper"
DRY_RUN=false
MODE=auto
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --portable|--gnome)
            selected_mode="${arg#--}"
            if [[ "$MODE" != auto && "$MODE" != "$selected_mode" ]]; then
                printf 'Choose either --portable or --gnome.\n' >&2; exit 2
            fi
            MODE="$selected_mode" ;;
        --help|-h) printf 'Usage: %s [--dry-run] [--portable | --gnome]\n' "$0"; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done
if [[ "$MODE" == auto ]]; then
    DESKTOP_NAME="${XDG_CURRENT_DESKTOP:-}"
    if [[ "${DESKTOP_NAME^^}" == *GNOME* ]]; then MODE=gnome; else MODE=portable; fi
fi
if "$DRY_RUN"; then
    printf '%s\n' "Would install WinSnipper in $MODE mode under $DATA_HOME." \
        "Would install launchers in $HOME/.local/bin and desktop/D-Bus entries." \
        'Would check Python 3, PyGObject, cairo and GTK 4.'
    if [[ "$MODE" == gnome ]]; then
        printf '%s\n' 'Would check GNOME Shell >= 45 and compile extension schemas.' \
            "Would preserve $DATA_DIR/keybinding-backup.json and take over Super+Shift+S."
    else
        printf '%s\n' 'Bind your desktop screenshot shortcut to: winsnipper --capture'
    fi
    exit 0
fi
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import cairo, gi; gi.require_version("Gtk", "4.0"); gi.require_foreign("cairo"); from gi.repository import Gtk' 2>/dev/null; then
    printf '%s\n' 'Python 3, PyGObject, cairo and GTK 4 are required.' \
        'Install dependencies with: sudo apt install python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-4.0 xdg-desktop-portal' >&2
    exit 1
fi
python3 "$SCRIPT_DIR/tools/install.py" check
if [[ "$MODE" == gnome ]]; then
    if ! command -v gnome-shell >/dev/null 2>&1; then
        printf 'GNOME Shell 45 or newer is required for --gnome. Use --portable on another desktop.\n' >&2
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
    python3 "$SCRIPT_DIR/tools/keybindings.py" --backup "$DATA_DIR/keybinding-backup.json" disable
    python3 "$SCRIPT_DIR/tools/keybindings.py" enable-extension
    if command -v gnome-extensions >/dev/null 2>&1; then gnome-extensions enable "$UUID" || true; fi
    printf '%s\n' 'WinSnipper installed. Log out and back in once, then press Super+Shift+S.'
else
    python3 "$SCRIPT_DIR/tools/install.py" install --root "$SCRIPT_DIR" --portable
    printf '%s\n' 'WinSnipper installed. Launch WinSnipper or bind your shortcut to: winsnipper --capture' \
        'Screen capture requires xdg-desktop-portal and the screenshot backend for your desktop.' \
        'Optional native X11 full-screen capture also needs gir1.2-gtk-3.0.'
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DATA_HOME/applications" || true
fi
