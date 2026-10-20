"""The tray icon: a StatusNotifierItem (what Plasma's system tray, and
GNOME's with the AppIndicator extension, show) with a com.canonical.dbusmenu
menu, over D-Bus with GIO, since GTK 4 has no tray of its own.

Tray(on_activate, on_toggle, on_quit) registers when the desktop has a
StatusNotifierWatcher (again whenever one appears); .available says whether
it is shown. update(connected, busy, tooltip) changes the icon and menu."""

import os

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

SNI_XML = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
 </interface>
</node>"""

MENU_XML = """<node>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg name="parentId" type="i" direction="in"/>
   <arg name="recursionDepth" type="i" direction="in"/>
   <arg name="propertyNames" type="as" direction="in"/>
   <arg name="revision" type="u" direction="out"/>
   <arg name="layout" type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg name="ids" type="ai" direction="in"/>
   <arg name="propertyNames" type="as" direction="in"/>
   <arg name="properties" type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg name="id" type="i" direction="in"/>
   <arg name="name" type="s" direction="in"/>
   <arg name="value" type="v" direction="out"/>
  </method>
  <method name="Event">
   <arg name="id" type="i" direction="in"/>
   <arg name="eventId" type="s" direction="in"/>
   <arg name="data" type="v" direction="in"/>
   <arg name="timestamp" type="u" direction="in"/>
  </method>
  <method name="EventGroup">
   <arg name="events" type="a(isvu)" direction="in"/>
   <arg name="idErrors" type="ai" direction="out"/>
  </method>
  <method name="AboutToShow">
   <arg name="id" type="i" direction="in"/>
   <arg name="needUpdate" type="b" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
   <arg name="ids" type="ai" direction="in"/>
   <arg name="updatesNeeded" type="ai" direction="out"/>
   <arg name="idErrors" type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
   <arg type="a(ia{sv})" name="updatedProps"/>
   <arg type="a(ias)" name="removedProps"/>
  </signal>
  <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
  <signal name="ItemActivationRequested"><arg type="i" name="id"/><arg type="u" name="timestamp"/></signal>
 </interface>
</node>"""

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
SIZES = (16, 22, 24, 32, 48, 64)

TOGGLE, OPEN, SEPARATOR, QUIT = 1, 2, 3, 4


def pixmaps(path, grey=False):
    """The icon at the tray's sizes, as StatusNotifierItem wants it: ARGB32
    in network byte order. grey: faded to grey (not connected)."""
    out = []
    for size in SIZES:
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
        except GLib.Error:
            continue
        if not pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0)
        w, h, stride, data = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_pixels()
        argb = bytearray(w * h * 4)
        for y in range(h):
            row = y * stride
            for x in range(w):
                r, g, b, a = data[row + x * 4 : row + x * 4 + 4]
                if grey:
                    r = g = b = (r * 299 + g * 587 + b * 114) // 1000
                    a = a * 3 // 4
                i = (y * w + x) * 4
                argb[i : i + 4] = bytes((a, r, g, b))
        out.append((w, h, bytes(argb)))
    return out


class Tray:
    def __init__(self, app_id, title, icon_path, on_activate, on_toggle, on_quit):
        self.app_id, self.title = app_id, title
        self.on_activate, self.on_toggle, self.on_quit = on_activate, on_toggle, on_quit
        self.icons = {False: pixmaps(icon_path, grey=True), True: pixmaps(icon_path)}
        self.connected, self.busy, self.tooltip = False, False, ""
        self.revision = 1
        self.available = False
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.bus.register_object(ITEM_PATH, sni, self._item_call, self._item_prop, None)
        self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_prop, None)
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_up, self._watcher_down)

    # -- registration ----------------------------------------------------

    def _watcher_up(self, conn, name, owner):
        def done(conn, res):
            try:
                conn.call_finish(res)
                self.available = True
            except GLib.Error:
                self.available = False

        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None, done)  # fmt: skip

    def _watcher_down(self, conn, name):
        self.available = False

    # -- the item --------------------------------------------------------

    def _item_prop(self, conn, sender, path, iface, prop):
        tip = self.tooltip or self.title
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": GLib.Variant("a(iiay)", self.icons[self.connected]),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], self.title, tip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _item_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "Activate":
            GLib.idle_add(lambda: self.on_activate() and False)
        elif method == "SecondaryActivate":
            GLib.idle_add(lambda: self.on_toggle() and False)
        invocation.return_value(None)

    def _emit(self, path, iface, signal, params=None):
        try:
            self.bus.emit_signal(None, path, iface, signal, params)
        except GLib.Error:
            pass

    # -- the menu --------------------------------------------------------

    def _items(self):
        toggle = "Connecting…" if self.busy else ("Disconnect" if self.connected else "Connect")
        return [
            (TOGGLE, {"label": GLib.Variant("s", toggle), "enabled": GLib.Variant("b", not self.busy)}),
            (OPEN, {"label": GLib.Variant("s", f"Open {self.title}")}),
            (SEPARATOR, {"type": GLib.Variant("s", "separator")}),
            (QUIT, {"label": GLib.Variant("s", "Quit")}),
        ]

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (i, props, [])) for i, props in self._items()]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _menu_prop(self, conn, sender, path, iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(prop)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            items = [(i, p) for i, p in self._items() if not ids or i in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (items,)))
        elif method == "GetProperty":
            item, name = params.unpack()
            props = dict(self._items()).get(item, {})
            invocation.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            item, event = params.unpack()[:2]
            if event == "clicked":
                GLib.idle_add(lambda: self._clicked(item) and False)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item, event, _, _ in params.unpack()[0]:
                if event == "clicked":
                    GLib.idle_add(lambda item=item: self._clicked(item) and False)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _clicked(self, item):
        {TOGGLE: self.on_toggle, OPEN: self.on_activate, QUIT: self.on_quit}.get(item, lambda: None)()

    # -- state -----------------------------------------------------------

    def update(self, connected, busy=False, tooltip=""):
        icon = connected != self.connected
        menu = (connected, busy) != (self.connected, self.busy)
        tip = tooltip != self.tooltip
        self.connected, self.busy, self.tooltip = connected, busy, tooltip
        if icon:
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")
        if tip:
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
        if menu:
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))
