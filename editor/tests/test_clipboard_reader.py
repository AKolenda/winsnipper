"""The isolated Shell reader must reject special files without waiting for peers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

READER = Path(__file__).resolve().parents[2] / 'extension/lib/read-clipboard.py'
PNG = b'\x89PNG\r\n\x1a\n' + b'clipboard test payload'


class ClipboardReaderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'shot.png'
        self.path.write_bytes(PNG)

    def read(self):
        return subprocess.run([sys.executable, '-I', '-S', str(READER), str(self.path)],
                              capture_output=True, timeout=5)

    def test_read_only_png_is_copied_byte_for_byte_without_modification(self):
        self.path.chmod(0o400)
        result = self.read()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, PNG)
        self.assertEqual(self.path.read_bytes(), PNG)

    def test_regular_path_replaced_by_fifo_is_rejected_without_writer(self):
        self.assertTrue(self.path.is_file())
        self.path.unlink()
        os.mkfifo(self.path)
        result = self.read()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'regular file', result.stderr)

    def test_symlink_invalid_signature_and_oversized_file_are_rejected(self):
        target = self.path.with_name('target.png')
        self.path.rename(target)
        self.path.symlink_to(target)
        result = self.read()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.path.unlink()
        self.path.write_bytes(b'not a PNG')
        result = self.read()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        with self.path.open('wb') as output:
            output.write(PNG)
            output.truncate(256 * 1024 * 1024)
        result = self.read()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'256 MiB', result.stderr)
        self.assertEqual(result.stdout, b'')

    def test_reader_blocked_by_backpressure_is_killable(self):
        with self.path.open('wb') as output:
            output.write(PNG)
            output.truncate(64 * 1024 * 1024)
        with subprocess.Popen([sys.executable, '-I', '-S', str(READER), str(self.path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                self.assertEqual(process.stdout.read(8), PNG[:8])
                self.assertIsNone(process.poll())
            finally:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
