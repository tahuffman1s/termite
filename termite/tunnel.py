"""The tunnel, once sso.sign_in() has a session. Two ways to run it:

- NetworkManager, with its OpenConnect plugin: the session goes to a
  NetworkManager connection (made if missing) through nmcli's own secret
  agent, as the plugin's sign-in dialogs would hand it over. NetworkManager
  sets up the routes and DNS, the desktop's network menu shows the VPN and
  can disconnect it, and it stays up when this app closes.
- openconnect directly, as root through pkexec (or sudo in a terminal), for
  systems without NetworkManager's plugin: the system's, or the AppImage's
  own. vpnc-script sets up routes and DNS. The tunnel lasts as long as this
  app holds it.
"""

import glob
import io
import os
import re
import stat
import subprocess
import tarfile
import threading

from . import dns
from .sso import host_env, which


class TunnelError(Exception):
    pass


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, env=host_env(), **kw)


def last_line(text, default="unknown error"):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else default


class NetworkManager:
    label = "NetworkManager"
    persistent = True  # the tunnel outlives the app

    # vpn.data for a new connection, as NetworkManager's own editor writes it: the user agent
    # says AnyConnect (gateways only offer SSO to clients that do), and the
    # session secrets are not saved (flags 2), so every connection signs in
    # fresh.
    VPN_DATA = (
        "protocol=anyconnect,enable_csd_trojan=no,pem_passphrase_fsid=no,"
        "prevent_invalid_cert=no,stoken_source=disabled,useragent=AnyConnect,"
        "cookie-flags=2,gateway-flags=2,gwcert-flags=2,resolve-flags=2,"
        "xmlconfig-flags=0,lasthost-flags=0,autoconnect-flags=0,certsigs-flags=0"
    )
    SERVICE = "org.freedesktop.NetworkManager.openconnect"

    def __init__(self, connection):
        self.connection = connection or "Termite"
        self.nmcli = which("nmcli")

    @staticmethod
    def available():
        nmcli = which("nmcli")
        if not nmcli:
            return False
        try:
            return run([nmcli, "-t", "-f", "RUNNING", "general"], timeout=10).stdout.strip() == "running"
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def plugin_installed():
        # NixOS's too, by a path that appimage-run's sandbox (which has an
        # /etc of its own) can see.
        dirs = ("/usr/lib/NetworkManager/VPN", "/usr/lib64/NetworkManager/VPN", "/usr/local/lib/NetworkManager/VPN",
                "/etc/NetworkManager/VPN", "/run/current-system/etc/NetworkManager/VPN")  # fmt: skip
        for d in dirs:
            for f in glob.glob(os.path.join(d, "*.name")):
                try:
                    with open(f) as fh:
                        if NetworkManager.SERVICE in fh.read():
                            return True
                except OSError:
                    pass
        return False

    def _connections(self, active=False):
        args = [self.nmcli, "-t", "-f", "NAME", "connection", "show"] + (["--active"] if active else [])
        try:
            out = run(args, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        # nmcli -t escapes ':' and '\' in names.
        return [line.replace("\\:", ":").replace("\\\\", "\\") for line in out.splitlines()]

    def is_up(self):
        return bool(self.nmcli) and self.connection in self._connections(active=True)

    def ensure(self, gateway, log):
        if not self.nmcli:
            raise TunnelError("nmcli is not installed")
        if self.connection in self._connections():
            kind = run([self.nmcli, "-g", "vpn.service-type", "connection", "show", self.connection]).stdout.strip()
            if kind and kind != self.SERVICE:
                raise TunnelError(f"the NetworkManager connection '{self.connection}' is not an OpenConnect VPN ({kind})")
            return
        if not self.plugin_installed():
            raise TunnelError(
                "NetworkManager's OpenConnect plugin is not installed (NetworkManager-openconnect on Fedora/openSUSE/Arch, "
                "network-manager-openconnect on Debian/Ubuntu); install it, or choose openconnect as the connection method"
            )
        host = gateway.split("://", 1)[-1].rstrip("/")
        if "," in host:
            raise TunnelError(f"the gateway address cannot contain a comma: {host}")
        log(f"adding the NetworkManager connection '{self.connection}'")
        r = run(
            [
                self.nmcli, "connection", "add", "type", "vpn", "con-name", self.connection,
                "vpn.service-type", self.SERVICE,
                "vpn.data", f"gateway={host},{self.VPN_DATA}",
                "connection.autoconnect", "no",
            ]
        )  # fmt: skip
        if r.returncode != 0:
            raise TunnelError(f"could not add the NetworkManager connection: {last_line(r.stderr)}")

    def up(self, session, log):
        # The secrets the OpenConnect plugin connects with, answered by
        # nmcli's own secret agent through a pipe; nothing is written to
        # disk. nmcli must answer every not-saved secret the connection has
        # (resolve too, empty), or it steps aside and the desktop's VPN
        # dialog asks instead.
        secrets = "".join(
            f"vpn.secrets.{k}:{v}\n"
            for k, v in (("gateway", session["url"]), ("cookie", session["cookie"]), ("gwcert", session["servercert"]), ("resolve", ""))
        )
        r_fd, w_fd = os.pipe()
        try:
            os.write(w_fd, secrets.encode())
            os.close(w_fd)
            w_fd = None
            log(f"bringing up '{self.connection}'")
            r = subprocess.run(
                [self.nmcli, "--wait", "120", "connection", "up", self.connection, "passwd-file", f"/dev/fd/{r_fd}"],
                pass_fds=(r_fd,),
                capture_output=True,
                text=True,
                env=host_env(),
            )
        finally:
            os.close(r_fd)
            if w_fd is not None:
                os.close(w_fd)
        if r.returncode != 0:
            raise TunnelError(f"{last_line(r.stderr or r.stdout)} (details: journalctl -b -u NetworkManager)")

    def down(self):
        r = run([self.nmcli, "connection", "down", self.connection])
        if r.returncode != 0 and self.is_up():
            raise TunnelError(last_line(r.stderr))

    def dns_servers(self):
        """The DNS servers the active connection has, the VPN's (and any
        the profile adds, which come first)."""
        try:
            out = run([self.nmcli, "-g", "IP4.DNS,IP6.DNS", "connection", "show", self.connection], timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        servers = []
        for line in out.splitlines():
            for server in line.split("|"):
                server = server.strip()
                if server and server not in servers:
                    servers.append(server)
        return servers

    def prefer_dns(self, server, session, log):
        """The server as the connection's own DNS (ipv4.dns or ipv6.dns,
        which NetworkManager puts before the gateway's), which takes on
        activation: so down and up again, with the same session. True when
        done; False when the profile could not be changed (the tunnel
        stays); TunnelError when it did not come back up."""
        key = "ipv6.dns" if ":" in server else "ipv4.dns"
        r = run([self.nmcli, "connection", "modify", self.connection, key, server])
        if r.returncode != 0:
            log(f"DNS: could not set {key} of '{self.connection}': {last_line(r.stderr)}")
            return False
        log(f"DNS: {key} of '{self.connection}' is now {server}, which NetworkManager puts first; reconnecting for it to take")
        try:
            self.down()
        except TunnelError as e:
            log(f"DNS: could not reconnect ({e}); it takes from the next connection")
            return False
        try:
            self.up(session, log)
        except TunnelError as e:
            raise TunnelError(f"{e}; the DNS change is saved, so connect again")
        return True

    def forget_dns(self, log):
        """Any DNS server prefer_dns() gave the connection, taken away
        again (the dns setting says: as the gateway gives them)."""
        try:
            out = run([self.nmcli, "-g", "ipv4.dns,ipv6.dns", "connection", "show", self.connection], timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        if not out.strip():
            return
        r = run([self.nmcli, "connection", "modify", self.connection, "ipv4.dns", "", "ipv6.dns", ""])
        if r.returncode != 0:
            log(f"DNS: could not take the DNS servers of '{self.connection}' away: {last_line(r.stderr)}")
        else:
            log(f"DNS: '{self.connection}' has no DNS servers of its own again")


# Run as root. Reads from stdin the cookie, then the size of the bundled
# OpenConnect directory and that directory as a tar (0 and nothing when the
# system's openconnect is in the arguments): root can't read the AppImage's
# mount, so it comes this way, into a directory only root can write to.
# Starts openconnect in the background (it returns once the tunnel is up)
# with a wrapper around vpnc-script (the bundled one, or the one named as
# the third argument) that keeps what it was told about DNS, says what
# that was ("dns SERVER...") and "connected" on stdout, then takes lines
# from stdin: "dns SERVER..." puts the VPN's DNS servers in that order
# wherever vpnc-script put them and answers "ok" or "failed"; any other
# line, the end of stdin, or a signal stops the tunnel. Holding the stop on
# stdin means the tunnel never outlives the app.
OPENCONNECT_SCRIPT = r"""
# Where the tools are, NixOS's included (for this and vpnc-script).
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin:/usr/bin:/bin:/run/current-system/sw/bin:/run/current-system/sw/sbin"
export PATH
pidfile=$1; resolvconf=$2; script=$3; shift 3
IFS= read -r cookie || exit 2
IFS= read -r size || exit 2
dir=$(mktemp -d "${TERMITE_RUN_DIR:-/run}/termite-openconnect.XXXXXX") || exit 2
cleanup() { rm -rf "$dir"; rm -f "$pidfile"; }
if [ "$size" -gt 0 ]; then
  head -c "$size" | tar -x -f - -C "$dir" || { cleanup; exit 2; }
  script=$dir/vpnc-script
  set -- "$dir/openconnect" "$@"
fi
if [ -n "$script" ]; then
  # Around vpnc-script: on connect, what it was told about DNS (for
  # dns_apply), then the script itself, with everything it was given.
  printf '%s\n' "$script" >"$dir/real"
  cat >"$dir/script" <<'EOF'
#!/bin/sh
d=$(dirname "$0")
[ "$reason" = connect ] && printf '%s\n' "$TUNDEV" "$INTERNAL_IP4_DNS" "$INTERNAL_IP6_DNS" "$CISCO_DEF_DOMAIN" >"$d/dns"
exec "$(cat "$d/real")" "$@"
EOF
  chmod 755 "$dir/script"
  oc=$1; shift
  set -- "$oc" --script="$dir/script" "$@"
fi
# The VPN's DNS servers in the order given ($@), put where vpnc-script put
# them: the tunnel's link in systemd-resolved, its resolvconf record, or
# the nameserver lines of resolv.conf itself (the ones that are the VPN's,
# where they are; any others stay).
dns_apply() {
  if [ -n "$TUNDEV" ] && command -v resolvectl >/dev/null 2>&1 && resolvectl dns "$TUNDEV" 2>/dev/null | grep -q -F " $1"; then
    resolvectl dns "$TUNDEV" "$@"
  elif [ -n "$TUNDEV" ] && command -v resolvconf >/dev/null 2>&1 && { [ -e "${TERMITE_RUN_DIR:-/run}/resolvconf/interface/$TUNDEV" ] || [ -e "${TERMITE_RUN_DIR:-/run}/resolvconf/interfaces/$TUNDEV" ]; }; then
    { for s; do echo "nameserver $s"; done; [ -z "$DOMAIN" ] || echo "search $DOMAIN"; } | resolvconf -a "$TUNDEV"
  else
    tmp=$(mktemp "$dir/resolv.XXXXXX") || return 1
    awk -v order="$*" '
      BEGIN { n = split(order, want, " "); for (i = 1; i <= n; i++) ours[want[i]] = 1 }
      $1 == "nameserver" && ($2 in ours) { if (!done) { for (i = 1; i <= n; i++) print "nameserver " want[i]; done = 1 }; next }
      { print }' "$resolvconf" >"$tmp" && grep -q -F "nameserver $1" "$tmp" && cat "$tmp" >"$resolvconf"
    s=$?; rm -f "$tmp"; return $s
  fi
}
printf '%s\n' "$cookie" | "$@" --background --pid-file="$pidfile" >&2 || { s=$?; cleanup; echo "failed $s"; exit 1; }
stop() {
  pid=$(cat "$pidfile" 2>/dev/null)
  if [ -n "$pid" ] && kill -INT "$pid" 2>/dev/null; then
    # Until it has run vpnc-script's disconnect, which puts routes and DNS back.
    n=0
    while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 150 ]; do sleep 0.1; n=$((n + 1)); done
  fi
  cleanup
  exit 0
}
trap stop INT TERM HUP
TUNDEV= DNS4= DNS6= DOMAIN=
if [ -r "$dir/dns" ]; then
  { IFS= read -r TUNDEV; IFS= read -r DNS4; IFS= read -r DNS6; IFS= read -r DOMAIN; } <"$dir/dns"
  echo "dns $DNS4 $DNS6"
fi
echo connected
while IFS= read -r line; do
  case "$line" in
    "dns "*) dns_apply ${line#dns } && echo ok || echo failed ;;
    *) break ;;
  esac
done
stop
"""


def elevator(name):
    """pkexec or sudo, the setuid one: NixOS has its setuid wrappers in
    /run/wrappers/bin and plain copies (which refuse to run) elsewhere on
    PATH, which may come first. Falls back to the first one on PATH."""
    path = host_env().get("PATH", os.defpath).split(":")
    for d in ["/run/wrappers/bin", *path]:
        candidate = os.path.join(d, name)
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        if st.st_mode & stat.S_ISUID and os.access(candidate, os.X_OK):
            return candidate
    return which(name)


def no_new_privs():
    """Whether this process may not gain privileges (prctl NO_NEW_PRIVS, set
    by some launchers and sandboxes): then pkexec and sudo can't work."""
    try:
        with open("/proc/self/status") as f:
            return any(line.split() == ["NoNewPrivs:", "1"] for line in f)
    except OSError:
        return False


# Where systemd-run is when PATH (in a sandbox, say) doesn't have it.
SYSTEMD_RUN = ("/run/current-system/sw/bin/systemd-run", "/usr/bin/systemd-run", "/bin/systemd-run")


def bundled_openconnect():
    """The AppImage's OpenConnect directory (openconnect, its libraries and
    vpnc-script), if there is one."""
    d = os.environ.get("TERMITE_OPENCONNECT_DIR")
    return d if d and os.access(os.path.join(d, "openconnect"), os.X_OK) else None


def system_openconnect():
    """The system's openconnect, if it is new enough for the single sign-on
    cookie (9.0, which brought it)."""
    path = which("openconnect", sbin=True)
    if not path:
        return None
    try:
        out = run([path, "--version"], timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"OpenConnect version v?(\d+)\.", out)
    return path if m and int(m.group(1)) >= 9 else None


def default_script(openconnect):
    """The vpnc-script the system's openconnect runs by default (--version
    says), or "" when it doesn't say."""
    try:
        out = run([openconnect, "--version"], timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"^Default vpnc-script.*: (\S+)$", out, re.M)
    return m.group(1) if m else ""


class OpenConnect:
    label = "openconnect"
    persistent = False

    def __init__(self, elevate="pkexec"):
        self.openconnect = system_openconnect()
        self.bundle = None if self.openconnect else bundled_openconnect()
        if self.bundle:
            self.label = "openconnect (built in)"
        self.script = default_script(self.openconnect) if self.openconnect else ""
        self.elevate = elevate
        self.proc = None
        self.connected = False
        self.dns = []  # what vpnc-script was given, once up
        self.pidfile = f"/run/termite-{os.getuid()}.pid"

    @staticmethod
    def available():
        return bool(system_openconnect() or bundled_openconnect())

    def is_up(self):
        if not (self.connected and self.proc is not None and self.proc.poll() is None):
            return False
        # openconnect can also end by itself (the gateway ended the session).
        try:
            with open(self.pidfile) as f:
                pid = int(f.read().strip())
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, ValueError, IndexError):
            return False

    def elevation(self):
        """How to run the root script, as (name, argv prefix), or None.
        pkexec (sudo from a terminal), unless this process may not gain
        privileges (appimage-run's sandbox on NixOS, some launchers): then
        systemd-run, which asks systemd over D-Bus (polkit asks for the
        password), not a setuid program."""
        if not no_new_privs():
            path = elevator(self.elevate)
            if path:
                return self.elevate, [path]
        path = which("systemd-run") or next((p for p in SYSTEMD_RUN if os.access(p, os.X_OK)), None)
        if path:
            return "systemd-run", [
                path, "--quiet", "--pipe", "--wait", "--collect", "--service-type=exec",
                "--description=Termite VPN (openconnect)",
            ]  # fmt: skip
        return None

    def ensure(self, gateway, log):
        if not (self.openconnect or self.bundle):
            raise TunnelError("openconnect 9 or newer is not installed")
        if self.elevation() is None:
            if no_new_privs():
                raise TunnelError(
                    "Termite runs with no new privileges allowed (appimage-run's sandbox, or its launcher), so it can't start "
                    "openconnect as root, and systemd-run isn't here to ask systemd instead; use NetworkManager, or run the AppImage directly"
                )
            raise TunnelError(f"{self.elevate} is not installed; it is needed to start openconnect as root")

    def _bundle_tar(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name in sorted(os.listdir(self.bundle)):
                tar.add(os.path.join(self.bundle, name), arcname=name)
        return buf.getvalue()

    def up(self, session, log):
        cmd = [self.openconnect] if self.openconnect else []
        cmd += ["--protocol=anyconnect", "--useragent=AnyConnect", "--cookie-on-stdin"]
        if session.get("servercert"):
            cmd.append(f"--servercert={session['servercert']}")
        cmd.append(session["url"])
        bundle = self._bundle_tar() if self.bundle else b""
        how, prefix = self.elevation() or (self.elevate, [self.elevate])
        log(f"starting {self.label} as root (through {how})")
        self.proc = subprocess.Popen(
            [*prefix, "/bin/sh", "-c", OPENCONNECT_SCRIPT, "termite", self.pidfile, dns.RESOLV_CONF, self.script, *cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=host_env(),
        )
        errors = []

        def drain(stream=self.proc.stderr):
            with stream:
                for raw in stream:
                    line = raw.decode(errors="replace").rstrip()
                    if line:
                        errors.append(line)
                        log(line)

        threading.Thread(target=drain, daemon=True).start()
        try:
            self.proc.stdin.write(f"{session['cookie']}\n{len(bundle)}\n".encode() + bundle)
            self.proc.stdin.flush()
        except BrokenPipeError:
            pass
        self.dns = []
        while True:
            status = self.proc.stdout.readline().decode(errors="replace").strip()
            if not status.startswith("dns "):
                break
            self.dns = status.split()[1:]
        if status != "connected":
            self.proc.wait()
            self.proc.stdout.close()
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
            self.proc = None
            if how != "sudo" and status == "" and not errors:
                raise TunnelError("the administrator password was not given")
            raise TunnelError(errors[-1] if errors else f"openconnect did not start ({status or 'no output'})")
        self.connected = True

    def dns_servers(self):
        """The DNS servers vpnc-script was given, as the root helper saw it
        hand them on; else the nameservers of /etc/resolv.conf when
        vpnc-script wrote that itself (its mark says so)."""
        if self.dns:
            return list(self.dns)
        text, servers = dns.read_resolv_conf()
        return servers if text.startswith("#@VPNC_GENERATED@") else []

    def prefer_dns(self, server, session, log):
        """The VPN's servers with this one first, put back where vpnc-script
        put them by the root helper (dns_apply there); vpnc-script takes
        them away again on disconnect as before."""
        order = [server] + [s for s in self.dns_servers() if s != server]
        if self.proc is None:
            return False
        try:
            self.proc.stdin.write(f"dns {' '.join(order)}\n".encode())
            self.proc.stdin.flush()
            answer = self.proc.stdout.readline().decode(errors="replace").strip()
        except (BrokenPipeError, ValueError, OSError):
            answer = ""
        if answer != "ok":
            log(f"DNS: could not put {server} first: the VPN's servers weren't found in {dns.RESOLV_CONF}, in systemd-resolved or in resolvconf")
            return False
        log(f"DNS: {server} is now the first of the VPN's servers")
        return True

    def forget_dns(self, log):
        """Nothing to take back: vpnc-script sets DNS up anew on every
        connection."""

    def down(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.write(b"\n")
            self.proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        self.proc.stdout.close()
        self.proc = None
        self.connected = False


def choose(cfg, elevate="pkexec"):
    backend = cfg.get("backend") or "auto"
    if backend == "networkmanager":
        return NetworkManager(cfg.get("connection"))
    if backend == "openconnect":
        return OpenConnect(elevate)
    if NetworkManager.available() and NetworkManager.plugin_installed():
        return NetworkManager(cfg.get("connection"))
    if OpenConnect.available():
        return OpenConnect(elevate)
    if NetworkManager.available():
        return NetworkManager(cfg.get("connection"))  # ensure() says what is missing
    raise TunnelError("neither NetworkManager (with its OpenConnect plugin) nor openconnect 9 or newer is installed")
