"""Atomic saves must never follow a replaced temporary pathname."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cairo
from winsnipper.model import Document
from winsnipper.render import BaseImage, Snapshot, render
from winsnipper.saving import atomic_save


class SavingSecurityTests(unittest.TestCase):
    def test_replaced_temporary_symlink_cannot_truncate_unrelated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            destination, victim = directory / 'shot.png', directory / 'unrelated.txt'
            destination.write_bytes(b'original screenshot')
            victim.write_bytes(b'private unrelated data')
            base = BaseImage.from_surface(cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 6))
            snapshot = Snapshot(base, Document(8, 6).state, 0)
            surface = render(snapshot)

            def replace_before_encoding(output):
                temporary, = directory.glob('.winsnipper-*')
                temporary.unlink()
                temporary.symlink_to(victim)
                surface.write_to_png(output)

            caught = None
            with patch('winsnipper.saving.render', return_value=SimpleNamespace(write_to_png=replace_before_encoding)):
                try:
                    atomic_save(destination, snapshot)
                except OSError as error:
                    caught = error
            self.assertEqual(victim.read_bytes(), b'private unrelated data')
            self.assertEqual(destination.read_bytes(), b'original screenshot')
            self.assertIsNotNone(caught, 'A replaced temporary name must abort the save')
            self.assertEqual(list(directory.glob('.winsnipper-*')), [])


if __name__ == '__main__':
    unittest.main()
