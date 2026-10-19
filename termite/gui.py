"""The window: GTK 4 and libadwaita. Written against libadwaita 1.1 (what
the AppImage bundles), using newer widgets only where they exist."""

import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__, config, sso, tunnel  # noqa: E402

LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"{APP_ID}.svg")

# The logo is the status: in colour when connected, greyed out when not.
CSS = b"""
.status-logo.disconnected { filter: grayscale(100%); opacity: 0.55; }
"""

METHODS = [
    ("auto", "Automatic"),
    ("networkmanager", "NetworkManager"),
    ("openconnect", "openconnect (asks for the admin password)"),
]


def background(fn, done):
    """fn() in a thread; done(result, error) back on the main loop."""

    def work():
        try:
            result, error = fn(), None
        except Exception as e:  # noqa: BLE001 - shown to the user
            result, error = None, e
        GLib.idle_add(lambda: done(result, error) and False)

    threading.Thread(target=work, daemon=True).start()


def entry_row(title, value, placeholder=""):
    """A text row: Adw.EntryRow on libadwaita >= 1.2, else an ActionRow with
    an entry. Returns (row, entry-like widget with get_text/set_text)."""
    if hasattr(Adw, "EntryRow"):
        row = Adw.EntryRow(title=title)
        row.set_text(value)
        return row, row
    row = Adw.ActionRow(title=title)
    entry = Gtk.Entry(text=value, placeholder_text=placeholder, valign=Gtk.Align.CENTER, hexpand=True)
    row.add_suffix(entry)
    row.set_activatable_widget(entry)
    return row, entry


def link_row(title, subtitle, callback):
    row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
    row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
    row.connect("activated", lambda *_: callback())
    return row


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Termite", default_width=480, default_height=760)
        self.cfg = config.load()
        self.tunnel = None  # the one that is up, or connecting
        self.busy = False
        self.cancel = None
        self.setup_proc = None
        self.browsers = sso.find_browsers()

        self.title = Adw.WindowTitle(title="Termite", subtitle="")
        header = Adw.HeaderBar(title_widget=self.title)
        menu = Gio.Menu()
        menu.append("About Termite", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        self.toasts = Adw.ToastOverlay()
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=520, tightening_threshold=400)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        clamp.set_child(page)
        scroll.set_child(clamp)
        self.toasts.set_child(scroll)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.toasts)
        self.set_content(box)

        page.append(self.build_status())
        page.append(self.build_connection())
        page.append(self.build_browser())
        page.append(self.build_details())

        self.connect("close-request", self.on_close)
        self.refresh_state(False)
        self.poll()
        GLib.timeout_add_seconds(3, self.poll)

    # -- layout ----------------------------------------------------------

    def build_status(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        self.icon = Gtk.Image.new_from_file(LOGO)
        self.icon.set_pixel_size(160)
        self.icon.add_css_class("status-logo")
        self.icon.add_css_class("disconnected")
        self.state_label = Gtk.Label(label="Not connected")
        self.state_label.add_css_class("title-1")
        self.spinner = Gtk.Spinner(visible=False)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=12)
        self.connect_button = Gtk.Button(label="Connect", width_request=160)
        self.connect_button.add_css_class("pill")
        self.connect_button.connect("clicked", self.on_connect_clicked)
        self.cancel_button = Gtk.Button(label="Cancel", visible=False)
        self.cancel_button.add_css_class("pill")
        self.cancel_button.connect("clicked", self.on_cancel)
        buttons.append(self.connect_button)
        buttons.append(self.cancel_button)

        for w in (self.icon, self.state_label, self.spinner, buttons):
            box.append(w)
        return box

    def build_connection(self):
        group = Adw.PreferencesGroup(title="Connection")
        self.gateway_row, self.gateway = entry_row("VPN address", self.cfg["gateway"], "vpn.example.com")
        self.group_row, self.group = entry_row("Group (optional)", self.cfg["group"], "the default")
        self.method_row = Adw.ComboRow(title="Connect with", model=Gtk.StringList.new([label for _, label in METHODS]))
        keys = [key for key, _ in METHODS]
        self.method_row.set_selected(keys.index(self.cfg["backend"]) if self.cfg["backend"] in keys else 0)
        self.method_row.connect("notify::selected", self.on_method_changed)
        self.nm_row, self.nm_name = entry_row("NetworkManager connection", self.cfg["connection"], "Termite")
        for row in (self.gateway_row, self.group_row, self.method_row, self.nm_row):
            group.add(row)
        self.on_method_changed()
        return group

    def build_browser(self):
        group = Adw.PreferencesGroup(
            title="Sign-in browser",
            description="The Microsoft sign-in opens in a separate browser profile with no extensions. "
            "To sign in with a password manager or passkey extension, add it to that profile here, "
            "and close the window before connecting.",
        )
        names = [name for name, _ in self.browsers]
        self.browser_paths = [path for _, path in self.browsers]
        if self.cfg["browser"] and self.cfg["browser"] not in self.browser_paths:
            names.append(os.path.basename(self.cfg["browser"]))
            self.browser_paths.append(self.cfg["browser"])
        self.browser_row = Adw.ComboRow(title="Browser")
        if names:
            self.browser_row.set_model(Gtk.StringList.new(names))
            if self.cfg["browser"] in self.browser_paths:
                self.browser_row.set_selected(self.browser_paths.index(self.cfg["browser"]))
            self.browser_row.connect("notify::selected", lambda *_: self.update_browser_subtitle())
        else:
            self.browser_row.set_model(Gtk.StringList.new(["None found"]))
            self.browser_row.set_sensitive(False)
        group.add(self.browser_row)
        self.update_browser_subtitle()
        self.setup_rows = [
            link_row("Add extensions", "The Chrome Web Store, in the sign-in profile", lambda: self.open_profile("store")),
            link_row("Manage extensions", "Turn extensions on or off, or remove them", lambda: self.open_profile("extensions")),
            link_row("Browser settings", "Passwords, passkeys, proxy and the rest", lambda: self.open_profile("settings")),
        ]
        for row in self.setup_rows:
            row.set_sensitive(bool(names))
            group.add(row)
        return group

    def build_details(self):
        group = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title="Details", subtitle="What happened while connecting")
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        for side in ("top", "bottom", "left", "right"):
            getattr(self.log_view, f"set_{side}_margin")(12)
        scroll = Gtk.ScrolledWindow(min_content_height=160, child=self.log_view)
        expander.add_row(scroll)
        group.add(expander)
        return group

    # -- state -----------------------------------------------------------

    def log(self, msg):
        def add():
            buf = self.log_view.get_buffer()
            buf.insert(buf.get_end_iter(), msg.rstrip() + "\n")
            self.log_view.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
            return False

        GLib.idle_add(add)

    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=5))

    def selected_browser(self):
        if not self.browser_paths:
            return None
        i = self.browser_row.get_selected()
        return self.browser_paths[i] if 0 <= i < len(self.browser_paths) else self.browser_paths[0]

    def update_browser_subtitle(self):
        b = self.selected_browser()
        self.browser_row.set_subtitle(
            f"Profile: {sso.default_profile(b).replace(os.path.expanduser('~'), '~', 1)}"
            if b
            else "Install Chromium, Google Chrome, Brave, Microsoft Edge or Vivaldi"
        )

    def read_settings(self):
        self.cfg.update(
            gateway=self.gateway.get_text().strip(),
            group=self.group.get_text().strip(),
            backend=METHODS[self.method_row.get_selected()][0],
            connection=self.nm_name.get_text().strip() or "Termite",
            browser=self.selected_browser() or "",
        )
        if self.browsers and self.cfg["browser"] == self.browsers[0][1]:
            self.cfg["browser"] = ""  # the first one found: keep following it
        try:
            config.save(self.cfg)
        except OSError as e:
            self.log(f"could not save the settings: {e}")
        return self.cfg

    def on_method_changed(self, *_):
        self.nm_row.set_visible(METHODS[self.method_row.get_selected()][0] != "openconnect")

    def refresh_state(self, up):
        self.up = up
        if self.busy:
            return
        if up:
            self.icon.remove_css_class("disconnected")
        else:
            self.icon.add_css_class("disconnected")
        self.state_label.set_label("Connected" if up else "Not connected")
        self.title.set_subtitle("Connected" if up else "")
        self.connect_button.set_label("Disconnect" if up else "Connect")
        for cls in ("suggested-action", "destructive-action"):
            self.connect_button.remove_css_class(cls)
        self.connect_button.add_css_class("destructive-action" if up else "suggested-action")
        for row in (self.gateway_row, self.group_row, self.method_row, self.nm_row):
            row.set_sensitive(not up)

    def set_busy(self, busy, label=""):
        self.busy = busy
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)
        self.connect_button.set_visible(not busy)
        self.cancel_button.set_visible(busy and self.cancel is not None)
        for row in (self.gateway_row, self.group_row, self.method_row, self.nm_row, self.browser_row, *self.setup_rows):
            row.set_sensitive(not busy)
        if busy:
            self.state_label.set_label(label)
            self.title.set_subtitle(label)
        else:
            self.browser_row.set_sensitive(bool(self.browser_paths))
            for row in self.setup_rows:
                row.set_sensitive(bool(self.browser_paths))

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
                self.toast("The tunnel ended (openconnect stopped)")
                self.present()
            return True
        cfg = dict(self.cfg)

        def check():
            t = tunnel.choose(cfg)
            return t, t.is_up()

        def done(result, error):
            if self.busy or (self.tunnel is not None and not self.tunnel.persistent):
                return
            if error is not None:
                self.method_row.set_subtitle(str(error))
                return
            t, up = result
            auto = self.method_row.get_selected() == 0
            self.method_row.set_subtitle(f"Uses {t.label} here" if auto else "")
            if auto:
                self.nm_row.set_visible(t.persistent)
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
        browser = self.selected_browser()
        if not browser:
            return
        self.read_settings()
        try:
            self.setup_proc = sso.open_profile(browser, sso.default_profile(browser), page)
        except OSError as e:
            self.toast(f"Could not start the browser: {e}")

    def on_connect_clicked(self, *_):
        if self.up:
            self.disconnect_vpn()
        else:
            self.connect_vpn()

    def connect_vpn(self):
        cfg = self.read_settings()
        if not cfg["gateway"]:
            self.toast("Enter the VPN address first")
            self.gateway.grab_focus()
            return
        if not self.selected_browser():
            self.toast("No Chromium-based browser found")
            return
        if self.setup_browser_open():
            self.toast("Close the browser profile window first")
            return
        self.cancel = threading.Event()
        self.set_busy(True, "Signing in…")
        self.log(f"--- connecting to {cfg['gateway']}")
        cancel = self.cancel

        def step(msg):
            self.log(msg)

        def work():
            t = tunnel.choose(cfg)
            self.log(f"connecting with {t.label}")
            t.ensure(cfg["gateway"], step)
            session = sso.sign_in(cfg["gateway"], cfg["group"], cfg["browser"] or None, log=step, cancel=cancel)
            if cancel.is_set():
                raise sso.Cancelled()
            GLib.idle_add(lambda: self.connecting(t) and False)
            t.up(session, step)
            return t

        def done(t, error):
            self.cancel = None
            self.set_busy(False)
            if error is None:
                self.tunnel = t
                self.refresh_state(True)
                self.log("connected")
                self.toast("Connected")
                return
            if isinstance(error, sso.Cancelled):
                self.log("cancelled")
                self.refresh_state(False)
                return
            msg = str(error)
            self.log(f"error: {msg}")
            self.refresh_state(False)
            self.toast(f"Could not connect: {msg}")

        background(work, done)

    def connecting(self, t):
        # Past the sign-in: the tunnel can't be cancelled half-way.
        self.cancel = None
        self.cancel_button.set_visible(False)
        self.state_label.set_label("Connecting…")
        self.title.set_subtitle("Connecting…")

    def on_cancel(self, *_):
        if self.cancel is not None:
            self.cancel.set()
            self.cancel_button.set_sensitive(False)
            GLib.timeout_add(1500, lambda: self.cancel_button.set_sensitive(True) and False)

    def disconnect_vpn(self):
        t = self.tunnel or tunnel.choose(self.cfg)
        self.cancel = None
        self.set_busy(True, "Disconnecting…")

        def done(_, error):
            self.set_busy(False)
            if error is not None:
                self.log(f"error: {error}")
                self.toast(f"Could not disconnect: {error}")
                self.refresh_state(t.is_up())
                return
            self.tunnel = None
            self.log("disconnected")
            self.refresh_state(False)

        background(t.down, done)

    def on_close(self, *_):
        self.read_settings()
        if self.tunnel is not None and not self.tunnel.persistent and self.tunnel.is_up():
            # The openconnect tunnel lives in this process: keep it, hide the
            # window; opening Termite again brings it back.
            self.set_visible(False)
            note = Gio.Notification.new("Termite is still connected")
            note.set_body("Open Termite again to disconnect.")
            self.get_application().send_notification("connected", note)
            return True
        return False


class App(Adw.Application):
    def __init__(self):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags)
        self.window = None
        for name, fn in (("about", self.on_about), ("quit", self.on_quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", fn)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self):
        if self.window is None:
            css = Gtk.CssProvider()
            css.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.window = Window(self)
        self.window.present()
        seconds = os.environ.get("TERMITE_SELFTEST")
        if seconds:
            GLib.timeout_add(int(float(seconds) * 1000), lambda: self_test(self) and False)

    def on_about(self, *_):
        kw = dict(
            application_name="Termite",
            application_icon=APP_ID,
            version=__version__,
            comments="Cisco Secure Client (AnyConnect) VPN with Microsoft sign-in in a Chromium-based browser.",
            website="https://github.com/tahuffman1s/termite",
        )
        if hasattr(Adw, "AboutWindow"):
            Adw.AboutWindow(transient_for=self.window, **kw).present()
        else:
            Gtk.AboutDialog(
                transient_for=self.window,
                modal=True,
                program_name=kw["application_name"],
                logo_icon_name=APP_ID,
                version=__version__,
                comments=kw["comments"],
                website=kw["website"],
            ).present()

    def on_quit(self, *_):
        w = self.window
        if w is not None:
            w.read_settings()
            if w.tunnel is not None and not w.tunnel.persistent:
                w.tunnel.down()
        self.quit()


def self_test(app):
    """Test hook (TERMITE_SELFTEST=seconds): say which GTK and libadwaita
    run, then quit after that long (time for a screenshot)."""
    print(f"Gtk {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
          f"Adw {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}", flush=True)  # fmt: skip
    app.quit()


def main(argv=None):
    return App().run(argv if argv is not None else sys.argv)
