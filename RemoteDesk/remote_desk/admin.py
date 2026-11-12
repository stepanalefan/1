"""Command-line administration of a running RemoteDesk server (Linux).

  remotedesk-admin list                 all desks: ID, computer name, online, IP, last seen
  remotedesk-admin sessions             active connections
  remotedesk-admin set-id OLD NEW       give a desk another 9-digit ID
  remotedesk-admin delete ID            forget a desk
  remotedesk-admin disconnect SID       end a session

Talks to the server through its admin socket (default /run/remotedesk/admin.sock);
run it as root or as a member of the 'remotedesk' group.
"""
import argparse
import json
import socket
import sys
import time

DEFAULT_SOCKET = "/run/remotedesk/admin.sock"


def request(path, req):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(path)
    except PermissionError:
        sys.exit(f"Permission denied for {path}: run with sudo or join the 'remotedesk' group.")
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit(f"The RemoteDesk server is not running (no admin socket at {path}).\n"
                 f"Check: systemctl status remotedesk-server")
    s.sendall(json.dumps(req).encode() + b"\n")
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf or b"{}")


def fmt_id(i):
    return f"{i[:3]} {i[3:6]} {i[6:]}" if len(i) == 9 else i


def age(t):
    if not t:
        return "—"
    s = int(time.time() - t)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d"


def main():
    ap = argparse.ArgumentParser(prog="remotedesk-admin", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("sessions")
    p = sub.add_parser("set-id"); p.add_argument("old"); p.add_argument("new")
    p = sub.add_parser("delete"); p.add_argument("id")
    p = sub.add_parser("disconnect"); p.add_argument("sid")
    a = ap.parse_args()

    req = {"cmd": a.cmd}
    if a.cmd == "set-id":
        req.update(old=a.old, new=a.new)
    elif a.cmd == "delete":
        req["id"] = a.id
    elif a.cmd == "disconnect":
        req["sid"] = a.sid
    r = request(a.socket, req)
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    if not r.get("ok"):
        sys.exit("Error: " + r.get("error", "unknown"))
    if a.cmd == "list":
        rows = r["desks"]
        print(f"{'DESK ID':<13} {'COMPUTER NAME':<26} {'STATUS':<16} {'IP ADDRESS':<40} LAST SEEN")
        for d in rows:
            status = f"online {age(d['since'])}" if d["online"] else "offline"
            last = "now" if d["online"] else (time.strftime("%Y-%m-%d %H:%M", time.localtime(d["last_seen"]))
                                              if d.get("last_seen") else "—")
            print(f"{fmt_id(d['id']):<13} {(d['name'] or '(unnamed)')[:25]:<26} {status:<16} "
                  f"{d['ip'] or '':<40} {last}")
        print(f"\n{len(rows)} desks, {sum(d['online'] for d in rows)} online")
    elif a.cmd == "sessions":
        print(f"{'SESSION':<10} {'DESK':<13} {'VIEWER':<40} {'DURATION':<10} TRANSFERRED")
        for x in r["sessions"]:
            print(f"{x['sid']:<10} {fmt_id(x['desk']):<13} {x['viewer']:<40} {age(x['since']):<10} "
                  f"{x['bytes'] / 1e6:.1f} MB")
        print(f"\n{len(r['sessions'])} active sessions")
    else:
        print("OK")


if __name__ == "__main__":
    main()
