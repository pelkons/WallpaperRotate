#!/usr/bin/env bash
# Installs wallpaper rotation: the wallpaper-rotate script, the timer (every 4 hours
# by default) and the WallpaperRotate app. Everything goes into your home folder.
# Requires python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (already present on Ubuntu with GNOME).
set -euo pipefail
cd "$(dirname "$0")"

APP_ID=io.github.pelkons.WallpaperRotate
APPS=~/.local/share/applications
ICONS=~/.local/share/icons/hicolor/scalable/apps

mkdir -p ~/.local/bin ~/.config/systemd/user ~/Pictures/Wallpapers "$APPS" "$ICONS"

install -m755 wallpaper-rotate.py ~/.local/bin/wallpaper-rotate
install -m755 wallpaper-rotate-app.py ~/.local/bin/wallpaper-rotate-app
install -m644 WallpaperRotate.svg "$ICONS/$APP_ID.svg"
install -m644 WallpaperRotate.desktop "$APPS/$APP_ID.desktop"
cp wallpaper-rotate.service wallpaper-rotate.timer ~/.config/systemd/user/

# The desktop entry uses a bare command, but ~/.local/bin may not be in GNOME's PATH
sed -i "s|^Exec=wallpaper-rotate-app|Exec=$HOME/.local/bin/wallpaper-rotate-app|" "$APPS/$APP_ID.desktop"

systemctl --user daemon-reload
systemctl --user enable --now wallpaper-rotate.timer
gtk-update-icon-cache -q -t ~/.local/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q "$APPS" 2>/dev/null || true
echo "Done: WallpaperRotate is in your app menu."
