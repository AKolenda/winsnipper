"""Native X11 capture tests: actual Xvfb pixels plus child lifecycle checks."""
import ctypes
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(os.environ.get("WINSNIPPER_GTK_TESTS") == "1", "Needs GTK and an isolated display")
class X11CaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global Gdk, GLib, EditorApplication, X11Capture, native_x11_available
        from winsnipper.gtk_support import Gdk, GLib
        from winsnipper.app import EditorApplication
        from winsnipper.capture_x11 import X11Capture, native_x11_available
        cls.application = EditorApplication("io.github.winsnipper.X11Tests")
        cls.application.register(None)

    @classmethod
    def tearDownClass(cls):
        cls.application.executor.shutdown(wait=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.results = []
        self.captures = []

    def tearDown(self):
        for capture in self.captures:
            capture.cancel()
        self.application.cancel_capture()
        for window in list(self.application.get_windows()):
            if hasattr(window, "saver"):
                window.saver.shutdown()
            window.destroy()

    def pump(self, seconds):
        end = time.monotonic() + seconds
        context = GLib.MainContext.default()
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.002)

    def spin(self, condition, timeout=10):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.pump(0.01)
        self.assertTrue(condition(), "Timed out waiting for X11 capture")

    def start(self, **kwargs):
        capture = X11Capture(self.application.executor, self.application.loader, self.root / "Pictures",
                             lambda *args: self.results.append(args), **kwargs)
        self.captures.append(capture)
        capture.start()
        return capture

    def require_x11(self):
        if not native_x11_available():
            self.skipTest("Pixel/child tests require native X11 (run under Xvfb)")
        import subprocess
        available = subprocess.run([sys.executable, "-c", "import gi; gi.require_version('Gdk', '3.0')"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if available.returncode:
            self.skipTest("Optional GTK3 bindings are not installed")

    def paint_root(self):
        xlib = ctypes.CDLL("libX11.so.6")
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XSetWindowBackground.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        xlib.XClearWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        xlib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = xlib.XOpenDisplay(None)
        self.assertTrue(display)
        try:
            root = xlib.XDefaultRootWindow(display)
            xlib.XSetWindowBackground(display, root, 0x2357AB)
            xlib.XClearWindow(display, root)
            xlib.XSync(display, 0)
        finally:
            xlib.XCloseDisplay(display)

    def test_actual_xvfb_pixels_and_private_output(self):
        self.require_x11()
        self.paint_root()
        with patch("winsnipper.capture_x11.tempfile.tempdir", str(self.root)):
            self.start()
            self.spin(lambda: bool(self.results))
        path, image, error, cancelled = self.results[0]
        self.assertIsNone(error)
        self.assertFalse(cancelled)
        self.assertGreater(image.width, 100)
        self.assertGreater(image.height, 100)
        for x, y in ((0, 0), (image.width // 2, image.height // 2), (image.width - 1, image.height - 1)):
            pixel = struct.unpack_from("=I", image.pixels, y * image.stride + x * 4)[0]
            self.assertEqual(pixel & 0xFFFFFF, 0x2357AB)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.root.glob("winsnipper-x11-*")), [])

    def test_app_x11_capture_opens_editor_and_crops(self):
        self.require_x11()
        self.paint_root()
        with patch("winsnipper.app.GLib.get_user_special_dir", return_value=str(self.root / "Pictures")):
            self.application.start_capture("x11-screen")
            self.spin(lambda: bool(self.application.windows_by_path))
        window = next(iter(self.application.windows_by_path.values()))
        self.assertIsNone(self.application.capture_window)
        self.assertFalse(self.application._capture_busy)
        self.assertEqual(window.path.parent, self.root / "Pictures" / "WinSnipper")
        from winsnipper.model import Rect
        from winsnipper.render import BaseImage
        window.document.crop(Rect(10, 10, 100, 80))
        window._edited()
        self.spin(lambda: window.saver.saved_revision == 1)
        saved = BaseImage.from_png(window.path)
        self.assertEqual((saved.width, saved.height), (100, 80))
        window.close()
        self.spin(lambda: not self.application.windows_by_path)

    def test_cancel_kills_owned_helper_and_cleans_private_temp(self):
        self.require_x11()
        with patch("winsnipper.capture_x11.helper_command", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]), \
                patch("winsnipper.capture_x11.tempfile.tempdir", str(self.root)):
            capture = self.start()
            self.spin(lambda: capture._process is not None)
            process = capture._process
            capture.cancel()
            self.spin(lambda: bool(self.results))
        self.assertEqual(self.results, [(None, None, None, True)])
        self.assertIsNotNone(process.poll())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_timeout_kills_helper_and_cleans_private_temp(self):
        self.require_x11()
        with patch("winsnipper.capture_x11.helper_command", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]), \
                patch("winsnipper.capture_x11.tempfile.tempdir", str(self.root)):
            self.start(timeout_seconds=0.05)
            self.spin(lambda: bool(self.results))
        self.assertIn("timed out", self.results[0][2])
        self.assertFalse(self.results[0][3])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_helper_failure_restores_app_controls(self):
        self.require_x11()
        with patch("winsnipper.capture_x11.helper_command", return_value=[sys.executable, "-c", "raise SystemExit(1)"]), \
                patch("winsnipper.app.GLib.get_user_special_dir", return_value=str(self.root / "Pictures")):
            self.application.start_capture("x11-screen")
            self.spin(lambda: not self.application._capture_busy)
        self.assertTrue(self.application.capture_window.get_visible())
        self.assertIn("helper failed", self.application.capture_window.status.get_label())
        self.assertEqual(self.application.windows_by_path, {})

    def test_wayland_environment_refuses_even_with_an_x11_display(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-test"}):
            self.assertFalse(native_x11_available())
            self.start()
            self.spin(lambda: bool(self.results))
        self.assertIn("native X11", self.results[0][2])
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}):
            self.assertFalse(native_x11_available())

    def test_controls_offer_fallback_only_on_native_x11(self):
        window = self.application.show_capture()
        self.assertEqual("x11-screen" in window.mode_ids, native_x11_available())
        if native_x11_available():
            window.mode.set_selected(window.mode_ids.index("x11-screen"))
            self.assertIn("without a picker", window.status.get_label())


class X11HelperGuardTests(unittest.TestCase):
    def test_helper_refuses_wayland_before_loading_gtk3(self):
        from winsnipper.x11_helper import ensure_native_x11
        with patch.dict(os.environ, {"DISPLAY": ":123", "WAYLAND_DISPLAY": "wayland-test"}), \
                patch("winsnipper.x11_helper.ctypes.CDLL") as load:
            with self.assertRaisesRegex(RuntimeError, "native X11"):
                ensure_native_x11()
            load.assert_not_called()

    def test_helper_refuses_xwayland_extension_with_missing_session_environment(self):
        from winsnipper.x11_helper import ensure_native_x11
        with patch.dict(os.environ, {"DISPLAY": ":123", "WAYLAND_DISPLAY": "", "XDG_SESSION_TYPE": ""}), \
                patch("winsnipper.x11_helper.ctypes.CDLL") as load:
            library = load.return_value
            library.XOpenDisplay.return_value = 1
            library.XQueryExtension.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "Xwayland capture is disabled"):
                ensure_native_x11()
            library.XCloseDisplay.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
