"""Tests against a fake AnyConnect gateway and fake browser, nmcli, pkexec
and openconnect (tests/fakes). Run: python3 -m unittest discover tests"""

import base64
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
FAKES = os.path.join(HERE, "fakes")
sys.path.insert(0, SRC)

from termite import config, dns, integrate, sso, tunnel  # noqa: E402

TOKEN = "saml-token-123"
SESSION = "session-456"


class FakeGateway(http.server.BaseHTTPRequestHandler):
    """The two AnyConnect requests (init, auth-reply) and the SSO page."""

    requests = []

    def log_message(self, *args):
        pass

    def reply(self, body, cookie=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # the SSO page: "signed in", the token cookie
        self.reply(b"<html>signed in</html>", f"acSamlv2Token={TOKEN}; path=/; Secure")

    def do_POST(self):
        root = ET.fromstring(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGateway.requests.append((self.path, dict(self.headers), root))
        port = self.server.server_address[1]
        if root.get("type") == "init":
            self.reply(
                f"""<?xml version="1.0"?><config-auth client="vpn" type="auth-request">
<opaque is-for="sg"><tunnel-group>Staff</tunnel-group></opaque>
<auth id="main"><title>Login</title>
<sso-v2-login>http://127.0.0.1:{port}/sso</sso-v2-login>
<sso-v2-token-cookie-name>acSamlv2Token</sso-v2-token-cookie-name>
<sso-v2-error-cookie-name>acSamlv2Error</sso-v2-error-cookie-name>
<form action="/auth"><select name="group_list"><option>Other</option><option selected="true">Staff</option></select></form>
</auth></config-auth>""".encode(),
                "webvpncontext=abc; path=/",
            )
        else:
            ok = root.findtext("auth/sso-token") == TOKEN
            self.reply(
                (f"<config-auth><session-token>{SESSION}</session-token></config-auth>" if ok else "<config-auth><auth><error>bad token</error></auth></config-auth>").encode()
            )


def read(path):
    with open(path) as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {
            "PATH": f"{FAKES}:{os.environ['PATH']}",
            "FAKE_BROWSER_LOG": os.path.join(self.tmp, "browser.log"),
            "FAKE_NM": self.tmp,
            "FAKE_OC": self.tmp,
            "TERMITE_RUN_DIR": self.tmp,  # the root helper's /run, for a user
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "XDG_DATA_HOME": os.path.join(self.tmp, "data"),
        }
        self.saved = dict(os.environ)
        os.environ.update(self.env)
        # The fake pkexec, even where a real setuid one is installed.
        self.elevator = tunnel.elevator
        tunnel.elevator = lambda name: os.path.join(FAKES, name)
        for var in ("TERMITE_CHROMIUM", "TERMITE_CHROMIUM_LIBS", "TERMITE_BROWSER", "TERMITE_PROFILE", "TERMITE_GATEWAY", "TERMITE_GROUP", "FAKE_BROWSER", "FAKE_OC_FAIL", "FAKE_OC_DNS", "FAKE_OC_SCRIPT_DELAY", "APPIMAGE"):
            os.environ.pop(var, None)

    def tearDown(self):
        tunnel.elevator = self.elevator
        os.environ.clear()
        os.environ.update(self.saved)


class SignIn(Base):
    def setUp(self):
        super().setUp()
        FakeGateway.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.gateway = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_session(self):
        browser = os.path.join(FAKES, "chromium")
        session = sso.sign_in(self.gateway, browser=browser, log=lambda m: None)
        self.assertEqual(session["url"], self.gateway + "/")
        strap, _, webvpn = session["cookie"].partition("; ")
        self.assertTrue(strap.startswith("openconnect_strapkey="))
        self.assertTrue(base64.b64decode(strap.split("=", 1)[1]))
        self.assertEqual(webvpn, f"webvpn={SESSION}")
        # As openconnect: init to the gateway, the answer to the form's action
        # with the gateway's cookie, the selected group and the opaque block.
        (p1, h1, init), (p2, h2, answer) = FakeGateway.requests
        self.assertEqual((p1, p2), ("/", "/auth"))
        self.assertIn("AnyConnect", h1["User-Agent"])
        self.assertIn("X-AnyConnect-STRAP-Pubkey", h1)
        self.assertEqual(h2["Cookie"], "webvpncontext=abc")
        self.assertEqual(answer.findtext("group-select"), "Staff")
        self.assertIsNotNone(answer.find("opaque"))
        # The browser got its own profile and the SSO page as an app window.
        args = json.loads(read(self.env["FAKE_BROWSER_LOG"]).splitlines()[0])
        self.assertIn(f"--user-data-dir={os.path.join(self.tmp, 'data', 'termite', 'chromium')}", args)
        self.assertIn(f"--app={self.gateway}/sso", args)

    def test_cli_prints_json(self):
        env = dict(os.environ, TERMITE_BROWSER=os.path.join(FAKES, "chromium"))
        r = subprocess.run([sys.executable, os.path.join(SRC, "termite", "sso.py"), self.gateway], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"webvpn={SESSION}", json.loads(r.stdout)["cookie"])

    def test_window_closed(self):
        os.environ["FAKE_BROWSER"] = "close"
        with self.assertRaisesRegex(sso.SignInError, "closed|did not start"):
            sso.sign_in(self.gateway, browser=os.path.join(FAKES, "chromium"), log=lambda m: None)

    def test_profile_in_use(self):
        os.environ["FAKE_BROWSER"] = "exit"
        with self.assertRaisesRegex(sso.SignInError, "close it and try again"):
            sso.sign_in(self.gateway, browser=os.path.join(FAKES, "chromium"), log=lambda m: None)

    def test_cancel(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(sso.Cancelled):
            sso.sign_in(self.gateway, browser=os.path.join(FAKES, "chromium"), log=lambda m: None, cancel=cancel)


class Browsers(Base):
    def test_profiles(self):
        data = os.path.join(self.tmp, "data", "termite")
        self.assertEqual(sso.default_profile("/usr/bin/chromium-browser"), os.path.join(data, "chromium"))
        self.assertEqual(sso.default_profile("/opt/google/chrome/google-chrome-stable"), os.path.join(data, "google-chrome"))
        self.assertEqual(sso.default_profile("/snap/bin/chromium"), os.path.expanduser("~/snap/chromium/common/termite"))

    def test_find(self):
        self.assertEqual(sso.find_browsers()[0], ("Chromium", os.path.join(FAKES, "chromium")))

    def test_open_profile(self):
        sso.open_profile(os.path.join(FAKES, "chromium"), os.path.join(self.tmp, "p"), "store").wait(10)
        args = json.loads(read(self.env["FAKE_BROWSER_LOG"]).splitlines()[0])
        self.assertEqual(args[-1], sso.SETUP_PAGES["store"])
        self.assertNotIn("--remote-debugging-pipe", args)

    def test_host_env(self):
        os.environ.update(
            TERMITE_HOST_VARS="LD_LIBRARY_PATH PYTHONHOME",
            TERMITE_HOST_UNSET=" PYTHONHOME",
            TERMITE_HOST_LD_LIBRARY_PATH="/host/lib",
            LD_LIBRARY_PATH="/appimage/lib:/host/lib",
            PYTHONHOME="/appimage/usr",
        )
        env = sso.host_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/host/lib")
        self.assertNotIn("PYTHONHOME", env)
        self.assertFalse([k for k in env if k.startswith("TERMITE_HOST_")])


SESSION_DICT = {"url": "https://vpn.example.com/", "cookie": "openconnect_strapkey=AAA; webvpn=S", "servercert": "ab12"}


class NetworkManagerTunnel(Base):
    def test_up_down(self):
        with open(os.path.join(self.tmp, "connections"), "w") as f:
            f.write("Termite\n")
        t = tunnel.NetworkManager("Termite")
        self.assertTrue(t.available())
        self.assertFalse(t.is_up())
        t.ensure("vpn.example.com", lambda m: None)
        t.up(SESSION_DICT, lambda m: None)
        self.assertTrue(t.is_up())
        self.assertEqual(
            read(os.path.join(self.tmp, "secrets")),
            "vpn.secrets.gateway:https://vpn.example.com/\nvpn.secrets.cookie:openconnect_strapkey=AAA; webvpn=S\n"
            "vpn.secrets.gwcert:ab12\nvpn.secrets.resolve:\n",
        )
        t.down()
        self.assertFalse(t.is_up())

    def test_adds_connection(self):
        t = tunnel.NetworkManager("Termite")
        t.plugin_installed = lambda: True
        t.ensure("https://vpn.example.com/", lambda m: None)
        calls = read(os.path.join(self.tmp, "calls"))
        self.assertIn("connection add type vpn con-name Termite vpn.service-type org.freedesktop.NetworkManager.openconnect", calls)
        self.assertIn("vpn.data gateway=vpn.example.com,protocol=anyconnect,", calls)

    def test_no_plugin(self):
        t = tunnel.NetworkManager("Termite")
        t.plugin_installed = lambda: False
        with self.assertRaisesRegex(tunnel.TunnelError, "OpenConnect plugin"):
            t.ensure("vpn.example.com", lambda m: None)


class OpenConnectTunnel(Base):
    def make(self):
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        return t

    def test_up_down(self):
        t = self.make()
        t.ensure("vpn.example.com", lambda m: None)
        lines = []
        t.up(SESSION_DICT, lines.append)
        self.assertTrue(t.is_up())
        self.assertEqual(read(os.path.join(self.tmp, "cookie")).strip(), SESSION_DICT["cookie"])
        args = read(os.path.join(self.tmp, "args"))
        self.assertIn("--cookie-on-stdin --servercert=ab12 https://vpn.example.com/ --background --pid-file=", args)
        self.assertNotIn("webvpn", args)  # the cookie never goes on the command line
        self.assertRegex(args, rf"^--script={re.escape(self.tmp)}/termite-openconnect\.\w+/script --protocol=anyconnect ")  # around the system's vpnc-script
        self.assertEqual(t.script, os.path.join(FAKES, "vpnc-script"))
        pid = int(read(t.pidfile))
        time.sleep(0.5)  # the fake tunnel's SIGINT handler is up
        t.down()
        self.assertFalse(t.is_up())
        time.sleep(0.2)
        self.assertFalse(os.path.exists(f"/proc/{pid}") and read(f"/proc/{pid}/stat").rsplit(")", 1)[1].split()[0] != "Z")

    def test_tunnel_ends_by_itself(self):
        t = self.make()
        t.up(SESSION_DICT, lambda m: None)
        os.kill(int(read(t.pidfile)), 15)
        time.sleep(0.3)
        self.assertFalse(t.is_up())
        t.down()

    def test_failure(self):
        os.environ["FAKE_OC_FAIL"] = "the gateway said no"
        t = self.make()
        with self.assertRaisesRegex(tunnel.TunnelError, "the gateway said no"):
            t.up(SESSION_DICT, lambda m: None)
        self.assertFalse(t.is_up())


class Bundled(Base):
    """The AppImage's Chromium, as AppRun announces it."""

    def setUp(self):
        super().setUp()
        self.chrome = os.path.join(FAKES, "chromium")
        os.environ.update(
            TERMITE_CHROMIUM=self.chrome,
            TERMITE_CHROMIUM_LIBS="/appimage/run/chromium-libs",
            TERMITE_HOST_VARS="LD_LIBRARY_PATH FONTCONFIG_FILE",
            TERMITE_HOST_UNSET=" FONTCONFIG_FILE",
            TERMITE_HOST_LD_LIBRARY_PATH="/host/lib",
            LD_LIBRARY_PATH="/appimage/usr/lib",
            FONTCONFIG_FILE="/appimage/etc/fonts/fonts.conf",
        )

    def test_first_with_its_own_profile(self):
        self.assertEqual(sso.find_browsers()[0], ("Chromium (built in)", self.chrome))
        self.assertEqual(len([p for _, p in sso.find_browsers() if p == self.chrome]), 1)
        self.assertEqual(sso.default_profile(self.chrome), os.path.join(self.tmp, "data", "termite", "chromium-builtin"))

    def test_env(self):
        env = sso.browser_env(self.chrome)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/appimage/run/chromium-libs:/host/lib")
        self.assertEqual(env["FONTCONFIG_FILE"], "/appimage/etc/fonts/fonts.conf")
        # Any other browser gets the system's environment back.
        other = sso.browser_env("/usr/bin/google-chrome")
        self.assertEqual(other["LD_LIBRARY_PATH"], "/host/lib")
        self.assertNotIn("FONTCONFIG_FILE", other)

    def test_sandbox(self):
        restricted = sso.userns_restricted
        try:
            sso.userns_restricted = lambda: True
            self.assertEqual(sso.browser_args(self.chrome), ["--ozone-platform-hint=auto", "--no-sandbox"])
            self.assertEqual(sso.browser_args("/usr/bin/chromium"), [])
            sso.userns_restricted = lambda: False
            self.assertEqual(sso.browser_args(self.chrome), ["--ozone-platform-hint=auto"])
        finally:
            sso.userns_restricted = restricted

    def test_launched_with_it(self):
        sso.open_profile(self.chrome, sso.default_profile(self.chrome), "store").wait(10)
        args = json.loads(read(self.env["FAKE_BROWSER_LOG"]).splitlines()[0])
        self.assertIn(f"--user-data-dir={os.path.join(self.tmp, 'data', 'termite', 'chromium-builtin')}", args)


class BundledOpenConnect(Base):
    """The AppImage's openconnect, streamed to the root script."""

    def setUp(self):
        super().setUp()
        bundle = os.path.join(self.tmp, "bundle")
        os.makedirs(os.path.join(bundle, "lib"))
        for name in ("openconnect", "vpnc-script"):
            with open(os.path.join(bundle, name), "w") as f:
                f.write(read(os.path.join(FAKES, name)))
            os.chmod(os.path.join(bundle, name), 0o755)
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(self.run_dir)
        os.environ.update(TERMITE_OPENCONNECT_DIR=bundle, TERMITE_RUN_DIR=self.run_dir)
        self.system = tunnel.system_openconnect
        tunnel.system_openconnect = lambda: None

    def tearDown(self):
        tunnel.system_openconnect = self.system
        super().tearDown()

    def test_up_down(self):
        self.assertTrue(tunnel.OpenConnect.available())
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        self.assertEqual(t.label, "openconnect (built in)")
        self.assertEqual(t.script, "")
        t.ensure("vpn.example.com", lambda m: None)
        os.environ["FAKE_OC_DNS"] = "10.0.0.53 10.0.1.53"
        t.up(SESSION_DICT, lambda m: None)
        self.assertTrue(t.is_up())
        args = read(os.path.join(self.tmp, "args"))
        self.assertIn("--script=" + self.run_dir, args)
        # The wrapper ran the bundled vpnc-script with all it was given, and kept the DNS part.
        self.assertEqual(read(os.path.join(self.tmp, "script")), "connect tun9 10.0.0.53 10.0.1.53\n")
        self.assertEqual(t.dns_servers(), ["10.0.0.53", "10.0.1.53"])
        self.assertIn("--protocol=anyconnect --useragent=AnyConnect --cookie-on-stdin --servercert=ab12 https://vpn.example.com/", args)
        self.assertEqual(read(os.path.join(self.tmp, "cookie")).strip(), SESSION_DICT["cookie"])
        self.assertEqual(len(os.listdir(self.run_dir)), 1)
        time.sleep(0.5)  # the fake tunnel's SIGINT handler is up
        t.down()
        self.assertFalse(t.is_up())
        self.assertEqual(os.listdir(self.run_dir), [])  # cleaned up

    def test_old_system_openconnect_is_passed_over(self):
        tunnel.system_openconnect = self.system
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        with open(os.path.join(old, "openconnect"), "w") as f:
            f.write("#!/bin/sh\necho 'OpenConnect version v8.20-1'\n")
        os.chmod(os.path.join(old, "openconnect"), 0o755)
        os.environ["PATH"] = f"{old}:{os.environ['PATH']}"
        self.assertIsNone(tunnel.system_openconnect())
        self.assertEqual(tunnel.OpenConnect().label, "openconnect (built in)")


class NoNewPrivs(Base):
    """In appimage-run's sandbox: systemd-run instead of pkexec."""

    def setUp(self):
        super().setUp()
        self.nnp = tunnel.no_new_privs
        tunnel.no_new_privs = lambda: True

    def tearDown(self):
        tunnel.no_new_privs = self.nnp
        super().tearDown()

    def test_systemd_run(self):
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        self.assertEqual(t.elevation()[0], "systemd-run")
        self.assertIn("--pipe", t.elevation()[1])
        lines = []
        t.ensure("vpn.example.com", lines.append)
        t.up(SESSION_DICT, lines.append)
        self.assertIn("through systemd-run", " ".join(lines))
        self.assertTrue(t.is_up())
        time.sleep(0.5)
        t.down()
        self.assertFalse(t.is_up())

    def test_neither(self):
        os.environ["PATH"] = "/nonexistent"
        t = tunnel.OpenConnect("pkexec")
        t.openconnect = "/bin/true"
        known, tunnel.SYSTEMD_RUN = tunnel.SYSTEMD_RUN, ()
        try:
            with self.assertRaisesRegex(tunnel.TunnelError, "no new privileges"):
                t.ensure("vpn.example.com", lambda m: None)
        finally:
            tunnel.SYSTEMD_RUN = known


RESOLV_CONF = """#@VPNC_GENERATED@ -- this file is generated by vpnc
# and will be overwritten by vpnc
# as long as the above mark is intact
search corp.example.com
nameserver {}
nameserver {}
options ndots:1
"""


class Dns(Base):
    """Four DNS servers on loopback: one that answers (127.0.0.1), one that
    listens but never answers (127.0.0.2), a port nobody listens on
    (127.0.0.3), and a split-horizon one that answers names starting with
    "vpn" and stays silent on the rest (127.0.0.4)."""

    def setUp(self):
        super().setUp()
        self.answering = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.answering.bind(("127.0.0.1", 0))
        self.port = self.answering.getsockname()[1]
        self.silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.silent.bind(("127.0.0.2", self.port))
        self.split = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.split.bind(("127.0.0.4", self.port))
        self.questions = []
        threading.Thread(target=self.serve, args=(self.answering, lambda q: True), daemon=True).start()
        threading.Thread(target=self.serve, args=(self.split, lambda q: q[12:].startswith(b"\x03vpn")), daemon=True).start()
        self.saved_dns = dns.PORT, dns.TIMEOUT, dns.RESOLV_CONF, dns.local
        dns.PORT, dns.TIMEOUT = self.port, 0.6
        dns.RESOLV_CONF = os.path.join(self.tmp, "resolv.conf")
        # The fake servers are on loopback too: only the stub addresses count as a local resolver here.
        dns.local = lambda server: server in ("127.0.0.53", "::1")

    def tearDown(self):
        dns.PORT, dns.TIMEOUT, dns.RESOLV_CONF, dns.local = self.saved_dns
        for sock in (self.answering, self.silent, self.split):
            sock.close()
        super().tearDown()

    def serve(self, sock, answers):
        while True:
            try:
                data, peer = sock.recvfrom(512)
            except OSError:
                return
            self.questions.append(data)
            if answers(data):
                sock.sendto(data[:2] + b"\x81\x80" + data[4:], peer)  # the question back, as an answer

    def resolv_conf(self, *servers, text=RESOLV_CONF):
        with open(dns.RESOLV_CONF, "w") as f:
            f.write(text.format(*servers))

    def test_probe(self):
        self.assertIsNotNone(dns.probe("127.0.0.1", "vpn.example.com"))
        self.assertLess(dns.probe("127.0.0.1", "vpn.example.com"), 0.5)
        self.assertIsNone(dns.probe("127.0.0.2", "vpn.example.com"))
        self.assertIsNotNone(dns.probe("127.0.0.4", "vpn.example.com"))
        self.assertIsNone(dns.probe("127.0.0.4", "example.com"))
        start = time.monotonic()
        self.assertIsNone(dns.probe("127.0.0.3", "vpn.example.com"))
        self.assertLess(time.monotonic() - start, 0.5)  # port unreachable: no waiting
        qid, packet = dns.query("vpn.example.com")
        self.assertEqual(packet[12:], b"\x03vpn\x07example\x03com\x00\x00\x01\x00\x01")
        self.assertIn(packet[12:], self.questions[0])

    def test_fastest(self):
        def results(*times):
            return [(server, [("in", a), ("out", b)]) for server, (a, b) in zip("abcd", times)]

        self.assertEqual(dns.fastest(results((None, None), (0.004, 0.005), (0.002, 0.003))), "b")  # within a step: the gateway's order
        self.assertEqual(dns.fastest(results((0.9, 0.9), (0.004, 0.005))), "b")
        self.assertEqual(dns.fastest(results((0.022, None), (0.049, 0.049))), "b")  # both answered beats faster
        self.assertEqual(dns.fastest(results((0.022, None), (None, None))), "a")  # one answered beats none
        self.assertEqual(dns.fastest(results((0.004, 0.9), (0.049, 0.049))), "b")  # the slowest answer counts
        self.assertIsNone(dns.fastest(results((None, None), (None, None))))
        self.assertEqual(dns.describe(results((None, 0.0042), (0.022, 0.0491))), "a: in no answer, out 4 ms; b: in 22 ms, out 49 ms")
        self.assertEqual(dns.unanswered([("in", None), ("out", 0.1)]), ["in"])

    def test_hostname(self):
        self.assertEqual(dns.hostname("https://vpn.example.com:443/path"), "vpn.example.com")
        self.assertEqual(dns.hostname("vpn.example.com"), "vpn.example.com")
        self.assertEqual(dns.hostname("https://10.0.0.1/"), "example.com")
        self.assertEqual(dns.hostname("[2001:db8::1]:443"), "example.com")
        self.assertEqual(dns.names("vpn.example.com"), ["vpn.example.com", "example.com"])
        self.assertEqual(dns.names("10.0.0.1"), ["example.com"])

    def test_read_resolv_conf(self):
        self.resolv_conf("10.0.0.1", "10.0.0.2")
        self.assertEqual(dns.read_resolv_conf(), (RESOLV_CONF.format("10.0.0.1", "10.0.0.2"), ["10.0.0.1", "10.0.0.2"]))
        self.assertEqual(dns.read_resolv_conf(os.path.join(self.tmp, "none")), ("", []))

    def test_openconnect_puts_the_answering_server_first(self):
        # No word from vpnc-script: resolv.conf, which it wrote itself.
        self.resolv_conf("127.0.0.2", "127.0.0.1")
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        t.up(SESSION_DICT, lambda m: None)
        self.assertEqual(t.dns, [])
        self.assertEqual(t.dns_servers(), ["127.0.0.2", "127.0.0.1"])
        lines = []
        note = dns.mitigate(t, "https://vpn.example.com/", SESSION_DICT, lines.append)
        self.assertEqual(note, "Using DNS server 127.0.0.1 first; 127.0.0.2 no answer for vpn.example.com, example.com")
        self.assertEqual(read(dns.RESOLV_CONF), RESOLV_CONF.format("127.0.0.1", "127.0.0.2"))  # the mark and the rest kept
        self.assertRegex(" ".join(lines), r"127\.0\.0\.2: vpn\.example\.com no answer, example\.com no answer; 127\.0\.0\.1: vpn\.example\.com \d+ ms, example\.com \d+ ms")
        self.assertTrue(t.is_up())
        # Again: the first one answers now, so nothing to do.
        self.assertIsNone(dns.mitigate(t, "https://vpn.example.com/", SESSION_DICT, lines.append))
        time.sleep(0.5)
        t.down()
        self.assertFalse(t.is_up())

    def test_split_horizon_server_that_drops_outside_names_goes_second(self):
        # The first answers the company's names only, and silently drops the rest.
        self.resolv_conf("127.0.0.4", "127.0.0.1")
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        t.up(SESSION_DICT, lambda m: None)
        lines = []
        note = dns.mitigate(t, "https://vpn.example.com/", SESSION_DICT, lines.append)
        self.assertEqual(note, "Using DNS server 127.0.0.1 first; 127.0.0.4 no answer for example.com")
        self.assertEqual(read(dns.RESOLV_CONF), RESOLV_CONF.format("127.0.0.1", "127.0.0.4"))
        self.assertRegex(" ".join(lines), r"127\.0\.0\.4: vpn\.example\.com \d+ ms, example\.com no answer")
        time.sleep(0.5)
        t.down()

    def test_openconnect_servers_from_vpnc_script_in_someone_elses_resolv_conf(self):
        # resolvconf's, say: the VPN's servers among others, which stay where they are.
        os.environ["FAKE_OC_DNS"] = "127.0.0.2 127.0.0.1"
        self.resolv_conf("127.0.0.2", "127.0.0.1", text="# Dynamic resolv.conf(5) file for glibc resolver(3) generated by resolvconf(8)\nnameserver {}\nnameserver {}\nnameserver 192.0.2.53\nsearch corp.example.com\n")
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        t.up(SESSION_DICT, lambda m: None)
        self.assertEqual(t.dns_servers(), ["127.0.0.2", "127.0.0.1"])
        lines = []
        note = dns.mitigate(t, "https://vpn.example.com/", SESSION_DICT, lines.append)
        self.assertEqual(note, "Using DNS server 127.0.0.1 first; 127.0.0.2 no answer for vpn.example.com, example.com")
        self.assertEqual(
            read(dns.RESOLV_CONF),
            "# Dynamic resolv.conf(5) file for glibc resolver(3) generated by resolvconf(8)\nnameserver 127.0.0.1\nnameserver 127.0.0.2\nnameserver 192.0.2.53\nsearch corp.example.com\n",
        )
        self.assertIn("127.0.0.1 is now the first of the VPN's servers", lines[-1])
        time.sleep(0.5)
        t.down()

    def fake_tool(self, name, body):
        """A stand-in for resolvectl or resolvconf, first on PATH, logging what it is called with."""
        d = os.path.join(self.tmp, "tools")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            f.write(f"#!/bin/sh\necho \"$*\" >>'{self.tmp}/{name}.calls'\n{body}\n")
        os.chmod(os.path.join(d, name), 0o755)
        if not os.environ["PATH"].startswith(d):
            os.environ["PATH"] = f"{d}:{os.environ['PATH']}"

    def test_openconnect_servers_in_systemd_resolved(self):
        # vpnc-script gave them to resolved for the tunnel's link: the new order goes there too.
        os.environ["FAKE_OC_DNS"] = "127.0.0.2 127.0.0.1"
        self.resolv_conf("127.0.0.2", "127.0.0.1", text="# This is /run/systemd/resolve/resolv.conf managed by man:systemd-resolved(8).\nnameserver {}\nnameserver {}\n")
        self.fake_tool("resolvectl", '[ "$1 $2" = "dns tun9" ] && [ $# -eq 2 ] && echo "Link 9 (tun9): 127.0.0.2 127.0.0.1"; exit 0')
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        t.up(SESSION_DICT, lambda m: None)
        note = dns.mitigate(t, "vpn.example.com", SESSION_DICT, lambda m: None)
        self.assertEqual(note, "Using DNS server 127.0.0.1 first; 127.0.0.2 no answer for vpn.example.com, example.com")
        self.assertEqual(read(os.path.join(self.tmp, "resolvectl.calls")).splitlines(), ["dns tun9", "dns tun9 127.0.0.1 127.0.0.2"])
        self.assertEqual(dns.read_resolv_conf()[1], ["127.0.0.2", "127.0.0.1"])  # resolved's own file, left to it
        time.sleep(0.5)
        t.down()

    def test_openconnect_servers_in_resolvconf(self):
        # vpnc-script gave them to resolvconf as the tunnel's record: that record anew, search domain and all.
        os.environ["FAKE_OC_DNS"] = "127.0.0.2 127.0.0.1"
        os.environ["TERMITE_RUN_DIR"] = self.tmp
        os.makedirs(os.path.join(self.tmp, "resolvconf", "interface"))
        open(os.path.join(self.tmp, "resolvconf", "interface", "tun9"), "w").close()
        self.resolv_conf("127.0.0.2", "127.0.0.1", text="# Dynamic resolv.conf(5) file for glibc resolver(3) generated by resolvconf(8)\nnameserver {}\nnameserver {}\n")
        self.fake_tool("resolvconf", f"cat >'{self.tmp}/resolvconf.record'")
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        t.up(SESSION_DICT, lambda m: None)
        note = dns.mitigate(t, "vpn.example.com", SESSION_DICT, lambda m: None)
        self.assertEqual(note, "Using DNS server 127.0.0.1 first; 127.0.0.2 no answer for vpn.example.com, example.com")
        self.assertEqual(read(os.path.join(self.tmp, "resolvconf.calls")), "-a tun9\n")
        self.assertEqual(read(os.path.join(self.tmp, "resolvconf.record")), "nameserver 127.0.0.1\nnameserver 127.0.0.2\nsearch corp.example.com\n")
        self.assertEqual(dns.read_resolv_conf()[1], ["127.0.0.2", "127.0.0.1"])  # resolvconf's own file, left to it
        time.sleep(0.5)
        t.down()

    def test_openconnect_waits_for_vpnc_script(self):
        # openconnect --background returns before vpnc-script has run: the helper waits for it.
        os.environ["FAKE_OC_DNS"] = "127.0.0.2 127.0.0.1"
        os.environ["FAKE_OC_SCRIPT_DELAY"] = "1.5"
        self.resolv_conf("127.0.0.2", "127.0.0.1", text="nameserver {}\nnameserver {}\n")
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        start = time.monotonic()
        t.up(SESSION_DICT, lambda m: None)
        self.assertGreater(time.monotonic() - start, 1.0)
        self.assertEqual(t.dns_servers(), ["127.0.0.2", "127.0.0.1"])
        self.assertEqual(read(os.path.join(self.tmp, "script")), "connect tun9 127.0.0.2 127.0.0.1\n")
        time.sleep(0.5)
        t.down()

    def test_openconnect_servers_not_in_resolv_conf(self):
        os.environ["FAKE_OC_DNS"] = "127.0.0.2 127.0.0.1"
        self.resolv_conf("192.0.2.53", "192.0.2.54", text="nameserver {}\nnameserver {}\n")
        t = tunnel.OpenConnect("pkexec")
        t.pidfile = os.path.join(self.tmp, "oc.pid")
        t.up(SESSION_DICT, lambda m: None)
        lines = []
        self.assertIsNone(dns.mitigate(t, "vpn.example.com", SESSION_DICT, lines.append))
        self.assertIn("could not put 127.0.0.1 first", lines[-1])
        self.assertEqual(read(dns.RESOLV_CONF), "nameserver 192.0.2.53\nnameserver 192.0.2.54\n")
        time.sleep(0.5)
        t.down()

    def test_openconnect_leaves_other_resolv_confs_alone(self):
        self.resolv_conf("127.0.0.2", "127.0.0.1", text="# Generated by NetworkManager\nnameserver {}\nnameserver {}\n")
        t = tunnel.OpenConnect("pkexec")
        self.assertEqual(t.dns_servers(), [])
        lines = []
        self.assertIsNone(dns.mitigate(t, "vpn.example.com", SESSION_DICT, lines.append))
        self.assertIn("leaving DNS alone", lines[-1])
        self.assertEqual(self.questions, [])

    def test_networkmanager_pins_the_answering_server_and_reconnects(self):
        self.resolv_conf("127.0.0.2", "127.0.0.1", text="# Generated by NetworkManager\nnameserver {}\nnameserver {}\nnameserver 192.0.2.53\n")
        with open(os.path.join(self.tmp, "connections"), "w") as f:
            f.write("Termite\n")
        with open(os.path.join(self.tmp, "dns"), "w") as f:
            f.write("127.0.0.2 | 127.0.0.1\n\n")
        t = tunnel.NetworkManager("Termite")
        t.up(SESSION_DICT, lambda m: None)
        self.assertEqual(t.dns_servers(), ["127.0.0.2", "127.0.0.1"])
        lines = []
        note = dns.mitigate(t, "vpn.example.com", SESSION_DICT, lines.append)
        self.assertEqual(note, "Using DNS server 127.0.0.1 first; 127.0.0.2 no answer for vpn.example.com, example.com")
        calls = read(os.path.join(self.tmp, "calls")).splitlines()
        i = calls.index("connection modify Termite ipv4.dns 127.0.0.1")
        self.assertEqual(calls[i + 1], "connection down Termite")
        self.assertTrue(calls[i + 2].startswith("--wait 120 connection up Termite passwd-file"))
        self.assertTrue(t.is_up())
        self.assertIn("vpn.secrets.cookie:openconnect_strapkey=AAA; webvpn=S", read(os.path.join(self.tmp, "secrets")))
        # The setting back to the gateway's order: the server comes off the profile, once.
        self.assertEqual(read(os.path.join(self.tmp, "pin")).strip(), "127.0.0.1")
        t.forget_dns(lines.append)
        self.assertIn("connection modify Termite ipv4.dns  ipv6.dns ", read(os.path.join(self.tmp, "calls")))
        self.assertEqual(read(os.path.join(self.tmp, "pin")).strip(), "")
        self.assertIn("no DNS servers of its own again", lines[-1])
        n = read(os.path.join(self.tmp, "calls")).count("connection modify")
        t.forget_dns(lines.append)
        self.assertEqual(read(os.path.join(self.tmp, "calls")).count("connection modify"), n)

    def test_networkmanager_first_one_answering_is_left(self):
        self.resolv_conf("127.0.0.1", "127.0.0.2", text="nameserver {}\nnameserver {}\n")
        with open(os.path.join(self.tmp, "dns"), "w") as f:
            f.write("127.0.0.1 | 127.0.0.2\n")
        t = tunnel.NetworkManager("Termite")
        self.assertIsNone(dns.mitigate(t, "vpn.example.com", SESSION_DICT, lambda m: None))
        self.assertNotIn("modify", read(os.path.join(self.tmp, "calls")))

    def test_none_answering(self):
        self.resolv_conf("127.0.0.2", "127.0.0.3", text="nameserver {}\nnameserver {}\n")
        with open(os.path.join(self.tmp, "dns"), "w") as f:
            f.write("127.0.0.2 | 127.0.0.3\n")
        t = tunnel.NetworkManager("Termite")
        self.assertEqual(dns.mitigate(t, "vpn.example.com", SESSION_DICT, lambda m: None), "No DNS server of the VPN answers (127.0.0.2, 127.0.0.3)")
        self.assertNotIn("modify", read(os.path.join(self.tmp, "calls")))

    def test_local(self):
        self.assertTrue(self.saved_dns[3]("127.0.0.53"))
        self.assertTrue(self.saved_dns[3]("::1"))
        self.assertFalse(self.saved_dns[3]("10.91.191.45"))
        self.assertFalse(self.saved_dns[3]("fe80::1%tun0"))
        self.assertFalse(self.saved_dns[3]("nonsense"))

    def test_local_resolver_left_alone(self):
        self.resolv_conf("127.0.0.53", "::1", text="nameserver {}\nnameserver {}\n")
        with open(os.path.join(self.tmp, "dns"), "w") as f:
            f.write("127.0.0.2 | 127.0.0.1\n")
        lines = []
        self.assertIsNone(dns.mitigate(tunnel.NetworkManager("Termite"), "vpn.example.com", SESSION_DICT, lines.append))
        self.assertIn("resolver on this machine", lines[-1])
        self.assertEqual(self.questions, [])


class Elevator(Base):
    def setUp(self):
        super().setUp()
        if os.path.exists("/run/wrappers/bin/pkexec"):
            self.skipTest("NixOS: the real setuid wrapper would come first")

    def test_setuid_one_first(self):
        # NixOS: a plain pkexec on PATH, the setuid wrapper elsewhere.
        plain, wrapped = os.path.join(self.tmp, "plain"), os.path.join(self.tmp, "wrappers")
        for d, mode in ((plain, 0o755), (wrapped, 0o4755)):
            os.makedirs(d)
            with open(os.path.join(d, "pkexec"), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(os.path.join(d, "pkexec"), mode)
        if not os.stat(os.path.join(wrapped, "pkexec")).st_mode & 0o4000:
            self.skipTest("this filesystem drops the setuid bit")
        os.environ["PATH"] = f"{plain}:{wrapped}"
        self.assertEqual(self.elevator("pkexec"), os.path.join(wrapped, "pkexec"))

    def test_falls_back_to_path(self):
        # No setuid one: the first on PATH.
        os.environ["PATH"] = FAKES
        self.assertEqual(self.elevator("pkexec"), os.path.join(FAKES, "pkexec"))


class Integrate(Base):
    def setUp(self):
        super().setUp()
        self.appimage = os.path.join(self.tmp, "My Apps", "Termite-1.4.1-x86_64.AppImage")
        os.makedirs(os.path.dirname(self.appimage))
        open(self.appimage, "w").close()
        os.environ["APPIMAGE"] = self.appimage
        self.data = os.path.join(self.tmp, "data")

    def test_install(self):
        self.assertTrue(integrate.install("1.4.1"))
        desktop = read(os.path.join(self.data, "applications", "io.github.tahuffman1s.Termite.desktop"))
        self.assertIn(f'Exec="{self.appimage}"', desktop)
        self.assertIn("Icon=io.github.tahuffman1s.Termite\n", desktop)
        self.assertIn("StartupWMClass=io.github.tahuffman1s.Termite", desktop)
        for name in ("io.github.tahuffman1s.Termite", "io.github.tahuffman1s.Termite-disconnected"):
            self.assertTrue(os.path.exists(os.path.join(self.data, "icons", "hicolor", "scalable", "apps", f"{name}.svg")))
        self.assertFalse(integrate.install("1.4.1"))  # nothing to change
        integrate.remove()
        self.assertFalse(os.path.exists(os.path.join(self.data, "applications", "io.github.tahuffman1s.Termite.desktop")))

    def test_leaves_others_alone(self):
        path = os.path.join(self.data, "applications", "io.github.tahuffman1s.Termite.desktop")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("[Desktop Entry]\nName=Termite (packaged)\n")
        self.assertFalse(integrate.install("1.4.1"))
        integrate.remove()
        self.assertIn("packaged", read(path))

    def test_an_appimage_manager_was_first(self):
        apps = os.path.join(self.data, "applications")
        os.makedirs(apps)
        with open(os.path.join(apps, "gearlever_termite_8a1b.desktop"), "w") as f:
            f.write(f"[Desktop Entry]\nName=Termite\nExec={self.appimage}\nStartupWMClass=io.github.tahuffman1s.Termite\n")
        self.assertFalse(integrate.install("1.4.1"))
        self.assertEqual(os.listdir(apps), ["gearlever_termite_8a1b.desktop"])

    def test_an_older_appimage_managers_entry_gets_matched(self):
        apps = os.path.join(self.data, "applications")
        os.makedirs(apps)
        entry = os.path.join(apps, "gearlever_termite_8a1b.desktop")
        with open(entry, "w") as f:
            f.write(f"[Desktop Entry]\nName=Termite\nExec={self.appimage}\n\n[Desktop Action x]\nName=X\n")
        self.assertTrue(integrate.install("1.4.1"))
        self.assertEqual(read(entry), f"[Desktop Entry]\nName=Termite\nExec={self.appimage}\nStartupWMClass=io.github.tahuffman1s.Termite\n\n[Desktop Action x]\nName=X\n")
        self.assertFalse(integrate.install("1.4.1"))
        self.assertEqual(os.listdir(apps), ["gearlever_termite_8a1b.desktop"])

    def test_not_an_appimage(self):
        del os.environ["APPIMAGE"]
        self.assertFalse(integrate.install("1.4.1"))

    def test_quote(self):
        self.assertEqual(integrate.quote('/a b/$x"y%'), '"/a b/\\$x\\"y%%"')


class Config(Base):
    def test_roundtrip(self):
        cfg = config.load()
        self.assertEqual(cfg["backend"], "auto")
        self.assertEqual(cfg["dns"], "fastest")
        cfg.update(gateway="vpn.example.com", extra="ignored")
        config.save(cfg)
        self.assertEqual(config.load()["gateway"], "vpn.example.com")
        self.assertNotIn("extra", json.loads(read(config.path())))
        os.environ["TERMITE_GATEWAY"] = "other.example.com"
        self.assertEqual(config.load()["gateway"], "other.example.com")


if __name__ == "__main__":
    unittest.main()
