#!/usr/bin/env python3
"""WallpaperRotate — a small window to manage GNOME wallpaper rotation.

Sits on top of the wallpaper-rotate script and the wallpaper-rotate.timer systemd unit:
the folder is stored in ~/.config/wallpaper-rotate/settings, the interval in the drop-in
~/.config/systemd/user/wallpaper-rotate.timer.d/interval.conf.
"""
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

APP_ID = "io.github.pelkons.WallpaperRotate"
APP_NAME = "WallpaperRotate"
HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
STATE = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local" / "state") / "wallpaper-rotate"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "wallpaper-rotate"
SETTINGS = CONFIG / "wallpaper-rotate" / "settings"
DROPIN = CONFIG / "systemd" / "user" / "wallpaper-rotate.timer.d" / "interval.conf"
TIMER = "wallpaper-rotate.timer"
DEFAULT_DIR = HOME / "Pictures" / "Wallpapers"
DEFAULT_HOURS = 4
EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# (label, hours); 0 means off
INTERVALS = [("Off", 0), ("Every hour", 1), ("Every 2 hours", 2),
             ("Every 3 hours", 3), ("Every 4 hours", 4), ("Every 6 hours", 6),
             ("Every 12 hours", 12), ("Once a day", 24)]

# How pictures that don't match the screen are shown: (label, FIT_MODE)
FIT_MODES = [("Auto", "auto"), ("Fill screen, crop edges", "fill"),
             ("Whole picture, black bars", "fit"), ("Whole picture, blurred edges", "blur")]
FIT_HELP = {
    "auto": "Fill if the shape is close to the screen, otherwise blurred edges",
    "fill": "Always fill the screen; parts that don't fit are cut off",
    "fit": "Always show the whole picture with black bars",
    "blur": "Always show the whole picture over a blurred copy of itself",
}

CSS = """
.preview { border-radius: 14px; }
.thumb { border-radius: 8px; }
.thumb-button { padding: 4px; border-radius: 12px; }
.thumb-button.current { box-shadow: inset 0 0 0 4px @accent_bg_color;
                        background: alpha(@accent_bg_color, 0.18); }
.thumb-button.selected { box-shadow: inset 0 0 0 4px @window_fg_color; }
.badge-current { background: @accent_bg_color; color: @accent_fg_color; }
.preview-note { margin: 12px; padding: 4px 12px; border-radius: 99px;
                background: rgba(0, 0, 0, 0.65); color: white; font-weight: bold; }
.badge { margin: 6px; padding: 1px 8px; border-radius: 99px; font-size: 0.8em; font-weight: bold; }
.badge-next { background: @accent_bg_color; color: @accent_fg_color; }
.badge-new { background: rgba(0, 0, 0, 0.6); color: white; }
.badge-info { background: rgba(0, 0, 0, 0.6); color: white; font-weight: normal; }
.badge-warn { background: @warning_bg_color; color: @warning_fg_color; }
"""


def script_path():
    p = HOME / ".local" / "bin" / "wallpaper-rotate"
    return str(p) if p.exists() else shutil.which("wallpaper-rotate") or str(p)


# ---------- settings and timer ----------

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


def write_setting(key, value):
    values = read_settings()
    values[key] = str(value)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text("# Written by WallpaperRotate\n"
                        + "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def read_dir():
    return Path(read_settings().get("WALLPAPER_DIR") or DEFAULT_DIR)


def read_fit_mode():
    mode = read_settings().get("FIT_MODE", "auto")
    return mode if mode in dict((m, l) for l, m in FIT_MODES) else "auto"


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, check=False)


def read_hours():
    if systemctl("is-enabled", TIMER).stdout.strip() != "enabled":
        return 0
    try:
        text = DROPIN.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_HOURS
    if m := re.search(r"^OnCalendar=\S+ 00/(\d+):00:00", text, re.M):
        return int(m.group(1))
    if re.search(r"^OnCalendar=\S+ 00:00:00", text, re.M):
        return 24
    if re.search(r"^OnCalendar=\S+ \*:00:00", text, re.M):
        return 1
    return DEFAULT_HOURS


def write_hours(hours):
    if hours == 0:
        systemctl("disable", "--now", TIMER)
        return
    if hours == 24:
        cal = "*-*-* 00:00:00"
    elif hours == 1:
        cal = "*-*-* *:00:00"
    else:
        cal = f"*-*-* 00/{hours}:00:00"
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    DROPIN.write_text("# Written by WallpaperRotate\n[Timer]\nOnCalendar=\n"
                      f"OnCalendar={cal}\n", encoding="utf-8")
    systemctl("daemon-reload")
    systemctl("enable", TIMER)
    systemctl("restart", TIMER)


def next_run_text():
    out = systemctl("show", TIMER, "-p", "NextElapseUSecRealtime",
                    "--timestamp=unix", "--value").stdout.strip()
    if not out.startswith("@"):
        return "—"
    when = datetime.fromtimestamp(int(out[1:]))
    today = datetime.now().date()
    if when.date() == today:
        day = "today"
    elif when.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = when.strftime("%b %d")
    return f"{day} at {when:%H:%M}"


def walk_dirs(folder):
    """The folder and all its subfolders (hidden ones and symlinks are skipped)."""
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        yield root, files


def list_images(folder):
    return [Path(root, f) for root, files in walk_dirs(folder) for f in sorted(files)
            if not f.startswith(".") and os.path.splitext(f)[1].lower() in EXTS]


def get_queue(folder):
    """Show queue from wallpaper-rotate --queue.

    Returns [{"status": new|seen, "effect": exact|crop|fit, "lowres": bool,
              "size": "WxH", "path": Path}, ...]
    """
    try:
        out = subprocess.run([script_path(), "--queue"], capture_output=True, text=True,
                             env={**os.environ, "WALLPAPER_DIR": str(folder)},
                             timeout=30, check=True).stdout
        queue = []
        for line in out.splitlines():
            parts = line.split("\t", 4)
            if len(parts) == 5:
                status, effect, lowres, size, path = parts
                queue.append({"status": status, "effect": effect, "lowres": lowres == "1",
                              "size": size.replace("x", "×"), "path": Path(path)})
        return queue
    except (OSError, subprocess.SubprocessError):
        return [{"status": "seen", "effect": "exact", "lowres": False, "size": "?", "path": p}
                for p in list_images(folder)]


def shown_file(bg):
    """The file GNOME actually shows (may be a composed copy in the cache)."""
    uri = bg.get_string("picture-uri")
    if Adw.StyleManager.get_default().get_dark():
        uri = bg.get_string("picture-uri-dark") or uri
    return Gio.File.new_for_uri(uri).get_path() if uri else None


def current_wallpaper(bg):
    """The original picture on screen."""
    path = shown_file(bg)
    if path and path.startswith(str(CACHE) + "/"):
        try:
            return (STATE / "current").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return path


def plural_images(n):
    return f"{n} picture" if n == 1 else f"{n} pictures"


def describe_count(queue, folder):
    text = plural_images(len(queue))
    dirs = {item["path"].parent for item in queue}
    if len(dirs) > 1 or (dirs and folder not in dirs):
        text += f" in {len(dirs)} folder" + ("" if len(dirs) == 1 else "s")
    return text


def short_path(path):
    s = str(path)
    home = str(HOME)
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


# ---------- background image loading ----------

def load_texture_bytes(path, width):
    """Runs in a worker thread: a downscaled image as raw pixels."""
    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), width, -1, True)
    pb = pb.apply_embedded_orientation() or pb
    return (pb.get_width(), pb.get_height(), pb.get_has_alpha(),
            pb.get_rowstride(), pb.read_pixel_bytes())


def to_texture(data):
    w, h, alpha, stride, pixels = data
    fmt = Gdk.MemoryFormat.R8G8B8A8 if alpha else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(w, h, fmt, pixels, stride)


class AspectBin(Gtk.Widget):
    """Shows its child at a fixed aspect ratio: as wide as the space allows,
    shrinking (down to min_height) when the window gets short."""

    def __init__(self, child, ratio, min_height, natural_width):
        super().__init__()
        self.ratio, self.min_height, self.natural_width = ratio, min_height, natural_width
        self.child = child
        child.set_parent(self)

    def do_dispose(self):
        if self.child:
            self.child.unparent()
            self.child = None

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        self.child.measure(orientation, -1)
        if orientation == Gtk.Orientation.HORIZONTAL:
            return round(self.min_height * self.ratio), self.natural_width, -1, -1
        width = for_size if for_size > 0 else self.natural_width
        return self.min_height, max(self.min_height, round(width / self.ratio)), -1, -1

    def do_size_allocate(self, width, height, _baseline):
        w = min(width, height * self.ratio)
        h = w / self.ratio
        where = Graphene.Point().init((width - w) / 2, (height - h) / 2)
        self.child.allocate(round(w), round(h), -1, Gsk.Transform.new().translate(where))


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(640, 860)
        self.set_size_request(480, 700)
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.bg = Gio.Settings.new("org.gnome.desktop.background")
        self.folder = read_dir()
        self.generation = 0
        self.thumb_buttons = {}
        self.current_badges = {}
        self.selected = None  # picture shown in the preview but not applied yet
        self.preview_gen = 0
        self.textures = {}  # (path, mtime) -> thumbnail
        self.monitors = []
        self.refresh_pending = 0
        self.ratio = self.screen_ratio()

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.toasts)
        self.set_content(view)

        # Everything always fits: the preview shrinks with the window and only the
        # queue scrolls. Wide windows get two columns: preview and settings | queue.
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                         margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.START)
        layout.append(box)
        self.toasts.set_child(layout)

        wide = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("min-width: 980sp"))
        wide.add_setter(layout, "orientation", Gtk.Orientation.HORIZONTAL)
        wide.add_setter(layout, "spacing", 24)
        self.add_breakpoint(wide)

        # Current wallpaper preview
        self.preview = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.preview.add_css_class("preview")
        self.preview.set_overflow(Gtk.Overflow.HIDDEN)
        preview_box = Gtk.Overlay(child=self.preview)
        self.preview_note = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.START,
                                      ellipsize=3, visible=False)
        self.preview_note.add_css_class("preview-note")
        preview_box.add_overlay(self.preview_note)
        self.cancel_btn = Gtk.Button(icon_name="window-close-symbolic", halign=Gtk.Align.END,
                                     valign=Gtk.Align.START, margin_top=10, margin_end=10,
                                     tooltip_text="Back to the current wallpaper (Esc)", visible=False)
        self.cancel_btn.add_css_class("circular")
        self.cancel_btn.add_css_class("osd")
        self.cancel_btn.connect("clicked", lambda *_: self.clear_selection())
        preview_box.add_overlay(self.cancel_btn)
        box.append(AspectBin(preview_box, self.ratio, min_height=90, natural_width=560))

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.change_btn = Gtk.Button(label="Change now", halign=Gtk.Align.CENTER)
        self.change_btn.add_css_class("pill")
        self.change_btn.add_css_class("suggested-action")
        self.change_btn.connect("clicked", self.on_change_now)
        box.append(self.change_btn)

        # Settings
        group = Adw.PreferencesGroup()
        box.append(group)

        self.folder_row = Adw.ActionRow(title="Folder")
        self.folder_row.add_prefix(Gtk.Image(icon_name="folder-pictures-symbolic"))
        open_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Open in Files")
        open_btn.add_css_class("flat")
        open_btn.connect("clicked", self.on_open_folder)
        pick_btn = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        pick_btn.connect("clicked", self.on_pick_folder)
        self.folder_row.add_suffix(open_btn)
        self.folder_row.add_suffix(pick_btn)
        group.add(self.folder_row)

        self.interval_row = Adw.ComboRow(
            title="Change wallpaper",
            model=Gtk.StringList.new([label for label, _ in INTERVALS]))
        self.interval_row.add_prefix(Gtk.Image(icon_name="alarm-symbolic"))
        hours = read_hours()
        idx = next((i for i, (_, h) in enumerate(INTERVALS) if h == hours), 4)
        self.interval_row.set_selected(idx)
        self.interval_row.connect("notify::selected", self.on_interval)
        group.add(self.interval_row)

        self.fit_row = Adw.ComboRow(
            title="Pictures of another shape",
            model=Gtk.StringList.new([label for label, _ in FIT_MODES]))
        self.fit_row.add_prefix(Gtk.Image(icon_name="zoom-fit-best-symbolic"))
        mode = read_fit_mode()
        self.fit_row.set_selected(next(i for i, (_, m) in enumerate(FIT_MODES) if m == mode))
        self.fit_row.set_subtitle(FIT_HELP[mode])
        self.fit_row.connect("notify::selected", self.on_fit_mode)
        group.add(self.fit_row)

        self.next_row = Adw.ActionRow(title="Next change")
        self.next_row.add_prefix(Gtk.Image(icon_name="x-office-calendar-symbolic"))
        group.add(self.next_row)

        # Thumbnails
        self.thumbs_group = Adw.PreferencesGroup(
            title="Queue",
            description="Click a picture to preview it, then press Change now")
        rescan_btn = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Read the folder again",
                                child=Adw.ButtonContent(icon_name="view-refresh-symbolic",
                                                        label="Rescan"))
        rescan_btn.add_css_class("flat")
        rescan_btn.connect("clicked", self.on_rescan)
        self.thumbs_group.set_header_suffix(rescan_btn)
        self.thumbs_group.set_vexpand(True)
        self.thumbs_group.set_hexpand(True)
        layout.append(self.thumbs_group)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=2, max_children_per_line=6,
                                row_spacing=6, column_spacing=6, valign=Gtk.Align.START,
                                halign=Gtk.Align.CENTER)
        self.empty = Gtk.Label(label="No pictures in this folder (jpg, png, webp)", margin_top=12)
        self.empty.add_css_class("dim-label")
        self.queue_scroll = Gtk.ScrolledWindow(child=self.flow, vexpand=True,
                                               hscrollbar_policy=Gtk.PolicyType.NEVER,
                                               min_content_height=100)
        self.thumbs_group.add(self.queue_scroll)
        self.thumbs_group.add(self.empty)

        self.bg.connect("changed::picture-uri", lambda *_: self.on_wallpaper_changed())
        self.bg.connect("changed::picture-uri-dark", lambda *_: self.on_wallpaper_changed())
        GLib.timeout_add_seconds(30, self.tick)

        self.load_folder()
        self.on_wallpaper_changed()
        self.update_next()

    # ----- helpers -----

    def screen_ratio(self):
        monitors = Gdk.Display.get_default().get_monitors()
        if monitors.get_n_items():
            geo = monitors.get_item(0).get_geometry()
            if geo.height:
                return geo.width / geo.height
        return 16 / 9

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def tick(self):
        self.update_next()
        return True

    def update_next(self):
        hours = INTERVALS[self.interval_row.get_selected()][1]
        self.next_row.set_subtitle(next_run_text() if hours else "rotation is off")

    def load_async(self, path, width, done):
        future = self.pool.submit(load_texture_bytes, path, width)

        def finished(f):
            try:
                data = f.result()
            except Exception:  # broken file etc.
                return
            GLib.idle_add(lambda: (done(to_texture(data)), False)[1])

        future.add_done_callback(finished)

    # ----- folder and thumbnails -----

    def load_folder(self, rescan=False):
        """Rebuild the queue and thumbnails. Returns the queue."""
        self.generation += 1
        gen = self.generation
        if rescan:
            self.textures.clear()
        queue = get_queue(self.folder)
        self.folder_row.set_subtitle(f"{short_path(self.folder)} · {describe_count(queue, self.folder)}")

        while child := self.flow.get_first_child():
            self.flow.remove(child)
        self.thumb_buttons.clear()
        self.current_badges.clear()
        self.empty.set_visible(not queue)
        self.queue_scroll.set_visible(bool(queue))

        tw = 168
        fit_label = "Black bars" if read_fit_mode() == "fit" else "Blurred edges"
        for i, item in enumerate(queue):
            path = item["path"]
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            pic.add_css_class("thumb")
            pic.set_overflow(Gtk.Overflow.HIDDEN)
            overlay = Gtk.Overlay(child=pic)
            badges = Gtk.Box(spacing=0, halign=Gtk.Align.START, valign=Gtk.Align.START)
            if i == 0:
                badges.append(self.badge("Next", "badge-next"))
            if item["status"] == "new":
                badges.append(self.badge("New", "badge-new"))
            overlay.add_overlay(badges)

            notes = Gtk.Box(spacing=0, halign=Gtk.Align.START, valign=Gtk.Align.END)
            try:
                name = str(path.relative_to(self.folder))
            except ValueError:
                name = path.name
            tip = [f"{name} — {item['size']}"]
            if item["effect"] == "crop":
                notes.append(self.badge("Cropped", "badge-info"))
                tip.append("Different shape from the screen: edges are cut off")
            elif item["effect"] == "fit":
                notes.append(self.badge(fit_label, "badge-info"))
                tip.append(f"Different shape from the screen: shown whole, {fit_label.lower()}")
            if item["lowres"]:
                notes.append(self.badge("Low res", "badge-warn"))
                tip.append("Smaller than the screen: will look blurry")
            overlay.add_overlay(notes)
            if path.parent != self.folder:
                # Which subfolder the picture came from
                where = Gtk.Label(label=path.parent.name, halign=Gtk.Align.END,
                                  valign=Gtk.Align.START, ellipsize=3, max_width_chars=14)
                where.add_css_class("badge")
                where.add_css_class("badge-info")
                overlay.add_overlay(where)
            btn = Gtk.Button(child=AspectBin(overlay, self.ratio, min_height=round(tw / self.ratio), natural_width=tw),
                             tooltip_text="\n".join(tip))
            btn.add_css_class("flat")
            btn.add_css_class("thumb-button")
            on_screen = self.badge("Current", "badge-current")
            on_screen.set_halign(Gtk.Align.END)
            on_screen.set_valign(Gtk.Align.END)
            on_screen.set_visible(False)
            overlay.add_overlay(on_screen)
            self.current_badges[str(path)] = on_screen
            btn.connect("clicked", lambda _b, p=path: self.select(p))
            self.flow.append(btn)
            self.thumb_buttons[str(path)] = btn

            try:
                key = (str(path), path.stat().st_mtime)
            except OSError:
                continue
            if key in self.textures:
                pic.set_paintable(self.textures[key])
                continue

            def done(tex, pic=pic, gen=gen, key=key):
                self.textures[key] = tex
                if gen == self.generation:
                    pic.set_paintable(tex)
            self.load_async(path, tw * 2, done)

        if self.selected and str(self.selected) not in self.thumb_buttons:
            self.clear_selection()  # the selected picture is gone
        self.highlight_current()
        self.mark_selected()
        self.watch_folder()
        return queue

    @staticmethod
    def badge(text, kind):
        label = Gtk.Label(label=text)
        label.add_css_class("badge")
        label.add_css_class(kind)
        return label

    def watch_folder(self):
        # One monitor per folder: GIO doesn't watch subfolders on its own.
        # A new subfolder triggers a reload, which starts watching it too.
        for m in self.monitors:
            m.cancel()
        self.monitors = []
        for root, _files in walk_dirs(self.folder):
            if len(self.monitors) >= 1000:
                break
            try:
                m = Gio.File.new_for_path(root).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error:
                continue
            m.connect("changed", self.on_folder_changed)
            self.monitors.append(m)

    def on_folder_changed(self, *_):
        self.schedule_reload()

    def schedule_reload(self):
        # Events often come in bursts: refresh once after a pause
        self.refresh_pending += 1
        mark = self.refresh_pending

        def later():
            if mark == self.refresh_pending:
                self.load_folder()
            return False
        GLib.timeout_add(700, later)

    def highlight_current(self):
        cur = current_wallpaper(self.bg)
        for path, btn in self.thumb_buttons.items():
            if path == cur:
                btn.add_css_class("current")
            else:
                btn.remove_css_class("current")
            self.current_badges[path].set_visible(path == cur)

    def mark_selected(self):
        for path, btn in self.thumb_buttons.items():
            if self.selected and path == str(self.selected):
                btn.add_css_class("selected")
            else:
                btn.remove_css_class("selected")

    def show_in_preview(self, path):
        self.preview_gen += 1
        gen = self.preview_gen

        def done(tex):
            if gen == self.preview_gen:
                self.preview.set_paintable(tex)
        if path and os.path.exists(path):
            self.load_async(path, 1400, done)

    def show_current_preview(self):
        self.show_in_preview(shown_file(self.bg))

    # ----- selecting a picture before applying it -----

    def select(self, path):
        if str(path) == current_wallpaper(self.bg):
            self.clear_selection()  # already on screen
            return
        self.selected = path
        self.mark_selected()
        self.preview_note.set_label(f"Preview: {path.name} — press Change now to set it")
        self.preview_note.set_visible(True)
        self.cancel_btn.set_visible(True)
        self.preview_gen += 1
        gen = self.preview_gen
        # Ask the script how the picture will look on this screen (it may compose it)
        try:
            proc = Gio.Subprocess.new([script_path(), "--preview", str(path)],
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error:
            self.show_in_preview(str(path))
            return

        def done(p, res):
            _ok, out, _err = p.communicate_utf8_finish(res)
            if gen == self.preview_gen and self.selected == path:
                self.show_in_preview((out or "").strip() or str(path))
        proc.communicate_utf8_async(None, None, done)

    def clear_selection(self):
        if self.selected is None:
            return
        self.selected = None
        self.mark_selected()
        self.preview_note.set_visible(False)
        self.cancel_btn.set_visible(False)
        self.show_current_preview()

    def on_key(self, _ctrl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape and self.selected:
            self.clear_selection()
            return True
        return False

    def on_change_now(self, _btn):
        if self.selected:
            path = self.selected
            self.selected = None
            self.mark_selected()
            self.preview_note.set_visible(False)
            self.cancel_btn.set_visible(False)
            self.run_script(str(path))
        else:
            self.run_script()  # next in the queue

    def on_wallpaper_changed(self):
        if not self.selected:
            self.show_current_preview()
        self.highlight_current()
        self.schedule_reload()  # the shown picture moved to the end of the queue

    # ----- actions -----

    def run_script(self, *args):
        self.change_btn.set_sensitive(False)
        try:
            proc = Gio.Subprocess.new([script_path(), *args],
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            self.change_btn.set_sensitive(True)
            self.toast(f"Could not run wallpaper-rotate: {e.message}")
            return

        def done(p, res):
            self.change_btn.set_sensitive(True)
            _ok, _out, err = p.communicate_utf8_finish(res)
            if not p.get_successful() or (err and err.strip()):
                self.toast((err or "").strip() or "Could not change the wallpaper")
            self.schedule_reload()
        proc.communicate_utf8_async(None, None, done)

    def on_pick_folder(self, _btn):
        dialog = Gtk.FileDialog(title="Wallpaper folder", modal=True)
        if self.folder.exists():
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.folder)))

        def chosen(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return  # cancelled
            if not f or not f.get_path():
                return
            self.folder = Path(f.get_path())
            write_setting("WALLPAPER_DIR", self.folder)
            self.load_folder()
            if list_images(self.folder):
                self.run_script()
            else:
                self.toast("No pictures in this folder yet")
        dialog.select_folder(self, None, chosen)

    def on_rescan(self, _btn):
        queue = self.load_folder(rescan=True)
        new = sum(1 for item in queue if item["status"] == "new")
        self.toast(f"Found {plural_images(len(queue))}, {new} new")

    def on_open_folder(self, _btn):
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(self.folder))).launch(self, None, None)

    def on_fit_mode(self, row, _pspec):
        label, mode = FIT_MODES[row.get_selected()]
        write_setting("FIT_MODE", mode)
        row.set_subtitle(FIT_HELP[mode])
        current = current_wallpaper(self.bg)
        if current and os.path.exists(current):
            self.run_script(current)  # show the current picture in the new mode
            if self.selected:
                self.select(self.selected)  # refresh the preview in the new mode
        else:
            self.schedule_reload()

    def on_interval(self, row, _pspec):
        label, hours = INTERVALS[row.get_selected()]
        write_hours(hours)
        self.update_next()
        self.toast("Automatic rotation is off" if hours == 0
                   else f"Wallpaper will change {label.lower()}")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q", "<Control>w"])

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
