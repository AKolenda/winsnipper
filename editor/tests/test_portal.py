"""Real session-bus integration with a deterministic Screenshot portal double.

No compositor is simulated: these tests verify our protocol and app lifecycle,
not the availability or visual behavior of an actual desktop screenshot picker.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(os.environ.get("WINSNIPPER_GTK_TESTS") == "1", "Needs Xvfb and a session bus")
class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global Gio, GLib, Gtk, PortalCapture, EditorApplication
        from winsnipper.gtk_support import Gio, GLib, Gtk
        from winsnipper.capture import PortalCapture
        from winsnipper.app import EditorApplication
        cls.application = EditorApplication("io.github.winsnipper.PortalTests")
        cls.application.register(None)
        cls.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        cls.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                 "org.freedesktop.DBus", "RequestName",
                                 GLib.Variant("(su)", ("org.freedesktop.portal.Desktop", 0)),
                                 None, Gio.DBusCallFlags.NONE, 5000, None)
        cls.info = Gio.DBusNodeInfo.new_for_xml('''<node>
          <interface name="org.freedesktop.portal.Screenshot">
            <method name="Screenshot"><arg type="s" direction="in"/>
              <arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
            <property name="version" type="u" access="read"/>
            <property name="AvailableTargets" type="u" access="read"/>
          </interface></node>''')
        cls.request_info = Gio.DBusNodeInfo.new_for_xml('''<node>
          <interface name="org.freedesktop.portal.Request"><method name="Close"/>
            <signal name="Response"><arg type="u"/><arg type="a{sv}"/></signal>
          </interface></node>''')

    @classmethod
    def tearDownClass(cls):
        cls.application.executor.shutdown(wait=True)
        cls.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                 "org.freedesktop.DBus", "ReleaseName",
                                 GLib.Variant("(s)", ("org.freedesktop.portal.Desktop",)),
                                 None, Gio.DBusCallFlags.NONE, 5000, None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "portal.png"
        import cairo
        cairo.ImageSurface(cairo.FORMAT_ARGB32, 40, 30).write_to_png(str(self.source))
        self.version = 3
        self.targets = 7
        self.reply = 0
        self.early = True
        self.legacy_handle = False
        self.pending = False
        self.defer_method = False
        self.requests = []
        self.closed = []
        self.registrations = []
        self.outcomes = []
        self.status = []
        self.captures = []
        self.registration = self.connection.register_object(
            "/org/freedesktop/portal/desktop", self.info.interfaces[0], self.handle,
            lambda _c, _s, _p, _i, prop: GLib.Variant("u", self.version if prop == "version" else self.targets), None)

    def tearDown(self):
        self.application.cancel_capture()
        for capture in self.captures:
            capture.cancel()
        self.pump(0.1)
        for window in list(self.application.get_windows()):
            if hasattr(window, "saver"):
                window.saver.shutdown()
            window.destroy()
        if self.registration:
            self.connection.unregister_object(self.registration)
        for registration in self.registrations:
            self.connection.unregister_object(registration)

    def pump(self, seconds):
        end = time.monotonic() + seconds
        context = GLib.MainContext.default()
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.002)

    def spin(self, condition, timeout=6):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.pump(0.01)
        self.assertTrue(condition(), "Timed out waiting for portal/application")

    def handle(self, connection, sender, _path, _interface, _method, parameters, invocation):
        _parent, options = parameters.unpack()
        self.requests.append(options)
        self.sender = sender
        token = "legacy" if self.legacy_handle else options["handle_token"]
        self.handle_path = "/org/freedesktop/portal/desktop/request/" + sender[1:].replace(".", "_") + "/" + token

        def closed(_c, _s, path, _i, _m, _p, call):
            self.closed.append(path)
            call.return_value(None)
        self.registrations.append(connection.register_object(
            self.handle_path, self.request_info.interfaces[0], closed, None, None))
        if self.defer_method:
            self.invocation = invocation
            return
        if self.early and not self.pending:
            self.respond()
        invocation.return_value(GLib.Variant("(o)", (self.handle_path,)))
        if not self.early and not self.pending:
            GLib.idle_add(self.respond)

    def respond(self):
        self.connection.emit_signal(self.sender, self.handle_path,
                                    "org.freedesktop.portal.Request", "Response",
                                    GLib.Variant("(ua{sv})", (self.reply, {"uri": GLib.Variant("s", self.source.as_uri())})))
        return GLib.SOURCE_REMOVE

    def start(self, mode="choose", **kwargs):
        capture = PortalCapture(self.connection, lambda *args: self.outcomes.append(args), self.status.append, **kwargs)
        self.captures.append(capture)
        capture.start(mode)
        return capture

    def test_v3_target_early_signal_and_exactly_once_completion(self):
        self.start("area")
        self.spin(lambda: bool(self.outcomes))
        self.assertEqual(self.outcomes, [(self.source.as_uri(), None, False)])
        self.assertEqual(self.requests[0]["target"], 4)
        self.assertTrue(self.requests[0]["interactive"])
        self.respond()
        self.pump(0.03)
        self.assertEqual(len(self.outcomes), 1)

    def test_legacy_portal_modes_and_unexpected_handle(self):
        self.version = 2
        self.legacy_handle = True
        self.start("window")
        self.spin(lambda: bool(self.outcomes))
        self.assertNotIn("target", self.requests[0])
        self.assertTrue(self.requests[0]["interactive"])
        self.assertIn("system dialog", self.status[-1])
        self.assertEqual(self.outcomes[0][0], self.source.as_uri())

    def test_unadvertised_target_and_v1_do_not_send_unsupported_options(self):
        self.version = 1
        self.targets = 0
        self.start("screen")
        self.spin(lambda: bool(self.outcomes))
        self.assertNotIn("target", self.requests[0])
        self.assertNotIn("interactive", self.requests[0])
        self.assertEqual(self.outcomes[0][0], self.source.as_uri())

    def test_user_cancellation_and_denial(self):
        self.reply = 1
        self.start()
        self.spin(lambda: bool(self.outcomes))
        self.assertEqual(self.outcomes, [(None, None, True)])
        self.outcomes.clear()
        self.reply = 2
        self.start()
        self.spin(lambda: bool(self.outcomes))
        self.assertIsNotNone(self.outcomes[0][1])

    def test_cancel_before_method_reply_closes_late_request(self):
        self.defer_method = True
        capture = self.start()
        self.spin(lambda: bool(self.requests))
        capture.cancel()
        self.invocation.return_value(GLib.Variant("(o)", (self.handle_path,)))
        self.spin(lambda: bool(self.closed))
        self.assertEqual(self.outcomes, [(None, None, True)])

    def test_timeout_closes_pending_request(self):
        self.pending = True
        self.start(timeout_seconds=1)
        self.spin(lambda: bool(self.outcomes), timeout=3)
        self.spin(lambda: bool(self.closed))
        self.assertIn("timed out", self.outcomes[0][1])

    def test_missing_interface_has_actionable_error(self):
        self.connection.unregister_object(self.registration)
        self.start()
        self.spin(lambda: bool(self.outcomes))
        self.assertIn("Screenshot backend", self.outcomes[0][1])
        self.registration = 0

    def test_app_capture_imports_copies_and_edits_private_file(self):
        from winsnipper.gtk_support import Gdk
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set_content(None)
        original = self.source.read_bytes()
        pictures = self.root / "Pictures"
        with patch("winsnipper.app.GLib.get_user_special_dir", return_value=str(pictures)):
            self.application.start_capture("screen")
            self.spin(lambda: bool(self.application.windows_by_path))
        window = next(iter(self.application.windows_by_path.values()))
        self.assertNotEqual(window.path, self.source)
        self.assertEqual(window.path.parent, pictures / "WinSnipper")
        self.assertIsNone(self.application.capture_window)
        self.assertFalse(self.application._capture_busy)
        self.spin(lambda: clipboard.get_formats().contain_gtype(Gdk.Texture))
        from winsnipper.model import Stroke
        window.document.add_stroke(Stroke(((1, 1), (25, 25))))
        window._edited()
        self.spin(lambda: window.saver.saved_revision == 1)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertNotEqual(window.path.read_bytes(), original)

    def test_cli_forwarding_capture_mode_delay_and_invalid_arguments(self):
        editor = str(Path(__file__).resolve().parents[1])
        launcher = (f"import sys; sys.path.insert(0, {editor!r}); "
                    "from winsnipper.app import EditorApplication; "
                    "raise SystemExit(EditorApplication('io.github.winsnipper.PortalTests').run(sys.argv))")
        with patch("winsnipper.app.GLib.get_user_special_dir", return_value=str(self.root / "Pictures")):
            process = subprocess.Popen([sys.executable, "-c", launcher, "--capture", "--mode", "area", "--delay", "0"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            self.spin(lambda: process.poll() is not None)
            self.assertEqual(process.returncode, 0, process.communicate()[1])
            self.spin(lambda: bool(self.application.windows_by_path))
        self.assertEqual(self.requests[0]["target"], 4)
        process = subprocess.Popen([sys.executable, "-c", launcher, "--capture", "--delay", "-1"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.spin(lambda: process.poll() is not None)
        self.assertEqual(process.returncode, 2, process.communicate()[1])

    def test_portal_cancel_restores_editor_and_new_snip_controls(self):
        self.application.open_path(self.source)
        self.spin(lambda: self.source in self.application.windows_by_path)
        editor = self.application.windows_by_path[self.source]
        self.pending = True
        self.application.start_capture()
        self.assertFalse(self.application.capture_window.get_visible())
        self.spin(lambda: bool(self.requests))
        self.assertFalse(editor.get_visible())
        self.application.cancel_capture()
        self.spin(lambda: bool(self.closed))
        self.assertTrue(editor.get_visible())
        self.assertTrue(self.application.capture_window.get_visible())
        self.assertTrue(self.application.capture_window.new_button.get_sensitive())
        self.assertEqual(self.application.get_accels_for_action("app.new-snip"), ["<Control>n"])
        self.assertEqual(self.application.get_accels_for_action("app.open"), ["<Control>o"])

    def test_delay_cancel_and_duplicate_request(self):
        self.application.start_capture("area", 5)
        self.application.start_capture("window", 0)
        self.assertTrue(self.application._capture_busy)
        self.application.cancel_capture()
        self.assertFalse(self.application._capture_busy)
        self.pump(0.3)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.application.capture_window.status.get_label(), "Snip cancelled.")

    def test_bounded_duplicate_prewarm_submissions(self):
        from concurrent.futures import Future
        pending = []
        def submit(*_args):
            future = Future()
            pending.append(future)
            return future
        with patch.object(self.application.executor, "submit", side_effect=submit):
            for _ in range(100):
                self.application._prewarm_path(None, GLib.Variant("s", str(self.source)))
            for index in range(100):
                self.application._prewarm_path(None, GLib.Variant("s", str(self.root / f"{index}.png")))
            self.assertEqual(len(pending), 2)
        for future in pending:
            future.set_result(None)
        self.spin(lambda: not self.application.prewarming)


if __name__ == "__main__":
    unittest.main()
