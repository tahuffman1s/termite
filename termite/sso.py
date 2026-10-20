"""Sign in to a Cisco AnyConnect gateway whose login is Microsoft (SAML) SSO,
in a real Chromium-based browser window instead of an embedded web view.

Cisco's "embedded browser" SSO hands the login back to the VPN client as a
cookie (acSamlv2Token) set in the browser that did the login. Embedded web
views (Plasma's, openconnect's) can't use passkeys on a phone (no QR code)
or browser extensions; Chromium can do both. So this does the AnyConnect XML
exchange that openconnect would do, opens the SSO page in a Chromium window
with its own profile, reads the token cookie over the DevTools pipe, and
returns the resulting session: {"url": ..., "cookie": ..., "servercert": ...},
the gateway, cookie and gwcert secrets that NetworkManager's OpenConnect
plugin connects with (or openconnect --cookie-on-stdin --servercert ... URL).

The module only uses the standard library (plus the openssl command, or the
cryptography package when it is there), so it also runs on its own:

Usage: python3 sso.py GATEWAY [GROUP]   (prints the session as JSON)
Environment: TERMITE_BROWSER (browser), TERMITE_PROFILE (its profile dir).
"""

import base64
import fcntl
import hashlib
import http.client
import json
import os
import re
import select
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

LOGIN_TIMEOUT = 600  # seconds the user has to finish signing in

# Chromium-based browsers, as (name, commands) in order of preference. They
# all take --user-data-dir, --app and --remote-debugging-pipe.
BROWSERS = [
    ("Chromium", ["chromium", "chromium-browser"]),
    ("Google Chrome", ["google-chrome-stable", "google-chrome"]),
    ("Brave", ["brave-browser", "brave"]),
    ("Microsoft Edge", ["microsoft-edge-stable", "microsoft-edge"]),
    ("Vivaldi", ["vivaldi-stable", "vivaldi"]),
]


class SignInError(Exception):
    pass


class Cancelled(SignInError):
    def __init__(self):
        super().__init__("sign-in cancelled")


def _stderr_log(msg):
    print(f"termite: {msg}", file=sys.stderr, flush=True)


def host_env():
    """The environment for programs this starts (browser, nmcli, openssl,
    openconnect): the AppImage's AppRun points LD_LIBRARY_PATH, PYTHONHOME
    and the GTK variables at its bundled libraries, which the host's programs
    must not load. AppRun keeps the originals as TERMITE_HOST_<NAME>."""
    env = dict(os.environ)
    unset = env.pop("TERMITE_HOST_UNSET", "").split()
    for name in env.pop("TERMITE_HOST_VARS", "").split():
        value = env.pop(f"TERMITE_HOST_{name}", None)
        if name in unset or value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def which(cmd, sbin=False):
    """A command on the host's PATH (with the sbin directories, which Debian
    leaves off users' PATH, if sbin)."""
    path = host_env().get("PATH", os.defpath)
    if sbin:
        path += ":/usr/local/sbin:/usr/sbin:/sbin"
    return shutil.which(cmd, path=path)


def data_dir():
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "termite")


def bundled_browser():
    """The Chromium in the AppImage (AppRun says where), if there is one."""
    path = os.environ.get("TERMITE_CHROMIUM")
    return path if path and os.access(path, os.X_OK) else None


def is_bundled(browser):
    return bool(browser) and browser == bundled_browser()


_userns = None


def userns_restricted():
    """Whether Chromium can't sandbox itself with unprivileged user
    namespaces (which it needs without its setuid helper): turned off by
    AppArmor (Ubuntu 24.04's default) or an old Debian switch, or refused
    outright (some containers). Checked once, by trying."""
    global _userns
    if _userns is None:
        _userns = False
        for path, off in (("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", "1"), ("/proc/sys/kernel/unprivileged_userns_clone", "0")):
            try:
                with open(path) as f:
                    if f.read().strip() == off:
                        _userns = True
            except OSError:
                pass
        if not _userns:
            probe = "import ctypes, sys; sys.exit(ctypes.CDLL(None, use_errno=True).unshare(0x10000000) != 0)"  # CLONE_NEWUSER
            try:
                _userns = subprocess.run([sys.executable, "-c", probe], capture_output=True, timeout=10).returncode != 0
            except (OSError, subprocess.TimeoutExpired):
                pass
    return _userns


def browser_args(browser):
    """Extra switches for the bundled Chromium: Wayland when the desktop is
    (it would use X11), and no sandbox where the system doesn't allow the
    one it can use from an AppImage (its setuid helper can't work there)."""
    if not is_bundled(browser):
        return []
    return ["--ozone-platform-hint=auto"] + (["--no-sandbox"] if userns_restricted() else [])


def browser_log():
    """Where the browser's own messages go: nowhere, unless
    TERMITE_BROWSER_LOG is set (then to Termite's stderr)."""
    return None if os.environ.get("TERMITE_BROWSER_LOG") else subprocess.DEVNULL


def browser_env(browser):
    """The environment to start a browser with: the system's, and for the
    bundled Chromium the bundled libraries the system lacks (AppRun links
    them into TERMITE_CHROMIUM_LIBS), with the font and keyboard settings
    that go with them."""
    env = host_env()
    if is_bundled(browser):
        libs = os.environ.get("TERMITE_CHROMIUM_LIBS", "")
        env["LD_LIBRARY_PATH"] = ":".join(p for p in (libs, env.get("LD_LIBRARY_PATH", "")) if p)
        for var in ("FONTCONFIG_FILE", "FONTCONFIG_PATH", "XKB_CONFIG_ROOT"):
            if var in os.environ:
                env[var] = os.environ[var]
    return env


def find_browsers():
    """The Chromium-based browsers here, as [(name, path)]: the AppImage's
    own Chromium first, if there is one, then the installed ones."""
    found, seen = [], set()
    if bundled_browser():
        found.append(("Chromium (built in)", bundled_browser()))
        seen.add(os.path.realpath(bundled_browser()))
    for name, cmds in BROWSERS:
        for cmd in cmds:
            path = which(cmd)
            if path and os.path.realpath(path) not in seen:
                seen.add(os.path.realpath(path))
                found.append((name, path))
                break
    return found


def default_browser():
    if os.environ.get("TERMITE_BROWSER"):
        return os.environ["TERMITE_BROWSER"]
    browsers = find_browsers()
    return browsers[0][1] if browsers else None


def default_profile(browser):
    """The sign-in profile of a browser: its own directory per browser, since
    Chrome and Chromium (say) don't share profiles well: Chromium's is
    ~/.local/share/termite/chromium."""
    if os.environ.get("TERMITE_PROFILE"):
        return os.environ["TERMITE_PROFILE"]
    if is_bundled(browser):
        # Its own, apart from an installed Chromium's (of another version).
        return os.path.join(data_dir(), "chromium-builtin")
    base = os.path.basename(browser or "chromium")
    kind = re.sub(r"-(stable|browser|beta|dev|unstable)$", "", base) or "chromium"
    if browser and (browser.startswith("/snap/") or os.path.realpath(browser).startswith("/snap/")):
        # A snap browser can only write to its own directory under ~/snap.
        return os.path.expanduser(f"~/snap/{base}/common/termite")
    return os.path.join(data_dir(), kind)


# Pages to open the sign-in profile on, to set it up: add a password manager
# or passkey extension, sign in to it, change settings. Nothing is installed
# in it by default.
SETUP_PAGES = {
    "extensions": "chrome://extensions",
    "store": "https://chromewebstore.google.com/category/extensions",
    "settings": "chrome://settings",
}


def open_profile(browser, profile, page="extensions"):
    """The sign-in profile in a normal browser window; returns the process.
    It has to be closed before signing in, since one profile can only be
    open in one browser process."""
    os.makedirs(profile, exist_ok=True)
    return subprocess.Popen(
        [browser, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", *browser_args(browser), SETUP_PAGES.get(page, page)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=browser_log(),
        env=browser_env(browser),
        start_new_session=True,
    )


def strap_keypair():
    """A fresh P-256 key pair, base64 DER (private as SEC1, public as SPKI),
    like the ones openconnect sends in its X-AnyConnect-STRAP-* headers.
    Gateways answer 404 without them, and tie the session to the first key:
    the tunnel must prove it holds that private key when it connects."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        priv = key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
        pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()
    except ImportError:
        pass
    try:
        env = host_env()

        def ossl(args, data=None):
            return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True, env=env).stdout

        pem = ossl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"])
        priv = ossl(["ec", "-outform", "DER"], pem)
        pub = ossl(["pkey", "-pubout", "-outform", "DER"], pem)
        return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()
    except (OSError, subprocess.CalledProcessError):
        return None, None


def openconnect_version():
    # Present ourselves as the openconnect that runs the tunnel does (the
    # system's, else the AppImage's); the gateway only offers SSO to user
    # agents that say "AnyConnect".
    bundled = os.path.join(os.environ.get("TERMITE_OPENCONNECT_DIR", ""), "openconnect")
    for path in (which("openconnect", sbin=True), bundled if os.access(bundled, os.X_OK) else None):
        if not path:
            continue
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, env=host_env()).stdout
            m = re.search(r"OpenConnect version (\S+)", out)
            if m:
                return m.group(1)
        except OSError:
            pass
    return "v9.21"


class Gateway:
    def __init__(self, url, version):
        self.url = url
        self.version = version
        self.cookies = {}
        self.tls = ssl.create_default_context()
        self.cert_sha1 = ""  # of the gateway's (verified) certificate
        # The same headers, in the same order and spelling, as openconnect's
        # requests (http.client keeps both; urllib would re-case them).
        self.useragent = f"AnyConnect-compatible OpenConnect VPN Agent {version}"
        self.headers = {
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "X-Transcend-Version": "1",
            "X-Aggregate-Auth": "1",
            "X-Support-HTTP-Auth": "true",
        }
        # The DH key only matters for external-browser mode; the first one
        # is the session's (see strap_keypair).
        self.strap_privkey, strap = strap_keypair()
        _, strap_dh = strap_keypair()
        if strap and strap_dh:
            self.headers["X-AnyConnect-STRAP-Pubkey"] = strap
            self.headers["X-AnyConnect-STRAP-DH-Pubkey"] = strap_dh
        else:
            self.strap_privkey = None

    def post(self, step, body):
        for _ in range(5):
            u = urllib.parse.urlsplit(self.url)
            if u.scheme == "https":
                conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=30, context=self.tls)
            else:
                conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
            try:
                conn.putrequest("POST", (u.path or "/") + (f"?{u.query}" if u.query else ""), skip_accept_encoding=True)
                conn.putheader("User-Agent", self.useragent)
                if self.cookies:
                    conn.putheader("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
                for name, value in self.headers.items():
                    conn.putheader(name, value)
                # openconnect pads the body length to a multiple of 64.
                conn.putheader("X-Pad", "0" * (64 * (1 + len(body) // 64) - len(body)))
                conn.putheader("Content-Type", "application/xml; charset=utf-8")
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                # Before getresponse(), which closes the socket when the
                # gateway says "Connection: close".
                if u.scheme == "https" and conn.sock is not None:
                    der = conn.sock.getpeercert(binary_form=True)
                    if der:
                        self.cert_sha1 = hashlib.sha1(der).hexdigest()
                resp = conn.getresponse()
                data = resp.read()
                for header in resp.headers.get_all("Set-Cookie") or []:
                    name, _, rest = header.partition("=")
                    self.cookies[name.strip()] = rest.split(";", 1)[0].strip()
            finally:
                conn.close()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                # Gateways send group URLs and load balancing this way.
                self.url = urllib.parse.urljoin(self.url, resp.getheader("Location"))
                continue
            if resp.status != 200:
                raise SignInError(f"{step}: the gateway answered HTTP {resp.status} {resp.reason} for {self.url}")
            return ET.fromstring(data)
        raise SignInError(f"{step}: too many redirects (last: {self.url})")

    def request(self, kind, children):
        # Laid out like openconnect's (libxml2's) requests.
        root = ET.Element("config-auth", {"client": "vpn", "type": kind, "aggregate-auth-version": "2"})
        ET.SubElement(root, "version", {"who": "vpn"}).text = self.version
        ET.SubElement(root, "device-id").text = "linux-64"
        caps = ET.SubElement(root, "capabilities")
        for method in ("single-sign-on-v2", "single-sign-on-external-browser"):
            ET.SubElement(caps, "auth-method").text = method
        for child in children:
            root.append(child)
        body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
        return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode()


def element(tag, text=None, children=()):
    e = ET.Element(tag)
    e.text = text
    for c in children:
        e.append(c)
    return e


def text(root, path):
    node = root.find(path)
    return node.text.strip() if node is not None and node.text else ""


class Chromium:
    """A Chromium window driven over --remote-debugging-pipe (fds 3 and 4)."""

    def __init__(self, browser, profile, url):
        to_child_r, self.to_child = os.pipe()
        self.from_child, from_child_w = os.pipe()
        # Park the child's ends above 3/4 first so the dup2s can't clobber them.
        hi_r = fcntl.fcntl(to_child_r, fcntl.F_DUPFD_CLOEXEC, 10)
        hi_w = fcntl.fcntl(from_child_w, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(to_child_r)
        os.close(from_child_w)

        def child_fds():
            os.dup2(hi_r, 3)
            os.dup2(hi_w, 4)

        os.makedirs(profile, exist_ok=True)
        self.started = time.monotonic()
        self.proc = subprocess.Popen(
            [
                browser,
                f"--user-data-dir={profile}",
                "--remote-debugging-pipe",
                "--no-first-run",
                "--no-default-browser-check",
                *browser_args(browser),
                f"--app={url}",
            ],
            preexec_fn=child_fds,
            pass_fds=(3, 4),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=browser_log(),
            env=browser_env(browser),
        )
        os.close(hi_r)
        os.close(hi_w)
        self.buf = b""
        self.next_id = 0

    def call(self, method, params=None, timeout=10):
        self.next_id += 1
        msg_id = self.next_id
        msg = {"id": msg_id, "method": method, "params": params or {}}
        os.write(self.to_child, json.dumps(msg).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while True:
            while b"\0" in self.buf:
                raw, self.buf = self.buf.split(b"\0", 1)
                reply = json.loads(raw)
                if reply.get("id") == msg_id:
                    if "error" in reply:
                        raise RuntimeError(f"{method}: {reply['error']}")
                    return reply.get("result", {})
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            ready, _, _ = select.select([self.from_child], [], [], left)
            if ready:
                chunk = os.read(self.from_child, 65536)
                if not chunk:
                    raise EOFError("the browser closed")
                self.buf += chunk

    def cookies(self):
        return self.call("Storage.getCookies").get("cookies", [])

    def close(self):
        try:
            self.call("Browser.close", timeout=5)
        except (OSError, RuntimeError, TimeoutError, EOFError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for fd in (self.to_child, self.from_child):
            try:
                os.close(fd)
            except OSError:
                pass


def browser_login(browser, profile, sso_login, token_name, error_name, cancel=None):
    chrome = Chromium(browser, profile, sso_login)
    try:
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                cookies = chrome.cookies()
            except (EOFError, BrokenPipeError, TimeoutError):
                cookies = None
            if cookies is None or chrome.proc.poll() is not None:
                if time.monotonic() - chrome.started < 5:
                    # A browser already running on this profile takes the
                    # window over and the new process exits at once.
                    raise SignInError(
                        f"the browser did not start a sign-in window; if {profile} is open in another window, close it and try again"
                    )
                raise SignInError("the sign-in window was closed")
            for c in cookies:
                if c.get("name") == token_name and c.get("value"):
                    return c["value"]
                if error_name and c.get("name") == error_name and c.get("value"):
                    raise SignInError(f"the gateway reported a sign-in error: {c['value']}")
            if cancel is not None:
                if cancel.wait(1):
                    raise Cancelled()
            else:
                time.sleep(1)
        raise SignInError("sign-in timed out")
    finally:
        chrome.close()


def sign_in(gateway, group="", browser=None, profile=None, log=_stderr_log, cancel=None):
    """The whole sign-in; returns the session dict. Raises SignInError."""
    if not gateway:
        raise SignInError("no gateway address")
    browser = browser or default_browser()
    if not browser:
        raise SignInError("no Chromium-based browser found (Chromium, Google Chrome, Brave, Edge or Vivaldi)")
    profile = profile or default_profile(browser)
    if "://" not in gateway:
        gateway = "https://" + gateway
    if not gateway.endswith("/"):
        gateway += "/"

    try:
        gw = Gateway(gateway, openconnect_version())

        # 1. init: ask for the login form, saying we can do SSO.
        init = [element("group-access", gateway)]
        if group:
            init.append(element("group-select", group))
        log(f"contacting {gateway}")
        reply = gw.post("asking for the sign-in form", gw.request("init", init))

        auth = reply.find("auth")
        sso_login = text(reply, "auth/sso-v2-login")
        if auth is None or not sso_login:
            msg = text(reply, "auth/message") or text(reply, "auth/error") or "no SSO login offered"
            raise SignInError(f"the gateway did not offer single sign-on ({msg})")
        if text(reply, "auth/sso-v2-browser-mode") == "external":
            # Cisco's external-browser mode returns the token encrypted to the
            # STRAP key via a localhost callback; openconnect handles that itself.
            raise SignInError("the gateway wants external-browser sign-in; use: openconnect --protocol=anyconnect " + gateway)
        token_name = text(reply, "auth/sso-v2-token-cookie-name") or "acSamlv2Token"
        error_name = text(reply, "auth/sso-v2-error-cookie-name")
        opaque = reply.find("opaque")
        connect_url = gw.url  # the tunnel connects to the gateway, not the form
        # Like openconnect: the answer goes to the form's action, if it has one.
        form = reply.find("auth/form")
        if form is not None and form.get("action"):
            gw.url = urllib.parse.urljoin(gw.url, form.get("action"))

        # The group the form settled on (the selected option, else the first).
        if not group:
            sel = reply.find("auth/form/select[@name='group_list']")
            if sel is not None:
                opts = sel.findall("option")
                chosen = [o for o in opts if o.get("selected") == "true"] or opts
                if chosen:
                    group = (chosen[0].get("value") or chosen[0].text or "").strip()

        # 2. The sign-in in the browser; the gateway sets the token cookie.
        log("opening the sign-in window")
        token = browser_login(browser, profile, sso_login, token_name, error_name, cancel)

        # 3. auth-reply with the token; the gateway answers with the session.
        answer = []
        if opaque is not None:
            answer.append(opaque)
        answer.append(element("auth", children=[element("sso-token", token)]))
        if group:
            answer.append(element("group-select", group))
        done = gw.post("sending the sign-in", gw.request("auth-reply", answer))
    except (ssl.SSLError, ConnectionError, TimeoutError, http.client.HTTPException) as e:
        raise SignInError(f"could not talk to the gateway: {e}") from e
    except (ET.ParseError, OSError) as e:
        raise SignInError(str(e)) from e

    session = text(done, "session-token")
    if not session:
        msg = text(done, "auth/error") or text(done, "auth/message") or "no session token"
        raise SignInError(f"the gateway refused the sign-in ({msg})")
    # NetworkManager's OpenConnect plugin wants the gateway's certificate
    # fingerprint, as its own sign-in dialogs pass it: a bare hex SHA-1 of the
    # certificate we just verified against the system CAs. openconnect's
    # --servercert takes the same.
    servercert = gw.cert_sha1 or text(done, "config/vpn-base-config/server-cert-hash")
    # The cookie as openconnect --authenticate would print it: with the STRAP
    # private key, which the tunnel uses to prove it is the same client.
    cookie = f"openconnect_strapkey={gw.strap_privkey}; webvpn={session}" if gw.strap_privkey else session
    log("signed in")
    return {"url": connect_url, "cookie": cookie, "servercert": servercert}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (1, 2):
        raise SystemExit(__doc__)
    try:
        session = sign_in(argv[0], argv[1] if len(argv) == 2 else "")
    except SignInError as e:
        raise SystemExit(f"termite: {e}")
    json.dump(session, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
