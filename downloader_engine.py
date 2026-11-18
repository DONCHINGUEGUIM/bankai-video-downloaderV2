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


# Hard stop for playlist expansion. YouTube "Mix" lists (list=RD…) generate
# entries as you scroll and effectively never end, so a download must never be
# unbounded — 500 is far beyond any real playlist a user pastes by hand.
MAX_PLAYLIST_ITEMS = 500

# Display names for the caption languages YouTube actually serves most often.
# Anything missing simply falls back to its code, so no language is hidden —
# only un-named.
_LANG_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "de": "German", "it": "Italian", "nl": "Dutch", "ru": "Russian",
    "ar": "Arabic", "hi": "Hindi", "bn": "Bengali", "ur": "Urdu",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "tr": "Turkish",
    "pl": "Polish", "uk": "Ukrainian", "vi": "Vietnamese", "th": "Thai",
    "id": "Indonesian", "ms": "Malay", "sw": "Swahili", "am": "Amharic",
    "ha": "Hausa", "yo": "Yoruba", "ig": "Igbo", "zu": "Zulu",
    "fa": "Persian", "he": "Hebrew", "el": "Greek", "ro": "Romanian",
    "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
    "cs": "Czech", "hu": "Hungarian", "bg": "Bulgarian", "sr": "Serbian",
    "hr": "Croatian", "sk": "Slovak", "ta": "Tamil", "te": "Telugu",
    "ml": "Malayalam", "kn": "Kannada", "mr": "Marathi", "gu": "Gujarati",
    "pa": "Punjabi", "ne": "Nepali", "si": "Sinhala", "km": "Khmer",
    "lo": "Lao", "my": "Burmese", "ka": "Georgian", "hy": "Armenian",
    "az": "Azerbaijani", "kk": "Kazakh", "uz": "Uzbek", "mn": "Mongolian",
    "af": "Afrikaans", "sq": "Albanian", "lt": "Lithuanian", "lv": "Latvian",
    "et": "Estonian", "sl": "Slovenian", "bs": "Bosnian", "mk": "Macedonian",
    "is": "Icelandic", "ga": "Irish", "cy": "Welsh", "mt": "Maltese",
    "gl": "Galician", "eu": "Basque", "ca": "Catalan", "eo": "Esperanto",
}


# Script subtags (zh-Hans, zh-Hant, sr-Latn …) name the writing system, not a
# country — "Chinese (Hans)" is jargon nobody reads fluently, so translate the
# ones that actually show up on YouTube.
_SCRIPT_NAMES = {"hans": "Simplified", "hant": "Traditional", "latn": "Latin",
                 "orig": "original"}
# Region subtags are ISO codes, and "Spanish (419)" is meaningless to a user.
# Only the ones YouTube actually serves are translated; the rest stay as codes.
_REGION_NAMES = {
    "419": "Latin America", "br": "Brazil", "pt": "Portugal", "us": "US",
    "gb": "UK", "cn": "China", "tw": "Taiwan", "hk": "Hong Kong",
    "mx": "Mexico", "es": "Spain", "ar": "Argentina", "in": "India",
}


def _lang_name(code):
    """Human name for a caption code ("en-US" → "English (US)").

    YouTube reports plain codes, regional variants and script variants; the
    suffix is kept because "pt-BR"/"pt-PT" and "zh-Hans"/"zh-Hant" are
    genuinely different tracks, not spelling variations.
    """
    base, _, suffix = code.partition("-")
    name = _LANG_NAMES.get(base)
    if not name:
        return code.upper()
    if not suffix:
        return name
    key = suffix.lower()
    label = _SCRIPT_NAMES.get(key) or _REGION_NAMES.get(key) or suffix
    return f"{name} ({label})"


def _subtitle_langs(info):
    """Compact list of a video's caption tracks: [{code, name, auto}].

    Manual captions win over auto-generated ones for the same code — they are
    the human-checked version — and 'live_chat' is a chat replay, not a
    subtitle, so it is dropped.

    The auto set needs filtering: YouTube returns every auto-TRANSLATED
    track as well — ~160 codes for a popular video — which would turn the
    language menu into a wall of names nobody wants. Translations carry a
    display name on their formats, the genuine auto track does not, so only
    the originals (plus whatever the video's own language points at) survive.
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    video_lang = (info.get("language") or "").split("-")[0].lower()
    # A video with manual 'en' also reports an auto 'en-orig' track holding
    # the same words; embedding both just puts two identical "English" lines
    # in the player's subtitle menu, so the manual one wins.
    manual_bases = {code.split("-")[0].lower() for code in manual}

    out = []
    for code in manual:
        if code == "live_chat":
            continue
        out.append({"code": code, "name": _lang_name(code), "auto": False})

    for code, formats in auto.items():
        if code == "live_chat" or code in manual:
            continue
        base = code.split("-")[0].lower()
        if base in manual_bases:
            continue
        translated = formats and all(f.get("name") for f in formats)
        if translated and not (video_lang and base == video_lang):
            continue
        out.append({"code": code, "name": _lang_name(code), "auto": True})
    return out


class DownloadCancelled(Exception):
    """Raised from a progress hook when the user hits Cancel.

    yt-dlp lets hook exceptions propagate out of ydl.download(), which is the
    only way to abort a transfer that is already running — checking a flag
    between videos would leave the current one downloading for minutes.
    """


def _watch_url(video_id):
    """Canonical watch URL for a bare video id (flat playlist entries)."""
    return f"https://www.youtube.com/watch?v={video_id}"


def _info_size(info):
    """Bytes for the formats an extraction selected (0 when unknown).

    A merged video+audio pick reports its parts under requested_formats and
    carries no size of its own; a single format reports on the info dict. If
    any part is unknown the sum would be a lie, so report 0 instead.
    """
    parts = info.get("requested_formats") or [info]
    total = 0
    for part in parts:
        size = part.get("filesize") or part.get("filesize_approx") or 0
        if not size:
            return 0
        total += int(size)
    return total


def _playlist_id(url):
    """The list= id if url is a PURE playlist link, else None.

    watch?v=…&list=… is a video that merely sits inside a playlist — the UI
    decides whether to expand it (its opt-in checkbox), so it is NOT treated
    as a playlist here.
    """
    match = re.search(r"[?&]list=([^&]+)", url)
    if not match or re.search(r"[?&]v=", url):
        return None
    return match.group(1)


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
    except DownloadCancelled:
        # Never retry a user cancel — that would restart the transfer
        raise
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

        # A pure playlist link gets its own cheap path: one flat entry list
        # instead of resolving every video's formats up front.
        if _playlist_id(url):
            return self._playlist_payload(url)

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

            # watch?v=…&list=… : noplaylist above deliberately collapses this
            # to the single video. Count the surrounding list so the UI can
            # offer the opt-in "download the whole playlist" checkbox.
            if "list=" in url and "v=" in url:
                info["playlist_count"] = self.playlist_count(url)

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
            # The raw caption dicts carry a URL per language per format —
            # huge, and the UI only needs code + name. Summarise first.
            info["subtitle_langs"] = _subtitle_langs(info)

            for key in list(info.keys()):
                if key not in ("title", "duration", "channel", "uploader", "thumbnail", "available_resolutions", "formats", "webpage_url", "id", "playlist_count", "subtitle_langs"):
                    del info[key]

            return info

        except Exception as e:
            return {"error": f"Analysis failed: {str(e)}"}

    def _all_subtitle_codes(self, url):
        """Explicit caption codes for "All available" ([] when there are none).

        A separate extraction is the only way to express "all the real
        tracks": yt-dlp's own 'all' would also pull every auto-translated
        track, and there is no flag to tell those apart (see
        _subtitle_langs). Only ever runs when the user picks "All available",
        so the extra second per video is opt-in.
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
            "noplaylist": True,
            "skip_download": True,
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github"},
        }
        if _has_firefox_cookies():
            opts["cookiesfrombrowser"] = ("firefox",)
        try:
            info = _ydl_run(opts, url, download=False)
        except Exception:
            return []
        return [lang["code"] for lang in _subtitle_langs(info)]

    def download(self, url, format_id=None, progress_callback=None,
                 download_path=None, should_cancel=None, subs=None):
        url = _clean_url(url)

        # Nothing to do if Cancel was pressed before this video started
        if should_cancel and should_cancel():
            return {"status": "cancelled"}

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

            The cancel check runs FIRST: raising out of a hook is the only
            way to abort a transfer that is already in flight — a flag
            checked between videos would leave the current one running.
            """
            if should_cancel and should_cancel():
                raise DownloadCancelled()
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

        # Subtitles, when the user asked for some:
        #  1. YouTube serves WebVTT → ffmpeg rewrites it as SRT (text only,
        #     no re-encode),
        #  2. the SRT is muxed into the mp4 as a soft subtitle track — also a
        #     stream copy, so it costs a second rather than a re-encode,
        #  3. already_have_subtitle keeps the .srt next to the video instead
        #     of deleting it after embedding (mp4/mkv players can turn the
        #     embedded track off, but the file is still useful elsewhere).
        if subs:
            # "All available" cannot go to yt-dlp as-is: its own 'all' also
            # means every auto-TRANSLATED track (~160 files on a popular
            # video). Resolve it to the tracks a human would expect.
            if "all" in subs:
                subs = self._all_subtitle_codes(url)
        # Resolution can come back empty (video has no captions) → skip it
        if subs:
            opts["writesubtitles"] = True
            # Also accept auto-generated captions: many videos have nothing
            # else, and yt-dlp prefers manual subs for the same language.
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = list(subs)
            opts["subtitlesformat"] = "vtt/srt/best"
            pps = opts.setdefault("postprocessors", [])
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
            # MP3 carries no subtitle stream — the .srt files stand alone
            if format_id != "audio_only":
                pps.append({"key": "FFmpegEmbedSubtitle",
                            "already_have_subtitle": True})

        try:
            # _ydl_run retries without cookies if extraction fails for a
            # cookie reason (profile vanished, locked DB, etc.).
            _ydl_run(opts, url, download=True)
            return {"status": "success"}
        except DownloadCancelled:
            return {"status": "cancelled"}
        except Exception as e:
            return {"error": f"Download failed: {str(e)}"}

    # ─── Playlists ───────────────────────────────────────────────

    def _playlist_info(self, url):
        """Flat-extract a playlist: title, count and entries — no formats.

        extract_flat='in_playlist' takes the entries straight from the
        playlist page instead of resolving every video, which is the
        difference between ~1s and minutes on a 100-video list.
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            # Must be OFF here: with noplaylist on, yt-dlp collapses
            # watch?v=…&list=… to the single video and we'd see no entries.
            "noplaylist": False,
            "playlistend": MAX_PLAYLIST_ITEMS,
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github"},
        }
        if _has_firefox_cookies():
            opts["cookiesfrombrowser"] = ("firefox",)
        return _ydl_run(opts, _clean_url(url), download=False)

    def playlist_count(self, url):
        """How many videos a playlist URL expands to (0 when it isn't one).

        Used only to label the UI's opt-in checkbox; any failure is
        non-fatal, so the checkbox just stays hidden.
        """
        try:
            info = self._playlist_info(url)
            return int(info.get("playlist_count")
                       or len(info.get("entries") or []))
        except Exception:
            return 0

    def _playlist_payload(self, url):
        """Shape a playlist extraction into the dict the GUI renders."""
        try:
            info = self._playlist_info(url)
            entries = [e for e in (info.get("entries") or []) if e]
            entries = entries[:MAX_PLAYLIST_ITEMS]
            if not entries:
                return {"error": "This playlist has no downloadable videos."}
            return {
                "type": "playlist",
                "title": info.get("title") or "Playlist",
                "count": len(entries),
                "thumbnail": info.get("thumbnail"),
                "webpage_url": url,
                "entries": [
                    {
                        "title": entry.get("title") or f"Video {i}",
                        "url": entry.get("url") or _watch_url(entry.get("id")),
                    }
                    for i, entry in enumerate(entries, start=1)
                ],
            }
        except Exception as e:
            return {"error": f"Playlist analysis failed: {e}"}

    def estimate_size(self, url, format_id=None):
        """Approximate download size in bytes for one video (0 = unknown).

        YouTube's playlist page carries no sizes at all, so this is a full
        extraction per video — the picker runs it in a small thread pool,
        never on the UI thread. format_id uses the same strings download()
        takes, so the number matches what would actually be fetched.
        """
        if format_id == "audio_only":
            fmt = "bestaudio/best"
        else:
            fmt = format_id or "bestvideo+bestaudio/best"

        opts = {
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
            "noplaylist": True,
            "skip_download": True,
            "extract_flat": False,
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github"},
            "format": fmt,
        }
        if _has_firefox_cookies():
            opts["cookiesfrombrowser"] = ("firefox",)
        try:
            return _info_size(_ydl_run(opts, _clean_url(url), download=False))
        except Exception:
            # A private/deleted/age-gated entry just shows "?" in the picker
            return 0

    def download_playlist(self, url, format_id=None, on_item=None,
                          progress_callback=None, download_path=None,
                          only=None, should_cancel=None, subs=None):
        """Download playlist videos one after another.

        Sequential on purpose: YouTube throttles parallel downloads from a
        single IP, and one-at-a-time keeps the progress bar honest. A failed
        entry is recorded and skipped — one private or deleted video must not
        abort a 200-item run.

        on_item(index, total, title) fires before each video so the GUI can
        show "4/27 — <title>"; progress_callback stays the per-video 0-100
        feed the bar already uses.

        only: set of 1-based playlist indices to fetch (the selector's ticks).
        None means the whole list. should_cancel: polled before every video
        and from inside the progress hook, so Cancel stops immediately.
        subs: caption language codes applied to every video that has them
        (['all'] takes whatever each video offers).
        """
        try:
            info = self._playlist_info(url)
        except Exception as e:
            return {"error": f"Playlist analysis failed: {e}"}

        entries = [e for e in (info.get("entries") or []) if e]
        entries = entries[:MAX_PLAYLIST_ITEMS]
        if only is not None:
            # Indices are playlist positions, so filter before renumbering
            entries = [e for i, e in enumerate(entries, start=1) if i in only]
        total = len(entries)
        if not total:
            return {"error": "No videos selected."}

        downloaded, failed, cancelled = [], [], False
        for i, entry in enumerate(entries, start=1):
            if should_cancel and should_cancel():
                cancelled = True
                break
            title = entry.get("title") or f"Video {i}"
            video_url = entry.get("url") or _watch_url(entry.get("id"))
            if on_item:
                on_item(i, total, title)
            res = self.download(
                video_url, format_id=format_id,
                progress_callback=progress_callback,
                download_path=download_path,
                should_cancel=should_cancel,
                subs=subs,
            )
            if res.get("status") == "cancelled":
                cancelled = True
                break
            if "error" in res:
                failed.append({"title": title, "error": res["error"]})
            else:
                downloaded.append({"title": title, "url": video_url})

        return {
            "status": "success",
            "cancelled": cancelled,
            "total": total,
            "downloaded": downloaded,
            "failed": failed,
            "folder": download_path or self.download_dir,
        }
