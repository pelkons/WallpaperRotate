#!/usr/bin/env python3
"""Rotate GNOME wallpapers through a queue.

  wallpaper-rotate                    — set the next picture in the queue
  wallpaper-rotate /path/to/pic.jpg   — set a specific picture
  wallpaper-rotate --queue            — print the queue: "new|seen<TAB>path"

The queue is rebuilt on every run from the current contents of the folder:
never-shown pictures first (in the order they were added to the folder),
then shown ones, from the least to the most recently shown.
A shown picture moves to the end of the queue.

Folder: $WALLPAPER_DIR, otherwise the WALLPAPER_DIR= line in
~/.config/wallpaper-rotate/settings (written by WallpaperRotate),
otherwise ~/Pictures/Wallpapers.
Show history: ~/.local/state/wallpaper-rotate/history ("time<TAB>path").
"""
import fcntl
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
STATE = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local" / "state") / "wallpaper-rotate"
SETTINGS = CONFIG / "wallpaper-rotate" / "settings"
HISTORY = STATE / "history"
OLD_SHOWN = STATE / "shown"  # old format: a plain list of paths
DEFAULT_DIR = HOME / "Pictures" / "Wallpapers"
EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SCHEMA = "org.gnome.desktop.background"


def folder():
    if os.environ.get("WALLPAPER_DIR"):
        return Path(os.environ["WALLPAPER_DIR"])
    try:
        for line in SETTINGS.read_text(encoding="utf-8").splitlines():
            if line.startswith("WALLPAPER_DIR=") and line.split("=", 1)[1].strip():
                return Path(line.split("=", 1)[1].strip())
    except OSError:
        pass
    return DEFAULT_DIR


def images(d):
    try:
        return [str(p) for p in d.iterdir() if p.is_file() and p.suffix.lower() in EXTS]
    except OSError:
        return []


def read_history():
    hist = {}
    if not HISTORY.exists() and OLD_SHOWN.exists():
        # Migrate the old list: keep the order, timestamps are "long ago"
        for i, line in enumerate(OLD_SHOWN.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                hist[line.strip()] = float(i)
    else:
        try:
            for line in HISTORY.read_text(encoding="utf-8").splitlines():
                t, _, path = line.partition("\t")
                if path:
                    hist[path] = float(t)
        except (OSError, ValueError):
            pass
    # Forget files that were deleted from disk
    return {p: t for p, t in hist.items() if os.path.isfile(p)}


def write_history(hist):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text("".join(f"{t:.3f}\t{p}\n" for p, t in sorted(hist.items(), key=lambda x: x[1])),
                   encoding="utf-8")
    tmp.replace(HISTORY)
    OLD_SHOWN.unlink(missing_ok=True)


def build_queue(imgs, hist):
    def added(p):
        try:
            return (os.stat(p).st_mtime, p)
        except OSError:
            return (0.0, p)
    new = sorted((p for p in imgs if p not in hist), key=added)
    seen = sorted((p for p in imgs if p in hist), key=lambda p: hist[p])
    return [("new", p) for p in new] + [("seen", p) for p in seen]


def gsettings(*args):
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, check=True).stdout


def current_wallpaper():
    uri = gsettings("get", SCHEMA, "picture-uri").strip().strip("'")
    return urllib.parse.unquote(urllib.parse.urlparse(uri).path) if uri else ""


def set_wallpaper(path, hist):
    # History first, then the wallpaper: the app refreshes the queue on the change signal
    hist[path] = time.time()
    write_history(hist)
    uri = Path(path).as_uri()
    gsettings("set", SCHEMA, "picture-uri", uri)
    gsettings("set", SCHEMA, "picture-uri-dark", uri)
    gsettings("set", SCHEMA, "picture-options", "zoom")
    print(f"wallpaper: {path}")


def main(argv):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the timer and the app never run at the same time
        hist = read_history()

        if argv and argv[0] == "--queue":
            for status, path in build_queue(images(folder()), hist):
                print(f"{status}\t{path}")
            return 0

        if argv:
            path = os.path.realpath(argv[0])
            if not os.path.isfile(path):
                print(f"no such file: {argv[0]}", file=sys.stderr)
                return 1
            set_wallpaper(path, hist)
            return 0

        d = folder()
        queue = [p for _, p in build_queue(images(d), hist)]
        if not queue:
            print(f"no pictures in {d}", file=sys.stderr)
            return 0
        pick = queue[0]
        if pick == current_wallpaper() and len(queue) > 1:
            pick = queue[1]  # already on screen (set by something else)
        set_wallpaper(pick, hist)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
