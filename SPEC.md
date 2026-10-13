# WinSnipper — Windows-style Snipping Tool for Linux

Goal: Super+Shift+S **instantly freezes the screen exactly as it looks at the moment of the key press**
(hover menus, tooltips, hover states preserved), lets the user select a region on the frozen image,
copies it to the clipboard, saves it to ~/Pictures/Screenshots, and shows a toast in the bottom-right
corner. Clicking the toast opens a lightweight annotation editor; every edit auto-saves to the same
file and refreshes the clipboard. Must feel as fast as GNOME's built-in screenshot UI.

Target: Zorin OS 18 (Ubuntu 24.04 base, **GNOME Shell 46**), Wayland session (X11 must also work).
Extension metadata declares shell-version ["45","46","47","48","49"] (ESM extension format).

## Portable capture

The standalone GTK app also captures on X11 and Wayland through
`org.freedesktop.portal.Screenshot`, without requiring the GNOME extension.
`winsnipper` opens New snip controls; `winsnipper --capture` requests capture;
`--mode choose|area|window|screen` and `--delay 0..60` configure the portal request.
Explicit `--mode x11-screen` captures the native X11 root screen using an optional
isolated GTK3 helper (gir1.2-gtk-3.0). It has no picker/window-selection overlay;
rectangles use the editor's Crop tool. It is never an automatic fallback after
portal denial or failure. Session indicators and the XWAYLAND extension reject
Wayland/Xwayland, and cancellation/20-second timeout terminate only the owned
helper process. Private temporary captures are imported through the same bounded
pipeline and then removed.
Ctrl+N opens snip controls from the editor; Ctrl+O opens a local PNG.

The desktop must provide a Screenshot portal backend. Picker UI, permissions,
supported targets and capture timing are desktop-owned. Screenshot v3 advertised
targets receive matching target hints; older portals choose modes in their dialog.
Portable operation does not guarantee frozen hover states, a uniform overlay, or
cross-compositor window enumeration. The instant freeze requirements above apply
to the GNOME extension path.

Requests are asynchronous, single-flight, subscribed before invocation, cancellable
with Request.Close, and bounded by a three-minute interaction timeout. The editor
hides before capture and restores controls/windows on cancellation or failure.
Returned local regular PNGs are copied into private owned
`<XDG_PICTURES_DIR>/WinSnipper` files (0700 directory/0600 files) before validation,
annotation, autosave or clipboard copy. Portal-owned originals are never edited.
The input cap is less than 256 MiB encoded and 32 megapixels decoded. All import
IO and image decoding run in workers.

## Architecture

```
extension/   GNOME Shell extension (runs inside the compositor)  — capture, overlay, toast, clipboard
editor/      GTK4 Python app (separate process, resident)        — portal capture and annotation
install.sh / uninstall.sh                                         — per-user install, no sudo
```

Why an extension: on Wayland only the compositor can capture the screen without a portal round-trip.
`Shell.Screenshot.screenshot_stage_to_content()` grabs the current stage as a GPU texture within one
frame, *before* we push a modal grab (a grab sends pointer-leave to apps and kills hover state).

## Fixed identifiers (contract between components — do not change)

| Thing | Value |
|---|---|
| Extension UUID | `winsnipper@winsnipper.local` |
| GSettings schema id / path | `org.gnome.shell.extensions.winsnipper` / `/org/gnome/shell/extensions/winsnipper/` |
| Shell D-Bus name / path / iface | `io.github.winsnipper.Shell` / `/io/github/winsnipper/Shell` / `io.github.winsnipper.Shell` |
| Editor app id (GApplication + D-Bus name) | `io.github.winsnipper.Editor`, object path `/io/github/winsnipper/Editor` |
| Editor install dir | `~/.local/share/winsnipper/editor/` |
| Editor launcher | `~/.local/bin/winsnipper-editor` (also `winsnipper` for capture) |
| Editor desktop file | `~/.local/share/applications/io.github.winsnipper.Editor.desktop` (DBusActivatable=true) |
| Editor D-Bus service | `~/.local/share/dbus-1/services/io.github.winsnipper.Editor.service` (Exec=`<launcher> --gapplication-service`) |
| Keybinding backup | `~/.local/share/winsnipper/keybinding-backup.json` |

### GSettings keys (`org.gnome.shell.extensions.winsnipper`)
- `snip-shortcut` (as) default `['<Shift><Super>s']`
- `save-to-disk` (b) default `true`
- `save-directory` (s) default `''` → means `<XDG_PICTURES_DIR>/Screenshots` (create if missing)
- `toast-timeout` (i) default `7` seconds (0 = never auto-hide)
- `default-mode` (s) default `'rectangle'` (one of rectangle|window|fullscreen)

When `save-to-disk` is false the file is still written (the editor needs a file) but to
`$XDG_CACHE_HOME/winsnipper/` instead.

File names: `Screenshot From YYYY-MM-DD HH-MM-SS.png` (append ` (2)`, ` (3)` … on collision).

### Shell D-Bus interface (exported by the extension on the session bus)
```xml
<interface name="io.github.winsnipper.Shell">
  <method name="SetClipboardImage"><arg type="s" name="path" direction="in"/></method>
  <method name="Snip"/>
</interface>
```
- `SetClipboardImage(path)`: path must be absolute, end in `.png`, be a regular file < 256 MiB.
  Reads it asynchronously and sets clipboard `image/png` via `St.Clipboard`. Clipboard owned by the
  shell persists after the editor exits (that is the point of this method). Returns a D-Bus error on failure.
- `Snip()`: starts a snip exactly as the shortcut does (handy for testing / alternate bindings).

### Editor activation (called by the extension)
- Prewarm when the toast appears: `org.freedesktop.Application.ActivateAction('prewarm', [], {})`
  on `io.github.winsnipper.Editor` `/io/github/winsnipper/Editor` (D-Bus activation starts the process).
  Must not show any window. Once the file is saved, call
  `ActivateAction('prewarm-path', [<s absolute-path>], {})` to decode it in the editor's executor.
  This string action also accepts an empty string for process-only prewarm. The original
  parameterless action remains supported. Validated images are cached for up to two minutes
  by resolved path, mtime_ns, and size, and consumed on open when the key still matches.
- Open on toast click: `org.freedesktop.Application.Open(['file:///…png'], {})` (`(asa{sv})`).
- All calls async; failures logged, never thrown into the shell.
- Fallback if D-Bus activation fails: spawn `~/.local/bin/winsnipper-editor <path>` via `GLib.spawn_async`/`Gio.Subprocess`.

## UX

### Snip overlay (extension)
1. Shortcut pressed → FIRST action is the stage capture (also snapshot visible window rects for
   window mode). Nothing else (no grab, no actor creation) before the capture is requested.
2. Fullscreen overlay across all monitors shows the frozen image, dimmed (~40% black) except the
   current selection which shows undimmed. Crosshair cursor.
3. Small toolbar top-center of the primary monitor: Rectangle | Window | Fullscreen | ✕.
   Esc cancels. Right-click cancels.
4. Rectangle: press-drag-release → finishes on release. A click without drag (<4 px) does nothing.
   Show selection border (1–2 px white/accent) and size label (W × H) near the selection.
5. Window: hover highlights the topmost window under the pointer (rects from the snapshot taken at
   capture time, in stacking order); click captures that rectangle from the frozen image.
6. Fullscreen: click anywhere captures the whole frozen image (all monitors).
7. On finish: destroy overlay & pop modal IMMEDIATELY, then encode PNG
   (`Shell.Screenshot.composite_to_stream`), set clipboard, show toast, and write file async.
   HiDPI/fractional scaling: selection is in stage (logical) coords; crop in texture pixels using the
   capture scale exactly like gnome-shell's own `ui/screenshot.js` does.

### Toast (extension)
- Bottom-right of the primary monitor's work area (above Zorin's bottom panel), 16 px margin.
- Thumbnail + "Snip copied to clipboard" + "Select here to mark up and share." + small ✕.
- Slides/fades in; auto-hides after `toast-timeout`; hovering pauses the timer.
- Click → dismiss toast, await its pending save, then open the editor with the resulting file.
  If saving fails, report the error without opening. A new snip replaces an existing toast.

### Editor (Python, GTK 4, no libadwaita requirement)
- Opens the PNG, window sized to image (max ~85% of monitor), image fit-to-window.
- Tools: Pen, Highlighter, Eraser (stroke eraser — removes whole strokes it touches), Crop.
  Colour palette (≈8 swatches + custom), thickness choice. Undo/Redo. Copy. Save As. Open folder.
- Shortcuts: Ctrl+Z undo, Ctrl+Shift+Z / Ctrl+Y redo, Ctrl+C copy, Ctrl+S save-as, Ctrl+W close,
  P/H/E/X(crop) tools, Ctrl+0 fit, Ctrl+1 100%, Ctrl+scroll zoom.
- Auto-save: after every completed edit (stroke end, erase, undo, redo, crop) debounce ~250 ms,
  render full-resolution PNG off the UI thread, write atomically (tmp file + os.replace) over the
  same path, then call Shell `SetClipboardImage(path)`; on failure fall back to `Gdk.Clipboard.set_texture`.
  Subtle "Saved" indicator. Closing the window flushes any pending save first.
- Resident: GApplication with HANDLES_OPEN, D-Bus activatable, `prewarm` action, inactivity timeout
  ~20 min so the next open is instant. Opening an already-open file presents its window.
- Smooth drawing on a 4K screenshot (cache committed strokes; only redraw what's needed).

## Hard rules
- Never block the shell's main loop: no sync file IO, no sync D-Bus calls, no spawn_sync in the extension.
- Every signal/timeout/keybinding/bus-name acquired in `enable()` is released in `disable()`.
- No `sudo` anywhere in install scripts. Installer prints the apt command if deps are missing.
- Pure, testable logic lives in small modules with unit tests (`extension/lib/*.js` tested with node;
  `editor/` model/render tested with python unittest).
