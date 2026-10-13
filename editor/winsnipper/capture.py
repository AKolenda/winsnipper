"""Asynchronous, permission-preserving XDG Screenshot portal client."""
import uuid

from .gtk_support import Gio, GLib

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"
REQUEST_IFACE = "org.freedesktop.portal.Request"
TARGETS = {"screen": 1, "window": 2, "area": 4}
MODES = ("choose", "area", "window", "screen")
MISSING_PORTAL = ("Your desktop's screenshot service is unavailable. Install xdg-desktop-portal "
                  "and a Screenshot backend for your desktop, then log out and back in. "
                  "The GTK portal backend alone does not provide screenshots.")


class PortalCapture:
    """One request, one completion; all callbacks execute on the GTK main loop.

    Subscribe before Screenshot to handle fast replies. The compositor owns its
    picker and permission dialog; we never bypass them or enumerate windows.
    """
    def __init__(self, connection, completed, status=lambda _text: None, timeout_seconds=180):
        self.connection = connection
        self.completed = completed
        self.status = status
        self.timeout_seconds = timeout_seconds
        self._done = False
        self._subscription = None
        self._timeout = None
        self._handle = None
        self._expected = None
        self._early = {}
        self._call_pending = False
        self._token = "winsnipper_" + uuid.uuid4().hex

    def start(self, mode="choose"):
        if mode not in MODES:
            raise ValueError("Unknown capture mode.")
        self.mode = mode
        if self.connection is None:
            self._finish(error="A desktop session D-Bus connection is required for screenshot capture. "
                               "Log into your desktop session and launch WinSnipper there.")
            return
        self._timeout = GLib.timeout_add_seconds(self.timeout_seconds, self._timed_out)
        self.connection.call(PORTAL_NAME, PORTAL_PATH, "org.freedesktop.DBus.Properties", "GetAll",
                             GLib.Variant("(s)", (SCREENSHOT_IFACE,)), GLib.VariantType.new("(a{sv})"),
                             Gio.DBusCallFlags.NONE, 10000, None, self._properties_ready)

    def _properties_ready(self, connection, result):
        if self._done:
            return
        try:
            properties = connection.call_finish(result).unpack()[0]
        except GLib.Error:
            self._finish(error=MISSING_PORTAL)
            return
        version = properties.get("version", 1)
        targets = properties.get("AvailableTargets", 0)
        options = {"handle_token": GLib.Variant("s", self._token),
                   "modal": GLib.Variant("b", False)}
        if version >= 2:
            options["interactive"] = GLib.Variant("b", True)
        if self.mode in TARGETS and version >= 3 and targets & TARGETS[self.mode]:
            options["target"] = GLib.Variant("u", TARGETS[self.mode])
            self.status("Choose your screenshot in the system dialog.")
        elif self.mode != "choose":
            self.status("This desktop chooses the capture mode in its system dialog.")
        else:
            self.status("Choose your screenshot in the system dialog.")
        sender = connection.get_unique_name()[1:].replace(".", "_")
        self._expected = f"{PORTAL_PATH}/request/{sender}/{self._token}"
        # A broad path subscription also handles older portals returning a
        # different handle. Only this request's returned/expected path is used.
        self._subscription = connection.signal_subscribe(
            PORTAL_NAME, REQUEST_IFACE, "Response", None, None,
            Gio.DBusSignalFlags.NONE, self._response)
        self._call_pending = True
        connection.call(PORTAL_NAME, PORTAL_PATH, SCREENSHOT_IFACE, "Screenshot",
                        GLib.Variant("(sa{sv})", ("", options)), GLib.VariantType.new("(o)"),
                        Gio.DBusCallFlags.NONE, 15000, None, self._request_ready)

    def _request_ready(self, connection, result):
        self._call_pending = False
        try:
            handle = connection.call_finish(result).unpack()[0]
        except GLib.Error as error:
            if not self._done:
                self._close_request(self._expected)
                self._finish(error=f"{MISSING_PORTAL}\n\n{error.message}")
            return
        self._handle = handle
        if self._done:
            self._close_request(handle)
            return
        if handle in self._early:
            self._consume_response(self._early[handle])
        self._early.clear()

    def _response(self, _connection, _sender, path, _interface, _signal, parameters):
        if self._done:
            return
        if path == self._handle or path == self._expected:
            self._consume_response(parameters)
        elif self._call_pending and len(self._early) < 16:
            self._early[path] = parameters

    def _consume_response(self, parameters):
        response, results = parameters.unpack()
        if response == 1:
            self._finish(cancelled=True)
        elif response != 0:
            self._finish(error="The desktop screenshot service could not complete the capture.")
        elif not isinstance(results.get("uri"), str):
            self._finish(error="The desktop screenshot service returned no image.")
        else:
            self._finish(uri=results["uri"])

    def cancel(self):
        if not self._done:
            self._close_request(self._handle or self._expected)
            self._finish(cancelled=True)

    def _close_request(self, handle):
        if handle is not None:
            self.connection.call(PORTAL_NAME, handle, REQUEST_IFACE, "Close", None, None,
                                 Gio.DBusCallFlags.NONE, 3000, None, self._closed)

    @staticmethod
    def _closed(connection, result):
        try:
            connection.call_finish(result)
        except GLib.Error:
            pass  # The request may already have completed or the portal vanished.

    def _timed_out(self):
        self._timeout = None
        self._close_request(self._handle or self._expected)
        self._finish(error="The screenshot request timed out. Try New snip again.")
        return GLib.SOURCE_REMOVE

    def _finish(self, uri=None, error=None, cancelled=False):
        if self._done:
            return
        self._done = True
        if self._timeout is not None:
            GLib.source_remove(self._timeout)
            self._timeout = None
        if self._subscription is not None:
            self.connection.signal_unsubscribe(self._subscription)
            self._subscription = None
        self._early.clear()
        self.completed(uri, error, cancelled)
