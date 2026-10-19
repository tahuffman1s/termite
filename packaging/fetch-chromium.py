#!/usr/bin/env python3
"""Downloads the open-source Chromium build for the AppImage: Google's
Chromium snapshot (chromium-browser-snapshots, Linux_x64) at, or just
before, the main-branch position the current Stable release branched from,
so it is that milestone's code.

Usage: fetch-chromium.py OUT.zip   (prints "<position> <stable version>")
Environment: CHROMIUM_POSITION, to take a given snapshot position instead.
"""

import json
import os
import sys
import urllib.request

DASH = "https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Linux&num=1"
LIST = "https://www.googleapis.com/storage/v1/b/chromium-browser-snapshots/o?prefix=Linux_x64/{}&delimiter=/&fields=prefixes,nextPageToken"
ZIP = "https://commondatastorage.googleapis.com/chromium-browser-snapshots/Linux_x64/{}/chrome-linux.zip"


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def snapshots(prefix):
    data = json.loads(get(LIST.format(prefix)))
    return sorted(int(p.rstrip("/").rsplit("/", 1)[1]) for p in data.get("prefixes", []))


def main():
    out = sys.argv[1]
    version = "?"
    if os.environ.get("CHROMIUM_POSITION"):
        position = int(os.environ["CHROMIUM_POSITION"])
    else:
        release = json.loads(get(DASH))[0]
        target, version = int(release["chromium_main_branch_position"]), release["version"]
        position = None
        # Snapshots aren't built for every position: the newest one at or
        # below the branch point, looking in ever wider prefixes.
        for cut in (2, 3, 4):
            found = [p for p in snapshots(str(target)[:-cut]) if p <= target]
            if found:
                position = found[-1]
                break
        if position is None:
            raise SystemExit(f"fetch-chromium: no snapshot near {target}")
    with urllib.request.urlopen(ZIP.format(position), timeout=60) as r, open(out, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    print(position, version)


if __name__ == "__main__":
    main()
