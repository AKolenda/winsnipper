"""New-snip controls shared by standalone launches and the annotation editor."""
from .gtk_support import Gio, Gtk

MODE_IDS = ("choose", "area", "window", "screen")
DELAYS = (0, 3, 5, 10)


class CaptureWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="WinSnipper")
        self.set_default_size(470, 300)
        self.set_resizable(False)
        self.set_titlebar(Gtk.HeaderBar())
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, "set_margin_" + side)(24)
        self.set_child(content)
        title = Gtk.Label(label="Take a new screenshot", xalign=0)
        title.add_css_class("title-2")
        content.append(title)
        from .capture_x11 import native_x11_available
        self.mode_ids = list(MODE_IDS)
        labels = ["System dialog", "Rectangle", "Window", "Full screen"]
        if native_x11_available():
            self.mode_ids.append("x11-screen")
            labels.append("X11 full screen (no picker)")
        self.mode = Gtk.DropDown.new_from_strings(labels)
        self.mode.set_tooltip_text("Capture mode; older portals choose the mode in their system dialog")
        self.delay = Gtk.DropDown.new_from_strings(["No delay", "3 seconds", "5 seconds", "10 seconds"])
        self.delay.set_tooltip_text("Wait before asking the desktop to capture")
        options = Gtk.Box(spacing=10)
        options.append(Gtk.Label(label="Mode"))
        options.append(self.mode)
        options.append(Gtk.Label(label="Delay"))
        options.append(self.delay)
        content.append(options)
        self.status = Gtk.Label(
            label="Your desktop provides the capture picker and permission dialog. "
                  "Available modes vary by desktop.", wrap=True, xalign=0)
        self.mode.connect("notify::selected", self._mode_changed)
        self.status.set_max_width_chars(52)
        self.status.set_vexpand(True)
        content.append(self.status)
        commands = Gtk.Box(spacing=8)
        self.new_button = Gtk.Button(label="New snip")
        self.new_button.add_css_class("suggested-action")
        self.new_button.connect("clicked", lambda *_: application.start_capture(
            self.mode_ids[self.mode.get_selected()], DELAYS[self.delay.get_selected()]))
        self.open_button = Gtk.Button(label="Open image…", action_name="app.open")
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", lambda *_: application.cancel_capture())
        self.cancel_button.set_visible(False)
        commands.append(self.new_button)
        commands.append(self.open_button)
        commands.append(self.cancel_button)
        content.append(commands)
        self.connect("close-request", self._close_requested)
        close = Gio.SimpleAction.new("close", None)
        close.connect("activate", lambda *_: self.close())
        self.add_action(close)

    def _mode_changed(self, *_args):
        if self.mode_ids[self.mode.get_selected()] == "x11-screen":
            self.status.set_label("Capture the whole native X11 screen directly, without a picker. "
                                  "Use Crop in the editor for a rectangle. GTK3 bindings are required.")
        else:
            self.status.set_label("Your desktop provides the capture picker and permission dialog. "
                                  "Available modes vary by desktop.")

    def set_busy(self, busy, choosing=False):
        available = not busy and not choosing
        self.mode.set_sensitive(available)
        self.delay.set_sensitive(available)
        self.new_button.set_sensitive(available)
        self.open_button.set_sensitive(available)
        self.cancel_button.set_visible(busy)

    def _close_requested(self, *_args):
        self.get_application().cancel_capture()
        return False
