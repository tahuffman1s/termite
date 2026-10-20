"""Desktop integration for the AppImage: Termite's icon and a menu entry for
this AppImage, in the user's own directories. Desktops find a window's icon
through the .desktop file named after its app ID (Plasma and GNOME on
Wayland), so without it the window has a generic icon.

Files written here say so (X-Termite-Integrated). When something else has
put Termite in the menu already (a package, an AppImage manager such as
Gear Lever or AppImageLauncher), nothing is added; that entry gets the
StartupWMClass line that matches it to the window, if it lacks one (as
entries made from Termite before 1.4.1 do)."""

import os

from . import APP_ID

MARK = "X-Termite-Integrated=true"
ICON_NAMES = (APP_ID, f"{APP_ID}-disconnected")
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "icons", "hicolor", "scalable", "apps")


def appimage():
    """The AppImage this runs from, if it does."""
    path = os.environ.get("APPIMAGE")
    return path if path and os.path.isfile(path) else None


def data_home():
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def desktop_file():
    return os.path.join(data_home(), "applications", f"{APP_ID}.desktop")


def icon_file(name):
    return os.path.join(data_home(), "icons", "hicolor", "scalable", "apps", f"{name}.svg")


def quote(path):
    """A path as a quoted Exec argument (the Desktop Entry spec's rules)."""
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in path)
    return f'"{escaped}"'.replace("%", "%%")


def entry(path, version):
    return f"""[Desktop Entry]
Type=Application
Name=Termite
GenericName=VPN Client
Comment=Cisco Secure Client (AnyConnect) VPN with Microsoft sign-in
Exec={quote(path)}
TryExec={path}
Icon={APP_ID}
Terminal=false
Categories=Network;GTK;
Keywords=vpn;anyconnect;cisco;secure client;openconnect;
StartupNotify=true
StartupWMClass={APP_ID}
X-AppImage-Version={version}
{MARK}
"""


def ours():
    """Whether Termite may manage its menu entry: there is none, or its own,
    and no other entry starts this AppImage."""
    try:
        with open(desktop_file()) as f:
            if MARK not in f.read():
                return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return elsewhere() is None


def elsewhere():
    """Another menu entry (an AppImage manager's) that starts this
    AppImage, if there is one."""
    path = appimage()
    if not path:
        return None
    apps = os.path.join(data_home(), "applications")
    try:
        names = os.listdir(apps)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".desktop") or name == f"{APP_ID}.desktop":
            continue
        try:
            with open(os.path.join(apps, name), errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if MARK not in text and (path in text or os.path.realpath(path) in text):
            return name
    return None


def match_window(path):
    """Adds StartupWMClass to another menu entry for this AppImage, so the
    desktop matches the window to it; True if it was missing."""
    with open(path, errors="replace") as f:
        lines = f.read().splitlines(keepends=True)
    in_main = False
    for i, line in enumerate(lines):
        if line.startswith("["):
            if in_main:  # the end of [Desktop Entry]: add it here
                break
            in_main = line.strip() == "[Desktop Entry]"
        elif in_main and line.startswith("StartupWMClass="):
            return False
    else:
        i = len(lines)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    while i > 0 and not lines[i - 1].strip():
        i -= 1
    lines.insert(i, f"StartupWMClass={APP_ID}\n")
    write(path, "".join(lines).encode())
    return True


def write(path, data):
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def install(version):
    """The icon and menu entry for this AppImage; True if anything changed.
    Nothing when not run from an AppImage, or when the entry isn't ours."""
    path = appimage()
    if not path:
        return False
    other = elsewhere()
    if other:
        return match_window(os.path.join(data_home(), "applications", other))
    if not ours():
        return False
    changed = False
    for name in ICON_NAMES:
        with open(os.path.join(ICONS, f"{name}.svg"), "rb") as f:
            changed |= write(icon_file(name), f.read())
    changed |= write(desktop_file(), entry(path, version).encode())
    if changed:
        # Desktops notice a new icon when the theme directory changes.
        try:
            os.utime(os.path.join(data_home(), "icons", "hicolor"))
        except OSError:
            pass
    return changed


def remove():
    """Takes Termite's own menu entry and icons away again."""
    if not ours():
        return
    for path in (desktop_file(), *map(icon_file, ICON_NAMES)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
