#!/usr/bin/env bash
# Builds Termite-<version>-x86_64.AppImage: Python, PyGObject, GTK 4 and
# libadwaita from Ubuntu 22.04, so it runs on distributions with glibc 2.35
# or newer (Ubuntu 22.04+, Debian 12+, Fedora 36+, Arch, openSUSE
# Tumbleweed, ...). Graphics, fonts and the like come from the host (see
# excludelist).
#
# Usage: packaging/build-appimage.sh [OUT_DIR]   (default: dist/)
# Runs itself in an ubuntu:22.04 container (docker or podman) unless
# TERMITE_BUILD_HERE=1, which CI sets on an Ubuntu 22.04 runner.
# APPIMAGETOOL and APPIMAGE_RUNTIME: already downloaded appimagetool and
# AppImage runtime files, instead of fetching them from GitHub.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$SRC/dist}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.0/appimagetool-x86_64.AppImage"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64"

if [ "${TERMITE_BUILD_HERE:-0}" != 1 ]; then
  engine="$(command -v podman || command -v docker || true)"
  [ -n "$engine" ] || {
    echo "build-appimage: needs docker or podman (or run on Ubuntu 22.04 with TERMITE_BUILD_HERE=1)" >&2
    exit 1
  }
  mounts=(-v "$SRC:/src:ro" -v "$OUT:/out")
  [ -z "${APPIMAGETOOL:-}" ] || mounts+=(-v "$(readlink -f "$APPIMAGETOOL"):/tools/appimagetool:ro" -e APPIMAGETOOL=/tools/appimagetool)
  [ -z "${APPIMAGE_RUNTIME:-}" ] || mounts+=(-v "$(readlink -f "$APPIMAGE_RUNTIME"):/tools/runtime:ro" -e APPIMAGE_RUNTIME=/tools/runtime)
  exec "$engine" run --rm -e TERMITE_BUILD_HERE=1 -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    "${mounts[@]}" ubuntu:22.04 /src/packaging/build-appimage.sh /out
fi

. /etc/os-release
[ "$VERSION_ID" = 22.04 ] || echo "build-appimage: warning: building on $PRETTY_NAME, not Ubuntu 22.04; the AppImage needs this glibc or newer" >&2

SUDO=""
[ "$(id -u)" = 0 ] || SUDO=sudo
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq --no-install-recommends \
  python3 python3-gi python3-cryptography gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 \
  librsvg2-common libgdk-pixbuf2.0-bin adwaita-icon-theme libglib2.0-bin libgtk-4-bin \
  libharfbuzz-gobject0 gsettings-desktop-schemas dconf-gsettings-backend shared-mime-info desktop-file-utils ca-certificates curl file >/dev/null

ARCH_LIB=/usr/lib/x86_64-linux-gnu
PY="$(readlink -f /usr/bin/python3)" # python3.10
PYVER="$(basename "$PY" | sed 's/^python//')"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/termite/__init__.py")"
WORK="$(mktemp -d)"
APPDIR="$WORK/AppDir"
LIB="$APPDIR/usr/lib"
mkdir -p "$APPDIR/usr/bin" "$LIB" "$APPDIR/usr/share"

echo "build-appimage: Termite $VERSION, Python $PYVER"

# Python: the interpreter, its standard library (less what a GUI app
# doesn't use) and the two packages from Ubuntu.
cp "$PY" "$APPDIR/usr/bin/python3"
cp -a "/usr/lib/python$PYVER" "$LIB/"
rm -rf "$LIB/python$PYVER"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,distutils,pydoc_data,config-*,site-packages,dist-packages}
find "$LIB/python$PYVER" -name __pycache__ -prune -exec rm -rf {} +
DIST="$LIB/python3/dist-packages"
mkdir -p "$DIST"
cp -a /usr/lib/python3/dist-packages/{gi,cryptography,_cffi_backend.cpython-*.so} "$DIST/"
cp -a "$SRC/termite" "$DIST/"
printf 'from termite.cli import main\n\nmain()\n' >"$APPDIR/usr/bin/termite"
find "$DIST/termite" -name __pycache__ -prune -exec rm -rf {} +

# Typelibs, and what loads them; the image loaders.
mkdir -p "$LIB/girepository-1.0" "$LIB/gdk-pixbuf-2.0/loaders" "$LIB/gio/modules"
cp "$ARCH_LIB"/girepository-1.0/*.typelib "$LIB/girepository-1.0/"
cp "$ARCH_LIB"/gdk-pixbuf-2.0/2.10.0/loaders/libpixbufloader-{svg,png,jpeg,gif}.so "$LIB/gdk-pixbuf-2.0/loaders/" 2>/dev/null || true
cp "$ARCH_LIB/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders" "$APPDIR/usr/bin/"
# GSettings through dconf, so GTK sees the desktop's settings (fonts, dark
# style, ...) where GNOME keeps them.
cp "$ARCH_LIB/gio/modules/libdconfsettings.so" "$LIB/gio/modules/"

# Every library those need, except the ones the host has to provide.
excluded="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$SRC/packaging/excludelist" | grep -v '^$')"
# Loaded by soname (typelibs, dlopen), so not in any ldd output.
roots=(
  "$APPDIR/usr/bin/python3" "$APPDIR/usr/bin/gdk-pixbuf-query-loaders"
  "$LIB/python$PYVER"/lib-dynload/*.so "$DIST"/gi/*.so "$DIST"/_cffi_backend*.so
  $(find "$DIST/cryptography" -name '*.so')
  "$LIB"/gdk-pixbuf-2.0/loaders/*.so "$LIB"/gio/modules/*.so
)
for so in libgtk-4.so.1 libadwaita-1.so.0 libgirepository-1.0.so.1 libgraphene-1.0.so.0 libcairo-gobject.so.2 \
  libpangocairo-1.0.so.0 libpangoft2-1.0.so.0 libharfbuzz-gobject.so.0 libgdk_pixbuf-2.0.so.0 librsvg-2.so.2; do
  cp -L "$ARCH_LIB/$so" "$LIB/"
  roots+=("$LIB/$so")
done
declare -A seen
queue=("${roots[@]}")
while [ "${#queue[@]}" -gt 0 ]; do
  f="${queue[0]}"
  queue=("${queue[@]:1}")
  while read -r name path; do
    [ -n "$path" ] && [ -f "$path" ] || continue
    [ -z "${seen[$name]:-}" ] || continue
    seen[$name]=1
    grep -Fxq "$name" <<<"$excluded" && continue
    [ -f "$LIB/$name" ] || cp -L "$path" "$LIB/$name"
    queue+=("$LIB/$name")
  done < <(ldd "$f" 2>/dev/null | sed -n 's/^\s*\(\S*\) => \(\S*\) .*/\1 \2/p')
done

# The settings schemas GTK and libadwaita read: their own and GNOME's
# desktop ones (fonts, style, ...).
mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
cp /usr/share/glib-2.0/schemas/org.gtk.gtk4.*.gschema.xml /usr/share/glib-2.0/schemas/org.gnome.desktop.*.gschema.xml \
  /usr/share/glib-2.0/schemas/*.enums.xml "$APPDIR/usr/share/glib-2.0/schemas/"
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"

# Icons: Adwaita's scalable and symbolic ones as the fallback theme (the
# host's own theme comes first), hicolor, and the app's.
mkdir -p "$APPDIR/usr/share/icons/Adwaita" "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -a /usr/share/icons/Adwaita/{index.theme,scalable,scalable-up-to-32} "$APPDIR/usr/share/icons/Adwaita/"
cp /usr/share/icons/hicolor/index.theme "$APPDIR/usr/share/icons/hicolor/"
cp "$SRC/termite/data/io.github.tahuffman1s.Termite.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/"
gtk4-update-icon-cache -q -t -f "$APPDIR/usr/share/icons/Adwaita"
gtk4-update-icon-cache -q -t -f "$APPDIR/usr/share/icons/hicolor"

# The MIME database, which GDK uses to tell an SVG icon is one; desktops
# have it, minimal systems may not.
mkdir -p "$APPDIR/usr/share/mime"
cp /usr/share/mime/{mime.cache,globs2,magic,aliases,subclasses,types,treemagic,generic-icons,icons} "$APPDIR/usr/share/mime/" 2>/dev/null || true

# Desktop entry and icon at the top, as AppImages have them.
mkdir -p "$APPDIR/usr/share/applications"
cp "$SRC/data/io.github.tahuffman1s.Termite.desktop" "$APPDIR/usr/share/applications/"
cp "$SRC/data/io.github.tahuffman1s.Termite.desktop" "$SRC/termite/data/io.github.tahuffman1s.Termite.svg" "$APPDIR/"
ln -s io.github.tahuffman1s.Termite.svg "$APPDIR/.DirIcon"
install -m755 "$SRC/packaging/AppRun" "$APPDIR/AppRun"

# Byte-compiled up front: the AppImage is read-only when it runs.
PYTHONHOME="$APPDIR/usr" LD_LIBRARY_PATH="$LIB" "$APPDIR/usr/bin/python3" -s -m compileall -q -j0 "$LIB/python$PYVER" "$DIST" >/dev/null

# Smoke test before packing: everything imports from the AppDir alone.
GI_TYPELIB_PATH="$LIB/girepository-1.0" PYTHONHOME="$APPDIR/usr" LD_LIBRARY_PATH="$LIB" \
  "$APPDIR/usr/bin/python3" -s -c '
import gi, ssl, cryptography
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
import termite.gui, termite.cli
print("build-appimage: imports ok:", ssl.OPENSSL_VERSION)'

tool="$WORK/appimagetool"
runtime="$WORK/runtime"
if [ -n "${APPIMAGETOOL:-}" ]; then cp "$APPIMAGETOOL" "$tool"; else curl -fsSL -o "$tool" "$APPIMAGETOOL_URL"; fi
if [ -n "${APPIMAGE_RUNTIME:-}" ]; then cp "$APPIMAGE_RUNTIME" "$runtime"; else curl -fsSL -o "$runtime" "$RUNTIME_URL"; fi
chmod +x "$tool"
out="$OUT/Termite-$VERSION-x86_64.AppImage"
# The static runtime: the AppImage needs no libfuse2 on the host.
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$tool" --no-appstream --runtime-file "$runtime" "$APPDIR" "$out" >"$WORK/appimagetool.log" 2>&1 || {
  cat "$WORK/appimagetool.log" >&2
  exit 1
}
[ -z "${HOST_UID:-}" ] || chown "$HOST_UID:${HOST_GID:-$HOST_UID}" "$out"
echo "build-appimage: $out ($(du -h "$out" | cut -f1))"
