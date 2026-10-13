"""GTK 4 annotation window. All points and edit operations use image pixels."""
from dataclasses import replace
import math
from pathlib import Path

from .canvas import Canvas
from .clipboard import set_image
from .gtk_support import Gdk, Gio, GLib, Gtk, dispatch, schedule, show_error
from .model import Document, Stroke
from .render import (LiveHighlighter, Snapshot, ViewCache, flattened_points,
                     hit_stroke, stroke_bounds)
from .saving import SaveCoordinator, atomic_save

PALETTE = (("Black", "#000000"), ("White", "#ffffff"), ("Red", "#e53935"),
           ("Yellow", "#ffeb3b"), ("Green", "#43a047"), ("Blue", "#1e88e5"),
           ("Purple", "#8e24aa"), ("Orange", "#fb8c00"))


def rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))


class EditorWindow(Gtk.ApplicationWindow):
    def __init__(self, application, path, base):
        super().__init__(application=application, title=path.name)
        self.path = Path(path)
        self.base = base
        self.document = Document(base.width, base.height)
        self.cache = ViewCache(base)
        self.tool = "pen"
        self.colors = {"pen": rgb("#e53935"), "highlighter": rgb("#ffeb3b")}
        self.widths = {"pen": 4.0, "highlighter": 24.0}
        self.zoom = 1.0
        self.fit_mode = True
        self._viewport_size = None
        self._resize_timer = None
        self._live_highlighter = None
        self._drag_start = None
        self._drag_tool = None
        self._points = []
        self._removed = set()
        self._erase_geometry = ()
        self._crop_end = None
        self._closing = False
        self._exports = 0
        self._copy_pending = False
        self._dialogs = set()
        self._actions = {}
        self._build_actions()
        self._build_ui()
        self.saver = SaveCoordinator(self.path, schedule, GLib.source_remove, dispatch,
                                     self._saved, self._save_error, self.status.set_label,
                                     executor=application.executor)
        self.connect("close-request", self._close_requested)
        self.connect("destroy", lambda *_: self._cancel_resize())
        self._size_to_image()
        for adjustment in (self.scroller.get_hadjustment(), self.scroller.get_vadjustment()):
            adjustment.connect("notify::page-size", self._viewport_changed)
        self._update_actions()

    def snapshot(self):
        return Snapshot(self.base, self.document.state, self.document.revision)

    def _build_actions(self):
        callbacks = {
            "undo": self.undo, "redo": self.redo, "copy": self.copy,
            "save-as": self.save_as, "close": self.close,
            "fit": self.fit, "actual-size": lambda: self.set_zoom(1.0),
            "folder": self.open_folder,
        }
        for tool in ("pen", "highlighter", "eraser", "crop"):
            callbacks[tool] = lambda selected=tool: self.select_tool(selected)
        for name, callback in callbacks.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _parameter, call=callback: call())
            self.add_action(action)
            self._actions[name] = action

    def _button(self, icon, tooltip, action):
        button = Gtk.Button.new_from_icon_name(icon)
        button.set_tooltip_text(tooltip)
        button.set_action_name("win." + action)
        return button

    def _build_ui(self):
        self.header = Gtk.HeaderBar()
        self.header.set_title_widget(Gtk.Label(label=""))
        self.set_titlebar(self.header)
        tools = Gtk.Box(spacing=2)
        self.tool_buttons = {}
        first = None
        for name, label, shortcut in (("pen", "Pen", "P"), ("highlighter", "Highlighter", "H"),
                                      ("eraser", "Eraser", "E"), ("crop", "Crop", "X")):
            button = Gtk.ToggleButton(label=label)
            button.set_tooltip_text(f"{label} ({shortcut})")
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", self._tool_toggled, name)
            self.tool_buttons[name] = button
            tools.append(button)
        self.tool_buttons["pen"].set_active(True)
        self.header.pack_start(tools)

        self.color_preview = Gtk.DrawingArea(content_width=20, content_height=20)
        self.color_preview.set_draw_func(self._draw_color)
        self.color_button = Gtk.MenuButton(child=self.color_preview, tooltip_text="Colour")
        palette_popover = Gtk.Popover()
        palette_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(palette_box, "set_margin_" + side)(10)
        grid = Gtk.Grid(row_spacing=4, column_spacing=4)
        for i, (name, value) in enumerate(PALETTE):
            swatch = Gtk.DrawingArea(content_width=24, content_height=24)
            swatch.set_draw_func(lambda _area, context, _width, _height, color=rgb(value):
                                 self._paint_swatch(context, color))
            button = Gtk.Button(child=swatch, tooltip_text=name)
            button.connect("clicked", lambda _button, color=rgb(value): self._choose_color(color))
            grid.attach(button, i % 4, i // 4, 1, 1)
        palette_box.append(grid)
        custom = Gtk.Button(label="Custom colour…")
        custom.connect("clicked", lambda *_: self.custom_color())
        palette_box.append(custom)
        palette_popover.set_child(palette_box)
        self.color_button.set_popover(palette_popover)
        self.header.pack_start(self.color_button)

        self.thickness_button = Gtk.MenuButton(label="4 px", tooltip_text="Stroke thickness")
        thickness_popover = Gtk.Popover()
        sizes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for label, index in (("Fine", 0), ("Small", 1), ("Medium", 2), ("Wide", 3), ("Extra wide", 4)):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _button, choice=index: self._choose_width(choice))
            sizes.append(button)
        thickness_popover.set_child(sizes)
        self.thickness_button.set_popover(thickness_popover)
        self.header.pack_start(self.thickness_button)

        commands = Gtk.Box(spacing=2)
        for icon, tooltip, action in (
                ("edit-undo-symbolic", "Undo (Ctrl+Z)", "undo"),
                ("edit-redo-symbolic", "Redo (Ctrl+Shift+Z / Ctrl+Y)", "redo"),
                ("edit-copy-symbolic", "Copy (Ctrl+C)", "copy"),
                ("document-save-as-symbolic", "Save As… (Ctrl+S)", "save-as"),
                ("folder-open-symbolic", "Open containing folder", "folder")):
            commands.append(self._button(icon, tooltip, action))
        self.header.pack_end(commands)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(content)
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.area = Canvas(self)
        self.area.set_cursor_from_name("crosshair")
        self.scroller.set_child(self.area)
        capture_bar = Gtk.Box(spacing=8)
        capture_bar.set_margin_start(12)
        capture_bar.set_margin_end(12)
        capture_bar.set_margin_top(6)
        capture_bar.set_margin_bottom(6)
        capture_bar.append(Gtk.Button(label="New snip", action_name="app.new-snip",
                                      tooltip_text="New snip (Ctrl+N)"))
        capture_bar.append(Gtk.Button(label="Open image…", action_name="app.open",
                                      tooltip_text="Open image (Ctrl+O)"))
        content.append(capture_bar)
        content.append(self.scroller)
        footer = Gtk.Box(spacing=12)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        footer.set_margin_top(5)
        footer.set_margin_bottom(5)
        self.dimensions = Gtk.Label(xalign=0, hexpand=True)
        footer.append(self.dimensions)
        self.status = Gtk.Label(label="Saved", xalign=1)
        footer.append(self.status)
        content.append(footer)

        self.drag = Gtk.GestureDrag.new()
        self.drag.set_button(Gdk.BUTTON_PRIMARY)
        self.drag.connect("drag-begin", self._drag_begin)
        self.drag.connect("drag-update", self._drag_update)
        self.drag.connect("drag-end", self._drag_end)
        self.drag.connect("cancel", self._drag_cancel)
        self.area.add_controller(self.drag)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._scroll)
        self.scroller.add_controller(scroll)
        keys = Gtk.EventControllerKey.new()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)

    def _size_to_image(self):
        display = self.get_display()
        monitors = display.get_monitors()
        monitor = monitors.get_item(0) if monitors.get_n_items() else None
        width, height = 1280, 900
        if monitor is not None:
            geometry = monitor.get_geometry()
            width, height = geometry.width, geometry.height
        default_width = min(int(width * 0.85), max(760, self.base.width + 32))
        default_height = min(int(height * 0.85), max(420, self.base.height + 154))
        self.set_default_size(default_width, default_height)
        # Account for the header/footer before the first allocation/present.
        self._viewport_size = (default_width, max(1, default_height - 154))
        self._fit_to_size()

    def _viewport_changed(self, *_args):
        size = (self.scroller.get_hadjustment().get_page_size(),
                self.scroller.get_vadjustment().get_page_size())
        if size != self._viewport_size and min(size) > 1:
            self._viewport_size = size
            if self.fit_mode:
                if self._resize_timer is not None:
                    GLib.source_remove(self._resize_timer)
                self._resize_timer = schedule(150, self._resize_settled)
                self._fit_to_size()

    def _resize_settled(self):
        self._resize_timer = None
        self.area.queue_draw()

    def _cancel_resize(self):
        if self._resize_timer is not None:
            GLib.source_remove(self._resize_timer)
            self._resize_timer = None

    def _fit_to_size(self):
        crop = self.document.state.crop
        width, height = self._viewport_size or (760, 420)
        self.zoom = min(1.0, max(0.01, (width - 32) / crop.width),
                        max(0.01, (height - 32) / crop.height))
        self._resize_canvas()

    def _resize_canvas(self):
        crop = self.document.state.crop
        self.area.set_content_size(math.ceil(crop.width * self.zoom) + 32,
                                   math.ceil(crop.height * self.zoom) + 32)
        self.dimensions.set_label(f"{crop.width} × {crop.height}  ·  {self.zoom * 100:.0f}%")
        self.area.queue_draw()

    def fit(self):
        self._cancel_resize()
        self.fit_mode = True
        self._fit_to_size()

    def set_zoom(self, zoom):
        self._cancel_resize()
        self.fit_mode = False
        self.zoom = max(0.05, min(8.0, zoom))
        self._resize_canvas()

    def _scroll(self, controller, _dx, dy):
        if controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            self.set_zoom(self.zoom * math.exp(-dy * 0.15))
            return True
        return False

    def _origin(self, width=None, height=None):
        crop = self.document.state.crop
        width = self.area.get_width() if width is None else width
        height = self.area.get_height() if height is None else height
        return ((width - crop.width * self.zoom) / 2,
                (height - crop.height * self.zoom) / 2)

    def _image_point(self, x, y, clamp=True):
        ox, oy = self._origin()
        crop = self.document.state.crop
        point = (crop.x + (x - ox) / self.zoom, crop.y + (y - oy) / self.zoom)
        if clamp:
            return (max(crop.x, min(crop.right, point[0])),
                    max(crop.y, min(crop.bottom, point[1])))
        return point if crop.contains(point) else None

    def _ensure_highlighter(self, scale):
        live = self._live_highlighter
        if live is None or live.scale != scale or live.crop != self.document.state.crop:
            live = LiveHighlighter(self.document.state.crop, scale,
                                   self.colors["highlighter"], self.widths["highlighter"])
            for point in self._points:
                live.add(point)
            self._live_highlighter = live
        else:
            for point in self._points[len(live.points):]:
                live.add(point)
        return live

    def _draw_crop(self, context, crop, rect):
        context.save()
        context.set_source_rgba(0, 0, 0, 0.42)
        for x, y, w, h in ((crop.x, crop.y, crop.width, rect.y - crop.y),
                           (crop.x, rect.bottom, crop.width, crop.bottom - rect.bottom),
                           (crop.x, rect.y, rect.x - crop.x, rect.height),
                           (rect.right, rect.y, crop.right - rect.right, rect.height)):
            context.rectangle(x, y, w, h)
        context.fill()
        context.set_source_rgb(1, 1, 1)
        context.set_line_width(1.5 / self.zoom)
        context.rectangle(rect.x, rect.y, rect.width, rect.height)
        context.stroke()
        context.set_font_size(13 / self.zoom)
        context.move_to(rect.x + 4 / self.zoom, rect.y + 17 / self.zoom)
        context.show_text(f"{rect.width} × {rect.height}")
        context.restore()

    def _drag_begin(self, gesture, x, y):
        if self._closing:
            return
        point = self._image_point(x, y, clamp=False)
        if point is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self.area.grab_focus()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._drag_start = (x, y)
        self._drag_tool = self.tool
        self._points = [point]
        self._crop_end = point
        self._removed = set()
        if self.tool == "eraser":
            self._erase_geometry = tuple((points, stroke_bounds(s, points))
                                         for s in self.document.state.strokes
                                         for points in (flattened_points(s),))
            self._erase_at(point, point)
        self._update_actions()
        self.area.queue_draw()

    def _drag_update(self, _gesture, dx, dy):
        if self._drag_start is None:
            return
        point = self._image_point(self._drag_start[0] + dx, self._drag_start[1] + dy)
        if self._drag_tool == "crop":
            self._crop_end = point
        elif self._drag_tool == "eraser":
            self._erase_at(self._points[-1], point)
            self._points = [point]
        elif math.dist(point, self._points[-1]) >= 0.35 / max(self.zoom, 0.2):
            self._points.append(point)
        self.area.queue_draw()

    def _erase_at(self, start, end):
        for index, stroke in enumerate(self.document.state.strokes):
            if index not in self._removed and hit_stroke(
                    stroke, start, end, radius=6 / self.zoom,
                    crop=self.document.state.crop, flattened=self._erase_geometry[index][0],
                    bounds=self._erase_geometry[index][1]):
                self._removed.add(index)

    def _drag_end(self, gesture, dx, dy):
        if self._drag_start is None:
            return
        self._drag_update(gesture, dx, dy)
        self._finish_gesture()

    def _finish_gesture(self):
        if self._drag_start is None:
            return
        before = self.document.state
        changed = False
        if self._drag_tool in ("pen", "highlighter"):
            stroke = Stroke(tuple(self._points), self.colors[self._drag_tool],
                            self.widths[self._drag_tool], self._drag_tool)
            changed = self.document.add_stroke(stroke)
            if changed:
                self.cache.append(before, self.document.state, stroke)
        elif self._drag_tool == "eraser":
            changed = self.document.erase_indices(self._removed)
            self.cache.invalidate()
        elif self._drag_tool == "crop":
            rect = self.document.state.crop.selection(self._points[0], self._crop_end)
            if rect is not None and rect.width * self.zoom >= 4 and rect.height * self.zoom >= 4:
                changed = self.document.crop(rect)
                self.cache.invalidate()
        self._reset_drag()
        if changed:
            self._edited()
        else:
            self._update_actions()
            self.area.queue_draw()

    def _reset_drag(self):
        self._live_highlighter = None
        self._drag_start = None
        self._drag_tool = None
        self._points = []
        self._crop_end = None
        self._removed = set()
        self._erase_geometry = ()

    def _drag_cancel(self, *_args):
        self._reset_drag()
        self.cache.invalidate()
        self._update_actions()
        self.area.queue_draw()

    def _key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape and self._drag_start is not None:
            self.drag.reset()
            self._drag_cancel()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self._drag_tool == "crop":
            self._finish_gesture()
            self.drag.reset()
            return True
        return False

    def _edited(self):
        if self.fit_mode:
            self._fit_to_size()
        else:
            self._resize_canvas()
        self._update_actions()
        self.saver.request(self.snapshot())

    def _update_actions(self):
        active = self._drag_start is None and not self._closing
        self._actions["undo"].set_enabled(active and self.document.can_undo)
        self._actions["redo"].set_enabled(active and self.document.can_redo)
        for name, action in self._actions.items():
            if name not in ("undo", "redo", "close"):
                action.set_enabled(active)
        if hasattr(self, "color_button"):
            drawing = self.tool in ("pen", "highlighter") and active
            self.color_button.set_sensitive(drawing)
            self.thickness_button.set_sensitive(drawing)
            for button in self.tool_buttons.values():
                button.set_sensitive(active)

    def undo(self):
        if self._drag_start is None and self.document.undo():
            self.cache.invalidate()
            self._edited()

    def redo(self):
        if self._drag_start is None and self.document.redo():
            self.cache.invalidate()
            self._edited()

    def _tool_toggled(self, button, tool):
        if button.get_active():
            self.select_tool(tool)

    def select_tool(self, tool):
        if self._drag_start is not None or self._closing:
            return
        self.tool = tool
        if not self.tool_buttons[tool].get_active():
            self.tool_buttons[tool].set_active(True)
        if hasattr(self, "color_preview"):
            self.color_preview.queue_draw()
            self.thickness_button.set_label(f"{self.widths.get(tool, 4):g} px")
            self._update_actions()

    @staticmethod
    def _paint_swatch(context, color):
        context.set_source_rgb(*color)
        context.arc(12, 12, 9, 0, math.tau)
        context.fill_preserve()
        context.set_source_rgba(0.5, 0.5, 0.5, 0.6)
        context.set_line_width(1)
        context.stroke()

    def _draw_color(self, _area, context, _width, _height):
        context.translate(-2, -2)
        self._paint_swatch(context, self.colors.get(self.tool, self.colors["pen"]))

    def _choose_color(self, color):
        if self.tool in self.colors:
            self.colors[self.tool] = color
        self.color_preview.queue_draw()
        self.color_button.popdown()

    def _choose_width(self, index):
        sizes = (2, 4, 6, 10, 16) if self.tool == "pen" else (12, 18, 24, 36, 48)
        if self.tool in self.widths:
            self.widths[self.tool] = sizes[index]
        self.thickness_button.set_label(f"{sizes[index]} px")
        self.thickness_button.popdown()

    def custom_color(self):
        self.color_button.popdown()
        color = Gdk.RGBA()
        color.red, color.green, color.blue = self.colors.get(self.tool, self.colors["pen"])
        color.alpha = 1
        if hasattr(Gtk, "ColorDialog"):
            dialog = Gtk.ColorDialog(title="Choose annotation colour", with_alpha=False)

            def chosen(source, result):
                try:
                    value = source.choose_rgba_finish(result)
                except GLib.Error:
                    return
                self._choose_color((value.red, value.green, value.blue))
            dialog.choose_rgba(self, color, None, chosen)
        else:
            dialog = Gtk.ColorChooserDialog(title="Choose annotation colour", transient_for=self,
                                           modal=True, use_alpha=False)
            dialog.set_rgba(color)
            self._dialogs.add(dialog)

            def response(source, response_id):
                if response_id == Gtk.ResponseType.OK:
                    value = source.get_rgba()
                    self._choose_color((value.red, value.green, value.blue))
                self._dialogs.discard(source)
                source.destroy()
            dialog.connect("response", response)
            dialog.present()

    def _saved(self, path):
        if not self._copy_pending:
            self._clipboard(path)

    def _clipboard(self, path):
        def finished(error):
            if error is not None and not self._closing:
                show_error(self, "Could not copy screenshot", error)
        set_image(self.get_application(), path, finished)

    def _save_error(self, error):
        show_error(self, "Could not save screenshot",
                   f"Your edits are still open. Check that the folder is writable and has free space.\n{error}")

    def copy(self):
        if self._copy_pending:
            return
        self._copy_pending = True

        def flushed(error):
            self._copy_pending = False
            if error is None:
                self._clipboard(self.path)
        self.saver.flush(flushed)

    def save_as(self):
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog(title="Save screenshot as PNG",
                                    initial_name=self.path.stem + " (edited).png")
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.path.parent)))
            filters = Gio.ListStore.new(Gtk.FileFilter)
            png = Gtk.FileFilter()
            png.set_name("PNG image")
            png.add_mime_type("image/png")
            filters.append(png)
            dialog.set_filters(filters)

            def chosen(source, result):
                try:
                    file = source.save_finish(result)
                except GLib.Error:
                    return
                self._export_to(file)
            dialog.save(self, None, chosen)
        else:
            dialog = Gtk.FileChooserNative(title="Save screenshot as PNG", transient_for=self,
                                           action=Gtk.FileChooserAction.SAVE,
                                           accept_label="Save", cancel_label="Cancel")
            dialog.set_current_name(self.path.stem + " (edited).png")
            dialog.set_current_folder(Gio.File.new_for_path(str(self.path.parent)))
            self._dialogs.add(dialog)

            def response(source, response_id):
                if response_id == Gtk.ResponseType.ACCEPT:
                    self._export_to(source.get_file())
                self._dialogs.discard(source)
                source.destroy()
            dialog.connect("response", response)
            dialog.show()

    def _export_to(self, file):
        path = file.get_path()
        if path is None:
            show_error(self, "Could not save screenshot", "Please choose a local folder.")
            return
        path = Path(path).resolve()
        if path.suffix.lower() != ".png":
            path = path.with_name(path.name + ".png")
        if path == self.path:
            self.saver.flush()
            return
        application = self.get_application()
        application.hold()
        self._exports += 1
        future = application.executor.submit(atomic_save, path, self.snapshot())

        def completed():
            self._exports -= 1
            error = future.exception()
            if error is not None:
                show_error(self, "Could not save screenshot", error)
            application.release()
            if self._closing and self._exports == 0:
                self._flush_and_close()
        future.add_done_callback(lambda _future: dispatch(completed))

    def open_folder(self):
        directory = Gio.File.new_for_path(str(self.path.parent))
        if hasattr(Gtk, "FileLauncher"):
            launcher = Gtk.FileLauncher.new(directory)

            def launched(source, result):
                try:
                    source.launch_finish(result)
                except GLib.Error as error:
                    show_error(self, "Could not open folder", error)
            launcher.launch(self, None, launched)
        else:
            try:
                Gio.AppInfo.launch_default_for_uri(directory.get_uri(), None)
            except GLib.Error as error:
                show_error(self, "Could not open folder", error)

    def _close_requested(self, *_args):
        if self._closing:
            return True
        # Finish an in-progress mark, rather than losing it on Ctrl+W.
        self._finish_gesture()
        self.drag.reset()
        self._closing = True
        self.get_application().hold()
        self.get_child().set_sensitive(False)
        self.header.set_sensitive(False)
        self._update_actions()
        for dialog in tuple(self._dialogs):
            dialog.destroy()
        self._dialogs.clear()
        if self._exports == 0:
            self._flush_and_close()
        return True

    def _flush_and_close(self):
        application = self.get_application()

        def flushed(error):
            if error is not None:
                self._closing = False
                self.get_child().set_sensitive(True)
                self.header.set_sensitive(True)
                self._update_actions()
            else:
                self.saver.shutdown()
                self.destroy()
            application.release()
        self.saver.flush(flushed)
