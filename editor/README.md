# WinSnipper editor

Run `python3 editor/winsnipper_editor.py /absolute/path/to/screenshot.png`.
The installer can copy this directory (excluding `tests/`) unchanged. Runtime
packages on Zorin/Ubuntu are `python3-gi`, `python3-gi-cairo`, `python3-cairo`, and
`gir1.2-gtk-4.0`. No libadwaita is used. `--gapplication-service` is supported.

PNG files must be smaller than 256 MiB and contain no more than 134,217,728 pixels
(128 × 1024 × 1024, 512 MiB per decoded raster). Full-screen snips span every monitor
at the highest monitor scale, so a 2× laptop beside two 4K monitors produces
18240 × 4320; that fits. The editor checks dimensions before decoding, including
speculative preloads, so a small compressed file cannot force a huge allocation.

`winsnipper_editor.py` is the entry point. `winsnipper/model.py` contains immutable
strokes, crop viewports, and undo/redo; `render.py` contains cairo rendering,
smoothed-path eraser hit testing, and the committed view cache; `saving.py`
contains atomic PNG writes and the coalescing save queue. These modules do not
import GTK. `app.py`, `window.py`, `clipboard.py`, and `gtk_support.py` implement
the application, UI, clipboard ownership, and main-loop helpers.

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
logical display pixel. The native GTK theme follows system settings.

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

Two binding/protocol details need equivalents to the wording in SPEC.md:

- `Gdk.Clipboard.set_texture` is [not exposed to language bindings](https://docs.gtk.org/gdk4/method.Clipboard.set_texture.html).
  The fallback uses `Clipboard.set()` with an explicit `Gdk.Texture` GValue,
  which is the equivalent `set_value` binding and advertises image content.
- Standard `org.freedesktop.Application.Open` takes `(as, a{sv})`, whereas
  SPEC.md and the Shell client send `(as, s, a{sv})`, normally the
  `org.gtk.Application.Open` signature. A narrowly scoped incoming D-Bus filter
  accepts the spec's three-argument call and dispatches it on the main loop.
  Standard GApplication Open, actions, and CLI forwarding continue to work.

The view cache renders at the display scale when shrinking and uses native
image resolution when zoomed, avoiding enormous zoom-scaled allocations. New
strokes append to the committed surface; live marks draw over it. Highlighter
opacity is applied once to an opaque stroke mask bounded to the mark, so a
stroke's self-intersections do not darken. Separate highlighter marks compound
intentionally, and multiply blending preserves dark screenshot text.

## Verification environment and limits

Development tests run on Debian 13, Python 3.13.5, PyGObject 3.50, GTK 4.18.6,
and pycairo 1.27.0 extracted locally. All 19 pure tests and 10 GTK integration
tests pass (29 total). Xvfb allows headless X11 GTK checks; the
service smoke test runs the real `--gapplication-service` lifecycle and keeps a
window open beyond the default 10-second service startup timeout. Target
Python 3.12 / PyGObject 3.48 / GTK 4.14 have not been executed here. Real GNOME
Shell 46, Wayland clipboard persistence, touch/stylus hardware, compositor frame
latency, system-theme switching, and desktop file-manager/portal interactions
need a Zorin desktop check.

A synthetic cached frame plus an 800-point live stroke measured approximately
2.6 ms pen / 4.0 ms highlighter at 3840×2160, and 3.5 ms / 4.6 ms at 5120×2880
(view cache at half scale). Full atomic PNG writes measured 251 ms and 432 ms,
respectively, off the UI thread. These are cairo microbenchmarks of a solid
screenshot, not a hardware/input or photographic-PNG encoding guarantee.
