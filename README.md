# WinSnipper

Windows-style snipping and annotation for Linux on Wayland and X11. The standalone
app uses your desktop's screenshot portal; the optional GNOME Shell 45–49 extension
provides instant frozen-screen selection (primary target: Zorin OS 18 / GNOME 46).
Requires Python 3, PyGObject and GTK 4.

**Debian package:** download `winsnipper_*_all.deb` from the latest GitHub release,
run `sudo apt install ./winsnipper_*_all.deb`, then log out and back in. At that
login each user's Super+Shift+S is taken over once (conflicts are backed up);
`winsnipper-setup --remove` restores the original bindings and disables it, and
`winsnipper-setup` turns it back on. Run `winsnipper-setup --remove` before
`sudo apt remove winsnipper`. Releases are built by `.github/workflows/release.yml`
on every push to master (`VERSION` plus the run number); build one locally with
`packaging/build-deb.sh 0.1.0`.

**From a checkout:** run `./install.sh`. It installs per user without elevated
privileges. On GNOME it enables the extension and backs up conflicting
Super+Shift+S bindings; on other desktops it installs the portable app. Use
`--gnome` or `--portable` to choose explicitly.
**For the GNOME extension on Wayland, log out and back in once.** `./install.sh --dry-run` previews actions
without changing files or settings; dependency checks run during actual installation.
The editor entry point must be present in `editor/` before installing.

**Standalone app (GNOME, KDE, and other desktops with a Screenshot portal):**
run `./install.sh --portable` to install without GNOME Shell or shortcut changes.
Launch **WinSnipper** from your applications menu or run `winsnipper`. Use **New
snip**, select a mode and optional 3/5/10-second delay, then annotate the result.
`Ctrl+N` opens the snip controls and `Ctrl+O` opens an existing PNG. For a custom
desktop shortcut, bind `winsnipper --capture`; options include
`winsnipper --capture --mode area --delay 3` (`choose`, `area`, `window`, `screen`;
CLI delays 0–60 seconds). Configure the shortcut in your desktop's keyboard settings.

Portable capture needs `xdg-desktop-portal` and a Screenshot backend selected for
your running desktop (for example GNOME, KDE, wlr or Hyprland). Installing only
`xdg-desktop-portal-gtk` is insufficient. The system picker and permission prompts
remain in control. Mode requests are sent when the portal advertises Screenshot
v3 and the target; older backends choose modes in their own dialog and may offer
only whole-screen capture. There is no universal Wayland window enumeration,
identical overlay, or guarantee that hover menus survive the portal interaction.
If the service is absent, denied, cancelled or times out, the app restores its
controls and explains what happened. Portable captures open immediately for
editing and copying; they do not use the GNOME extension toast.

**Native X11 without a Screenshot portal (for example XFCE/MATE):** install the
optional `gir1.2-gtk-3.0` package and select **X11 full screen (no picker)**, or run
`winsnipper --capture --mode x11-screen`. This explicitly captures the X11 root
screen; use the editor's Crop tool for a rectangle. It does not offer native window
selection or a portal picker. GTK3 runs in an isolated helper process, so the
editor remains GTK4. The app never switches to direct X11 capture automatically
after a portal failure or denial. This mode is disabled on Wayland and detected
Xwayland servers; Wayland always uses its compositor's Screenshot portal.

Portal images are copied into private `Pictures/WinSnipper` files before editing
(XDG Pictures location, falling back to `~/Pictures`). Autosave never overwrites
the portal's source file. Existing `WinSnipper` output folders must have private
permissions (0700). Screenshot files use 0600 permissions. Clipboard persistence
after app exit depends on the desktop's clipboard manager when the GNOME extension
is unavailable.

**GNOME extension:** press **Super+Shift+S**, choose Rectangle, Window or Fullscreen, then drag or click.
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
