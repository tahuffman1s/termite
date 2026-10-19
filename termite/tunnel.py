"""The tunnel, once sso.sign_in() has a session. Two ways to run it:

- NetworkManager, with its OpenConnect plugin: the session goes to a
  NetworkManager connection (made if missing) through nmcli's own secret
  agent, as the plugin's sign-in dialogs would hand it over. NetworkManager
  sets up the routes and DNS, the desktop's network menu shows the VPN and
  can disconnect it, and it stays up when this app closes.
- openconnect directly, as root through pkexec (or sudo in a terminal), for
  systems without NetworkManager's plugin. openconnect's vpnc-script sets up
  routes and DNS. The tunnel lasts as long as this app holds it.
"""

import glob
import os
import subprocess
import threading

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
        for d in ("/usr/lib/NetworkManager/VPN", "/usr/lib64/NetworkManager/VPN", "/usr/local/lib/NetworkManager/VPN", "/etc/NetworkManager/VPN"):
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


# Run as root: reads the cookie from stdin, starts openconnect in the
# background (it returns once the tunnel is up), says "connected" on stdout,
# then waits for a line (or the end) of stdin, or a signal, and stops it.
# Holding the stop on stdin means the tunnel never outlives the app.
OPENCONNECT_SCRIPT = r"""
pidfile=$1; shift
IFS= read -r cookie || exit 2
printf '%s\n' "$cookie" | "$@" --background --pid-file="$pidfile" >&2 || { echo "failed $?"; exit 1; }
stop() { [ -s "$pidfile" ] && kill -INT "$(cat "$pidfile")" 2>/dev/null; rm -f "$pidfile"; exit 0; }
trap stop INT TERM HUP
echo connected
read -r _ || true
stop
"""


class OpenConnect:
    label = "openconnect"
    persistent = False

    def __init__(self, elevate="pkexec"):
        self.openconnect = which("openconnect", sbin=True)
        self.elevate = elevate
        self.proc = None
        self.connected = False
        self.pidfile = f"/run/termite-{os.getuid()}.pid"

    @staticmethod
    def available():
        return bool(which("openconnect", sbin=True))

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

    def ensure(self, gateway, log):
        if not self.openconnect:
            raise TunnelError("openconnect is not installed")
        if not which(self.elevate):
            raise TunnelError(f"{self.elevate} is not installed; it is needed to start openconnect as root")

    def up(self, session, log):
        cmd = [self.openconnect, "--protocol=anyconnect", "--useragent=AnyConnect", "--cookie-on-stdin"]
        if session.get("servercert"):
            cmd.append(f"--servercert={session['servercert']}")
        cmd.append(session["url"])
        log(f"starting openconnect as root (through {self.elevate})")
        self.proc = subprocess.Popen(
            [which(self.elevate), "/bin/sh", "-c", OPENCONNECT_SCRIPT, "termite", self.pidfile, *cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=host_env(),
        )
        errors = []

        def drain(stream=self.proc.stderr):
            with stream:
                for line in stream:
                    line = line.rstrip()
                    if line:
                        errors.append(line)
                        log(line)

        threading.Thread(target=drain, daemon=True).start()
        try:
            self.proc.stdin.write(session["cookie"] + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            pass
        status = self.proc.stdout.readline().strip()
        if status != "connected":
            self.proc.wait()
            self.proc.stdout.close()
            self.proc = None
            if self.elevate == "pkexec" and status == "" and not errors:
                raise TunnelError("the administrator password was not given")
            raise TunnelError(errors[-1] if errors else f"openconnect did not start ({status or 'no output'})")
        self.connected = True

    def down(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.write("\n")
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
    raise TunnelError("neither NetworkManager (with its OpenConnect plugin) nor openconnect is installed")
