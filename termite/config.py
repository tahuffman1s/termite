"""Settings, in $XDG_CONFIG_HOME/termite/config.json. Nothing secret is
kept: the VPN session lives only as long as the connection, and the
Microsoft sign-in stays in the browser profile."""

import json
import os

DEFAULTS = {
    "gateway": "",  # e.g. vpn.example.com, as in Cisco Secure Client
    "group": "",  # the connection profile (group); blank for the default
    "browser": "",  # path of a Chromium-based browser; blank to pick one
    "backend": "auto",  # auto | networkmanager | openconnect
    "connection": "Termite",  # NetworkManager connection name
    "dns": "fastest",  # fastest: after connecting, the VPN's DNS server that answers best first | gateway: as given
    "split_dns": "yes",  # yes: the VPN's DNS servers only for the company's names, where a resolver can route by domain | no: for every name
    "domains": "",  # company domains besides the ones the gateway names (space or comma separated)
    "menu": "yes",  # the AppImage's icon and menu entry (integrate): yes | no
}


def path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "termite", "config.json")


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(path()) as f:
            data = json.load(f)
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS and isinstance(v, str)})
    except (OSError, ValueError):
        pass
    # Overrides for launchers that set the gateway themselves.
    for key, var in (("gateway", "TERMITE_GATEWAY"), ("group", "TERMITE_GROUP")):
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    return cfg


def save(cfg):
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: cfg.get(k, v) for k, v in DEFAULTS.items()}, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)
