"""Decode cache validation, expiry, consumption, and in-flight sharing."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from winsnipper.loading import ImageLoader, image_key


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
