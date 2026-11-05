"""IPv4 / IPv6 helpers shared by the server, host and viewer."""
import ipaddress
import socket


def parse_hostport(s, default_port):
    """'host', 'host:port', '1.2.3.4:5', '2001:db8::1', '[2001:db8::1]:5' -> (host, port).

    Returns None for an empty string. Raises ValueError for a malformed address.
    """
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith("["):                       # [v6]:port
        end = s.find("]")
        if end < 0:
            raise ValueError(f"missing ']' in {s!r}")
        host, rest = s[1:end], s[end + 1:]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:] else default_port
        return host, port
    if s.count(":") > 1:                        # bare v6 address, no port
        return s, default_port
    host, sep, port = s.partition(":")
    return host, (int(port) if sep and port else default_port)


def format_hostport(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def clean_ip(ip):
    """'::ffff:1.2.3.4' (IPv4 on a dual-stack socket) -> '1.2.3.4'; strips %zone."""
    ip = ip.split("%")[0]
    return ip[7:] if ip.startswith("::ffff:") and "." in ip else ip


def listen(port, bind=""):
    """TCP listener on all IPv4 *and* IPv6 addresses when the OS supports it."""
    if bind:
        fam = socket.AF_INET6 if ":" in bind else socket.AF_INET
        return socket.create_server((bind, port), family=fam)
    if socket.has_dualstack_ipv6():
        return socket.create_server(("", port), family=socket.AF_INET6, dualstack_ipv6=True)
    return socket.create_server(("0.0.0.0", port))


def connect(host, port, timeout):
    """Like socket.create_connection (tries every IPv4/IPv6 address of a name)."""
    return socket.create_connection((host, port), timeout=timeout)


def local_addresses():
    """Addresses of this machine others may reach: IPv4 (private + public) and
    global IPv6. Loopback and link-local addresses are left out."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except OSError:
        pass
    # the source address of the default route (no packets are sent)
    for fam, probe in ((socket.AF_INET, "8.8.8.8"), (socket.AF_INET6, "2001:4860:4860::8888")):
        try:
            s = socket.socket(fam, socket.SOCK_DGRAM)
            s.connect((probe, 80))
            found.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    out = []
    for a in found:
        try:
            ip = ipaddress.ip_address(clean_ip(a))
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        out.append(ip)
    out.sort(key=lambda ip: (ip.version, str(ip)))
    return [str(ip) for ip in out]
