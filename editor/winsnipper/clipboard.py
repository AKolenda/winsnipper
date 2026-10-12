"""Prefer the Shell clipboard owner so copied PNGs outlive this process."""
from .gtk_support import Gdk, Gio, GLib, GObject

BUS_NAME = "io.github.winsnipper.Shell"
OBJECT_PATH = "/io/github/winsnipper/Shell"
INTERFACE = "io.github.winsnipper.Shell"


def set_image(application, path, callback=lambda error: None):
    application.hold()
    path = str(path)

    def finish(error=None):
        try:
            callback(error)
        finally:
            application.release()

    def fallback():
        try:
            display = Gdk.Display.get_default()
            if display is None:
                raise RuntimeError("No display is available for the clipboard")
            clipboard = display.get_clipboard()
            texture = Gdk.Texture.new_from_filename(path)
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
        except (GLib.Error, OSError, RuntimeError) as error:
            finish(error)
        else:
            finish()

    def called(connection, result):
        try:
            connection.call_finish(result)
        except GLib.Error:
            fallback()
        else:
            finish()

    def connected(_source, result):
        try:
            connection = Gio.bus_get_finish(result)
        except GLib.Error:
            fallback()
            return
        connection.call(BUS_NAME, OBJECT_PATH, INTERFACE, "SetClipboardImage",
                        GLib.Variant("(s)", (path,)), None, Gio.DBusCallFlags.NONE,
                        5000, None, called)

    Gio.bus_get(Gio.BusType.SESSION, None, connected)
