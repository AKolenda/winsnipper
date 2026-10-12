"""Immutable image-space annotations and an undoable crop viewport (no GTK)."""
from dataclasses import dataclass, replace
import math

Point = tuple[float, float]
Color = tuple[float, float, float]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def contains(self, point):
        return self.x <= point[0] <= self.right and self.y <= point[1] <= self.bottom

    def selection(self, start: Point, end: Point):
        """Enclose the drag in integer pixels, clipped to this viewport."""
        x = max(self.x, math.floor(min(start[0], end[0])))
        y = max(self.y, math.floor(min(start[1], end[1])))
        right = min(self.right, math.ceil(max(start[0], end[0])))
        bottom = min(self.bottom, math.ceil(max(start[1], end[1])))
        if right <= x or bottom <= y:
            return None
        return Rect(x, y, right - x, bottom - y)


@dataclass(frozen=True)
class Stroke:
    points: tuple[Point, ...]
    color: Color = (1.0, 0.0, 0.0)
    width: float = 4.0
    tool: str = "pen"

    def __post_init__(self):
        if not self.points or self.width <= 0:
            raise ValueError("A stroke needs points and a positive width")
        if self.tool not in ("pen", "highlighter"):
            raise ValueError("Unknown drawing tool")
        if not all(math.isfinite(v) for point in self.points for v in point):
            raise ValueError("Stroke points must be finite")
        if len(self.color) != 3 or not all(0 <= v <= 1 for v in self.color):
            raise ValueError("Colours must contain three values between zero and one")


@dataclass(frozen=True)
class State:
    strokes: tuple[Stroke, ...]
    crop: Rect


class Document:
    """History shares immutable strokes; cropping never changes their coordinates."""

    def __init__(self, width: int, height: int):
        if width <= 0 or height <= 0:
            raise ValueError("An image must have positive dimensions")
        self.state = State((), Rect(0, 0, width, height))
        self.revision = 0
        self._undo = []
        self._redo = []

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def _commit(self, state):
        if state == self.state:
            return False
        self._undo.append(self.state)
        self._redo.clear()
        self.state = state
        self.revision += 1
        return True

    def add_stroke(self, stroke):
        return self._commit(replace(self.state, strokes=self.state.strokes + (stroke,)))

    def erase_indices(self, indices):
        removed = set(indices)
        return self._commit(replace(self.state, strokes=tuple(
            stroke for i, stroke in enumerate(self.state.strokes) if i not in removed)))

    def crop(self, rect):
        viewport = self.state.crop
        if (rect is None or rect.width <= 0 or rect.height <= 0
                or rect.x < viewport.x or rect.y < viewport.y
                or rect.right > viewport.right or rect.bottom > viewport.bottom):
            return False
        return self._commit(replace(self.state, crop=rect))

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(self.state)
        self.state = self._undo.pop()
        self.revision += 1
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self.state)
        self.state = self._redo.pop()
        self.revision += 1
        return True
