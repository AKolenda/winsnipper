"""D-Bus-activatable GTK application, image loading, and resident lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cairo

from .gtk_support import Gio, GLib, Gtk, dispatch, show_error
from .render import BaseImage

APPLICATION_ID = "io.github.winsnipper.Editor"


class EditorApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APPLICATION_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.set_inactivity_timeout(20 * 60 * 1000)
        self.windows_by_path = {}
        self.loading = set()
        self._open_filter = None
        self.connect("window-removed", self._window_removed)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="snip-io")

    def _window_removed(self, _application, window):
        for path, existing in tuple(self.windows_by_path.items()):
            if existing is window:
                del self.windows_by_path[path]

    def do_dbus_register(self, connection, object_path):
        registered = Gtk.Application.do_dbus_register(self, connection, object_path)
        if registered:
            self._open_filter = connection.add_filter(self._compat_open, object_path)
        return registered

    def do_dbus_unregister(self, connection, object_path):
        if self._open_filter is not None:
            connection.remove_filter(self._open_filter)
            self._open_filter = None
        Gtk.Application.do_dbus_unregister(self, connection, object_path)

    def _compat_open(self, connection, message, incoming, object_path):
        # GApplication's standard freedesktop Open takes (uris, platform_data).
        # SPEC.md and the Shell client use (uris, hint, platform_data), normally
        # the org.gtk.Application signature. Accept that exact contract too,
        # without replacing GApplication's normal exported interfaces/actions.
        if (incoming and message.get_message_type() == Gio.DBusMessageType.METHOD_CALL
                and message.get_path() == object_path
                and message.get_interface() == "org.freedesktop.Application"
                and message.get_member() == "Open"
                and message.get_signature() == "assa{sv}"):
            uris, hint, _platform_data = message.get_body().unpack()

            def opened():
                self.do_open([Gio.File.new_for_uri(uri) for uri in uris], len(uris), hint)
                if not message.get_flags() & Gio.DBusMessageFlags.NO_REPLY_EXPECTED:
                    try:
                        connection.send_message(message.new_method_reply(), Gio.DBusSendMessageFlags.NONE)
                    except GLib.Error:
                        pass  # caller disconnected
            # Filters run on GDBus's worker thread; GTK/GApplication calls run
            # only after dispatching back to the main context.
            dispatch(opened)
            return None
        return message

    def do_startup(self):
        Gtk.Application.do_startup(self)
        action = Gio.SimpleAction.new("prewarm", None)
        action.connect("activate", self._prewarm)
        self.add_action(action)
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

        def load():
            if not path.is_file():
                raise OSError("The screenshot is missing or is not a regular file.")
            if path.stat().st_size >= 256 * 1024 * 1024:
                raise OSError("The screenshot is too large (the limit is 256 MiB).")
            if path.suffix.lower() != ".png":
                raise OSError("Please select a PNG screenshot.")
            return BaseImage.from_png(path)

        future = self.executor.submit(load)

        def completed():
            self.loading.remove(path)
            try:
                base = future.result()
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
        self.executor.shutdown(wait=False)
        Gtk.Application.do_shutdown(self)
