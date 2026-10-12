#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
UUID='winsnipper@winsnipper.local'
DATA_DIR="$HOME/.local/share/winsnipper"
EXTENSION_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
EDITOR_DIR="$DATA_DIR/editor"
LAUNCHER="$HOME/.local/bin/winsnipper-editor"
DESKTOP="$HOME/.local/share/applications/io.github.winsnipper.Editor.desktop"
SERVICE="$HOME/.local/share/dbus-1/services/io.github.winsnipper.Editor.service"
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
        'Would check GNOME Shell >= 45, python3, PyGObject and Gtk 4.' \
        "Would copy extension/ to $EXTENSION_DIR and compile its schemas." \
        "Would copy editor/ (excluding tests and __pycache__) to $EDITOR_DIR." \
        "Would write launcher: $LAUNCHER" \
        "Would write desktop file: $DESKTOP" \
        "Would write D-Bus service: $SERVICE" \
        "Would scan/remove only Super+Shift+S conflicts; preserve $DATA_DIR/keybinding-backup.json." \
        "Would add $UUID to enabled-extensions and try gnome-extensions enable." \
        'Would run update-desktop-database if available.' \
        'On Wayland, log out and back in once after installing.'
    exit 0
fi

if ! command -v gnome-shell >/dev/null 2>&1; then
    printf 'GNOME Shell 45 or newer is required; gnome-shell was not found.\n' >&2
    exit 1
fi
SHELL_VERSION=$(gnome-shell --version)
if [[ ! "$SHELL_VERSION" =~ ([0-9]+)\. ]] || (( BASH_REMATCH[1] < 45 )); then
    printf 'GNOME Shell 45 or newer is required (found: %s).\n' "$SHELL_VERSION" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' 2>/dev/null; then
    printf '%s\n' 'Python 3 with PyGObject and Gtk 4 is required.' 'Install dependencies with: sudo apt install python3-gi gir1.2-gtk-4.0' >&2
    exit 1
fi
for dependency in gsettings glib-compile-schemas; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Required command is missing: %s\n' "$dependency" >&2
        exit 1
    fi
done
if [[ ! -f "$SCRIPT_DIR/editor/winsnipper_editor.py" ]]; then
    printf 'Missing editor/winsnipper_editor.py; install from a complete WinSnipper checkout.\n' >&2
    exit 1
fi

mkdir -p "$EXTENSION_DIR" "$EDITOR_DIR" "$HOME/.local/bin" "$(dirname -- "$DESKTOP")" "$(dirname -- "$SERVICE")" "$DATA_DIR/tools"
cp -R "$SCRIPT_DIR/extension/." "$EXTENSION_DIR/"
glib-compile-schemas "$EXTENSION_DIR/schemas"
cp "$SCRIPT_DIR/tools/keybindings.py" "$DATA_DIR/tools/keybindings.py"
# Copy every editor module/asset, excluding test and bytecode directories.
python3 - "$SCRIPT_DIR/editor" "$EDITOR_DIR" "$LAUNCHER" "$DESKTOP" "$SERVICE" <<'PY'
from pathlib import Path
import shlex
import shutil
import sys

source, destination, launcher, desktop, service = map(Path, sys.argv[1:])
shutil.copytree(source, destination, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('tests', '__pycache__', '*.pyc'))
entry = destination / 'winsnipper_editor.py'
launcher.write_text('#!/usr/bin/env bash\nset -euo pipefail\nexec python3 ' + shlex.quote(str(entry)) + ' "$@"\n')
launcher.chmod(0o755)
# Desktop Entry / D-Bus service Exec use double quotes, not shell single quotes.
def exec_quote(path):
    return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'
def desktop_quote(path):
    # Desktop value escaping is applied before its Exec command-line parsing.
    return exec_quote(path).replace('\\', '\\\\').replace('%', '%%')
desktop.write_text('[Desktop Entry]\nType=Application\nName=WinSnipper Editor\n'
                   'Exec=' + desktop_quote(launcher) + ' %U\nIcon=accessories-screenshot\n'
                   'DBusActivatable=true\nNoDisplay=true\nTerminal=false\nMimeType=image/png;\n')
service.write_text('[D-BUS Service]\nName=io.github.winsnipper.Editor\n'
                   'Exec=' + exec_quote(launcher) + ' --gapplication-service\n')
PY
# Only now take over Super+Shift+S, so a failed copy never leaves it unbound.
# Failures preserve the backup and stop before the extension is enabled.
python3 "$SCRIPT_DIR/tools/keybindings.py" --backup "$DATA_DIR/keybinding-backup.json" disable
python3 "$SCRIPT_DIR/tools/keybindings.py" enable-extension
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions enable "$UUID" || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" || true
fi
printf '%s\n' 'WinSnipper installed. On Wayland, log out and back in once, then press Super+Shift+S.'
