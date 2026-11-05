"""Relay / rendezvous server (run on a machine reachable by both sides).

  python -m remote_desk.relay --port 7070

Hosts keep a control connection and are addressed by a 9-digit ID. When a
viewer asks for an ID the relay tells the host, the host opens a data
connection with the given token, and the relay pipes bytes between the two.
Traffic is end-to-end encrypted; the relay never sees the password or keys.
"""
import argparse, logging, os, secrets, socket, threading, time

from .protocol import recv_line, send_line

log = logging.getLogger("relay")


class Relay:
    def __init__(self, host="0.0.0.0", port=7070):
        self.addr = (host, port)
        self.hosts = {}     # id -> control socket
        self.pending = {}   # token -> (viewer socket, time)
        self.lock = threading.Lock()

    def serve_forever(self):
        srv = socket.create_server(self.addr, reuse_port=False)
        log.info("relay listening on %s:%d", *self.addr)
        threading.Thread(target=self._reaper, daemon=True).start()
        while True:
            conn, peer = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._handle, args=(conn, peer), daemon=True).start()

    def _reaper(self):
        while True:
            time.sleep(10)
            with self.lock:
                for tok, (s, t) in list(self.pending.items()):
                    if time.time() - t > 30:
                        del self.pending[tok]
                        s.close()

    def _handle(self, conn, peer):
        try:
            conn.settimeout(30)
            msg = recv_line(conn)
            role = msg.get("role")
            if role == "host":
                self._host_control(conn, msg)
            elif role == "viewer":
                self._viewer(conn, str(msg.get("id", "")))
            elif role == "host-data":
                self._host_data(conn, msg.get("token", ""))
            else:
                conn.close()
        except Exception as e:
            log.debug("%s: %s", peer, e)
            conn.close()

    def _host_control(self, conn, msg):
        want = str(msg.get("id") or "")
        with self.lock:
            if not (want.isdigit() and len(want) == 9) or want in self.hosts:
                want = self._new_id()
            self.hosts[want] = conn
        send_line(conn, {"ok": True, "id": want})
        log.info("host %s online", want)
        conn.settimeout(None)
        try:
            while conn.recv(64):   # keep-alive pings; EOF => host gone
                pass
        finally:
            with self.lock:
                if self.hosts.get(want) is conn:
                    del self.hosts[want]
            log.info("host %s offline", want)
            conn.close()

    def _new_id(self):
        while True:
            i = str(secrets.randbelow(900_000_000) + 100_000_000)
            if i not in self.hosts:
                return i

    def _viewer(self, conn, host_id):
        with self.lock:
            ctrl = self.hosts.get(host_id)
            if ctrl:
                token = secrets.token_hex(16)
                self.pending[token] = (conn, time.time())
        if not ctrl:
            send_line(conn, {"ok": False, "error": "host offline or unknown ID"})
            conn.close()
            return
        try:
            send_line(ctrl, {"connect": token})
        except OSError:
            send_line(conn, {"ok": False, "error": "host unreachable"})
            conn.close()

    def _host_data(self, conn, token):
        with self.lock:
            item = self.pending.pop(token, None)
        if not item:
            conn.close()
            return
        viewer = item[0]
        send_line(viewer, {"ok": True})
        conn.settimeout(None)
        viewer.settimeout(None)
        threading.Thread(target=_pipe, args=(conn, viewer), daemon=True).start()
        _pipe(viewer, conn)


def _pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    for s in (a, b):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="RemoteDesk relay server")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RD_RELAY_PORT", 7070)))
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(message)s")
    Relay(a.bind, a.port).serve_forever()


if __name__ == "__main__":
    main()
