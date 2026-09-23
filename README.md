# BanKai Downloader (Cross-Platform)

YouTube downloader with a modern GUI. Works on Windows, Linux, and macOS.

## Requirements

- Python 3.10+ **with tkinter** (the GUI toolkit — see the check below)
- ffmpeg (bundled as `ffmpeg.exe` for Windows)

## Setup

### Linux (Debian / Ubuntu / Pop!_OS)

**1. Install system dependencies:**
```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip python3-tk ffmpeg git
```

**2. Verify your Python has tkinter (critical — the app won't start without it):**
```bash
python3 -c "import tkinter" && echo "USE python3"
```

- Prints `USE python3` → your `python3` is fine, continue with `PY=python3`.
- Errors with `No module named '_tkinter'` → your `python3` is Homebrew/pyenv/shims,
  not the Ubuntu one. Use the system interpreter instead:
  ```bash
  /usr/bin/python3.12 -c "import tkinter" && echo "USE python3.12"
  # then:  PY=/usr/bin/python3.12
  ```
  Note: `apt install python3-tk` only adds tkinter to Ubuntu's **own** Python
  (3.12 on 24.04) — a brew/pyenv `python3` will never see it.

**3. Create the venv and install packages (use the interpreter that passed step 2):**
```bash
cd yt_downloader_gui_win
PY=python3                      # or PY=/usr/bin/python3.12
$PY -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### macOS
```bash
brew install python@3.10 ffmpeg
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
If the tkinter check fails on macOS, run `brew install python-tk` and rebuild the venv with the brew Python.

### Windows
```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
(Windows python.org builds include tkinter by default.)

## Run

### Linux / macOS — easy way
```bash
bash run.sh
```

### Linux / macOS — from app menu
```bash
bash install.sh
```
This adds "BanKai Downloader" to your applications menu.

### Linux / macOS — manual
```bash
source venv/bin/activate
python3 app.py
```

### Windows
```cmd
venv\Scripts\activate
python app.py
```

## Build Standalone Executable

### Linux
```bash
source venv/bin/activate
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.png --name "BanKaiDownloader" --add-binary "ffmpeg:." app.py
```
Output: `dist/BanKaiDownloader`

### Windows
```cmd
venv\Scripts\activate
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.png --name "BanKaiDownloader" --add-binary "ffmpeg.exe;." app.py
```
Output: `dist/BanKaiDownloader.exe`

### macOS
```bash
source venv/bin/activate
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.png --name "BanKaiDownloader" --add-binary "ffmpeg:." app.py
```
Output: `dist/BanKaiDownloader`

## How to Use

1. Launch the app
2. (Optional) Click **Browse…** in the **SAVE TO** row to pick where files go
   — remembered between launches (default `~/Downloads`)
3. Paste a YouTube link
4. Click **ANALYZE**
5. Select quality
6. Click **Download**

History and settings live in `~/.config/bankai_downloader/` (Linux),
`%LOCALAPPDATA%\BanKaiDownloader\` (Windows), or
`~/Library/Application Support/BanKaiDownloader/` (macOS) — outside the app
folder, so reinstalls never lose your history.

## Features

- **Choose save folder** — "SAVE TO" row with a Browse… picker, remembered between launches
- Download YouTube videos in multiple qualities
- Extract audio as MP3
- Download history with quick access
- Thumbnail preview
- Progress tracking
- Cross-platform (Windows, Linux, macOS)

## Dependencies

| Package | Purpose |
|---------|---------|
| customtkinter | GUI framework |
| Pillow | Image handling |
| requests | HTTP requests (thumbnails) |
| yt-dlp | YouTube downloading (Python library) |
| pyinstaller | Building standalone executable (build only) |

## ffmpeg

- **Windows**: `ffmpeg.exe` (139MB) bundled inside the .exe — no install needed
- **Linux (Debian/Ubuntu)**: `sudo apt install ffmpeg`
- **macOS**: `brew install ffmpeg`

To verify ffmpeg is installed:
```bash
ffmpeg -version
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | Main GUI application |
| `downloader_engine.py` | yt-dlp wrapper (Python library) |
| `download_tracker.py` | Download history |
| `requirements.txt` | Python dependencies |
| `ffmpeg.exe` | Audio conversion (Windows) |
| `run.sh` | Launcher script (Linux/macOS) |
| `install.sh` | App menu integration (Linux) |
| `bankai-downloader.desktop` | Desktop entry file |
| `build.sh` | Build script |
| `icon.png` | Application icon |

## Troubleshooting

- **App crashes instantly with `No module named '_tkinter'`** — the venv was
  built on a Python without tkinter (Homebrew/pyenv `python3`). Rebuild on the
  system interpreter:
  ```bash
  rm -rf venv
  /usr/bin/python3.12 -m venv venv     # match your installed python3.X
  ./venv/bin/pip install -r requirements.txt
  ```
  Your menu entry and `install.sh` need no changes — only the venv is redone.
- **`ModuleNotFoundError: No module named 'tkinter'`** — the interpreter is
  missing Tk entirely: `sudo apt install python3-tk` (Debian/Ubuntu) or
  `brew install python-tk` (macOS), then rebuild the venv with that interpreter.
- **"Module not found" (customtkinter, yt-dlp, …)**: Run `source venv/bin/activate`
  first, then `pip install -r requirements.txt`
- **MP3 download fails**: Ensure ffmpeg is installed — run `ffmpeg -version` to check
- **"python3-venv not found"** (Debian/Ubuntu): Run `sudo apt install python3-venv`
- **`python3 -m venv` builds the wrong environment**: your shell's `python3` is
  not Ubuntu's (check `readlink -f $(which python3)`). Pass the full path
  explicitly: `/usr/bin/python3.12 -m venv venv`
