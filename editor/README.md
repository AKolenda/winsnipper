# WinSnipper editor

Run `python3 editor/winsnipper_editor.py` for New snip / Open image controls,
`python3 editor/winsnipper_editor.py --capture --mode area --delay 3` for a portal
capture, or `python3 editor/winsnipper_editor.py /absolute/path/to/screenshot.png`
to edit a file. Mode names are `choose`, `area`, `window`, `screen`; delay is 0–60
seconds. Invalid combinations exit with status 2. CLI invocations forward to the
resident process; `--gapplication-service` continues to start invisibly.
The installer can copy this directory (excluding `tests/`) unchanged. Runtime
packages on Zorin/Ubuntu are `python3-gi`, `python3-gi-cairo`, `python3-cairo`, and
`gir1.2-gtk-4.0`. Portable capture also needs `xdg-desktop-portal` and your desktop's
Screenshot backend; `xdg-desktop-portal-gtk` alone is insufficient. No libadwaita is used. `--gapplication-service` is supported.

`winsnipper_editor.py` is the entry point. `winsnipper/model.py` contains immutable
strokes, crop viewports, and undo/redo; `render.py` contains cairo rendering,
smoothed-path eraser hit testing, and the committed view cache; `saving.py`
contains atomic PNG writes and the coalescing save queue. These modules do not
import GTK. `app.py`, `window.py`, `clipboard.py`, and `gtk_support.py` implement
the application, UI, clipboard ownership, and main-loop helpers. `capture.py`
implements the asynchronous portal protocol; `capture_files.py` imports local
portal PNGs into private owned output; `capture_window.py` provides mode/delay controls.

Edits save after a 250 ms debounce to the original path. Rendering and PNG
encoding run in a worker, and replacement happens only after the temporary file
has been completely written and fsynced. Each document has one write in flight
and one newest pending snapshot. Closing finishes any active mark and flushes
the newest snapshot; a write failure keeps the window and its edits open so the
user can retry. Original decoded pixels remain in memory for undo while the
window is open. Undo history is not persisted across closing and reopening.

The palette and five thickness choices open from header-bar popovers. Pen and
highlighter remember separate colours and widths. Crop applies on drag release
(or Enter during a drag); Escape cancels the current gesture. The status appears
in the bottom bar. Ctrl+scroll zooms; Ctrl+0 fits; Ctrl+1 uses one image pixel per
logical display pixel. The native GTK theme follows system settings. Ctrl+N opens New snip controls;
Ctrl+O opens an existing PNG. The controls support no delay or 3/5/10 seconds.
WinSnipper windows hide before the portal is invoked and restore on cancellation
or failure. Only one capture is active at a time. Closing its controls cancels a
pending countdown. The portal owns capture selection and permission prompts.

## Local tests without installing system packages

From the repository root, with a distro-matched `/usr/bin/python3`:

```bash
bash editor/tests/setup-test-env.sh
source editor/tests/test-env.sh
/usr/bin/python3 -m unittest discover -s editor/tests
```

The setup script downloads distro `.deb` archives and uses `dpkg -x` to extract
them into the gitignored `.testenv/`. It does not install packages and needs no
root privileges. For GTK tests, add `--gtk` to the setup command. The script also
extracts runtime dependencies and compiles GTK's local GSettings schemas.
Python native modules must match the interpreter's ABI; do not substitute a
Python virtual environment with a different Python version. Existing apt
metadata, `apt-get`, `dpkg`, and `dpkg-architecture` are required.

If Xvfb and `dbus-run-session` are available:

```bash
bash editor/tests/setup-test-env.sh --gtk
source editor/tests/test-env.sh
GTK_A11Y=none GDK_BACKEND=x11 GSK_RENDERER=cairo WINSNIPPER_GTK_TESTS=1 \
  xvfb-run -a dbus-run-session -- /usr/bin/python3 -m unittest discover -s editor/tests
GTK_A11Y=none GDK_BACKEND=x11 GSK_RENDERER=cairo \
  xvfb-run -a dbus-run-session -- /usr/bin/python3 editor/tests/smoke_service.py
/usr/bin/python3 editor/tests/benchmark.py
```

The regular discovery command runs the pure cairo/model/save tests and skips
GTK checks unless `WINSNIPPER_GTK_TESTS=1`. Integration checks exercise actual
GTK construction, gestures, save failures and close flushing, exports, friendly
corrupt-file errors, duplicate opening, shortcuts, CLI forwarding, D-Bus Open
and prewarm, clipboard fallback, and older-dialog API fallbacks. Shell clipboard
calls use a small test D-Bus service, not the real GNOME extension.

## Compatibility and D-Bus contract

The application ID and path are `io.github.winsnipper.Editor` and
`/io/github/winsnipper/Editor`. The `prewarm` action creates no window; the
inactivity timeout is 20 minutes, with GTK holding the app while windows are
open. Clipboard updates asynchronously call `SetClipboardImage(s)` on
`io.github.winsnipper.Shell`, at `/io/github/winsnipper/Shell` using the same
interface name.

GTK APIs used are available on 4.14. `Gtk.ColorDialog`, `Gtk.FileDialog`, and
`Gtk.FileLauncher` (4.10+) are feature-detected; fallbacks use
`Gtk.ColorChooserDialog`, `Gtk.FileChooserNative`, and
`Gio.AppInfo.launch_default_for_uri`. `Gtk.MessageDialog` and style-context
colour lookup are deliberately retained for compatibility, despite deprecation
warnings from newer GTK. No GTK 4.16+ API is required.

`Gdk.Clipboard.set_texture` is [not exposed to language bindings](https://docs.gtk.org/gdk4/method.Clipboard.set_texture.html).
The fallback uses `Clipboard.set()` with an explicit `Gdk.Texture` GValue.
Standard `org.freedesktop.Application.Open` takes `(as, a{sv})`; both the editor
and extension use that signature. There is no compatibility filter for an
incorrect three-argument Open call.

Portable capture follows the [Screenshot portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Screenshot.html)
and [Request lifecycle](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Request.html).
The client subscribes before invoking Screenshot, handles an early Response or
legacy returned handle, closes requests on cancellation/timeout, and delivers one
completion. Screenshot v3 target hints are sent only when advertised; earlier
versions delegate mode choice to the system dialog. Interactive permission and
capture UI remain desktop-controlled, so frozen hover menus and identical window
selection are not promised outside the GNOME extension.

Returned images must be local regular PNG files. The importer pins a nonblocking,
no-follow source descriptor, bounds encoded size (less than 256 MiB) and decoded
pixels (32 megapixels), and writes a unique private copy in the XDG Pictures
folder's `WinSnipper` directory. It validates the decoded copy before opening it
and removes partial/invalid copies; the portal source is never changed or removed.
Speculative prewarm submissions are deduplicated and limited to two pending paths;
file opens are limited to eight concurrent loads, keeping D-Bus bursts from
creating an unlimited executor queue. Additional concurrent opens can be retried.

The view cache renders at the display scale when shrinking and uses native
image resolution when zoomed, avoiding enormous zoom-scaled allocations. New
strokes append to the committed surface; live marks draw over it. Highlighter
opacity is applied once to an opaque stroke mask bounded to the mark, so a
stroke's self-intersections do not darken. Separate highlighter marks compound
intentionally, and multiply blending preserves dark screenshot text.

## Verification environment and limits

Development tests run on Debian 13, Python 3.13.5, PyGObject 3.50, GTK 4.18.6,
and pycairo 1.27.0 extracted locally. The test suite includes pure image/import tests and GTK integration checks.
Xvfb allows headless X11 GTK checks; the
service smoke test runs the real `--gapplication-service` lifecycle and keeps a
window open beyond the default 10-second service startup timeout. Target
Python 3.12 / PyGObject 3.48 / GTK 4.14 have not been executed here. GTK tests also run on a real headless Weston Wayland compositor. Portal tests
use an explicitly registered test D-Bus service: they validate protocol ordering,
cancellation, errors, secure import, CLI forwarding and autosave isolation, and do
not prove any real portal backend's picker behavior. Real GNOME Shell 46, KDE/wlr/
Hyprland portal pickers, Wayland clipboard persistence, touch/stylus hardware,
compositor frame latency and system-theme switching still need desktop checks.

A synthetic cached frame plus an 800-point live stroke measured approximately
2.6 ms pen / 4.0 ms highlighter at 3840×2160, and 3.5 ms / 4.6 ms at 5120×2880
(view cache at half scale). Full atomic PNG writes measured 251 ms and 432 ms,
respectively, off the UI thread. These are cairo microbenchmarks of a solid
screenshot, not a hardware/input or photographic-PNG encoding guarantee.
