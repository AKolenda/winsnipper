"""D-Bus-activatable GTK application, image loading, and resident lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cairo

from .gtk_support import Gio, GLib, Gtk, dispatch, show_error
from .loading import ImageLoader
from .render import ImageTooLarge

APPLICATION_ID = "io.github.winsnipper.Editor"


class EditorApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APPLICATION_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.set_inactivity_timeout(20 * 60 * 1000)
        self.windows_by_path = {}
        self.loading = set()
        self.loader = ImageLoader()
        self._prewarm_expiry = None
        self.connect("window-removed", self._window_removed)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="snip-io")

    def _window_removed(self, _application, window):
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
        for name, accelerators in {
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
        path = parameter.get_string()
        if not path:
            return
        self.hold()
        future = self.executor.submit(self.loader.load, path, True)

        def completed():
            try:
                future.result()
            except (OSError, ValueError, cairo.Error):
                pass  # Speculative load: no dialog and no window.
            finally:
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
        self._choose_open()

    def do_open(self, files, _count, _hint):
        for file in files:
            path = file.get_path()
            if path is None:
                self._open_error("Please open a local PNG file.")
            else:
                self.open_path(path)

    def open_path(self, path):
        path = Path(path).expanduser().resolve()
        if path in self.windows_by_path:
            self.windows_by_path[path].present()
            return
        if path in self.loading:
            return
        self.loading.add(path)
        self.hold()

        future = self.executor.submit(self.loader.load, path)

        def completed():
            self.loading.remove(path)
            try:
                base = future.result()
            except ImageTooLarge as error:
                self._open_error(f"Could not open “{path.name}”.\n{error}")
            except (OSError, ValueError, cairo.Error):
                self._open_error(f"Could not read “{path.name}”.\n"
                                 "The file may have moved or may not be a valid PNG image.")
            else:
                from .window import EditorWindow
                window = EditorWindow(self, path, base)
                self.windows_by_path[path] = window
                window.present()
            finally:
                self.release()
        future.add_done_callback(lambda _future: dispatch(completed))

    def _open_error(self, message):
        self.hold()
        dialog = show_error(self.get_active_window(), "Could not open screenshot", message,
                            on_close=self.release)
        dialog.set_application(self)

    def _choose_open(self):
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
                    self.release()
            dialog.open(None, None, chosen)
        else:
            dialog = Gtk.FileChooserNative(title="Open screenshot",
                                           action=Gtk.FileChooserAction.OPEN,
                                           accept_label="Open", cancel_label="Cancel")

            def response(source, response_id):
                if response_id == Gtk.ResponseType.ACCEPT:
                    self.do_open([source.get_file()], 1, "")
                source.destroy()
                self.release()
            dialog.connect("response", response)
            dialog.show()

    def do_shutdown(self):
        if self._prewarm_expiry is not None:
            GLib.source_remove(self._prewarm_expiry)
            self._prewarm_expiry = None
        self.executor.shutdown(wait=False)
        Gtk.Application.do_shutdown(self)
