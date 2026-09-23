import customtkinter as ctk
from tkinter import messagebox, filedialog
from PIL import Image, ImageTk
import threading
import requests
from io import BytesIO
import json
import os
import re
import sys
import subprocess
from downloader_engine import DownloaderEngine
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


# ─── Settings (download folder, persisted across launches) ────────
# Stored next to download_history.json so all app data lives in one place.
SETTINGS_FILE = os.path.join(HISTORY_DIR, "settings.json")
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads")


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

ctk.set_appearance_mode("Dark")

def _thread(fn):
    threading.Thread(target=fn, daemon=True).start()


# ─── App ──────────────────────────────────────────────────────────
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
        self.info = None
        self.current_url = ""
        # Save location: persisted choice, default ~/Downloads (loaded before _build
        # so the SAVE TO label starts with the right value)
        self.dl_path = _load_download_folder()
        self._thumb = None    # CTkImage currently shown (keeps PIL alive for GC)
        self._pil_thumb = None  # original PIL image, kept for re-fits on resize
        self._tab = "download"
        self._overlay = None
        self._resize_job = None  # pending debounced-resize after() id

        self._build()
        self.url_input.bind("<Return>", lambda e: self._analyze())
        self._switch("download")

        # Debounced window resize → re-fit title wrap + thumbnail
        self.bind("<Configure>", self._on_window_configure)
        self.after(10, self._center_window)
        # First sizing pass once the window has its real dimensions
        self.after(200, self._apply_resize)

    def _center_window(self):
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"+{x}+{y}")

    # ─── Responsive resize (debounced) ─────────────────────────────

    def _on_window_configure(self, event):
        """Window resize hook — filter to the window itself, debounce 150ms."""
        if event.widget is not self:
            return
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(150, self._apply_resize)

    def _apply_resize(self):
        """Re-fit title wrap width + thumbnail to the current window size."""
        self._resize_job = None
        w = self.winfo_width()
        if w <= 1:  # not yet realised
            return
        self.v_title.configure(wraplength=max(300, w - 220))
        self._refit_thumb()

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
        # Box tracks window width, clamped to sane bounds
        box_w = max(300, min(640, w - 200))
        box_h = max(200, min(380, int(box_w * ph / pw)))
        img = self._pil_thumb.copy()
        img.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
        self._thumb = ctk.CTkImage(
            light_image=img, dark_image=img,
            size=(img.width, img.height)
        )
        self.thumb.configure(image=self._thumb, width=box_w, height=box_h)

    # ─── Build ────────────────────────────────────────────────────

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ─── Custom Title Bar ─────────────────────────────────
        tb = ctk.CTkFrame(self, height=44, fg_color=Theme.title_bg, corner_radius=0, border_width=0)
        tb.grid(row=0, column=0, sticky="ew")
        tb.grid_columnconfigure(3, weight=1)

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

        self.tab_dl = ctk.CTkButton(
            tb, text="Download", width=80, height=26,
            fg_color="transparent", text_color=Theme.cyan,
            hover=False, corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self._switch("download")
        )
        self.tab_dl.grid(row=0, column=1, padx=2)

        self.tab_hist = ctk.CTkButton(
            tb, text="History", width=80, height=26,
            fg_color="transparent", text_color=Theme.muted,
            hover=False, corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self._switch("history")
        )
        self.tab_hist.grid(row=0, column=2, padx=2)

        # ─── Download View ───────────────────────────────────────
        self.dl_frame = ctk.CTkFrame(self, fg_color=Theme.bg)
        self.dl_frame.grid(row=1, column=0, sticky="nsew")
        self.dl_frame.grid_columnconfigure(0, weight=1)
        self.dl_frame.grid_rowconfigure(0, weight=1)

        # Center wrapper — stretches with the window so preview/thumbnails
        # get the full width and height (nsew)
        wrap = ctk.CTkFrame(self.dl_frame, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="nsew", padx=48)
        wrap.grid_columnconfigure(0, weight=1)

        # Row 0: fixed top spacer (no weight → no blank gap).
        # Row 3 (preview): takes ALL vertical slack so sticky="nsew" works.
        wrap.grid_rowconfigure(0, weight=0)
        wrap.grid_rowconfigure(3, weight=1)

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

        self.btn_go = ctk.CTkButton(
            inp, text="ANALYZE", width=110, height=42,
            fg_color=Theme.cyan, hover_color="#00CCDD",
            text_color="#000", corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._analyze
        )
        self.btn_go.grid(row=0, column=1, padx=(0, 6), pady=6)

        # ── Folder chooser row ─────────────────────────────────
        frow = ctk.CTkFrame(wrap, fg_color=Theme.surface, corner_radius=14, border_width=0)
        frow.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        frow.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            frow, text="SAVE TO",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=Theme.muted
        ).grid(row=0, column=0, padx=(16, 10), pady=8)

        # Shows the current download folder; updated via _set_folder_label
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

        # Show the folder downloads will save into
        self._set_folder_label(self.dl_path)

        # ── Preview (hidden) ────────────────────────────────────
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
            font=ctk.CTkFont(size=12)
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

        # Progress bar (thin, hidden)
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

        # ─── History View ───────────────────────────────────────
        self.hist_frame = ctk.CTkFrame(self, fg_color=Theme.bg)
        self.hist_frame.grid(row=1, column=0, sticky="nsew")
        self.hist_frame.grid_columnconfigure(0, weight=1)
        self.hist_frame.grid_rowconfigure(1, weight=1)

        # Header
        hdr = ctk.CTkFrame(self.hist_frame, fg_color=Theme.surface, corner_radius=12, border_width=0)
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
            self.hist_frame, fg_color="transparent",
            corner_radius=0, scrollbar_button_color=Theme.glass,
            scrollbar_button_hover_color=Theme.cyan,
            border_width=0
        )
        self.hist_scroll.grid(row=1, column=0, padx=48, pady=(0, 28), sticky="nsew")
        self.hist_scroll.grid_columnconfigure(0, weight=1)
        # inner frame
        self.hist_inner = ctk.CTkFrame(self.hist_scroll, fg_color="transparent")
        self.hist_inner.grid(row=0, column=0, sticky="nsew")
        self.hist_inner.grid_columnconfigure(0, weight=1)

        self.hist_frame.grid_remove()

    # ─── History Render ───────────────────────────────────────────

    def _render_hist(self):
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

            # Index
            ctk.CTkLabel(
                card,
                text=str(i + 1).zfill(2),
                width=28, height=28, corner_radius=6,
                fg_color=Theme.glass, text_color=Theme.cyan,
                font=ctk.CTkFont(size=10, weight="bold")
            ).grid(row=0, column=0, rowspan=2, padx=(12, 8), pady=10)

            # Title
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

            # Buttons
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

    # ─── Nav ──────────────────────────────────────────────────────

    def _switch(self, tab):
        self._tab = tab
        if tab == "download":
            self.hist_frame.grid_remove()
            self.dl_frame.grid()
            self.tab_dl.configure(text_color=Theme.cyan)
            self.tab_hist.configure(text_color=Theme.muted)
            # Download view was hidden while on History — re-fit once Tk
            # has laid it out (winfo_width is still 1 immediately after grid()).
            self.after(0, self._apply_resize)
        else:
            self.dl_frame.grid_remove()
            self._render_hist()
            self.hist_frame.grid()
            self.tab_hist.configure(text_color=Theme.cyan)
            self.tab_dl.configure(text_color=Theme.muted)

    # ─── UI helpers ───────────────────────────────────────────────

    def _set_folder_label(self, path):
        """Refresh the 'save to' label after the download folder changes."""
        self.folder_lbl.configure(text=_pretty_path(path))

    def _pick_folder(self):
        """Open the native folder picker, persist the choice, refresh label."""
        chosen = filedialog.askdirectory(
            title="Choose download folder",
            initialdir=self.dl_path
        )
        if chosen:  # empty string = user cancelled
            self.dl_path = chosen
            _save_download_folder(chosen)
            self._set_folder_label(chosen)

    def _show_content(self):
        self.empty.place_forget()
        self.content.grid()

    def _hide_content(self):
        self.content.grid_remove()
        self.empty.place(relx=0.5, rely=0.4, anchor="center")

    def _show_progress(self):
        self.prog_frame.grid()
        self.prog_txt.grid()

    def _hide_progress(self):
        self.prog_frame.grid_remove()
        self.prog_txt.grid_remove()
        self.prog.set(0)

    def _idle(self):
        self.btn_go.configure(state="normal", text="\u2192")
        self.btn_dl.configure(state="normal", text="Download")
        self._hide_progress()

    # ─── In-Window Overlay ─────────────────────────────────────────

    def _show_overlay(self, build_fn):
        if self._overlay and self._overlay.winfo_exists():
            self._overlay.destroy()
        overlay = ctk.CTkFrame(self, fg_color=Theme.bg, corner_radius=0, border_width=0)
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lift()
        card = build_fn(overlay)
        card.place(relx=0.5, rely=0.5, anchor="center")
        overlay.update()
        overlay.bind("<Button-1>", lambda e: self._close_overlay() if e.widget == overlay else None)
        self._overlay = overlay

    def _close_overlay(self):
        if self._overlay and self._overlay.winfo_exists():
            self._overlay.destroy()
            self._overlay = None

    # ─── Download Complete Popup ───────────────────────────────────

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

            ctk.CTkFrame(card, height=3, fg_color=Theme.green, corner_radius=0,
                         border_width=0).grid(row=0, column=0, sticky="ew")

            ctk.CTkLabel(
                card, text="\u2713",
                font=ctk.CTkFont(size=48, weight="bold"),
                text_color=Theme.green
            ).grid(row=1, column=0, pady=(24, 4))

            ctk.CTkLabel(
                card, text="DOWNLOAD COMPLETE",
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color=Theme.text
            ).grid(row=2, column=0, pady=(0, 16))

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
                text=f"{quality}  \u2022  {os.path.basename(filepath) if is_file else folder}",
                font=ctk.CTkFont(size=10),
                text_color=Theme.muted
            ).grid(row=1, column=0, padx=16, pady=(0, 4), sticky="w")

            ctk.CTkLabel(
                info_card, text=folder,
                font=ctk.CTkFont(size=9),
                text_color=Theme.muted
            ).grid(row=2, column=0, padx=16, pady=(0, 14), sticky="w")

            act = ctk.CTkFrame(card, fg_color="transparent")
            act.grid(row=4, column=0, pady=(20, 24))

            # Play only makes sense when we actually resolved a file
            if is_file:
                ctk.CTkButton(
                    act, text="\u25B6  Play", width=110, height=36,
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

    # ─── Analysis ─────────────────────────────────────────────────

    def _analyze(self):
        url = self.url_input.get().strip()
        if not url:
            return
        self.current_url = url
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

        thumb = info.get("thumbnail")
        if thumb:
            _thread(lambda: self._load_thumb(thumb))

        # Title/wrap depend on the now-realised content — re-fit immediately
        self._apply_resize()
        self._idle()

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

    # ─── Download ─────────────────────────────────────────────────

    def _download(self):
        q = self.q_menu.get()
        if not q or q == "Select":
            messagebox.showwarning("Hold on", "Pick a quality first.")
            return

        fmt = "audio_only" if q == "Audio Only (MP3)" else (
            f"bestvideo[height<={q.split('p')[0]}]+bestaudio/best[height<={q.split('p')[0]}]"
            if q.split("p")[0].isdigit() else "bestvideo+bestaudio/best"
        )

        self.btn_dl.configure(state="disabled", text="...")
        self._show_progress()
        self.prog.set(0)
        self.prog_txt.configure(text="starting ...")
        _thread(lambda: self._run_dl(fmt, q))

    def _run_dl(self, fmt, label):
        def tick(p):
            self.after(0, lambda: self.prog.set(p / 100))
            self.after(0, lambda: self.prog_txt.configure(text=f"{p:.1f}%"))

        res = self.engine.download(
            self.current_url, format_id=fmt,
            progress_callback=tick, download_path=self.dl_path
        )
        self.after(0, lambda: self._on_dl(res, label))

    def _on_dl(self, res, label):
        if "error" in res:
            self.prog_txt.configure(text="failed")
            messagebox.showerror("Error", res["error"])
            self._idle()
            return

        title = self.info.get("title", "Unknown") if self.info else "Unknown"
        latest = self._latest(self.dl_path)
        fname = os.path.basename(latest) if latest else ""
        filepath = latest or self.dl_path
        self.tracker.add(title=title, url=self.current_url, quality=label, filename=fname, filepath=filepath)

        self._idle()
        self.prog_txt.configure(text="done \u2713")
        self._show_done_popup(title, filepath, label)

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

    def _clear_hist(self):
        self.tracker.clear()
        self._render_hist()


if __name__ == "__main__":
    app = App()
    app.mainloop()
