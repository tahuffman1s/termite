<p align="center">
  <img src="termite/data/io.github.tahuffman1s.Termite.svg" width="128" alt="Termite">
</p>

<h1 align="center">Termite</h1>

<p align="center">
  <b>Cisco Secure Client VPN with Microsoft sign-in, for any Linux desktop.</b><br>
  <sub>Termites dig tunnels.</sub>
</p>

<p align="center">
  <a href="https://github.com/tahuffman1s/termite/releases/latest"><img src="https://img.shields.io/github/v/release/tahuffman1s/termite?label=AppImage&color=e66100" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-1c71d8" alt="MIT license"></a>
</p>

<p align="center">
  <img src="docs/screenshot.png" alt="Termite, connected (light) and not connected (dark)" width="820">
</p>

Cisco's "embedded browser" sign-in only works in the window it happened in, so
the usual Linux VPN dialogs can't use a passkey, a phone, or a password-manager
extension. Termite signs you in through a real Chromium window instead, then
brings up the tunnel.

## Features

- 🔑 **Real browser sign-in**: passkeys, "use a phone" QR codes, and
  password-manager extensions, in a Chromium profile of its own
- 📦 **Everything included**: Chromium and OpenConnect come in the AppImage;
  no plugins needed
- 🌐 **Plays well with your desktop**: uses NetworkManager when it can, so the
  VPN shows in your network menu
- 🐜 **Tray icon**: see at a glance whether you're connected; connect and
  disconnect from the menu
- 🐧 **Any distribution**: Ubuntu, Debian, Fedora, Arch, openSUSE, NixOS…
  (glibc 2.35 or newer)
- 🔒 **Nothing secret stored**: the VPN session ends with the connection

## Get it

Download the AppImage from the
[latest release](https://github.com/tahuffman1s/termite/releases/latest), then:

```sh
chmod +x Termite-*-x86_64.AppImage
./Termite-*-x86_64.AppImage
```

Enter your VPN address (the one Cisco Secure Client uses), press **Connect**,
and sign in with Microsoft in the window that opens. That's it.

## Extensions

The sign-in browser starts clean. To sign in with a password manager or
passkey extension (Proton Pass, Bitwarden, 1Password…), add it from the Chrome
Web Store with **Add extensions**. You can also use an installed Chrome, Brave,
Edge or Vivaldi instead of the built-in Chromium.

<p align="center">
  <img src="docs/extensions.png" alt="The sign-in browser settings" width="820">
</p>

<details>
<summary><b>Command line</b></summary>

```
termite                          the window
termite connect --gateway vpn.example.com [--group G]
termite disconnect | status
termite browser [store|extensions|settings]
termite sign-in GATEWAY [GROUP]  only sign in; print the session as JSON
termite info                     what was found on this system
```

Settings live in `~/.config/termite/config.json`.
</details>

<details>
<summary><b>How it works</b></summary>

- Termite does the AnyConnect handshake itself, opens the Microsoft sign-in in
  Chromium, and reads the result over Chromium's DevTools pipe.
- With NetworkManager's OpenConnect plugin, it hands the session to
  NetworkManager: no password prompt, and the VPN shows in the network menu.
- Without the plugin, it runs OpenConnect (yours if it's 9.0+, else the bundled
  9.21) as root, after asking for your password through `pkexec`, or
  `systemd-run` inside sandboxes like NixOS's `appimage-run`. vpnc-script sets
  up routes and DNS.
- The built-in Chromium is Google's open-source build from the current Stable
  branch point, refreshed with each Termite release.
- Closing the window keeps Termite in the tray. Quit from the tray, or with
  Ctrl+Q.
- `TERMITE_BROWSER_LOG=1` shows the browser's own messages if it won't start.
</details>

<details>
<summary><b>Building</b></summary>

```sh
packaging/build-appimage.sh     # → dist/Termite-<version>-x86_64.AppImage
python3 -m unittest discover -s tests
```

The build runs in an `ubuntu:22.04` container (Docker or Podman). It bundles
Python, GTK 4 and libadwaita from Ubuntu, Chromium, and OpenConnect built from
source. Behind a TLS-inspecting proxy, set `BUILD_CA_CERT=<ca file>`. GitHub
Actions builds every push. Run the workflow with **release** ticked to publish.
</details>

## License

Termite is [MIT](LICENSE). The AppImage also carries Chromium (BSD),
OpenConnect (LGPL-2.1, source included), vpnc-script (GPL-2.0) and libraries
from Ubuntu. Their licenses and sources are listed in the AppImage under
`usr/share/licenses/`.
