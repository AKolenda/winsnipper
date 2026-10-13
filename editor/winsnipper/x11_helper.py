"""Isolated GTK3 helper: native X11 root image as PNG on stdout.

Never import this module into the GTK4 application. A parent process owns the
private output descriptor and enforces the wall-clock timeout/cancellation.
"""
import ctypes
import os
import resource
import sys

MAX_PIXELS = 32 * 1024 * 1024
MAX_BYTES = 256 * 1024 * 1024


def ensure_native_x11():
    if (not os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"):
        raise RuntimeError("X11 full-screen capture requires a native X11 session. Use the system dialog on Wayland.")
    # The standard XWAYLAND extension identifies Xwayland even when a launcher
    # dropped WAYLAND_DISPLAY/XDG_SESSION_TYPE from its environment.
    xlib = ctypes.CDLL("libX11.so.6")
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xlib.XQueryExtension.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_int)]
    xlib.XQueryExtension.restype = ctypes.c_int
    display = xlib.XOpenDisplay(None)
    if not display:
        raise RuntimeError("Could not connect to the X11 display.")
    try:
        opcode, event, error = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if xlib.XQueryExtension(display, b"XWAYLAND", ctypes.byref(opcode),
                               ctypes.byref(event), ctypes.byref(error)):
            raise RuntimeError("Xwayland capture is disabled. Use the system screenshot dialog on Wayland.")
    finally:
        xlib.XCloseDisplay(display)


def main():
    try:
        ensure_native_x11()
        try:
            import gi
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk
        except (ImportError, ValueError) as error:
            raise RuntimeError("X11 full-screen capture needs GTK3 bindings. Install "
                               f"gir1.2-gtk-3.0 and python3-gi. {error}") from error
        Gdk.set_allowed_backends("x11")
        Gdk.init([])
        # Older Xwayland predates the identifying extension. Its virtual input
        # devices expose the same identity used by X.Org's xinput detection.
        manager = Gdk.Display.get_default().get_device_manager()
        for kind in (Gdk.DeviceType.MASTER, Gdk.DeviceType.SLAVE, Gdk.DeviceType.FLOATING):
            if any(device.get_name().lower().startswith("xwayland-")
                   for device in manager.list_devices(kind)):
                raise RuntimeError("Xwayland capture is disabled. Use the system screenshot dialog on Wayland.")
        root = Gdk.get_default_root_window()
        if root is None:
            raise RuntimeError("The X11 desktop has no accessible root window.")
        width, height, scale = root.get_width(), root.get_height(), root.get_scale_factor()
        if width < 1 or height < 1 or width * height * scale * scale > MAX_PIXELS:
            raise RuntimeError("The X11 screen exceeds the 32 megapixel capture limit.")
        pixels = Gdk.pixbuf_get_from_window(root, 0, 0, width, height)
        if pixels is None:
            raise RuntimeError("The X11 server could not provide the screen image.")
        # Never raise either inherited limit, including a deliberately lowered
        # soft limit. RLIM_INFINITY is a sentinel, not a finite negative size.
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        soft, hard = (MAX_BYTES - 1 if value == resource.RLIM_INFINITY
                      else min(value, MAX_BYTES - 1) for value in (soft, hard))
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Could not apply the X11 capture file-size limit: {error}") from error
        success, png = pixels.save_to_bufferv("png", [], [])
        if not success:
            raise RuntimeError("The X11 capture could not be encoded as PNG.")
        if len(png) > soft:
            raise RuntimeError(f"The X11 capture exceeds this process's file-size limit ({soft} bytes).")
        sys.stdout.buffer.write(png)
        sys.stdout.buffer.flush()
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
