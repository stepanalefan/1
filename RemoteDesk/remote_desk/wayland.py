"""Sharing a Wayland desktop through xdg-desktop-portal.

On Wayland an app may not read the screen or inject input directly. The desktop's
portal does it on the user's behalf:

  * RemoteDesktop portal: screen (via ScreenCast) + pointer + keyboard  (GNOME, KDE)
  * ScreenCast portal only: screen, no input -> view only                 (wlroots: Sway, labwc)

The first time, the desktop asks the person at this computer for permission; the
answer is kept as a "restore token" (~/.config/remotedesk/portal_token) so later
sessions start without asking again (where the desktop supports that).

Screen frames arrive as a PipeWire stream, read with GStreamer (appsink).
Needs: python3-gi, gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0,
gstreamer1.0-pipewire, gstreamer1.0-plugins-base.
"""
import logging
import os
import secrets
import sys
import threading

log = logging.getLogger("wayland")

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJ_PATH = "/org/freedesktop/portal/desktop"
RD = "org.freedesktop.portal.RemoteDesktop"
SC = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"

DEVICE_KEYBOARD, DEVICE_POINTER = 1, 2
SOURCE_MONITOR = 1
CURSOR_HIDDEN, CURSOR_EMBEDDED, CURSOR_METADATA = 1, 2, 4
PERSIST_UNTIL_REVOKED = 2

TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".config", "remotedesk", "portal_token")

# GStreamer source for one screen; tests replace it with videotestsrc
PIPELINE = ("pipewiresrc fd={fd} path={node} do-timestamp=true keepalive-time=1000 ! "
            "videoconvert ! video/x-raw,format=RGB ! appsink name=sink max-buffers=1 drop=true sync=false")


class PortalError(Exception):
    pass


def wayland_session():
    return sys.platform.startswith("linux") and (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY")))


def _gi():
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gio, GLib, Gst, GstVideo
    return Gio, GLib, Gst, GstVideo


def probe():
    """What this desktop's portal can do: {"screen": bool, "input": bool, "error": str|None}."""
    try:
        Gio, GLib, Gst, _ = _gi()
    except Exception as e:
        return {"screen": False, "input": False,
                "error": f"missing packages for Wayland sharing ({e.__class__.__name__}: {e})"}
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def prop(iface, name):
            try:
                v = bus.call_sync(BUS_NAME, OBJ_PATH, "org.freedesktop.DBus.Properties", "Get",
                                  GLib.Variant("(ss)", (iface, name)), GLib.VariantType("(v)"),
                                  Gio.DBusCallFlags.NONE, 3000, None)
                return v.unpack()[0]
            except Exception:
                return None
        sources = prop(SC, "AvailableSourceTypes")
        devices = prop(RD, "AvailableDeviceTypes")
    except Exception as e:
        return {"screen": False, "input": False, "error": f"no desktop portal ({e})"}
    Gst.init(None)
    if not Gst.ElementFactory.find("pipewiresrc") and "pipewiresrc" in PIPELINE:
        return {"screen": False, "input": False,
                "error": "GStreamer PipeWire plugin missing (install gstreamer1.0-pipewire)"}
    screen = bool(sources and sources & SOURCE_MONITOR)
    inp = bool(devices and devices & DEVICE_POINTER)
    err = None if screen else "this desktop's portal does not offer screen sharing"
    return {"screen": screen, "input": screen and inp, "error": err}


class PortalSession:
    """One portal session: screen streams + (if available) pointer/keyboard."""

    def __init__(self, want_input=True, token_file=TOKEN_FILE, timeout=180):
        self.Gio, self.GLib, self.Gst, self.GstVideo = _gi()
        self.Gst.init(None)
        self.bus = self.Gio.bus_get_sync(self.Gio.BusType.SESSION, None)
        self.want_input = want_input
        self.token_file = token_file
        self.timeout = timeout
        self.session = None
        self.input = False
        self.streams = []           # [{"node", "w", "h"}]  (logical size, for pointer coordinates)
        self.fd = None
        self._loop = self.GLib.MainLoop()
        threading.Thread(target=self._loop.run, daemon=True, name="rd-portal-loop").start()

    # --------------------------------------------------------------- dbus
    def _request(self, iface, method, signature, args):
        """Calls a portal method that answers through a Request object's Response signal."""
        GLib = self.GLib
        token = "rd" + secrets.token_hex(6)
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        done = threading.Event()
        result = {}

        def on_response(conn, sender_name, obj, interface, signal, params, data):
            code, results = params.unpack()
            result["code"], result["results"] = code, results
            done.set()
        sub = self.bus.signal_subscribe(BUS_NAME, REQUEST, "Response", path, None,
                                        self.Gio.DBusSignalFlags.NONE, on_response, None)
        try:
            opts = dict(args[-1])
            opts["handle_token"] = GLib.Variant("s", token)
            args = tuple(args[:-1]) + (opts,)
            self.bus.call_sync(BUS_NAME, OBJ_PATH, iface, method, GLib.Variant(signature, args),
                               GLib.VariantType("(o)"), self.Gio.DBusCallFlags.NONE, 30000, None)
            if not done.wait(self.timeout):
                raise PortalError("no answer from the person at this computer")
        finally:
            self.bus.signal_unsubscribe(sub)
        if result["code"] == 1:
            raise PortalError("screen sharing was declined on this computer")
        if result["code"] != 0:
            raise PortalError(f"the desktop refused screen sharing (code {result['code']})")
        return result["results"]

    def _load_token(self):
        try:
            with open(self.token_file) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def _save_token(self, token):
        if not token:
            return
        try:
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
            with open(self.token_file, "w") as f:
                f.write(token)
            os.chmod(self.token_file, 0o600)
        except OSError as e:
            log.warning("cannot save portal token: %s", e)

    def _cursor_mode(self):
        try:
            v = self.bus.call_sync(BUS_NAME, OBJ_PATH, "org.freedesktop.DBus.Properties", "Get",
                                   self.GLib.Variant("(ss)", (SC, "AvailableCursorModes")),
                                   self.GLib.VariantType("(v)"), self.Gio.DBusCallFlags.NONE, 3000, None)
            modes = v.unpack()[0]
        except Exception:
            modes = 0
        return CURSOR_EMBEDDED if modes & CURSOR_EMBEDDED else CURSOR_HIDDEN

    # -------------------------------------------------------------- start
    def start(self):
        """Creates the session. May show the desktop's permission dialog (first time)."""
        V = self.GLib.Variant
        iface = RD if self.want_input else SC
        r = self._request(iface, "CreateSession", "(a{sv})",
                          ({"session_handle_token": V("s", "rd" + secrets.token_hex(6))},))
        self.session = r["session_handle"]
        restore = self._load_token()
        if self.want_input:
            opts = {"types": V("u", DEVICE_KEYBOARD | DEVICE_POINTER),
                    "persist_mode": V("u", PERSIST_UNTIL_REVOKED)}
            if restore:
                opts["restore_token"] = V("s", restore)
            self._request(RD, "SelectDevices", "(oa{sv})", (self.session, opts))
        src = {"types": V("u", SOURCE_MONITOR), "multiple": V("b", True),
               "cursor_mode": V("u", self._cursor_mode())}
        if not self.want_input:             # ScreenCast-only sessions persist on their own
            src["persist_mode"] = V("u", PERSIST_UNTIL_REVOKED)
            if restore:
                src["restore_token"] = V("s", restore)
        self._request(SC, "SelectSources", "(oa{sv})", (self.session, src))
        r = self._request(iface, "Start", "(osa{sv})", (self.session, "", {}))
        self._save_token(r.get("restore_token"))
        self.input = self.want_input and bool(r.get("devices", DEVICE_POINTER) & DEVICE_POINTER)
        self.streams = []
        for node, props in r.get("streams", []):
            w, h = props.get("size", (0, 0))
            self.streams.append({"node": int(node), "w": int(w), "h": int(h)})
        if not self.streams:
            raise PortalError("the desktop did not share any screen")
        # file descriptor of the PipeWire connection for these streams
        res, fds = self.bus.call_with_unix_fd_list_sync(
            BUS_NAME, OBJ_PATH, SC, "OpenPipeWireRemote", V("(oa{sv})", (self.session, {})),
            self.GLib.VariantType("(h)"), self.Gio.DBusCallFlags.NONE, 30000, None, None)
        self.fd = fds.get(res.unpack()[0])
        return self

    def close(self):
        if self.session:
            try:
                self.bus.call_sync(BUS_NAME, self.session, SESSION, "Close", None, None,
                                   self.Gio.DBusCallFlags.NONE, 3000, None)
            except Exception:
                pass
            self.session = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self._loop.quit()

    # -------------------------------------------------------------- input
    def _notify(self, method, signature, args):
        if not (self.session and self.input):
            return
        self.bus.call(BUS_NAME, OBJ_PATH, RD, method, self.GLib.Variant(signature, (self.session, {}) + args),
                      None, self.Gio.DBusCallFlags.NONE, 3000, None, None, None)

    def pointer_move(self, stream, x, y):
        self._notify("NotifyPointerMotionAbsolute", "(oa{sv}udd)", (stream["node"], float(x), float(y)))

    def pointer_button(self, button, pressed):
        self._notify("NotifyPointerButton", "(oa{sv}iu)", (button, 1 if pressed else 0))

    def pointer_scroll(self, axis, steps):
        self._notify("NotifyPointerAxisDiscrete", "(oa{sv}ui)", (axis, steps))

    def key(self, keysym, pressed):
        self._notify("NotifyKeyboardKeysym", "(oa{sv}iu)", (keysym, 1 if pressed else 0))


class WaylandCapture:
    """Same interface as host.ScreenCapture, reading the portal's PipeWire streams."""

    def __init__(self, session):
        self.s = session
        Gst, GstVideo = session.Gst, session.GstVideo
        self.Gst, self.GstVideo = Gst, GstVideo
        self.pipes = []
        for st in session.streams:
            desc = PIPELINE.format(fd=os.dup(session.fd) if session.fd is not None else -1, node=st["node"])
            pipe = Gst.parse_launch(desc)
            pipe.set_state(Gst.State.PLAYING)
            self.pipes.append((pipe, pipe.get_by_name("sink")))
        self.monitor = 1                              # 1-based, like ScreenCapture
        self._last = [None] * len(self.pipes)

    @property
    def monitors(self):
        return [{"w": st["w"], "h": st["h"]} for st in self.s.streams]

    def set_monitor(self, idx):
        if 0 <= idx < len(self.pipes):
            self.monitor = idx + 1

    @property
    def geometry(self):
        st = self.s.streams[self.monitor - 1]
        return {"left": 0, "top": 0, "width": st["w"], "height": st["h"], "stream": st}

    def grab(self):
        from PIL import Image
        i = self.monitor - 1
        pipe, sink = self.pipes[i]
        sample = sink.emit("try-pull-sample", 500 * self.Gst.MSECOND)
        if sample is None:
            if self._last[i] is not None:             # screen unchanged: PipeWire sends nothing
                return self._last[i]
            sample = sink.emit("try-pull-sample", 5 * self.Gst.SECOND)
            if sample is None:
                raise RuntimeError("no picture from the screen stream")
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        buf = sample.get_buffer()
        ok, m = buf.map(self.Gst.MapFlags.READ)
        try:
            img = Image.frombuffer("RGB", (info.width, info.height), bytes(m.data), "raw", "RGB",
                                   info.stride[0], 1).copy()
        finally:
            buf.unmap(m)
        self._last[i] = img
        return img

    def close(self):
        for pipe, _ in self.pipes:
            pipe.set_state(self.Gst.State.NULL)


# keysyms for the special key names the viewers send
KEYSYMS = {
    "enter": 0xff0d, "return": 0xff0d, "tab": 0xff09, "backspace": 0xff08, "escape": 0xff1b,
    "delete": 0xffff, "insert": 0xff63, "home": 0xff50, "end": 0xff57, "pageup": 0xff55,
    "pagedown": 0xff56, "left": 0xff51, "up": 0xff52, "right": 0xff53, "down": 0xff54,
    "shift": 0xffe1, "control": 0xffe3, "alt": 0xffe9, "meta": 0xffeb, "space": 0x20,
    "capslock": 0xffe5, "printscreen": 0xff61, "menu": 0xff67,
    **{f"f{i}": 0xffbe + i - 1 for i in range(1, 21)},
}
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}       # Linux evdev BTN_* codes


def char_keysym(ch):
    o = ord(ch)
    if 0x20 <= o <= 0x7e or 0xa0 <= o <= 0xff:
        return o
    return 0x01000000 | o                                       # Unicode keysym


class PortalInjector:
    """Same interface as host.InputInjector, sending input through the portal."""

    def __init__(self, session):
        self.s = session

    def mouse(self, ev, geo):
        st = geo.get("stream")
        if not st:
            return
        x = max(0.0, min(st["w"] - 1, ev["x"] * st["w"]))
        y = max(0.0, min(st["h"] - 1, ev["y"] * st["h"]))
        self.s.pointer_move(st, x, y)
        act = ev.get("action")
        if act in ("down", "up"):
            self.s.pointer_button(BUTTONS.get(ev.get("btn") or "left", 0x110), act == "down")
        elif act == "wheel":
            if ev.get("dy"):
                self.s.pointer_scroll(0, -int(ev["dy"]))          # wheel up = negative steps
            if ev.get("dx"):
                self.s.pointer_scroll(1, int(ev["dx"]))

    def key(self, ev):
        name = (ev.get("key") or "").lower()
        down = bool(ev.get("down"))
        text = ev.get("text") or ""
        if name in KEYSYMS:
            sym = KEYSYMS[name]
        elif len(text) == 1:
            sym = char_keysym(text)
        elif len(name) == 1:
            sym = char_keysym(name)
        else:
            return
        self.s.key(sym, down)
