"""Decode cache validation, expiry, consumption, and in-flight sharing."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cairo
from winsnipper.loading import ImageLoader, image_key
from winsnipper.render import BaseImage, MAX_IMAGE_PIXELS


def png_header(width, height):
    data = b'IHDR' + struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + data + struct.pack('>I', zlib.crc32(data))


class PngLoadingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'shot.png'

    def test_small_png_decodes_through_open_and_prewarm(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 3, 2)
        context = cairo.Context(surface)
        context.set_source_rgb(1, 0, 0)
        context.paint()
        surface.write_to_png(str(self.path))
        for prewarm in (False, True):
            with self.subTest(prewarm=prewarm):
                base = ImageLoader().load(self.path, prewarm)
                self.assertEqual((base.width, base.height), (3, 2))
                self.assertEqual(base.pixels, bytes(surface.get_data()))

    def test_pixel_budget_boundary_and_8k_reach_decoder_without_large_allocations(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        self.assertEqual(8192 * 4096, MAX_IMAGE_PIXELS)
        for width, height in ((1, 1), (8192, 4096), (7680, 4320)):
            with self.subTest(width=width, height=height):
                header = png_header(width, height)
                self.path.write_bytes(header)
                with patch('winsnipper.render.cairo.ImageSurface') as surface_class:
                    def decode(stream):
                        self.assertEqual(stream.read(8) + stream.read(25), header)
                        return surface
                    surface_class.create_from_png.side_effect = decode
                    BaseImage.from_png(self.path)
                    surface_class.create_from_png.assert_called_once()

    def test_oversized_png_rejected_before_decode_for_open_and_prewarm(self):
        for width, height in ((8193, 4096), (8192, 8192)):
            self.path.write_bytes(png_header(width, height))
            for prewarm in (False, True):
                with self.subTest(width=width, height=height, prewarm=prewarm):
                    loader = ImageLoader()
                    with patch('winsnipper.render.cairo.ImageSurface') as surface_class:
                        with self.assertRaisesRegex(OSError, 'pixel limit'):
                            loader.load(self.path, prewarm)
                        surface_class.create_from_png.assert_not_called()
                    self.assertFalse(loader._cache)
                    self.assertFalse(loader._inflight)

    def test_invalid_headers_rejected_before_decode(self):
        header = png_header(2, 2)
        invalid = [header[:length] for length in (0, 8, 16, 24, 32)]
        invalid += [b'not PNG!' + header[8:], header[:8] + struct.pack('>I', 14) + header[12:],
                    header[:12] + b'IDAT' + header[16:], header[:-1] + bytes([header[-1] ^ 1]),
                    png_header(0, 1), png_header(1, 0)]
        for data in invalid:
            with self.subTest(header=data):
                self.path.write_bytes(data)
                with patch('winsnipper.render.cairo.ImageSurface') as surface_class:
                    with self.assertRaises(OSError):
                        BaseImage.from_png(self.path)
                    surface_class.create_from_png.assert_not_called()

    def test_valid_header_with_truncated_png_is_rejected_by_decoder(self):
        self.path.write_bytes(png_header(2, 2))
        with self.assertRaises(cairo.Error):
            BaseImage.from_png(self.path)

    def test_decode_uses_opened_file_when_path_is_replaced(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 3, 2)
        surface.write_to_png(str(self.path))
        replacement = self.path.with_name('replacement.png')
        replacement.write_bytes(png_header(8192, 8192))
        decode = cairo.ImageSurface.create_from_png
        with patch('winsnipper.render.cairo.ImageSurface') as surface_class:
            def replace_then_decode(stream):
                os.replace(replacement, self.path)
                return decode(stream)
            surface_class.create_from_png.side_effect = replace_then_decode
            base = BaseImage.from_png(self.path)
        self.assertEqual((base.width, base.height), (3, 2))

    def test_checked_header_is_replayed_if_file_header_changes(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 3, 2)
        surface.write_to_png(str(self.path))
        decode = cairo.ImageSurface.create_from_png
        # Disable file buffering so a seek-and-reread would see the changed IHDR.
        with self.path.open('rb', buffering=0) as source, \
                patch('winsnipper.render.open', return_value=source), \
                patch('winsnipper.render.cairo.ImageSurface') as surface_class:
            def mutate_then_decode(stream):
                with self.path.open('r+b') as changed:
                    changed.write(png_header(8192, 8192))
                return decode(stream)
            surface_class.create_from_png.side_effect = mutate_then_decode
            base = BaseImage.from_png(self.path)
        self.assertEqual((base.width, base.height), (3, 2))


class LoadingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'shot.png'
        self.path.write_bytes(b'PNG placeholder')
        self.calls = []
        self.now = 0

        def decode(path):
            self.calls.append(path)
            return object()
        self.loader = ImageLoader(decode, lambda: self.now)

    def test_warm_cache_consumed_once_and_key_changes_decode_again(self):
        base = self.loader.load(self.path, True)
        self.assertIs(self.loader.load(self.path), base)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNot(self.loader.load(self.path), base)
        base = self.loader.load(self.path, True)
        # Same length, changed nanosecond timestamp.
        old = self.path.stat()
        os.utime(self.path, ns=(old.st_atime_ns, old.st_mtime_ns + 100))
        self.assertIsNot(self.loader.load(self.path), base)
        base = self.loader.load(self.path, True)
        self.path.write_bytes(b'PNG another size')
        self.assertIsNot(self.loader.load(self.path), base)

    def test_expiry_and_two_entry_memory_bound(self):
        base = self.loader.load(self.path, True)
        self.now = 121
        self.loader.expire()
        self.assertEqual(self.loader._cache, {})
        self.assertIsNot(self.loader.load(self.path), base)
        for index in range(3):
            path = self.path.with_name(f'{index}.png')
            path.write_bytes(b'PNG')
            self.loader.load(path, True)
        self.assertEqual(len(self.loader._cache), 2)

    def test_validation_is_shared_by_open_and_prewarm(self):
        invalid = [self.path.parent, self.path.with_name('missing.png')]
        wrong_suffix = self.path.with_suffix('.txt')
        wrong_suffix.write_bytes(b'PNG')
        invalid.append(wrong_suffix)
        large = self.path.with_name('large.png')
        with large.open('wb') as stream:
            stream.truncate(256 * 1024 * 1024)
        invalid.append(large)
        for path in invalid:
            for prewarm in (False, True):
                with self.subTest(path=path, prewarm=prewarm), self.assertRaises(OSError):
                    self.loader.load(path, prewarm)
        self.assertFalse(self.calls)
        link = self.path.with_name('alias.png')
        link.symlink_to(self.path)
        self.assertEqual(image_key(link), image_key(self.path))

    def test_open_shares_inflight_prewarm_without_redecoding(self):
        entered, finish = threading.Event(), threading.Event()
        base = object()

        def decode(_path):
            self.calls.append(_path)
            entered.set()
            self.assertTrue(finish.wait(5))
            return base
        self.loader.decoder = decode
        with ThreadPoolExecutor(max_workers=2) as executor:
            warm = executor.submit(self.loader.load, self.path, True)
            self.assertTrue(entered.wait(5))
            opened = executor.submit(self.loader.load, self.path)
            finish.set()
            self.assertIs(warm.result(5), base)
            self.assertIs(opened.result(5), base)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.loader._cache)

    def test_changed_during_decode_and_failed_decode_never_cache(self):
        def changed(path):
            path.write_bytes(b'changed content with another size')
            return object()
        self.loader.decoder = changed
        with self.assertRaisesRegex(OSError, 'changed'):
            self.loader.load(self.path, True)
        self.assertFalse(self.loader._cache)
        self.assertFalse(self.loader._inflight)
        def failed(_path):
            raise OSError('invalid PNG')
        self.loader.decoder = failed
        for _ in range(2):
            with self.assertRaises(OSError):
                self.loader.load(self.path, True)
        self.assertFalse(self.loader._cache)
        self.assertFalse(self.loader._inflight)


if __name__ == '__main__':
    unittest.main()
