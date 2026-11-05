"""Relay / rendezvous server.

Run headless:   python -m remote_desk.relay --port 7070
Run with GUI:   python -m remote_desk.server_gui

Hosts keep a control connection and are addressed by a 9-digit ID. When a
viewer asks for an ID the relay tells the host, the host opens a data
connection with the given token, and the relay pipes bytes between the two.
Traffic is end-to-end encrypted; the relay never sees the password or keys.
"""
import argparse, logging, os, secrets, socket, threading, time

from .protocol import recv_line, send_line

log = logging.getLogger("relay")


class Relay:
    def __init__(self, host="0.0.0.0", port=7070, on_log=None):
        self.addr = (host, port)
        self.hosts = {}     # id -> {"sock", "ip", "since"}
        self.pending = {}   # token -> (viewer sock, time, host_id, viewer ip)
        self.sessions = {}  # sid -> {"host_id", "viewer", "since", "bytes", "socks"}
        self.lock = threading.Lock()
        self.on_log = on_log or (lambda msg: log.info(msg))
        self._srv = None
        self.running = False
        self.total_sessions = 0

    # ----------------------------------------------------------- lifecycle
    def start(self):
        """Bind and serve in a background thread (raises if the port is busy)."""
        self._srv = socket.create_server(self.addr)
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        self.on_log(f"Server started on {self.addr[0]}:{self.addr[1]}")

    def serve_forever(self):
        self.start()
        while self.running:
            time.sleep(1)

    def stop(self):
        self.running = False
        if self._srv:
            self._srv.close()
        with self.lock:
            socks = [h["sock"] for h in self.hosts.values()]
            socks += [p[0] for p in self.pending.values()]
            for s in self.sessions.values():
                socks += s["socks"]
            self.hosts.clear(); self.pending.clear(); self.sessions.clear()
        for s in socks:
            _close(s)
        self.on_log("Server stopped")

    def disconnect_session(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
        if s:
            for x in s["socks"]:
                _close(x)
            self.on_log(f"Session {s['viewer']} -> {s['host_id']} closed by administrator")

    def snapshot(self):
        with self.lock:
            hosts = [(i, h["ip"], h["since"]) for i, h in self.hosts.items()]
            sess = [(sid, s["host_id"], s["viewer"], s["since"], s["bytes"])
                    for sid, s in self.sessions.items()]
        return hosts, sess

    # ------------------------------------------------------------- serving
    def _accept_loop(self):
        while self.running:
            try:
                conn, peer = self._srv.accept()
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._handle, args=(conn, peer[0]), daemon=True).start()

    def _reaper(self):
        while self.running:
            time.sleep(10)
            with self.lock:
                for tok, p in list(self.pending.items()):
                    if time.time() - p[1] > 60:
                        del self.pending[tok]
                        _close(p[0])

    def _handle(self, conn, ip):
        try:
            conn.settimeout(30)
            msg = recv_line(conn)
            role = msg.get("role")
            if role == "host":
                self._host_control(conn, msg, ip)
            elif role == "viewer":
                self._viewer(conn, str(msg.get("id", "")), ip)
            elif role == "host-data":
                self._host_data(conn, msg.get("token", ""))
            else:
                conn.close()
        except Exception as e:
            log.debug("%s: %s", ip, e)
            _close(conn)

    def _host_control(self, conn, msg, ip):
        want = str(msg.get("id") or "")
        with self.lock:
            if not (want.isdigit() and len(want) == 9) or want in self.hosts:
                want = self._new_id()
            self.hosts[want] = {"sock": conn, "ip": ip, "since": time.time()}
        send_line(conn, {"ok": True, "id": want})
        self.on_log(f"Desk {want} online ({ip})")
        conn.settimeout(90)  # clients ping every 20 s
        try:
            while conn.recv(64):
                pass
        except OSError:
            pass
        finally:
            with self.lock:
                if self.hosts.get(want, {}).get("sock") is conn:
                    del self.hosts[want]
            if self.running:
                self.on_log(f"Desk {want} offline")
            _close(conn)

    def _new_id(self):
        while True:
            i = str(secrets.randbelow(900_000_000) + 100_000_000)
            if i not in self.hosts:
                return i

    def _viewer(self, conn, host_id, ip):
        with self.lock:
            h = self.hosts.get(host_id)
            if h:
                token = secrets.token_hex(16)
                self.pending[token] = (conn, time.time(), host_id, ip)
        if not h:
            self.on_log(f"{ip} asked for desk {host_id}: not online")
            send_line(conn, {"ok": False, "error": "Desk is offline or the ID is unknown"})
            conn.close()
            return
        self.on_log(f"{ip} is connecting to desk {host_id}")
        try:
            send_line(h["sock"], {"connect": token, "from": ip})
        except OSError:
            send_line(conn, {"ok": False, "error": "Desk unreachable"})
            conn.close()

    def _host_data(self, conn, token):
        with self.lock:
            item = self.pending.pop(token, None)
        if not item:
            conn.close()
            return
        viewer, _, host_id, vip = item
        send_line(viewer, {"ok": True})
        conn.settimeout(None)
        viewer.settimeout(None)
        sid = secrets.token_hex(4)
        sess = {"host_id": host_id, "viewer": vip, "since": time.time(), "bytes": 0,
                "socks": (conn, viewer)}
        with self.lock:
            self.sessions[sid] = sess
            self.total_sessions += 1
        t = threading.Thread(target=_pipe, args=(conn, viewer, sess), daemon=True)
        t.start()
        _pipe(viewer, conn, sess)
        t.join()
        with self.lock:
            self.sessions.pop(sid, None)
        mb = sess["bytes"] / 1e6
        self.on_log(f"Session {vip} -> {host_id} ended ({mb:.1f} MB)")


def _close(s):
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def _pipe(a, b, sess):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
            sess["bytes"] += len(d)
    except OSError:
        pass
    for s in (a, b):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="RemoteDesk relay server (headless)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RD_RELAY_PORT", 7070)))
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(message)s")
    Relay(a.bind, a.port).serve_forever()


if __name__ == "__main__":
    main()
