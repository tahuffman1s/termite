#!/usr/bin/env bash
# Builds current GTK 4 and libadwaita (with what they need that Ubuntu
# 22.04's are too old for) into PREFIX, for the AppImage: libadwaita 1.9 on
# Ubuntu 22.04's glibc, so the AppImage keeps running on glibc 2.35.
#
# Usage: packaging/build-gnome.sh [PREFIX]   (default /opt/termite-gnome)
# Run on Ubuntu 22.04 (build-appimage.sh does, installing what it needs).
# Sources come from upstream release tarballs, checked against SHA-256
# sums below (SOURCES_DIR: where to keep the downloads). Each one's license
# texts and source go to PREFIX/share/licenses.
set -euo pipefail

PREFIX="${1:-/opt/termite-gnome}"
SOURCES_DIR="${SOURCES_DIR:-}"
JOBS="$(nproc)"

# name  version  url  sha256
SOURCES=(
  "glib 2.88.4 https://download.gnome.org/sources/glib/2.88/glib-2.88.4.tar.xz 25fd169f3b2f91fe2391ad297883ce0fcf30c69fc789ffd1c9672d1332b1c5a8"
  "gobject-introspection 1.86.0 https://download.gnome.org/sources/gobject-introspection/1.86/gobject-introspection-1.86.0.tar.xz 920d1a3fcedeadc32acff95c2e203b319039dd4b4a08dd1a2dfd283d19c0b9ae"
  "harfbuzz 14.6.0 https://github.com/harfbuzz/harfbuzz/releases/download/14.6.0/harfbuzz-14.6.0.tar.xz d07a007327277708a2a73ae437887cdbaf282937f6d03ca5467723e9099af586"
  "fontconfig 2.17.1 https://gitlab.freedesktop.org/api/v4/projects/890/packages/generic/fontconfig/2.17.1/fontconfig-2.17.1.tar.xz 9f5cae93f4fffc1fbc05ae99cdfc708cd60dfd6612ffc0512827025c026fa541"
  "cairo 1.18.4 https://cairographics.org/releases/cairo-1.18.4.tar.xz 445ed8208a6e4823de1226a74ca319d3600e83f6369f99b14265006599c32ccb"
  "pango 1.56.4 https://download.gnome.org/sources/pango/1.56/pango-1.56.4.tar.xz 17065e2fcc5f5a5bdbffc884c956bfc7c451a96e8c4fb2f8ad837c6413cb5a01"
  "wayland 1.24.0 https://gitlab.freedesktop.org/wayland/wayland/-/releases/1.24.0/downloads/wayland-1.24.0.tar.xz 82892487a01ad67b334eca83b54317a7c86a03a89cfadacfef5211f11a5d0536"
  "wayland-protocols 1.49 https://gitlab.freedesktop.org/wayland/wayland-protocols/-/releases/1.49/downloads/wayland-protocols-1.49.tar.xz ec4c8f74942d6dff7ace8b4ce4764f0ef9ff618a935d974ea77edee2ad240b14"
  "gtk 4.22.5 https://download.gnome.org/sources/gtk/4.22/gtk-4.22.5.tar.xz 7fd725deb2cb3f8dc218ad862c5056ff8548f49d3b0e4081796e444c22d19686"
  "libfyaml 0.9.6 https://github.com/pantoniou/libfyaml/releases/download/v0.9.6/libfyaml-0.9.6.tar.gz a59cc3331e2eb903ec36933ad52a45888041cac31e44f553a00511131242c483"
  "libxmlb 0.3.29 https://github.com/hughsie/libxmlb/releases/download/0.3.29/libxmlb-0.3.29.tar.xz 448294be33bfae62f00fa66e506f1cae80237ce71b7ab6530aefa75005eeb08a"
  "AppStream 1.2.1 https://www.freedesktop.org/software/appstream/releases/AppStream-1.2.1.tar.xz 7df664fd3ad3b1640c6a0ff77fa84f9fabe264111bf1fe8f5df3444d6d4caaca"
  "libadwaita 1.9.4 https://download.gnome.org/sources/libadwaita/1.9/libadwaita-1.9.4.tar.xz f5ab3453ee743dc293cdf96d6821789cd20e8d125d9b41637703c12bfa4162ee"
  "pygobject 3.58.1 https://download.gnome.org/sources/pygobject/3.58/pygobject-3.58.1.tar.gz 4c80598ade17fbaa7798e01a25d0bf29ce109740786026a074c5c62bb3e79d23"
)

export PATH="$PREFIX/bin:$PATH"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export LD_LIBRARY_PATH="$PREFIX/lib"
export XDG_DATA_DIRS="$PREFIX/share:/usr/share"
export GI_TYPELIB_PATH="$PREFIX/lib/girepository-1.0"
export CFLAGS="-O2" CXXFLAGS="-O2"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SOURCES_DIR="${SOURCES_DIR:-$WORK/sources}"
mkdir -p "$SOURCES_DIR" "$PREFIX/share/licenses"

src() { # src NAME: unpacked source directory of NAME
  local entry name version url sum file
  for entry in "${SOURCES[@]}"; do
    read -r name version url sum <<<"$entry"
    [ "$name" = "$1" ] || continue
    file="$SOURCES_DIR/${url##*/}"
    [ -s "$file" ] || curl -fsSL -o "$file" "$url"
    echo "$sum  $file" | sha256sum -c --quiet
    tar -xf "$file" -C "$WORK"
    # Its license texts, and where it came from, for the AppImage.
    mkdir -p "$PREFIX/share/licenses/$name"
    find "$WORK/$name-$version" -maxdepth 1 \( -name 'COPYING*' -o -name 'LICENSE*' -o -name 'LICENSES' \) -exec cp -r {} "$PREFIX/share/licenses/$name/" \;
    echo "- $name $version: $url (sha256 $sum)" >"$PREFIX/share/licenses/$name/SOURCE"
    echo "$WORK/$name-$version"
    return
  done
  echo "build-gnome: no source $1" >&2
  exit 1
}

meson_build() { # meson_build NAME [meson options]
  local name="$1" dir
  shift
  dir="$(src "$name")"
  echo "build-gnome: $name" >&2
  meson setup "$WORK/build-$name" "$dir" --prefix="$PREFIX" --libdir=lib --buildtype=release \
    --wrap-mode=nodownload "$@" >"$WORK/$name.log" 2>&1 || { tail -40 "$WORK/$name.log" >&2; exit 1; }
  ninja -C "$WORK/build-$name" -j"$JOBS" >>"$WORK/$name.log" 2>&1 || { tail -40 "$WORK/$name.log" >&2; exit 1; }
  ninja -C "$WORK/build-$name" install >>"$WORK/$name.log" 2>&1 || { tail -40 "$WORK/$name.log" >&2; exit 1; }
  rm -rf "$WORK/build-$name" "$dir"
}

stage() { # stage NAME: build NAME unless done before
  [ -e "$PREFIX/.done-$1" ] && return
  "build_$1"
  touch "$PREFIX/.done-$1"
}

# GLib's introspection data needs gobject-introspection, which needs GLib:
# GLib without it, gobject-introspection, then GLib again with it.
build_glib0() { meson_build glib -Dintrospection=disabled -Dtests=false -Dselinux=disabled -Dlibelf=disabled -Dman-pages=disabled -Ddocumentation=false -Dsysprof=disabled; }
build_gi() { meson_build gobject-introspection -Dcairo=disabled -Dgtk_doc=false -Ddoctool=disabled -Dbuild_introspection_data=true; }
build_glib() { meson_build glib -Dintrospection=enabled -Dtests=false -Dselinux=disabled -Dlibelf=disabled -Dman-pages=disabled -Ddocumentation=false -Dsysprof=disabled; }
build_harfbuzz() { meson_build harfbuzz -Dintrospection=enabled -Dgobject=enabled -Dfreetype=enabled -Dcairo=disabled -Dicu=disabled -Dtests=disabled -Ddocs=disabled -Dutilities=disabled -Dbenchmark=disabled; }
build_fontconfig() { meson_build fontconfig -Ddoc=disabled -Dtests=disabled -Dtools=disabled -Dcache-build=disabled -Dnls=disabled; }
build_cairo() { meson_build cairo -Dtests=disabled -Dgtk_doc=false -Dspectre=disabled -Dsymbol-lookup=disabled -Dlzo=disabled -Dglib=enabled; }
build_pango() { meson_build pango -Dintrospection=enabled -Ddocumentation=false -Dman-pages=false -Dbuild-testsuite=false -Dbuild-examples=false -Dlibthai=disabled -Dxft=disabled; }
build_wayland() { meson_build wayland -Ddocumentation=false -Dtests=false -Ddtd_validation=false; }
build_wlprotocols() { meson_build wayland-protocols -Dtests=false; }
build_gtk() {
  meson_build gtk -Dintrospection=enabled -Ddocumentation=false -Dman-pages=false -Dbuild-demos=false -Dbuild-examples=false \
    -Dbuild-tests=false -Dbuild-testsuite=false -Dmedia-gstreamer=disabled -Dprint-cups=disabled -Dvulkan=disabled \
    -Dcloudproviders=disabled -Dsysprof=disabled -Dtracker=disabled -Dcolord=disabled -Df16c=enabled -Daccesskit=disabled
}
build_fyaml() {
  local dir
  dir="$(src libfyaml)"
  echo "build-gnome: libfyaml" >&2
  (cd "$dir" && ./configure --prefix="$PREFIX" --disable-static --disable-network >"$WORK/fyaml.log" 2>&1 &&
    make -j"$JOBS" >>"$WORK/fyaml.log" 2>&1 && make install >>"$WORK/fyaml.log" 2>&1) || { tail -40 "$WORK/fyaml.log" >&2; exit 1; }
  rm -rf "$dir"
}
build_xmlb() { meson_build libxmlb -Dintrospection=false -Dgtkdoc=false -Dtests=false -Dstemmer=false -Dcli=false -Dlzma=disabled -Dzstd=disabled; }
build_appstream() {
  meson_build AppStream -Dstemming=false -Dgir=false -Dapidocs=false -Dinstall-docs=false -Dsystemd=false -Ddocs=false \
    -Dzstd-support=false -Dcompose=false -Dqt=false -Dvapi=false -Dtools=false -Dman=false -Dbash-completion=false -Ddisplay-detection=none
}
build_adwaita() { meson_build libadwaita -Dintrospection=enabled -Dvapi=false -Dtests=false -Dexamples=false -Ddocumentation=false; }
build_pygobject() { meson_build pygobject -Dpycairo=disabled -Dtests=false -Dpython="$(command -v python3)"; }

for s in glib0 gi glib harfbuzz fontconfig cairo pango wayland wlprotocols gtk fyaml xmlb appstream adwaita pygobject; do
  stage "$s"
done
echo "build-gnome: done in $PREFIX" >&2
