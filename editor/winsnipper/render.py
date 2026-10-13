"""Pure cairo rendering, shared smoothed geometry, and stroke erasing."""
from dataclasses import dataclass
import io
import math
import os
import stat
import struct
import cairo

from .model import Rect, State, Stroke

HIGHLIGHT_ALPHA = 0.32
MAX_IMAGE_BYTES = 256 * 1024 * 1024
# Up to an 8K desktop, with at most 128 MiB per decoded ARGB surface.
MAX_IMAGE_PIXELS = 32 * 1024 * 1024


def read_png(path):
    """Read one bounded PNG snapshot before giving it to a native decoder.

    Validate the opened descriptor, not a pathname that can change between
    stat and open. NONBLOCK keeps a replacement FIFO from hanging a worker.
    Decoding these immutable bytes also prevents a changed IHDR from bypassing
    the pixel limit after validation.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError('The screenshot is not a regular file.')
        if info.st_size >= MAX_IMAGE_BYTES:
            raise OSError('The screenshot is too large (the limit is 256 MiB).')
        header = source.read(33)
        if (len(header) != 33 or header[:8] != b'\x89PNG\r\n\x1a\n'
                or header[8:16] != b'\x00\x00\x00\rIHDR'):
            raise OSError('The screenshot does not have a valid PNG header.')
        width, height = struct.unpack('>II', header[16:24])
        if width == 0 or height == 0 or width * height > MAX_IMAGE_PIXELS:
            raise OSError('The screenshot dimensions exceed the 32-megapixel limit.')
        encoded = header + source.read(MAX_IMAGE_BYTES - len(header))
        if len(encoded) >= MAX_IMAGE_BYTES:
            raise OSError('The screenshot is too large (the limit is 256 MiB).')
        return encoded


@dataclass(frozen=True)
class BaseImage:
    width: int
    height: int
    stride: int
    format: int
    pixels: bytes

    @classmethod
    def from_surface(cls, surface):
        surface.flush()
        return cls(surface.get_width(), surface.get_height(), surface.get_stride(),
                   surface.get_format(), bytes(surface.get_data()))

    @classmethod
    def from_png(cls, path):
        return cls.from_surface(cairo.ImageSurface.create_from_png(io.BytesIO(read_png(path))))

    def surface(self):
        # Each thread gets its own surface and writable backing storage. The
        # immutable original bytes survive all saves and all crop operations.
        return cairo.ImageSurface.create_for_data(bytearray(self.pixels), self.format,
                                                  self.width, self.height, self.stride)


@dataclass(frozen=True)
class Snapshot:
    base: BaseImage
    state: State
    revision: int


def curves(points):
    """Yield midpoint quadratic segments as (start, control, end)."""
    start = points[0]
    for i in range(1, len(points)):
        control = points[i - 1]
        end = ((control[0] + points[i][0]) / 2,
               (control[1] + points[i][1]) / 2)
        yield start, control, end
        start = end
    if len(points) > 1:
        yield start, points[-1], points[-1]


def stroke_path(context, stroke):
    context.new_path()
    if len(stroke.points) == 1:
        context.arc(*stroke.points[0], stroke.width / 2, 0, math.tau)
        return False
    context.move_to(*stroke.points[0])
    for start, control, end in curves(stroke.points):
        # Cairo exposes cubics; this exactly converts a quadratic to a cubic.
        context.curve_to(start[0] + (control[0] - start[0]) * 2 / 3,
                         start[1] + (control[1] - start[1]) * 2 / 3,
                         end[0] + (control[0] - end[0]) * 2 / 3,
                         end[1] + (control[1] - end[1]) * 2 / 3, *end)
    return True


def draw_stroke(context, stroke):
    context.save()
    context.set_line_width(stroke.width)
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    context.set_line_join(cairo.LINE_JOIN_ROUND)
    if stroke.tool == "highlighter":
        # An opaque group is a union mask: revisiting a point within this stroke
        # cannot increase its alpha. Different strokes intentionally compound.
        # Bound the group to the stroke, instead of allocating a full 4K mask
        # on each pointer motion. Midpoint curves stay inside this convex hull.
        padding = stroke.width / 2 + 2
        left = min(p[0] for p in stroke.points) - padding
        top = min(p[1] for p in stroke.points) - padding
        right = max(p[0] for p in stroke.points) + padding
        bottom = max(p[1] for p in stroke.points) + padding
        context.rectangle(left, top, right - left, bottom - top)
        context.clip()
        context.push_group()
    context.set_source_rgb(*stroke.color)
    if stroke_path(context, stroke):
        context.stroke()
    else:
        context.fill()
    if stroke.tool == "highlighter":
        context.pop_group_to_source()
        context.set_operator(cairo.OPERATOR_MULTIPLY)
        context.paint_with_alpha(HIGHLIGHT_ALPHA)
    context.restore()


def render(snapshot, scale=1.0, source=None):
    crop = snapshot.state.crop
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                 max(1, math.ceil(crop.width * scale)),
                                 max(1, math.ceil(crop.height * scale)))
    context = cairo.Context(surface)
    context.scale(scale, scale)
    context.translate(-crop.x, -crop.y)
    context.set_source_surface(source if source is not None else snapshot.base.surface(), 0, 0)
    context.paint()
    for stroke in snapshot.state.strokes:
        draw_stroke(context, stroke)
    surface.flush()
    return surface


class ViewCache:
    """Rasterize committed content at view/HiDPI scale; append strokes in place."""

    def __init__(self, base):
        self.source = base.surface()
        self.surface = None
        self.state = None
        self.scale = None
        self.version = 0
        self._half_source = None

    def reuse_scale(self, snapshot, desired):
        """During a resize, keep the raster until the debounce expires."""
        return self.scale if self.surface is not None and self.state == snapshot.state else desired

    def invalidate(self):
        self.state = None

    def get(self, snapshot, scale):
        if self.state != snapshot.state or self.scale != scale:
            source = self.source
            if scale < 0.5:
                if self._half_source is None:
                    empty = State((), Rect(0, 0, snapshot.base.width, snapshot.base.height))
                    self._half_source = render(Snapshot(snapshot.base, empty, snapshot.revision), 0.5, self.source)
                    self._half_source.set_device_scale(0.5, 0.5)
                source = self._half_source
            self.surface = render(snapshot, scale, source)
            self.state = snapshot.state
            self.scale = scale
            self.version += 1
        return self.surface

    def append(self, before, after, stroke):
        if self.surface is not None and self.state == before and before.crop == after.crop:
            context = cairo.Context(self.surface)
            context.scale(self.scale, self.scale)
            context.translate(-before.crop.x, -before.crop.y)
            draw_stroke(context, stroke)
            self.surface.flush()
            self.state = after
            self.version += 1


def stroke_bounds(stroke, points=None, padding=None):
    """Conservative image-space bounds, including flattening error for erasing."""
    points = stroke.points if points is None else points
    padding = stroke.width / 2 + 0.5 if padding is None else padding
    left = min(p[0] for p in points) - padding
    top = min(p[1] for p in points) - padding
    right = max(p[0] for p in points) + padding
    bottom = max(p[1] for p in points) + padding
    return Rect(left, top, right - left, bottom - top)


def swept_intersects(bounds, start, end, radius):
    return not (max(start[0], end[0]) + radius < bounds.x
                or min(start[0], end[0]) - radius > bounds.right
                or max(start[1], end[1]) + radius < bounds.y
                or min(start[1], end[1]) - radius > bounds.bottom)


class LiveHighlighter:
    """One view-resolution A8 union mask with spatially bounded updates.

    A new point settles one quadratic and changes the provisional tail. Replay
    only curves touching that dirty rectangle, in one cairo stroke, so joins
    and antialiased self-intersections have the same union coverage as the
    committed path. Repeated OVER strokes would accumulate edge alpha.
    """

    def __init__(self, crop, scale, color, width):
        self.crop, self.scale, self.color, self.width = crop, scale, color, width
        self.points = []
        self.bounds = None
        self.tail_bounds = None
        self._segments = []
        self._cells = {}
        self._cell_size = 64 / scale
        size = (max(1, math.ceil(crop.width * scale)), max(1, math.ceil(crop.height * scale)))
        self.mask = cairo.ImageSurface(cairo.FORMAT_A8, *size)
        self.mask.set_device_scale(scale, scale)

    def _bounds(self, points):
        padding = self.width / 2 + 2 / self.scale
        left, top = min(p[0] for p in points) - padding, min(p[1] for p in points) - padding
        right, bottom = max(p[0] for p in points) + padding, max(p[1] for p in points) + padding
        return Rect(left, top, right - left, bottom - top)

    def _tiles(self, bounds):
        size = self._cell_size
        for x in range(math.floor(bounds.x / size), math.floor(bounds.right / size) + 1):
            for y in range(math.floor(bounds.y / size), math.floor(bounds.bottom / size) + 1):
                yield x, y

    @staticmethod
    def _curve(context, start, control, end):
        context.curve_to(start[0] + (control[0] - start[0]) * 2 / 3,
                         start[1] + (control[1] - start[1]) * 2 / 3,
                         end[0] + (control[0] - end[0]) * 2 / 3,
                         end[1] + (control[1] - end[1]) * 2 / 3, *end)

    def add(self, point):
        self.points.append(point)
        n = len(self.points)
        if n > 1:
            control = self.points[-2]
            start = self.points[0] if n == 2 else tuple(
                (a + b) / 2 for a, b in zip(self.points[-3], control))
            end = tuple((a + b) / 2 for a, b in zip(control, point))
            bounds = self._bounds((start, control, end))
            index = len(self._segments)
            self._segments.append((start, control, end, bounds))
            for cell in self._tiles(bounds):
                self._cells.setdefault(cell, []).append(index)
            tail = (end, point)
        else:
            tail = (point,)
        new_bounds = self._bounds(tail)
        previous = self.tail_bounds or new_bounds
        left, top = min(previous.x, new_bounds.x), min(previous.y, new_bounds.y)
        right, bottom = max(previous.right, new_bounds.right), max(previous.bottom, new_bounds.bottom)
        # Pixel-aligned clearing avoids partially clearing boundary mask pixels.
        left = self.crop.x + math.floor((left - self.crop.x) * self.scale) / self.scale
        top = self.crop.y + math.floor((top - self.crop.y) * self.scale) / self.scale
        right = self.crop.x + math.ceil((right - self.crop.x) * self.scale) / self.scale
        bottom = self.crop.y + math.ceil((bottom - self.crop.y) * self.scale) / self.scale
        dirty = Rect(left, top, right - left, bottom - top)
        context = cairo.Context(self.mask)
        context.translate(-self.crop.x, -self.crop.y)
        context.rectangle(dirty.x, dirty.y, dirty.width, dirty.height)
        context.clip()
        context.set_operator(cairo.OPERATOR_CLEAR)
        context.paint()
        context.set_operator(cairo.OPERATOR_OVER)
        context.set_source_rgba(1, 1, 1, 1)
        context.set_line_width(self.width)
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        context.set_line_join(cairo.LINE_JOIN_ROUND)
        indices = {i for cell in self._tiles(dirty) for i in self._cells.get(cell, ())}
        previous_index = -2
        for index in sorted(indices):
            start, control, end, bounds = self._segments[index]
            if not swept_intersects(bounds, (dirty.x, dirty.y), (dirty.right, dirty.bottom), 0):
                continue
            if index != previous_index + 1:
                context.move_to(*start)
            self._curve(context, start, control, end)
            previous_index = index
        if n == 1:
            context.arc(*point, self.width / 2, 0, math.tau)
            context.fill()
        else:
            if previous_index != len(self._segments) - 1:
                context.move_to(*tail[0])
            self._curve(context, tail[0], point, point)
            context.stroke()
        self.tail_bounds = new_bounds
        if self.bounds is None:
            self.bounds = new_bounds
        else:
            b = self.bounds
            x, y = min(b.x, new_bounds.x), min(b.y, new_bounds.y)
            self.bounds = Rect(x, y, max(b.right, new_bounds.right) - x,
                               max(b.bottom, new_bounds.bottom) - y)

    def draw(self, context, multiply=True):
        if self.bounds is None:
            return
        context.save()
        b = self.bounds
        context.rectangle(b.x, b.y, b.width, b.height)
        context.clip()
        context.set_operator(cairo.OPERATOR_MULTIPLY if multiply else cairo.OPERATOR_OVER)
        context.set_source_rgba(*self.color, HIGHLIGHT_ALPHA)
        context.mask_surface(self.mask, self.crop.x, self.crop.y)
        context.restore()


def flattened_points(stroke, tolerance=0.5):
    """Flatten the same curves used for drawing, without GTK or pixel scanning."""
    result = [stroke.points[0]]

    def subdivide(a, b, c, depth=0):
        if depth >= 14 or point_segment_distance(b, a, c) <= tolerance:
            result.append(c)
            return
        ab = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        bc = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2)
        mid = ((ab[0] + bc[0]) / 2, (ab[1] + bc[1]) / 2)
        subdivide(a, ab, mid, depth + 1)
        subdivide(mid, bc, c, depth + 1)

    for start, control, end in curves(stroke.points):
        subdivide(start, control, end)
    return tuple(result)


def point_segment_distance(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    denominator = dx * dx + dy * dy
    t = 0 if denominator == 0 else max(0, min(1, (
        (point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator))
    return math.hypot(point[0] - start[0] - t * dx, point[1] - start[1] - t * dy)


def _intersects(a, b, c, d):
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    u, v, w, z = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
    if u * v < 0 and w * z < 0:
        return True
    return any(abs(value) < 1e-9 and point_segment_distance(p, q, r) < 1e-9
               for value, p, q, r in ((u, c, a, b), (v, d, a, b),
                                       (w, a, c, d), (z, b, c, d)))


def _clip_segment(a, b, rect):
    """Liang–Barsky clip: an eraser cannot hit invisible, cropped-off geometry."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    lo, hi = 0.0, 1.0
    for p, q in ((-dx, a[0] - rect.x), (dx, rect.right - a[0]),
                 (-dy, a[1] - rect.y), (dy, rect.bottom - a[1])):
        if p == 0:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                lo = max(lo, t)
            else:
                hi = min(hi, t)
            if lo > hi:
                return None
    return ((a[0] + lo * dx, a[1] + lo * dy), (a[0] + hi * dx, a[1] + hi * dy))


def hit_stroke(stroke, start, end=None, radius=6.0, crop=None, flattened=None, bounds=None):
    """Hit a swept eraser segment, including sparse/fast pointer events."""
    end = start if end is None else end
    reach = radius + stroke.width / 2 + 0.5  # bound on curve flattening error
    points = flattened if flattened is not None else flattened_points(stroke)
    bounds = stroke_bounds(stroke, points) if bounds is None else bounds
    if not swept_intersects(bounds, start, end, radius):
        return False
    expanded = None
    if crop is not None:
        visible_radius = stroke.width / 2 + 0.5
        expanded = Rect(crop.x - visible_radius, crop.y - visible_radius,
                        crop.width + visible_radius * 2, crop.height + visible_radius * 2)
    for a, b in zip(points, points[1:] or points):
        if expanded is not None:
            clipped = _clip_segment(a, b, expanded)
            if clipped is None:
                continue
            a, b = clipped
        if _intersects(start, end, a, b) or min(
                point_segment_distance(start, a, b), point_segment_distance(end, a, b),
                point_segment_distance(a, start, end), point_segment_distance(b, start, end)) <= reach:
            return True
    return False
