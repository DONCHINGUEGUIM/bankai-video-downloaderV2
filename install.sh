#!/bin/bash
# install.sh - Integrates BanKai Downloader into your system menu

# Auto-detect where this script lives (portable across machines/distros)
APP_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
DESKTOP_FILE="bankai-downloader.desktop"
ICON_NAME="bankai-downloader"

echo "Integrating BanKai Downloader into your system..."
echo "  App directory: $APP_DIR"

# Ensure directories exist
mkdir -p ~/.local/share/applications/
mkdir -p ~/.local/share/icons/hicolor/256x256/apps/

# Generate desktop file with correct paths for THIS machine
sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/$DESKTOP_FILE" > ~/.local/share/applications/$DESKTOP_FILE

# Copy icon to standard theme location
cp "$APP_DIR/icon.png" ~/.local/share/icons/hicolor/256x256/apps/${ICON_NAME}.png

# Also create smaller sizes for the dock
for size in 48 64 96 128; do
    mkdir -p ~/.local/share/icons/hicolor/${size}x${size}/apps/
    cp "$APP_DIR/icon.png" ~/.local/share/icons/hicolor/${size}x${size}/apps/${ICON_NAME}.png
done

# Update the application database
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database ~/.local/share/applications/
fi

# Update icon cache
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache ~/.local/share/icons/hicolor/ -f
fi

echo "Done! You can now find 'BanKai Downloader' in your application menu."
