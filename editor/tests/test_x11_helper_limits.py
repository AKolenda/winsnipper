"""Actual X11 helper subprocesses must preserve inherited file-size limits."""
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "winsnipper/x11_helper.py"
WRAPPER = """import resource, runpy, sys
resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[2]), int(sys.argv[3])))
runpy.run_path(sys.argv[1], run_name="__main__")
"""


@unittest.skipUnless(os.environ.get("WINSNIPPER_GTK_TESTS") == "1"
                     and os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
                     and os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland",
                     "Needs GTK3 and a native X11 display (run under Xvfb)")
class X11HelperLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        available = subprocess.run([sys.executable, "-c", "import gi; gi.require_version('Gdk', '3.0')"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if available.returncode:
            raise unittest.SkipTest("Optional GTK3 bindings are not installed")

    def capture(self, soft, hard):
        # A wrapper applies limits only in the owned child. stdout is a regular
        # file, matching the application and exercising actual RLIMIT_FSIZE.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([sys.executable, "-c", WRAPPER, str(HELPER), str(soft), str(hard)],
                                    stdout=output, stderr=subprocess.PIPE, timeout=10)
            output.seek(0)
            return result, output.read()

    def test_lower_inherited_hard_limit_allows_small_capture(self):
        result, png = self.capture(1024 * 1024, 1024 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(png), 1024 * 1024)

    def test_lower_inherited_soft_limit_fails_before_partial_output(self):
        result, png = self.capture(64, resource.RLIM_INFINITY)
        self.assertEqual(result.returncode, 1, result.stderr.decode())
        self.assertIn(b"file-size limit", result.stderr)
        self.assertNotIn(b"needs GTK3", result.stderr)
        self.assertEqual(png, b"")


if __name__ == "__main__":
    unittest.main()
