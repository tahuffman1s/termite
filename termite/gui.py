"""The window: GTK 4 and libadwaita (1.6 or newer; the AppImage has 1.9),
laid out the GNOME way: the connection on a status page, settings in a
preferences dialog, the rest in the primary menu."""

import json
import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import APP_ID, __version__, config, dns, integrate, sso, tray, tunnel  # noqa: E402

if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6):
    raise SystemExit(f"termite: needs libadwaita 1.6 or newer, not {Adw.get_major_version()}.{Adw.get_minor_version()} (the AppImage has it)")

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ICONS = os.path.join(DATA, "icons")
LOGO = os.path.join(ICONS, "hicolor", "scalable", "apps", f"{APP_ID}.svg")
ICON_CONNECTED = APP_ID
ICON_DISCONNECTED = f"{APP_ID}-disconnected"
REPO = "https://github.com/tahuffman1s/termite"

METHODS = [
    ("auto", "Automatic"),
    ("networkmanager", "NetworkManager"),
    ("openconnect", "OpenConnect"),
]

SHORTCUTS = [
    ("General", [
        ("Preferences", "<Control>comma"),
        ("Connection Log", "<Control>l"),
        ("Keyboard Shortcuts", "<Control>question"),
        ("Close Window", "<Control>w"),
        ("Quit", "<Control>q"),
    ]),
    ("Connection", [("Connect or Disconnect", "<Control>Return")]),
]  # fmt: skip


def background(fn, done):
    """fn() in a thread; done(result, error) back on the main loop."""

    def work():
        try:
            result, error = fn(), None
        except Exception as e:  # noqa: BLE001 - shown to the user
            result, error = None, e
        GLib.idle_add(lambda: done(result, error) and False)

    threading.Thread(target=work, daemon=True).start()


def sentence(msg):
    """An error message as a sentence, for a dialog's body."""
    msg = msg.strip()
    if not msg:
        return msg
    return msg[0].upper() + msg[1:] + ("" if msg[-1] in ".!?)" else ".")


def home(path):
    return path.replace(os.path.expanduser("~"), "~", 1)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Termite", default_width=420, default_height=600)
        self.set_size_request(360, 480)
        self.cfg = config.load()
        self.tunnel = None  # the one that is up, or connecting
        self.up = False
        self.busy = False
        self.cancel = None
        self.setup_proc = None
        self.method_label = ""  # what Automatic uses here
        self.method_error = ""  # or why there's nothing to use
        self.log_buffer = Gtk.TextBuffer()

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Connection _Log", "app.log")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("_Preferences", "app.preferences")
        section.append("_Keyboard Shortcuts", "app.shortcuts")
        section.append("_About Termite", "app.about")
        menu.append_section(None, section)
        header = Adw.HeaderBar()
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu", primary=True))

        self.status = Adw.StatusPage(icon_name=ICON_DISCONNECTED, title="Not Connected", vexpand=True)
        self.status.set_child(self.build_controls())
        self.toasts = Adw.ToastOverlay(child=self.status)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_content(view)

        # Narrow windows: the smaller status page layout.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 400sp"))
        narrow.add_setter(self.status, "css-classes", GObject.Value(GObject.type_from_name("GStrv"), ["compact"]))
        self.add_breakpoint(narrow)

        toggle = Gio.SimpleAction.new("toggle-connection", None)
        toggle.connect("activate", lambda *_: self.on_connect_clicked())
        self.add_action(toggle)

        # Enter connects; with an address filled in, the button has focus.
        self.set_default_widget(self.connect_button)
        self.connect("map", lambda *_: self.cfg["gateway"] and GLib.idle_add(lambda: self.connect_button.grab_focus() and False))

        self.connect("close-request", self.on_close)
        self.refresh_state(False)
        self.poll()
        GLib.timeout_add_seconds(3, self.poll)

    def build_controls(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        group = Adw.PreferencesGroup()
        self.gateway = Adw.EntryRow(title="VPN Address", text=self.cfg["gateway"], input_purpose=Gtk.InputPurpose.URL)
        self.gateway.connect("entry-activated", lambda *_: self.connect_vpn())
        self.gateway.connect("changed", lambda *_: self.save_gateway())
        group.add(self.gateway)
        box.append(group)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.connect_button = Gtk.Button(label="_Connect", use_underline=True, width_request=160)
        self.connect_button.add_css_class("pill")
        self.connect_button.connect("clicked", lambda *_: self.on_connect_clicked())
        self.spinner = Adw.Spinner(width_request=32, height_request=32, visible=False)
        self.cancel_button = Gtk.Button(label="C_ancel", use_underline=True, visible=False)
        self.cancel_button.add_css_class("pill")
        self.cancel_button.connect("clicked", self.on_cancel)
        for w in (self.connect_button, self.spinner, self.cancel_button):
            buttons.append(w)
        box.append(buttons)
        return Adw.Clamp(maximum_size=400, child=box)

    # -- state -----------------------------------------------------------

    def log(self, msg):
        def add():
            self.log_buffer.insert(self.log_buffer.get_end_iter(), msg.rstrip() + "\n")
            return False

        GLib.idle_add(add)

    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg))
        if not self.get_visible():
            self.notify(msg)

    def notify(self, msg):
        """A desktop notification (for when the window is in the tray)."""
        note = Gio.Notification.new("Termite")
        note.set_body(msg)
        self.get_application().send_notification(None, note)

    def alert(self, heading, body):
        """An error that needs reading: a dialog, with the log a click away."""
        if not self.get_visible():
            self.notify(f"{heading}: {body}")
            return
        dialog = Adw.AlertDialog(heading=heading, body=sentence(body))
        dialog.add_response("log", "Show _Log")
        dialog.add_response("close", "_Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.connect("response", lambda _, r: r == "log" and self.show_log())
        dialog.present(self)

    def sync_tray(self):
        app = self.get_application()
        if app is None or getattr(app, "tray", None) is None:
            return
        if self.busy:
            tip = self.status.get_title()
        elif self.up:
            tip = f"Connected to {self.cfg['gateway']}" if self.cfg["gateway"] else "Connected"
        else:
            tip = "Not connected"
        app.tray.update(self.up, self.busy, tip)

    def save_gateway(self):
        self.cfg["gateway"] = self.gateway.get_text().strip()
        self.save()

    def save(self):
        try:
            config.save(self.cfg)
        except OSError as e:
            self.log(f"could not save the settings: {e}")

    def browser(self):
        """The browser the settings name, or the first one found."""
        if self.cfg["browser"] and os.access(self.cfg["browser"], os.X_OK):
            return self.cfg["browser"]
        found = sso.find_browsers()
        return found[0][1] if found else None

    def refresh_state(self, up):
        self.up = up
        if self.busy:
            return
        self.status.set_icon_name(ICON_CONNECTED if up else ICON_DISCONNECTED)
        self.status.set_title("Connected" if up else "Not Connected")
        self.connect_button.set_label("_Disconnect" if up else "_Connect")
        self.connect_button.remove_css_class("destructive-action" if up else "suggested-action")
        self.connect_button.add_css_class("suggested-action" if not up else "destructive-action")
        self.gateway.set_sensitive(not up)
        self.sync_tray()

    def set_busy(self, busy, title=""):
        self.busy = busy
        self.spinner.set_visible(busy)
        self.connect_button.set_visible(not busy)
        self.cancel_button.set_visible(busy and self.cancel is not None)
        self.gateway.set_sensitive(not busy and not self.up)
        if busy:
            self.status.set_title(title)
            self.sync_tray()

    def poll(self):
        if self.busy:
            return True
        if self.tunnel is not None and not self.tunnel.persistent:
            was_up = self.up
            up = self.tunnel.is_up()
            if was_up and not up:
                self.tunnel.down()
                self.tunnel = None
                self.refresh_state(False)
                self.toast("The tunnel ended")
                self.log("the tunnel ended (openconnect stopped)")
            return True
        cfg = dict(self.cfg)

        def check():
            t = tunnel.choose(cfg)
            return t, t.is_up()

        def done(result, error):
            if self.busy or (self.tunnel is not None and not self.tunnel.persistent):
                return
            if error is not None:
                self.method_label, self.method_error = "", str(error)
                return
            t, up = result
            self.method_label, self.method_error = t.label, ""
            if up:
                self.tunnel = t
            elif self.tunnel is not None:
                self.tunnel = None
            if up != self.up:
                self.refresh_state(up)

        background(check, done)
        return True

    # -- actions ---------------------------------------------------------

    def setup_browser_open(self):
        return self.setup_proc is not None and self.setup_proc.poll() is None

    def open_profile(self, page):
        browser = self.browser()
        if not browser:
            return
        try:
            self.setup_proc = sso.open_profile(browser, sso.default_profile(browser), page)
        except OSError as e:
            self.alert("Could Not Start the Browser", str(e))

    def on_connect_clicked(self):
        if self.busy:
            return
        if self.up:
            self.disconnect_vpn()
        else:
            self.connect_vpn()

    def connect_vpn(self):
        if self.busy or self.up:
            return
        self.save_gateway()
        cfg = dict(self.cfg)
        if not cfg["gateway"]:
            self.present()
            self.gateway.grab_focus()
            self.toast("Enter the VPN address first")
            return
        if not self.browser():
            self.alert("No Browser for the Sign-In", "Install Chromium, Google Chrome, Brave, Microsoft Edge or Vivaldi.")
            return
        if self.setup_browser_open():
            self.toast("Close the browser window first")
            return
        self.cancel = threading.Event()
        self.set_busy(True, "Signing In…")
        self.log(f"--- connecting to {cfg['gateway']}")
        cancel = self.cancel
        notes = []  # what the DNS check has to say

        def work():
            t = tunnel.choose(cfg)
            self.log(f"connecting with {t.label}")
            t.ensure(cfg["gateway"], self.log)
            if cfg["dns"] == "gateway":
                t.forget_dns(self.log)
            session = sso.sign_in(cfg["gateway"], cfg["group"], cfg["browser"] or None, log=self.log, cancel=cancel)
            if cancel.is_set():
                raise sso.Cancelled()
            GLib.idle_add(lambda: self.connecting() and False)
            t.up(session, self.log)
            if cfg["dns"] != "gateway":
                note = dns.mitigate(t, cfg["gateway"], session, self.log)
                if note:
                    notes.append(note)
            return t

        def done(t, error):
            self.cancel = None
            self.set_busy(False)
            if error is None:
                self.tunnel = t
                self.log("connected")
                self.refresh_state(True)
                if not self.get_visible():
                    self.notify(f"Connected to {cfg['gateway']}")
                for note in notes:
                    self.log(note)
                    self.toast(note)
                return
            self.refresh_state(False)
            if isinstance(error, sso.Cancelled):
                self.log("cancelled")
                return
            self.log(f"error: {error}")
            self.alert("Could Not Connect", str(error))

        background(work, done)

    def connecting(self):
        # Past the sign-in: the tunnel can't be cancelled half-way.
        self.cancel = None
        self.cancel_button.set_visible(False)
        self.status.set_title("Connecting…")
        self.sync_tray()

    def on_cancel(self, *_):
        if self.cancel is not None:
            self.cancel.set()
            self.cancel_button.set_sensitive(False)
            GLib.timeout_add(1500, lambda: self.cancel_button.set_sensitive(True) and False)

    def disconnect_vpn(self):
        if self.busy:
            return
        try:
            t = self.tunnel or tunnel.choose(self.cfg)
        except tunnel.TunnelError as e:
            self.alert("Could Not Disconnect", str(e))
            return
        self.cancel = None
        self.set_busy(True, "Disconnecting…")

        def done(_, error):
            self.set_busy(False)
            if error is not None:
                self.log(f"error: {error}")
                self.refresh_state(t.is_up())
                self.alert("Could Not Disconnect", str(error))
                return
            self.tunnel = None
            self.log("disconnected")
            self.refresh_state(False)

        background(t.down, done)

    def show_log(self):
        LogDialog(self.log_buffer).present(self)

    def on_close(self, *_):
        app = self.get_application()
        if getattr(app, "tray", None) is not None and app.tray.available:
            # To the tray: Termite keeps running (and the tunnel with it).
            self.set_visible(False)
            return True
        if self.tunnel is not None and not self.tunnel.persistent and self.tunnel.is_up():
            # The openconnect tunnel lives in this process: keep it, hide the
            # window; opening Termite again brings it back.
            self.set_visible(False)
            self.notify("Termite is still connected. Open it again to disconnect.")
            return True
        return False


class LogDialog(Adw.Dialog):
    """What happened while connecting."""

    def __init__(self, buffer):
        super().__init__(title="Connection Log", content_width=560, content_height=420)
        self.buffer = buffer
        header = Adw.HeaderBar()
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy Log")
        copy.connect("clicked", self.on_copy)
        header.pack_start(copy)
        text = Gtk.TextView(buffer=buffer, editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        text.add_css_class("inline")
        for side in ("top", "bottom", "left", "right"):
            getattr(text, f"set_{side}_margin")(12)
        self.text = text
        self.stack = Gtk.Stack()
        self.stack.add_named(Adw.StatusPage(icon_name="text-x-generic-symbolic", title="Nothing Yet", description="Connect, and what happens shows here"), "empty")
        self.stack.add_named(Gtk.ScrolledWindow(child=text, vexpand=True), "log")
        self.changed = buffer.connect("changed", lambda *_: self.update())
        self.connect("closed", lambda *_: buffer.disconnect(self.changed))
        self.update()
        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_child(view)

    def update(self):
        empty = self.buffer.get_char_count() == 0
        self.stack.set_visible_child_name("empty" if empty else "log")
        if not empty:
            GLib.idle_add(lambda: self.text.scroll_to_iter(self.buffer.get_end_iter(), 0, False, 0, 0) and False)

    def on_copy(self, *_):
        start, end = self.buffer.get_bounds()
        self.get_clipboard().set(self.buffer.get_text(start, end, False))
        self.toasts.add_toast(Adw.Toast(title="Copied"))


class Preferences(Adw.PreferencesDialog):
    """The settings, saved as they change."""

    def __init__(self, window):
        super().__init__(search_enabled=True)
        self.window = window
        cfg = window.cfg

        page = Adw.PreferencesPage(name="connection", title="Connection", icon_name="network-vpn-symbolic")
        group = Adw.PreferencesGroup(title="Gateway", description="Leave the group empty for the VPN’s default connection profile")
        self.group_row = Adw.EntryRow(title="Group", text=cfg["group"])
        self.group_row.connect("changed", lambda r: self.set("group", r.get_text().strip()))
        group.add(self.group_row)
        page.add(group)

        group = Adw.PreferencesGroup(title="Method")
        self.method_row = Adw.ComboRow(title="Connect With", model=Gtk.StringList.new([label for _, label in METHODS]))
        keys = [key for key, _ in METHODS]
        self.method_row.set_selected(keys.index(cfg["backend"]) if cfg["backend"] in keys else 0)
        self.method_row.connect("notify::selected", self.on_method)
        self.nm_row = Adw.EntryRow(title="NetworkManager Connection", text=cfg["connection"])
        self.nm_row.connect("changed", lambda r: self.set("connection", r.get_text().strip() or "Termite"))
        group.add(self.method_row)
        group.add(self.nm_row)
        page.add(group)

        group = Adw.PreferencesGroup(title="DNS")
        row = Adw.SwitchRow(title="Prefer the DNS Server That Answers",
                            subtitle="After connecting, ask the VPN’s DNS servers for a company name and an outside one, and put the one that answers best first",
                            active=cfg["dns"] != "gateway")  # fmt: skip
        row.connect("notify::active", lambda r, *_: self.set("dns", "fastest" if r.get_active() else "gateway"))
        group.add(row)
        page.add(group)

        if integrate.appimage():
            group = Adw.PreferencesGroup(title="Desktop")
            row = Adw.SwitchRow(title="Add to App Menu", subtitle="Termite’s icon and a menu entry for this AppImage",
                                active=cfg["menu"] != "no")  # fmt: skip
            if not integrate.ours():
                row.set_subtitle("Installed some other way already")
                row.set_sensitive(False)
            row.connect("notify::active", self.on_menu)
            group.add(row)
            page.add(group)
        self.add(page)

        page = Adw.PreferencesPage(name="browser", title="Browser", icon_name="web-browser-symbolic")
        group = Adw.PreferencesGroup(
            title="Sign-In Browser",
            description="The Microsoft sign-in opens in a browser profile of its own, with no extensions. "
            "To use a password manager or passkey extension, add it here, then close the browser window before connecting.",
        )
        self.browsers = sso.find_browsers()
        paths = [path for _, path in self.browsers]
        names = [name for name, _ in self.browsers]
        if cfg["browser"] and cfg["browser"] not in paths and os.path.exists(cfg["browser"]):
            paths.append(cfg["browser"])
            names.append(os.path.basename(cfg["browser"]))
        self.paths = paths
        self.browser_row = Adw.ComboRow(title="Browser", model=Gtk.StringList.new(names or ["None Found"]), sensitive=bool(names))
        if cfg["browser"] in paths:
            self.browser_row.set_selected(paths.index(cfg["browser"]))
        self.browser_row.connect("notify::selected", self.on_browser)
        group.add(self.browser_row)
        page.add(group)

        group = Adw.PreferencesGroup(title="Profile")
        self.profile_group = group
        for title, subtitle, target in (
            ("Add Extensions", "The Chrome Web Store", "store"),
            ("Manage Extensions", "Turn extensions on or off, or remove them", "extensions"),
            ("Browser Settings", "Passwords, passkeys and the rest", "settings"),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True, sensitive=bool(names))
            row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            row.connect("activated", lambda _, t=target: window.open_profile(t))
            group.add(row)
        page.add(group)
        self.add(page)

        self.on_method()
        self.on_browser()

    def set(self, key, value):
        self.window.cfg[key] = value
        self.window.save()

    def on_method(self, *_):
        key = METHODS[self.method_row.get_selected()][0]
        self.set("backend", key)
        if key == "auto":
            w = self.window
            self.method_row.set_subtitle(f"Uses {w.method_label} here" if w.method_label else w.method_error)
        elif key == "openconnect":
            self.method_row.set_subtitle("Asks for the administrator password")
        else:
            self.method_row.set_subtitle("No password needed; shows in the network menu")
        self.nm_row.set_visible(key == "networkmanager" or (key == "auto" and self.window.method_label == "NetworkManager"))

    def on_menu(self, row, *_):
        self.set("menu", "yes" if row.get_active() else "no")
        try:
            if row.get_active():
                integrate.install(__version__)
            else:
                integrate.remove()
        except OSError as e:
            self.add_toast(Adw.Toast(title=f"Could not change the menu entry: {e.strerror}"))

    def on_browser(self, *_):
        if not self.paths:
            self.browser_row.set_subtitle("Install Chromium, Google Chrome, Brave, Microsoft Edge or Vivaldi")
            return
        i = self.browser_row.get_selected()
        path = self.paths[i] if 0 <= i < len(self.paths) else self.paths[0]
        # The first one found is the default: keep following it.
        self.set("browser", "" if self.browsers and path == self.browsers[0][1] else path)
        self.profile_group.set_description(f"Kept in {home(sso.default_profile(path))}")


class App(Adw.Application):
    def __init__(self):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags | Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.SEND_ENVIRONMENT)
        self.window = None
        self.tray = None
        for name, fn, accels in (
            ("preferences", self.on_preferences, ["<Control>comma"]),
            ("log", lambda *_: self.window.show_log(), ["<Control>l"]),
            ("shortcuts", self.on_shortcuts, ["<Control>question"]),
            ("about", self.on_about, []),
            ("quit", self.on_quit, ["<Control>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", fn)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("window.close", ["<Control>w"])
        self.set_accels_for_action("win.toggle-connection", ["<Control>Return"])

    def do_activate(self):
        if self.window is None:
            # The logo, connected and not, for the status page (installed
            # with the AppImage; beside the code when run from source).
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(ICONS)
            Gtk.Window.set_default_icon_name(APP_ID)
            # The window's icon, on Wayland, comes from the menu entry named
            # after the app ID: the AppImage puts one in place.
            if config.load()["menu"] != "no":
                try:
                    integrate.install(__version__)
                except OSError as e:
                    print(f"termite: could not add the menu entry: {e}", file=sys.stderr)
            self.window = Window(self)
            try:
                self.tray = tray.Tray(APP_ID, "Termite", LOGO, self.tray_activate, self.tray_toggle, self.on_quit)
            except GLib.Error as e:
                print(f"termite: no tray icon: {e.message}", file=sys.stderr)
            self.window.sync_tray()
        self.window.present()
        seconds = os.environ.get("TERMITE_SELFTEST")
        if seconds:
            GLib.timeout_add(int(float(seconds) * 1000), lambda: self_test(self) and False)

    def on_preferences(self, *_):
        self.window.present()
        Preferences(self.window).present(self.window)

    def on_shortcuts(self, *_):
        if not hasattr(Adw, "ShortcutsDialog"):  # libadwaita 1.8
            return
        dialog = Adw.ShortcutsDialog()
        for title, items in SHORTCUTS:
            section = Adw.ShortcutsSection(title=title)
            for name, accel in items:
                section.add(Adw.ShortcutsItem.new(name, accel))
            dialog.add(section)
        dialog.present(self.window)

    def on_about(self, *_):
        about = Adw.AboutDialog(
            application_name="Termite",
            application_icon=APP_ID,
            developer_name="Travis Huffman",
            version=__version__,
            comments="Cisco Secure Client VPN with Microsoft sign-in, for any Linux desktop.",
            website=REPO,
            issue_url=f"{REPO}/issues",
            copyright="© 2026 Travis Huffman",
            license_type=Gtk.License.MIT_X11,
            debug_info=debug_info(self.window.cfg),
            debug_info_filename="termite-info.json",
        )
        about.add_link("Releases", f"{REPO}/releases")
        about.add_legal_section("Chromium", "© The Chromium Authors", Gtk.License.BSD_3, None)
        about.add_legal_section("OpenConnect", "© David Woodhouse and the OpenConnect contributors", Gtk.License.LGPL_2_1, None)
        about.add_legal_section("vpnc-script", "© The OpenConnect and vpnc contributors", Gtk.License.GPL_2_0, None)
        about.add_legal_section("GTK, libadwaita and GLib", "© The GNOME Project", Gtk.License.LGPL_2_1, None)
        about.present(self.window)

    def do_command_line(self, cmdline):
        """Started again while running (one Termite at a time): the window,
        and a word if the new one is another version, which can't take over
        from this one while it runs."""
        self.activate()
        launched = cmdline.getenv("TERMITE_LAUNCH_VERSION")
        if launched and launched != __version__:
            self.window.toast(f"Termite {__version__} is running; quit it to start {launched}")
        return 0

    def tray_activate(self):
        """Left click: the window, or away with it."""
        w = self.window
        if w.get_visible() and w.is_active():
            w.set_visible(False)
        else:
            w.present()

    def tray_toggle(self):
        """The menu's Connect / Disconnect."""
        self.window.on_connect_clicked()

    def on_quit(self, *_):
        w = self.window
        if w is not None and w.tunnel is not None and not w.tunnel.persistent:
            w.tunnel.down()
        self.quit()


def debug_info(cfg):
    """For About → Troubleshooting: versions, and what this system has."""
    info = {
        "termite": __version__,
        "gtk": f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        "libadwaita": f"{Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
        "browsers": [name for name, _ in sso.find_browsers()],
        "networkmanager": tunnel.NetworkManager.available(),
        "networkmanager_openconnect_plugin": tunnel.NetworkManager.plugin_installed(),
        "openconnect": tunnel.system_openconnect() or tunnel.bundled_openconnect(),
        "no_new_privs": tunnel.no_new_privs(),
        "settings": {k: v for k, v in cfg.items() if k != "gateway"},
    }
    try:
        info["method"] = tunnel.choose(cfg).label
    except tunnel.TunnelError as e:
        info["method"] = f"none: {e}"
    return json.dumps(info, indent=2)


def self_test(app):
    """Test hook (TERMITE_SELFTEST=seconds): say which GTK and libadwaita
    run, then quit after that long (time for a screenshot)."""
    print(f"Gtk {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
          f"Adw {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}", flush=True)  # fmt: skip
    app.quit()


def main(argv=None):
    # For a Termite already running (App.do_command_line).
    os.environ["TERMITE_LAUNCH_VERSION"] = __version__
    return App().run(argv if argv is not None else sys.argv)
