"""Optional GTK/DBus integration checks; run with WINSNIPPER_GTK_TESTS=1."""
import os
import builtins
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(os.environ.get("WINSNIPPER_GTK_TESTS") == "1",
                     "Set WINSNIPPER_GTK_TESTS=1 under Xvfb or a desktop to run GTK integration tests")
class GtkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global Gtk, Gdk, Gio, GLib, EditorApplication, EditorWindow, BaseImage, Document, Stroke
        global Snapshot, atomic_save, set_image, cairo
        import cairo
        from winsnipper.gtk_support import Gtk, Gdk, Gio, GLib
        from winsnipper.app import EditorApplication
        from winsnipper.window import EditorWindow
        from winsnipper.render import BaseImage, Snapshot
        from winsnipper.model import Document, Stroke
        from winsnipper.saving import atomic_save
        from winsnipper.clipboard import set_image
        cls.application = EditorApplication()
        cls.application.register(None)
        cls.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        cls.shell_calls = []
        cls.shell_fail = False
        info = Gio.DBusNodeInfo.new_for_xml('''<node><interface name="io.github.winsnipper.Shell">
            <method name="SetClipboardImage"><arg type="s" direction="in" name="path"/></method>
            </interface></node>''')

        def handle(_connection, _sender, _path, _interface, _method, parameters, invocation):
            cls.shell_calls.append(parameters.unpack()[0])
            if cls.shell_fail:
                invocation.return_dbus_error("io.github.winsnipper.Error", "Test clipboard failure")
            else:
                invocation.return_value(None)
        cls.registration = cls.connection.register_object("/io/github/winsnipper/Shell",
                                                           info.interfaces[0], handle, None, None)
        cls.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                 "org.freedesktop.DBus", "RequestName",
                                 GLib.Variant("(su)", ("io.github.winsnipper.Shell", 0)),
                                 None, Gio.DBusCallFlags.NONE, 5000, None)

    @classmethod
    def tearDownClass(cls):
        cls.connection.unregister_object(cls.registration)
        cls.application.executor.shutdown(wait=True)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shot.png"
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 600)
        context = cairo.Context(surface)
        context.set_source_rgb(1, 1, 1)
        context.paint()
        self.base = BaseImage.from_surface(surface)
        atomic_save(self.path, Snapshot(self.base, Document(800, 600).state, 0))
        type(self).shell_calls.clear()
        type(self).shell_fail = False
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.close()
            self.spin(lambda: self.window.get_application() is None)
        for window in list(self.application.get_windows()):
            window.destroy()
        self.spin(lambda: not self.application.loading)
        # Dispatch pending clipboard callbacks before deleting their PNG files.
        self.pump(0.1)

    def pump(self, seconds):
        end = time.monotonic() + seconds
        context = GLib.MainContext.default()
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)

    def spin(self, condition, timeout=10):
        end = time.monotonic() + timeout
        context = GLib.MainContext.default()
        while not condition() and time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)
        self.assertTrue(condition(), "Timed out waiting for GTK/worker completion")

    def open_window(self):
        self.application.open_path(self.path)
        self.spin(lambda: self.path in self.application.windows_by_path)
        self.window = self.application.windows_by_path[self.path]
        self.spin(lambda: self.window.area.get_width() > 100)
        return self.window

    def test_standard_dbus_open_and_cli_forwarding(self):
        responses = []

        def callback(source, result):
            try:
                source.call_finish(result)
            except GLib.Error as error:
                responses.append(error)
            else:
                responses.append(None)
        self.connection.call("io.github.winsnipper.Editor", "/io/github/winsnipper/Editor",
                             "org.freedesktop.Application", "Open",
                             GLib.Variant("(asa{sv})", ([self.path.as_uri()], {})), None,
                             Gio.DBusCallFlags.NONE, 5000, None, callback)
        self.spin(lambda: bool(responses))
        self.assertIsNone(responses[0])
        self.spin(lambda: self.path in self.application.windows_by_path)
        self.window = self.application.windows_by_path[self.path]
        process = subprocess.Popen([sys.executable, str(Path(__file__).parents[1] / "winsnipper_editor.py"),
                                    str(self.path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.spin(lambda: process.poll() is not None, timeout=60)
        _stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        self.pump(0.05)
        self.assertEqual(len(self.application.windows_by_path), 1)

    def test_prewarm_and_shortcuts(self):
        self.application.activate_action("prewarm", None)
        self.assertEqual(self.application.get_windows(), [])
        self.assertEqual(self.application.get_application_id(), "io.github.winsnipper.Editor")
        self.assertEqual(self.application.get_inactivity_timeout(), 1200000)
        self.assertEqual(self.application.get_accels_for_action("win.copy"), ["<Control>c"])
        self.assertEqual(len(self.application.get_accels_for_action("win.redo")), 2)

    def test_window_construct_fit_zoom_and_duplicate_open(self):
        window = self.open_window()
        self.assertTrue(window.fit_mode)
        window.set_zoom(2)
        self.assertEqual(window.zoom, 2)
        window.fit()
        self.assertLessEqual(window.zoom, 1)
        window.select_tool("highlighter")
        self.assertEqual(window.colors["highlighter"], (1, 235 / 255, 59 / 255))
        window._choose_width(4)
        self.assertEqual(window.widths["highlighter"], 48)
        self.application.open_path(self.path)
        self.assertIs(self.application.windows_by_path[self.path], window)
        self.assertEqual(len(self.application.windows_by_path), 1)

    def test_draw_gesture_autosaves_and_refreshes_shell_clipboard(self):
        window = self.open_window()

        class Gesture:
            def set_state(self, state):
                pass
        ox, oy = window._origin()
        window._drag_begin(Gesture(), ox + 20 * window.zoom, oy + 20 * window.zoom)
        window._drag_update(None, 100 * window.zoom, 0)
        window._drag_end(None, 100 * window.zoom, 0)
        self.assertEqual(len(window.document.state.strokes), 1)
        self.assertEqual(window.document.state.strokes[0].points[0], (20, 20))
        self.spin(lambda: window.status.get_label() == "Saved" and len(self.shell_calls) == 1)
        self.assertEqual(self.shell_calls[-1], str(self.path))
        self.assertEqual(window.saver.saved_revision, 1)

    def test_close_flushes_immediately_and_waits_for_worker(self):
        window = self.open_window()
        original = self.path.read_bytes()
        window.document.add_stroke(Stroke(((10, 10), (100, 10))))
        window._edited()
        window.close()
        self.spin(lambda: self.path not in self.application.windows_by_path)
        self.assertNotEqual(self.path.read_bytes(), original)
        self.assertEqual(window.saver.saved_revision, 1)
        self.assertFalse(window.saver.busy)

    def test_close_failure_keeps_edits_open_and_retry_succeeds(self):
        window = self.open_window()
        window.document.add_stroke(Stroke(((10, 10), (100, 10))))
        window._edited()
        with patch("winsnipper.saving.os.replace", side_effect=OSError("disk full")):
            window.close()
            self.spin(lambda: window.status.get_label() == "Save failed")
            self.assertFalse(window._closing)
            self.assertTrue(window.get_child().get_sensitive())
            self.assertTrue(window.saver.busy)
        for dialog in list(self.application.get_windows()):
            if isinstance(dialog, Gtk.MessageDialog):
                dialog.response(Gtk.ResponseType.CLOSE)
        window.close()
        self.spin(lambda: self.path not in self.application.windows_by_path)

    def test_shell_failure_falls_back_to_texture_clipboard(self):
        self.open_window()
        type(self).shell_fail = True
        completed = []
        set_image(self.application, self.path, completed.append)
        self.spin(lambda: bool(completed))
        self.assertIsNone(completed[0])
        formats = Gdk.Display.get_default().get_clipboard().get_formats()
        self.assertTrue(formats.contain_gtype(Gdk.Texture))

    def test_export_png_and_open_corrupt_file_dialog(self):
        window = self.open_window()
        window.document.add_stroke(Stroke(((10, 10), (100, 10))))
        window._edited()
        output = Path(self.directory.name) / "export.png"
        window._export_to(Gio.File.new_for_path(str(output)))
        self.spin(lambda: output.is_file() and window._exports == 0)
        self.assertEqual(BaseImage.from_png(output).width, 800)
        corrupt = Path(self.directory.name) / "corrupt.png"
        corrupt.write_bytes(b"invalid png")
        self.application.open_path(corrupt)
        self.spin(lambda: corrupt not in self.application.loading)
        self.assertNotIn(corrupt, self.application.windows_by_path)
        errors = [w for w in self.application.get_windows() if isinstance(w, Gtk.MessageDialog)]
        self.assertEqual(len(errors), 1)
        errors[0].response(Gtk.ResponseType.CLOSE)

    def record_snapshot(self, window):
        class Recorder:
            def __init__(self):
                self.snapshot = Gtk.Snapshot.new()
                self.cairo_bounds = []
                self.blends = []
                self.textures = []
            def __getattr__(self, name):
                return getattr(self.snapshot, name)
            def append_cairo(self, bounds):
                self.cairo_bounds.append(bounds)
                return self.snapshot.append_cairo(bounds)
            def push_blend(self, blend):
                self.blends.append(blend)
                self.snapshot.push_blend(blend)
            def append_scaled_texture(self, texture, filter, bounds):
                self.textures.append(texture)
                self.snapshot.append_scaled_texture(texture, filter, bounds)
        recorder = Recorder()
        window.area.do_snapshot(recorder)
        self.assertIsNotNone(recorder.snapshot.to_paintable(None))
        return recorder

    def test_texture_reused_during_motion_and_overlay_is_bounded(self):
        window = self.open_window()
        self.pump(.2)
        initial = self.record_snapshot(window)
        self.assertFalse(initial.cairo_bounds)
        builds = window.area.texture_builds
        class Gesture:
            def set_state(self, _state):
                pass
        ox, oy = window._origin()
        window._drag_begin(Gesture(), ox + 20 * window.zoom, oy + 20 * window.zoom)
        window._drag_update(None, 100 * window.zoom, 10 * window.zoom)
        live = self.record_snapshot(window)
        self.assertEqual(window.area.texture_builds, builds)
        self.assertEqual(len(live.cairo_bounds), 1)
        bounds = live.cairo_bounds[0]
        self.assertLess(bounds.get_width(), 130 * window.zoom)
        self.assertLess(bounds.get_height(), 40 * window.zoom)
        self.assertIs(live.textures[0], initial.textures[0])
        window._finish_gesture()
        self.record_snapshot(window)
        self.assertEqual(window.area.texture_builds, builds + 1)

    def test_highlighter_snapshot_blend_crop_scroll_and_input_mapping(self):
        from winsnipper.canvas import Gsk
        window = self.open_window()
        window.set_zoom(2)
        self.pump(.1)
        h, v = window.scroller.get_hadjustment(), window.scroller.get_vadjustment()
        h.set_value(50)
        v.set_value(60)
        ox, oy = window._origin()
        self.assertEqual(window._image_point(ox + 200, oy + 240), (100, 120))
        window.select_tool("highlighter")
        class Gesture:
            def set_state(self, _state):
                pass
        window._drag_begin(Gesture(), ox + 100, oy + 100)
        window._drag_update(None, 120, 80)
        record = self.record_snapshot(window)
        self.assertEqual(record.blends, [Gsk.BlendMode.MULTIPLY])
        b = record.cairo_bounds[0]
        self.assertGreaterEqual(b.get_x(), h.get_value())
        self.assertGreaterEqual(b.get_y(), v.get_value())
        window._drag_cancel()
        window.select_tool("crop")
        window._drag_begin(Gesture(), ox + 100, oy + 100)
        window._drag_update(None, 300, 250)
        record = self.record_snapshot(window)
        self.assertFalse(record.blends)
        self.assertLessEqual(record.cairo_bounds[0].get_width(), h.get_page_size())
        self.assertLessEqual(record.cairo_bounds[0].get_height(), v.get_page_size())
        window._finish_gesture()
        self.pump(.1)
        ox, oy = window._origin()
        crop = window.document.state.crop
        self.assertEqual(window._image_point(ox, oy), (crop.x, crop.y))
        class Scroll:
            def get_current_event_state(self):
                return Gdk.ModifierType.CONTROL_MASK
        previous = window.zoom
        self.assertTrue(window._scroll(Scroll(), 0, -1))
        self.assertGreater(window.zoom, previous)

    def test_fit_resize_debounces_raster_rebuild_without_a_tick_callback(self):
        window = self.open_window()
        self.pump(.25)
        self.record_snapshot(window)
        version = window.cache.version
        initial_scale = window.cache.scale
        h, v = window.scroller.get_hadjustment(), window.scroller.get_vadjustment()
        for factor in (.95, .9, .85):
            h.set_page_size(700 * factor)
            v.set_page_size(450 * factor)
            self.assertIsNotNone(window._resize_timer)
            self.record_snapshot(window)
            self.assertEqual(window.cache.version, version)
            self.assertEqual(window.cache.scale, initial_scale)
        # GTK may restore the real allocation after the synthetic notifications.
        # Restore the last requested fit size at the timeout boundary to test
        # the raster decision independently of those allocation notifications.
        window._cancel_resize()
        window._viewport_size = (595, 382.5)
        window._fit_to_size()
        window._resize_settled()
        self.record_snapshot(window)
        self.assertEqual(window.cache.version, version + 1)
        self.assertFalse(hasattr(window, '_viewport_tick'))

    def test_idle_window_does_not_advance_frame_clock(self):
        window = self.open_window()
        self.pump(.5)
        clock = window.get_frame_clock()
        frame = clock.get_frame_counter()
        self.pump(.2)
        self.assertEqual(clock.get_frame_counter(), frame)

    def test_large_image_has_fit_zoom_before_present_and_texture_channel_order(self):
        large = cairo.ImageSurface(cairo.FORMAT_ARGB32, 3840, 2160)
        window = EditorWindow(self.application, self.path, BaseImage.from_surface(large))
        self.addCleanup(window.destroy)
        self.assertLess(window.zoom, .5)
        self.assertIsNone(window.cache.surface)
        self.assertLess(window.area.content_size[0], 3840)
        # ARGB32 premultiplied texture conversion must preserve red and alpha.
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 2, 2)
        context = cairo.Context(surface)
        context.set_source_rgba(1, 0, 0, .5)
        context.paint()
        window.cache.version += 1
        texture = window.area._texture(surface)
        output = self.path.with_name('texture.png')
        texture.save_to_png(str(output))
        decoded = cairo.ImageSurface.create_from_png(str(output))
        from test_core import pixel
        self.assertEqual(pixel(decoded, 0, 0), (128, 0, 0, 128))

    def test_path_prewarm_decodes_hidden_and_open_consumes_it(self):
        calls = []
        decoder = self.application.loader.decoder
        def decode(path):
            calls.append(path)
            return decoder(path)
        with patch.object(self.application.loader, 'decoder', side_effect=decode):
            self.application.activate_action('prewarm-path', GLib.Variant('s', str(self.path)))
            self.spin(lambda: bool(self.application.loader._cache))
            self.assertEqual(self.application.get_windows(), [])
            self.open_window()
            self.assertEqual(calls, [self.path])
        self.application.activate_action('prewarm-path', GLib.Variant('s', ''))
        bad = self.path.with_name('bad.png')
        bad.write_bytes(b'not a PNG')
        self.application.activate_action('prewarm-path', GLib.Variant('s', str(bad)))
        self.pump(.15)
        self.assertEqual(len(self.application.get_windows()), 1)
        self.assertFalse(self.application.loader._inflight)

    def test_older_gtk_dialog_and_folder_fallbacks(self):
        window = self.open_window()

        def feature_detect(object, name):
            if object is Gtk and name in ("ColorDialog", "FileDialog", "FileLauncher"):
                return False
            return builtins.hasattr(object, name)
        with patch("winsnipper.window.hasattr", feature_detect, create=True):
            window.custom_color()
            self.assertEqual(len(window._dialogs), 1)
            dialog = next(iter(window._dialogs))
            self.assertIsInstance(dialog, Gtk.ColorChooserDialog)
            dialog.response(Gtk.ResponseType.CANCEL)
            window.save_as()
            self.assertEqual(len(window._dialogs), 1)
            dialog = next(iter(window._dialogs))
            self.assertIsInstance(dialog, Gtk.FileChooserNative)
            dialog.emit("response", Gtk.ResponseType.CANCEL)
            with patch.object(Gio.AppInfo, "launch_default_for_uri") as launch:
                window.open_folder()
                launch.assert_called_once_with(self.path.parent.as_uri(), None)

    def test_dbus_activate_action_and_open_contract(self):
        responses = []

        def callback(source, result):
            try:
                source.call_finish(result)
            except GLib.Error as error:
                responses.append(error)
            else:
                responses.append(None)
        self.connection.call("io.github.winsnipper.Editor", "/io/github/winsnipper/Editor",
                             "org.freedesktop.Application", "ActivateAction",
                             GLib.Variant("(sava{sv})", ("prewarm", [], {})), None,
                             Gio.DBusCallFlags.NONE, 5000, None, callback)
        self.spin(lambda: len(responses) == 1)
        self.assertIsNone(responses[0])
        self.assertEqual(self.application.get_windows(), [])
        self.connection.call("io.github.winsnipper.Editor", "/io/github/winsnipper/Editor",
                             "org.freedesktop.Application", "Open",
                             GLib.Variant("(asa{sv})", ([self.path.as_uri()], {})), None,
                             Gio.DBusCallFlags.NONE, 5000, None, callback)
        self.spin(lambda: len(responses) == 2)
        self.assertIsNone(responses[1])
        self.spin(lambda: self.path in self.application.windows_by_path)
        self.window = self.application.windows_by_path[self.path]


if __name__ == "__main__":
    unittest.main()
