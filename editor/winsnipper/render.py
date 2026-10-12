"""Pure cairo rendering, shared smoothed geometry, and stroke erasing."""
from dataclasses import dataclass
import math
import cairo

from .model import Rect, State, Stroke

HIGHLIGHT_ALPHA = 0.32


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
        return cls.from_surface(cairo.ImageSurface.create_from_png(str(path)))

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

    def invalidate(self):
        self.state = None

    def get(self, snapshot, scale):
        if self.state != snapshot.state or self.scale != scale:
            self.surface = render(snapshot, scale, self.source)
            self.state = snapshot.state
            self.scale = scale
        return self.surface

    def append(self, before, after, stroke):
        if self.surface is not None and self.state == before and before.crop == after.crop:
            context = cairo.Context(self.surface)
            context.scale(self.scale, self.scale)
            context.translate(-before.crop.x, -before.crop.y)
            draw_stroke(context, stroke)
            self.surface.flush()
            self.state = after


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


def hit_stroke(stroke, start, end=None, radius=6.0, crop=None, flattened=None):
    """Hit a swept eraser segment, including sparse/fast pointer events."""
    end = start if end is None else end
    reach = radius + stroke.width / 2 + 0.5  # bound on curve flattening error
    points = flattened if flattened is not None else flattened_points(stroke)
    for a, b in zip(points, points[1:] or points):
        if crop is not None:
            visible_radius = stroke.width / 2 + 0.5
            expanded = Rect(crop.x - visible_radius, crop.y - visible_radius,
                            crop.width + visible_radius * 2, crop.height + visible_radius * 2)
            clipped = _clip_segment(a, b, expanded)
            if clipped is None:
                continue
            a, b = clipped
        if _intersects(start, end, a, b) or min(
                point_segment_distance(start, a, b), point_segment_distance(end, a, b),
                point_segment_distance(a, start, end), point_segment_distance(b, start, end)) <= reach:
            return True
    return False
