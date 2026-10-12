"""Run in a fresh D-Bus session/display; exercises actual --gapplication-service.

The service must stay alive with an open window beyond GApplication's default
10-second service startup timeout. The script explicitly quits after checking.
"""
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cairo
from winsnipper.app import EditorApplication
from winsnipper.gtk_support import Gio, GLib
from winsnipper.model import Stroke

application = EditorApplication()
errors = []
started = time.monotonic()
directory = tempfile.TemporaryDirectory()
path = Path(directory.name) / "service.png"
surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 80, 60)
context = cairo.Context(surface)
context.set_source_rgb(1, 1, 1)
context.paint()
surface.write_to_png(str(path))
original = path.read_bytes()
stage = "prewarm"


def tick():
    global stage
    try:
        if stage == "prewarm":
            assert application.get_windows() == [], "service startup showed a window"
            application.activate_action("prewarm", None)
            assert application.get_windows() == [], "prewarm showed a window"
            application.open([Gio.File.new_for_path(str(path))], "")
            stage = "open"
        elif stage == "open" and path in application.windows_by_path:
            window = application.windows_by_path[path]
            window.document.add_stroke(Stroke(((10, 20), (60, 20))))
            window._edited()
            stage = "resident"
        elif stage == "resident" and time.monotonic() - started > 11:
            assert len(application.get_windows()) == 1
            window = application.windows_by_path[path]
            assert window.status.get_label() == "Saved"
            assert path.read_bytes() != original
            window.document.undo()
            window._edited()
            window.close()
            stage = "close"
        elif stage == "close" and not application.windows_by_path:
            assert path.read_bytes() == original, "close did not flush the undo"
            stage = "done"
            print("PASS: service prewarm hidden; open window survived >11 s; close flushed latest edit")
            application.quit()
            return False
        if time.monotonic() - started > 20:
            raise AssertionError("Service smoke test timed out")
    except Exception as error:
        errors.append(str(error))
        application.quit()
        return False
    return True


GLib.timeout_add(50, tick)
result = application.run(["winsnipper-editor", "--gapplication-service"])
directory.cleanup()
if errors or stage != "done":
    print("FAIL:", errors or ["Service exited early"], file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(result)
