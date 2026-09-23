import glob
import os
import re
import shutil
import sys
import yt_dlp


def _get_ffmpeg_path():
    """Absolute path to the ffmpeg binary (or None if not found).

    Frozen builds ship ffmpeg inside the bundle (_MEIPASS); dev runs must
    resolve the system ffmpeg from PATH — a bare "ffmpeg" would make
    os.path.dirname() return "" and yt-dlp would reject ffmpeg_location.
    """
    if getattr(sys, 'frozen', False):
        bundle_dir = sys._MEIPASS
        if sys.platform == "win32":
            ffmpeg = os.path.join(bundle_dir, "ffmpeg.exe")
        else:
            ffmpeg = os.path.join(bundle_dir, "ffmpeg")
        if os.path.exists(ffmpeg):
            return ffmpeg
    return shutil.which("ffmpeg")


def _clean_url(url):
    if "&pp=" in url:
        url = url.split("&pp=")[0]
    return url.strip()


def _has_firefox_cookies():
    """True if a Firefox cookies.sqlite exists somewhere on this machine.

    Mirrors yt-dlp's own search roots/patterns so we only request
    cookiesfrombrowser when it can succeed — machines without Firefox
    (the packaged Windows exe) never trigger yt-dlp's hard-fail path.
    Chrome is deliberately NOT used as a fallback: since Chrome 127 its
    cookies are app-bound (v20) which yt-dlp cannot decrypt on Windows
    (DPAPI fallback raises DownloadError), per yt-dlp's official FAQ.
    """
    home = os.path.expanduser("~")
    if sys.platform in ("win32", "cygwin"):
        roots = [os.path.join(os.environ.get("APPDATA", ""),
                              "Mozilla", "Firefox")]
    elif sys.platform == "darwin":
        roots = [os.path.join(home, "Library", "Application Support", "Firefox")]
    else:
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        roots = [
            os.path.join(cfg, "mozilla", "firefox"),            # FF147+ XDG
            os.path.join(home, ".mozilla", "firefox"),          # classic
            os.path.join(home, ".var/app/org.mozilla.firefox/config/mozilla/firefox"),
            os.path.join(home, ".var/app/org.mozilla.firefox/.mozilla/firefox"),
            os.path.join(home, "snap/firefox/common/.mozilla/firefox"),
        ]
    # Same glob patterns yt-dlp uses (_firefox_cookie_dbs)
    for root in roots:
        for pattern in ("", "*/", "Profiles/*/"):
            if glob.glob(os.path.join(root, pattern, "cookies.sqlite")):
                return True
    return False


def _ydl_run(opts, url, download=False):
    """Run yt-dlp (extract or download) with one cookieless retry.

    cookiesfrombrowser hard-fails on machines with no Firefox profile —
    the packaged Windows exe must still run there. Cookies only help with
    age-restricted/consent videos, so silently retrying without them is the
    safe fallback; genuine non-cookie errors propagate unchanged.
    """

    def run(o):
        with yt_dlp.YoutubeDL(o) as ydl:
            if download:
                ydl.download([url])
                return None
            return ydl.extract_info(url, download=False)

    try:
        return run(opts)
    except Exception as e:
        # Retry only for cookie-loader failure modes (missing DB, locked
        # DB, DPAPI decrypt) — and only when we requested browser cookies.
        if "cookiesfrombrowser" not in opts:
            raise
        msg = str(e).lower()
        if not any(t in msg for t in ("cookie", "database", "dpapi")):
            raise
        retry = {k: v for k, v in opts.items() if k != "cookiesfrombrowser"}
        return run(retry)


class DownloaderEngine:
    def __init__(self, download_dir="Downloads"):
        self.download_dir = download_dir
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

    def get_info(self, url):
        url = _clean_url(url)

        # Firefox cookies help with age-restricted videos — only request
        # them when a profile actually exists (else analysis would hard-fail
        # on the packaged exe). _ydl_run still retries cookieless if the
        # extraction breaks for any cookie reason.
        opts = {
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
            "noplaylist": True,
            "skip_download": True,
            "extract_flat": False,
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github"},
        }
        if _has_firefox_cookies():
            opts["cookiesfrombrowser"] = ("firefox",)

        try:
            info = _ydl_run(opts, url, download=False)

            formats = info.get("formats", [])
            res_map = {}

            for f in formats:
                h = f.get("height")
                if h and f.get("vcodec") != "none":
                    size = f.get("filesize") or f.get("filesize_approx") or 0
                    if h not in res_map or size > res_map[h]:
                        res_map[h] = size

            sorted_heights = sorted(res_map.keys(), reverse=True)
            formatted_res = []
            for h in sorted_heights:
                size_bytes = res_map[h]
                if size_bytes > 0:
                    size_mb = size_bytes / (1024 * 1024)
                    formatted_res.append(f"{h}p (~{size_mb:.1f} MB)")
                else:
                    formatted_res.append(f"{h}p")

            formatted_res.append("Audio Only (MP3)")
            info["available_resolutions"] = formatted_res

            for key in list(info.keys()):
                if key not in ("title", "duration", "channel", "uploader", "thumbnail", "available_resolutions", "formats", "webpage_url", "id"):
                    del info[key]

            return info

        except Exception as e:
            return {"error": f"Analysis failed: {str(e)}"}

    def download(self, url, format_id=None, progress_callback=None, download_path=None):
        url = _clean_url(url)

        target_dir = download_path or self.download_dir
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)

        def progress_hook(d):
            """Push download percent into the GUI bar.

            Prefer raw byte counts: when stdout is a TTY (app launched from
            a terminal) yt-dlp colorizes _percent_str with ANSI escapes and
            float() parsing silently fails, freezing the bar. Byte math is
            immune to colors. finished → 100 so the bar completes before
            ffmpeg merge/convert (no hook events fire during postprocess).
            """
            if not progress_callback:
                return
            status = d.get("status")
            if status == "finished":
                progress_callback(100.0)
                return
            if status != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes")
            if total and done is not None and total > 0:
                progress_callback(100.0 * done / total)
                return
            # Fallback: strip ANSI colors, then parse the printed percent
            pct = re.sub(r"\x1b\[[0-9;]*m", "",
                         str(d.get("_percent_str") or "0")).replace("%", "").strip()
            try:
                progress_callback(float(pct))
            except ValueError:
                pass

        opts = {
            # GUI bar is the UX — keep yt-dlp's own [download] lines and
            # [generic] chatter out of the terminal (multi-tab runs would
            # interleave into an unreadable mess). Warnings/errors still
            # print, and failures raise → shown in the error dialog.
            "quiet": True,
            "noprogress": True,
            "geo_bypass": True,
            "noplaylist": True,
            # Must match get_info(): without node + EJS the n-signature
            # challenge fails and YouTube rejects with
            # "The page needs to be reloaded."
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github"},
            "progress_hooks": [progress_hook],
            "outtmpl": os.path.join(target_dir, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
        }
        # Same conditional Firefox cookies as get_info() — keep the two
        # opts builders consistent (age-restricted login state).
        if _has_firefox_cookies():
            opts["cookiesfrombrowser"] = ("firefox",)
        # Only set when ffmpeg resolves to a real directory/file — passing
        # "" would make yt-dlp skip ffmpeg entirely and abort on merge.
        ffmpeg_path = _get_ffmpeg_path()
        if ffmpeg_path:
            opts["ffmpeg_location"] = ffmpeg_path

        if format_id == "audio_only":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                },
                {"key": "EmbedThumbnail"},
            ]
        elif format_id:
            opts["format"] = format_id
        else:
            opts["format"] = "bestvideo+bestaudio/best"

        try:
            # _ydl_run retries without cookies if extraction fails for a
            # cookie reason (profile vanished, locked DB, etc.).
            _ydl_run(opts, url, download=True)
            return {"status": "success"}
        except Exception as e:
            return {"error": f"Download failed: {str(e)}"}
