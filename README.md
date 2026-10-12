# WinSnipper

Windows-style snipping for GNOME Shell 45–49, targeting Zorin OS 18 / GNOME 46.
Works on Wayland and X11. Requires Python 3, PyGObject and GTK 4.

Run `./install.sh` from this checkout. It installs per user without elevated
privileges, enables the extension and backs up conflicting Super+Shift+S bindings.
**On Wayland, log out and back in once.** `./install.sh --dry-run` previews actions
without changing files or settings; dependency checks run during actual installation.
The editor entry point must be present in `editor/` before installing.

Press **Super+Shift+S**, choose Rectangle, Window or Fullscreen, then drag or click.
Escape/right-click cancels. A snip is copied and saved to Pictures/Screenshots;
click its bottom-right toast to annotate. Hover pauses dismissal. Extension
preferences control destination, default mode and timeout (0 keeps the toast).
Turning off “Save to disk” keeps an editor file in the user cache instead.

The compositor captures the stage **before** creating the selection UI or taking
an input grab, preserving hover states. Selection uses the frozen image, with
capture-scale cropping for HiDPI. After the UI is destroyed, PNG encoding, disk
writes and editor D-Bus activation run asynchronously. The shell owns the image
clipboard, so it survives editor exit. Repeated shortcuts during a snip are ignored.

Run `./uninstall.sh` to disable/remove the extension and editor and restore the
original shortcut bindings. `--dry-run` is supported. Screenshots are retained.
Reinstall preserves the first shortcut backup; if new conflicts appeared since
installation, uninstall first to establish a fresh backup safely.

If the shortcut does nothing, log out/in on Wayland and check
`gnome-extensions info winsnipper@winsnipper.local`. Inspect errors with
`journalctl --user -f /usr/bin/gnome-shell` (messages start with `[winsnipper]`).
Check permissions on the save directory and that the editor launcher exists at
`~/.local/bin/winsnipper-editor`. The installer prints the apt command when GTK
bindings are missing. GNOME Shell 45 is the minimum; 46 is the primary target.

Development checks: `node --test tests/*.test.js`,
`python3 -m unittest discover -s tests -p 'test_keybindings.py'`,
`find extension -name '*.js' -exec node --check {} \;`,
`bash -n install.sh uninstall.sh`, and `shellcheck install.sh uninstall.sh`.
Optional GJS IO/activation-signature check: `gjs -m tests/gjs-io-smoke.js`.
GNOME capture/grab/clipboard/preview APIs were checked against upstream tags
45.0, 46.0, 47.0, 48.0 and 49.0; desktop integration requires testing in a real session.
