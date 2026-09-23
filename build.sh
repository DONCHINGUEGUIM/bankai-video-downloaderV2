#!/bin/bash
# build.sh - Build standalone executable with bundled ffmpeg
# On Linux: builds Linux binary
# On Windows (Git Bash/WSL): builds Windows .exe

echo "Installing dependencies..."
pip install -r requirements.txt

# Detect OS and set ffmpeg path
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    # Windows - expects ffmpeg.exe in current folder or C:/ffmpeg/bin/
    FFMPEG_PATH="C:/ffmpeg/bin/ffmpeg.exe"
    if [[ ! -f "$FFMPEG_PATH" ]]; then
        FFMPEG_PATH="./ffmpeg.exe"
    fi
    if [[ ! -f "$FFMPEG_PATH" ]]; then
        echo "Error: ffmpeg.exe not found. Download from https://ffmpeg.org/download.html"
        echo "Place ffmpeg.exe in this folder or at C:/ffmpeg/bin/ffmpeg.exe"
        exit 1
    fi
    echo "Bundling ffmpeg from: $FFMPEG_PATH"
    pyinstaller --onefile --windowed --icon=icon.png --name "BanKaiDownloader" \
        --add-binary "$FFMPEG_PATH;." app.py
else
    # Linux/macOS
    FFMPEG_PATH=$(which ffmpeg)
    if [[ -z "$FFMPEG_PATH" ]]; then
        echo "Error: ffmpeg not found. Install it first."
        exit 1
    fi
    echo "Bundling ffmpeg from: $FFMPEG_PATH"
    pyinstaller --onefile --windowed --icon=icon.png --name "BanKaiDownloader" \
        --add-binary "$FFMPEG_PATH:." app.py
fi

echo "Done! Check dist/ folder."
