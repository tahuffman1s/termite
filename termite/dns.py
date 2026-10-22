"""The VPN's DNS servers, once the tunnel is up: each one gets two
queries, and the one that answers them best goes first.

glibc's resolver tries the nameservers in /etc/resolv.conf in order, and
gives each one five seconds before moving on. A gateway that hands out a
server that doesn't answer (it pings, but nothing listens on port 53) makes
every lookup wait those five seconds, and programs with short timeouts of
their own fail outright. A split-horizon server does the same for part of
the world: it answers the company's names and stays silent, rather than
saying "refused", on everyone else's. So after connecting, Termite sends
every server the VPN gave two queries (in parallel, two seconds at most):
the gateway's own name, and an outside one. A server that answers both
comes before one that answers only one, then the faster; and when the best
one isn't the first, it goes first:
with openconnect, the root helper puts the servers back in that order
wherever vpnc-script put them (resolv.conf, systemd-resolved's link or
resolvconf), and a NetworkManager connection gets the server as its own DNS
(ipv4.dns, which NetworkManager puts before the gateway's), then reconnects
for it to take.

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
EXTERNAL = "example.com"  # the outside name; any answer counts, even "no such name"
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
        return host or EXTERNAL
    return EXTERNAL


def names(gateway):
    """What to ask every server: the gateway's name (one the company's
    servers know) and an outside one."""
    inside = hostname(gateway)
    return [inside, EXTERNAL] if inside != EXTERNAL else [EXTERNAL]


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


def check(servers, names):
    """Every server asked every name, all at once:
    [(server, [(name, seconds or None), ...])], in the servers' order."""
    results = [[None] * len(names) for _ in servers]

    def one(i, j):
        results[i][j] = probe(servers[i], names[j])

    threads = [threading.Thread(target=one, args=(i, j), daemon=True) for i in range(len(servers)) for j in range(len(names))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [(server, list(zip(names, times))) for server, times in zip(servers, results)]


def fastest(results):
    """The server that answered the most names, then the one whose slowest
    answer came soonest, the gateway's order deciding between ones within
    STEP of each other; None when none answered anything."""
    ranked = []
    for i, (server, answers) in enumerate(results):
        times = [seconds for _, seconds in answers if seconds is not None]
        if times:
            ranked.append((len(answers) - len(times), round(max(times) / STEP), i, server))
    return min(ranked)[3] if ranked else None


def unanswered(answers):
    return [name for name, seconds in answers if seconds is None]


def describe(results):
    """One line: "10.0.0.1: vpn.example.com 22 ms, example.com no answer; 10.0.0.2: ..." """
    return "; ".join(
        f"{server}: " + ", ".join(f"{name} {seconds * 1000:.0f} ms" if seconds is not None else f"{name} no answer" for name, seconds in answers)
        for server, answers in results
    )


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
        log("DNS: no servers from the VPN were seen; leaving DNS alone")
        return None
    results = check(servers, names(gateway))
    log(f"DNS: {describe(results)}")
    best = fastest(results)
    if best is None:
        return f"No DNS server of the VPN answers ({', '.join(servers)})"
    if best == servers[0]:
        return None
    if not t.prefer_dns(best, session, log):
        return None
    silent = [f"{server} no answer for {', '.join(unanswered(answers))}" for server, answers in results if server != best and unanswered(answers)]
    return f"Using DNS server {best} first" + ("; " + "; ".join(silent) if silent else "")
