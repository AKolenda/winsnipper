"""Screenshot-like cairo benchmarks (CPU work, not compositor latency).

Capture a baseline with --output /tmp/before.json; compare with --before that file.
"""
import argparse
import io
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cairo
from winsnipper.model import Document, Stroke
from winsnipper import render as drawing
from winsnipper.render import BaseImage, Snapshot, ViewCache, draw_stroke, flattened_points, hit_stroke
from winsnipper.saving import atomic_save


def timed(call, repeats=5):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        call()
        values.append((time.perf_counter() - start) * 1000)
    return statistics.median(values)


def screenshot(width, height):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    gradient = cairo.LinearGradient(0, 0, width, height)
    gradient.add_color_stop_rgb(0, .10, .18, .29)
    gradient.add_color_stop_rgb(.48, .45, .57, .70)
    gradient.add_color_stop_rgb(1, .92, .87, .78)
    ctx.set_source(gradient)
    ctx.paint()
    ctx.select_font_face('monospace')
    ctx.set_font_size(16)
    for col in range(0, width, 620):
        ctx.set_source_rgba(.96, .97, .99, .9)
        ctx.rectangle(col + 18, 35, 580, height - 70)
        ctx.fill()
        for row, y in enumerate(range(65, height - 35, 24)):
            ctx.set_source_rgb(.08 + row % 4 * .03, .14, .22)
            ctx.move_to(col + 30, y)
            ctx.show_text(f'{row:03d}  Window / document: capture and annotate {row * 137:06d}')
    return surface


def benchmarks(width, height):
    base = BaseImage.from_surface(screenshot(width, height))
    document = Document(width, height)
    snap = Snapshot(base, document.state, 0)
    cache = ViewCache(base)
    out = {}
    out['base copy'] = timed(base.surface)
    out['cache 1.0'] = timed(lambda: drawing.render(snap, 1, cache.source), 3)
    out['cache 0.45'] = timed(lambda: ViewCache(base).get(snap, .45), 3)
    cache.get(snap, .45)
    resize_scales = [.43 + i * .002 for i in range(10)]
    out['resize burst 10 frames'] = timed(lambda: [cache.get(snap, s) for s in resize_scales], 3)
    if hasattr(cache, 'reuse_scale'):
        def resize_reuse():
            for scale in resize_scales:
                cache.get(snap, cache.reuse_scale(snap, scale))
            desired = .43 if cache.scale > .44 else .46
            cache.get(snap, desired)
        out['resize burst debounced'] = timed(resize_reuse, 3)
    target = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(target)
    source = cache.get(snap, 1)
    def frame():
        ctx.set_source_surface(source)
        ctx.paint()
    out['full image CPU repaint'] = timed(frame, 15)
    # Large convex hull with 800 points: the old group allocation is half-screen.
    points = tuple((width * (.08 + .84 * i / 799),
                    height * (.5 + .24 * math.sin(i / 60))) for i in range(800))
    stroke = Stroke(points, (1, 1, 0), 24, 'highlighter')
    out['live highlighter 800 points'] = timed(lambda: draw_stroke(ctx, stroke), 15)
    if hasattr(drawing, 'LiveHighlighter'):
        live = drawing.LiveHighlighter(snap.state.crop, 1, stroke.color, stroke.width)
        for point in points[:-1]:
            live.add(point)
        # CPU side of the production texture/blend canvas: update a small
        # mask region, emit a tinted cairo overlay, multiply on the GPU.
        counter = 0
        def add_point():
            nonlocal counter
            counter += 1
            live.add((points[-1][0] + counter * .6, points[-1][1] + math.sin(counter / 8)))
        out['incremental mask update'] = timed(add_point, 15)
        def live_frame():
            add_point()
            b = live.bounds
            x, y = math.floor(b.x), math.floor(b.y)
            overlay = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                         math.ceil(b.right) - x, math.ceil(b.bottom) - y)
            context = cairo.Context(overlay)
            context.translate(-x, -y)
            live.draw(context, multiply=False)
        out['incremental highlighter frame CPU'] = timed(live_frame, 15)
    strokes = tuple(Stroke(tuple((10 + i * 4, 30 + j * 8 + 10 * math.sin(i / 7))
                                 for i in range(300))) for j in range(100))
    flat = tuple(flattened_points(s) for s in strokes)
    bounds = tuple(drawing.stroke_bounds(s, p) for s, p in zip(strokes, flat)) if hasattr(drawing, 'stroke_bounds') else None
    for count in (40, 100):
        def misses():
            return [hit_stroke(s, (width - 30, height - 30), (width - 10, height - 10),
                               crop=snap.state.crop, flattened=p,
                               **({'bounds': b} if bounds is not None else {}))
                    for s, p, b in zip(strokes[:count], flat[:count], bounds or [None] * count)]
        out[f'eraser {count} misses'] = timed(misses, 7)
    document.add_stroke(Stroke(points))
    edited = Snapshot(base, document.state, 1)
    rendered = drawing.render(edited, source=cache.source)
    out['PNG fileobj encode'] = timed(lambda: rendered.write_to_png(io.BytesIO()), 3)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'shot.png'
        out['PNG path encode'] = timed(lambda: rendered.write_to_png(str(path)), 3)
        out['atomic save'] = timed(lambda: atomic_save(path, edited), 3)
        if 'source' in __import__('inspect').signature(atomic_save).parameters:
            out['atomic save reused source'] = timed(lambda: atomic_save(path, edited, cache.source), 3)
        out['PNG decode'] = timed(lambda: BaseImage.from_png(path), 3)
    # First draw: old full-resolution cache plus fit; new initial-fit cache only.
    out['first frame old full + fit'] = timed(lambda: [drawing.render(snap, s, cache.source) for s in (1, .45)], 3)
    out['first frame initial fit'] = timed(lambda: ViewCache(base).get(snap, .45), 3)
    return out


def gtk_benchmarks(width, height):
    from winsnipper.app import EditorApplication
    from winsnipper.gtk_support import GLib, Gtk
    from winsnipper.window import EditorWindow
    app = EditorApplication()
    app.register(None)
    base = BaseImage.from_surface(screenshot(width, height))
    window = EditorWindow(app, Path('/tmp/benchmark-unsaved.png'), base)
    window.present()
    window.set_zoom(1)
    def pump(seconds):
        end = time.monotonic() + seconds
        context = GLib.MainContext.default()
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(.002)
    pump(.25)
    def frame():
        snap = Gtk.Snapshot.new()
        window.area.do_snapshot(snap)
        snap.to_paintable(None)
    frame()
    out = {'GTK texture-only snapshot CPU': timed(frame, 15)}
    builds = window.area.texture_builds
    window._points = [(20, 20), (120, 30)]
    window._drag_tool = 'pen'
    out['GTK texture + pen snapshot CPU'] = timed(frame, 15)
    out['GTK texture rebuilds during 15 motions'] = window.area.texture_builds - builds
    window._points = []
    window._drag_tool = None
    window.destroy()
    app.executor.shutdown(wait=True)
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--ref', help='Load render/saving from a git ref for a reproducible baseline')
    parser.add_argument('--gtk', action='store_true', help='Also measure GTK snapshot CPU work under a display')
    args = parser.parse_args()
    if args.ref:
        import subprocess
        import types
        from winsnipper import model
        if args.gtk:
            parser.error('--ref measures the historical pure cairo paths; omit --gtk')
        package = types.ModuleType('benchmark_baseline')
        package.__path__ = []
        sys.modules['benchmark_baseline'] = package
        sys.modules['benchmark_baseline.model'] = model
        modules = {}
        for name in ('render', 'saving'):
            module = types.ModuleType('benchmark_baseline.' + name)
            module.__package__ = 'benchmark_baseline'
            sys.modules[module.__name__] = module
            code = subprocess.check_output(['git', 'show', f'{args.ref}:editor/winsnipper/{name}.py'], text=True)
            exec(compile(code, f'{args.ref}/{name}.py', 'exec'), module.__dict__)
            modules[name] = module
        drawing = modules['render']
        BaseImage, Snapshot, ViewCache = drawing.BaseImage, drawing.Snapshot, drawing.ViewCache
        draw_stroke, flattened_points, hit_stroke = drawing.draw_stroke, drawing.flattened_points, drawing.hit_stroke
        atomic_save = modules['saving'].atomic_save
    before = json.loads(args.before.read_text()) if args.before else {}
    results = {}
    for width, height in ((3840, 2160), (5120, 2880)):
        key = f'{width}x{height}'
        results[key] = benchmarks(width, height)
        if args.gtk:
            results[key].update(gtk_benchmarks(width, height))
        print(key, flush=True)
        for name, ms in results[key].items():
            old = before.get(key, {}).get(name)
            prefix = f'{old:.2f} -> ' if old is not None else ''
            print(f'  {name}: {prefix}{ms:.2f} ms')
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + '\n')
