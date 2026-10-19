"""ungoogled-chromium for the sign-in window, for systems without a
Chromium-based browser: downloaded on request from the portable Linux
builds (github.com/ungoogled-software/ungoogled-chromium-portablelinux)
into ~/.local/share/termite/browser/ungoogled-chromium, and used only by
Termite. ungoogled-chromium doesn't talk to the Chrome Web Store, so the
first "Add extensions" in its profile also offers the Chromium Web Store
extension (github.com/NeverDecaf/chromium-web-store), which adds the
store's install buttons and keeps extensions updated."""

import http.server
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.request

from . import __version__, sso

REPO = "ungoogled-software/ungoogled-chromium-portablelinux"
API = os.environ.get("TERMITE_UC_API", f"https://api.github.com/repos/{REPO}/releases/latest")
LATEST = os.environ.get("TERMITE_UC_LATEST", f"https://github.com/{REPO}/releases/latest")
DOWNLOAD = os.environ.get("TERMITE_UC_DOWNLOAD", f"https://github.com/{REPO}/releases/download")

WEB_STORE_HELPER_ID = "ocaahdebbfolfmndjeplogmgcagdmblk"
WEB_STORE_HELPER = os.environ.get(
    "TERMITE_WEB_STORE_HELPER", "https://github.com/NeverDecaf/chromium-web-store/releases/latest/download/Chromium.Web.Store.crx"
)

SIZE_HINT = "a 160 MB download, about 550 MB on disk"


class DownloadError(Exception):
    pass


def unsupported():
    """Why the download can't be used here, or None."""
    if platform.machine() not in ("x86_64", "AMD64"):
        return "ungoogled-chromium's portable builds are for x86_64 only"
    if os.path.exists("/etc/NIXOS"):
        return "On NixOS, install ungoogled-chromium with Nix (pkgs.ungoogled-chromium); downloaded builds can't find their libraries there"
    return None


def installed_version():
    try:
        with open(os.path.join(sso.downloaded_dir(), "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return None


def _open(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": f"Termite/{__version__}"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest():
    """The latest release, as (version, download URL)."""
    tag = None
    try:
        with _open(API) as r:
            data = json.load(r)
        tag = data["tag_name"]
        for asset in data.get("assets", []):
            if asset["name"].endswith("x86_64_linux.tar.xz"):
                return tag, asset["browser_download_url"]
    except (OSError, ValueError, KeyError):
        # The API allows few requests without signing in; the releases
        # page redirects to the latest tag too.
        try:
            with _open(LATEST) as r:
                m = re.search(r"/releases/tag/([^/?#]+)", r.geturl())
            tag = m.group(1) if m else None
        except OSError as e:
            raise DownloadError(f"could not find the latest ungoogled-chromium: {e}") from e
    if not tag:
        raise DownloadError("could not find the latest ungoogled-chromium")
    return tag, f"{DOWNLOAD}/{tag}/ungoogled-chromium-{tag}-x86_64_linux.tar.xz"


def install(progress=None, log=None, cancel=None):
    """Download and unpack the latest ungoogled-chromium; returns its
    version. progress(fraction or None) during the download."""
    log = log or (lambda msg: None)
    reason = unsupported()
    if reason:
        raise DownloadError(reason)
    version, url = latest()
    log(f"downloading ungoogled-chromium {version}")
    base = os.path.dirname(sso.downloaded_dir())
    os.makedirs(base, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".download-", dir=base)
    try:
        archive = os.path.join(work, "chromium.tar.xz")
        try:
            with _open(url, timeout=60) as r, open(archive, "wb") as out:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadError("cancelled")
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done / total if total else None)
        except OSError as e:
            raise DownloadError(f"the download failed: {e}") from e
        log("unpacking")
        if progress:
            progress(None)
        unpacked = os.path.join(work, "unpacked")
        try:
            with tarfile.open(archive, "r:xz") as tar:
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(unpacked, filter="data")
                else:
                    for m in tar.getmembers():
                        if m.name.startswith(("/", "..")) or "/../" in m.name or m.issym() and m.linkname.startswith("/"):
                            raise DownloadError(f"unexpected path in the archive: {m.name}")
                    tar.extractall(unpacked)
        except (OSError, tarfile.TarError) as e:
            raise DownloadError(f"could not unpack the download: {e}") from e
        tops = os.listdir(unpacked)
        top = os.path.join(unpacked, tops[0]) if len(tops) == 1 else unpacked
        chrome = os.path.join(top, "chrome")
        if not os.access(chrome, os.X_OK):
            raise DownloadError("the download has no chrome program in it")
        prune(top)
        with open(os.path.join(top, "VERSION"), "w") as f:
            f.write(version + "\n")
        # Swap it in whole, so a failed update leaves the old one working.
        target = sso.downloaded_dir()
        old = os.path.join(work, "old")
        if os.path.exists(target):
            os.rename(target, old)
        os.rename(top, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"ungoogled-chromium {version} is ready")
    return version


def wanted_languages():
    """Language codes of the desktop's locale (de, pt, ...), and English."""
    langs = {"en"}
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for part in os.environ.get(var, "").split(":"):
            code = re.split(r"[_.@-]", part)[0].lower()
            if code and code not in ("c", "posix"):
                langs.add(code)
    return langs


def prune(top):
    """Leave out what the sign-in doesn't need: the WebDriver tool and the
    translations other than English and the desktop's language (177 MB of
    the download; Chromium falls back to English)."""
    try:
        os.remove(os.path.join(top, "chromedriver"))
    except OSError:
        pass
    locales = os.path.join(top, "locales")
    langs = wanted_languages()
    for name in os.listdir(locales) if os.path.isdir(locales) else []:
        if name.endswith(".info") or re.split(r"[-_.]", name)[0].lower() not in langs:
            os.remove(os.path.join(locales, name))


def remove():
    shutil.rmtree(sso.downloaded_dir(), ignore_errors=True)


def web_store_helper_installed(profile):
    return os.path.isdir(os.path.join(profile, "Default", "Extensions", WEB_STORE_HELPER_ID))


def serve_web_store_helper(minutes=10):
    """The Chromium Web Store extension, from a local address that serves it
    as an extension (GitHub serves it as a plain file, which the browser
    would only save), so the browser offers to install it. Returns its URL;
    the server stops after a while."""
    try:
        with _open(WEB_STORE_HELPER, timeout=60) as r:
            crx = r.read()
    except OSError as e:
        raise DownloadError(f"could not download the Chromium Web Store extension: {e}") from e

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-chrome-extension")
            self.send_header("Content-Length", str(len(crx)))
            self.end_headers()
            self.wfile.write(crx)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Timer(minutes * 60, server.shutdown)
    stop.daemon = True
    stop.start()
    return f"http://127.0.0.1:{server.server_address[1]}/Chromium.Web.Store.crx"
