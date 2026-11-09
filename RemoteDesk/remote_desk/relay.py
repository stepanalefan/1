"""Relay / rendezvous server.

Run headless:   python -m remote_desk.relay --port 7070
Run with GUI:   python -m remote_desk.server_gui

Hosts keep a control connection and are addressed by a 9-digit ID. When a
viewer asks for an ID the relay tells the host, the host opens a data
connection with the given token, and the relay pipes bytes between the two.
Traffic is end-to-end encrypted; the relay never sees the password or keys.

IDs are assigned and stored by the server (see Registry): a desk keeps its ID
across server and client restarts. Desks are recognised by a random machine
key they generate once, or by their computer name if the key is unknown.
"""
import argparse, json, logging, os, secrets, socket, threading, time

from .netutil import clean_ip, format_hostport, listen
from .protocol import recv_line, send_line

log = logging.getLogger("relay")
REGISTRY_FILE = os.path.join(os.path.expanduser("~"), ".remotedesk_server_desks.json")


def valid_id(i):
    return isinstance(i, str) and len(i) == 9 and i.isdigit() and i[0] != "0"


class Registry:
    """Persistent desk ID table: id -> {id, name, key, ip, created, last_seen}."""

    def __init__(self, path=REGISTRY_FILE, match_by_name=True):
        self.path = path
        self.match_by_name = match_by_name
        self.lock = threading.RLock()
        self.desks = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("desks", []):
                if valid_id(d.get("id")):
                    self.desks[d["id"]] = d
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("cannot read %s: %s", path, e)

    def save(self):
        with self.lock:
            data = {"desks": sorted(self.desks.values(), key=lambda d: d["id"])}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def new_id(self):
        while True:
            i = str(secrets.randbelow(900_000_000) + 100_000_000)
            if i not in self.desks:
                return i

    def resolve(self, key, name, hint, online_ids):
        """Returns the desk ID for a connecting desk (creating an entry if needed)."""
        name = (name or "").strip()[:64]
        key = (key or "").strip()[:128]
        with self.lock:
            entry = None
            if key:
                entry = next((d for d in self.desks.values() if d.get("key") == key), None)
            if entry is None and name and self.match_by_name:
                # same computer name, not online right now (e.g. reinstalled / config lost)
                cands = [d for d in self.desks.values()
                         if d.get("name", "").lower() == name.lower() and d["id"] not in online_ids]
                if len(cands) == 1:
                    entry = cands[0]
            if entry is None and not key and valid_id(hint) and hint in self.desks \
                    and not self.desks[hint].get("key") and hint not in online_ids:
                entry = self.desks[hint]                      # old client without a key
            if entry is None:
                i = hint if valid_id(hint) and hint not in self.desks else self.new_id()
                entry = {"id": i, "created": time.time()}
                self.desks[i] = entry
            if key:
                entry["key"] = key
            if name:
                entry["name"] = name
            entry["last_seen"] = time.time()
            self.save()
            return entry["id"]

    def touch(self, desk_id, ip=None):
        with self.lock:
            d = self.desks.get(desk_id)
            if d:
                d["last_seen"] = time.time()
                if ip:
                    d["ip"] = ip
                self.save()

    def change_id(self, old, new):
        with self.lock:
            if not valid_id(new):
                raise ValueError("An ID has 9 digits and does not start with 0")
            if new == old:
                return
            if new in self.desks:
                raise ValueError(f"ID {new} is already used by {self.desks[new].get('name') or 'another desk'}")
            d = self.desks.pop(old)
            d["id"] = new
            self.desks[new] = d
            self.save()

    def delete(self, desk_id):
        with self.lock:
            self.desks.pop(desk_id, None)
            self.save()

    def list(self):
        with self.lock:
            return [dict(d) for d in self.desks.values()]


class Relay:
    def __init__(self, host="", port=7070, on_log=None, registry=None):
        self.addr = (host, port)
        self.registry = registry or Registry()
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
        self._srv = listen(self.addr[1], self.addr[0])
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        fam = "IPv4 + IPv6" if self._srv.family == socket.AF_INET6 and not self.addr[0] else \
            ("IPv6" if self._srv.family == socket.AF_INET6 else "IPv4")
        self.on_log(f"Server started on port {self.addr[1]} ({fam})")

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

    def change_id(self, old, new):
        """Admin: give a desk another ID. An online desk is told at once."""
        self.registry.change_id(old, new)
        with self.lock:
            h = self.hosts.pop(old, None)
            if h:
                self.hosts[new] = h
                h["id_box"][0] = new
        if h:
            try:
                send_line(h["sock"], {"id": new})
            except OSError:
                pass
        self.on_log(f"Desk ID {old} changed to {new}")

    def delete_desk(self, desk_id):
        with self.lock:
            h = self.hosts.get(desk_id)
        if h:
            _close(h["sock"])
        self.registry.delete(desk_id)
        self.on_log(f"Desk {desk_id} removed")

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
            threading.Thread(target=self._handle, args=(conn, clean_ip(peer[0])), daemon=True).start()

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
        name = str(msg.get("name") or "")
        key = str(msg.get("key") or "")
        with self.lock:
            online = set(self.hosts)
        desk_id = self.registry.resolve(key, name, str(msg.get("id") or ""), online)
        id_box = [desk_id]                     # the ID can be changed by the admin later
        with self.lock:
            old = self.hosts.get(desk_id)
            self.hosts[desk_id] = {"sock": conn, "ip": ip, "since": time.time(),
                                   "name": name, "id_box": id_box}
        if old:                                # same desk reconnected: drop the stale link
            _close(old["sock"])
        self.registry.touch(desk_id, ip)
        send_line(conn, {"ok": True, "id": desk_id})
        self.on_log(f"Desk {desk_id} online — {name or 'unnamed'} ({ip})")
        conn.settimeout(90)  # clients ping every 20 s
        try:
            while conn.recv(64):
                pass
        except OSError:
            pass
        finally:
            cur = id_box[0]
            with self.lock:
                if self.hosts.get(cur, {}).get("sock") is conn:
                    del self.hosts[cur]
            self.registry.touch(cur)
            if self.running:
                self.on_log(f"Desk {cur} offline — {name or 'unnamed'}")
            _close(conn)

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
    ap.add_argument("--bind", default="", help="address to listen on (default: all IPv4 and IPv6)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RD_RELAY_PORT", 7070)))
    ap.add_argument("--desks", default=REGISTRY_FILE, help="file that stores desk IDs")
    ap.add_argument("--no-name-match", action="store_true",
                    help="do not recognise desks by computer name when their key is unknown")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(message)s")
    Relay(a.bind, a.port, registry=Registry(a.desks, not a.no_name_match)).serve_forever()


if __name__ == "__main__":
    main()
