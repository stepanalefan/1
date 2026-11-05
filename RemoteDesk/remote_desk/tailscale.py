"""Tailscale integration: find this device's Tailscale address and the other
devices in the tailnet, so desks can connect directly without a RemoteDesk
Server. Uses the `tailscale status --json` command of the installed app.
"""
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field

TS_V4 = ipaddress.ip_network("100.64.0.0/10")
TS_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
DOWNLOAD_URL = "https://tailscale.com/download"
REMOTE_PORT = 7071          # default direct port of other desks


@dataclass
class Peer:
    name: str               # host name, e.g. "office-pc"
    dns: str                # MagicDNS name, e.g. "office-pc.tail1234.ts.net"
    ips: list
    os: str = ""
    online: bool = False
    remotedesk: bool = False   # our direct port answered

    @property
    def ip(self):
        v4 = [i for i in self.ips if "." in i]
        return (v4 or self.ips or [""])[0]


@dataclass
class Status:
    installed: bool = False
    state: str = ""         # Running, NeedsLogin, Stopped, ... ("" = unknown)
    name: str = ""
    dns: str = ""
    ips: list = field(default_factory=list)
    tailnet: str = ""
    peers: list = field(default_factory=list)
    error: str = ""

    @property
    def running(self):
        return self.state == "Running" and bool(self.ips)

    @property
    def ip(self):
        v4 = [i for i in self.ips if "." in i]
        return (v4 or self.ips or [""])[0]


def is_tailscale_ip(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return a in TS_V4 or a in TS_V6


def find_cli():
    exe = shutil.which("tailscale")
    if exe:
        return exe
    candidates = []
    if sys.platform == "win32":
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(os.path.join(base, "Tailscale", "tailscale.exe"))
    elif sys.platform == "darwin":
        candidates += ["/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                       "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"]
    else:
        candidates += ["/usr/bin/tailscale", "/usr/sbin/tailscale", "/usr/local/bin/tailscale"]
    return next((c for c in candidates if os.path.isfile(c)), None)


def _local_tailscale_ips():
    """Fallback when the CLI is missing: Tailscale addresses on this machine."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            if is_tailscale_ip(info[4][0]):
                ips.add(info[4][0])
    except OSError:
        pass
    try:  # the route to the Tailscale DNS server goes through the tunnel
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("100.100.100.100", 53))
        ip = s.getsockname()[0]
        s.close()
        if is_tailscale_ip(ip):
            ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def parse_status(data):
    """Turns `tailscale status --json` output into a Status."""
    st = Status(installed=True, state=data.get("BackendState", ""))
    me = data.get("Self") or {}
    st.name = me.get("HostName", "")
    st.dns = (me.get("DNSName") or "").rstrip(".")
    st.ips = list(me.get("TailscaleIPs") or data.get("TailscaleIPs") or [])
    st.tailnet = (data.get("CurrentTailnet") or {}).get("Name", "") or \
        (data.get("MagicDNSSuffix") or "")
    for p in (data.get("Peer") or {}).values():
        if p.get("ShareeNode"):
            continue
        ips = list(p.get("TailscaleIPs") or [])
        if not ips:
            continue
        st.peers.append(Peer(name=p.get("HostName") or (p.get("DNSName") or "?").split(".")[0],
                             dns=(p.get("DNSName") or "").rstrip("."), ips=ips,
                             os=p.get("OS", ""), online=bool(p.get("Online"))))
    st.peers.sort(key=lambda p: (not p.online, p.name.lower()))
    return st


def status(timeout=6):
    cli = find_cli()
    if not cli:
        ips = _local_tailscale_ips()
        if ips:   # running, but we cannot list peers
            return Status(installed=True, state="Running", ips=ips,
                          error="Tailscale command line not found; device list unavailable")
        return Status(installed=False)
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        out = subprocess.run([cli, "status", "--json"], capture_output=True, timeout=timeout,
                             creationflags=flags)
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception as e:
        return Status(installed=True, error=str(e), ips=_local_tailscale_ips(),
                      state="Running" if _local_tailscale_ips() else "")
    return parse_status(data)


def probe(ip, port, timeout=1.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_peers(peers, port, timeout=1.0):
    """Marks online peers whose RemoteDesk direct port answers (in parallel)."""
    def one(p):
        p.remotedesk = probe(p.ip, port, timeout)
    threads = [threading.Thread(target=one, args=(p,), daemon=True) for p in peers if p.online]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 0.5)
