"""Portable imports protect both the portal source and private edited copy."""
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from winsnipper.capture_files import import_capture, local_capture_path


class Loader:
    def load(self, path):
        return path.read_bytes()


class CaptureFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pictures = self.root / "Pictures"
        self.source = self.root / "portal image.png"
        self.data = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + struct.pack(">II", 4, 4) + b"fake PNG decoder fixture"
        self.source.write_bytes(self.data)

    def capture(self):
        return import_capture(self.source.as_uri(), self.pictures, Loader())

    def test_import_preserves_source_and_creates_private_unique_files(self):
        first, image = self.capture()
        second, _ = self.capture()
        self.assertNotEqual(first, second)
        self.assertEqual(image, self.data)
        first.write_bytes(b"autosaved")
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(first.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(local_capture_path(self.source.as_uri()), self.source)

    def test_rejects_nonlocal_or_malformed_uri(self):
        for uri in ("https://example.org/image.png", "file://remote/image.png", "file:relative.png",
                    "file:///tmp/image.png?query", "file:///tmp/image%00.png", None):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                local_capture_path(uri)

    def test_source_symlink_and_fifo_are_rejected_without_blocking(self):
        actual = self.source.with_name("actual.png")
        self.source.rename(actual)
        self.source.symlink_to(actual)
        with self.assertRaises(OSError):
            self.capture()
        self.source.unlink()
        os.mkfifo(self.source)
        with self.assertRaises(ValueError):
            self.capture()

    def test_output_symlink_or_shared_directory_are_rejected(self):
        self.pictures.mkdir()
        foreign = self.root / "foreign"
        foreign.mkdir()
        (self.pictures / "WinSnipper").symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(OSError):
            self.capture()
        self.assertEqual(list(foreign.iterdir()), [])
        (self.pictures / "WinSnipper").unlink()
        (self.pictures / "WinSnipper").mkdir(mode=0o755)
        (self.pictures / "WinSnipper").chmod(0o755)
        with self.assertRaises(OSError):
            self.capture()

    def test_invalid_and_oversized_images_leave_no_output(self):
        for data in (b"not a PNG", self.data[:16] + struct.pack(">II", 100000, 100000)):
            self.source.write_bytes(data)
            with self.assertRaises(ValueError):
                self.capture()
            self.assertEqual(list((self.pictures / "WinSnipper").iterdir()), [])

    def test_decoder_error_removes_import_but_not_original(self):
        class BrokenLoader:
            def load(self, path):
                raise ValueError("Invalid PNG structure")
        with self.assertRaises(ValueError):
            import_capture(self.source.as_uri(), self.pictures, BrokenLoader())
        self.assertEqual(list((self.pictures / "WinSnipper").iterdir()), [])
        self.assertEqual(self.source.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
