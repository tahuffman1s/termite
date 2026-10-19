# Termite

A small GNOME (GTK 4 + libadwaita) app for **Cisco Secure Client / AnyConnect
VPNs that sign in with Microsoft (Entra ID) single sign-on**, on any Linux
distribution. It ships as one AppImage. (Termites dig tunnels.)

Cisco's "embedded browser" sign-in hands the login back to the VPN client from
the window it happened in, so a normal browser tab can't be used, and the
embedded web views of openconnect or the desktop's VPN dialog can't use a
passkey on a phone or a password-manager extension. Termite does the
AnyConnect exchange itself, opens the Microsoft sign-in in a real
Chromium-based browser window, reads the result from it, and then starts the
tunnel:

- **NetworkManager** (with its OpenConnect plugin) if it is there: the VPN shows
  in the desktop's network menu, NetworkManager sets up routes and DNS, and it
  stays up when the window closes. A `Termite` connection is added the first
  time if there is none.
- **openconnect** otherwise, started as root through `pkexec` (it asks for the
  administrator password), with its vpnc-script for routes and DNS. The tunnel
  lasts while Termite runs; closing the window hides it while connected.

Nothing secret is stored: the VPN session only lives as long as the
connection, and the Microsoft sign-in stays in the browser profile.

## The sign-in browser

The sign-in window is **Chromium, built into the AppImage**: Google's
open-source Chromium build, nothing to install. It has a profile of its own
(`~/.local/share/termite/chromium-builtin/`) and nothing is added to it by
default. To sign in with a password manager or passkey extension (Proton
Pass, Bitwarden, 1Password, ...), use **Add extensions** (the Chrome Web Store),
**Manage extensions** or **Browser settings** in the window (or
`termite browser store|extensions|settings`): they open that profile in a
normal Chromium window. Close it again before connecting; a profile can only be
open in one window at a time.

An installed Chromium-based browser (Chromium, Google Chrome, Brave, Microsoft
Edge or Vivaldi) can be picked instead, in **Browser**; each gets its own
profile in `~/.local/share/termite/<browser>/` (for a snap browser,
`~/snap/<browser>/common/termite/`). Flatpak browsers can't be driven this
way.

About the built-in Chromium:

- It is the Chromium snapshot (`chromium-browser-snapshots`) at the point
  the current Stable release branched from, so that milestone's code, picked
  when the AppImage is built (`packaging/fetch-chromium.py`). It doesn't get
  the fixes Stable gets after branching until the next Termite build, so keep
  Termite up to date, or pick an installed browser that your distribution
  updates.
- It sandboxes itself with unprivileged user namespaces, as its setuid helper
  can't work from an AppImage. Where the system doesn't allow those (Ubuntu
  24.04's AppArmor default, some containers), it runs without the sandbox.
- It runs natively on Wayland desktops, and on X11 otherwise. The libraries
  it needs come from the system, or from the AppImage where the system has
  none (NixOS).
- `TERMITE_BROWSER_LOG=1` shows the browser's own messages, for when it
  doesn't start.

## Install

Download `Termite-<version>-x86_64.AppImage` from the
[releases](https://github.com/tahuffman1s/termite/releases), then:

```sh
chmod +x Termite-*-x86_64.AppImage
./Termite-*-x86_64.AppImage
```

It needs glibc 2.35 or newer (Ubuntu 22.04+, Debian 12+, Fedora 36+, Arch,
openSUSE Tumbleweed, ...) and, on the host:

- NetworkManager's OpenConnect plugin (`network-manager-openconnect` on
  Debian/Ubuntu, `NetworkManager-openconnect` on Fedora/openSUSE/Arch), **or**
  `openconnect` and polkit's `pkexec`.

Python, GTK, libadwaita, OpenSSL and Chromium are inside the AppImage (about
210 MB); graphics, fonts and CA certificates come from the system. Common
libraries every desktop has (fontconfig, X11, zlib, ...) come from the system
too, or from the AppImage on systems that keep them elsewhere, like NixOS
(where `programs.nix-ld` must be on, as for any AppImage run directly, or use
`appimage-run`). The AppImage uses the static AppImage runtime, so it doesn't
need `libfuse2`. Tools like Gear Lever or AppImageLauncher add it to the app
menu.

## Command line

The AppImage (or `python3 -m termite`) also works from a terminal:

```
termite                          the window
termite connect --gateway vpn.example.com [--group G] [--backend networkmanager|openconnect]
termite disconnect | status      the NetworkManager connection
termite browser [store|extensions|settings|URL]
termite sign-in GATEWAY [GROUP]  only sign in; print the session as JSON
termite info                     what was found on this system
```

With openconnect, `connect` stays in the foreground (it uses `sudo` in a
terminal) and Ctrl+C disconnects. Settings are kept in
`~/.config/termite/config.json`.

`termite/sso.py` runs on its own with just Python
(`python3 sso.py GATEWAY [GROUP]`; `TERMITE_BROWSER` and `TERMITE_PROFILE`
choose the browser and its profile), for scripts or other launchers that
only need the sign-in.

## Build

```sh
packaging/build-appimage.sh            # dist/Termite-<version>-x86_64.AppImage
```

It builds in an `ubuntu:22.04` container with Docker or Podman (or directly on
Ubuntu 22.04 with `TERMITE_BUILD_HERE=1`, as CI does): Python, PyGObject,
GTK 4.6 and libadwaita 1.1 from Ubuntu, the libraries they need except those in
`packaging/excludelist`, and `packaging/AppRun`, which points them at the
bundle and gives programs Termite starts (browser, nmcli, openconnect) the
system's own environment back. The GitHub workflow
`.github/workflows/appimage.yml` builds it on every push and pull request
and attaches it to a release for `v*` tags (`git tag v1.0.0 && git push --tags`), or
when run by hand from the Actions tab with "release" ticked (tagged
`v<version>` from `termite/__init__.py`).

Tests (a fake gateway, browser, nmcli, pkexec and openconnect; no network or
root needed):

```sh
python3 -m unittest discover -s tests
```

To run from the source tree on a system with PyGObject, GTK 4 and libadwaita:
`python3 -m termite`.

## License

MIT; see [LICENSE](LICENSE). `packaging/excludelist` is from
[pkg2appimage](https://github.com/AppImageCommunity/pkg2appimage) (MIT).
