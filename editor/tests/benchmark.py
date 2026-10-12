"""Synthetic cairo timings; no GTK/display needed. Not a compositor latency test."""
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cairo
from winsnipper.model import Document, Stroke
from winsnipper.render import BaseImage, Snapshot, ViewCache, draw_stroke
from winsnipper.saving import atomic_save

for width, height in ((3840, 2160), (5120, 2880)):
    image = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(image)
    context.set_source_rgb(1, 1, 1)
    context.paint()
    base = BaseImage.from_surface(image)
    document = Document(width, height)
    snap = Snapshot(base, document.state, 0)
    cache = ViewCache(base)
    start = time.perf_counter()
    cached = cache.get(snap, 0.5)
    cache_ms = (time.perf_counter() - start) * 1000
    target = cairo.ImageSurface(cairo.FORMAT_ARGB32, width // 2, height // 2)
    context = cairo.Context(target)
    points = tuple((50 + i * 2, 300 + 30 * math.sin(i / 20)) for i in range(800))
    timings = {}
    for tool, thickness in (("pen", 4), ("highlighter", 24)):
        stroke = Stroke(points, (1, 1, 0), thickness, tool)
        samples = []
        for _ in range(30):
            start = time.perf_counter()
            context.set_source_surface(cache.get(snap, 0.5), 0, 0)
            context.paint()
            context.save()
            context.scale(0.5, 0.5)
            draw_stroke(context, stroke)
            context.restore()
            samples.append((time.perf_counter() - start) * 1000)
        timings[tool] = statistics.median(samples)
    document.add_stroke(Stroke(points))
    with tempfile.TemporaryDirectory() as directory:
        start = time.perf_counter()
        atomic_save(Path(directory) / "shot.png", Snapshot(base, document.state, document.revision))
        save_ms = (time.perf_counter() - start) * 1000
    print(f"{width}x{height}: build cache {cache_ms:.1f} ms; "
          f"cached frame + live 800-point pen {timings['pen']:.1f} ms / "
          f"highlighter {timings['highlighter']:.1f} ms; atomic PNG {save_ms:.1f} ms")
