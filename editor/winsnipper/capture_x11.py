"""Explicit full-screen capture for native X11 desktops without a portal."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from .capture_files import import_capture
from .gtk_support import Gdk, dispatch


def native_x11_available():
    display = Gdk.Display.get_default()
    return bool(display is not None and display.__gtype__.name == "GdkX11Display"
                and os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
                and os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland")


def helper_command():
    return [sys.executable, str(Path(__file__).with_name("x11_helper.py"))]


class CaptureCancelled(Exception):
    pass


class X11Capture:
    """Run GTK3 in a worker-owned child, then import through the same safe path.

    Completion is (path, image, error, cancelled) on the GTK main loop. Cancel
    kills only this owned helper; a temporary directory is always cleaned by
    the worker after the child exits, even for failure, timeout or cancellation.
    """
    def __init__(self, executor, loader, pictures, completed, timeout_seconds=20):
        self.executor, self.loader, self.pictures = executor, loader, pictures
        self.completed = completed
        self.timeout_seconds = timeout_seconds
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None

    def start(self):
        if not native_x11_available():
            dispatch(lambda: self.completed(None, None,
                "X11 full-screen capture requires a native X11 session. Use the system dialog on Wayland.", False))
            return
        future = self.executor.submit(self._capture)
        future.add_done_callback(lambda _future: dispatch(lambda: self._deliver(future)))

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.kill()

    def _capture(self):
        if self._cancelled.is_set():
            raise CaptureCancelled()
        with tempfile.TemporaryDirectory(prefix="winsnipper-x11-") as directory:
            source = Path(directory) / "capture.png"
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as output:
                environment = os.environ.copy()
                environment["GDK_BACKEND"] = "x11"
                process = subprocess.Popen(helper_command(), stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.PIPE, env=environment)
                with self._lock:
                    self._process = process
                    if self._cancelled.is_set():
                        process.kill()
                try:
                    try:
                        _stdout, stderr = process.communicate(timeout=self.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                        raise OSError("X11 capture timed out. Try New snip again.")
                finally:
                    with self._lock:
                        self._process = None
                if self._cancelled.is_set():
                    raise CaptureCancelled()
                if process.returncode:
                    detail = stderr.decode("utf-8", errors="replace").strip()[:2000]
                    raise OSError(detail or "The X11 screenshot helper failed.")
            result = import_capture(source.as_uri(), self.pictures, self.loader)
            if self._cancelled.is_set():
                result[0].unlink()
                raise CaptureCancelled()
            return result

    def _deliver(self, future):
        try:
            path, image = future.result()
        except CaptureCancelled:
            self.completed(None, None, None, True)
        except Exception as error:
            self.completed(None, None, str(error), False)
        else:
            if self._cancelled.is_set():
                # Cancellation arrived after the worker completed; the private
                # screenshot remains saved but is not opened unexpectedly.
                self.completed(None, None, None, True)
            else:
                self.completed(path, image, None, False)
