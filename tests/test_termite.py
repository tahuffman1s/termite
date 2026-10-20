"""Tests against a fake AnyConnect gateway and fake browser, nmcli, pkexec
and openconnect (tests/fakes). Run: python3 -m unittest discover tests"""

import base64
import http.server
import json
import os
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

from termite import config, sso, tunnel  # noqa: E402

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
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "XDG_DATA_HOME": os.path.join(self.tmp, "data"),
        }
        self.saved = dict(os.environ)
        os.environ.update(self.env)
        # The fake pkexec, even where a real setuid one is installed.
        self.elevator = tunnel.elevator
        tunnel.elevator = lambda name: os.path.join(FAKES, name)
        for var in ("TERMITE_CHROMIUM", "TERMITE_CHROMIUM_LIBS", "TERMITE_BROWSER", "TERMITE_PROFILE", "TERMITE_GATEWAY", "TERMITE_GROUP", "FAKE_BROWSER", "FAKE_OC_FAIL"):
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
                f.write(read(os.path.join(FAKES, "openconnect")))
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
        t.ensure("vpn.example.com", lambda m: None)
        t.up(SESSION_DICT, lambda m: None)
        self.assertTrue(t.is_up())
        args = read(os.path.join(self.tmp, "args"))
        self.assertIn("--script=" + self.run_dir, args)
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


class Config(Base):
    def test_roundtrip(self):
        cfg = config.load()
        self.assertEqual(cfg["backend"], "auto")
        cfg.update(gateway="vpn.example.com", extra="ignored")
        config.save(cfg)
        self.assertEqual(config.load()["gateway"], "vpn.example.com")
        self.assertNotIn("extra", json.loads(read(config.path())))
        os.environ["TERMITE_GATEWAY"] = "other.example.com"
        self.assertEqual(config.load()["gateway"], "other.example.com")


if __name__ == "__main__":
    unittest.main()
