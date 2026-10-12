"""A cached texture plus a bounded live cairo node, rather than a DrawingArea."""
from dataclasses import replace
import sys

import gi

gi.require_version('Graphene', '1.0')
gi.require_version('Gsk', '4.0')
from gi.repository import Graphene, Gsk

from .gtk_support import Gdk, GLib, Gtk
from .model import Rect, Stroke
from .render import draw_stroke, stroke_bounds


def rect(bounds):
    return Graphene.Rect().init(bounds.x, bounds.y, bounds.width, bounds.height)


def intersection(a, b):
    x, y = max(a.x, b.x), max(a.y, b.y)
    right, bottom = min(a.right, b.right), min(a.bottom, b.bottom)
    return Rect(x, y, max(0, right - x), max(0, bottom - y))


class Canvas(Gtk.Widget):
    def __init__(self, window):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        self.window = window
        self.content_size = (1, 1)
        self.texture = None
        self.texture_version = None
        self.texture_builds = 0
        self.last_overlay_bounds = None

    def set_content_size(self, width, height):
        size = (width, height)
        if self.content_size != size:
            self.content_size = size
            self.queue_resize()

    def do_measure(self, orientation, _for_size):
        value = self.content_size[0 if orientation == Gtk.Orientation.HORIZONTAL else 1]
        return value, value, -1, -1

    def _texture(self, surface):
        if self.texture_version != self.window.cache.version:
            surface.flush()
            # GBytes owns a copy: ViewCache.append may mutate the original.
            data = GLib.Bytes.new(bytes(surface.get_data()))
            format = (Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED if sys.byteorder == 'little'
                      else Gdk.MemoryFormat.A8R8G8B8_PREMULTIPLIED)
            self.texture = Gdk.MemoryTexture.new(surface.get_width(), surface.get_height(),
                                                 format, data, surface.get_stride())
            self.texture_version = self.window.cache.version
            self.texture_builds += 1
        return self.texture

    def _append_texture(self, snapshot, texture, bounds):
        if hasattr(snapshot, 'append_scaled_texture'):
            snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.LINEAR, rect(bounds))
        else:
            snapshot.append_texture(texture, rect(bounds))

    def do_snapshot(self, snapshot):
        w = self.window
        width, height = self.get_width(), self.get_height()
        found, background = w.get_style_context().lookup_color('theme_bg_color')
        if not found:
            background = Gdk.RGBA()
            background.parse('#2e2e2e')
        snapshot.append_color(background, rect(Rect(0, 0, width, height)))
        content = w.snapshot()
        if w._removed:
            content = replace(content, state=replace(content.state, strokes=tuple(
                stroke for i, stroke in enumerate(content.state.strokes) if i not in w._removed)))
        scale = min(1.0, w.zoom * self.get_scale_factor())
        if w._resize_timer is not None:
            scale = w.cache.reuse_scale(content, scale)
        surface = w.cache.get(content, scale)
        texture = self._texture(surface)
        ox, oy = w._origin(width, height)
        crop = content.state.crop
        image_bounds = Rect(ox, oy, crop.width * w.zoom, crop.height * w.zoom)
        texture_bounds = Rect(ox, oy, surface.get_width() * w.zoom / scale,
                              surface.get_height() * w.zoom / scale)
        overlay = None
        live = None
        if w._points and w._drag_tool in ('pen', 'highlighter'):
            if w._drag_tool == 'highlighter':
                live = w._ensure_highlighter(min(1.0, w.zoom * self.get_scale_factor()))
                bounds = live.bounds
            else:
                stroke = Stroke(tuple(w._points), w.colors["pen"], w.widths["pen"], "pen")
                bounds = stroke_bounds(stroke, padding=stroke.width / 2 + 2 / w.zoom)
            overlay = Rect(ox + (bounds.x - crop.x) * w.zoom,
                           oy + (bounds.y - crop.y) * w.zoom,
                           bounds.width * w.zoom, bounds.height * w.zoom)
        elif w._points and w._drag_tool == 'crop' and w._crop_end is not None:
            overlay = image_bounds
        # Bound CPU rasterization to both the live region and the viewport.
        if overlay is not None:
            h, v = w.scroller.get_hadjustment(), w.scroller.get_vadjustment()
            visible = Rect(h.get_value(), v.get_value(), h.get_page_size(), v.get_page_size())
            overlay = intersection(intersection(overlay, image_bounds), visible)
            if overlay.width <= 0 or overlay.height <= 0:
                overlay = None
        self.last_overlay_bounds = overlay
        snapshot.push_clip(rect(image_bounds))
        if live is not None and overlay is not None:
            snapshot.push_blend(Gsk.BlendMode.MULTIPLY)
        self._append_texture(snapshot, texture, texture_bounds)
        if live is not None and overlay is not None:
            snapshot.pop()  # bottom child of the blend
        if overlay is not None:
            context = snapshot.append_cairo(rect(overlay))
            context.translate(ox, oy)
            context.scale(w.zoom, w.zoom)
            context.translate(-crop.x, -crop.y)
            if live is not None:
                live.draw(context, multiply=False)
            elif w._drag_tool == 'pen':
                draw_stroke(context, stroke)
            else:
                selected = crop.selection(w._points[0], w._crop_end)
                if selected is not None:
                    w._draw_crop(context, crop, selected)
            del context
        if live is not None and overlay is not None:
            snapshot.pop()  # top child and completed blend
        snapshot.pop()  # image clip
