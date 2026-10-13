"""D-Bus-activatable GTK application, image loading, and resident lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import sys

import cairo

from .gtk_support import Gio, GLib, Gtk, dispatch, show_error
from .loading import ImageLoader

APPLICATION_ID = "io.github.winsnipper.Editor"


class EditorApplication(Gtk.Application):
    def __init__(self, application_id=APPLICATION_ID):
        super().__init__(application_id=application_id,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.set_inactivity_timeout(20 * 60 * 1000)
        self.windows_by_path = {}
        self.loading = set()
        self.prewarming = set()
        self.capture_window = None
        self.capture_request = None
        self._capture_timer = None
        self._capture_busy = False
        self._hidden_windows = []
        self._shutting_down = False
        self._open_dialog = None
        self._open_error_dialog = None
        self.add_main_option("capture", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Take a screenshot using the desktop portal", None)
        self.add_main_option("mode", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Capture mode: choose, area, window, screen", "MODE")
        self.add_main_option("delay", 0, GLib.OptionFlags.NONE, GLib.OptionArg.INT,
                             "Delay before capture (0–60 seconds)", "SECONDS")
        self.add_main_option(GLib.OPTION_REMAINING, 0, GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING_ARRAY, "PNG files to edit", "PNG…")
        self.loader = ImageLoader()
        self._prewarm_expiry = None
        self.connect("window-removed", self._window_removed)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="snip-io")

    def _window_removed(self, _application, window):
        if window is self.capture_window:
            self.capture_window = None
        for path, existing in tuple(self.windows_by_path.items()):
            if existing is window:
                del self.windows_by_path[path]

    def do_startup(self):
        Gtk.Application.do_startup(self)
        action = Gio.SimpleAction.new("prewarm", None)
        action.connect("activate", self._prewarm)
        self.add_action(action)
        path_action = Gio.SimpleAction.new("prewarm-path", GLib.VariantType.new("s"))
        path_action.connect("activate", self._prewarm_path)
        self.add_action(path_action)
        for name, callback in (("new-snip", self.show_capture), ("open", self._choose_open)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _parameter, call=callback: call())
            self.add_action(action)
        for name, accelerators in {
            "app.new-snip": ["<Primary>n"], "app.open": ["<Primary>o"],
            "win.undo": ["<Primary>z"],
            "win.redo": ["<Primary><Shift>z", "<Primary>y"],
            "win.copy": ["<Primary>c"],
            "win.save-as": ["<Primary>s"],
            "win.close": ["<Primary>w"],
            "win.pen": ["p"], "win.highlighter": ["h"],
            "win.eraser": ["e"], "win.crop": ["x"],
            "win.fit": ["<Primary>0"], "win.actual-size": ["<Primary>1"],
        }.items():
            self.set_accels_for_action(name, accelerators)
        self._prewarm()

    def _prewarm(self, *_args):
        from . import window  # noqa: F401 -- no window creation or visible activation
        # A balanced hold/release starts the inactivity timer. GTK holds the
        # application itself while any application windows are open.
        self.hold()
        self.release()

    def _prewarm_path(self, _action, parameter):
        self._prewarm()
        raw_path = parameter.get_string()
        if not raw_path:
            return
        path = Path(os.path.abspath(os.path.expanduser(raw_path)))
        if (path in self.prewarming or path in self.loading or path in self.windows_by_path
                or len(self.prewarming) >= 2 or self._shutting_down):
            return
        self.prewarming.add(path)
        self.hold()
        future = self.executor.submit(self.loader.load, path, True)

        def completed():
            try:
                future.result()
            except (OSError, ValueError, cairo.Error):
                pass  # Speculative load: no dialog and no window.
            finally:
                self.prewarming.discard(path)
                if self._prewarm_expiry is not None:
                    GLib.source_remove(self._prewarm_expiry)
                self._prewarm_expiry = GLib.timeout_add_seconds(121, self._expire_prewarm)
                self.release()
        future.add_done_callback(lambda _future: dispatch(completed))

    def _expire_prewarm(self):
        self._prewarm_expiry = None
        self.loader.expire()
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        window = self.get_active_window()
        if window is not None:
            window.present()
            return
        self.show_capture()

    @staticmethod
    def _option_error(options):
        mode, delay = options.get("mode", "choose"), options.get("delay", 0)
        capture, files = options.get("capture", False), options.get(GLib.OPTION_REMAINING, [])
        if mode not in ("choose", "area", "window", "screen") or not 0 <= delay <= 60:
            return "Choose mode choose/area/window/screen and delay 0–60 seconds."
        if (not capture and ("mode" in options or "delay" in options)) or (capture and files):
            return "Use --mode and --delay with --capture; open PNG files separately."
        return None

    def do_handle_local_options(self, options):
        values = {}
        for name in ("capture", "mode", "delay", GLib.OPTION_REMAINING):
            value = options.lookup_value(name, None)
            if value is not None:
                values[name] = value.unpack()
        error = self._option_error(values)
        if error is not None:
            print(error, file=sys.stderr)
            return 2
        return -1

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        capture = options.get("capture", False)
        mode = options.get("mode", "choose")
        delay = options.get("delay", 0)
        files = options.get(GLib.OPTION_REMAINING, [])
        if self._option_error(options) is not None:
            return 2
        if capture:
            self.start_capture(mode, delay)
        elif files:
            self.do_open([command_line.create_file_for_arg(path) for path in files], len(files), "")
        else:
            self.activate()
        return 0

    def show_capture(self, present=True):
        if self.capture_window is None:
            from .capture_window import CaptureWindow
            self.capture_window = CaptureWindow(self)
        if present:
            self.capture_window.present()
        return self.capture_window

    def start_capture(self, mode="choose", delay=0):
        if self._capture_busy or self._shutting_down:
            return
        if mode not in ("choose", "area", "window", "screen") or not 0 <= delay <= 60:
            raise ValueError("Invalid capture mode or delay.")
        window = self.show_capture(present=delay > 0)
        self._capture_busy = True
        self.hold()
        window.set_busy(True)
        remaining = delay

        def countdown():
            nonlocal remaining
            if remaining:
                window.status.set_label(f"Taking a snip in {remaining} seconds…")
                remaining -= 1
                self._capture_timer = GLib.timeout_add(1000, countdown)
            else:
                self._capture_timer = None
                self._hidden_windows = [w for w in self.get_windows() if w.get_visible()]
                for hidden in self._hidden_windows:
                    hidden.set_visible(False)
                # Give the compositor a chance to remove our windows before
                # opening its picker. Timing of the actual capture is desktop-owned.
                self._capture_timer = GLib.timeout_add(200, self._request_capture, mode)
            return GLib.SOURCE_REMOVE
        countdown()

    def _request_capture(self, mode):
        from .capture import PortalCapture
        self._capture_timer = None
        self.capture_request = PortalCapture(self.get_dbus_connection(), self._capture_response,
                                             self.capture_window.status.set_label)
        self.capture_request.start(mode)
        return GLib.SOURCE_REMOVE

    def cancel_capture(self):
        if not self._capture_busy:
            return
        if self._capture_timer is not None:
            GLib.source_remove(self._capture_timer)
            self._capture_timer = None
            self._capture_finished(cancelled=True)
        elif self.capture_request is not None:
            self.capture_request.cancel()
        # A received image is already importing in a bounded worker operation.

    def _capture_response(self, uri, error, cancelled):
        self.capture_request = None
        if error or cancelled:
            self._capture_finished(error=error, cancelled=cancelled)
            return
        from .capture_files import import_capture
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        pictures = pictures or str(Path.home() / "Pictures")
        future = self.executor.submit(import_capture, uri, pictures, self.loader)

        def imported():
            try:
                path, base = future.result()
            except Exception as error:
                self._capture_finished(error=f"Could not import the screenshot. {error}")
            else:
                self._capture_finished(path=path, base=base)
        future.add_done_callback(lambda _future: dispatch(imported))

    def _capture_finished(self, path=None, base=None, error=None, cancelled=False):
        self._capture_busy = False
        for window in self._hidden_windows:
            if window is not self.capture_window and window.get_application() is self:
                window.set_visible(True)
        self._hidden_windows = []
        try:
            if self._shutting_down:
                return
            if path is not None:
                if self.capture_window is not None:
                    self.capture_window.destroy()
                window = self._present_image(path, base)
                window._clipboard(path)
            else:
                window = self.show_capture()
                window.set_busy(False)
                window.status.set_label("Snip cancelled." if cancelled else str(error))
        finally:
            self.release()

    def _present_image(self, path, base):
        if path in self.windows_by_path:
            window = self.windows_by_path[path]
        else:
            from .window import EditorWindow
            window = EditorWindow(self, path, base)
            self.windows_by_path[path] = window
        window.present()
        return window

    def do_open(self, files, _count, _hint):
        for file in files:
            path = file.get_path()
            if path is None:
                self._open_error("Please open a local PNG file.")
            else:
                self.open_path(path)

    def open_path(self, path):
        path = Path(os.path.abspath(os.path.expanduser(path)))
        if path in self.windows_by_path:
            self.windows_by_path[path].present()
            return
        if path in self.loading or self._shutting_down:
            return
        if len(self.loading) >= 8:
            self._open_error("Please wait for the current screenshots to open, then try the remaining files again.")
            return  # Bound untrusted D-Bus/CLI bursts.
        self.loading.add(path)
        self.hold()

        def load():
            canonical = path.resolve()
            return canonical, self.loader.load(canonical)
        future = self.executor.submit(load)

        def completed():
            self.loading.remove(path)
            try:
                canonical, base = future.result()
            except (OSError, ValueError, cairo.Error):
                self._open_error(f"Could not read “{path.name}”.\n"
                                 "The file may have moved or may not be a valid PNG image.")
            else:
                self._present_image(canonical, base)
            finally:
                self.release()
        future.add_done_callback(lambda _future: dispatch(completed))

    def _open_error(self, message):
        if self._open_error_dialog is not None:
            self._open_error_dialog.present()
            return
        self.hold()
        def closed():
            self._open_error_dialog = None
            self.release()
        self._open_error_dialog = show_error(self.get_active_window(), "Could not open screenshot",
                                             message, on_close=closed)
        self._open_error_dialog.set_application(self)

    def _choose_open(self):
        if self._open_dialog is not None:
            return
        self.hold()
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog(title="Open screenshot")

            def chosen(source, result):
                try:
                    file = source.open_finish(result)
                    self.do_open([file], 1, "")
                except GLib.Error:
                    pass  # user cancelled
                finally:
                    self._open_dialog = None
                    self.release()
            self._open_dialog = dialog
            dialog.open(self.get_active_window(), None, chosen)
        else:
            dialog = Gtk.FileChooserNative(title="Open screenshot",
                                           action=Gtk.FileChooserAction.OPEN,
                                           accept_label="Open", cancel_label="Cancel")

            def response(source, response_id):
                if response_id == Gtk.ResponseType.ACCEPT:
                    self.do_open([source.get_file()], 1, "")
                source.destroy()
                self._open_dialog = None
                self.release()
            self._open_dialog = dialog
            dialog.connect("response", response)
            dialog.show()

    def do_shutdown(self):
        self._shutting_down = True
        self.cancel_capture()
        if self._prewarm_expiry is not None:
            GLib.source_remove(self._prewarm_expiry)
            self._prewarm_expiry = None
        self.executor.shutdown(wait=False)
        Gtk.Application.do_shutdown(self)
