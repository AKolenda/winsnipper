"""Validated PNG loading with a bounded, short-lived decode cache (no GTK)."""
from concurrent.futures import Future
from pathlib import Path
import stat
import threading
import time

from .render import BaseImage


def image_key(path):
    path = Path(path).expanduser().resolve()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError('The screenshot is missing or is not a regular file.')
    if info.st_size >= 256 * 1024 * 1024:
        raise OSError('The screenshot is too large (the limit is 256 MiB).')
    if path.suffix.lower() != '.png':
        raise OSError('Please select a PNG screenshot.')
    return path, info.st_mtime_ns, info.st_size


class ImageLoader:
    """Share in-flight decodes, consume on open, retain at most two warm images.

    Called only in executor workers. Keys are revalidated before/after decode;
    failed prewarms never retain an exception or produce UI.
    """
    def __init__(self, decoder=BaseImage.from_png, clock=time.monotonic, ttl=120):
        self.decoder, self.clock, self.ttl = decoder, clock, ttl
        self._lock = threading.Lock()
        self._cache = {}
        self._inflight = {}

    def expire(self):
        with self._lock:
            now = self.clock()
            self._cache = {key: value for key, value in self._cache.items() if value[0] > now}

    def load(self, path, prewarm=False):
        key = image_key(path)
        self.expire()
        with self._lock:
            if key in self._cache:
                entry = self._cache[key] if prewarm else self._cache.pop(key)
                return entry[1]
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future
        if not owner:
            base = future.result()
            # A prewarm may finish just as an open starts. Consume its entry.
            if not prewarm:
                with self._lock:
                    self._cache.pop(key, None)
            return base
        try:
            base = self.decoder(key[0])
            if image_key(key[0]) != key:
                raise OSError('The screenshot changed while it was being decoded.')
            with self._lock:
                if prewarm:
                    # Remove stale versions of this file and evict oldest entries.
                    self._cache = {k: v for k, v in self._cache.items() if k[0] != key[0]}
                    while len(self._cache) >= 2:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[key] = (self.clock() + self.ttl, base)
                self._inflight.pop(key, None)
                future.set_result(base)
            return base
        except BaseException as error:
            with self._lock:
                self._inflight.pop(key, None)
                future.set_exception(error)
            raise
