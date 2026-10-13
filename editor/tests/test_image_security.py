"""PNG allocation and warm-cache regressions at the real decoder boundary."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cairo
from winsnipper.loading import ImageLoader
from winsnipper.render import BaseImage, read_png


def png_header(width, height):
    ihdr = b'IHDR' + struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + ihdr + struct.pack('>I', zlib.crc32(ihdr))


class ImageSecurityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'shot.png'

    def test_oversized_dimensions_rejected_before_native_decoder(self):
        for width, height in ((8192, 8192), (2**31 - 1, 1), (1, 2**31 - 1)):
            with self.subTest(width=width, height=height):
                self.path.write_bytes(png_header(width, height))
                with patch('winsnipper.render.cairo.ImageSurface') as native:
                    native.create_from_png.side_effect = AssertionError('unsafe native decode')
                    with self.assertRaisesRegex(OSError, 'dimensions|pixels'):
                        ImageLoader().load(self.path)
                    native.create_from_png.assert_not_called()

    def test_direct_decoder_rejects_large_encoded_file(self):
        with self.path.open('wb') as output:
            output.write(png_header(1, 1))
            output.truncate(256 * 1024 * 1024)
        with self.assertRaisesRegex(OSError, '256 MiB'):
            BaseImage.from_png(self.path)

    def test_standard_8k_header_fits_decoded_budget(self):
        self.path.write_bytes(png_header(7680, 4320))
        self.assertEqual(read_png(self.path), png_header(7680, 4320))

    def test_fifo_rejected_without_waiting_for_writer(self):
        os.mkfifo(self.path)
        result = subprocess.run([
            sys.executable, '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); '
            'from winsnipper.render import BaseImage; BaseImage.from_png(sys.argv[2])',
            str(Path(__file__).resolve().parents[1]), str(self.path),
        ], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a regular file', result.stderr)

    def test_regular_png_and_symlink_decode_correct_pixels(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 80, 60)
        context = cairo.Context(surface)
        context.set_source_rgba(.25, .5, .75, .5)
        context.paint()
        surface.write_to_png(str(self.path))
        alias = self.path.with_name('alias.png')
        alias.symlink_to(self.path)
        base = BaseImage.from_png(alias)
        self.assertEqual((base.width, base.height), (80, 60))
        self.assertEqual(base.pixels, BaseImage.from_surface(surface).pixels)

    def test_atomic_replacement_with_preserved_mtime_invalidates_prewarm(self):
        self.path.write_bytes(b'old screenshot')
        loader = ImageLoader(lambda path: path.read_bytes())
        self.assertEqual(loader.load(self.path, True), b'old screenshot')
        original = self.path.stat()
        replacement = self.path.with_name('replacement.png')
        replacement.write_bytes(b'new screenshot')
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, self.path)
        self.assertEqual(loader.load(self.path), b'new screenshot')


if __name__ == '__main__':
    unittest.main()
