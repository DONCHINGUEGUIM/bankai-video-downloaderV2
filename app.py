import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox, filedialog
from PIL import Image, ImageDraw, ImageTk
import threading
import requests
from io import BytesIO
import json
import os
import re
import sys
import subprocess
from downloader_engine import DownloaderEngine, _lang_name
from download_tracker import DownloadTracker, HISTORY_DIR


def _open_path(path):
    # Redirect child stdio: file managers pull in Qt/NSS/Tracker/PipeWire
    # which otherwise inherit our terminal and flood it with warnings.
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _thread(fn):
    threading.Thread(target=fn, daemon=True).start()


# ─── Right-click edit menu ───────────────────────────────────────
def _attach_context_menu(ctk_entry):
    """Give a CTkEntry a working right-click Cut / Copy / Paste menu.

    Tk ships no context menu of its own: a right click in an Entry does
    nothing on Windows and only moves the caret on X11, so users who expect
    right-click-paste get silence. We build one tk.Menu and pop it up at the
    pointer.

    CTkEntry is a frame wrapping a real tkinter.Entry (._entry) — the menu
    must drive that inner widget, which is where the click is bound.
    """
    inner = getattr(ctk_entry, "_entry", ctk_entry)

    # Same palette as the rest of the UI — Tk's default menu is light grey
    # and looks pasted-on over the dark window.
    menu = tk.Menu(
        ctk_entry, tearoff=0,
        bg=Theme.surface, fg=Theme.text,
        activebackground=Theme.cyan, activeforeground="#000000",
        bd=0, relief="flat", activeborderwidth=0,
    )
    # Actions talk to the clipboard directly instead of relying on Tk's
    # <<Cut>> / <<Copy>> / <<Paste>> virtual events — those silently do
    # nothing when the widget isn't the focus owner, which is exactly the
    # state a right-click leaves the field in.
    def _placeholder_active():
        # CTkEntry fakes its placeholder by putting REAL text in the Entry
        # and flagging it — every action below has to ignore that text.
        return getattr(ctk_entry, "_placeholder_text_active", False)

    def _selected():
        if _placeholder_active():
            return ""  # visible text is only the placeholder
        try:
            return inner.selection_get()
        except Exception:
            return ""  # nothing selected

    def do_cut():
        text = _selected()
        if text:
            ctk_entry.clipboard_clear()
            ctk_entry.clipboard_append(text)
            inner.delete("sel.first", "sel.last")

    def do_copy():
        text = _selected()
        if text:
            ctk_entry.clipboard_clear()
            ctk_entry.clipboard_append(text)

    def do_paste():
        try:
            text = ctk_entry.clipboard_get()
        except Exception:
            return  # empty clipboard → nothing to paste
        if _placeholder_active():
            # Clearing via CTkEntry also unsets its flag; writing straight to
            # the inner Entry would leave get() returning "".
            ctk_entry._deactivate_placeholder()
        elif inner.selection_present():
            # Pasting over a selection replaces it, like every other editor
            inner.delete("sel.first", "sel.last")
        inner.insert("insert", text)

    def do_select_all():
        if _placeholder_active():
            return  # nothing real to select
        inner.selection_range(0, "end")
        inner.icursor("end")

    menu.add_command(label="Cut", command=do_cut)
    menu.add_command(label="Copy", command=do_copy)
    menu.add_command(label="Paste", command=do_paste)
    menu.add_separator()
    menu.add_command(label="Select All", command=do_select_all)

    def popup(event):
        inner.focus_set()  # paste must land here, not in the previous focus
        # tk_popup() posts the menu AND installs the grab that makes it
        # modal — item clicks and dismiss-on-outside-click both depend on
        # it. Releasing that grab right after the call (the widely
        # copy-pasted try/finally idiom) breaks exactly those two things:
        # the menu stays open and clicking an item does nothing.
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    # CTkEntry.bind() forwards to the inner Entry, so binding the inner
    # widget alone covers both — binding both would pop the menu twice.
    # Bind the RELEASE, not the press. Opening on <Button-3> means the
    # button-release that always follows lands on the freshly posted menu,
    # and Tk reads that as a click outside → the menu closes instantly.
    # That is why the menu only survived while the button stayed held.
    inner.bind("<ButtonRelease-3>", popup)          # Windows / X11 right-click
    inner.bind("<ButtonRelease-2>", popup)          # X11 middle-click habit
    inner.bind("<Control-ButtonRelease-1>", popup)  # macOS right-click
    # Keep a reference on the widget: a menu owned only by these closures
    # can be garbage-collected while it is posted.
    ctk_entry._context_menu = menu
    return menu


# ─── Settings (shared download folder, persisted across launches) ─
# Stored next to download_history.json so all app data lives in one place.
SETTINGS_FILE = os.path.join(HISTORY_DIR, "settings.json")
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads")


# ─── Credits (footer line at the bottom of the window) ───────────
# Kept as constants so rebranding is a one-line change — the footer label
# is built from these in App._build().
APP_AUTHOR = "Herun"
APP_YEAR = "2026"
APP_CREDIT = f"Made by {APP_AUTHOR}  ·  © {APP_YEAR} {APP_AUTHOR}. All rights reserved."

# Quality buckets offered on a playlist card. Per-video sizes would require
# resolving every entry's formats up front, so playlists trade that detail
# for a fast analysis.
PLAYLIST_QUALITIES = ["Best available", "1080p", "720p", "480p", "Audio Only (MP3)"]

# Languages offered on a playlist card. A flat playlist analysis carries no
# caption metadata, so the list cannot be discovered per video — these are
# simply the ones people ask for; yt-dlp skips videos that lack them.
COMMON_SUBTITLE_LANGS = ("en", "fr", "es", "pt", "de", "ar", "hi",
                         "zh-Hans", "ru", "ja", "ko", "it")


def _load_download_folder():
    """Read the saved download folder; fall back to ~/Downloads."""
    try:
        with open(SETTINGS_FILE) as f:
            path = json.load(f).get("download_folder", "")
        # Only accept an existing directory — ignore stale/broken values
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return DEFAULT_FOLDER


def _save_download_folder(path):
    """Persist the chosen download folder to settings.json."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    data = {}
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["download_folder"] = path
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _pretty_path(path):
    """Show home-relative path (~) and middle-truncate long ones."""
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) > 72:
        path = "…" + path[-71:]
    return path


# ─── Theme ────────────────────────────────────────────────────────
class Theme:
    bg = "#06060E"
    surface = "#0C0C18"
    card = "#101020"
    border = "#1C1C34"
    cyan = "#00F0FF"
    purple = "#8B5CF6"
    pink = "#FF2D78"
    green = "#10B981"
    text = "#E8E8FF"
    muted = "#5C5C80"
    glass = "#141428"
    title_bg = "#08081A"


# ─── Paste icon (drawn, so it renders on every platform) ─────────
def _clipboard_icon(size=20, color=None):
    """Draw a small clipboard glyph for the PASTE button.

    Drawn with PIL instead of the 📋 emoji: Tk does no font fallback on
    Linux, so emoji come out as tofu boxes there while they work on Windows.
    A drawn image looks the same everywhere and takes its colour from the
    theme.
    """
    color = color or Theme.text
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # "#RRGGBB" → RGB
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Board, clip on the top edge, then two text lines inside
    d.rounded_rectangle([2, 4, size - 3, size - 2], radius=2, outline=rgb, width=2)
    d.rounded_rectangle([size // 2 - 4, 1, size // 2 + 4, 6], radius=1,
                        outline=rgb, width=2)
    d.line([5, 11, size - 6, 11], fill=rgb, width=2)
    d.line([5, 15, size - 8, 15], fill=rgb, width=2)
    return img


def _erase_icon(size=20, color=None):
    """Draw a small ✕ glyph for the CLEAR button.

    Same reason as the clipboard icon: Tk does no font fallback on Linux, so
    an emoji or a "✕" character renders as a tofu box there while working on
    Windows. A drawn image looks identical on every platform and follows the
    theme colour.
    """
    color = color or Theme.text
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # "#RRGGBB" → RGB
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 5
    # Two diagonals; dots at the ends fake round line caps (PIL has none)
    d.line([pad, pad, size - pad, size - pad], fill=rgb, width=2)
    d.line([size - pad, pad, pad, size - pad], fill=rgb, width=2)
    for x, y in ((pad, pad), (size - pad, pad),
                 (pad, size - pad), (size - pad, size - pad)):
        d.ellipse([x - 1, y - 1, x + 1, y + 1], fill=rgb)
    return img


def _fmt_size(nbytes):
    """Human-readable size for a byte count; 0/None means "unknown"."""
    if not nbytes:
        return "?"
    mb = nbytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    if mb >= 10:
        return f"{mb:.0f} MB"
    return f"{mb:.1f} MB"


ctk.set_appearance_mode("Dark")


# ─── Single Download Tab ──────────────────────────────────────────
class DownloadTab(ctk.CTkFrame):
    def __init__(self, parent, engine, tracker, app):
        super().__init__(parent, fg_color=Theme.bg)
        self.engine = engine
        self.tracker = tracker
        self.app = app
        self.info = None
        self.current_url = ""
        self.is_playlist = False   # card currently shows a playlist
        self.playlist_count = 0    # videos behind the current link
        self.playlist_selection = None  # set of ticks from the selector
        self._entries = None  # cached playlist entries (None = not loaded yet)
        self._size_cache = {}  # (url, format) -> bytes, filled by the picker
        self._size_token = None  # abandons a superseded size probe
        self._sub_map = {"None": None}  # subtitle menu label -> language code
        self._cancel_evt = threading.Event()  # set by the Cancel button
        self._thumb = None   # CTkImage currently shown (keeps PIL alive for GC)
        self._pil_thumb = None  # original PIL image, kept for re-fits on resize

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Scrollable host: the content (340px thumbnail + title + quality
        # selector + Download button + progress bar) is taller than a short
        # window (min height 620) and gets even taller under Windows display
        # scaling — previously it was simply clipped with no way to reach the
        # Download button. CTkScrollableFrame makes the mouse wheel work on
        # every platform (it binds <Button-4/5>/<MouseWheel> globally — the
        # same mechanism the History tab already uses).
        wrap = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            corner_radius=0, border_width=0,
            scrollbar_fg_color=Theme.bg,
            scrollbar_button_color=Theme.glass,
            scrollbar_button_hover_color=Theme.cyan
        )
        # nsew: stretch with the window so preview/thumbnails get full width.
        # grid() is forwarded by CTkScrollableFrame to its outer container.
        wrap.grid(row=0, column=0, sticky="nsew", padx=48)
        # CTkScrollableFrame does NOT forward grid_columnconfigure /
        # grid_rowconfigure — they apply to its inner canvas frame, which is
        # exactly where our children live. Correct as written.
        wrap.grid_columnconfigure(0, weight=1)
        # Row 0: fixed top spacer (no weight → no blank gap).
        # Row 3 (preview): takes ALL vertical slack so sticky="nsew" works.
        wrap.grid_rowconfigure(0, weight=0)
        wrap.grid_rowconfigure(3, weight=1)
        self.wrap = wrap
        # CTk grids its scrollbar at creation → initial state = shown
        self._sb_shown = True
        # Content visibility tracked in Python: Tk's winfo_reqheight() turns
        # stale (stays at the OLD, taller value) after grid_remove() on this
        # build — verified by measurement (734 kept after hiding 400px of
        # content) — so the geometry sync must never read the live requested
        # height while the content is hidden.
        self._content_shown = False
        # Natural height of the EMPTY state (input row + folder row + preview
        # default), captured on the very first sync — before anything has ever
        # been shown/hidden, so the value is guaranteed fresh. It is
        # width-independent (no wrapping/thumbnail inside the empty preview),
        # so caching it once is safe for the tab's whole lifetime.
        self._empty_req = None
        # Window resized → re-decide stretch vs. scroll (see the sync method).
        # add="+" is ESSENTIAL: a plain bind() REPLACES CTk's own <Configure>
        # handler that syncs the inner frame's width to the canvas — without
        # it the content squeezes down to its requested width (290px) inside
        # a full-width canvas.
        wrap._parent_canvas.bind("<Configure>", self._sync_scroll_geometry,
                                 add="+")

        # ── Input line ──────────────────────────────────────────
        line = ctk.CTkFrame(wrap, fg_color=Theme.surface, corner_radius=14, border_width=0)
        line.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        line.grid_columnconfigure(0, weight=1)

        inp = ctk.CTkFrame(line, fg_color="transparent")
        inp.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        inp.grid_columnconfigure(0, weight=1)

        self.url_input = ctk.CTkEntry(
            inp,
            placeholder_text="Paste your YouTube link here ...",
            border_width=0, fg_color="transparent",
            font=ctk.CTkFont(size=16), text_color=Theme.text
        )
        self.url_input.grid(row=0, column=0, padx=(18, 8), pady=10, sticky="ew")
        # Right-click menu on the link field (Cut / Copy / Paste / Select All)
        _attach_context_menu(self.url_input)

        # CLEAR — wipes the link field and returns the card to its empty
        # state. Sits right against the field, where an "x" is expected.
        self._clear_img = ctk.CTkImage(
            light_image=_erase_icon(), dark_image=_erase_icon(),
            size=(20, 20)
        )
        self.btn_clear = ctk.CTkButton(
            inp, text="", image=self._clear_img, width=44, height=42,
            fg_color="transparent", hover_color=Theme.pink,
            corner_radius=10, command=self._clear_url
        )
        self.btn_clear.grid(row=0, column=1, padx=(0, 6), pady=6)

        # PASTE — icon-only button between the field and ANALYZE. The CTkImage
        # is kept on self so the bitmap can't be dropped after layout.
        self._paste_img = ctk.CTkImage(
            light_image=_clipboard_icon(), dark_image=_clipboard_icon(),
            size=(20, 20)
        )
        self.btn_paste = ctk.CTkButton(
            inp, text="", image=self._paste_img, width=52, height=42,
            fg_color=Theme.glass, hover_color=Theme.purple,
            corner_radius=10, command=self._paste_url
        )
        self.btn_paste.grid(row=0, column=2, padx=(0, 6), pady=6)

        self.btn_go = ctk.CTkButton(
            inp, text="ANALYZE", width=110, height=42,
            fg_color=Theme.cyan, hover_color="#00CCDD",
            text_color="#000", corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._analyze
        )
        self.btn_go.grid(row=0, column=3, padx=(0, 6), pady=6)

        # ── Folder chooser row (shared across all tabs) ─────────
        frow = ctk.CTkFrame(wrap, fg_color=Theme.surface, corner_radius=14, border_width=0)
        frow.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        frow.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            frow, text="SAVE TO",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=Theme.muted
        ).grid(row=0, column=0, padx=(16, 10), pady=8)

        # Shows the current shared folder; updated via _set_folder_label
        self.folder_lbl = ctk.CTkLabel(
            frow, text="",
            font=ctk.CTkFont(size=12), text_color=Theme.text,
            anchor="w"
        )
        self.folder_lbl.grid(row=0, column=1, sticky="ew", pady=8)

        self.btn_browse = ctk.CTkButton(
            frow, text="Browse…", width=90, height=28,
            fg_color=Theme.glass, hover_color=Theme.cyan,
            text_color=Theme.text, corner_radius=8,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._pick_folder
        )
        self.btn_browse.grid(row=0, column=2, padx=(0, 10), pady=6)

        # Show the folder this tab will save into (shared app-level value)
        self._set_folder_label(self.app.download_folder)

        # ── Preview ─────────────────────────────────────────────
        self.preview = ctk.CTkFrame(wrap, fg_color=Theme.surface, corner_radius=16, border_width=0)
        self.preview.grid(row=3, column=0, sticky="nsew")
        self.preview.grid_columnconfigure(0, weight=1)
        self.preview.grid_rowconfigure(0, weight=1)

        # Empty state
        self.empty = ctk.CTkLabel(
            self.preview,
            text="Paste a link and press \u2192",
            font=ctk.CTkFont(size=13),
            text_color=Theme.muted
        )
        self.empty.place(relx=0.5, rely=0.4, anchor="center")

        # Content (hidden)
        self.content = ctk.CTkFrame(self.preview, fg_color="transparent")
        self.content.grid_columnconfigure(0, weight=1)

        # Thumbnail
        tw = ctk.CTkFrame(self.content, fg_color="#000", corner_radius=12)
        tw.grid(row=0, column=0, padx=32, pady=(20, 2), sticky="ew")
        tw.grid_columnconfigure(0, weight=1)

        self.thumb = ctk.CTkLabel(tw, text="", width=640, height=340, corner_radius=12, fg_color="#000")
        self.thumb.grid(row=0, column=0)

        # Title
        self.v_title = ctk.CTkLabel(
            self.content, text="",
            font=ctk.CTkFont(size=18, weight="bold"),
            wraplength=680, text_color=Theme.text
        )
        self.v_title.grid(row=1, column=0, padx=32, pady=(12, 2), sticky="w")

        # Meta line
        self.v_meta = ctk.CTkLabel(
            self.content, text="",
            font=ctk.CTkFont(size=11), text_color=Theme.muted
        )
        self.v_meta.grid(row=2, column=0, padx=32, pady=(0, 16), sticky="w")

        # Actions row
        act = ctk.CTkFrame(self.content, fg_color="transparent")
        act.grid(row=3, column=0, padx=32, pady=(0, 18), sticky="ew")
        act.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            act, text="Quality",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=Theme.muted
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        sel_row = ctk.CTkFrame(act, fg_color="transparent")
        sel_row.grid(row=1, column=0, sticky="ew")
        sel_row.grid_columnconfigure(0, weight=1)

        self.q_menu = ctk.CTkOptionMenu(
            sel_row, values=["Select"],
            width=220, height=36,
            fg_color=Theme.glass, button_color=Theme.cyan,
            button_hover_color=Theme.purple,
            dropdown_fg_color=Theme.card,
            dropdown_hover_color=Theme.glass,
            text_color=Theme.text, corner_radius=8,
            font=ctk.CTkFont(size=12),
            command=self._on_quality_pick
        )
        self.q_menu.grid(row=0, column=0, sticky="w")

        self.btn_dl = ctk.CTkButton(
            sel_row, text="Download",
            fg_color=Theme.pink, hover_color="#CC2460",
            width=140, height=36, corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._download
        )
        self.btn_dl.grid(row=0, column=1, padx=(12, 0))

        # Playlist only: opens the checkbox list so the user can pick which
        # videos to fetch instead of taking the whole list
        self.btn_select = ctk.CTkButton(
            sel_row, text="Select…", width=90, height=36,
            fg_color=Theme.glass, hover_color=Theme.cyan,
            text_color=Theme.text, corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_selector
        )
        self.btn_select.grid(row=0, column=2, padx=(8, 0))
        self.btn_select.grid_remove()

        # Cancel — only while something is running (see _start_download)
        self.btn_cancel = ctk.CTkButton(
            sel_row, text="Cancel", width=90, height=36,
            fg_color="transparent", hover_color=Theme.pink,
            text_color=Theme.muted, corner_radius=8,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._cancel_download
        )
        self.btn_cancel.grid(row=0, column=3, padx=(8, 0))
        self.btn_cancel.grid_remove()

        # Opt-in for watch?v=…&list=… links. Unchecked — the default — keeps
        # today's "just this video" behaviour; hidden unless the link really
        # sits inside a playlist.
        self.chk_playlist = ctk.CTkCheckBox(
            act, text="Download the whole playlist instead",
            font=ctk.CTkFont(size=11), text_color=Theme.muted,
            fg_color=Theme.cyan, hover_color=Theme.purple,
            border_width=1, border_color=Theme.border,
            checkbox_width=16, checkbox_height=16, corner_radius=4,
            command=self._on_playlist_toggle
        )
        self.chk_playlist.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.chk_playlist.grid_remove()

        # ── Subtitles row ───────────────────────────────────────
        # A video card lists the captions that video really has; a playlist
        # card offers the common languages plus "All available", because a
        # flat playlist analysis carries no caption metadata at all.
        sub_row = ctk.CTkFrame(act, fg_color="transparent")
        sub_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        sub_row.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            sub_row, text="Subtitles",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=Theme.muted
        ).grid(row=0, column=0, sticky="w")

        self.sub_menu = ctk.CTkOptionMenu(
            sub_row, values=["None"], width=220, height=32,
            fg_color=Theme.glass, button_color=Theme.cyan,
            button_hover_color=Theme.purple,
            dropdown_fg_color=Theme.card,
            dropdown_hover_color=Theme.glass,
            text_color=Theme.text, corner_radius=8,
            font=ctk.CTkFont(size=11),
            command=self._on_subtitle_pick
        )
        self.sub_menu.grid(row=0, column=1, padx=(12, 0), sticky="w")
        self.sub_menu.configure(state="disabled")

        # Says what the choice actually does to the downloaded file
        self.sub_hint = ctk.CTkLabel(
            sub_row, text="",
            font=ctk.CTkFont(size=9), text_color=Theme.muted, anchor="w"
        )
        self.sub_hint.grid(row=0, column=2, padx=(12, 0), sticky="w")

        # Progress bar
        self.prog_frame = ctk.CTkFrame(self.content, fg_color="transparent", height=4)
        self.prog_frame.grid(row=4, column=0, padx=32, pady=(0, 20), sticky="ew")
        self.prog_frame.grid_columnconfigure(0, weight=1)
        self.prog_frame.grid_propagate(False)

        self.prog = ctk.CTkProgressBar(
            self.prog_frame, height=4,
            fg_color=Theme.glass, progress_color=Theme.cyan,
            corner_radius=2, border_width=0
        )
        self.prog.grid(row=0, column=0, sticky="ew")
        self.prog.set(0)

        self.prog_txt = ctk.CTkLabel(
            self.content, text="",
            font=ctk.CTkFont(size=10), text_color=Theme.muted
        )
        self.prog_txt.grid(row=5, column=0, padx=32, pady=(0, 20), sticky="w")

        self.prog_frame.grid_remove()
        self.prog_txt.grid_remove()
        self.content.grid_remove()

        self.url_input.bind("<Return>", lambda e: self._analyze())

    # ─── Scroll geometry (stretch when it fits, scroll when tall) ─

    def _sync_scroll_geometry(self, event=None):
        """Size the scroll host's inner frame to max(content, visible area).

        CTk's vertical CTkScrollableFrame only syncs the inner frame's WIDTH
        to its canvas (see its _fit_frame_dimensions_to_canvas), so by default
        the inner frame keeps its *requested* height. That means:
          - short window: content overflows and must become scrollable;
          - tall window: content must STRETCH (the preview panel has to fill
            the tab, like the old non-scrolling layout).

        We therefore force:  inner_height = max(requested, canvas_height)
          - content taller than the window -> the canvas scrollregion grows ->
            the mouse wheel scrolls the tab (button/progress always reachable);
          - content shorter -> the inner frame fills the canvas, grid row 3
            (preview) absorbs the slack -> identical look to before.

        Also hides the scrollbar while everything fits — same idea as the
        tab strip's _sync_tab_scrollbar.
        """
        canvas = self.wrap._parent_canvas
        if canvas.winfo_height() <= 1:
            return  # window not realised yet — Configure fires again later
        self.update_idletasks()            # ensure requested sizes are fresh
        # Natural height of the content — shrink-proof (see _content_shown):
        #   visible → live value: gridding content back in and resizing its
        #             children both refresh winfo_reqheight() correctly
        #             (measured 328 → 734 when the content appeared);
        #   hidden  → the cached empty height: reading the live value here
        #             would return the STALE pre-hide height (~734) and keep
        #             a phantom scrollbar + dead scroll space on an empty tab.
        if self._content_shown:
            req = self.wrap.winfo_reqheight()
        else:
            if self._empty_req is None:  # first pass, still pristine
                self._empty_req = self.wrap.winfo_reqheight()
            req = self._empty_req
        avail = canvas.winfo_height()      # height of the visible area
        canvas.itemconfigure(self.wrap._create_window_id,
                             height=max(req, avail))
        bbox = canvas.bbox("all")
        if bbox:
            canvas.configure(scrollregion=bbox)
        # Toggle the scrollbar ONLY on state change — re-gridding inside a
        # <Configure> handler would resize the canvas and loop forever.
        # State is tracked in a flag rather than winfo_ismapped()/grid_info()
        # because Tk processes map/unmap asynchronously: those queries are
        # stale within the same call stack and would flip the bar every pass.
        overflow = req > avail + 1
        if overflow and not self._sb_shown:
            self.wrap._scrollbar.grid()        # restore CTk's own grid options
            self._sb_shown = True
        elif not overflow and self._sb_shown:
            self.wrap._scrollbar.grid_remove() # fits → no bar needed
            self._sb_shown = False

    # ─── UI helpers ──────────────────────────────────────────────

    def _apply_resize(self):
        """Re-fit title wrap width + thumbnail to this tab's current size.

        Called by the app's debounced window-resize fan-out (cheap when
        nothing actually changed).
        """
        w = self.winfo_width()
        if w <= 1:  # not yet realised
            return
        self.v_title.configure(wraplength=max(300, w - 220))
        self._refit_thumb()
        # Title/thumbnail sizes changed → content height changed → re-decide
        # stretch vs. scroll (cheap no-op when nothing moved).
        self._sync_scroll_geometry()

    def _refit_thumb(self):
        """Scale the stored PIL thumbnail into the available content box.

        Keeps aspect ratio, caps the box so it never dominates, and swaps
        a fresh CTkImage onto the label (rebuilding it is required — CTk
        does not re-scale an existing image on resize).
        """
        if self._pil_thumb is None:
            return
        pw, ph = self._pil_thumb.size
        if pw <= 0 or ph <= 0:
            return
        w = self.winfo_width()
        # Box tracks tab width, clamped to sane bounds
        box_w = max(300, min(640, w - 200))
        box_h = max(200, min(380, int(box_w * ph / pw)))
        img = self._pil_thumb.copy()
        img.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
        self._thumb = ctk.CTkImage(
            light_image=img, dark_image=img,
            size=(img.width, img.height)
        )
        self.thumb.configure(image=self._thumb, width=box_w, height=box_h)
        # Thumbnail height changed → refresh scrollregion / stretch decision
        self._sync_scroll_geometry()

    def _set_folder_label(self, path):
        """Refresh this tab's 'save to' label after the shared folder changes."""
        self.folder_lbl.configure(text=_pretty_path(path))

    def _pick_folder(self):
        """Open the native folder picker and apply it app-wide (all tabs)."""
        chosen = filedialog.askdirectory(
            title="Choose download folder",
            initialdir=self.app.download_folder
        )
        if chosen:  # empty string = user cancelled
            self.app.set_download_folder(chosen)

    def _show_content(self):
        self.empty.place_forget()
        self.content.grid()
        self._content_shown = True  # from here on the sync reads live heights
        self._sync_scroll_geometry()  # content appeared → maybe now scrollable

    def _hide_content(self):
        self.content.grid_remove()
        # Flag BEFORE the sync: winfo_reqheight() is already stale-high the
        # moment the content leaves the grid, so the sync must use the cache.
        self._content_shown = False
        self.empty.place(relx=0.5, rely=0.4, anchor="center")
        self._sync_scroll_geometry()  # content gone → back to stretched layout

    def _show_progress(self):
        self.prog_frame.grid()
        self.prog_txt.grid()
        self._sync_scroll_geometry()  # progress row added at the bottom

    def _hide_progress(self):
        self.prog_frame.grid_remove()
        self.prog_txt.grid_remove()
        self.prog.set(0)
        # Best-effort refresh: removing grid children can leave the parents'
        # winfo_reqheight() stale-HIGH (the ~72px the progress rows occupied),
        # which would keep a strip of dead scroll space under the content.
        # A configure-driven size change DOES travel up the chain
        # (content → preview → wrap), so briefly rewrapping the title to
        # 1px forces Tk to recompute the requested heights bottom-up — first
        # with the fat title, then again with the restored wraplength, i.e.
        # with the progress rows already gone. Harmless if the chain had
        # already refreshed itself; cget() hands back the exact raw value
        # (scaling is re-applied internally), so the restore cannot drift.
        if self._content_shown:
            wl = self.v_title.cget("wraplength")
            if wl:
                self.v_title.configure(wraplength=1)
                self.update_idletasks()
                self.v_title.configure(wraplength=wl)
                self.update_idletasks()
        self._sync_scroll_geometry()  # progress row removed

    def _playlist_mode(self):
        """True when the next Download click should fetch a whole playlist."""
        return self.is_playlist or bool(self.chk_playlist.get())

    def _idle(self):
        self.btn_go.configure(state="normal", text="\u2192")
        self.btn_dl.configure(state="normal", text=self._scope_label())
        self.btn_select.configure(state="normal")
        self.chk_playlist.configure(state="normal")
        # Only re-enable the caption menu when the card actually has choices
        self.sub_menu.configure(state="normal" if len(self._sub_map) > 1
                                else "disabled")
        self.btn_cancel.grid_remove()
        self._hide_progress()

    def _scope_label(self):
        """Download-button text for the current scope.

        "Download all N" when the whole list is in play, "Download N selected"
        once the picker narrowed it down, plain "Download" for a single video.
        """
        if self.playlist_selection is not None:
            return f"Download {len(self.playlist_selection)} selected"
        if self._playlist_mode():
            return f"Download all {self.playlist_count}"
        return "Download"

    # ─── Analysis ────────────────────────────────────────────────

    def _paste_url(self):
        """Fill the link field from the clipboard.

        A paste into a single-URL field means "use this link", so any
        existing text is replaced rather than appended — otherwise clicking
        PASTE twice would leave two links glued together.
        """
        try:
            text = self.clipboard_get()
        except Exception:
            return  # Tk raises TclError when the clipboard is empty
        self.url_input.delete(0, "end")
        self.url_input.insert(0, text.strip())
        self.url_input.focus()

    def _clear_url(self):
        """Wipe the link field and put the card back in its empty state.

        CTkEntry stores its placeholder as real text, so delete() is safe
        either way: if the placeholder is showing, the widget immediately
        puts it back (the entry is not focused at that point).
        """
        self.url_input.delete(0, "end")

        # A running download owns the card — wipe the text, leave the job be
        if self.btn_cancel.grid_info():
            self.url_input.focus()
            return

        # Forget everything the cleared link produced, so a stale card can't
        # be downloaded by accident after the field is empty.
        self.current_url = ""
        self.info = None
        self.is_playlist = False
        self.playlist_count = 0
        self.playlist_selection = None
        self._entries = None
        self._size_token = None  # stop any size probe still running
        self.chk_playlist.deselect()
        self.chk_playlist.grid_remove()
        self.btn_select.grid_remove()
        self.q_menu.set("Select")
        self._set_subtitle_menu([])  # nothing analysed → no caption choices
        self._hide_content()
        self._idle()
        self.url_input.focus()

    def _analyze(self):
        url = self.url_input.get().strip()
        if not url:
            return
        self.current_url = url
        # A new link invalidates the previous playlist's cached entries
        self._entries = None
        self.playlist_selection = None
        self._size_token = None  # abandon any size probe for the old link
        self.btn_go.configure(state="disabled", text="...")
        self._hide_content()
        self._hide_progress()
        _thread(self._do_analyze)

    def _do_analyze(self):
        info = self.engine.get_info(self.current_url)
        self.after(0, lambda: self._on_info(info))

    def _on_info(self, info):
        if "error" in info:
            messagebox.showerror("Error", info["error"])
            self._idle()
            return

        self.info = info

        # Playlist link → its own card. Nothing downloads until the user
        # clicks "Download all", so analysis stays harmless.
        if info.get("type") == "playlist":
            self._show_playlist_info(info)
            return

        self.is_playlist = False
        self._show_content()

        self.v_title.configure(text=info.get("title", "?"))

        dur = info.get("duration", 0)
        m, s = divmod(int(dur), 60)
        author = info.get("channel") or info.get("uploader") or "?"
        self.v_meta.configure(text=f"{author}  \u2022  {m}:{s:02d}")

        fmts = info.get("available_resolutions", [])
        if fmts:
            self.q_menu.configure(values=fmts)
            self.q_menu.set(fmts[0])

        self._sync_playlist_checkbox(info)
        # Caption languages this video really offers (manual + auto)
        self._set_subtitle_menu(info.get("subtitle_langs") or [])

        thumb = info.get("thumbnail")
        if thumb:
            _thread(lambda: self._load_thumb(thumb))

        # Title/wrap depend on the now-realised content — re-fit immediately
        self._apply_resize()
        self._idle()

    # ─── Playlists ───────────────────────────────────────────────

    def _show_playlist_info(self, info):
        """Render a playlist card: title, video count, fixed quality buckets."""
        self.is_playlist = True
        self.playlist_count = int(info.get("count") or 0)
        # New link → no subset chosen yet, so the button means "the whole list"
        self.playlist_selection = None
        # The entries came with the analysis — keep them so the picker opens
        # instantly instead of re-reading the playlist
        self._entries = list(info.get("entries") or [])
        # The playlist card is already the opt-in — the checkbox has no job
        self.chk_playlist.deselect()
        self.chk_playlist.grid_remove()
        # A playlist is the only place a picker makes sense
        self.btn_select.grid()
        # Captions differ per video here, so offer the common set
        self._set_playlist_subtitles()

        self._show_content()
        self.v_title.configure(text=info.get("title", "Playlist"))
        self.v_meta.configure(
            text=f"PLAYLIST  \u2022  {self.playlist_count} videos")

        # Sizes are unknown without resolving every entry, so offer heights
        self.q_menu.configure(values=PLAYLIST_QUALITIES)
        self.q_menu.set(PLAYLIST_QUALITIES[0])

        thumb = info.get("thumbnail")
        if thumb:
            _thread(lambda: self._load_thumb(thumb))

        self._apply_resize()
        self._idle()

    def _sync_playlist_checkbox(self, info):
        """Show the opt-in checkbox only for watch?v=…&list=… links."""
        count = int(info.get("playlist_count") or 0)
        self.chk_playlist.deselect()
        # Single-video card: no picker, and any old subset is meaningless now
        self.playlist_selection = None
        self._entries = None
        self.btn_select.grid_remove()
        if count > 1:
            self.playlist_count = count
            self.chk_playlist.configure(
                text=f"Download the whole playlist instead ({count} videos)")
            self.chk_playlist.grid()
        else:
            self.playlist_count = 0
            self.chk_playlist.grid_remove()

    def _on_playlist_toggle(self):
        """Checkbox flipped → scope changes, so does the picker's availability.

        Ticking the box turns the card into "the whole playlist", which is
        exactly when a video subset becomes worth choosing — so Select…
        appears with it and disappears when it is unticked.
        """
        if self.chk_playlist.get():
            self.playlist_selection = None
            self._entries = None  # not read yet — the picker loads them
            self.btn_select.grid()
            # Whole-list mode → captions are no longer this video's alone
            self._set_playlist_subtitles()
        else:
            # Back to "just this video": a playlist subset no longer applies
            self.playlist_selection = None
            self._entries = None
            self.btn_select.grid_remove()
            self._set_subtitle_menu(
                (self.info or {}).get("subtitle_langs") or [])
        self.btn_dl.configure(text=self._scope_label())

    # ─── Subtitles ───────────────────────────────────────────────

    def _set_subtitle_menu(self, langs):
        """Fill the SUBTITLES menu with the captions a video actually has.

        "None" is always first, so the default stays "no subtitles" and
        nothing changes for anyone who never touches this. Auto-generated
        tracks are labelled, because YouTube's speech recognition is a very
        different product from a hand-written subtitle.
        """
        self._sub_map = {"None": None}
        values = ["None"]
        for lang in langs:
            # "English (en)", but "Chinese (Simplified) · zh-Hans" — a name
            # that already carries a qualifier does not get a second bracket.
            if lang["name"].endswith(")"):
                label = f"{lang['name']} \u00b7 {lang['code']}"
            else:
                label = f"{lang['name']} ({lang['code']})"
            if lang.get("auto"):
                label += "  \u2013 auto"
            self._sub_map[label] = lang["code"]
            values.append(label)
        if langs:
            self._sub_map["All available"] = "all"
            values.append("All available")
        self.sub_menu.configure(values=values,
                                state="normal" if langs else "disabled")
        self.sub_menu.set("None")
        self._on_subtitle_pick()

    def _set_playlist_subtitles(self):
        """Common languages + "All available" for playlist downloads.

        A flat playlist analysis carries no caption metadata, so nothing can
        be listed per video; yt-dlp simply skips the entries that do not have
        the chosen language.
        """
        self._set_subtitle_menu([
            {"code": code, "name": _lang_name(code), "auto": False}
            for code in COMMON_SUBTITLE_LANGS
        ])

    def _chosen_subs(self):
        """Caption codes for the next download ([] = none)."""
        code = self._sub_map.get(self.sub_menu.get())
        return [code] if code else []

    def _on_subtitle_pick(self, _value=None):
        """Selection changed → update the hint that explains the result."""
        self.sub_hint.configure(text=self._subtitle_hint())

    def _on_quality_pick(self, _value=None):
        """Quality changed → MP3 changes what subtitles can do."""
        self._on_subtitle_pick()

    def _subtitle_hint(self):
        """One line saying where the subtitles end up."""
        if not self._chosen_subs():
            return ""
        if self.q_menu.get() == "Audio Only (MP3)":
            return ".srt files next to the MP3"
        return "embedded in the video + .srt kept"

    # ─── Playlist selector ───────────────────────────────────────

    def _playlist_url(self):
        """URL that expands to the playlist behind the current card.

        A playlist card already holds one; a video card only carries the list
        id inside its watch link, so rebuild the canonical playlist URL.
        """
        if self.is_playlist:
            return self.current_url
        m = re.search(r"[?&]list=([^&]+)", self.current_url or "")
        if not m:
            return ""
        return f"https://www.youtube.com/playlist?list={m.group(1)}"

    def _open_selector(self):
        """Open the picker that chooses which playlist videos to fetch.

        A playlist analysis already brings the entries along. A
        watch?v=…&list=… card only knows the list id, so those are read on
        demand behind a small "reading playlist…" card — the fetch runs on a
        worker thread so the window never freezes.
        """
        if self._entries:
            self.app._show_overlay(
                lambda parent: self._build_selector(parent, self._entries))
            return

        url = self._playlist_url()
        if not url:
            return
        self.app._show_overlay(self._build_loading)
        _thread(lambda: self._load_entries(url))

    def _load_entries(self, url):
        """Worker: read the playlist's flat entry list (no formats resolved)."""
        info = self.engine.get_info(url)
        self.after(0, lambda: self._on_entries(info))

    def _on_entries(self, info):
        """Entry list arrived (or failed) → swap the loading card for it."""
        entries = info.get("entries") or []
        if not entries:
            self.app._close_overlay()
            messagebox.showerror(
                "Error", info.get("error") or "Could not read this playlist.")
            return
        self._entries = entries
        self.playlist_count = len(entries)
        # The card's count can differ from reality (private/deleted entries)
        self.btn_dl.configure(text=self._scope_label())
        self.app._show_overlay(
            lambda parent: self._build_selector(parent, entries))

    def _build_loading(self, parent):
        """Placeholder card shown while the entry list is being fetched."""
        card = ctk.CTkFrame(parent, fg_color=Theme.surface, corner_radius=14,
                            border_width=1, border_color=Theme.border)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkFrame(card, height=3, fg_color=Theme.cyan, corner_radius=0,
                     border_width=0).grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            card, text="Reading playlist\u2026",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=Theme.text
        ).grid(row=1, column=0, padx=40, pady=(22, 6))

        ctk.CTkLabel(
            card, text="fetching the video list",
            font=ctk.CTkFont(size=10), text_color=Theme.muted
        ).grid(row=2, column=0, padx=40, pady=(0, 22))

        return card

    def _build_selector(self, parent, entries):
        """Checkbox list of the playlist's entries, inside the app overlay.

        Ticks are playlist positions (1-based), which is exactly what
        download_playlist(only=…) filters on. Ticking everything collapses
        back to None so the plain "whole list" path runs unchanged.
        """
        total = len(entries)

        card = ctk.CTkFrame(parent, fg_color=Theme.surface, corner_radius=14,
                            border_width=1, border_color=Theme.border)
        card.grid_columnconfigure(0, weight=1)

        # Cyan accent strip, matching the other overlay cards
        ctk.CTkFrame(card, height=3, fg_color=Theme.cyan, corner_radius=0,
                     border_width=0).grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            card, text="Select videos",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=Theme.text
        ).grid(row=1, column=0, padx=24, pady=(18, 0), sticky="w")

        count_lbl = ctk.CTkLabel(
            card, text=f"{total} of {total} selected",
            font=ctk.CTkFont(size=11), text_color=Theme.muted
        )
        count_lbl.grid(row=2, column=0, padx=24, pady=(2, 0), sticky="w")

        # Scrolling checkbox list — playlists can be hundreds of entries long
        box = ctk.CTkScrollableFrame(
            card, width=460, height=300,
            fg_color=Theme.glass, corner_radius=10
        )
        box.grid(row=3, column=0, padx=24, pady=(12, 0), sticky="ew")
        box.grid_columnconfigure(0, weight=1)

        # Reopening the picker restores the previous ticks (None = all)
        prev = self.playlist_selection
        rows = []       # (index, BooleanVar) — index is the playlist position
        size_lbls = {}  # index -> label showing that video's size
        sizes = {}      # index -> bytes (0 = unknown)
        state = {"got": 0, "finished": False}

        for i, entry in enumerate(entries, start=1):
            title = entry.get("title") or f"Video {i}"
            short = title if len(title) <= 56 else title[:55] + "\u2026"
            var = tk.BooleanVar(value=(prev is None or i in prev))

            # A row frame, so the size can sit flush right of the title
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.grid(row=i - 1, column=0, sticky="ew", padx=4, pady=1)
            row.grid_columnconfigure(0, weight=1)

            ctk.CTkCheckBox(
                row, text=f"{i}.  {short}", variable=var,
                font=ctk.CTkFont(size=11), text_color=Theme.text,
                fg_color=Theme.cyan, hover_color=Theme.purple,
                border_width=1, border_color=Theme.border,
                checkbox_width=16, checkbox_height=16, corner_radius=4,
                command=lambda: refresh()
            ).grid(row=0, column=0, sticky="w")

            # "…" while its size is being resolved, "?" when YouTube hides it
            lbl = ctk.CTkLabel(
                row, text="\u2026", width=66, anchor="e",
                font=ctk.CTkFont(size=10), text_color=Theme.muted
            )
            lbl.grid(row=0, column=1, sticky="e", padx=(8, 6))

            rows.append((i, var))
            size_lbls[i] = lbl

        def refresh():
            """Counter plus the running total for whatever is ticked."""
            if not count_lbl.winfo_exists():
                # Picker closed → stop our probe instead of hammering YouTube
                if self._size_token is token:
                    self._size_token = None
                return
            picked = [i for i, v in rows if v.get()]
            known = [sizes[i] for i in picked if sizes.get(i)]
            text = f"{len(picked)} of {total} selected"
            if known:
                text += f"  \u00b7  ~{_fmt_size(sum(known))}"
                missing = len(picked) - len(known)
                if missing:
                    text += f" + {missing} unknown"
            if not state["finished"]:
                text += f"   \u00b7  sizes {state['got']}/{total}"
            count_lbl.configure(text=text)

        def apply_sizes(batch):
            """Fold a batch of resolved sizes into the labels and the total."""
            for i, nbytes in batch.items():
                sizes[i] = nbytes
                lbl = size_lbls.get(i)
                if lbl is not None and lbl.winfo_exists():
                    lbl.configure(text=_fmt_size(nbytes))
            state["got"] += len(batch)
            refresh()

        def sizes_done():
            state["finished"] = True
            refresh()

        # Sizes depend on the quality, so probe with the format in use
        token = object()  # identifies this picker's probe
        self._probe_sizes(entries, self._format_for(self.q_menu.get()),
                          apply_sizes, sizes_done, token)
        refresh()

        def set_all(value):
            for _, v in rows:
                v.set(value)
            refresh()

        def confirm():
            chosen = {i for i, v in rows if v.get()}
            if not chosen:
                messagebox.showwarning("Nothing selected",
                                       "Tick at least one video.")
                return
            # Everything ticked → behave exactly like "Download all"
            self.playlist_selection = None if len(chosen) == total else chosen
            self.app._close_overlay()
            self._start_download(self.q_menu.get(),
                                 only=self.playlist_selection)

        # Footer: bulk toggles on the left, actions on the right
        foot = ctk.CTkFrame(card, fg_color="transparent")
        foot.grid(row=4, column=0, padx=24, pady=(14, 18), sticky="ew")
        foot.grid_columnconfigure(2, weight=1)

        for col, (text, value) in enumerate((("All", True), ("None", False))):
            ctk.CTkButton(
                foot, text=text, width=60, height=30,
                fg_color=Theme.glass, hover_color=Theme.cyan,
                text_color=Theme.text, corner_radius=8,
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda v=value: set_all(v)
            ).grid(row=0, column=col, padx=(0, 8))

        ctk.CTkButton(
            foot, text="Cancel", width=90, height=32,
            fg_color="transparent", hover_color=Theme.glass,
            text_color=Theme.muted, corner_radius=8,
            font=ctk.CTkFont(size=11),
            command=self.app._close_overlay
        ).grid(row=0, column=3, padx=(0, 8))

        ctk.CTkButton(
            foot, text="Download selected", width=150, height=32,
            fg_color=Theme.cyan, hover_color=Theme.purple,
            text_color="#000000", corner_radius=8,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=confirm
        ).grid(row=0, column=4)

        return card

    def _probe_sizes(self, entries, fmt, on_batch, on_done, token):
        """Resolve per-video sizes in the background (YouTube gives none).

        A playlist page carries no file sizes at all, so every entry costs a
        full extraction (~1-2s). Four workers keep a 50-video list near 20s
        without looking like a burst to YouTube. Results are cached per
        (url, quality), so reopening the picker is instant; a superseded
        probe (picker closed, new link analysed) drops its results instead of
        poking widgets that no longer exist.
        """
        self._size_token = token  # supersedes any probe still running

        cached, todo = {}, []
        for i, entry in enumerate(entries, start=1):
            url = entry.get("url") or ""
            key = (url, fmt)
            if key in self._size_cache:
                cached[i] = self._size_cache[key]
            else:
                todo.append((i, url))

        if cached:
            on_batch(cached)
        if not todo:
            on_done()
            return

        def worker():
            from concurrent.futures import (ThreadPoolExecutor, wait,
                                            FIRST_COMPLETED)
            # Only 4 extractions are ever in flight, and the next one is
            # queued only when a slot frees up — closing the picker therefore
            # stops a 500-entry list after ~4 more requests instead of
            # grinding through all of them.
            pending, nxt = {}, 0
            with ThreadPoolExecutor(max_workers=4) as pool:
                while nxt < len(todo) or pending:
                    while nxt < len(todo) and len(pending) < 4:
                        i, url = todo[nxt]
                        nxt += 1
                        pending[pool.submit(
                            self.engine.estimate_size, url, fmt)] = (i, url)
                    if self._size_token is not token:
                        return  # picker closed or a newer probe took over
                    finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        i, url = pending.pop(fut)
                        try:
                            size = fut.result()
                        except Exception:
                            size = 0
                        self._size_cache[(url, fmt)] = size
                        # Default arg: the loop variable moves on
                        self.after(0, lambda b={i: size}: on_batch(b))
            if self._size_token is token:
                self.after(0, on_done)

        _thread(worker)

    def _load_thumb(self, url):
        try:
            r = requests.get(url, timeout=10)
            img = Image.open(BytesIO(r.content))
            # Keep a generously-sized master (1280 cap) so re-fits stay sharp
            img.thumbnail((1280, 720), Image.Resampling.LANCZOS)
            self._pil_thumb = img
            # _refit_thumb touches widgets → must run on the Tk main thread
            self.after(0, self._refit_thumb)
        except Exception:
            pass

    # ─── Download ────────────────────────────────────────────────

    def _download(self):
        """Download button → whole list for a playlist, else one video."""
        q = self.q_menu.get()
        if not q or q == "Select":
            messagebox.showwarning("Hold on", "Pick a quality first.")
            return
        self._start_download(q)

    def _start_download(self, q, only=None):
        """Kick off a worker thread; only = playlist indices from the selector."""
        fmt = self._format_for(q)
        self._cancel_evt.clear()
        self.btn_dl.configure(state="disabled", text="...")
        self.btn_select.configure(state="disabled")
        self.chk_playlist.configure(state="disabled")
        self.sub_menu.configure(state="disabled")
        self.btn_cancel.configure(state="normal", text="Cancel")
        self.btn_cancel.grid()
        self._show_progress()
        self.prog.set(0)
        self.prog_txt.configure(text="starting ...")
        subs = self._chosen_subs()
        if self._playlist_mode():
            _thread(lambda: self._run_playlist(fmt, q, only, subs))
        else:
            _thread(lambda: self._run_dl(fmt, q, subs))

    def _cancel_download(self):
        """Ask the worker to stop — it aborts the transfer in flight."""
        self._cancel_evt.set()
        self.btn_cancel.configure(state="disabled", text="stopping\u2026")
        self.prog_txt.configure(text="cancelling \u2026")

    @staticmethod
    def _format_for(q):
        """Map a quality-menu label to a yt-dlp format string.

        Handles both shapes the menu produces: "1080p (~120 MB)" from a video
        card and the bare "1080p" buckets offered on a playlist card.
        """
        if q == "Audio Only (MP3)":
            return "audio_only"
        height = q.split("p")[0]
        if height.isdigit():
            return (f"bestvideo[height<={height}]+bestaudio/"
                    f"best[height<={height}]")
        return "bestvideo+bestaudio/best"

    def _run_dl(self, fmt, label, subs=None):
        def tick(p):
            self.after(0, lambda: self.prog.set(p / 100))
            self.after(0, lambda: self.prog_txt.configure(text=f"{p:.1f}%"))

        # Save into the shared, user-chosen folder (not hardcoded ~/Downloads)
        dl_path = self.app.download_folder
        os.makedirs(dl_path, exist_ok=True)
        res = self.engine.download(
            self.current_url, format_id=fmt,
            progress_callback=tick, download_path=dl_path,
            should_cancel=self._cancel_evt.is_set, subs=subs
        )
        self.after(0, lambda: self._on_dl(res, label, dl_path))

    def _on_dl(self, res, label, dl_path):
        if res.get("status") == "cancelled":
            self._idle()
            self.prog_txt.configure(text="cancelled \u2715")
            return

        if "error" in res:
            self.prog_txt.configure(text="failed")
            messagebox.showerror("Error", res["error"])
            self._idle()
            return

        title = self.info.get("title", "Unknown") if self.info else "Unknown"
        latest = self._latest(dl_path)
        fname = os.path.basename(latest) if latest else ""
        filepath = latest or dl_path
        self.tracker.add(title=title, url=self.current_url, quality=label, filename=fname, filepath=filepath)

        self._idle()
        self.prog_txt.configure(text="done \u2713")
        # Same completion card as the original: Play / Open Folder / Close
        self.app._show_done_popup(title, filepath, label)

    def _run_playlist(self, fmt, label, only=None, subs=None):
        """Download every entry in the playlist, one video at a time."""
        def item(i, total, title):
            short = title if len(title) <= 60 else title[:59] + "\u2026"
            self.after(0, lambda: self.prog_txt.configure(
                text=f"{i}/{total}  \u2022  {short}"))
            self.after(0, lambda: self.prog.set(0))

        def tick(p):
            self.after(0, lambda: self.prog.set(p / 100))

        dl_path = self.app.download_folder
        os.makedirs(dl_path, exist_ok=True)
        res = self.engine.download_playlist(
            self.current_url, format_id=fmt,
            on_item=item, progress_callback=tick, download_path=dl_path,
            only=only, should_cancel=self._cancel_evt.is_set, subs=subs
        )
        self.after(0, lambda: self._on_playlist_dl(res, label, dl_path))

    def _on_playlist_dl(self, res, label, dl_path):
        if "error" in res:
            self.prog_txt.configure(text="failed")
            messagebox.showerror("Error", res["error"])
            self._idle()
            return

        # One history row per video so History stays a per-file record
        for entry in res["downloaded"]:
            self.tracker.add(title=entry["title"], url=entry["url"],
                             quality=label, filename="", filepath=dl_path)

        done = len(res["downloaded"])
        total = res["total"]

        # Cancel stops mid-list: keep what finished, skip the "all done" card
        if res.get("cancelled"):
            self._idle()
            self.prog_txt.configure(text=f"cancelled \u2715  {done}/{total}")
            return

        self._idle()
        self.prog_txt.configure(text=f"done \u2713  {done}/{total}")
        # Folder (not a file) → the card drops its Play button by itself
        self.app._show_done_popup(
            f"{self.info.get('title', 'Playlist')}  \u2014  {done}/{total} videos",
            dl_path, label
        )
        if res["failed"]:
            names = "\n".join(f"\u2022 {f['title']}" for f in res["failed"][:10])
            messagebox.showwarning(
                "Some videos failed",
                f"{len(res['failed'])} of {total} could not be downloaded:\n\n{names}"
            )

    def _latest(self, d):
        """Newest finished file in d — never a temp/partial of any tab.

        Skips .part/.ytdl/dotfiles and intermediate per-format chunks
        (Title.f137.mp4) left by a concurrent download in the same folder.
        """
        try:
            chunk_re = re.compile(r"\.f\d+(-\d+)?\.[A-Za-z0-9]+$")
            files = [
                os.path.join(d, f)
                for f in os.listdir(d)
                if os.path.isfile(os.path.join(d, f))
                and not f.startswith(".")
                and not f.endswith((".part", ".ytdl"))
                and not chunk_re.search(f)
            ]
            return max(files, key=os.path.getmtime) if files else None
        except Exception:
            return None


# ─── History View ─────────────────────────────────────────────────
class HistoryView(ctk.CTkFrame):
    def __init__(self, parent, tracker):
        super().__init__(parent, fg_color=Theme.bg)
        self.tracker = tracker

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        hdr = ctk.CTkFrame(self, fg_color=Theme.surface, corner_radius=12, border_width=0)
        hdr.grid(row=0, column=0, padx=48, pady=(24, 12), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr, text="History",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=Theme.text
        ).grid(row=0, column=0, padx=20, pady=(14, 2), sticky="w")

        ctk.CTkLabel(
            hdr, text="recent downloads",
            font=ctk.CTkFont(size=11),
            text_color=Theme.muted
        ).grid(row=1, column=0, padx=20, pady=(0, 14), sticky="w")

        self.btn_clear = ctk.CTkButton(
            hdr, text="clear", width=70, height=28,
            fg_color="transparent", hover_color=Theme.pink,
            text_color=Theme.muted, corner_radius=6,
            font=ctk.CTkFont(size=11),
            command=self._clear_hist
        )
        self.btn_clear.grid(row=0, column=1, rowspan=2, padx=(0, 16))

        # Scroll
        self.hist_scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            corner_radius=0, scrollbar_button_color=Theme.glass,
            scrollbar_button_hover_color=Theme.cyan,
            border_width=0
        )
        self.hist_scroll.grid(row=1, column=0, padx=48, pady=(0, 28), sticky="nsew")
        self.hist_scroll.grid_columnconfigure(0, weight=1)

        self.hist_inner = ctk.CTkFrame(self.hist_scroll, fg_color="transparent")
        self.hist_inner.grid(row=0, column=0, sticky="nsew")
        self.hist_inner.grid_columnconfigure(0, weight=1)

    def render(self):
        for w in self.hist_inner.winfo_children():
            w.destroy()

        items = self.tracker.get_recent(50)
        if not items:
            e = ctk.CTkLabel(
                self.hist_inner,
                text="No downloads yet",
                font=ctk.CTkFont(size=13),
                text_color=Theme.muted
            )
            e.grid(row=0, column=0, pady=80)
            self.btn_clear.configure(state="disabled")
            return

        self.btn_clear.configure(state="normal")

        for i, entry in enumerate(items):
            card = ctk.CTkFrame(
                self.hist_inner, fg_color=Theme.surface,
                corner_radius=12, border_width=0
            )
            card.grid(row=i, column=0, padx=0, pady=(0, 8), sticky="ew")
            card.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(
                card, text=str(i + 1).zfill(2),
                width=28, height=28, corner_radius=6,
                fg_color=Theme.glass, text_color=Theme.cyan,
                font=ctk.CTkFont(size=10, weight="bold")
            ).grid(row=0, column=0, rowspan=2, padx=(12, 8), pady=10)

            ctk.CTkLabel(
                card, text=entry.get("title", "?"),
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color=Theme.text, anchor="w",
                wraplength=460
            ).grid(row=0, column=1, sticky="w", pady=(10, 0))

            q = entry.get("quality", "")
            d = entry.get("date", "")
            ctk.CTkLabel(
                card,
                text=f"{q}  \u2022  {d}" if q else d,
                font=ctk.CTkFont(size=10),
                text_color=Theme.muted, anchor="w"
            ).grid(row=1, column=1, sticky="w", pady=(2, 10))

            acts = ctk.CTkFrame(card, fg_color="transparent")
            acts.grid(row=0, column=2, rowspan=2, padx=(4, 12))

            fp = entry.get("filepath", "")
            if fp and os.path.exists(fp):
                ctk.CTkButton(
                    acts, text="\u25B6", width=32, height=28,
                    fg_color=Theme.glass, hover_color=Theme.green,
                    text_color=Theme.text, corner_radius=6,
                    font=ctk.CTkFont(size=10),
                    command=lambda p=fp: _open_path(p)
                ).pack(side="left", padx=(0, 4))

                ctk.CTkButton(
                    acts, text="\U0001F4C2", width=32, height=28,
                    fg_color=Theme.glass, hover_color=Theme.purple,
                    text_color=Theme.text, corner_radius=6,
                    font=ctk.CTkFont(size=10),
                    command=lambda p=fp: _open_path(os.path.dirname(p))
                ).pack(side="left")

    def _clear_hist(self):
        self.tracker.clear()
        self.render()


# ─── Main App ─────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("BanKai Downloader")
        self.geometry("1120x780")
        # Keep the layout usable — below this the thumb/input rows would break
        self.minsize(900, 620)

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
        if os.path.exists(icon_path):
            self._icon = ImageTk.PhotoImage(Image.open(icon_path).resize((32, 32), Image.Resampling.LANCZOS))
            self.tk.call("wm", "iconphoto", self._w, self._icon)
        self.configure(fg_color=Theme.bg)

        self.engine = DownloaderEngine()
        self.tracker = DownloadTracker()

        # Shared download folder — loaded before tabs are built so every
        # tab's 'save to' label starts with the persisted value.
        self.download_folder = _load_download_folder()

        self._tabs = []
        self._tab_buttons = []
        self._current_view = None
        self._resize_job = None  # pending debounced-resize after() id
        self._overlay = None  # active full-window "done" overlay (if any)

        self._build()
        # Open one tab on launch so the app never starts on an empty view
        self._add_tab()
        self._bind_tab_strip_wheel()
        # Debounced window resize → tab layouts re-fit (thumb, wraplength)
        self.bind("<Configure>", self._on_window_configure)
        self.after(10, self._center_window)
        # First sizing pass once the window has its real dimensions
        self.after(200, self._fan_out_resize)

    def _center_window(self):
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"+{x}+{y}")

    # ─── Responsive resize (debounced fan-out to tabs) ───────────

    def _on_window_configure(self, event):
        """Window/child resize hook — filter to the window itself, debounce 150ms."""
        if event.widget is not self:
            return
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(150, self._fan_out_resize)

    def _fan_out_resize(self):
        """Apply one resize pass to every open tab (cheap no-ops when unchanged)."""
        self._resize_job = None
        for tab in self._tabs:
            tab._apply_resize()

    # ─── Tab strip horizontal wheel scrolling ────────────────────

    def _bind_tab_strip_wheel(self):
        """Plain wheel over the strip scrolls tabs sideways.

        customtkinter's own wheel handler scrolls Y (no-op for a horizontal
        strip) and only scrolls X when Shift is held — so we add our own
        bind_all handler for when the pointer is inside the strip.
        """
        if sys.platform.startswith("linux"):
            self.bind_all("<Button-4>", self._on_tab_wheel, add="+")
            self.bind_all("<Button-5>", self._on_tab_wheel, add="+")
        else:
            self.bind_all("<MouseWheel>", self._on_tab_wheel, add="+")

    def _on_tab_wheel(self, event):
        """Scroll the tab strip horizontally; only when the wheel is over it."""
        canvas = self.tab_bar._parent_canvas
        # Tab-strip widgets are Tk children of the strip canvas → path prefix
        if not str(event.widget).startswith(str(canvas)):
            return None
        # CTk already scrolls X on Shift — don't double-scroll
        if getattr(self.tab_bar, "_shift_pressed", False):
            return None
        if canvas.xview() == (0.0, 1.0):
            return None  # everything fits — nothing to scroll
        if sys.platform.startswith("linux"):
            canvas.xview_scroll(-1 if event.num == 4 else 1, "units")
        elif sys.platform == "darwin":
            canvas.xview_scroll(-event.delta, "units")
        else:  # Windows
            canvas.xview_scroll(-int(event.delta / 6) or -1, "units")
        return "break"

    def _sync_tab_scrollbar(self, first, last):
        """Drive the strip's scrollbar and hide it while all tabs fit.

        Installed as the canvas's xscrollcommand (wraps CTk's scrollbar.set),
        so it updates on tab add/remove and on window resize automatically.
        """
        self.tab_bar._scrollbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.tab_bar._scrollbar.grid_remove()  # no overflow → no bar
        else:
            self.tab_bar._scrollbar.grid()  # restore (keeps original geometry)

    # ─── In-Window Overlay (Download Complete popup) ─────────────

    def _show_overlay(self, build_fn):
        """Show a full-window overlay containing a centered card built by build_fn."""
        if self._overlay and self._overlay.winfo_exists():
            self._overlay.destroy()
        overlay = ctk.CTkFrame(self, fg_color=Theme.bg, corner_radius=0, border_width=0)
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lift()
        card = build_fn(overlay)
        card.place(relx=0.5, rely=0.5, anchor="center")
        overlay.update()
        # Click on the empty backdrop (not the card) dismisses the overlay
        overlay.bind("<Button-1>",
                     lambda e: self._close_overlay() if e.widget == overlay else None)
        self._overlay = overlay

    def _close_overlay(self):
        """Destroy the active overlay, if any."""
        if self._overlay and self._overlay.winfo_exists():
            self._overlay.destroy()
            self._overlay = None

    def _show_done_popup(self, title, filepath, quality):
        """Full-window 'DOWNLOAD COMPLETE' card with Play / Open Folder / Close.

        filepath may be a file (normal) or the folder itself (fallback when
        the newest file couldn't be identified).
        """
        is_file = os.path.isfile(filepath)
        # Folder to reveal: parent of the file, or the path itself if it's a dir
        folder = os.path.dirname(filepath) if is_file else filepath

        def build(parent):
            card = ctk.CTkFrame(parent, fg_color=Theme.surface, corner_radius=14,
                                border_width=1, border_color=Theme.border)
            card.grid_columnconfigure(0, weight=1)

            # Green accent strip across the top of the card
            ctk.CTkFrame(card, height=3, fg_color=Theme.green, corner_radius=0,
                         border_width=0).grid(row=0, column=0, sticky="ew")

            ctk.CTkLabel(
                card, text="✓",
                font=ctk.CTkFont(size=48, weight="bold"),
                text_color=Theme.green
            ).grid(row=1, column=0, pady=(24, 4))

            ctk.CTkLabel(
                card, text="DOWNLOAD COMPLETE",
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color=Theme.text
            ).grid(row=2, column=0, pady=(0, 16))

            # Title / filename / folder info block
            info_card = ctk.CTkFrame(card, fg_color=Theme.glass, corner_radius=10, border_width=0)
            info_card.grid(row=3, column=0, padx=28, sticky="ew")
            info_card.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                info_card, text=title,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=Theme.text, wraplength=380
            ).grid(row=0, column=0, padx=16, pady=(14, 6), sticky="w")

            ctk.CTkLabel(
                info_card,
                text=f"{quality}  •  {os.path.basename(filepath) if is_file else folder}",
                font=ctk.CTkFont(size=10),
                text_color=Theme.muted
            ).grid(row=1, column=0, padx=16, pady=(0, 4), sticky="w")

            ctk.CTkLabel(
                info_card, text=folder,
                font=ctk.CTkFont(size=9),
                text_color=Theme.muted
            ).grid(row=2, column=0, padx=16, pady=(0, 14), sticky="w")

            # Action buttons
            act = ctk.CTkFrame(card, fg_color="transparent")
            act.grid(row=4, column=0, pady=(20, 24))

            if is_file:
                ctk.CTkButton(
                    act, text="▶  Play", width=110, height=36,
                    fg_color=Theme.glass, hover_color=Theme.green,
                    text_color=Theme.text, corner_radius=8,
                    font=ctk.CTkFont(size=12, weight="bold"),
                    command=lambda: (_open_path(filepath), self._close_overlay())
                ).pack(side="left", padx=(0, 8))

            ctk.CTkButton(
                act, text="\U0001F4C2  Open Folder", width=130, height=36,
                fg_color=Theme.glass, hover_color=Theme.purple,
                text_color=Theme.text, corner_radius=8,
                font=ctk.CTkFont(size=12, weight="bold"),
                command=lambda: (_open_path(folder), self._close_overlay())
            ).pack(side="left", padx=(0, 8))

            ctk.CTkButton(
                act, text="Close", width=90, height=36,
                fg_color="transparent", hover_color=Theme.glass,
                text_color=Theme.muted, corner_radius=8,
                font=ctk.CTkFont(size=11),
                command=self._close_overlay
            ).pack(side="left")

            return card

        self._show_overlay(build)

    # ─── Build ───────────────────────────────────────────────────

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ─── Title bar ──────────────────────────────────────────
        tb = ctk.CTkFrame(self, height=48, fg_color=Theme.title_bg, corner_radius=0, border_width=0)
        tb.grid(row=0, column=0, sticky="ew")
        # Column 1 (the tab strip) absorbs ALL spare width so History/+ never shift
        tb.grid_columnconfigure(1, weight=1)

        # Logo
        logo_box = ctk.CTkFrame(tb, fg_color="transparent")
        logo_box.grid(row=0, column=0, padx=(16, 0))

        ctk.CTkLabel(
            logo_box, text="⚡",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=Theme.pink
        ).pack(side="left")
        ctk.CTkLabel(
            logo_box, text="BANKAI",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=Theme.cyan
        ).pack(side="left", padx=(4, 24))

        # Scrollable tab strip host — clips overflowing tabs instead of
        # pushing the buttons on the right off-screen
        strip_host = ctk.CTkFrame(tb, fg_color="transparent")
        strip_host.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        strip_host.grid_columnconfigure(0, weight=1)

        self.tab_bar = ctk.CTkScrollableFrame(
            strip_host,
            orientation="horizontal",
            width=400, height=26,
            corner_radius=0, border_width=0,
            fg_color="transparent",
            scrollbar_fg_color=Theme.title_bg,
            scrollbar_button_color="#2E2E52",
            scrollbar_button_hover_color=Theme.cyan,
        )
        self.tab_bar.grid(row=0, column=0, sticky="ew")

        # Hide the strip scrollbar whenever the tabs all fit
        self.tab_bar._parent_canvas.configure(xscrollcommand=self._sync_tab_scrollbar)
        self._sync_tab_scrollbar(*self.tab_bar._parent_canvas.xview())

        # History button — outside the strip, can never be pushed away
        self.btn_hist = ctk.CTkButton(
            tb, text="History", width=80, height=26,
            fg_color="transparent", text_color=Theme.muted,
            hover=False, corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._show_history
        )
        self.btn_hist.grid(row=0, column=2, padx=(0, 8))

        # Add tab button — pinned right, outside the strip
        self.btn_add = ctk.CTkButton(
            tb, text="+", width=28, height=26,
            fg_color=Theme.glass, hover_color=Theme.cyan,
            text_color=Theme.text, corner_radius=6,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._add_tab
        )
        self.btn_add.grid(row=0, column=3, padx=(0, 12))

        # ─── Content area ───────────────────────────────────────
        self.content_frame = ctk.CTkFrame(self, fg_color=Theme.bg)
        self.content_frame.grid(row=1, column=0, sticky="nsew")
        self.content_frame.grid_columnconfigure(0, weight=1)
        self.content_frame.grid_rowconfigure(0, weight=1)

        self.history_view = HistoryView(self.content_frame, self.tracker)
        self.history_view.grid(row=0, column=0, sticky="nsew")
        self.history_view.grid_remove()

        # ─── Footer: author credit / copyright ──────────────────
        # Row 2 is left at weight=0, so the footer keeps its natural height
        # and only the content area (row 1) stretches on window resize.
        ctk.CTkLabel(
            self, text=APP_CREDIT,
            height=26, fg_color=Theme.title_bg, corner_radius=0,
            font=ctk.CTkFont(size=10), text_color=Theme.muted,
        ).grid(row=2, column=0, sticky="ew")

    # ─── Shared download folder ──────────────────────────────────

    def set_download_folder(self, path):
        """Apply a new download folder app-wide: persist it and refresh labels."""
        self.download_folder = path
        _save_download_folder(path)
        for tab in self._tabs:
            tab._set_folder_label(path)

    # ─── Tab management ─────────────────────────────────────────

    def _add_tab(self):
        tab = DownloadTab(self.content_frame, self.engine, self.tracker, self)
        self._tabs.append(tab)

        btn_frame = ctk.CTkFrame(self.tab_bar, fg_color="transparent")
        btn_frame.pack(side="left", padx=2)

        idx = len(self._tabs) - 1
        label = f"Tab {idx + 1}"

        btn = ctk.CTkButton(
            btn_frame, text=label, height=26,
            fg_color=Theme.glass, hover_color=Theme.cyan,
            text_color=Theme.text, corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda i=idx: self._switch_tab(i)
        )
        btn.pack(side="left", padx=(0, 2))

        close_btn = ctk.CTkButton(
            btn_frame, text="×", width=20, height=26,
            fg_color="transparent", hover_color=Theme.pink,
            text_color=Theme.muted, corner_radius=4,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda i=idx, f=btn_frame: self._close_tab(i, f)
        )
        close_btn.pack(side="left")

        self._tab_buttons.append((btn_frame, btn, close_btn))
        self._switch_tab(idx)
        # New tabs appear at the strip's right end — keep them in view
        try:
            self.tab_bar._parent_canvas.xview_moveto(1.0)
        except Exception:
            pass

    def _switch_tab(self, idx):
        if idx >= len(self._tabs):
            return

        self._hide_all()
        self._tabs[idx].grid(row=0, column=0, sticky="nsew")
        self._current_view = self._tabs[idx]
        # Tab may have been hidden during a window resize — re-fit once Tk
        # has laid it out (winfo_width is still 1 immediately after grid()).
        shown = self._tabs[idx]
        self.after(0, shown._apply_resize)

        for i, (frame, btn, _) in enumerate(self._tab_buttons):
            if i == idx:
                btn.configure(fg_color=Theme.cyan, text_color="#000")
            else:
                btn.configure(fg_color=Theme.glass, text_color=Theme.text)

        self.btn_hist.configure(text_color=Theme.muted)

    def _close_tab(self, idx, frame):
        if len(self._tabs) <= 1:
            return

        self._tabs[idx].destroy()
        self._tabs.pop(idx)
        frame.destroy()
        self._tab_buttons.pop(idx)

        # Reindex buttons
        for i, (f, btn, close) in enumerate(self._tab_buttons):
            btn.configure(command=lambda ii=i: self._switch_tab(ii))
            close.configure(command=lambda ii=i, ff=f: self._close_tab(ii, ff))
            btn.configure(text=f"Tab {i + 1}")

        if self._tabs:
            self._switch_tab(min(idx, len(self._tabs) - 1))

    def _show_history(self):
        self._hide_all()
        self.history_view.grid(row=0, column=0, sticky="nsew")
        self.history_view.render()
        self._current_view = self.history_view

        for _, btn, _ in self._tab_buttons:
            btn.configure(fg_color=Theme.glass, text_color=Theme.text)
        self.btn_hist.configure(text_color=Theme.cyan)

    def _hide_all(self):
        for tab in self._tabs:
            tab.grid_remove()
        self.history_view.grid_remove()


if __name__ == "__main__":
    app = App()
    app.mainloop()
