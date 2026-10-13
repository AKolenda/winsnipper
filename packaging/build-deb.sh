#!/usr/bin/env bash
# Usage: packaging/build-deb.sh VERSION [OUT_DIR]  ->  OUT_DIR/winsnipper_VERSION_all.deb
set -euo pipefail

VERSION=${1:?usage: build-deb.sh VERSION [OUT_DIR]}
if ! dpkg --validate-version "$VERSION" 2>/dev/null; then
    printf 'Invalid Debian package version: %s\n' "$VERSION" >&2
    exit 2
fi
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUT_DIR=$(mkdir -p "${2:-$ROOT/dist}" && cd -- "${2:-$ROOT/dist}" && pwd)
UUID='winsnipper@winsnipper.local'
STAGE=$(mktemp -d)
trap 'rm -rf -- "$STAGE"' EXIT
PKG="$STAGE/winsnipper"

install -d "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/winsnipper/tools" \
    "$PKG/usr/share/gnome-shell/extensions/$UUID" "$PKG/usr/share/glib-2.0/schemas" \
    "$PKG/usr/share/applications" "$PKG/usr/share/dbus-1/services" "$PKG/etc/xdg/autostart" \
    "$PKG/usr/share/doc/winsnipper"

# The schema goes in the system directory, where dpkg's glib trigger compiles it.
# A schemas/ dir without gschemas.compiled inside the extension would break getSettings().
cp -R "$ROOT/extension/." "$PKG/usr/share/gnome-shell/extensions/$UUID/"
mv "$PKG/usr/share/gnome-shell/extensions/$UUID/schemas/"*.gschema.xml "$PKG/usr/share/glib-2.0/schemas/"
rm -rf "$PKG/usr/share/gnome-shell/extensions/$UUID/schemas"

cp -R "$ROOT/editor" "$PKG/usr/share/winsnipper/editor"
rm -rf "$PKG/usr/share/winsnipper/editor/tests" "$PKG/usr/share/winsnipper/editor/README.md"
find "$PKG/usr/share/winsnipper/editor" \( -name __pycache__ -o -name '*.pyc' \) -prune -exec rm -rf {} +
install -m 644 "$ROOT/tools/keybindings.py" "$PKG/usr/share/winsnipper/tools/keybindings.py"
install -m 755 "$ROOT/packaging/winsnipper-setup" "$PKG/usr/bin/winsnipper-setup"
install -m 644 "$ROOT/README.md" "$PKG/usr/share/doc/winsnipper/README.md"

cat > "$PKG/usr/bin/winsnipper-editor" <<'SH'
#!/usr/bin/env bash
exec python3 /usr/share/winsnipper/editor/winsnipper_editor.py "$@"
SH
chmod 755 "$PKG/usr/bin/winsnipper-editor"

cat > "$PKG/usr/share/applications/io.github.winsnipper.Editor.desktop" <<'INI'
[Desktop Entry]
Type=Application
Name=WinSnipper Editor
Exec=/usr/bin/winsnipper-editor %U
Icon=accessories-screenshot
DBusActivatable=true
NoDisplay=true
Terminal=false
MimeType=image/png;
INI

cat > "$PKG/usr/share/dbus-1/services/io.github.winsnipper.Editor.service" <<'INI'
[D-BUS Service]
Name=io.github.winsnipper.Editor
Exec=/usr/bin/winsnipper-editor --gapplication-service
INI

# Takes over Super+Shift+S once per user at their next login; later choices stick.
cat > "$PKG/etc/xdg/autostart/io.github.winsnipper.Setup.desktop" <<'INI'
[Desktop Entry]
Type=Application
Name=WinSnipper first-run setup
Exec=/usr/bin/winsnipper-setup --first-run
OnlyShowIn=GNOME;
NoDisplay=true
X-GNOME-Autostart-Phase=Applications
INI

find "$PKG" -type d -exec chmod 755 {} +
find "$PKG/usr/share" "$PKG/etc" -type f -exec chmod 644 {} +

cat > "$PKG/DEBIAN/control" <<CONTROL
Package: winsnipper
Version: $VERSION
Architecture: all
Maintainer: AKolenda <AKolenda@users.noreply.github.com>
Installed-Size: $(du -sk --exclude=DEBIAN "$PKG" | cut -f1)
Depends: gnome-shell (>= 45), python3, python3-gi, python3-gi-cairo, python3-cairo, gir1.2-gtk-4.0, libglib2.0-bin
Section: graphics
Priority: optional
Homepage: https://github.com/AKolenda/winsnipper
Description: Windows-style Super+Shift+S snipping for GNOME Shell
 Freezes the screen before the selection UI appears, so hover states are kept,
 copies the snip, saves it to Pictures/Screenshots and opens an annotation
 editor from the toast. Each user's shortcut is taken over at their next login;
 run winsnipper-setup --remove to restore the original bindings.
CONTROL

DEB="$OUT_DIR/winsnipper_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$DEB" >/dev/null
printf '%s\n' "$DEB"
