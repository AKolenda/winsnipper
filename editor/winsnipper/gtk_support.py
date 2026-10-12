"""GTK imports and small asynchronous main-loop helpers."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
# Required for DrawingArea's cairo.Context marshalling.
gi.require_foreign("cairo")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk  # noqa: E402


def dispatch(callback):
    def run():
        callback()
        return GLib.SOURCE_REMOVE
    return GLib.idle_add(run)


def schedule(milliseconds, callback):
    def run():
        callback()
        return GLib.SOURCE_REMOVE
    return GLib.timeout_add(milliseconds, run)


def show_error(parent, title, message, on_close=None):
    # MessageDialog works on GTK 4.0–4.14; no dependency on Adwaita or newer AlertDialog.
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True,
                               message_type=Gtk.MessageType.ERROR,
                               buttons=Gtk.ButtonsType.CLOSE, text=title,
                               secondary_text=str(message))
    if parent is not None:
        dialog.set_application(parent.get_application())

    def response(widget, _response):
        widget.destroy()
        if on_close is not None:
            on_close()
    dialog.connect("response", response)
    dialog.present()
    return dialog
