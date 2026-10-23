"""termite: the window with no arguments, or the same from a terminal."""

import argparse
import json
import sys
import time

from . import __version__, config, dns, sso, tunnel


def log(msg):
    print(f"termite: {msg}", file=sys.stderr, flush=True)


def settings(args):
    cfg = config.load()
    for key in ("gateway", "group", "browser", "backend", "connection", "dns", "split_dns", "domains"):
        value = getattr(args, key, None)
        if value:
            cfg[key] = value
    return cfg


def cmd_connect(args):
    cfg = settings(args)
    if not cfg["gateway"]:
        raise SystemExit("termite: no gateway; pass --gateway vpn.example.com (or set it in the window)")
    elevate = "sudo" if sys.stdin.isatty() else "pkexec"
    try:
        t = tunnel.choose(cfg, elevate)
        if t.is_up():
            log(f"already connected ({t.label})")
            return
        t.ensure(cfg["gateway"], log)
        t.prepare_dns(cfg, log)
        if cfg["dns"] == "gateway":
            t.forget_dns(log)
        session = sso.sign_in(cfg["gateway"], cfg["group"], cfg["browser"] or None, log=log)
        t.up(session, log)
        note = dns.split(t, cfg, log)
        if note:
            log(note)
        if cfg["dns"] != "gateway":
            note = dns.mitigate(t, cfg["gateway"], session, log)
            if note:
                log(note)
    except (sso.SignInError, tunnel.TunnelError) as e:
        raise SystemExit(f"termite: {e}")
    if t.persistent:
        log(f"connected ({t.label})")
        return
    log("connected; Ctrl+C disconnects")
    try:
        while t.is_up():
            time.sleep(2)
        log("the tunnel ended")
    except KeyboardInterrupt:
        log("disconnecting")
    finally:
        t.down()


def cmd_disconnect(args):
    cfg = settings(args)
    t = tunnel.NetworkManager(cfg["connection"])
    if not t.is_up():
        log("not connected")
        return
    try:
        t.down()
    except tunnel.TunnelError as e:
        raise SystemExit(f"termite: {e}")
    log("disconnected")


def cmd_status(args):
    cfg = settings(args)
    up = tunnel.NetworkManager.available() and tunnel.NetworkManager(cfg["connection"]).is_up()
    print("connected" if up else "disconnected")
    sys.exit(0 if up else 1)


def cmd_sign_in(args):
    sso.main([args.gateway] + ([args.group] if args.group else []))


def cmd_browser(args):
    cfg = settings(args)
    browser = cfg["browser"] or sso.default_browser()
    if not browser:
        raise SystemExit("termite: no Chromium-based browser found")
    sso.open_profile(browser, sso.default_profile(browser), args.page)


def cmd_info(args):
    cfg = settings(args)
    browsers = sso.find_browsers()
    info = {
        "version": __version__,
        "config": config.path(),
        "settings": cfg,
        "browsers": [{"name": n, "path": p, "profile": sso.default_profile(p)} for n, p in browsers],
        "networkmanager": tunnel.NetworkManager.available(),
        "networkmanager_openconnect_plugin": tunnel.NetworkManager.plugin_installed(),
        "openconnect": tunnel.system_openconnect() or tunnel.bundled_openconnect(),
        "vpnc_script": tunnel.OpenConnect().script or ("bundled" if tunnel.bundled_openconnect() else ""),
        "root_through": (tunnel.OpenConnect().elevation() or ["nothing usable"])[0],
        "no_new_privs": tunnel.no_new_privs(),
        "bundled_chromium": sso.bundled_browser(),
    }
    try:
        info["method"] = tunnel.choose(cfg).label
    except tunnel.TunnelError as e:
        info["method"] = f"none: {e}"
    print(json.dumps(info, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="termite", description="Cisco Secure Client VPN with Microsoft sign-in in a Chromium-based browser.")
    p.add_argument("--version", action="version", version=f"termite {__version__}")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("--gateway", help="VPN address, e.g. vpn.example.com")
        sp.add_argument("--group", help="connection profile (group)")
        sp.add_argument("--browser", help="path of a Chromium-based browser")
        sp.add_argument("--backend", choices=["auto", "networkmanager", "openconnect"])
        sp.add_argument("--connection", help="NetworkManager connection name")
        sp.add_argument("--dns", choices=["fastest", "gateway"], help="after connecting, put the VPN's DNS server that answers best first (the default), or leave the gateway's order")
        sp.add_argument("--split-dns", choices=["yes", "no"], help="keep the VPN's DNS servers to the company's names (the default, where a resolver can route by domain), or give them every name")
        sp.add_argument("--domains", help="company domains besides the ones the VPN names, space or comma separated")
        return sp

    sub.add_parser("gui", help="open the window (the default)")
    common(sub.add_parser("connect", help="sign in and connect"))
    common(sub.add_parser("disconnect", help="disconnect the NetworkManager connection"))
    common(sub.add_parser("status", help="connected or not (NetworkManager)"))
    s = sub.add_parser("sign-in", help="only sign in; print the session as JSON")
    s.add_argument("gateway")
    s.add_argument("group", nargs="?")
    b = common(sub.add_parser("browser", help="open the sign-in browser profile, to add extensions or change settings"))
    b.add_argument("page", nargs="?", default="extensions", help="extensions, store, settings or a URL")
    common(sub.add_parser("info", help="what was found on this system"))

    args, rest = p.parse_known_args(argv)
    if args.command in (None, "gui"):
        from . import gui

        sys.exit(gui.main([sys.argv[0], *rest]))
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    {
        "connect": cmd_connect,
        "disconnect": cmd_disconnect,
        "status": cmd_status,
        "sign-in": cmd_sign_in,
        "browser": cmd_browser,
        "info": cmd_info,
    }[args.command](args)
