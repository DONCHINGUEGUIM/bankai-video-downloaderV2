import json
import os
import sys
import time
from datetime import datetime


def _get_history_dir():
    """Return a permanent, user-writable directory for app data."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
        return os.path.join(base, "BanKaiDownloader")
    elif sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "BanKaiDownloader")
    else:
        return os.path.join(os.path.expanduser("~"), ".config", "bankai_downloader")


HISTORY_DIR = _get_history_dir()
HISTORY_FILE = os.path.join(HISTORY_DIR, "download_history.json")
MAX_ENTRIES = 50


class DownloadTracker:
    def __init__(self):
        self.history = self._load()

    def _load(self):
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE) as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        os.makedirs(HISTORY_DIR, exist_ok=True)
        with open(HISTORY_FILE, "w") as f:
            json.dump(self.history, f, indent=2)

    def add(self, title, url, quality, filename, filepath):
        entry = {
            "title": title,
            "url": url,
            "quality": quality,
            "filename": filename,
            "filepath": filepath,
            "timestamp": time.time(),
            "date": datetime.now().strftime("%b %d, %Y  %H:%M")
        }
        self.history.insert(0, entry)
        if len(self.history) > MAX_ENTRIES:
            self.history = self.history[:MAX_ENTRIES]
        self._save()

    def get_all(self):
        return self.history

    def get_recent(self, n=10):
        return self.history[:n]

    def clear(self):
        self.history = []
        self._save()
