"""The VPN's DNS servers, once the tunnel is up: each one gets a query,
and the one that answers fastest goes first.

glibc's resolver tries the nameservers in /etc/resolv.conf in order, and
gives each one five seconds before moving on. A gateway that hands out a
server that doesn't answer (it pings, but nothing listens on port 53) makes
every lookup wait those five seconds, and programs with short timeouts of
their own fail outright. So after connecting, Termite sends every server the
VPN gave a query (in parallel, two seconds at most), and when the first one
doesn't answer, or another answers clearly faster, puts the best one first:
openconnect's resolv.conf gets reordered through the root helper, and a
NetworkManager connection gets the server as its own DNS (ipv4.dns, which
NetworkManager puts before the gateway's), then reconnects for it to take.

Nothing happens when a local resolver (systemd-resolved, dnsmasq) stands in
front of the servers: those try the next one by themselves."""

import ipaddress
import random
import select
import socket
import struct
import threading
import time

RESOLV_CONF = "/etc/resolv.conf"
PORT = 53
TIMEOUT = 2.0  # seconds a server gets to answer, with a second query half-way
STEP = 0.1  # servers that answer within this much of each other keep the gateway's order


def hostname(gateway):
    """The gateway's host, for a query any corporate server has an answer
    to (any answer counts, even a refusal: the point is that it answers)."""
    host = gateway.split("://", 1)[-1].split("/", 1)[0]
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host or "example.com"
    return "example.com"


def query(name):
    """A DNS query packet (an A question, recursion wanted), and its id."""
    qid = random.randrange(65536)
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    labels = [p for p in name.rstrip(".").encode("idna").split(b".") if p]
    qname = b"".join(bytes([len(p)]) + p for p in labels) + b"\0"
    return qid, header + qname + struct.pack(">HH", 1, 1)


def probe(server, name, timeout=None, port=None):
    """How long the server takes to answer a query for name, in seconds, or
    None when it doesn't within the timeout (or refuses the port)."""
    timeout = TIMEOUT if timeout is None else timeout
    port = PORT if port is None else port
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    qid, packet = query(name)
    start = time.monotonic()
    deadline = start + timeout
    resend = start + timeout / 2  # once, for a lost packet
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.connect((server, port))  # connected: a port unreachable comes back as an error
            s.send(packet)
            while True:
                now = time.monotonic()
                if now >= deadline:
                    return None
                if resend is not None and now >= resend:
                    s.send(packet)
                    resend = None
                wait = (deadline if resend is None else resend) - now
                ready, _, _ = select.select([s], [], [], max(wait, 0))
                if not ready:
                    continue
                data = s.recv(512)
                if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == qid:
                    return time.monotonic() - start
    except OSError:
        return None


def check(servers, name):
    """Every server probed at once: [(server, seconds or None)], in order."""
    results = [None] * len(servers)

    def one(i, server):
        results[i] = probe(server, name)

    threads = [threading.Thread(target=one, args=(i, s), daemon=True) for i, s in enumerate(servers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return list(zip(servers, results))


def fastest(results):
    """The server that answered fastest, the gateway's order deciding
    between ones within STEP of each other; None when none answered."""
    answered = [(round(seconds / STEP), i, server) for i, (server, seconds) in enumerate(results) if seconds is not None]
    return min(answered)[2] if answered else None


def describe(results):
    return ", ".join(f"{server} {seconds * 1000:.0f} ms" if seconds is not None else f"{server} no answer" for server, seconds in results)


def read_resolv_conf(path=None):
    """(the text, its nameservers), or ("", []) when it can't be read."""
    try:
        with open(path or RESOLV_CONF) as f:
            text = f.read()
    except OSError:
        return "", []
    return text, [line.split()[1] for line in text.splitlines() if line.split()[:1] == ["nameserver"] and len(line.split()) > 1]


def local(server):
    """Whether the server is this machine (a resolver of its own)."""
    try:
        return ipaddress.ip_address(server.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def reorder(text, first):
    """The resolv.conf text with `first` as the first nameserver, the other
    nameserver lines following in their order and all else as it was."""
    servers = [line.split()[1] for line in text.splitlines() if line.split()[:1] == ["nameserver"] and len(line.split()) > 1]
    if first not in servers:
        return text
    order = iter([first] + [s for s in servers if s != first])
    lines = []
    for line in text.splitlines(keepends=True):
        if line.split()[:1] == ["nameserver"] and len(line.split()) > 1:
            line = f"nameserver {next(order)}\n"
        lines.append(line)
    return "".join(lines)


def shell_line(text):
    """The text as one line for the root helper, which prints it back
    with printf %b."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def mitigate(t, gateway, session, log):
    """After t.up(): probes the VPN's DNS servers and puts the best first.
    Returns a word for the user when something was done or found wrong,
    else None. Raises TunnelError only when the tunnel was lost over it."""
    _, resolvers = read_resolv_conf()
    if resolvers and all(map(local, resolvers)):
        log("DNS: a resolver on this machine takes the VPN's servers, and skips one that doesn't answer by itself")
        return None
    servers = t.dns_servers()
    if not servers:
        log("DNS: the VPN's servers aren't in /etc/resolv.conf directly; leaving DNS alone")
        return None
    results = check(servers, hostname(gateway))
    log(f"DNS: {describe(results)}")
    best = fastest(results)
    dead = [server for server, seconds in results if seconds is None]
    if best is None:
        return f"No DNS server of the VPN answers ({', '.join(dead)})"
    if best == servers[0]:
        return None
    if not t.prefer_dns(best, session, log):
        return None
    return f"Using DNS server {best} first" + (f"; {', '.join(dead)} not answering" if dead else "")
