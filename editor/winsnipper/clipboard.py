"""Prefer the Shell clipboard owner so copied PNGs outlive this process."""
from .gtk_support import Gdk, Gio, GLib, GObject, dispatch
from .render import read_png

BUS_NAME = "io.github.winsnipper.Shell"
OBJECT_PATH = "/io/github/winsnipper/Shell"
INTERFACE = "io.github.winsnipper.Shell"


def set_image(application, path, callback=lambda error: None):
    application.hold()
    path = str(path)
    generation = getattr(application, '_clipboard_generation', 0) + 1
    application._clipboard_generation = generation

    def current():
        return application._clipboard_generation == generation

    def finish(error=None):
        try:
            callback(error)
        finally:
            application.release()

    def decoded(future):
        if not current():
            finish()
            return
        try:
            texture = future.result()
            display = Gdk.Display.get_default()
            if display is None:
                raise RuntimeError("No display is available for the clipboard")
            clipboard = display.get_clipboard()
            if hasattr(clipboard, "set_texture"):
                clipboard.set_texture(texture)
            else:
                # gdk_clipboard_set_texture is marked introspectable=false.
                # set() is the binding of set_value(). Use the public base type
                # rather than the concrete MemoryTexture subclass, so PNG
                # serializers and read_texture_async recognize the content.
                value = GObject.Value()
                value.init(Gdk.Texture)
                value.set_object(texture)
                clipboard.set(value)
        except (GLib.Error, OSError, RuntimeError, ValueError) as error:
            finish(error)
        else:
            finish()

    def load_texture():
        # GdkTexture loaders are thread-safe. Keep disk IO and PNG decoding off
        # GTK's main loop, and apply the same bounds as editor image loading.
        if current():
            return Gdk.Texture.new_from_bytes(GLib.Bytes.new(read_png(path)))
        return None

    def fallback():
        if not current():
            finish()
            return
        try:
            future = application.executor.submit(load_texture)
        except RuntimeError as error:
            finish(error)
        else:
            future.add_done_callback(lambda done: dispatch(lambda: decoded(done)))

    def called(connection, result):
        try:
            connection.call_finish(result)
        except GLib.Error as error:
            if Gio.DBusError.get_remote_error(error) == f'{INTERFACE}.Cancelled':
                # A new snip may have taken the clipboard while this read was
                # pending. Falling back here would restore the old image.
                finish()
            else:
                fallback()
        else:
            finish()

    def connected(_source, result):
        try:
            connection = Gio.bus_get_finish(result)
        except GLib.Error:
            fallback()
            return
        if not current():
            finish()
            return
        connection.call(BUS_NAME, OBJECT_PATH, INTERFACE, "SetClipboardImage",
                        GLib.Variant("(s)", (path,)), None, Gio.DBusCallFlags.NONE,
                        5000, None, called)

    Gio.bus_get(Gio.BusType.SESSION, None, connected)
