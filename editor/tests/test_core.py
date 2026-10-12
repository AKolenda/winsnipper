"""Run from the repository root: python3 -m unittest discover -s editor/tests."""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
import os
import random
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cairo
from winsnipper.model import Document, Rect, Stroke
from winsnipper.render import (BaseImage, LiveHighlighter, Snapshot, ViewCache,
                               flattened_points, hit_stroke, render, stroke_bounds)
from winsnipper.saving import SaveCoordinator, atomic_save


def solid_base(width=80, height=60, color=(1, 1, 1)):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    context.set_source_rgb(*color)
    context.paint()
    return BaseImage.from_surface(surface)


def pixel(surface, x, y):
    surface.flush()
    offset = y * surface.get_stride() + x * 4
    value = int.from_bytes(bytes(surface.get_data()[offset:offset + 4]), sys.byteorder)
    return ((value >> 16) & 255, (value >> 8) & 255, value & 255, value >> 24)


def snapshot(document, base=None):
    return Snapshot(base or solid_base(), document.state, document.revision)


class ModelTests(unittest.TestCase):
    def test_undo_redo_and_branch(self):
        document = Document(80, 60)
        original = document.state
        first = Stroke(((10, 10), (40, 10)))
        document.add_stroke(first)
        drawn = document.state
        document.crop(Rect(5, 5, 30, 30))
        cropped = document.state
        self.assertTrue(document.undo())
        self.assertEqual(document.state, drawn)
        self.assertTrue(document.undo())
        self.assertEqual(document.state, original)
        self.assertTrue(document.redo())
        self.assertTrue(document.redo())
        self.assertEqual(document.state, cropped)
        self.assertEqual(document.state.strokes[0].points, first.points)
        document.undo()
        document.add_stroke(Stroke(((5, 5),)))
        self.assertFalse(document.can_redo)
        self.assertEqual(document.revision, 8)

    def test_eraser_is_one_undoable_operation(self):
        document = Document(80, 60)
        first = Stroke(((10, 10), (40, 10)))
        second = Stroke(((10, 30), (40, 30)))
        document.add_stroke(first)
        document.add_stroke(second)
        document.erase_indices({0, 1, 100})
        self.assertEqual(document.state.strokes, ())
        document.undo()
        self.assertEqual(document.state.strokes, (first, second))
        self.assertFalse(document.erase_indices(set()))

    def test_crop_selection_and_invalid_crops(self):
        document = Document(80, 60)
        viewport = Rect(10, 10, 50, 40)
        document.crop(viewport)
        self.assertEqual(viewport.selection((58.4, 48.1), (5.5, 9.2)), Rect(10, 10, 49, 39))
        self.assertEqual(viewport.selection((68.4, 58.1), (5.5, 9.2)), viewport)
        self.assertIsNone(viewport.selection((20, 20), (20, 20)))
        self.assertFalse(document.crop(Rect(0, 0, 20, 20)))
        self.assertFalse(document.crop(Rect(10, 10, 0, 1)))
        self.assertFalse(document.crop(viewport))

    def test_invalid_strokes(self):
        for kwargs in ({"points": ()}, {"points": ((1, 1),), "width": 0},
                       {"points": ((float("nan"), 1),)},
                       {"points": ((1, 1),), "color": (2, 0, 0)}):
            with self.assertRaises(ValueError):
                Stroke(**kwargs)


class RenderTests(unittest.TestCase):
    def test_original_and_pen_pixels(self):
        document = Document(80, 60)
        base = solid_base()
        document.add_stroke(Stroke(((10, 20), (60, 20)), (1, 0, 0), 6))
        result = render(snapshot(document, base))
        self.assertEqual(pixel(result, 30, 20), (255, 0, 0, 255))
        self.assertEqual(pixel(result, 30, 40), (255, 255, 255, 255))
        document.undo()
        result = render(snapshot(document, base))
        self.assertEqual(bytes(result.get_data()), base.pixels)

    def test_round_dot(self):
        document = Document(80, 60)
        document.add_stroke(Stroke(((20, 20),), (0, 0, 1), 10))
        result = render(snapshot(document))
        self.assertEqual(pixel(result, 20, 20), (0, 0, 255, 255))
        self.assertEqual(pixel(result, 14, 14), (255, 255, 255, 255))

    def test_highlighter_does_not_darken_self_overlap(self):
        document = Document(80, 60)
        once = Stroke(((10, 30), (60, 30)), (1, 1, 0), 12, "highlighter")
        document.add_stroke(once)
        single = pixel(render(snapshot(document)), 30, 30)
        repeated = replace(once, points=((10, 30), (60, 30), (10, 30), (60, 30)))
        document.undo()
        document.add_stroke(repeated)
        self.assertEqual(pixel(render(snapshot(document)), 30, 30), single)
        self.assertEqual(single[:2], (255, 255))
        self.assertAlmostEqual(single[2], 173, delta=1)
        document.add_stroke(once)
        self.assertLess(pixel(render(snapshot(document)), 30, 30)[2], single[2])

    def test_highlighter_multiply_preserves_dark_text(self):
        document = Document(80, 60)
        document.add_stroke(Stroke(((10, 30), (60, 30)), (1, 1, 0), 12, "highlighter"))
        result = render(snapshot(document, solid_base(color=(0, 0, 0))))
        self.assertEqual(pixel(result, 30, 30), (0, 0, 0, 255))

    def test_crop_output_and_image_coordinate_transform(self):
        document = Document(80, 60)
        document.add_stroke(Stroke(((20, 25), (60, 25)), (0, 0, 1), 6))
        document.crop(Rect(15, 20, 25, 15))
        result = render(snapshot(document))
        self.assertEqual((result.get_width(), result.get_height()), (25, 15))
        self.assertEqual(pixel(result, 10, 5), (0, 0, 255, 255))
        document.undo()
        document.undo()
        self.assertEqual(render(snapshot(document)).get_width(), 80)

    def test_smoothed_eraser_and_fast_swept_motion(self):
        straight = Stroke(((10, 30), (60, 30)), width=4)
        self.assertTrue(hit_stroke(straight, (30, 30), radius=0))
        self.assertFalse(hit_stroke(straight, (30, 40), radius=0))
        self.assertTrue(hit_stroke(straight, (30, 0), (30, 60), radius=0))
        self.assertFalse(hit_stroke(straight, (70, 0), (70, 60), radius=0))
        dot = Stroke(((20, 20),), width=10)
        self.assertTrue(hit_stroke(dot, (24, 20), radius=0))
        bend = Stroke(((10, 10), (30, 50), (60, 10)), width=8)
        # Compare hit-testing with the actual path at clearly interior pixels.
        doc = Document(80, 60)
        doc.add_stroke(bend)
        result = render(snapshot(doc))
        interior = [(x, y) for x in range(80) for y in range(60)
                    if pixel(result, x, y) == (255, 0, 0, 255)]
        self.assertGreater(len(interior), 100)
        self.assertTrue(all(hit_stroke(bend, (x + 0.5, y + 0.5), radius=0)
                            for x, y in interior))
        outside = Stroke(((0, 5), (5, 5)), width=2)
        self.assertFalse(hit_stroke(outside, (20, 20), radius=1, crop=Rect(20, 20, 30, 30)))
        invisible = Stroke(((10, 16), (60, 16)), width=2)
        self.assertFalse(hit_stroke(invisible, (30, 20), radius=6, crop=Rect(20, 20, 30, 30)))

    def test_view_cache_appends_without_rerendering_base(self):
        document = Document(80, 60)
        base = solid_base()
        cache = ViewCache(base)
        original_surface = cache.get(snapshot(document, base), 0.5)
        before = document.state
        stroke = Stroke(((10, 20), (60, 20)), width=6)
        document.add_stroke(stroke)
        with patch("winsnipper.render.render", side_effect=AssertionError("full redraw")):
            cache.append(before, document.state, stroke)
            self.assertIs(cache.get(snapshot(document, base), 0.5), original_surface)
        self.assertEqual(bytes(original_surface.get_data()),
                         bytes(render(snapshot(document, base), 0.5).get_data()))

    def test_eraser_bounds_match_segment_only_reference(self):
        rng = random.Random(17)
        # Passing an infinite box disables only the new rejection; the exact
        # existing segment tests and crop clipping remain the reference.
        everything = Rect(-1e9, -1e9, 2e9, 2e9)
        for _ in range(200):
            stroke = Stroke(tuple((rng.uniform(-40, 120), rng.uniform(-30, 90))
                                  for _ in range(rng.randint(1, 12))), width=rng.uniform(.1, 30))
            flat = flattened_points(stroke)
            bounds = stroke_bounds(stroke, flat)
            start, end = ((rng.uniform(-70, 150), rng.uniform(-60, 120)) for _ in range(2))
            radius = rng.uniform(0, 15)
            crop = rng.choice((None, Rect(10, 10, 50, 40)))
            self.assertEqual(hit_stroke(stroke, start, end, radius, crop, flat, bounds),
                             hit_stroke(stroke, start, end, radius, crop, flat, everything))
        dot = Stroke(((20, 20),), width=10)
        self.assertTrue(hit_stroke(dot, (25.5, 20), radius=0))

    def test_live_highlighter_matches_committed_union_and_provisional_tail(self):
        rng = random.Random(3)
        paths = [((20, 20),), ((10, 30), (60, 30), (10, 30), (60, 30)),
                 tuple((rng.randrange(10, 70), rng.randrange(10, 50)) for _ in range(35))]
        crop = Rect(5, 5, 70, 50)
        for scale in (0.45, 1, 2):
            for points in paths:
                live = LiveHighlighter(crop, scale, (1, 1, 0), 12)
                for i, point in enumerate(points):
                    live.add(point)
                    state = replace(Document(80, 60).state, crop=crop)
                    snap = Snapshot(solid_base(), state, 0)
                    expected = render(replace(snap, state=replace(state, strokes=(
                        Stroke(points[:i + 1], (1, 1, 0), 12, "highlighter"),))), scale)
                    actual = render(snap, scale)
                    context = cairo.Context(actual)
                    context.scale(scale, scale)
                    context.translate(-crop.x, -crop.y)
                    live.draw(context)
                    actual.flush()
                    diffs = [abs(a - b) for a, b in zip(actual.get_data().cast('B'),
                                                       expected.get_data().cast('B'))]
                    # Local clipping/tessellation and A8 vs ARGB groups can
                    # differ at antialiased edges; interior coverage is equal.
                    self.assertLessEqual(max(diffs), 6)
                    self.assertLess(sum(diffs) / len(diffs), .03)

    def test_live_highlighter_self_overlap_and_separate_strokes_compound(self):
        live = LiveHighlighter(Rect(0, 0, 80, 60), 1, (1, 1, 0), 12)
        for point in ((10, 30), (60, 30), (10, 30), (60, 30)):
            live.add(point)
        result = render(snapshot(Document(80, 60)))
        context = cairo.Context(result)
        live.draw(context)
        self.assertAlmostEqual(pixel(result, 30, 30)[2], 173, delta=1)
        live.draw(context)
        self.assertLess(pixel(result, 30, 30)[2], 173)

    def test_view_cache_reuses_resize_raster_and_mip_keeps_crop_coordinates(self):
        base = solid_base(160, 120)
        doc = Document(160, 120)
        doc.add_stroke(Stroke(((45, 55), (100, 55)), (0, 0, 1), 14))
        doc.crop(Rect(30, 40, 100, 60))
        snap = snapshot(doc, base)
        cache = ViewCache(base)
        surface = cache.get(snap, .45)
        version = cache.version
        self.assertEqual(pixel(surface, 10, 7), (0, 0, 255, 255))
        self.assertIs(cache.get(snap, cache.reuse_scale(snap, .4)), surface)
        self.assertEqual(cache.version, version)
        self.assertIsNot(cache.get(snap, .4), surface)
        self.assertEqual(cache.version, version + 1)
        doc.undo()
        self.assertEqual(cache.reuse_scale(snapshot(doc, base), .3), .3)


class FakeLoop:
    def __init__(self):
        self.timers = {}
        self.idles = []
        self.counter = 0

    def schedule(self, milliseconds, callback):
        if milliseconds != 250:
            raise AssertionError(milliseconds)
        self.counter += 1
        self.timers[self.counter] = callback
        return self.counter

    def cancel(self, handle):
        self.timers.pop(handle, None)

    def dispatch(self, callback):
        self.idles.append(callback)

    def debounce(self):
        timers, self.timers = self.timers, {}
        for callback in timers.values():
            callback()

    def drain(self):
        while self.idles:
            callbacks, self.idles = self.idles, []
            for callback in callbacks:
                callback()


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, callback, *args):
        future = Future()
        self.jobs.append((future, callback, args))
        return future

    def finish(self):
        future, callback, args = self.jobs.pop(0)
        try:
            future.set_result(callback(*args))
        except Exception as error:
            future.set_exception(error)


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shot.png"
        self.base = solid_base()
        self.document = Document(80, 60)
        atomic_save(self.path, snapshot(self.document, self.base))

    def edit(self):
        self.document.add_stroke(Stroke(((20, 20),), width=10))
        return snapshot(self.document, self.base)

    def coordinator(self, writer=atomic_save):
        self.loop = FakeLoop()
        self.executor = ManualExecutor()
        self.saved = []
        self.errors = []
        self.statuses = []
        return SaveCoordinator(self.path, self.loop.schedule, self.loop.cancel,
                               self.loop.dispatch, self.saved.append, self.errors.append,
                               self.statuses.append, self.executor, writer)

    def test_atomic_save_and_undo_restores_original(self):
        original = self.path.read_bytes()
        os.chmod(self.path, 0o640)
        atomic_save(self.path, self.edit())
        self.assertNotEqual(self.path.read_bytes(), original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.document.undo()
        atomic_save(self.path, snapshot(self.document, self.base))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_replace_preserves_original_and_cleans_temp(self):
        original = self.path.read_bytes()
        with patch("winsnipper.saving.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_save(self.path, self.edit())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_atomic_save_supports_maximum_length_filename(self):
        path = self.path.parent / ("x" * 251 + ".png")
        atomic_save(path, self.edit())
        self.assertTrue(path.is_file())

    def test_coordinator_reuses_a_source_owned_by_each_coordinator(self):
        coordinator = self.coordinator()
        # Patch with an explicit original so cairo still receives a real source.
        original = BaseImage.surface
        with patch.object(BaseImage, "surface", autospec=True, side_effect=original) as copied:
            for _ in range(2):
                coordinator.request(self.edit())
                coordinator.flush()
                self.executor.finish()
                self.loop.drain()
            self.assertEqual(copied.call_count, 1)
        other = self.coordinator()
        other.request(self.edit())
        other.flush()
        self.executor.finish()
        self.loop.drain()
        self.assertIsNot(other._source, coordinator._source)

    def test_path_encode_receives_a_filename_and_fsync_failure_cleans_temp(self):
        original = self.path.read_bytes()
        with patch("winsnipper.saving.render") as mocked:
            atomic_save(self.path, self.edit())
            filename = mocked.return_value.write_to_png.call_args.args[0]
            self.assertIsInstance(filename, str)
        self.path.write_bytes(original)
        with patch("winsnipper.saving.os.fsync", side_effect=OSError("sync failed")):
            with self.assertRaises(OSError):
                atomic_save(self.path, self.edit())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_reused_source_preserves_transparency(self):
        image = cairo.ImageSurface(cairo.FORMAT_ARGB32, 80, 60)
        context = cairo.Context(image)
        context.set_source_rgba(.7, .2, .1, .4)
        context.paint()
        base = BaseImage.from_surface(image)
        snap = snapshot(self.document, base)
        atomic_save(self.path, snap)
        original = self.path.read_bytes()
        atomic_save(self.path, snap, base.surface())
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_encode_preserves_original_and_cleans_temp(self):
        original = self.path.read_bytes()
        with patch("winsnipper.saving.render") as mocked:
            mocked.return_value.write_to_png.side_effect = OSError("encode failed")
            with self.assertRaises(OSError):
                atomic_save(self.path, self.edit())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_debounce_coalesces_latest_snapshot(self):
        coordinator = self.coordinator()
        coordinator.request(self.edit())
        coordinator.request(self.edit())
        self.assertEqual(len(self.loop.timers), 1)
        self.loop.debounce()
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.finish()
        self.loop.drain()
        self.assertEqual(coordinator.saved_revision, 2)
        self.assertEqual(self.saved, [self.path])
        self.assertFalse(coordinator.busy)
        self.assertEqual(self.statuses[-1], "Saved")
        coordinator.shutdown()

    def test_inflight_save_coalesces_and_flush_waits_for_newest(self):
        writes = []

        def writer(path, snap):
            writes.append(snap.revision)
            atomic_save(path, snap)
        coordinator = self.coordinator(writer)
        coordinator.request(self.edit())
        self.loop.debounce()
        coordinator.request(self.edit())
        latest = self.edit()
        coordinator.request(latest)
        finished = []
        coordinator.flush(finished.append)
        self.executor.finish()
        self.loop.drain()
        self.assertEqual(finished, [])
        self.assertEqual(self.saved, [])
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.finish()
        self.loop.drain()
        self.assertEqual(writes, [1, 3])
        self.assertEqual(finished, [None])
        self.assertEqual(self.saved, [self.path])
        self.assertEqual(bytes(cairo.ImageSurface.create_from_png(str(self.path)).get_data()),
                         bytes(render(latest).get_data()))

    def test_flush_starts_immediately_and_failed_save_can_be_retried(self):
        attempts = []

        def writer(path, snap):
            attempts.append(snap.revision)
            if len(attempts) == 1:
                raise OSError("no space")
            atomic_save(path, snap)
        coordinator = self.coordinator(writer)
        coordinator.request(self.edit())
        finished = []
        coordinator.flush(finished.append)
        self.assertFalse(self.loop.timers)
        self.executor.finish()
        self.loop.drain()
        self.assertIsInstance(finished[0], OSError)
        self.assertTrue(coordinator.busy)
        coordinator.flush(finished.append)
        self.executor.finish()
        self.loop.drain()
        self.assertIsNone(finished[-1])
        self.assertEqual(attempts, [1, 1])
        self.assertFalse(coordinator.busy)

    def test_real_worker_uses_a_different_thread(self):
        thread_ids = []
        ready = threading.Event()
        loop = FakeLoop()
        main_id = threading.get_ident()

        def writer(path, snap):
            thread_ids.append(threading.get_ident())
            atomic_save(path, snap)
            ready.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            coordinator = SaveCoordinator(self.path, loop.schedule, loop.cancel, loop.dispatch,
                                          lambda _path: None, self.fail, executor=executor, writer=writer)
            coordinator.request(self.edit())
            coordinator.flush()
            self.assertTrue(ready.wait(10))
        loop.drain()
        self.assertNotEqual(thread_ids, [main_id])
        self.assertFalse(coordinator.busy)


if __name__ == "__main__":
    unittest.main()
