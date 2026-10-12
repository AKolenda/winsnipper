"""Atomic PNG writes and a main-loop-neutral, coalescing save coordinator."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile

from .render import render


def atomic_save(path, snapshot):
    """Encode completely before replacing the target; failures leave it intact."""
    path = Path(path)
    surface = render(snapshot)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd, temporary = tempfile.mkstemp(prefix=".winsnipper-", suffix=".png.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            surface.write_to_png(output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class SaveCoordinator:
    """All state lives on the UI thread; only the immutable snapshot is submitted.

    schedule(ms, callback), cancel(handle), and dispatch(callback) are injected.
    There is at most one worker in flight, and at most one newest pending state.
    flush callbacks complete only once the newest state is written, or on error.
    """

    def __init__(self, path, schedule, cancel, dispatch, on_saved, on_error,
                 on_status=lambda text: None, executor=None, writer=atomic_save):
        self.path = Path(path)
        self.schedule = schedule
        self.cancel = cancel
        self.dispatch = dispatch
        self.on_saved = on_saved
        self.on_error = on_error
        self.on_status = on_status
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="snip-save")
        self._owns_executor = executor is None
        self.writer = writer
        self._pending = None
        self._running = None
        self._timer = None
        self._waiters = []
        self.saved_revision = 0

    @property
    def busy(self):
        return self._pending is not None or self._running is not None

    def request(self, snapshot):
        self._pending = snapshot
        self.on_status("Saving…")
        self._cancel_timer()
        if self._running is None:
            self._timer = self.schedule(250, self._start)

    def _cancel_timer(self):
        if self._timer is not None:
            self.cancel(self._timer)
            self._timer = None

    def flush(self, callback=lambda error: None):
        self._cancel_timer()
        if not self.busy:
            callback(None)
            return
        self._waiters.append(callback)
        if self._running is None:
            self._start()

    def _start(self):
        self._timer = None
        if self._running is not None or self._pending is None:
            return False
        snapshot, self._pending = self._pending, None
        self._running = snapshot
        future = self.executor.submit(self.writer, self.path, snapshot)
        future.add_done_callback(lambda done: self.dispatch(lambda: self._finished(done)))
        return False

    def _finished(self, future):
        snapshot, self._running = self._running, None
        error = future.exception()
        if error is None:
            self.saved_revision = snapshot.revision
            # Only refresh the clipboard for the newest completed edit.
            if self._pending is None:
                self.on_saved(self.path)
        if self._pending is not None:
            self._start()
            return
        if error is not None:
            # Keep the failed snapshot so closing/copying can retry instead of
            # silently discarding the user's unsaved edit.
            self._pending = snapshot
            self.on_status("Save failed")
            self.on_error(error)
        else:
            self.on_status("Saved")
        waiters, self._waiters = self._waiters, []
        for callback in waiters:
            callback(error)

    def shutdown(self):
        if self.busy:
            raise RuntimeError("Flush pending edits before shutting down")
        self._cancel_timer()
        if self._owns_executor:
            self.executor.shutdown(wait=False)
