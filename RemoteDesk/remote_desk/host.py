"""Host side: shares this computer's screen and accepts remote input.

Every session must pass the password check and (unless unattended mode is
on) be accepted by the local user. While a session is active the GUI shows a
visible banner and the local user can end it at any time.
"""
import hashlib, io, json, logging, os, socket, struct, sys, threading, time

from . import protocol as P
from .netutil import clean_ip, connect, format_hostport, listen

log = logging.getLogger("host")
TILE = 64
DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads", "RemoteDesk")


# --------------------------------------------------------------- capture
class ScreenCapture:
    def __init__(self):
        import mss
        self._mss = mss.mss()
        self.monitor = 1 if len(self._mss.monitors) > 1 else 0

    @property
    def monitors(self):
        return [{"w": m["width"], "h": m["height"]} for m in self._mss.monitors[1:]]

    def set_monitor(self, idx):
        if 0 <= idx < len(self._mss.monitors) - 1:
            self.monitor = idx + 1

    @property
    def geometry(self):
        return self._mss.monitors[self.monitor]

    def grab(self):
        from PIL import Image
        shot = self._mss.grab(self.geometry)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# ------------------------------------------------------------------ input
SPECIAL = {
    "enter": "enter", "return": "enter", "tab": "tab", "backspace": "backspace",
    "escape": "esc", "delete": "delete", "insert": "insert", "home": "home",
    "end": "end", "pageup": "page_up", "pagedown": "page_down", "left": "left",
    "right": "right", "up": "up", "down": "down", "shift": "shift",
    "control": "ctrl", "alt": "alt", "meta": "cmd", "space": "space",
    "capslock": "caps_lock", "printscreen": "print_screen", "menu": "menu",
    **{f"f{i}": f"f{i}" for i in range(1, 21)},
}


class InputInjector:
    def __init__(self):
        from pynput import mouse, keyboard
        self._m, self._k = mouse.Controller(), keyboard.Controller()
        self._Button, self._Key, self._KeyCode = mouse.Button, keyboard.Key, keyboard.KeyCode
        self._mods = set()

    def mouse(self, ev, geo):
        x = geo["left"] + int(ev["x"] * geo["width"])
        y = geo["top"] + int(ev["y"] * geo["height"])
        self._m.position = (x, y)
        act = ev.get("action")
        btn = {"left": self._Button.left, "right": self._Button.right,
               "middle": self._Button.middle}.get(ev.get("btn"), self._Button.left)
        if act == "down":
            self._m.press(btn)
        elif act == "up":
            self._m.release(btn)
        elif act == "wheel":
            self._m.scroll(ev.get("dx", 0), ev.get("dy", 0))

    def key(self, ev):
        name = (ev.get("key") or "").lower()
        down = bool(ev.get("down"))
        if name in ("control", "alt", "meta"):
            (self._mods.add if down else self._mods.discard)(name)
        text = ev.get("text") or ""
        if name in SPECIAL:
            k = getattr(self._Key, SPECIAL[name], None)
        elif len(text) == 1 and self._mods and text.isascii() and text.isalnum() \
                and sys.platform == "win32":
            # Ctrl/Alt/Win + letter: use the virtual-key code so shortcuts work
            # (a Unicode-injected character is not seen as Ctrl+C etc.)
            k = self._KeyCode.from_vk(ord(text.upper()))
        elif len(text) == 1:
            k = self._KeyCode.from_char(text)
        elif len(name) == 1:
            k = self._KeyCode.from_char(name)
        else:
            return
        if k is None:
            return
        (self._k.press if down else self._k.release)(k)


# ----------------------------------------------------------------- frames
class Frame:
    """One captured image of a monitor with per-tile hashes, shared by all viewers."""

    def __init__(self, seq, img, geometry):
        self.seq, self.img, self.geometry = seq, img, geometry
        self.w, self.h = img.size
        raw = img.tobytes()
        row = self.w * 3
        self.hashes = {}
        for ty in range(0, self.h, TILE):
            th = min(TILE, self.h - ty)
            rows = [raw[(ty + r) * row:(ty + r + 1) * row] for r in range(th)]
            for tx in range(0, self.w, TILE):
                tw = min(TILE, self.w - tx)
                self.hashes[(tx, ty)] = hashlib.blake2b(
                    b"".join(r[tx * 3:(tx + tw) * 3] for r in rows), digest_size=8).digest()


class CaptureHub:
    """Grabs the monitors the viewers are watching, at the highest rate any of
    them asked for. Runs while at least one viewer is connected."""

    def __init__(self, host):
        self.host = host
        self.cond = threading.Condition()
        self.frames = {}            # monitor index (0-based) -> Frame
        self.seq = 0
        self.monitors = []          # [{"w", "h"}]
        self.default_monitor = 0
        self.ready = threading.Event()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            cap = ScreenCapture()       # mss objects must stay in the thread that made them
            self.monitors = cap.monitors
            self.default_monitor = max(0, cap.monitor - 1)
        except Exception as e:
            log.error("screen capture unavailable: %s", e)
            self.running = False
            self.ready.set()
            return
        self.ready.set()
        while self.running:
            t0 = time.time()
            viewers = self.host.viewer_list()
            if not viewers:
                time.sleep(0.05)
                continue
            fps = max(v.fps for v in viewers)
            for m in {v.monitor for v in viewers}:
                try:
                    cap.set_monitor(m)
                    frame = Frame(0, cap.grab(), dict(cap.geometry))
                except Exception as e:
                    log.warning("capture failed: %s", e)
                    continue
                with self.cond:
                    self.seq += 1
                    frame.seq = self.seq
                    self.frames[m] = frame
                    self.cond.notify_all()
            time.sleep(max(0.0, 1 / fps - (time.time() - t0)))

    def geometry(self, monitor):
        f = self.frames.get(monitor)
        return f.geometry if f else None

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify_all()


# ---------------------------------------------------------------- viewers
class ViewerConn:
    """One connected viewer: its own quality, frame rate, monitor and tile state."""

    def __init__(self, host, chan, peer, may_control):
        self.host, self.chan, self.peer = host, chan, peer
        self.may_control = may_control
        self.control = False
        self.name = peer
        self.quality, self.fps = 60, 15
        self.monitor = 0                  # set by Host.add_viewer
        self.running = True
        self.since = time.time()
        self._sent = {}
        self._force_full = True
        self._info_dirty = True
        self._last_seq = -1
        self._file = None

    def start(self):
        threading.Thread(target=self._send_loop, daemon=True).start()
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.chan.close()
        if self._file:
            self._file[0].close()
        hub = self.host.hub
        if hub:
            with hub.cond:
                hub.cond.notify_all()
        self.host.remove_viewer(self)

    def send(self, t, payload=b""):
        try:
            self.chan.send(t, payload)
        except Exception:
            self.stop()

    def _send_loop(self):
        hub = self.host.hub
        info = None
        try:
            while self.running:
                with hub.cond:
                    hub.cond.wait_for(lambda: not self.running or self._info_dirty or self._force_full
                                      or (hub.frames.get(self.monitor) and
                                          hub.frames[self.monitor].seq != self._last_seq), timeout=1)
                frame = hub.frames.get(self.monitor)
                if not self.running or frame is None:
                    continue
                t0 = time.time()
                if self._info_dirty or info != (frame.w, frame.h):
                    info = (frame.w, frame.h)
                    self._info_dirty = False
                    self._sent.clear()
                    self.chan.send(P.FRAME_INFO, {"w": frame.w, "h": frame.h, "monitors": hub.monitors,
                                                  "monitor": self.monitor})
                if self._force_full:
                    self._force_full = False
                    self._sent.clear()
                self._last_seq = frame.seq
                sent = 0
                for (tx, ty), hsh in frame.hashes.items():
                    if self._sent.get((tx, ty)) == hsh:
                        continue
                    self._sent[(tx, ty)] = hsh
                    buf = io.BytesIO()
                    frame.img.crop((tx, ty, min(tx + TILE, frame.w), min(ty + TILE, frame.h))) \
                        .save(buf, "JPEG", quality=self.quality)
                    self.chan.send(P.TILE, struct.pack(">HH", tx, ty) + buf.getvalue())
                    sent += 1
                if sent:
                    self.chan.send(P.FRAME_END)
                time.sleep(max(0.0, 1 / self.fps - (time.time() - t0)))
        except Exception as e:
            log.info("viewer %s: %s", self.peer, e)
        self.stop()

    def _recv_loop(self):
        try:
            while self.running:
                t, data = self.chan.recv()
                if t == P.BYE:
                    break
                self._dispatch(t, data)
        except Exception as e:
            log.info("viewer %s closed: %s", self.peer, e)
        self.stop()

    def _dispatch(self, t, data):
        host = self.host
        if t in (P.MOUSE, P.KEY, P.CLIPBOARD, P.FILE_BEGIN, P.FILE_DATA, P.FILE_END) \
                and not self.control:
            return                                    # only the controlling viewer may do these
        if t == P.MOUSE:
            inj, geo = host.injector(), host.hub.geometry(self.monitor)
            if inj and geo:
                inj.mouse(json.loads(data), geo)
        elif t == P.KEY:
            inj = host.injector()
            if inj:
                inj.key(json.loads(data))
        elif t == P.SET_MONITOR:
            idx = int(json.loads(data).get("index", 0))
            if 0 <= idx < max(1, len(host.hub.monitors)):
                self.monitor = idx
                self._info_dirty = True
        elif t == P.SET_QUALITY:
            q = json.loads(data)
            self.quality = max(10, min(95, int(q.get("quality", self.quality))))
            self.fps = max(1, min(60, int(q.get("fps", self.fps))))
            self._force_full = True
        elif t == P.HELLO:
            hello = json.loads(data)
            self.name = str(hello.get("name") or self.peer)[:64]
            host.update_roles()
        elif t == P.CLIPBOARD:
            host.ev("clipboard", data.decode("utf-8", "replace"))
        elif t == P.CHAT:
            host.on_chat(self, data)
        elif t == P.FILE_BEGIN:
            meta = json.loads(data)
            os.makedirs(DOWNLOADS, exist_ok=True)
            name = os.path.basename(meta["name"]) or "file"
            path = os.path.join(DOWNLOADS, name)
            self._file = (open(path, "wb"), path)
        elif t == P.FILE_DATA and self._file:
            self._file[0].write(data)
        elif t == P.FILE_END and self._file:
            self._file[0].close()
            host.ev("file", self._file[1])
            self._file = None


# ------------------------------------------------------------------ host
class Host:
    """Listens directly on a TCP port and/or registers with a relay.

    events(name, arg) is called for: id, status, accept (returns bool),
    session_start, session_end, clipboard, chat, file.
    """

    def __init__(self, password, events, direct_port=7071, relay=None,
                 host_id=None, unattended=False, view_only=False, machine_key=None, name=None):
        self.password, self.ev = password, events
        self.machine_key = machine_key
        self.name = name or socket.gethostname()
        self.direct_port, self.relay, self.host_id = direct_port, relay, host_id
        self.unattended, self.view_only = unattended, view_only
        self.viewers = []
        self.hub = None
        self._vlock = threading.RLock()
        self._injector = None
        self._fails = {}
        self._gen = 0
        self._ctrl = None

    def start(self):
        if self.direct_port:
            threading.Thread(target=self._listen, daemon=True).start()
        if self.relay:
            threading.Thread(target=self._relay_loop, args=(self._gen,), daemon=True).start()

    def set_relay(self, relay):
        """Switch to another relay server (None = direct connections only)."""
        self._gen += 1
        if self._ctrl:
            try:
                self._ctrl.shutdown(socket.SHUT_RDWR)
                self._ctrl.close()
            except OSError:
                pass
        self.relay = relay
        if relay:
            threading.Thread(target=self._relay_loop, args=(self._gen,), daemon=True).start()

    def _listen(self):
        try:
            srv = listen(self.direct_port)
        except OSError as e:
            self.ev("status", f"Direct port {self.direct_port} unavailable: {e}")
            return
        self.ev("status", f"Direct connections: port {self.direct_port}")
        while True:
            conn, peer = srv.accept()
            threading.Thread(target=self._incoming, args=(conn, clean_ip(peer[0])), daemon=True).start()

    def _relay_loop(self, gen):
        host, port = self.relay
        while gen == self._gen:
            try:
                ctrl = connect(host, port, timeout=10)
                self._ctrl = ctrl
                P.send_line(ctrl, {"role": "host", "id": self.host_id,
                                   "key": self.machine_key, "name": self.name})
                r = P.recv_line(ctrl)
                if gen != self._gen:
                    ctrl.close()
                    return
                self.host_id = r["id"]
                self.ev("id", self.host_id)
                self.ev("online", True)
                ctrl.settimeout(None)
                threading.Thread(target=self._ping, args=(ctrl,), daemon=True).start()
                while True:
                    msg = P.recv_line(ctrl)
                    if "id" in msg and "connect" not in msg:      # the admin changed our ID
                        self.host_id = str(msg["id"])
                        self.ev("id", self.host_id)
                    if "connect" in msg:
                        d = connect(host, port, timeout=10)
                        P.send_line(d, {"role": "host-data", "token": msg["connect"]})
                        who = f'{msg.get("from", "unknown")} (via server)'
                        threading.Thread(target=self._incoming, args=(d, who),
                                         daemon=True).start()
            except Exception as e:
                if gen != self._gen:
                    return
                self.ev("online", False)
                self.ev("status", f"Server {format_hostport(host, port)} unreachable ({e}); retrying...")
                time.sleep(5)

    @staticmethod
    def _ping(s):
        try:
            while True:
                time.sleep(20)
                s.sendall(b".")
        except OSError:
            pass

    def _incoming(self, sock, peer):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # simple brute-force throttling per peer
        n, last = self._fails.get(peer, (0, 0))
        if n >= 5 and time.time() - last < 60 * min(n - 4, 30):
            sock.close()
            return
        try:
            sock.settimeout(30)
            chan = P.host_handshake(sock, self.password)
            sock.settimeout(None)
        except P.AuthError:
            self._fails[peer] = (n + 1, time.time())
            self.ev("status", f"rejected connection from {peer}: wrong password")
            sock.close()
            return
        except Exception:
            sock.close()
            return
        self._fails.pop(peer, None)
        may_control = not self.view_only
        if not self.unattended:
            # the GUI is told whether someone else already has control
            res = self.ev("accept", {"peer": peer, "controller": self.controller_name()})
            if not res:
                chan.close()
                return
            if isinstance(res, dict) and not res.get("control", True):
                may_control = False
        self.add_viewer(ViewerConn(self, chan, peer, may_control))

    # ------------------------------------------------------------ viewers
    def viewer_list(self):
        with self._vlock:
            return list(self.viewers)

    def add_viewer(self, v):
        with self._vlock:
            if self.hub is None or not self.hub.running:
                self.hub = CaptureHub(self)
            hub = self.hub
        hub.ready.wait(10)
        v.monitor = hub.default_monitor
        with self._vlock:
            self.viewers.append(v)
        v.start()
        self.update_roles()
        self.ev("session_start", v.peer)

    def remove_viewer(self, v):
        with self._vlock:
            if v not in self.viewers:
                return
            self.viewers.remove(v)
            empty = not self.viewers
            if empty and self.hub:
                self.hub.stop()
                self.hub = None
        self.update_roles()
        if empty:
            self.ev("session_end", None)

    def update_roles(self):
        """The first viewer that may control gets control; everyone else views."""
        with self._vlock:
            viewers = list(self.viewers)
            controller = next((v for v in viewers if v.may_control and not self.view_only), None)
            for v in viewers:
                v.control = v is controller
        names = [{"name": v.name, "control": v.control} for v in viewers]
        for v in viewers:
            v.send(P.ROLE, {"control": v.control,
                            "controller": controller.name if controller else None,
                            "viewers": names})
        self.ev("viewers", [{"peer": v.peer, "name": v.name, "control": v.control,
                             "since": v.since} for v in viewers])

    def controller_name(self):
        with self._vlock:
            return next((v.name for v in self.viewers if v.control), None)

    def injector(self):
        if self.view_only:
            return None
        if self._injector is None:
            try:
                self._injector = InputInjector()
            except Exception as e:
                log.warning("input injection unavailable: %s", e)
                self._injector = False
        return self._injector or None

    # --------------------------------------------------------------- chat
    def on_chat(self, viewer, data):
        text, sender = _parse_chat(data)
        msg = {"from": sender or viewer.name, "text": text, "ts": time.time()}
        self.ev("chat", msg)
        for v in self.viewer_list():
            if v is not viewer:
                v.send(P.CHAT, msg)

    def send_chat(self, text):
        """Message from the person at this computer to every viewer."""
        msg = {"from": self.name, "text": text, "ts": time.time()}
        for v in self.viewer_list():
            v.send(P.CHAT, msg)
        return msg

    def end_session(self):
        for v in self.viewer_list():
            v.stop()

    @property
    def session(self):          # kept for older callers: truthy while someone is connected
        return bool(self.viewer_list())


def _parse_chat(data):
    """CHAT payloads are JSON {"from", "text"}; older clients send plain text."""
    s = data.decode("utf-8", "replace")
    if s.startswith("{"):
        try:
            m = json.loads(s)
            return str(m.get("text", "")), m.get("from")
        except ValueError:
            pass
    return s, None
