# WinSnipper editor

Run `python3 editor/winsnipper_editor.py /absolute/path/to/screenshot.png`.
The installer can copy this directory unchanged. Runtime
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

## Untested environments

Target Python 3.12 / PyGObject 3.48 / GTK 4.14 have not been exercised. Real GNOME
Shell 46, Wayland clipboard persistence, touch/stylus hardware, compositor frame
latency, system-theme switching, and desktop file-manager/portal interactions
need a Zorin desktop check.
