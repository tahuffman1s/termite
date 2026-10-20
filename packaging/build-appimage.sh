#!/usr/bin/env bash
# Builds Termite-<version>-x86_64.AppImage: Python, PyGObject, GTK 4 and
# libadwaita from Ubuntu 22.04, so it runs on distributions with glibc 2.35
# or newer (Ubuntu 22.04+, Debian 12+, Fedora 36+, Arch, openSUSE
# Tumbleweed, ...), and the open-source Chromium build for the sign-in
# (packaging/fetch-chromium.py), and OpenConnect built from source with
# vpnc-script, for connecting without NetworkManager's plugin. Graphics,
# fonts and the like come from the host (see excludelist). License texts
# and where the sources are: usr/share/licenses.
#
# Usage: packaging/build-appimage.sh [OUT_DIR]   (default: dist/)
# Runs itself in an ubuntu:22.04 container (docker or podman) unless
# TERMITE_BUILD_HERE=1, which CI sets on an Ubuntu 22.04 runner.
# APPIMAGETOOL, APPIMAGE_RUNTIME and CHROMIUM_ZIP: already downloaded
# appimagetool, AppImage runtime and Chromium (chrome-linux.zip) files,
# instead of fetching them. BUILD_CA_CERT: a CA bundle to trust for those
# downloads (behind a TLS-inspecting proxy).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$SRC/dist}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.0/appimagetool-x86_64.AppImage"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64"
OPENCONNECT_VERSION=9.21
OPENCONNECT_URL="https://www.infradead.org/openconnect/download/openconnect-$OPENCONNECT_VERSION.tar.gz"
OPENCONNECT_SHA256=5b32369467db6e5f317aa1ed12cfcbb81ed00bdbc765450b6bfcbdc300944a58
VPNC_SCRIPTS_COMMIT=ce9e961bd0f6b867e1c7c35f78f6fb973f6ff101
VPNC_SCRIPT_SHA256=f0c4d936a382f07711263242699b5e2d85d1ace37136bb78785d352997c17742
VPNC_COPYING_SHA256=8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643

if [ "${TERMITE_BUILD_HERE:-0}" != 1 ]; then
  engine="$(command -v podman || command -v docker || true)"
  [ -n "$engine" ] || {
    echo "build-appimage: needs docker or podman (or run on Ubuntu 22.04 with TERMITE_BUILD_HERE=1)" >&2
    exit 1
  }
  mounts=(-v "$SRC:/src:ro" -v "$OUT:/out")
  [ -z "${APPIMAGETOOL:-}" ] || mounts+=(-v "$(readlink -f "$APPIMAGETOOL"):/tools/appimagetool:ro" -e APPIMAGETOOL=/tools/appimagetool)
  [ -z "${APPIMAGE_RUNTIME:-}" ] || mounts+=(-v "$(readlink -f "$APPIMAGE_RUNTIME"):/tools/runtime:ro" -e APPIMAGE_RUNTIME=/tools/runtime)
  [ -z "${CHROMIUM_ZIP:-}" ] || mounts+=(-v "$(readlink -f "$CHROMIUM_ZIP"):/tools/chrome-linux.zip:ro" -e CHROMIUM_ZIP=/tools/chrome-linux.zip)
  [ -z "${CHROMIUM_POSITION:-}" ] || mounts+=(-e CHROMIUM_POSITION="$CHROMIUM_POSITION")
  [ -z "${BUILD_CA_CERT:-}" ] || mounts+=(-v "$(readlink -f "$BUILD_CA_CERT"):/tools/ca.crt:ro" -e BUILD_CA_CERT=/tools/ca.crt)
  exec "$engine" run --rm -e TERMITE_BUILD_HERE=1 -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    "${mounts[@]}" ubuntu:22.04 /src/packaging/build-appimage.sh /out
fi

if [ -n "${BUILD_CA_CERT:-}" ]; then
  export CURL_CA_BUNDLE="$BUILD_CA_CERT" SSL_CERT_FILE="$BUILD_CA_CERT"
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
  libharfbuzz-gobject0 gsettings-desktop-schemas dconf-gsettings-backend shared-mime-info xkb-data desktop-file-utils ca-certificates curl file \
  unzip binutils libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libcups2 libdrm2 libgbm1 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxext6 libxshmfence1 libasound2 libpango-1.0-0 libcairo2 libdbus-1-3 \
  build-essential pkg-config libssl-dev libxml2-dev zlib1g-dev liblz4-dev patchelf >/dev/null

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

# OpenSSL's provider modules: Ubuntu's cryptography loads the legacy one,
# from where Ubuntu keeps it (AppRun points OPENSSL_MODULES at these).
cp -a "$ARCH_LIB/ossl-modules" "$LIB/"

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
# Of those, the ones every desktop has but not where an AppImage looks on
# some systems (NixOS keeps them in /nix/store): bundled in usr/lib/fallback,
# which AppRun uses only for the ones the host doesn't have in the usual
# places. Only glibc and the graphics drivers' libraries are always the
# host's.
fallback="libstdc++.so.6 libgcc_s.so.1 libz.so.1 libexpat.so.1 libuuid.so.1 libgmp.so.10 libfribidi.so.0
libfontconfig.so.1 libfreetype.so.6 libharfbuzz.so.0 libX11.so.6 libX11-xcb.so.1 libxcb.so.1
libwayland-client.so.0 libcom_err.so.2 libgpg-error.so.0 libICE.so.6 libSM.so.6"
mkdir -p "$LIB/fallback"
# Loaded by soname (typelibs, dlopen), so not in any ldd output.
roots=(
  "$APPDIR/usr/bin/python3" "$APPDIR/usr/bin/gdk-pixbuf-query-loaders"
  "$LIB/python$PYVER"/lib-dynload/*.so "$DIST"/gi/*.so "$DIST"/_cffi_backend*.so
  $(find "$DIST/cryptography" -name '*.so')
  "$LIB"/gdk-pixbuf-2.0/loaders/*.so "$LIB"/gio/modules/*.so "$LIB"/ossl-modules/*.so
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
    if tr ' ' '\n' <<<"$fallback" | grep -Fxq "$name"; then
      cp -L "$path" "$LIB/fallback/$name"
      queue+=("$LIB/fallback/$name")
      continue
    fi
    grep -Fxq "$name" <<<"$excluded" && continue
    [ -f "$LIB/$name" ] || cp -L "$path" "$LIB/$name"
    queue+=("$LIB/$name")
  done < <(ldd "$f" 2>/dev/null | sed -n 's/^\s*\(\S*\) => \(\S*\) .*/\1 \2/p')
done

# Chromium for the sign-in, with its own copies of the libraries it needs
# (usr/lib/chromium-deps), which AppRun hands it only for the ones the
# system doesn't have (NixOS: none of them), like usr/lib/fallback. Its
# setuid sandbox helper can't work from an AppImage: it sandboxes itself
# with user namespaces instead (sso.browser_args).
CHROME="$LIB/chromium"
zip="${CHROMIUM_ZIP:-}"
chromium_snapshot="${CHROMIUM_POSITION:-unknown}"
if [ -z "$zip" ]; then
  zip="$WORK/chrome-linux.zip"
  chromium_snapshot="$(python3 "$SRC/packaging/fetch-chromium.py" "$zip")"
  echo "build-appimage: Chromium snapshot (position, stable version): $chromium_snapshot"
fi
unzip -q "$zip" -d "$WORK/chromium"
mv "$WORK/chromium/chrome-linux" "$CHROME"
rm -f "$CHROME/chrome_sandbox" "$CHROME/chrome-wrapper" "$CHROME"/locales/*.info
strip "$CHROME/chrome" "$CHROME/chrome_crashpad_handler" "$CHROME"/*.so* 2>/dev/null || true
echo "build-appimage: $("$CHROME/chrome" --version)"
DEPS="$LIB/chromium-deps"
mkdir -p "$DEPS/nss"
# NSS loads these itself, from an nss/ directory beside libnss3.
cp -L "$ARCH_LIB"/nss/*.so "$DEPS/nss/"
glibc="ld-linux-x86-64.so.2 libc.so.6 libm.so.6 libdl.so.2 libpthread.so.0 librt.so.1 libresolv.so.2 libutil.so.1"
gl="libGL.so.1 libEGL.so.1 libGLX.so.0 libGLdispatch.so.0 libOpenGL.so.0"
declare -A cseen
cqueue=("$CHROME/chrome" "$CHROME"/*.so* "$DEPS"/nss/*.so)
while [ "${#cqueue[@]}" -gt 0 ]; do
  f="${cqueue[0]}"
  cqueue=("${cqueue[@]:1}")
  while read -r name path; do
    [ -n "$path" ] && [ -f "$path" ] || continue
    [ -z "${cseen[$name]:-}" ] || continue
    cseen[$name]=1
    case " $glibc $gl " in *" $name "*) continue ;; esac
    [ -f "$CHROME/$name" ] && continue # its own (libEGL.so and the like)
    cp -L "$path" "$DEPS/$name"
    cqueue+=("$DEPS/$name")
  done < <(LD_LIBRARY_PATH="$CHROME" ldd "$f" 2>/dev/null | sed -n 's/^\s*\(\S*\) => \(\S*\) .*/\1 \2/p')
done
missing="$(LD_LIBRARY_PATH="$DEPS:$CHROME" ldd "$CHROME/chrome" | grep 'not found' || true)"
[ -z "$missing" ] || { echo "build-appimage: Chromium needs libraries that aren't installed: $missing" >&2; exit 1; }

# OpenConnect, for connecting without NetworkManager's plugin. Ubuntu
# 22.04's (8.20) predates the single sign-on cookie Termite uses, so it is
# built here. It runs as root, which can't read the AppImage's mount:
# Termite streams this directory to it (termite.tunnel), so everything it
# needs is in it, and RUNPATH ($ORIGIN) finds its libraries without an
# LD_LIBRARY_PATH that vpnc-script's programs would inherit.
OC="$LIB/openconnect"
mkdir -p "$OC/lib" "$WORK/oc"
curl -fsSL -o "$WORK/oc/src.tar.gz" "$OPENCONNECT_URL"
echo "$OPENCONNECT_SHA256  $WORK/oc/src.tar.gz" | sha256sum -c --quiet
curl -fsSL -o "$OC/vpnc-script" "https://gitlab.com/openconnect/vpnc-scripts/-/raw/$VPNC_SCRIPTS_COMMIT/vpnc-script"
echo "$VPNC_SCRIPT_SHA256  $OC/vpnc-script" | sha256sum -c --quiet
chmod 755 "$OC/vpnc-script"
tar -xzf "$WORK/oc/src.tar.gz" -C "$WORK/oc"
(
  cd "$WORK/oc/openconnect-$OPENCONNECT_VERSION"
  ./configure --prefix=/opt/openconnect --with-openssl --without-gnutls --disable-nls --disable-docs \
    --without-libproxy --without-stoken --without-libpskc --without-libpcsclite --without-gssapi \
    --without-libseccomp --without-tss2 --with-vpnc-script=/bin/false >"$WORK/oc/configure.log" 2>&1 ||
    { tail -20 "$WORK/oc/configure.log" >&2; exit 1; }
  make -j"$(nproc)" >"$WORK/oc/make.log" 2>&1 || { tail -20 "$WORK/oc/make.log" >&2; exit 1; }
  make install DESTDIR="$WORK/oc/root" >/dev/null
)
cp "$WORK/oc/root/opt/openconnect/sbin/openconnect" "$OC/openconnect"
strip "$OC/openconnect"
declare -A oseen
oqueue=("$OC/openconnect")
while [ "${#oqueue[@]}" -gt 0 ]; do
  f="${oqueue[0]}"
  oqueue=("${oqueue[@]:1}")
  while read -r name path; do
    [ -n "$path" ] && [ -f "$path" ] || continue
    [ -z "${oseen[$name]:-}" ] || continue
    oseen[$name]=1
    case " $glibc " in *" $name "*) continue ;; esac
    cp -L "$path" "$OC/lib/$name"
    strip "$OC/lib/$name" 2>/dev/null || true
    oqueue+=("$OC/lib/$name")
  done < <(LD_LIBRARY_PATH="$WORK/oc/root/opt/openconnect/lib" ldd "$f" 2>/dev/null | sed -n 's/^\s*\(\S*\) => \(\S*\) .*/\1 \2/p')
done
patchelf --set-rpath '$ORIGIN/lib' "$OC/openconnect"
for f in "$OC"/lib/*; do patchelf --set-rpath '$ORIGIN' "$f"; done
echo "build-appimage: $("$OC/openconnect" --version | head -1)"

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

# Keyboard layouts (AppRun uses them where the system has none in
# /usr/share/X11/xkb).
mkdir -p "$APPDIR/usr/share/X11"
cp -a /usr/share/X11/xkb "$APPDIR/usr/share/X11/"

# Desktop entry and icon at the top, as AppImages have them.
mkdir -p "$APPDIR/usr/share/applications"
cp "$SRC/data/io.github.tahuffman1s.Termite.desktop" "$APPDIR/usr/share/applications/"
cp "$SRC/data/io.github.tahuffman1s.Termite.desktop" "$SRC/termite/data/io.github.tahuffman1s.Termite.svg" "$APPDIR/"
ln -s io.github.tahuffman1s.Termite.svg "$APPDIR/.DirIcon"
install -m755 "$SRC/packaging/AppRun" "$APPDIR/AppRun"
install -Dm644 "$SRC/packaging/fonts.conf" "$APPDIR/etc/fonts/fonts.conf"

# Licenses, and where the sources are. Ubuntu's copyright files for every
# package something here comes from; Chromium's license (its third-party
# notices are at chrome://credits in it); OpenConnect's source tarball
# (LGPL) and vpnc-script, which is its own source (GPL).
LIC="$APPDIR/usr/share/licenses"
mkdir -p "$LIC/ubuntu" "$LIC/chromium" "$LIC/openconnect" "$LIC/vpnc-script"
cp "$SRC/LICENSE" "$LIC/termite.LICENSE"
patterns=$(find "$APPDIR/usr" -type f \( -name '*.so*' -o -name '*.typelib' \) -printf '*/%f\n' | sort -u)
pkgs="$( { dpkg -S $patterns 2>/dev/null || true; } | cut -d: -f1 | tr ',' '\n' | tr -d ' ')
python3.10 libpython3.10-stdlib libpython3.10-minimal python3-gi python3-cryptography adwaita-icon-theme hicolor-icon-theme
xkb-data shared-mime-info gsettings-desktop-schemas libgtk-4-common dconf-gsettings-backend libgdk-pixbuf2.0-bin"
pkgs="$(sort -u <<<"$pkgs" | grep -v '^$')"
for pkg in $pkgs; do
  [ -f "/usr/share/doc/$pkg/copyright" ] && cp -L "/usr/share/doc/$pkg/copyright" "$LIC/ubuntu/$pkg.copyright"
done
cp "$SRC/packaging/licenses/chromium-LICENSE" "$LIC/chromium/LICENSE"
cp "$WORK/oc/openconnect-$OPENCONNECT_VERSION/COPYING.LGPL" "$LIC/openconnect/"
cp "$WORK/oc/src.tar.gz" "$LIC/openconnect/openconnect-$OPENCONNECT_VERSION.tar.gz"
curl -fsSL -o "$LIC/vpnc-script/COPYING" "https://gitlab.com/openconnect/vpnc-scripts/-/raw/$VPNC_SCRIPTS_COMMIT/COPYING"
echo "$VPNC_COPYING_SHA256  $LIC/vpnc-script/COPYING" | sha256sum -c --quiet
{
  echo "# What Termite's AppImage contains, and where the sources are"
  echo
  echo "- Termite $VERSION (MIT): https://github.com/tahuffman1s/termite"
  echo "- Chromium $("$CHROME/chrome" --version | awk '{print $2}') (BSD-3-Clause; third-party notices at chrome://credits),"
  echo "  the chromium-browser-snapshots build at position ${chromium_snapshot%% *}:"
  echo "  https://chromium.googlesource.com/chromium/src (the position is a main-branch commit)"
  echo "- OpenConnect $OPENCONNECT_VERSION (LGPL-2.1): its source is openconnect/openconnect-$OPENCONNECT_VERSION.tar.gz here,"
  echo "  built with packaging/build-appimage.sh from the Termite repository"
  echo "- vpnc-script (GPL-2.0), the script itself: https://gitlab.com/openconnect/vpnc-scripts/-/tree/$VPNC_SCRIPTS_COMMIT"
  echo "- From Ubuntu 22.04, unchanged (their copyright files are in ubuntu/; the sources are"
  echo "  at https://launchpad.net/ubuntu/+source/<source package>/<version>):"
  echo
  dpkg-query -W -f='  - ${Package} ${Version} (source: ${source:Package})\n' $pkgs 2>/dev/null || true
} >"$LIC/SOURCES.md"

# Byte-compiled up front: the AppImage is read-only when it runs.
PYTHONHOME="$APPDIR/usr" LD_LIBRARY_PATH="$LIB:$LIB/fallback" "$APPDIR/usr/bin/python3" -s -m compileall -q -j0 "$LIB/python$PYVER" "$DIST" >/dev/null

# Smoke test before packing: everything imports from the AppDir alone.
GI_TYPELIB_PATH="$LIB/girepository-1.0" PYTHONHOME="$APPDIR/usr" LD_LIBRARY_PATH="$LIB:$LIB/fallback" \
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
