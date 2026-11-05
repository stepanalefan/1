"""Host side: shares this computer's screen and accepts remote input.

Every session must pass the password check and (unless unattended mode is
on) be accepted by the local user. While a session is active the GUI shows a
visible banner and the local user can end it at any time.
"""
import hashlib, io, json, logging, os, socket, struct, threading, time

from . import protocol as P

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
        if name in SPECIAL:
            k = getattr(self._Key, SPECIAL[name], None)
        elif ev.get("text") and len(ev["text"]) == 1:
            k = self._KeyCode.from_char(ev["text"])
        elif len(name) == 1:
            k = self._KeyCode.from_char(name)
        else:
            return
        if k is None:
            return
        (self._k.press if ev.get("down") else self._k.release)(k)


# ---------------------------------------------------------------- session
class HostSession:
    def __init__(self, chan, cfg, events):
        self.chan, self.cfg, self.ev = chan, cfg, events
        self.quality, self.fps = 60, 15
        self.running = True
        self.view_only = cfg.get("view_only", False)
        self._file = None
        self._force_full = True

    def run(self):
        self.cap = ScreenCapture()
        try:
            self.inj = None if self.view_only else InputInjector()
        except Exception as e:
            log.warning("input injection unavailable: %s", e)
            self.inj = None
        threading.Thread(target=self._recv_loop, daemon=True).start()
        try:
            self._send_loop()
        finally:
            self.stop()

    def stop(self):
        if self.running:
            self.running = False
            self.chan.close()
            self.ev("session_end", None)

    def _send_info(self):
        g = self.cap.geometry
        self.chan.send(P.FRAME_INFO, {"w": g["width"], "h": g["height"],
                                      "monitors": self.cap.monitors,
                                      "monitor": self.cap.monitor - 1})

    def _send_loop(self):
        from PIL import Image  # noqa
        hashes = {}
        self._send_info()
        while self.running:
            t0 = time.time()
            img = self.cap.grab()
            if self._force_full:
                hashes.clear()
                self._force_full = False
            w, h = img.size
            raw = img.tobytes()
            row = w * 3
            sent = 0
            for ty in range(0, h, TILE):
                th = min(TILE, h - ty)
                rows = [raw[(ty + r) * row:(ty + r + 1) * row] for r in range(th)]
                for tx in range(0, w, TILE):
                    tw = min(TILE, w - tx)
                    hsh = hashlib.blake2b(b"".join(r[tx * 3:(tx + tw) * 3] for r in rows),
                                          digest_size=8).digest()
                    if hashes.get((tx, ty)) == hsh:
                        continue
                    hashes[(tx, ty)] = hsh
                    buf = io.BytesIO()
                    img.crop((tx, ty, tx + tw, ty + th)).save(buf, "JPEG", quality=self.quality)
                    self.chan.send(P.TILE, struct.pack(">HH", tx, ty) + buf.getvalue())
                    sent += 1
            if sent:
                self.chan.send(P.FRAME_END)
            time.sleep(max(0, 1 / self.fps - (time.time() - t0)))

    def _recv_loop(self):
        try:
            while self.running:
                t, data = self.chan.recv()
                if t == P.BYE:
                    break
                self._dispatch(t, data)
        except Exception as e:
            log.info("session closed: %s", e)
        self.stop()

    def _dispatch(self, t, data):
        if t == P.MOUSE and self.inj:
            self.inj.mouse(json.loads(data), self.cap.geometry)
        elif t == P.KEY and self.inj:
            self.inj.key(json.loads(data))
        elif t == P.SET_MONITOR:
            self.cap.set_monitor(json.loads(data)["index"])
            self._force_full = True
            self._send_info()
        elif t == P.SET_QUALITY:
            q = json.loads(data)
            self.quality = max(10, min(95, int(q.get("quality", self.quality))))
            self.fps = max(1, min(60, int(q.get("fps", self.fps))))
            self._force_full = True
        elif t == P.CLIPBOARD:
            self.ev("clipboard", data.decode("utf-8", "replace"))
        elif t == P.CHAT:
            self.ev("chat", data.decode("utf-8", "replace"))
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
            self.ev("file", self._file[1])
            self._file = None


# ------------------------------------------------------------------ host
class Host:
    """Listens directly on a TCP port and/or registers with a relay.

    events(name, arg) is called for: id, status, accept (returns bool),
    session_start, session_end, clipboard, chat, file.
    """

    def __init__(self, password, events, direct_port=7071, relay=None,
                 host_id=None, unattended=False, view_only=False):
        self.password, self.ev = password, events
        self.direct_port, self.relay, self.host_id = direct_port, relay, host_id
        self.unattended, self.view_only = unattended, view_only
        self.session = None
        self._fails = {}

    def start(self):
        if self.direct_port:
            threading.Thread(target=self._listen, daemon=True).start()
        if self.relay:
            threading.Thread(target=self._relay_loop, daemon=True).start()

    def _listen(self):
        srv = socket.create_server(("0.0.0.0", self.direct_port))
        self.ev("status", f"direct: listening on port {self.direct_port}")
        while True:
            conn, peer = srv.accept()
            threading.Thread(target=self._incoming, args=(conn, peer[0]), daemon=True).start()

    def _relay_loop(self):
        host, port = self.relay
        while True:
            try:
                ctrl = socket.create_connection((host, port), timeout=10)
                P.send_line(ctrl, {"role": "host", "id": self.host_id})
                r = P.recv_line(ctrl)
                self.host_id = r["id"]
                self.ev("id", self.host_id)
                self.ev("status", f"online via relay {host}:{port}")
                ctrl.settimeout(None)
                threading.Thread(target=self._ping, args=(ctrl,), daemon=True).start()
                while True:
                    msg = P.recv_line(ctrl)
                    if "connect" in msg:
                        d = socket.create_connection((host, port), timeout=10)
                        P.send_line(d, {"role": "host-data", "token": msg["connect"]})
                        threading.Thread(target=self._incoming, args=(d, "relay"),
                                         daemon=True).start()
            except Exception as e:
                self.ev("status", f"relay offline ({e}); retrying...")
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
        if self.session and self.session.running:
            chan.close()
            return
        if not self.unattended and not self.ev("accept", peer):
            chan.close()
            return
        self.session = HostSession(chan, {"view_only": self.view_only}, self.ev)
        self.ev("session_start", peer)
        self.session.run()

    def end_session(self):
        if self.session:
            self.session.stop()
