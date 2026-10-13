"""Clipboard fallbacks must not decode on GTK's thread or restore stale images."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from winsnipper import clipboard
    from winsnipper.gtk_support import GLib
except (ImportError, ValueError):
    clipboard = None


@unittest.skipIf(clipboard is None, 'GTK bindings are required; no display is needed')
class ClipboardRuntimeTests(unittest.TestCase):
    def setUp(self):
        import cairo
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.paths = [Path(self.directory.name) / f'{name}.png' for name in ('old', 'new')]
        self.labels = {}
        for path, channel in zip(self.paths, (0, 1)):
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 2, 2)
            context = cairo.Context(surface)
            context.set_source_rgb(channel, 0, 0)
            context.paint()
            surface.write_to_png(str(path))
            self.labels[path.read_bytes()] = path.stem
        self.holds = 0
        self.content = []
        self.decoded_threads = []
        self.callback_threads = []
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.app = SimpleNamespace(executor=self.executor, hold=self.hold, release=self.release)
        target = SimpleNamespace(set_texture=self.content.append)
        display = SimpleNamespace(get_clipboard=lambda: target)
        self.gdk = SimpleNamespace(
            Display=SimpleNamespace(get_default=lambda: display),
            Texture=SimpleNamespace(new_from_bytes=self.decode_bytes,
                                    new_from_filename=lambda path: self.decode_bytes(Path(path).read_bytes())))
        self.addCleanup(patch.stopall)
        patch.object(clipboard, 'Gdk', self.gdk).start()
        patch.object(clipboard.Gio, 'bus_get', side_effect=lambda _type, _cancel, callback: callback(None, None)).start()
        patch.object(clipboard.Gio, 'bus_get_finish', side_effect=GLib.Error('No Shell service')).start()

    def hold(self):
        self.holds += 1

    def release(self):
        self.holds -= 1

    def decode_bytes(self, encoded):
        self.decoded_threads.append(threading.get_ident())
        return self.labels[encoded if isinstance(encoded, bytes) else encoded.get_data()]

    def completed(self, error):
        self.assertIsNone(error)
        self.callback_threads.append(threading.get_ident())

    def spin(self, condition):
        deadline = time.monotonic() + 5
        context = GLib.MainContext.default()
        while not condition() and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(.001)
        self.assertTrue(condition(), 'Clipboard work did not complete')

    def test_fallback_decode_uses_worker_and_completion_returns_to_main_loop(self):
        owner = threading.get_ident()
        clipboard.set_image(self.app, self.paths[0], self.completed)
        self.spin(lambda: self.holds == 0)
        self.assertEqual(self.content, ['old'])
        self.assertEqual(self.callback_threads, [owner])
        self.assertEqual(len(self.decoded_threads), 1)
        self.assertNotEqual(self.decoded_threads[0], owner)

    def test_slow_older_fallback_cannot_replace_newer_clipboard(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.gdk.Texture.new_from_bytes

        def decode(encoded):
            label = original(encoded)
            if label == 'old':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Test did not release older decode')
            return label
        self.gdk.Texture.new_from_bytes = decode
        clipboard.set_image(self.app, self.paths[0], self.completed)
        self.assertTrue(entered.wait(2), 'Fallback did not submit a background decode')
        clipboard.set_image(self.app, self.paths[1], self.completed)
        self.spin(lambda: self.content == ['new'])
        release.set()
        self.spin(lambda: self.holds == 0)
        self.assertEqual(self.content, ['new'])
        self.assertEqual(len(self.callback_threads), 2)

    def test_superseded_shell_request_does_not_fallback_over_new_capture(self):
        cancellation = clipboard.Gio.DBusError.new_for_dbus_error(
            'io.github.winsnipper.Shell.Cancelled', 'New screenshot copied')
        connection = SimpleNamespace()
        connection.call = lambda *args: args[-1](connection, None)

        def call_finish(_result):
            raise cancellation
        connection.call_finish = call_finish
        clipboard.Gio.bus_get_finish.side_effect = None
        clipboard.Gio.bus_get_finish.return_value = connection
        clipboard.set_image(self.app, self.paths[0], self.completed)
        self.spin(lambda: self.holds == 0)
        self.assertEqual(self.decoded_threads, [])
        self.assertEqual(self.content, [])
        self.assertEqual(len(self.callback_threads), 1)

    def test_decode_value_error_releases_application_hold_and_reports_error(self):
        errors = []
        with patch.object(clipboard, 'read_png', side_effect=ValueError('Invalid screenshot path')):
            clipboard.set_image(self.app, self.paths[0], errors.append)
            self.spin(lambda: self.holds == 0)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)


if __name__ == '__main__':
    unittest.main()
