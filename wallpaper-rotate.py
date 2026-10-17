#!/usr/bin/env python3
"""Rotate GNOME wallpapers through a queue.

  wallpaper-rotate                    — set the next picture in the queue
  wallpaper-rotate /path/to/pic.jpg   — set a specific picture
  wallpaper-rotate --queue            — print the queue, one picture per line:
                                        status<TAB>effect<TAB>lowres<TAB>WxH<TAB>path
                                        status: new|seen, effect: exact|crop|fit,
                                        lowres: 1|0

The queue is rebuilt on every run from the current contents of the folder
and all its subfolders:
never-shown pictures first (in the order they were added to the folder),
then shown ones, from the least to the most recently shown.
A shown picture moves to the end of the queue.

Pictures whose aspect ratio differs from the screen are handled by the fit mode:
  auto  — fill the screen if the aspect ratio is close, otherwise fit the whole
          picture over a blurred, darkened copy of itself (default)
  fill  — always fill the screen, cropping what doesn't fit
  fit   — always show the whole picture, black bars around it
  blur  — always show the whole picture over a blurred copy of itself

Settings (~/.config/wallpaper-rotate/settings, written by WallpaperRotate):
  WALLPAPER_DIR=  folder, default ~/Pictures/Wallpapers ($WALLPAPER_DIR wins)
  FIT_MODE=       auto|fill|fit|blur ($WALLPAPER_FIT wins)
Show history: ~/.local/state/wallpaper-rotate/history ("time<TAB>path").
A picture moved to another subfolder (same file name) keeps its history.
Composed pictures are cached in ~/.cache/wallpaper-rotate.
"""
import fcntl
import hashlib
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
STATE = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local" / "state") / "wallpaper-rotate"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "wallpaper-rotate"
SETTINGS = CONFIG / "wallpaper-rotate" / "settings"
HISTORY = STATE / "history"
CURRENT = STATE / "current"  # original picture behind a composed wallpaper
OLD_SHOWN = STATE / "shown"  # old format: a plain list of paths
DEFAULT_DIR = HOME / "Pictures" / "Wallpapers"
EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SCHEMA = "org.gnome.desktop.background"
MODES = ("auto", "fill", "fit", "blur")

EXACT_TOLERANCE = 0.03  # aspect ratios within 3% count as the same
AUTO_TOLERANCE = 0.10   # auto mode crops up to a 10% difference, fits beyond that
LOWRES_FACTOR = 1.5     # upscaled more than 1.5x looks blurry
KEEP_CACHED = 4


def read_settings():
    values = {}
    try:
        for line in SETTINGS.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#") and value.strip():
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def folder():
    return Path(os.environ.get("WALLPAPER_DIR") or read_settings().get("WALLPAPER_DIR") or DEFAULT_DIR)


def fit_mode():
    mode = os.environ.get("WALLPAPER_FIT") or read_settings().get("FIT_MODE") or "auto"
    return mode if mode in MODES else "auto"


def screen_size():
    """Pixel size of the primary monitor, from Mutter; None if unknown."""
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None, "org.gnome.Mutter.DisplayConfig",
            "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig", None)
        _serial, monitors, logical, _props = proxy.call_sync(
            "GetCurrentState", None, Gio.DBusCallFlags.NONE, 2000, None).unpack()
    except GLib.Error:
        return None
    modes = {}
    for spec, monitor_modes, _ in monitors:
        for mode in monitor_modes:
            if mode[6].get("is-current"):
                modes[spec[0]] = (mode[1], mode[2])
    for _x, _y, _scale, transform, primary, specs, _ in logical:
        if primary and specs and specs[0][0] in modes:
            w, h = modes[specs[0][0]]
            return (h, w) if transform in (1, 3, 5, 7) else (w, h)  # rotated
    return next(iter(modes.values()), None)


def images(d):
    """All pictures in the folder and its subfolders (hidden ones are skipped)."""
    found = []
    for root, dirs, files in os.walk(d):  # symlinked folders are not followed
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        found += [os.path.join(root, f) for f in files
                  if not f.startswith(".") and os.path.splitext(f)[1].lower() in EXTS]
    return found


def image_size(path):
    info = GdkPixbuf.Pixbuf.get_file_info(path)
    return (info[1], info[2]) if info and info[0] else None


def judge(size, screen, mode):
    """How a picture of this size will be shown: (effect, lowres)."""
    if not size or not screen:
        return "exact", False
    (w, h), (sw, sh) = size, screen
    diff = abs((w / h) / (sw / sh) - 1)
    if diff <= EXACT_TOLERANCE:
        effect = "exact"
    elif mode == "fill" or (mode == "auto" and diff <= AUTO_TOLERANCE):
        effect = "crop"
    else:
        effect = "fit"
    upscale = min(sw / w, sh / h) if effect == "fit" else max(sw / w, sh / h)
    return effect, upscale > LOWRES_FACTOR


# ---------- history and queue ----------

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
    return hist


def reconcile(hist, imgs):
    """Forget deleted files, but keep the history of a picture moved to another
    subfolder: a missing file whose name matches exactly one unseen picture."""
    unseen = {}
    for p in imgs:
        if p not in hist:
            unseen.setdefault(os.path.basename(p), []).append(p)
    out = {}
    for p, t in sorted(hist.items(), key=lambda x: -x[1]):  # most recent first
        if os.path.isfile(p):
            out.setdefault(p, t)
            continue
        match = unseen.pop(os.path.basename(p), [])
        if len(match) == 1 and match[0] not in out:
            out[match[0]] = t
    return out


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


# ---------- composing pictures that don't match the screen ----------

def load(path):
    pb = GdkPixbuf.Pixbuf.new_from_file(path)
    return pb.apply_embedded_orientation() or pb


def cover(pb, w, h):
    """Scale to cover w×h and crop the middle."""
    scale = max(w / pb.get_width(), h / pb.get_height())
    sw = max(w, round(pb.get_width() * scale))
    sh = max(h, round(pb.get_height() * scale))
    scaled = pb.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR)
    return scaled.new_subpixbuf((sw - w) // 2, (sh - h) // 2, w, h).copy()


def box_blur(pixels, w, h, channels, stride, radius, passes):
    for _ in range(passes):
        for horizontal in (True, False):
            out = bytearray(pixels)
            for y in range(h):
                for x in range(w):
                    for c in range(3):
                        total = count = 0
                        for d in range(-radius, radius + 1):
                            xx, yy = (x + d, y) if horizontal else (x, y + d)
                            if 0 <= xx < w and 0 <= yy < h:
                                total += pixels[yy * stride + xx * channels + c]
                                count += 1
                        out[y * stride + x * channels + c] = total // count
            pixels = out
    return pixels


def blurred_background(pb, sw, sh):
    """The picture itself, heavily blurred and darkened, covering the screen."""
    small_w = 72
    small_h = max(1, round(small_w * sh / sw))
    small = cover(pb, small_w, small_h)
    if small.get_has_alpha():
        small = small.composite_color_simple(small_w, small_h, GdkPixbuf.InterpType.BILINEAR,
                                             255, 1, 0, 0)
    ch, stride = small.get_n_channels(), small.get_rowstride()
    pixels = box_blur(bytearray(small.get_pixels()), small_w, small_h, ch, stride, 2, 2)
    for y in range(small_h):
        for x in range(small_w):
            for c in range(3):
                i = y * stride + x * ch + c
                pixels[i] = pixels[i] * 62 // 100  # darken so the picture stands out
    small = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(bytes(pixels)), GdkPixbuf.Colorspace.RGB,
                                            ch == 4, 8, small_w, small_h, stride)
    return small.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR)


def compose(path, screen, blur):
    """Whole picture centered on the screen; returns the cached file."""
    sw, sh = screen
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_mtime}|{st.st_size}|{sw}x{sh}|{blur}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.jpg"
    if not out.exists():
        pb = load(path)
        if blur:
            bg = blurred_background(pb, sw, sh)
        else:
            bg = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, sw, sh)
            bg.fill(0x000000FF)
        scale = min(sw / pb.get_width(), sh / pb.get_height())
        fw, fh = round(pb.get_width() * scale), round(pb.get_height() * scale)
        x, y = (sw - fw) // 2, (sh - fh) // 2
        pb.composite(bg, x, y, fw, fh, x, y, scale, scale, GdkPixbuf.InterpType.HYPER, 255)
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        bg.savev(str(tmp), "jpeg", ["quality"], ["92"])
        tmp.replace(out)
    os.utime(out)
    # Keep only a few recent compositions
    old = sorted(CACHE.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in old[KEEP_CACHED:]:
        p.unlink(missing_ok=True)
    return str(out)


# ---------- setting the wallpaper ----------

def gsettings(*args):
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, check=True).stdout


def current_wallpaper():
    """The original picture on screen (not the composed copy)."""
    uri = gsettings("get", SCHEMA, "picture-uri").strip().strip("'")
    path = urllib.parse.unquote(urllib.parse.urlparse(uri).path) if uri else ""
    if path.startswith(str(CACHE) + "/"):
        try:
            return CURRENT.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return path


def set_wallpaper(path, hist):
    screen = screen_size()
    mode = fit_mode()
    try:
        effect, _ = judge(image_size(path), screen, mode)
        shown = compose(path, screen, blur=mode != "fit") if effect == "fit" else path
    except (GLib.Error, OSError) as e:
        print(f"could not compose {path}: {e}", file=sys.stderr)
        effect, shown = "exact", path  # GNOME will still try to show the original

    # History first, then the wallpaper: the app refreshes the queue on the change signal
    hist[path] = time.time()
    write_history(hist)
    CURRENT.write_text(path + "\n", encoding="utf-8")
    uri = Path(shown).as_uri()
    gsettings("set", SCHEMA, "picture-uri", uri)
    gsettings("set", SCHEMA, "picture-uri-dark", uri)
    gsettings("set", SCHEMA, "picture-options", "zoom")
    note = {"exact": "", "crop": " (cropped)", "fit": f" ({'fit' if mode == 'fit' else 'blurred edges'})"}
    print(f"wallpaper: {path}{note[effect]}")


def main(argv):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the timer and the app never run at the same time
        d = folder()
        imgs = images(d)
        hist = reconcile(read_history(), imgs)

        if argv and argv[0] == "--queue":
            screen, mode = screen_size(), fit_mode()
            for status, path in build_queue(imgs, hist):
                try:
                    size = image_size(path)
                except GLib.Error:
                    size = None
                effect, lowres = judge(size, screen, mode)
                dims = f"{size[0]}x{size[1]}" if size else "?"
                print(f"{status}\t{effect}\t{int(lowres)}\t{dims}\t{path}")
            return 0

        if argv:
            path = os.path.realpath(argv[0])
            if not os.path.isfile(path):
                print(f"no such file: {argv[0]}", file=sys.stderr)
                return 1
            set_wallpaper(path, hist)
            return 0

        queue = [p for _, p in build_queue(imgs, hist)]
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
