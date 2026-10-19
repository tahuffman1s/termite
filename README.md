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

The sign-in window uses a **separate profile** of an installed Chromium-based
browser (Chromium, Google Chrome, Brave, Microsoft Edge or Vivaldi; pick one in
the window). Nothing is added to it by default. To sign in with a password
manager or passkey extension (Proton Pass, Bitwarden, 1Password, ...), use
**Add extensions**, **Manage extensions** or **Browser settings** in the window
(or `termite browser store|extensions|settings`): they open that profile in a
normal browser window. Close it again before connecting; a profile can only be
open in one window at a time.

Profiles are kept in `~/.local/share/termite/<browser>/` (for a snap browser,
`~/snap/<browser>/common/termite/`). Flatpak browsers can't be driven this
way; use a distribution package or the vendor's own package.

## Install

Download `Termite-<version>-x86_64.AppImage` from the
[releases](https://github.com/tahuffman1s/termite/releases), then:

```sh
chmod +x Termite-*-x86_64.AppImage
./Termite-*-x86_64.AppImage
```

It needs glibc 2.35 or newer (Ubuntu 22.04+, Debian 12+, Fedora 36+, Arch,
openSUSE Tumbleweed, ...) and, on the host:

- a Chromium-based browser;
- NetworkManager's OpenConnect plugin (`network-manager-openconnect` on
  Debian/Ubuntu, `NetworkManager-openconnect` on Fedora/openSUSE/Arch), **or**
  `openconnect` and polkit's `pkexec`.

Python, GTK, libadwaita and OpenSSL are inside the AppImage; graphics, fonts and
CA certificates come from the system. The AppImage uses the static AppImage
runtime, so it doesn't need `libfuse2`. Tools like Gear Lever or
AppImageLauncher add it to the app menu.

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
and attaches it to a release for `v*` tags (`git tag v1.0.0 && git push --tags`).

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
