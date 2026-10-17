# WallpaperRotate

Scheduled wallpaper rotation for GNOME, with a fair show queue and a small app to manage it.

![WallpaperRotate](docs/screenshot.png)

## Features

- Changes the wallpaper on its own: every hour, every 2, 3, 4, 6 or 12 hours, or once a day. Runs as a systemd user timer, so it works with the app closed.
- **Show queue.** Pictures that were never shown come first (in the order they were added to the folder), then the rest, from the least to the most recently shown. A shown picture moves to the end of the queue, so nothing repeats until every picture has been shown.
- New pictures are picked up automatically: the folder is re-read before every change.
- Show history is always kept; deleted files drop out of it on their own.
- The WallpaperRotate app:
  - preview of the current wallpaper and a **Change now** button;
  - folder and interval settings, time of the next change;
  - thumbnails in queue order with **Next** and **New** badges; click one to set it right away;
  - a **Rescan** button.
- Formats: jpg, jpeg, png, webp. The picture is set for both the light and the dark style.
- If the computer was off when a change was due, the wallpaper changes right after it starts.

## Install

You need GNOME, Python 3 and the GTK 4 / libadwaita bindings (already present on Ubuntu with GNOME):

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

```bash
git clone https://github.com/pelkons/WallpaperRotate.git
```

```bash
bash WallpaperRotate/install.sh
```

The installer puts everything into your home folder, no sudo needed:

| What | Where |
|---|---|
| rotation script | `~/.local/bin/wallpaper-rotate` |
| app | `~/.local/bin/wallpaper-rotate-app` |
| menu entry and icon | `~/.local/share/applications/`, `~/.local/share/icons/` |
| timer (every 4 hours by default) | `~/.config/systemd/user/wallpaper-rotate.timer` |

Pictures are taken from `~/Pictures/Wallpapers` by default; you can pick another folder in the app.

## Command line

```bash
wallpaper-rotate                      # next picture in the queue
wallpaper-rotate ~/Pictures/beach.jpg # a specific picture
wallpaper-rotate --queue              # print the queue
```

```bash
systemctl --user list-timers wallpaper-rotate.timer   # when the next change is due
journalctl --user -u wallpaper-rotate                 # change log
```

## Files

- folder: `~/.config/wallpaper-rotate/settings` (`WALLPAPER_DIR=` line); the `WALLPAPER_DIR` environment variable takes precedence;
- interval: `~/.config/systemd/user/wallpaper-rotate.timer.d/interval.conf`;
- show history: `~/.local/state/wallpaper-rotate/history` (`time<TAB>path` lines).

## Uninstall

```bash
systemctl --user disable --now wallpaper-rotate.timer
```

```bash
rm -rf ~/.local/bin/wallpaper-rotate ~/.local/bin/wallpaper-rotate-app ~/.config/systemd/user/wallpaper-rotate.* ~/.config/wallpaper-rotate ~/.local/state/wallpaper-rotate ~/.local/share/applications/*WallpaperRotate.desktop ~/.local/share/icons/hicolor/scalable/apps/*WallpaperRotate.svg
```

## License

[MIT](LICENSE)
