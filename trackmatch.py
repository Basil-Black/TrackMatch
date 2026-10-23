#!/usr/bin/env python3
"""
TrackMatch

Five tabs:

  1) Download Missing Tracks
     Give a playlist - a Spotify / Deezer / Apple Music / YouTube link, a
     CSV / .txt / .m3u file, or pasted songs - and a folder to check against.
     Anything not already in that folder can be fetched from YouTube via
     yt-dlp and saved into a chosen destination folder (defaults to the
     same folder you checked against).

  2) Create Playlist
     Give a playlist (any source the Download tab takes) and a music
     folder, then either write an .m3u8
     (paths relative to the playlist's own location, for portability to
     USB drives / Rekordbox / other devices) or copy the matched files
     into a folder. Every track is shown for review - worst matches
     first - before anything is written.

  3) Tagging
     Pick a folder of music and look each file up for cover art, genre,
     label and year - on Beatport (which also has BPM and key), Deezer,
     iTunes, or Auto (Beatport first, then Deezer, then iTunes). Nothing
     is written until you approve it in the review window.

  4) Convert Audio
     Convert a folder of songs between mp3 / m4a / flac / wav / aiff,
     keeping tags and cover art - mainly mp3 for burning CDs, older CDJs
     and car/USB players. Presets for common uses, and an estimate of how
     many audio / MP3 CDs the result fills.

  5) Settings
     Dark/light mode and accent colour, YouTube cookies, the Tagging tab's Large
     Library Mode and minimum match. Saved to a small settings file next
     to this script so your choices persist between runs.

Requirements:
    pip install mutagen yt-dlp requests beautifulsoup4 send2trash

yt-dlp also needs ffmpeg on your PATH for audio extraction/conversion.

Custom title bar icon:
    Put an .ico file (Windows) next to this script and set ICON_PATH below
    to its filename, e.g. ICON_PATH = "my_icon.ico".
"""

import base64
import csv
import ctypes
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from tkinter import colorchooser, filedialog, ttk
from tkinter import font as tkfont

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, APIC, TALB, TBPM, TCON, TDRC, TIT2, TKEY, TPE1, TPUB, TSRC, TXXX
    from mutagen.flac import Picture
    from mutagen.mp4 import MP4Cover, MP4FreeForm, MP4Tags
except ImportError:
    MutagenFile = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    import requests
except ImportError:
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from send2trash import send2trash
except Exception:  # not installed, or failed to set itself up on this system - the app must still start
    send2trash = None

TRASH_NAME = "Recycle Bin" if sys.platform == "win32" else "Trash"
FFMPEG_INSTALL_HINT = {"win32": "winget install ffmpeg", "darwin": "brew install ffmpeg"}.get(
    sys.platform, "sudo apt install ffmpeg  (or your distro's package manager)")


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".aac"}

# YouTube's best audio is ~128-160kbps AAC/Opus, so m4a/opus keep that stream
# as-is (no re-encode). mp3 has to be re-encoded, which makes a bigger file but
# can't add quality back - there's no lossless option since YouTube has none.
YT_FORMATS = ["m4a", "mp3", "opus"]
YT_BITRATES = ["320", "256", "192", "128"]  # kbps; only used for mp3
YT_FORMAT_NOTES = {
    "m4a": "m4a: YouTube's original AAC audio, saved without re-encoding - fastest, best quality. "
           "Plays in Rekordbox, Serato, Traktor, iTunes.",
    "mp3": "mp3: re-encoded from YouTube's audio - bigger file, not better quality. "
           "Use it for older CDJs/players that can't read m4a.",
    "opus": "opus: YouTube's original Opus audio, no re-encoding. Not supported by Rekordbox.",
}

DOWNLOAD_WORKERS = 3          # tracks downloaded at once by Download All
YT_AUTO_SEARCH_RESULTS = 10   # results Download All scores per track
YT_AUTO_PICK_MIN = 0.75       # below this, Download All leaves the track for a manual pick

# Set this to an .ico (preferred) or .png file sitting next to this script
# to change the window/title-bar icon. Leave as None to use the default.
ICON_PATH = "icon.ico" #"my_icon.ico"

APP_VERSION = "1.1.1"
REPO_URL = "https://github.com/Basil-Black/TrackMatch"
PROFILE_URL = "https://github.com/Basil-Black"

# Column names that different export tools use for the same data.
# Matched case-insensitively against the CSV header row.
TITLE_COLUMN_CANDIDATES = ["Track Name", "track_name", "name", "Title", "track"]
ARTIST_COLUMN_CANDIDATES = ["Artist Name(s)", "artist_name", "artist", "Artist"]
ALBUM_COLUMN_CANDIDATES = ["Album Name", "album_name", "album", "Album"]
GENRE_COLUMN_CANDIDATES = ["Genres", "genres", "genre", "Genre"]
DURATION_COLUMN_CANDIDATES = ["Duration (ms)", "duration_ms", "Duration_ms", "duration"]

# Suffixes that describe the version/mix of a track but don't identify it -
# these get stripped before comparison since local filenames and Spotify
# titles disagree constantly about whether/how to include them.
MIX_SUFFIX_RE = re.compile(
    r"\((?:[^()]*\b(?:original|extended|radio|club|remix|mix|edit|version|vip|"
    r"instrumental|acoustic|live|clean|explicit|rework|bootleg|edit)\b[^()]*)\)",
    re.IGNORECASE,
)
ARTIST_SPLIT_RE = re.compile(r"[;,/&]|(?:\bfeat\.?\b)|(?:\bft\.?\b)|(?:\bx\b)|(?:\bwith\b)", re.IGNORECASE)
FEAT_RE = re.compile(r"\(?\b(?:feat|ft)\.?\s+[^()]*\)?", re.IGNORECASE)


# ---------- Theme system ----------
# THEME is a single mutable dict that every styled widget reads from at
# creation time AND re-reads whenever retheme_all() runs. Every style_*
# helper below registers a small "reapply" closure in THEMED_WIDGETS, so
# toggling the theme can walk back over every widget - across the main
# window and any open popups - and recolor it in place.

DARK_THEME = {
    "BG": "#1e1e1e",
    "FG": "#e6e6e6",
    "SUBTLE_FG": "#9a9a9a",
    "FOUND_FG": "#8fd19e",
    "MISSING_FG": "#e0a458",
    "ENTRY_BG": "#2b2b2b",
    "BUTTON_BG": "#3c3f41",
    "BUTTON_ACTIVE_BG": "#4a4d4f",
    "TROUGH": "#3c3f41",
    "ROW_BORDER": "#3c3f41",
    "ACCENT": "#5a9bd5",
}

LIGHT_THEME = {
    "BG": "#f4f4f4",
    "FG": "#1a1a1a",
    "SUBTLE_FG": "#5c5c5c",
    "FOUND_FG": "#1e7d34",
    "MISSING_FG": "#a15c00",
    "ENTRY_BG": "#ffffff",
    "BUTTON_BG": "#e2e2e2",
    "BUTTON_ACTIVE_BG": "#d0d0d0",
    "TROUGH": "#d5d5d5",
    "ROW_BORDER": "#c4c4c4",
    "ACCENT": "#2f6fb0",
}

THEME = dict(DARK_THEME)  # active theme - mutated in place, never reassigned
THEME_NAME = "dark"

# Browser to pull YouTube login cookies from, for age-restricted videos.
# "none" (default) means no cookies are sent - most videos work fine without this.
COOKIE_BROWSER_OPTIONS = ["None", "Chrome", "Firefox", "Edge", "Brave", "Opera", "Vivaldi", "Safari"]
COOKIE_BROWSER = "none"  # lowercase internal value, matches yt-dlp's expected browser keys

# Metadata Editor settings (Settings tab). METADATA_MIN_SCORE is how close a
# lookup result must be to count as a confident match.
MIN_MATCH_CHOICES = [0.75, 0.85, 0.95, 1.0]
METADATA_MIN_SCORE = 0.75
LARGE_LIBRARY_MODE = False
# Create Playlist > "From my music library": which songs get included
# (mutated in place, remembered in the settings file).
LIBRARY_INCLUDE = {"other_genres": True, "unknown_genre": True, "unknown_year": True, "unknown_bpm": True}
SHOW_LOG_PANELS = False  # the tabs' log boxes; logs are always written to files either way
COVER_FALLBACK = True    # look a cover up on iTunes when the matched source's cover can't be downloaded
ACCENT_PRESETS = {
    "Purple": "#8b5cf6", "Blue": "#3b82f6", "Teal": "#14b8a6", "Green": "#22c55e",
    "Orange": "#f97316", "Pink": "#ec4899", "Red": "#ef4444",
}
ACCENT_COLOR = ACCENT_PRESETS["Purple"]
SETTING_LISTENERS = []  # zero-arg callables run when a setting is changed from anywhere

THEMED_WIDGETS = []  # list of zero-arg callables that reapply current THEME colors


_NEXT_PRUNE = [1000]


def register(apply_fn):
    """Runs apply_fn now and remembers it so retheme_all() can re-run it later.
    Closures for destroyed widgets (closed review windows...) fail with
    TclError and are dropped - on every retheme, and whenever the list has
    doubled since it was last tidied, so it can't grow without limit."""
    apply_fn()
    THEMED_WIDGETS.append(apply_fn)
    if len(THEMED_WIDGETS) >= _NEXT_PRUNE[0]:
        _prune_themed_widgets()
        _NEXT_PRUNE[0] = max(1000, 2 * len(THEMED_WIDGETS))


def _prune_themed_widgets():
    alive = []
    for apply_fn in THEMED_WIDGETS:
        try:
            apply_fn()
            alive.append(apply_fn)
        except tk.TclError:
            pass  # its widget was destroyed
    THEMED_WIDGETS[:] = alive


def retheme_all():
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    THEME["ACCENT"] = ACCENT_COLOR
    configure_ttk_styles(style)
    style.configure("TProgressbar", background=THEME["ACCENT"], troughcolor=THEME["TROUGH"],
                     bordercolor=THEME["BG"], lightcolor=THEME["ACCENT"], darkcolor=THEME["ACCENT"])
    # Every edge colour is set - clam otherwise draws light default edges
    # around each tab and the page.
    edge = THEME["ROW_BORDER"]
    style.configure("TNotebook", background=THEME["BG"], borderwidth=1, bordercolor=edge,
                    lightcolor=THEME["BG"], darkcolor=THEME["BG"], tabmargins=(12, 8, 12, 0))
    style.configure("TNotebook.Tab", background=THEME["BUTTON_BG"], foreground=THEME["SUBTLE_FG"],
                    padding=(16, 7), borderwidth=1, bordercolor=edge, lightcolor=THEME["BUTTON_BG"],
                    darkcolor=THEME["BUTTON_BG"], focuscolor=THEME["BUTTON_BG"])
    style.map("TNotebook.Tab",
              background=[("selected", THEME["BG"]), ("active", THEME["BUTTON_ACTIVE_BG"])],
              foreground=[("selected", THEME["ACCENT"]), ("active", THEME["FG"])],
              lightcolor=[("selected", THEME["BG"]), ("active", THEME["BUTTON_ACTIVE_BG"])],
              darkcolor=[("selected", THEME["BG"]), ("active", THEME["BUTTON_ACTIVE_BG"])],
              focuscolor=[("selected", THEME["BG"])],
              padding=[("selected", (16, 7))])
    style.configure("Treeview", background=THEME["ENTRY_BG"], fieldbackground=THEME["ENTRY_BG"],
                    foreground=THEME["FG"], rowheight=22, borderwidth=0)
    style.configure("Treeview.Heading", background=THEME["BUTTON_BG"], foreground=THEME["FG"], relief="flat")
    style.map("Treeview", background=[("selected", THEME["ACCENT"])], foreground=[("selected", "#ffffff")])
    style.map("Treeview.Heading", background=[("active", THEME["BUTTON_ACTIVE_BG"])])

    _prune_themed_widgets()


def mix_color(color, other, amount):
    """Blend two #rrggbb colours - amount 0 = color, 1 = other."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def configure_ttk_styles(style):
    """Colours every themed widget from THEME. The "clam" theme is drawn by
    Tk itself, so this looks the same on Windows, macOS and Linux - unlike
    classic Tk buttons (grey native buttons on macOS) and scrollbars (always
    light on Windows)."""
    t = THEME
    accent = t["ACCENT"]
    accent_hover = mix_color(accent, "#ffffff" if THEME_NAME == "dark" else "#000000", 0.15)
    disabled_fg = mix_color(t["SUBTLE_FG"], t["BG"], 0.35)
    common = dict(bordercolor=t["ROW_BORDER"], lightcolor=t["BUTTON_BG"], darkcolor=t["BUTTON_BG"],
                  focuscolor=accent)

    style.configure("TButton", background=t["BUTTON_BG"], foreground=t["FG"], padding=(10, 4), **common)
    style.map("TButton",
              background=[("disabled", t["BG"]), ("pressed", t["BUTTON_ACTIVE_BG"]), ("active", t["BUTTON_ACTIVE_BG"])],
              foreground=[("disabled", disabled_fg)],
              lightcolor=[("active", t["BUTTON_ACTIVE_BG"])], darkcolor=[("active", t["BUTTON_ACTIVE_BG"])])
    style.configure("Accent.TButton", background=accent, foreground="#ffffff", bordercolor=accent,
                    lightcolor=accent, darkcolor=accent, focuscolor="#ffffff", padding=(14, 5))
    style.map("Accent.TButton",
              background=[("disabled", t["BUTTON_BG"]), ("pressed", accent_hover), ("active", accent_hover)],
              foreground=[("disabled", disabled_fg)],
              lightcolor=[("active", accent_hover)], darkcolor=[("active", accent_hover)],
              bordercolor=[("disabled", t["BUTTON_BG"])])
    style.configure("Info.TButton", background=t["BG"], foreground=accent, bordercolor=t["BG"],
                    lightcolor=t["BG"], darkcolor=t["BG"], padding=(2, 0), focuscolor=t["BG"])
    style.map("Info.TButton", background=[("active", t["BUTTON_BG"])], lightcolor=[("active", t["BUTTON_BG"])],
              darkcolor=[("active", t["BUTTON_BG"])])

    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=t["BG"], foreground=t["FG"], indicatorbackground=t["ENTRY_BG"],
                        indicatorforeground="#ffffff", upperbordercolor=t["ROW_BORDER"],
                        lowerbordercolor=t["ROW_BORDER"], focuscolor=t["BG"], indicatormargin=(0, 0, 6, 0))
        style.map(name, background=[("active", t["BG"])],
                  foreground=[("disabled", disabled_fg)],
                  indicatorbackground=[("disabled", t["BG"]), ("selected", accent), ("pressed", t["BUTTON_BG"])],
                  upperbordercolor=[("selected", accent)], lowerbordercolor=[("selected", accent)])
    style.configure("Highlight.TRadiobutton", foreground=t["FOUND_FG"])
    try:
        _install_check_indicator(style)
        style.configure("TCheckbutton", padding=(0, 1))
    except tk.TclError:
        pass  # keep clam's own indicator if images can't be made

    for name in ("TEntry", "TSpinbox"):
        style.configure(name, fieldbackground=t["ENTRY_BG"], foreground=t["FG"], insertcolor=t["FG"],
                        bordercolor=t["ROW_BORDER"], lightcolor=t["ENTRY_BG"], darkcolor=t["ENTRY_BG"],
                        selectbackground=accent, selectforeground="#ffffff", padding=(4, 3))
        style.map(name, fieldbackground=[("disabled", t["BG"]), ("readonly", t["BG"])],
                  foreground=[("disabled", disabled_fg)],
                  bordercolor=[("focus", accent)], lightcolor=[("focus", accent)])
    style.configure("TSpinbox", arrowcolor=t["FG"], background=t["BUTTON_BG"], arrowsize=12)
    style.map("TSpinbox", arrowcolor=[("disabled", disabled_fg)], background=[("disabled", t["BG"])])
    style.configure("Placeholder.TEntry", foreground=t["SUBTLE_FG"])

    style.configure("TMenubutton", background=t["BUTTON_BG"], foreground=t["FG"], arrowcolor=t["FG"],
                    padding=(8, 3), relief="flat", **common)
    style.map("TMenubutton", background=[("disabled", t["BG"]), ("active", t["BUTTON_ACTIVE_BG"])],
              foreground=[("disabled", disabled_fg)], arrowcolor=[("disabled", disabled_fg)])

    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=t["BUTTON_BG"], troughcolor=t["BG"], bordercolor=t["BG"],
                        lightcolor=t["BUTTON_BG"], darkcolor=t["BUTTON_BG"], arrowcolor=t["SUBTLE_FG"],
                        gripcount=0, arrowsize=12)
        style.map(name, background=[("active", t["BUTTON_ACTIVE_BG"]), ("pressed", t["BUTTON_ACTIVE_BG"])])
    style.configure("TSeparator", background=t["ROW_BORDER"])
    style.configure("TFrame", background=t["BG"])


_CHECK_IMAGES = {}
_CHECK_MARK = [(3, 7), (4, 8), (5, 9), (6, 8), (7, 7), (8, 6), (9, 5), (10, 4)]  # a tick, drawn 2px thick


def _draw_check_images():
    """Checkbox indicator images for the current theme/accent: an empty box,
    a ticked accent box, and greyed versions. Redrawn in place on theme
    changes, so every checkbox updates."""
    size = 14
    t = THEME
    variants = {
        "off": (t["ENTRY_BG"], t["SUBTLE_FG"], None),
        "on": (t["ACCENT"], t["ACCENT"], "#ffffff"),
        "off_disabled": (t["BG"], t["ROW_BORDER"], None),
        "on_disabled": (t["ROW_BORDER"], t["ROW_BORDER"], t["BG"]),
    }
    for name, (fill, border, tick) in variants.items():
        img = _CHECK_IMAGES.get(name)
        if img is None:
            img = _CHECK_IMAGES[name] = tk.PhotoImage(width=size, height=size)
        img.put(border, to=(0, 0, size, size))
        img.put(fill, to=(1, 1, size - 1, size - 1))
        if tick:
            for x, y in _CHECK_MARK:
                img.put(tick, to=(x, y, x + 2, y + 2))


def _install_check_indicator(style):
    _draw_check_images()
    if "TM.Checkbutton.indicator" in style.element_names():
        return
    style.element_create("TM.Checkbutton.indicator", "image", _CHECK_IMAGES["off"],
                         ("disabled", "selected", _CHECK_IMAGES["on_disabled"]),
                         ("disabled", _CHECK_IMAGES["off_disabled"]),
                         ("selected", _CHECK_IMAGES["on"]),
                         border=0, sticky="w", width=21)
    style.layout("TCheckbutton", [
        ("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("TM.Checkbutton.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])


def set_theme(name: str):
    global THEME_NAME
    preset = DARK_THEME if name == "dark" else LIGHT_THEME
    THEME.clear()
    THEME.update(preset)
    THEME["ACCENT"] = ACCENT_COLOR
    THEME_NAME = name
    retheme_all()
    save_settings()


def set_cookie_browser(browser_display_name: str):
    global COOKIE_BROWSER
    COOKIE_BROWSER = browser_display_name.lower()
    save_settings()


def set_large_library_mode(enabled: bool):
    global LARGE_LIBRARY_MODE
    LARGE_LIBRARY_MODE = bool(enabled)
    save_settings()
    _notify_setting_listeners()


def set_min_match(value: float):
    global METADATA_MIN_SCORE
    METADATA_MIN_SCORE = min(MIN_MATCH_CHOICES, key=lambda c: abs(c - value))
    save_settings()
    _notify_setting_listeners()


def set_accent_color(color: str):
    global ACCENT_COLOR
    ACCENT_COLOR = color.lower()
    THEME["ACCENT"] = ACCENT_COLOR
    save_settings()
    retheme_all()


def set_cover_fallback(enabled: bool):
    global COVER_FALLBACK
    COVER_FALLBACK = bool(enabled)
    save_settings()


def set_show_log_panels(show: bool):
    global SHOW_LOG_PANELS
    SHOW_LOG_PANELS = bool(show)
    save_settings()
    apply_log_panel_visibility()


def set_library_include(key: str, value: bool):
    LIBRARY_INCLUDE[key] = bool(value)
    save_settings()


def _notify_setting_listeners():
    for listener in list(SETTING_LISTENERS):
        try:
            listener()
        except tk.TclError:
            pass  # widget was destroyed


def settings_file_path() -> Path:
    return user_data_dir() / "trackmatch_settings.json"


def load_settings():
    global THEME_NAME, COOKIE_BROWSER, LARGE_LIBRARY_MODE, METADATA_MIN_SCORE, SHOW_LOG_PANELS, ACCENT_COLOR
    global COVER_FALLBACK
    try:
        data = json.loads(settings_file_path().read_text(encoding="utf-8"))
        name = data.get("theme")
        if name in ("dark", "light"):
            THEME_NAME = name
            THEME.clear()
            THEME.update(DARK_THEME if name == "dark" else LIGHT_THEME)
        browser = data.get("cookie_browser")
        if isinstance(browser, str):
            COOKIE_BROWSER = browser.lower()
        LARGE_LIBRARY_MODE = bool(data.get("large_library_mode", False))
        if data.get("min_match") in MIN_MATCH_CHOICES:
            METADATA_MIN_SCORE = data["min_match"]
        SHOW_LOG_PANELS = bool(data.get("show_log_panels", False))
        COVER_FALLBACK = bool(data.get("cover_fallback", True))
        if isinstance(data.get("accent"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", data["accent"]):
            ACCENT_COLOR = data["accent"].lower()
        saved_include = data.get("library_include")
        if isinstance(saved_include, dict):
            for key in LIBRARY_INCLUDE:
                if isinstance(saved_include.get(key), bool):
                    LIBRARY_INCLUDE[key] = saved_include[key]
    except Exception:
        pass  # no settings file yet, or it's malformed - just use the defaults


def save_settings():
    try:
        settings_file_path().write_text(
            json.dumps({"theme": THEME_NAME, "cookie_browser": COOKIE_BROWSER,
                        "large_library_mode": LARGE_LIBRARY_MODE, "min_match": METADATA_MIN_SCORE,
                        "library_include": LIBRARY_INCLUDE, "show_log_panels": SHOW_LOG_PANELS,
                        "accent": ACCENT_COLOR, "cover_fallback": COVER_FALLBACK}),
            encoding="utf-8"
        )
    except Exception:
        pass  # not critical if this fails - just means the choice won't persist


# ---------- Style helpers (each registers itself for re-theming) ----------

def style_window(window):
    register(lambda: window.configure(bg=THEME["BG"]))


def style_label(widget, subtle=False):
    register(lambda: widget.configure(bg=THEME["BG"], fg=THEME["SUBTLE_FG"] if subtle else THEME["FG"]))


def style_status_label(widget, kind="normal"):
    """kind: 'found', 'missing', or 'normal'/'subtle'."""
    def apply():
        if kind == "found":
            fg = THEME["FOUND_FG"]
        elif kind == "missing":
            fg = THEME["MISSING_FG"]
        elif kind == "subtle":
            fg = THEME["SUBTLE_FG"]
        else:
            fg = THEME["FG"]
        widget.configure(bg=THEME["BG"], fg=fg)
    register(apply)


def style_frame(widget):
    register(lambda: widget.configure(bg=THEME["BG"]))


def style_canvas(widget):
    register(lambda: widget.configure(bg=THEME["BG"], highlightthickness=0))


def style_row_frame(widget):
    """A bordered row (used in review lists) - background + border color."""
    register(lambda: widget.configure(bg=THEME["BG"], highlightbackground=THEME["ROW_BORDER"]))


def style_radiobutton(widget, highlighted=False):
    if isinstance(widget, ttk.Widget):
        if highlighted:
            wrap = re.match(r"W(\d+)\.", str(widget.cget("style")))
            name = "Highlight.TRadiobutton"
            if wrap:
                name = f"W{wrap.group(1)}.{name}"
                ttk.Style().configure(name, wraplength=int(wrap.group(1)), justify="left")
            widget.configure(style=name)
        return

    def apply():
        fg = THEME["FOUND_FG"] if highlighted else THEME["FG"]
        widget.configure(bg=THEME["BG"], fg=fg, selectcolor=THEME["ENTRY_BG"], activebackground=THEME["BG"],
                          activeforeground=fg, highlightthickness=0)
    register(apply)


def style_scrolledtext(widget):
    register(lambda: widget.configure(bg=THEME["ENTRY_BG"], fg=THEME["FG"], insertbackground=THEME["FG"],
                                       relief="flat", highlightthickness=0, selectbackground=THEME["ACCENT"],
                                       selectforeground="#ffffff"))


def style_listbox(widget):
    register(lambda: widget.configure(bg=THEME["ENTRY_BG"], fg=THEME["FG"], relief="flat", highlightthickness=0,
                                      selectbackground=THEME["ACCENT"], selectforeground="#ffffff"))


# ---------- Themed widgets (same look on Windows, macOS and Linux) ----------
# Classic Tk buttons ignore colours on macOS, and Tk's own scrollbars and
# disabled text boxes are drawn light on Windows whatever the theme - so every
# interactive widget is a ttk widget, coloured centrally by
# configure_ttk_styles(). These wrappers take the classic options used in
# this file and drop or translate the ones ttk doesn't have.

_TK_ONLY_OPTIONS = {"bg", "fg", "background", "foreground", "activebackground", "activeforeground", "relief",
                    "highlightthickness", "highlightbackground", "highlightcolor", "selectcolor", "bd",
                    "borderwidth", "font", "anchor", "justify", "indicatoron", "wraplength"}


def _wrapping_style(base, wraplength):
    """A derived style ("W580.TRadiobutton") whose text wraps - ttk buttons
    have no wraplength option of their own, but their label element does."""
    name = f"W{int(wraplength)}.{base}"
    ttk.Style().configure(name, wraplength=int(wraplength), justify="left")
    return name


def _ttk_kwargs(kw, base):
    wrap = kw.get("wraplength")
    for key in list(kw):
        if key in _TK_ONLY_OPTIONS:
            kw.pop(key)
    if wrap and "style" not in kw:
        kw["style"] = _wrapping_style(base, wrap)
    return kw


class ThemedButton(ttk.Button):
    """accent=True makes it the tab's main action button (accent colour)."""
    def __init__(self, master=None, accent=False, **kw):
        kw = _ttk_kwargs(kw, "TButton")
        kw.setdefault("style", "Accent.TButton" if accent else "TButton")
        super().__init__(master, **kw)


class ThemedCheckbutton(ttk.Checkbutton):
    def __init__(self, master=None, **kw):
        super().__init__(master, **_ttk_kwargs(kw, "TCheckbutton"))


class ThemedRadiobutton(ttk.Radiobutton):
    def __init__(self, master=None, **kw):
        super().__init__(master, **_ttk_kwargs(kw, "TRadiobutton"))


class ThemedEntry(ttk.Entry):
    """ttk.Entry with an optional grey placeholder shown while it's empty
    (placeholder text is never returned by get())."""
    def __init__(self, master=None, placeholder="", **kw):
        super().__init__(master, **_ttk_kwargs(kw, "TEntry"))
        self._placeholder = placeholder
        self._showing_placeholder = False
        if placeholder:
            self.bind("<FocusIn>", lambda e: self._hide_placeholder(), add="+")
            self.bind("<FocusOut>", lambda e: self._show_placeholder(), add="+")
            self._show_placeholder()

    def _show_placeholder(self):
        if self._placeholder and not super().get() and str(self.cget("state")) != "disabled":
            self._showing_placeholder = True
            super().insert(0, self._placeholder)
            self.configure(style="Placeholder.TEntry")

    def _hide_placeholder(self):
        if self._showing_placeholder:
            self._showing_placeholder = False
            super().delete(0, "end")
            self.configure(style="TEntry")

    def get(self):
        return "" if self._showing_placeholder else super().get()

    def insert(self, index, string):
        self._hide_placeholder()
        super().insert(index, string)

    def delete(self, first, last=None):
        if self._showing_placeholder:
            return
        super().delete(first, last)
        if self.focus_get() is not self:
            self.after_idle(self._show_placeholder)


class ThemedSpinbox(ttk.Spinbox):
    def __init__(self, master=None, **kw):
        super().__init__(master, **_ttk_kwargs(kw, "TSpinbox"))


class ThemedOptionMenu(ttk.Menubutton):
    """Drop-in for tk.OptionMenu(master, variable, value, *values, command=)
    - including widget["menu"] returning the dropdown menu."""
    def __init__(self, master, variable, value, *values, command=None, **kw):
        super().__init__(master, textvariable=variable, direction="below", **_ttk_kwargs(kw, "TMenubutton"))
        self.menu = tk.Menu(self, tearoff=0)
        self.configure(menu=self.menu)
        for choice in (value, *values):
            self.menu.add_command(label=choice, command=lambda c=choice: self._choose(variable, c, command))
        register(lambda: self.menu.configure(bg=THEME["ENTRY_BG"], fg=THEME["FG"], activebackground=THEME["ACCENT"],
                                             activeforeground="#ffffff", relief="flat", bd=0))

    @staticmethod
    def _choose(variable, choice, command):
        variable.set(choice)
        if command:
            command(choice)

    def __getitem__(self, key):
        return self.menu if key == "menu" else super().__getitem__(key)


class ThemedScrolledText(tk.Text):
    """Like tkinter.scrolledtext.ScrolledText, but with a themed scrollbar.
    Geometry methods (pack/grid/place...) act on the surrounding frame."""
    def __init__(self, master=None, **kw):
        self.frame = tk.Frame(master)
        style_frame(self.frame)
        self.vbar = ttk.Scrollbar(self.frame, orient="vertical")
        self.vbar.pack(side="right", fill="y")
        kw["yscrollcommand"] = self.vbar.set
        super().__init__(self.frame, **kw)
        self.pack(side="left", fill="both", expand=True)
        self.vbar["command"] = self.yview
        text_methods = vars(tk.Text).keys()
        geometry = vars(tk.Pack).keys() | vars(tk.Grid).keys() | vars(tk.Place).keys()
        for method in geometry - text_methods:
            if method[0] != "_" and method not in ("config", "configure"):
                setattr(self, method, getattr(self.frame, method))


_SCROLL_CANVASES = []
_WHEEL_BOUND = []


def make_wheel_scrollable(canvas):
    """Mouse-wheel / trackpad scrolling for a canvas while the pointer is over
    it, on Windows (<MouseWheel> in steps of 120), macOS (<MouseWheel> in
    small steps) and Linux (<Button-4>/<Button-5>)."""
    _SCROLL_CANVASES[:] = [c for c in _SCROLL_CANVASES if c.winfo_exists()]  # drop closed windows' canvases
    _SCROLL_CANVASES.append(canvas)
    if _WHEEL_BOUND:
        return
    _WHEEL_BOUND.append(True)

    def canvas_under(widget):
        while widget is not None:
            if widget in _SCROLL_CANVASES:
                return widget
            widget = getattr(widget, "master", None)
        return None

    def on_wheel(event):
        target = canvas_under(event.widget)
        if target is None or not target.winfo_exists():
            return
        if getattr(event, "num", None) in (4, 5):
            step = -1 if event.num == 4 else 1
        elif sys.platform == "darwin":
            step = -event.delta
        else:
            step = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        target.yview_scroll(step, "units")

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        canvas.bind_all(sequence, on_wheel, add="+")


def app_base_dir() -> Path:
    """Folder the script/exe lives in - works whether running as a .py or a
    PyInstaller-built .exe (where sys.executable is the exe's own path)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _system_user_dir() -> Path:
    """The usual per-user app data folder for this system."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "TrackMatch"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TrackMatch"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "TrackMatch"


def _is_writable_dir(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".trackmatch_write_test_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


_DATA_DIR = []


def user_data_dir() -> Path:
    """Where TrackMatch keeps its own files - settings, saved lookups, logs.
    Normally right next to the app, so a TrackMatch folder is portable. A
    macOS .app bundle mustn't be written into, so there it's
    ~/Library/Application Support/TrackMatch; and if the app's folder can't
    be written to (e.g. a protected install location), the system's usual
    per-user folder."""
    if not _DATA_DIR:
        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            candidates = [_system_user_dir()]
        else:
            candidates = [app_base_dir(), _system_user_dir()]
        chosen = next((c for c in candidates if _is_writable_dir(c)), candidates[-1])
        _DATA_DIR.append(chosen)
    return _DATA_DIR[0]


def bundled_file(name: str) -> Path:
    """A file shipped with the app (e.g. icon.png): inside the PyInstaller
    bundle if it was added there, otherwise next to the app."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle and (Path(bundle) / name).is_file():
        return Path(bundle) / name
    return app_base_dir() / name


# ---------- Session log files ----------

LOG_RETENTION_DAYS = 30
LOG_SOURCES = {
    "DownloadTab": "Download",
    "CsvPlaylistPanel": "Playlist (from a playlist)",
    "LibraryPlaylistPanel": "Playlist (from library)",
    "MetadataEditorTab": "Tagging",
    "ConvertAudioTab": "Convert",
}


class SessionLog:
    """Everything the tabs log, plus full details of any unexpected error,
    written to one file per app session in a "logs" folder next to the app
    (see user_data_dir), or the system's per-user folder if that isn't writable. The file is
    opened on first use; session files older than LOG_RETENTION_DAYS are
    deleted then."""

    def __init__(self):
        self._lock = threading.Lock()
        self._file = None
        self.folder = None
        self.path = None

    def _candidate_folders(self):
        yield user_data_dir() / "logs"
        yield _system_user_dir() / "logs"

    def _open(self):
        if self._file is not None:
            return
        started = datetime.now()
        for folder in self._candidate_folders():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"trackmatch-{started:%Y-%m-%d_%H-%M-%S}.log"
                self._file = open(path, "a", encoding="utf-8")
                self.folder, self.path = folder, path
                break
            except OSError:
                continue
        if self._file is None:
            self._file = False  # nowhere writable - carry on without file logging rather than retry every line
            return
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        for old in self.folder.glob("trackmatch-*.log"):
            try:
                if old != self.path and old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    def write(self, source, message):
        with self._lock:
            self._open()
            if not self._file:
                return
            stamp = datetime.now().strftime("%H:%M:%S")
            lines = str(message).strip("\n").splitlines() or [""]
            try:
                for line in lines:
                    self._file.write(f"{stamp} [{source}] {line}\n")
                self._file.flush()
            except (OSError, ValueError):
                pass

    def exception(self, source, exc_type, exc, tb):
        details = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
        self.write(source, f"Unexpected error:\n{details}")

    def logs_folder(self):
        with self._lock:
            self._open()
        return self.folder


SESSION_LOG = SessionLog()


def log_source_name(obj):
    return LOG_SOURCES.get(type(obj).__name__, type(obj).__name__)


def setup_fonts(root):
    """The system's own UI font everywhere (Segoe UI on Windows - Tk's default
    there is an old one; macOS and Linux already use theirs), plus the
    named fonts headings use."""
    default = tkfont.nametofont("TkDefaultFont")
    if sys.platform == "win32":
        default.configure(family="Segoe UI", size=10)
    family, size = default.cget("family"), default.cget("size")
    for name in ("TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name).configure(family=family, size=size)
        except tk.TclError:
            pass
    existing = set(tkfont.names(root))
    for name, options in (("TMHeading", dict(size=abs(size) + 2, weight="bold")),
                          ("TMSubheading", dict(size=abs(size), weight="bold")),
                          ("TMSmall", dict(size=max(8, abs(size) - 2)))):
        if name in existing:
            tkfont.nametofont(name).configure(family=family, **options)
        else:
            tkfont.Font(root, name=name, family=family, **options)


class KeepAwake:
    """Stops the computer going to sleep while a long job runs - scans,
    applying tags, Download All, converting, copying playlists. The screen
    can still turn off and lock as normal. Nestable and shared across
    threads: awake while at least one job is running.
      Windows: SetThreadExecutionState, held by a small helper thread (the
               setting belongs to the thread that made it).
      macOS:   `caffeinate -i` while TrackMatch is busy.
      Linux:   `systemd-inhibit` where available.
    Anything unavailable is skipped silently - the job still runs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._count = 0
        self._release = None

    def __enter__(self):
        with self._lock:
            self._count += 1
            if self._count == 1:
                try:
                    self._release = self._hold()
                except Exception:
                    self._release = None
        return self

    def __exit__(self, *exc):
        with self._lock:
            self._count -= 1
            if self._count == 0 and self._release:
                try:
                    self._release()
                except Exception:
                    pass
                self._release = None
        return False

    @staticmethod
    def _hold():
        if sys.platform == "win32":
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            done = threading.Event()

            def holder():
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                done.wait()
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

            threading.Thread(target=holder, name="keep-awake", daemon=True).start()
            return done.set
        if sys.platform == "darwin":
            cmd = ["caffeinate", "-i", "-w", str(os.getpid())]
        elif shutil.which("systemd-inhibit"):
            cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=TrackMatch",
                   "--why=TrackMatch is working", "sleep", "infinity"]
        else:
            return None
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.terminate


KEEP_AWAKE = KeepAwake()


def install_error_logging(app):
    """Sends full details of otherwise-unseen errors - uncaught ones, ones in
    background threads, and ones in Tk callbacks - to the session log, then
    lets the normal handling carry on (printing to the console when there
    is one)."""
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def excepthook(exc_type, exc, tb):
        SESSION_LOG.exception("Error", exc_type, exc, tb)
        previous_hook(exc_type, exc, tb)

    def thread_hook(args):
        name = args.thread.name if args.thread else "thread"
        SESSION_LOG.exception(f"Error in {name}", args.exc_type, args.exc_value, args.exc_traceback)
        previous_thread_hook(args)

    def tk_callback_hook(exc_type, exc, tb):
        SESSION_LOG.exception("Error", exc_type, exc, tb)
        if sys.stderr:
            traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    app.report_callback_exception = tk_callback_hook


LOG_PANELS = []  # [(widget, its pack options), ...] for every tab's log label/box


def register_log_panel(*widgets):
    """Remembers where these (already packed) log widgets go, then shows or
    hides them per the "Show log panels" setting. They must be the last
    things packed in their container, so re-packing puts them back in place."""
    for widget in widgets:
        LOG_PANELS.append((widget, widget.pack_info()))
        if not SHOW_LOG_PANELS:
            widget.pack_forget()


def apply_log_panel_visibility():
    for widget, info in LOG_PANELS:
        try:
            if SHOW_LOG_PANELS:
                widget.pack(**info)
            else:
                widget.pack_forget()
        except tk.TclError:
            pass  # widget was destroyed


def open_folder(folder: Path):
    if sys.platform == "win32":
        os.startfile(folder)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


def enable_dark_titlebar(window):
    """Windows-only: asks DWM to draw this window's title bar dark or light
    to match the current theme. Silently does nothing on other platforms or
    older Windows builds."""
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if THEME_NAME == "dark" else 0)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                break
    except Exception:
        pass


def set_window_icon(window, icon_path):
    """Sets a custom title-bar/taskbar icon if ICON_PATH points at a real file."""
    if not icon_path:
        return
    name = Path(icon_path)
    if name.suffix.lower() == ".ico" and sys.platform != "win32":
        name = name.with_suffix(".png")  # macOS / Linux can't use .ico - icon.png is shipped alongside it
    path = name if name.is_absolute() else bundled_file(str(name))
    if not path.is_file():
        return
    try:
        if path.suffix.lower() == ".ico" and sys.platform == "win32":
            window.iconbitmap(str(path))
        else:
            img = tk.PhotoImage(file=str(path))
            window.iconphoto(True, img)
            window._icon_image_ref = img
    except Exception:
        pass


# ---------- Core matching logic ----------

def strip_accents(text: str) -> str:
    """Convert accented characters to their ASCII equivalent instead of dropping them
    (e.g. accented o -> plain o, not deleted entirely)."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def normalize(text: str) -> str:
    text = strip_accents(text)
    text = text.lower()
    text = FEAT_RE.sub(" ", text)
    text = MIX_SUFFIX_RE.sub(" ", text)
    text = re.sub(r"\[.*?\]", " ", text)
    text = re.sub(r"^\s*\d+[\s.\-_]+", " ", text)  # leading track numbers like "01 - "
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sanitize_filename(name: str) -> str:
    """Strips characters that aren't valid in Windows/Mac/Linux filenames.
    Also swaps ';' for ', ' - some CD burning software doesn't handle
    semicolons well in filenames, even though we keep ';' in the actual
    metadata tags for multi-artist tracks."""
    name = name.replace(";", ", ")
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "")
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Untitled"


def sanitize_playlist_name(name: str) -> str:
    """sanitize_filename for playlist and folder names: '/' becomes ' - ', so
    "Rap/Hip Hop" saves as "Rap - Hip Hop" rather than "RapHip Hop". Song
    file names keep the plain rule, so "AC/DC" doesn't become "AC - DC"."""
    return sanitize_filename(name.replace("/", " - "))


def token_sort(text: str) -> str:
    """Word order shouldn't matter (e.g. filename is 'Title - Artist' instead of
    'Artist - Title') so compare sorted word sets as a second pass."""
    return " ".join(sorted(text.split()))


def split_artists(text: str) -> set:
    """Splits a multi-artist string ('A;B', 'A, B', 'A feat. B') into a set of
    normalized individual names, so ordering/delimiter differences don't matter."""
    parts = ARTIST_SPLIT_RE.split(text)
    return {normalize(p) for p in parts if normalize(p)}


def best_ratio(a: str, b: str) -> float:
    """Max of direct comparison and word-order-independent comparison."""
    direct = difflib.SequenceMatcher(None, a, b).ratio()
    sorted_ratio = difflib.SequenceMatcher(None, token_sort(a), token_sort(b)).ratio()
    return max(direct, sorted_ratio)


# Below these, a high title+artist score still isn't the same song: a
# featured artist alone matching ("Bodyrox ft. Luciana" vs a different
# "Yeah Yeah" Luciana sang on), or the same artist with a loosely similar
# title ("The Game of Love" vs "One More Time").
SAME_SONG_TITLE_MIN = 0.8
SAME_SONG_ARTIST_MIN = 0.85
# A playlist song's best guess at least this good counts as already in the folder
FOUND_THRESHOLD = 0.75


def primary_artist(text: str) -> str:
    """The first-named artist, normalized - 'Bodyrox ft. Luciana' -> 'bodyrox'."""
    return next((n for n in (normalize(p) for p in ARTIST_SPLIT_RE.split(text)) if n), "")


def is_same_song(target_title, target_artist, candidate_title, candidate_artist_set) -> bool:
    """Whether a candidate can be the song at all: its title is close and it
    credits the song's main artist (not just a featured one). Titles are
    normalized; target_artist is the raw artist string."""
    main = primary_artist(target_artist)
    return (best_ratio(target_title, candidate_title) >= SAME_SONG_TITLE_MIN
            and bool(main)
            and max((best_ratio(main, b) for b in candidate_artist_set), default=0.0) >= SAME_SONG_ARTIST_MIN)


def find_column(fieldnames, candidates):
    """Case-insensitive match of a candidate column name against the CSV header."""
    lower_map = {f.lower().strip(): f for f in fieldnames}
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return None


def read_playlist_csv(csv_path: Path, log):
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row / appears empty.")

        title_col = find_column(reader.fieldnames, TITLE_COLUMN_CANDIDATES)
        artist_col = find_column(reader.fieldnames, ARTIST_COLUMN_CANDIDATES)
        album_col = find_column(reader.fieldnames, ALBUM_COLUMN_CANDIDATES)
        genre_col = find_column(reader.fieldnames, GENRE_COLUMN_CANDIDATES)
        duration_col = find_column(reader.fieldnames, DURATION_COLUMN_CANDIDATES)

        if not title_col or not artist_col:
            raise ValueError(
                f"Couldn't find title/artist columns in CSV. Found headers: {reader.fieldnames}\n"
                f"Expected something like 'Track Name' and 'Artist Name(s)' "
                f"(this is what an Exportify export looks like)."
            )

        tracks = []
        for row in reader:
            title = (row.get(title_col) or "").strip()
            artist = (row.get(artist_col) or "").strip()
            album = (row.get(album_col) or "").strip() if album_col else ""
            genre = (row.get(genre_col) or "").strip() if genre_col else ""
            duration_ms = None
            if duration_col:
                raw = (row.get(duration_col) or "").strip()
                if raw.isdigit():
                    duration_ms = int(raw)
            if title:
                tracks.append({
                    "title": title, "artist": artist, "album": album, "genre": genre,
                    "duration_ms": duration_ms,
                })

    log(f"Playlist CSV has {len(tracks)} tracks.")
    return tracks


def read_local_metadata(filepath: Path):
    title, artist = "", ""
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio and audio.tags:
            title = (audio.tags.get("title") or [""])[0]
            artist = (audio.tags.get("artist") or [""])[0]
    except Exception:
        pass

    if not title:
        stem = filepath.stem
        if " - " in stem:
            guessed_artist, guessed_title = stem.split(" - ", 1)
            title = title or guessed_title
            artist = artist or guessed_artist
        else:
            title = stem

    return title, artist


def read_existing_tags(filepath: Path):
    """Reads whatever Title/Artist/Album/Genre tags already exist on a file -
    used by the Metadata Editor as a fallback for any field Beatport doesn't
    supply, so applying a match never blanks out good existing data. Falls
    back to filename parsing for title/artist only, same as
    read_local_metadata - album/genre have no meaningful filename fallback."""
    title, artist, album, genre = "", "", "", ""
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio and audio.tags:
            title = (audio.tags.get("title") or [""])[0]
            artist = (audio.tags.get("artist") or [""])[0]
            album = (audio.tags.get("album") or [""])[0]
            genre = (audio.tags.get("genre") or [""])[0]
    except Exception:
        pass

    if not title:
        stem = filepath.stem
        if " - " in stem:
            guessed_artist, guessed_title = stem.split(" - ", 1)
            title = title or guessed_title
            artist = artist or guessed_artist
        else:
            title = stem

    return {"title": title, "artist": artist, "album": album, "genre": genre}


# Words too common to narrow down a match ("the", "remix", single letters...).
MATCH_STOPWORDS = {"the", "a", "an", "and", "of", "in", "on", "to", "my", "me", "you", "is", "it", "feat", "ft",
                   "vs", "mix", "remix", "edit", "original", "version", "radio", "extended", "club", "remastered"}


def _match_words(norm_title, artist_set):
    words = set(norm_title.split())
    for artist in artist_set:
        words.update(artist.split())
    return {w for w in words if len(w) > 1 and w not in MATCH_STOPWORDS}


class MusicIndex(list):
    """The audio files in a music folder with their normalized title/artist,
    plus a word -> files lookup, so matching only compares a song against
    files sharing at least one word with it instead of every file in the
    folder (the slow part with big libraries)."""

    def __init__(self, entries):
        super().__init__(entries)
        self.postings = {}
        for i, entry in enumerate(self):
            for word in _match_words(entry["norm_title"], entry["artist_set"]):
                self.postings.setdefault(word, []).append(i)

    def candidates(self, norm_title, artist_set):
        ids = set()
        for word in _match_words(norm_title, artist_set):
            ids.update(self.postings.get(word, ()))
        # Nothing in common with any file: fall back to comparing against everything.
        return [self[i] for i in ids] if ids else self


# Title/artist read from each file this session, keyed by path and reused
# while the file's size and modified time are unchanged - re-running a
# playlist against the same folder only re-reads new or changed files.
_FILE_TAG_CACHE = {}
_FILE_TAG_LOCK = threading.Lock()


def index_music_folder(music_dir: Path, log):
    log(f"Scanning {music_dir} for audio files...")
    all_files = [p for p in music_dir.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS]
    log(f"Found {len(all_files)} audio files. Reading metadata...")
    counts = {"done": 0, "read": 0}

    def entry_for(filepath):
        try:
            st = filepath.stat()
        except OSError:
            return None
        key = str(filepath)
        with _FILE_TAG_LOCK:
            cached = _FILE_TAG_CACHE.get(key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            title, artist = cached[2], cached[3]
        else:
            title, artist = read_local_metadata(filepath)
            with _FILE_TAG_LOCK:
                _FILE_TAG_CACHE[key] = (st.st_size, st.st_mtime_ns, title, artist)
                counts["read"] += 1
        with _FILE_TAG_LOCK:
            counts["done"] += 1
            if counts["done"] % 500 == 0:
                log(f"  ...{counts['done']}/{len(all_files)}")
        return {"path": filepath, "title": title, "artist": artist,
                "norm_title": normalize(title), "artist_set": split_artists(artist)}

    # Reading tags is mostly waiting on the disk, so several at once is much faster.
    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = [e for e in pool.map(entry_for, all_files) if e]
    reused = len(entries) - counts["read"]
    if reused:
        log(f"  {reused} unchanged since the last scan this session - only {counts['read']} read again.")
    return MusicIndex(entries)


def score_match(target_title, target_artist_set, candidate):
    title_score = best_ratio(target_title, candidate["norm_title"])

    if target_artist_set and candidate["artist_set"]:
        # Best pairwise match between any target artist and any candidate artist -
        # handles multi-artist CSV strings vs a file only tagged with one artist.
        artist_score = max(
            (best_ratio(a, b) for a in target_artist_set for b in candidate["artist_set"]),
            default=0.0,
        )
    else:
        artist_score = 0.0

    # Title and artist weighted equally - a title-only match (e.g. two different
    # songs that happen to share a title) is not enough to score highly on its own.
    return (title_score * 0.5) + (artist_score * 0.5)


def find_best_match(track, index):
    norm_title = normalize(track["title"])
    target_artist_set = split_artists(track["artist"])
    pool = index.candidates(norm_title, target_artist_set) if isinstance(index, MusicIndex) else index

    best_candidate, best_score = None, 0.0
    for candidate in pool:
        score = score_match(norm_title, target_artist_set, candidate)
        if not is_same_song(norm_title, track["artist"], candidate["norm_title"], candidate["artist_set"]):
            # Can still be offered as a guess, but never counts as found
            score = min(score, FOUND_THRESHOLD - 0.05)
        if score > best_score:
            best_candidate, best_score = candidate, score

    return best_candidate, best_score


def write_m3u(output_path: Path, matches):
    """Writes paths RELATIVE to output_path's own folder (forward slashes, for
    cross-platform / Rekordbox compatibility). Keep the .m3u8 and music folder
    together (e.g. both copied onto the USB as a unit) for these to resolve."""
    base_dir = output_path.parent.resolve()
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for m in matches:
            try:
                rel_path = os.path.relpath(Path(m["path"]).resolve(), base_dir)
            except ValueError:
                # Music on a different drive than the playlist (e.g. playlist on
                # the USB, songs on C:) - no relative path exists, so use the full one.
                rel_path = str(Path(m["path"]).resolve())
            rel_path = rel_path.replace("\\", "/")
            f.write(f"#EXTINF:-1,{m['artist']} - {m['title']}\n")
            f.write(f"{rel_path}\n")


def copy_files_to_folder(dest_dir: Path, matches, log):
    """Copies each matched file into dest_dir (flat - no subfolders). If two
    matches would produce the same filename, a ' (2)', ' (3)' etc. suffix is
    added so nothing gets silently overwritten."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    used_names = set()
    copied = 0

    for i, m in enumerate(matches, 1):
        src = Path(m["path"])
        target_name = src.name
        stem, suffix = src.stem, src.suffix
        counter = 2
        while target_name in used_names:
            target_name = f"{stem} ({counter}){suffix}"
            counter += 1
        used_names.add(target_name)

        dest = dest_dir / target_name
        try:
            shutil.copy2(src, dest)
            copied += 1
        except Exception as e:
            log(f"  Could not copy \"{src.name}\": {e}")

        if i % 25 == 0:
            log(f"  ...copied {i}/{len(matches)}")

    return copied


# ---------- YouTube search / download (yt-dlp) ----------

def format_duration(seconds):
    """Formats a duration in seconds as m:ss. Returns '?' for None/0."""
    if not seconds:
        return "?"
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class _YtDlpLogger:
    """Routes yt-dlp's internal log lines into our GUI log callback."""
    def __init__(self, log):
        self.log = log

    def debug(self, msg):
        pass  # yt-dlp sends a lot of noisy debug lines - skip them

    # Warnings yt-dlp repeats on every single video - shown once per session.
    _ONCE_MARKERS = ("JavaScript runtime",)
    _shown_once = set()

    def warning(self, msg):
        marker = next((m for m in self._ONCE_MARKERS if m in msg), None)
        if marker:
            if marker in _YtDlpLogger._shown_once:
                return
            _YtDlpLogger._shown_once.add(marker)
        self.log(f"  [yt-dlp] {msg}")

    def error(self, msg):
        self.log(f"  [yt-dlp] ERROR: {msg}")


# Browser cookies are read once per browser and reused. Reading them means
# decrypting the browser's whole cookie store (a second or two, and Chrome
# locks it while open), which yt-dlp would otherwise redo on every search
# and download.
_COOKIE_CACHE = {"browser": None, "jar": None}
_COOKIE_LOCK = threading.Lock()


def _cached_cookiejar():
    """The cookie jar for the browser picked in Settings, or None if cookies
    are off. Loaded on first use and cached until the browser changes."""
    if not COOKIE_BROWSER or COOKIE_BROWSER == "none":
        return None
    with _COOKIE_LOCK:
        if _COOKIE_CACHE["browser"] != COOKIE_BROWSER:
            from yt_dlp.cookies import extract_cookies_from_browser
            try:
                jar = extract_cookies_from_browser(COOKIE_BROWSER)
            except Exception as e:
                raise RuntimeError(
                    f"Couldn't read {COOKIE_BROWSER.title()} cookies ({e}). Close the browser and try "
                    f"again, or pick another browser / None in Settings."
                ) from e
            _COOKIE_CACHE.update(browser=COOKIE_BROWSER, jar=jar)
        return _COOKIE_CACHE["jar"]


def make_ydl(ydl_opts):
    """A YoutubeDL instance using the cached browser cookies (if any).
    YoutubeDL.cookiejar is a functools.cached_property, so putting the jar
    in the instance dict up front is exactly what it would cache itself -
    just without re-reading the browser each time."""
    ydl = yt_dlp.YoutubeDL(ydl_opts)
    jar = _cached_cookiejar()
    if jar is not None:
        ydl.__dict__["cookiejar"] = jar
    return ydl


def search_youtube(query: str, max_results: int = 5):
    """Returns up to max_results dicts: {'title','uploader','duration',
    'duration_seconds','url','thumbnail'}. Does not download anything.
    Uses flat extraction: one request for the whole results page instead of
    opening every video's page to resolve its formats (several times faster,
    and nothing here needs the formats)."""
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                "extract_flat": "in_playlist"}
    with make_ydl(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)

    results = []
    for entry in info.get("entries", []) or []:
        if not entry:
            continue
        thumbnails = entry.get("thumbnails") or []
        results.append({
            "title": entry.get("title", "Unknown title"),
            "uploader": entry.get("channel") or entry.get("uploader") or "Unknown channel",
            "duration": format_duration(entry.get("duration")),
            "duration_seconds": entry.get("duration"),
            "url": entry.get("webpage_url") or entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
            "thumbnail": thumbnails[-1].get("url", "") if thumbnails else "",  # yt-dlp sorts smallest -> largest
        })
    return results


# Words that mark a YouTube upload as the wrong version of a track, unless
# the playlist's own title has them too (then it's the version wanted).
YT_UNWANTED_WORDS = ["live", "cover", "sped up", "slowed", "nightcore", "reverb", "karaoke",
                     "instrumental", "remix", "8d", "hour", "reaction", "acapella", "bass boosted"]


def score_youtube_result(track, result) -> float:
    """0..1 guess at how likely a YouTube result is the playlist track:
    title and artist similarity plus - weighted highest - how close the
    video length is to the playlist length (Exportify includes it), with a
    bonus for YouTube Music "Artist - Topic" / "Official Audio" uploads and
    a penalty for live/cover/sped-up/remix-style versions."""
    track_title = normalize(track["title"])
    yt_title = result["title"] or ""
    channel = result.get("uploader") or ""
    is_topic = channel.endswith(" - Topic")
    channel_name = channel[:-len(" - Topic")] if is_topic else channel
    yt_norm = normalize(yt_title)
    haystack = normalize(f"{yt_title} {channel_name}")

    if track_title and track_title in yt_norm:
        title_score = 1.0
    else:
        title_score = best_ratio(track_title, yt_norm)

    artists = split_artists(track.get("artist", ""))
    if not artists:
        artist_score = 0.5
    elif any(a in haystack for a in artists):
        artist_score = 1.0
    else:
        artist_score = max(best_ratio(a, normalize(channel_name)) for a in artists)

    target = (track["duration_ms"] / 1000) if track.get("duration_ms") else None
    secs = result.get("duration_seconds")
    if target and secs:
        diff = abs(secs - target)
        duration_score = 1.0 if diff <= 3 else max(0.0, 1.0 - (diff - 3) / 27)
    else:
        duration_score = 0.5  # unknown - neither helps nor rules it out

    score = title_score * 0.35 + artist_score * 0.25 + duration_score * 0.4

    yt_lower = yt_title.lower()
    wanted_lower = track["title"].lower()
    if is_topic:
        score += 0.08
    elif "official audio" in yt_lower:
        score += 0.05
    if artists and any(best_ratio(a, normalize(channel_name)) >= 0.9 for a in artists):
        score += 0.03  # uploaded by the artist's own channel
    for word in YT_UNWANTED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", yt_lower) and word not in wanted_lower:
            score -= 0.25

    # Extra words beyond the artist/title (TV show names, "| 4Music",
    # "Fresh On Fridays"...) suggest a re-upload or a performance rather
    # than the release itself.
    expected_words = set(normalize(f"{track.get('artist', '')} {track['title']}").split()) | YT_NEUTRAL_WORDS
    extra_words = [w for w in yt_norm.split() if w not in expected_words]
    score -= min(0.15, 0.02 * len(extra_words))

    # Not clamped to 1.0 - rank_youtube_results sorts on the raw value so the
    # bonuses still separate results that all match perfectly. Clamp for display.
    return max(0.0, score)


# Words that say nothing about which version an upload is.
YT_NEUTRAL_WORDS = {"official", "audio", "video", "music", "lyrics", "lyric", "hd", "hq", "visualizer",
                    "visualiser", "full", "version", "original", "feat", "ft", "and", "the", "x"}


def rank_youtube_results(track, results):
    """Returns [(score, result), ...] best first."""
    return sorted(((score_youtube_result(track, r), r) for r in results), key=lambda sr: sr[0], reverse=True)


# yt-dlp format selector per output format. m4a/opus ask for YouTube's own
# AAC/Opus stream so FFmpegExtractAudio just copies it into the container
# (no re-encode, near-instant); mp3 always has to be re-encoded.
YT_FORMAT_SELECTORS = {
    "m4a": "bestaudio[ext=m4a]/bestaudio/best",
    "opus": "bestaudio[acodec=opus]/bestaudio/best",
    "mp3": "bestaudio/best",
}


def glob_escape(text: str) -> str:
    """Escapes [ ] * ? so a filename can be used literally in Path.glob."""
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def _partial_download_files(dest_dir: Path, safe_name: str):
    """yt-dlp's leftovers from an interrupted download - only .part/.ytdl,
    never anything that could be a finished file."""
    return [p for p in dest_dir.glob(f"{glob_escape(safe_name)}.*") if p.suffix.lower() in (".part", ".ytdl")]


def download_youtube_audio(video_url: str, dest_dir: Path, fmt: str, bitrate: str, desired_name: str, log,
                           progress=None, cancel_event=None):
    """Downloads audio via yt-dlp into dest_dir as "<desired_name>.<fmt>"
    (named after the playlist's Artist - Title, not the video title).
    Tags/cover are NOT written here - see fetch_youtube_track. progress, if
    given, is called with short status strings ("Downloading 42% ...");
    setting cancel_event aborts the download.

    Returns (final Path, already_existed). If the target file already
    exists nothing is downloaded."""
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(desired_name)
    final_path = dest_dir / f"{safe_name}.{fmt}"
    if final_path.exists():
        return final_path, True

    last_pct = [-1]

    def progress_hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Cancelled")
        if progress is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                pct = int(done * 100 / total)
                if pct == last_pct[0]:
                    return  # only update the GUI when the whole-number percent changes
                last_pct[0] = pct
                speed = d.get("speed")
                speed_text = f" at {speed / 1_000_000:.1f} MB/s" if speed else ""
                progress(f"Downloading {pct}%{speed_text}")
        elif d.get("status") == "finished":
            progress("Converting to mp3..." if fmt == "mp3" else "Finishing...")

    extract_pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
    if fmt == "mp3":
        extract_pp["preferredquality"] = bitrate

    ydl_opts = {
        "format": YT_FORMAT_SELECTORS.get(fmt, "bestaudio/best"),
        "outtmpl": str(dest_dir / f"{safe_name}.%(ext)s"),
        "postprocessors": [extract_pp],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logger": _YtDlpLogger(log),
        "progress_hooks": [progress_hook],
        "retries": 10,
        "fragment_retries": 10,
        # Download in 10MB ranges - YouTube throttles long single-connection
        # downloads, and chunking keeps each request under that limit.
        "http_chunk_size": 10 * 1024 * 1024,
    }

    try:
        for attempt in range(2):
            try:
                with make_ydl(ydl_opts) as ydl:
                    ydl.extract_info(video_url, download=True)
                break
            except yt_dlp.utils.DownloadError as e:
                # YouTube sometimes refuses one download link (HTTP 403) - trying
                # again fetches fresh links, which usually works.
                if attempt == 0 and "403" in str(e):
                    log("  YouTube refused the download (403) - retrying once...")
                    time.sleep(2)
                    continue
                raise
    except yt_dlp.utils.DownloadCancelled:
        for leftover in _partial_download_files(dest_dir, safe_name):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise

    if not final_path.exists():
        # Fallback in case yt-dlp/ffmpeg produced a slightly different name
        candidates = sorted(dest_dir.glob(f"{glob_escape(safe_name)}*.{fmt}"))
        if candidates:
            final_path = candidates[0]
        else:
            raise RuntimeError(f"Download finished but couldn't locate the output file (expected {final_path.name}).")

    return final_path, False


def _no_window_flags():
    """Stops a console window flashing up for ffmpeg in the windowed .exe."""
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def youtube_cover_art(thumbnail_url: str):
    """Downloads a YouTube thumbnail and centre-crops it to a square JPEG,
    since thumbnails are 16:9 (Topic/official-audio uploads are the album
    art with bars either side, which the crop removes). Returns
    (bytes, mime) or (None, None)."""
    image_bytes, mime = download_image(thumbnail_url, any_format=True)
    if not image_bytes:
        return None, None
    tmp_dir = Path(tempfile.mkdtemp(prefix="trackmatch_"))
    try:
        src = tmp_dir / "thumb"
        out = tmp_dir / "cover.jpg"
        src.write_bytes(image_bytes)
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src),
             "-vf", "crop='min(iw,ih)':'min(iw,ih)'", "-frames:v", "1", "-q:v", "2", str(out)],
            check=True, timeout=30, creationflags=_no_window_flags(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return out.read_bytes(), "image/jpeg"
    except Exception:
        # ffmpeg missing/failed - use the uncropped thumbnail if players can read it
        return (image_bytes, mime) if mime in ("image/jpeg", "image/png") else (None, None)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def fetch_youtube_track(track, video, dest_dir: Path, dl_opts, log, progress=None, cancel_event=None):
    """Downloads one YouTube result for a playlist track and tags it in a
    single write: the playlist's own Title/Artist/Album/Genre, plus cover
    art. If dl_opts.metadata_source is set (Auto / Beatport / Deezer /
    iTunes), a confident match from it replaces those with its tags and
    release cover, as in the Metadata Editor. Otherwise the cover is the
    square-cropped YouTube thumbnail. Either way the hidden source tags
    record where the metadata came from.

    Returns (Path, short status text for the GUI)."""
    desired_name = f"{track['artist']} - {track['title']}"
    path, existed = download_youtube_audio(
        video["url"], dest_dir, dl_opts.fmt, dl_opts.bitrate, desired_name, log, progress, cancel_event
    )
    if existed:
        log(f"  Already exists, skipped download: {path.name}")
        return path, f"Already in folder: {path.name}"

    if progress:
        progress("Tagging...")
    fields = {"title": track["title"], "artist": track["artist"],
              "album": track.get("album", ""), "genre": track.get("genre", ""),
              **source_tag_values("YouTube + playlist", video.get("url", ""))}
    image_bytes, image_mime = None, None
    note = ""

    if dl_opts.metadata_source:
        length = (track["duration_ms"] / 1000) if track.get("duration_ms") else None
        results, tried = lookup_metadata(dl_opts.metadata_source, track["artist"], track["title"], length, log)
        best = results[0] if results else None
        if best and best["score"] >= METADATA_MIN_SCORE:
            fields = merge_metadata_fields(best, fields)
            image_bytes, image_mime = fetch_cover_art(best, log)
            note = f" + {best['source']} tags"
            log(f"  {best['source']} match for \"{path.name}\": {best['artists']} - {best['title']} "
                f"({best['genre'] or 'no genre'})")
        else:
            note = f" (no confident match on {', '.join(tried)} - playlist tags used)"

    if not image_bytes:
        image_bytes, image_mime = youtube_cover_art(video.get("thumbnail", ""))

    try:
        embed_full_metadata(path, fields, image_bytes, image_mime)
    except Exception as e:
        log(f"  Downloaded, but couldn't tag \"{path.name}\": {e}")
        return path, f"Downloaded (tagging failed: {e}): {path.name}"

    log(f"  Downloaded: {path.name}")
    return path, f"Downloaded{note}: {path.name}"


# ---------- Beatport metadata lookup ----------
# Beatport has no public API, so this reads the __NEXT_DATA__ JSON blob
# embedded in their search page's HTML (the same technique used by the
# standalone Beatport Cover Finder tool). Catalog is dance/electronic
# focused - other genres will mostly come back with no results.

BEATPORT_SEARCH_URL = "https://www.beatport.com/search/tracks?q={query}"
BEATPORT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


_HTTP = {"session": None}
_HTTP_LOCK = threading.Lock()


def http_get(url, **kwargs):
    """requests.get through one shared session, so repeat requests to the same
    site reuse the open connection - about 3x faster than connecting afresh
    every time. Safe to use from several threads at once."""
    with _HTTP_LOCK:
        if _HTTP["session"] is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update(BEATPORT_HEADERS)
            _HTTP["session"] = session
    kwargs.setdefault("timeout", 15)
    return _HTTP["session"].get(url, **kwargs)
BEATPORT_COVER_SIZE = 500  # px, square - Beatport's CDN resizes the release artwork to this

# A lookup result (from any source) has to score at least METADATA_MIN_SCORE
# (the Settings tab's "Minimum match") against the file's own title/artist to
# be pre-selected in the review window. Anything lower is shown for manual
# picking but defaults to "don't change", so Apply All never writes another
# song's cover/genre into a file.

# Beatport tags same-named artists with a disambiguator, e.g. "FISHER (OZ)" -
# stripped before comparing against local tags, which never have it.
ARTIST_DISAMBIGUATOR_RE = re.compile(r"\s*\([^()]*\)")
BEATPORT_ARTIST_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")  # only a trailing one, when writing names


def _find_beatport_track_list(node):
    """Recursively looks for the first list of dicts that look like
    Beatport track objects - identified by the presence of a 'track_name'
    field, confirmed against a real response - rather than assuming a
    specific wrapper key name like 'tracks'. Beatport's actual shape is a
    flat list directly under state.data['data'], with no 'tracks' nesting."""
    if isinstance(node, list):
        if node and isinstance(node[0], dict) and "track_name" in node[0]:
            return node
        for item in node:
            result = _find_beatport_track_list(item)
            if result is not None:
                return result
    elif isinstance(node, dict):
        for value in node.values():
            result = _find_beatport_track_list(value)
            if result is not None:
                return result
    return None


def _beatport_name(value, *keys):
    """Beatport nests names inconsistently: genre is a LIST of
    {'genre_name'} dicts, sub_genre/label/release are single dicts, and
    some fields are plain strings. Returns the first non-empty name from
    whichever shape it is, or ""."""
    if isinstance(value, list):
        for v in value:
            name = _beatport_name(v, *keys)
            if name:
                return name
        return ""
    if isinstance(value, dict):
        for k in keys + ("name",):
            if value.get(k):
                return str(value[k]).strip()
        return ""
    return value.strip() if isinstance(value, str) else ""


def beatport_key_to_tag(key_name: str) -> str:
    """'G Major' -> 'G', 'A Minor' -> 'Am', 'F# Minor' -> 'F#m' - the
    short form ID3 TKEY expects and Rekordbox/Serato/Traktor all read."""
    key_name = (key_name or "").replace("♯", "#").replace("♭", "b").strip()
    if not key_name:
        return ""
    parts = key_name.split()
    note = parts[0]
    mode = parts[1].lower() if len(parts) > 1 else ""
    return note + ("m" if mode.startswith("min") else "")


def _parse_beatport_track(t):
    artist_names = []
    for a in t.get("artists") or []:
        name = a.get("artist_name", "").strip() if isinstance(a, dict) else ""
        # "FISHER (OZ)" -> "FISHER": Beatport's disambiguator isn't part of the name.
        name = BEATPORT_ARTIST_SUFFIX_RE.sub("", name).strip()
        # Beatport sometimes lists the same artist twice
        if name and name.lower() not in (n.lower() for n in artist_names):
            artist_names.append(name)

    base_title = (t.get("track_name") or "").strip()
    mix_name = (t.get("mix_name") or "").strip()
    # Beatport keeps the mix ("Extended Mix", "Fisher Rework") separate from
    # the track name - put it back in brackets the way DJ libraries expect,
    # unless the track name already includes it.
    if mix_name and mix_name.lower() not in base_title.lower():
        full_title = f"{base_title} ({mix_name})"
    else:
        full_title = base_title

    # Genre first, sub-genre ('Dance', 'Peak Time' etc.) only as a fallback.
    genre = (_beatport_name(t.get("genre"), "genre_name")
             or _beatport_name(t.get("sub_genre"), "sub_genre_name", "genre_name"))

    release_val = t.get("release")
    release_dict = release_val if isinstance(release_val, dict) else {}

    # Only the RELEASE artwork is real cover art. The track-level
    # track_image_uri is a 1500x250 waveform PNG, so it's deliberately never
    # used as a fallback - no cover is better than a waveform as a cover.
    image_url = release_dict.get("release_image_dynamic_uri") or release_dict.get("release_image_uri") or ""
    if image_url:
        size = str(BEATPORT_COVER_SIZE)
        image_url = image_url.replace("{w}", size).replace("{h}", size)
        image_url = re.sub(r"/image_size/\d+x\d+/", f"/image_size/{size}x{size}/", image_url)
        if image_url.startswith("//"):
            image_url = "https:" + image_url

    # release_date is when the release actually came out; publish_date is when
    # it went up on Beatport, which for older catalogue can be decades later.
    release_date = (t.get("release_date") or t.get("publish_date") or "")[:10]
    bpm = t.get("bpm")
    track_id = t.get("track_id")
    length_ms = t.get("length")

    return {
        "source": "Beatport",
        "title": full_title or "Unknown title",
        "base_title": base_title,
        "mix_name": mix_name,
        "artists": ", ".join(artist_names) or "Unknown artist",
        "artist_list": artist_names or ["Unknown artist"],
        "label": _beatport_name(t.get("label"), "label_name"),
        "genre": genre,
        "release": _beatport_name(release_val, "release_name"),
        "date": release_date,
        "year": release_date[:4],
        "bpm": str(round(bpm)) if isinstance(bpm, (int, float)) and bpm > 0 else "",
        "key": beatport_key_to_tag(t.get("key_name") or ""),
        "isrc": (t.get("isrc") or "").strip(),
        "length": length_ms / 1000 if isinstance(length_ms, (int, float)) and length_ms > 0 else None,
        "image_url": image_url,
        "url": f"https://www.beatport.com/track/-/{track_id}" if track_id else "",
    }


def _fetch_beatport_tracks(query: str, dbg):
    url = BEATPORT_SEARCH_URL.format(query=requests.utils.quote(query))
    for attempt in range(4):
        _BEATPORT_LIMITER.wait()
        resp = http_get(url, headers=BEATPORT_HEADERS, timeout=15)
        dbg(f"GET {url} -> HTTP {resp.status_code}, {len(resp.text)} bytes")
        # Refused/overloaded (e.g. after many searches in a row) - back off and retry
        # rather than reporting "None found" for the rest of a big scan.
        if (resp.status_code in (403, 429) or resp.status_code >= 500) and attempt < 3:
            time.sleep(5 * (attempt + 1))
            continue
        break
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        dbg("No <script id=\"__NEXT_DATA__\"> tag found - Beatport's page structure may have changed.")
        return []
    try:
        data = json.loads(script_tag.string)
    except (json.JSONDecodeError, TypeError) as e:
        dbg(f"JSON parse failed: {e}")
        return []

    track_list = _find_beatport_track_list(data)
    if not track_list:
        dbg("No track list found in the page JSON.")
        return []
    return track_list


def score_metadata_result(artist: str, title: str, result, length=None) -> float:
    """0..1 similarity between a local file's artist/title and a lookup
    result from any source, using the same title+artist weighting as
    playlist matching, plus:
      - a bonus/penalty for the mix name, so 'X (Extended Mix)' on disk
        prefers the Extended over the Radio Edit or a remix
      - a bonus/penalty for track length when both are known (the file's
        own length, or the playlist's), which is what really tells a
        radio edit from an extended mix."""
    candidate = {
        "norm_title": normalize(result["base_title"]),
        "artist_set": split_artists(ARTIST_DISAMBIGUATOR_RE.sub("", result["artists"])),
    }
    target_artists = split_artists(ARTIST_DISAMBIGUATOR_RE.sub("", artist or ""))
    # Title alone can't tell apart different songs with the same name, so
    # without a real artist match the result is never confident - the user
    # picks by hand. Applied last, after the bonuses below.
    never_confident = False
    if target_artists:
        score = score_match(normalize(title), target_artists, candidate)
        # e.g. a cover, a cast recording, "Killer Queen" vs "Queen", or a
        # different song that only shares a featured artist
        never_confident = not is_same_song(normalize(title), ARTIST_DISAMBIGUATOR_RE.sub("", artist or ""),
                                           candidate["norm_title"], candidate["artist_set"])
    else:
        # Nothing to compare artists against (untagged file with no
        # "Artist - Title" filename).
        score = best_ratio(normalize(title), candidate["norm_title"])
        never_confident = True

    local_mix = " ".join(m.strip("() ").lower() for m in MIX_SUFFIX_RE.findall(title or "")).strip()
    remote_mix = result["mix_name"].lower().strip()
    local_is_remix = bool(local_mix) and not is_original_version(local_mix)
    remote_is_remix = bool(remote_mix) and not is_original_version(remote_mix)
    mix_similarity = difflib.SequenceMatcher(None, local_mix, remote_mix).ratio() if local_mix and remote_mix else 0.0
    if remote_is_remix and not local_is_remix:
        # File doesn't name a remix/live/alternate version, so this is the
        # wrong recording - its title, cover and BPM would be wrong too.
        score -= 0.1
        never_confident = True
    elif local_is_remix and mix_similarity < 0.6:
        # File IS a specific remix, and this isn't it.
        never_confident = True
    elif not local_mix and not remote_is_remix:
        # No version named on the file - the original is the likeliest one.
        score += 0.03
    if local_mix and remote_mix:
        score += 0.1 * (mix_similarity - 0.5)

    # Length separates a radio edit from an extended mix of the same
    # recording - a smaller signal than being the wrong recording entirely.
    remote_length = result.get("length")
    if length and remote_length:
        diff = abs(remote_length - length)
        if diff <= 3:
            score += 0.03
        elif diff > 30:
            score -= 0.08
        elif diff > 10:
            score -= 0.04
    if never_confident:
        score = min(score, METADATA_MIN_SCORE - 0.05)
    # Deliberately not clamped to 1.0 here - rank_metadata_results sorts on
    # the raw value so these bonuses still break ties between perfect matches.
    return max(0.0, score)


# Words in a version name that still mean "the song itself" - "Extended
# Mix", "Radio Edit", "Remastered 2011", "Original Club Mix". Any other word
# makes it a remix or alternate take ("Simon Mills Full Sugar Mix", "Live
# from...", "Chilled Mix", "Intro").
ORIGINAL_VERSION_WORDS = {
    "original", "extended", "radio", "club", "main", "album", "single", "version", "mix", "edit", "vocal",
    "full", "length", "remaster", "remastered", "ov", "clean", "explicit", "mono", "stereo",
}


def is_original_version(mix_name: str) -> bool:
    return all(w in ORIGINAL_VERSION_WORDS for w in re.findall(r"[a-z]+", mix_name.lower()))


def rank_metadata_results(artist, title, results, length=None):
    """Scores results in place and sorts them best-first. Equal scores (the
    same song on its original release, compilations, remix bundles...) go
    to the earliest release - the original, whose cover and genre are the
    ones people expect. Scores are clamped to 1.0 after sorting."""
    for r in results:
        r["score"] = score_metadata_result(artist, title, r, length)
    results.sort(key=lambda r: (-round(r["score"], 3), r["date"] or "9999"))
    for r in results:
        r["score"] = min(1.0, r["score"])
    return results


def search_beatport(artist: str, title: str, max_results: int = 5, log=None, length=None):
    """Searches Beatport and returns up to max_results result dicts, ranked
    best-first by how well each matches the given artist/title/length (each
    dict has a 'score' 0..1 - see score_metadata_result - plus 'source',
    'title', 'artists', 'artist_list', 'label', 'genre', 'release', 'date',
    'year', 'bpm', 'key', 'isrc', 'length', 'image_url', 'url'). Beatport's
    own ordering is by popularity, not closeness, so its first hit is often
    a different track entirely. Returns [] if nothing was found or the page
    couldn't be parsed - callers should treat that as "None found"."""
    if requests is None or BeautifulSoup is None:
        raise RuntimeError("Missing dependency. Run: pip install requests beautifulsoup4")

    def dbg(msg):
        if log:
            log(f"    [beatport debug] {msg}")

    query = f"{artist} {title}".strip()
    track_list = _fetch_beatport_tracks(query, dbg)
    if not track_list:
        # Retry without mix suffixes / feat. credits / punctuation, which
        # sometimes stop Beatport's search matching at all.
        clean_query = f"{normalize(artist)} {normalize(title)}".strip()
        if clean_query and clean_query != query.lower():
            track_list = _fetch_beatport_tracks(clean_query, dbg)

    results = [_parse_beatport_track(t) for t in track_list if isinstance(t, dict)]
    rank_metadata_results(artist, title, results, length)
    if results:
        dbg(f"{len(track_list)} results, best match {results[0]['score']:.2f}: "
            f"{results[0]['artists']} - {results[0]['title']}")
    return results[:max_results]


# ---------- Deezer / iTunes metadata lookup ----------
# Both are free public APIs with no account or key. Deezer allows ~50
# requests per 5 seconds; Apple asks for ~20 per minute, so iTunes is kept
# last in Auto mode and throttled hard.

DEEZER_API = "https://api.deezer.com"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_COUNTRY = "US"       # which iTunes store to search - the US store has the widest catalogue
ITUNES_COVER_SIZE = 1400    # px, square - Apple's CDN renders the artwork at the requested size
DEEZER_ENRICH_TOP = 1       # genre/label/date need 2 extra requests per result - the best one now, others when applied


class _RateLimiter:
    """Spaces calls at least min_interval seconds apart across all threads."""
    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = time.monotonic() + self.min_interval


_BEATPORT_LIMITER = _RateLimiter(0.4)  # ~2.5 searches/s, gentle on a site with no official API
_DEEZER_LIMITER = _RateLimiter(0.15)   # ~33 per 5s, under Deezer's 50
_ITUNES_LIMITER = _RateLimiter(3.1)    # ~19 per minute, under Apple's ~20


def _get_json(url, params, limiter, service):
    """GET a JSON API with rate limiting, backing off and retrying (up to 3
    times) when the service says it's overloaded - HTTP 429/403/5xx, or
    Deezer's 'quota exceeded' error, which comes back as HTTP 200."""
    if requests is None:
        raise RuntimeError("Missing dependency. Run: pip install requests")
    for attempt in range(4):
        limiter.wait()
        resp = http_get(url, params=params, headers=BEATPORT_HEADERS, timeout=15)
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
        resp.raise_for_status()
        data = resp.json()
        error = data.get("error") if isinstance(data, dict) else None
        if error and error.get("code") == 4 and attempt < 3:  # Deezer: quota limit exceeded
            time.sleep(5 * (attempt + 1))
            continue
        return data
    raise RuntimeError(f"{service} kept refusing requests - try again in a few minutes")


# The same version words in square brackets - iTunes often writes remixes as
# "Yeah Yeah (feat. Luciana) [D. Ramirez Vocal Club Mix]"
MIX_SUFFIX_BRACKET_RE = re.compile(MIX_SUFFIX_RE.pattern.replace(r"\(", r"\[").replace(r"\)", r"\]")
                                   .replace("[^()]", r"[^\[\]]"), re.IGNORECASE)


def _split_title_version(title):
    """'Blinding Lights (Remix)' -> ('Blinding Lights', 'Remix'); also '[Remix]'."""
    title = title or ""
    versions = MIX_SUFFIX_RE.findall(title) + MIX_SUFFIX_BRACKET_RE.findall(title)
    base = MIX_SUFFIX_BRACKET_RE.sub("", MIX_SUFFIX_RE.sub("", title)).strip()
    return base or title, " ".join(v.strip("()[] ") for v in versions)


def _parse_deezer_track(t):
    album = t.get("album") or {}
    base_title = (t.get("title_short") or t.get("title") or "").strip()
    mix_name = (t.get("title_version") or "").strip().strip("()").strip()
    if not mix_name:
        base_title, mix_name = _split_title_version(t.get("title") or base_title)
    artist_name = ((t.get("artist") or {}).get("name") or "").strip()
    return {
        "source": "Deezer",
        "title": (t.get("title") or base_title).strip() or "Unknown title",
        "base_title": base_title,
        "mix_name": mix_name,
        "artists": artist_name or "Unknown artist",
        "artist_list": [artist_name] if artist_name else ["Unknown artist"],
        "label": "",
        "genre": "",
        "release": (album.get("title") or "").strip(),
        "date": "",
        "year": "",
        "bpm": "",
        "key": "",
        "isrc": (t.get("isrc") or "").strip(),
        "length": t.get("duration") or None,
        "image_url": album.get("cover_xl") or album.get("cover_big") or "",
        "url": t.get("link") or "",
        "_track_id": t.get("id"),
        "_album_id": album.get("id"),
    }


def _enrich_deezer_result(r):
    """Search results don't carry genre, label, date or the full artist list
    - those come from the track and album endpoints."""
    if r.get("_track_id"):
        track = _get_json(f"{DEEZER_API}/track/{r['_track_id']}", None, _DEEZER_LIMITER, "Deezer")
        if not track.get("error"):
            names = []
            for c in track.get("contributors") or []:
                name = (c.get("name") or "").strip()
                if name and name.lower() not in (n.lower() for n in names):
                    names.append(name)
            if names:
                r["artist_list"] = names
                r["artists"] = ", ".join(names)
            r["date"] = (track.get("release_date") or "")[:10]
            bpm = track.get("bpm")
            if isinstance(bpm, (int, float)) and bpm > 0:
                r["bpm"] = str(round(bpm))
    if r.get("_album_id"):
        album = _get_json(f"{DEEZER_API}/album/{r['_album_id']}", None, _DEEZER_LIMITER, "Deezer")
        if not album.get("error"):
            genres = (album.get("genres") or {}).get("data") or []
            r["genre"] = (genres[0].get("name") or "").strip() if genres else ""
            r["label"] = (album.get("label") or "").strip()
            r["date"] = r["date"] or (album.get("release_date") or "")[:10]
            r["image_url"] = album.get("cover_xl") or r["image_url"]
    r["year"] = r["date"][:4]
    r["_enriched"] = True


def search_deezer(artist: str, title: str, max_results: int = 5, log=None, length=None):
    """Same contract as search_beatport, from Deezer's catalogue (strong on
    mainstream and European releases; 1000px covers; genre is per album and
    broad, e.g. 'Pop', 'Rap/Hip Hop', 'Dance')."""
    query = f"{artist} {title}".strip()
    data = _get_json(f"{DEEZER_API}/search", {"q": query, "limit": 15}, _DEEZER_LIMITER, "Deezer")
    items = data.get("data") or []
    if not items:
        clean_query = f"{normalize(artist)} {normalize(title)}".strip()
        if clean_query and clean_query != query.lower():
            data = _get_json(f"{DEEZER_API}/search", {"q": clean_query, "limit": 15}, _DEEZER_LIMITER, "Deezer")
            items = data.get("data") or []

    results = rank_metadata_results(artist, title, [_parse_deezer_track(t) for t in items], length)[:max_results]
    for r in results[:DEEZER_ENRICH_TOP]:
        try:
            _enrich_deezer_result(r)
        except Exception as e:
            if log:
                log(f"    Deezer details lookup failed: {e}")
    # Enrichment can change the artist list/date, so re-rank the final set.
    return rank_metadata_results(artist, title, results, length)


def _parse_itunes_track(t):
    track_name = (t.get("trackName") or "").strip()
    base_title, mix_name = _split_title_version(track_name)
    artist_name = (t.get("artistName") or "").strip()
    release = re.sub(r"\s+-\s+(Single|EP)$", "", (t.get("collectionName") or "").strip())
    release_date = (t.get("releaseDate") or "")[:10]
    artwork = t.get("artworkUrl100") or ""
    if artwork:
        artwork = re.sub(r"/\d+x\d+bb\.", f"/{ITUNES_COVER_SIZE}x{ITUNES_COVER_SIZE}bb.", artwork)
    millis = t.get("trackTimeMillis")
    return {
        "source": "iTunes",
        "title": track_name or "Unknown title",
        "base_title": base_title,
        "mix_name": mix_name,
        # iTunes gives all artists as one string ("A & B") - kept whole, since
        # splitting on "&" would break names like "Simon & Garfunkel".
        "artists": artist_name or "Unknown artist",
        "artist_list": [artist_name] if artist_name else ["Unknown artist"],
        "label": "",
        "genre": (t.get("primaryGenreName") or "").strip(),
        "release": release,
        "date": release_date,
        "year": release_date[:4],
        "bpm": "",
        "key": "",
        "isrc": "",
        "length": millis / 1000 if millis else None,
        "image_url": artwork,
        "url": (t.get("trackViewUrl") or "").split("?")[0],
    }


def search_itunes(artist: str, title: str, max_results: int = 5, log=None, length=None):
    """Same contract as search_beatport, from Apple's iTunes Store catalogue
    (strong on mainstream pop / hip-hop / R&B / country; one broad genre per
    track; 1400px covers; no label/BPM/key)."""
    params = {"term": f"{artist} {title}".strip(), "media": "music", "entity": "song",
              "limit": 15, "country": ITUNES_COUNTRY}
    data = _get_json(ITUNES_SEARCH_URL, params, _ITUNES_LIMITER, "iTunes")
    items = [t for t in data.get("results") or [] if t.get("kind") == "song"]
    return rank_metadata_results(artist, title, [_parse_itunes_track(t) for t in items], length)[:max_results]


def find_itunes_album_art(artist: str, album: str):
    """Artwork URL for an album on iTunes, or "" if no album there clearly
    matches both the artist and the album name."""
    def core_name(name):
        # Drop "(Legacy Edition)", "[Remastered]", " - Single" etc. - the same
        # album is often labelled differently between stores.
        name = re.sub(r"\s+-\s+(Single|EP)$", "", name or "")
        return normalize(re.sub(r"[(\[][^()\[\]]*[)\]]", " ", name))

    target_album = core_name(album)
    main_artist = re.split(r",|&", artist or "")[0].strip()
    params = {"term": f"{main_artist} {target_album}".strip(), "media": "music", "entity": "album",
              "limit": 10, "country": ITUNES_COUNTRY}
    data = _get_json(ITUNES_SEARCH_URL, params, _ITUNES_LIMITER, "iTunes")
    target_artists = split_artists(artist)
    for a in data.get("results") or []:
        album_ok = best_ratio(target_album, core_name(a.get("collectionName"))) >= 0.85
        artist_ok = any(best_ratio(t, c) >= 0.85 for t in target_artists
                        for c in split_artists(a.get("artistName") or ""))
        if album_ok and artist_ok and a.get("artworkUrl100"):
            return re.sub(r"/\d+x\d+bb\.", f"/{ITUNES_COVER_SIZE}x{ITUNES_COVER_SIZE}bb.", a["artworkUrl100"])
    return ""


_BLOCKED_IMAGE_HOSTS = set()
# iTunes artwork found for an album this session: (artist, album) -> (url, album shown) - many
# songs share an album, and each iTunes lookup costs ~3s because of Apple's rate limit.
_ITUNES_ART_CACHE = {}
_COVER_LOCK = threading.Lock()


def fetch_cover_art(result, log=None):
    """Downloads a lookup result's cover. If that fails - Deezer's image
    server refuses some connections outright (HTTP 403) even though its
    API works - and "Find missing covers on iTunes" is on, falls back to the
    same album's artwork on iTunes (remembered per album for the session).
    Returns (bytes, mime) or (None, None)."""
    url = result.get("image_url", "")
    host = urlparse(url).netloc if url else ""
    image_bytes, image_mime = download_image(url)
    if image_bytes or result.get("source") == "iTunes" or not result.get("release") or not COVER_FALLBACK:
        return image_bytes, image_mime
    if host in _BLOCKED_IMAGE_HOSTS and log and host not in _COVER_NOTICES:
        _COVER_NOTICES.add(host)
        log(f"    {result['source']}'s image server is refusing downloads on this connection - using iTunes "
            f"artwork instead (slower: iTunes allows ~20 lookups a minute). You can turn this off in Settings.")

    album_core = re.sub(r"[(\[][^()\[\]]*[)\]]", " ", result["release"])  # "After Hours (Deluxe)" = "After Hours"
    key = (normalize(re.split(r",|&", result.get("artists", ""))[0]), normalize(album_core))
    with _COVER_LOCK:
        cached = _ITUNES_ART_CACHE.get(key)
    if cached is not None:
        art_url, where = cached
    else:
        try:
            art_url = find_itunes_album_art(result.get("artists", ""), result["release"])
            where = f"\"{result['release']}\""
            if not art_url:
                # The song may sit on a differently-named album there - look the
                # track itself up and use its release's artwork if it's confident.
                matches = search_itunes(result.get("artists", ""), result.get("base_title") or result["title"],
                                        max_results=1, length=result.get("length"))
                if matches and matches[0]["score"] >= METADATA_MIN_SCORE:
                    art_url, where = matches[0]["image_url"], f"\"{matches[0]['release']}\""
        except Exception:
            return None, None  # network trouble - don't remember a miss
        with _COVER_LOCK:
            _ITUNES_ART_CACHE[key] = (art_url, where)
    if art_url:
        image_bytes, image_mime = download_image(art_url)
        if image_bytes and log:
            log(f"    {result['source']} cover unavailable - used the iTunes artwork for {where}")
    return image_bytes, image_mime


_COVER_NOTICES = set()


METADATA_SEARCHERS = {"Beatport": search_beatport, "Deezer": search_deezer, "iTunes": search_itunes}
METADATA_AUTO = "Auto"
METADATA_AUTO_ORDER = ["Beatport", "Deezer", "iTunes"]
METADATA_SOURCE_LABELS = {
    METADATA_AUTO: "Auto (Beatport, then Deezer, then iTunes)",
    "Beatport": "Beatport only (electronic - adds BPM & key)",
    "Deezer": "Deezer only",
    "iTunes": "iTunes only",
}


def lookup_metadata(source, artist, title, length=None, log=None, max_results=6, errors=None):
    """Looks a track up on one source, or in Auto mode on Beatport, then
    Deezer, then iTunes - stopping at the first source with a confident
    match (score >= METADATA_MIN_SCORE), so electronic tracks keep
    Beatport's BPM/key and the slow iTunes API is only used for leftovers.

    Returns (results best-first across every source tried, [sources tried]).
    A source that errors (network down, site refusing...) is logged and
    skipped rather than failing the lookup - and added to `errors` if a list
    is passed, so callers know the answer may be incomplete."""
    order = METADATA_AUTO_ORDER if source == METADATA_AUTO else [source]
    results, tried = [], []
    for name in order:
        tried.append(name)
        try:
            found = METADATA_SEARCHERS[name](artist, title, max_results=max_results, length=length)
        except Exception as e:
            if log:
                log(f"    {name} lookup failed: {e}")
            if errors is not None:
                errors.append(name)
            found = []
        results.extend(found)
        if found and found[0]["score"] >= METADATA_MIN_SCORE:
            break
    results.sort(key=lambda r: (-round(r["score"], 3), order.index(r["source"])))
    return results[:max_results], tried


def merge_metadata_fields(best, existing):
    """Tag fields for a lookup result: its value wins for each field when it
    has one, otherwise `existing` (a dict of title/artist/album/genre - the
    file's current tags or the playlist's data) is kept, so applying a
    match never blanks out good data. Label/date/BPM/key/ISRC only come
    from lookups - empty just leaves that tag untouched. Also adds the
    hidden source tags (see SOURCE_TAG_FIELDS) recording where it came from."""
    return {
        "title": best.get("title") or existing.get("title", ""),
        "artist": best.get("artist_list") or ([existing["artist"]] if existing.get("artist") else []),
        "album": best.get("release") or existing.get("album", ""),
        "genre": best.get("genre") or existing.get("genre", ""),
        "label": best.get("label", ""),
        "date": best.get("date", ""),
        "year": best.get("year", ""),
        "bpm": best.get("bpm", ""),
        "key": best.get("key", ""),
        "isrc": best.get("isrc", ""),
        **source_tag_values(best.get("source", ""), best.get("url", "")),
    }


# Hidden tags recording where a file's metadata came from, so files can
# later be picked out by source (e.g. everything tagged from Beatport).
# Custom fields: TXXX frames in ID3, plain comments in FLAC/Ogg, freeform
# atoms in m4a - DJ software doesn't display any of them.
SOURCE_TAG_FIELDS = {"source": "TRACKMATCH_SOURCE", "source_url": "TRACKMATCH_URL",
                     "tagged_on": "TRACKMATCH_TAGGED"}


def source_tag_values(source, url=""):
    return {"source": source, "source_url": url, "tagged_on": date.today().isoformat()}


def sniff_image_mime(data):
    """Identifies the image type from its bytes rather than trusting the
    server's Content-Type header - a wrong mime in the cover tag makes
    Rekordbox/Explorer show no artwork. Returns None if it isn't a
    JPEG/PNG (e.g. an HTML error page), since players only reliably
    support those two."""
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def _make_flac_picture(image_bytes, image_mime):
    pic = Picture()
    pic.data = image_bytes
    pic.type = 3  # front cover
    pic.mime = image_mime
    pic.desc = "Cover"
    return pic


def _as_list(value):
    """Tag values as a list - the artist field is already a list (one entry
    per artist, written as separate tag values), everything else is a string."""
    return list(value) if isinstance(value, list) else [value]


def _set_id3_tags(tags, fields, image_bytes, image_mime):
    frame_map = [
        ("title", TIT2), ("artist", TPE1), ("album", TALB), ("genre", TCON),
        ("label", TPUB), ("year", TDRC), ("bpm", TBPM), ("key", TKEY), ("isrc", TSRC),
    ]
    for field, frame_cls in frame_map:
        value = fields.get(field)
        if value:
            tags.setall(frame_cls.__name__, [frame_cls(encoding=3, text=_as_list(value))])
    for field, name in SOURCE_TAG_FIELDS.items():
        if fields.get(field):
            tags.delall(f"TXXX:{name}")
            tags.add(TXXX(encoding=3, desc=name, text=[fields[field]]))
    if image_bytes:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=image_mime, type=3, desc="Cover", data=image_bytes))


def _set_vorbis_tags(tags, fields):
    """FLAC / Ogg / Opus comments. Label and key get written under both
    common names, since different DJ apps read different ones."""
    key_map = {
        "title": ["title"], "artist": ["artist"], "album": ["album"], "genre": ["genre"],
        "label": ["organization", "label"], "date": ["date"], "bpm": ["bpm"],
        "key": ["initialkey", "key"], "isrc": ["isrc"],
    }
    for field, name in SOURCE_TAG_FIELDS.items():
        key_map[field] = [name.lower()]
    for field, keys in key_map.items():
        value = fields.get(field)
        if value:
            for k in keys:
                tags[k] = _as_list(value)


def embed_full_metadata(file_path: Path, fields: dict, image_bytes, image_mime: str):
    """Writes the text tags in `fields` (title, artist, album, genre, label,
    date, year, bpm, key, isrc, plus the hidden source/source_url/tagged_on
    fields - empty/missing values leave that tag alone; artist is a list,
    saved as one tag value per artist)
    plus front-cover art if image_bytes is given. Format-specific, since
    cover art isn't a simple key=value tag: ID3 APIC for mp3/wav/aiff,
    Picture block for flac, METADATA_BLOCK_PICTURE for ogg/opus, covr atom
    for m4a/mp4. Other formats get text tags only. Raises on failure so the
    caller can report it rather than claiming success.

    Returns True if cover art was embedded."""
    if MutagenFile is None:
        raise RuntimeError("mutagen is not installed")

    suffix = file_path.suffix.lower()
    audio = MutagenFile(file_path)
    if audio is None:
        raise RuntimeError("unrecognised audio file")

    if suffix in (".mp3", ".wav", ".aiff", ".aif"):
        if audio.tags is None:
            audio.add_tags()
        _set_id3_tags(audio.tags, fields, image_bytes, image_mime)
        # ID3v2.3 rather than mutagen's default v2.4 - Rekordbox, Windows
        # Explorer and older players can miss cover art / genre in v2.4.
        # v23_sep=None keeps multiple artists as separate null-separated
        # values instead of mutagen's default of joining them with "/".
        audio.tags.update_to_v23()
        audio.save(v2_version=3, v23_sep=None)
        return bool(image_bytes)

    if suffix == ".flac":
        if audio.tags is None:
            audio.add_tags()
        _set_vorbis_tags(audio.tags, fields)
        if image_bytes:
            audio.clear_pictures()
            audio.add_picture(_make_flac_picture(image_bytes, image_mime))
        audio.save()
        return bool(image_bytes)

    if suffix in (".ogg", ".opus"):
        _set_vorbis_tags(audio.tags, fields)
        if image_bytes:
            picture_data = _make_flac_picture(image_bytes, image_mime).write()
            audio.tags["metadata_block_picture"] = [base64.b64encode(picture_data).decode("ascii")]
        audio.save()
        return bool(image_bytes)

    if suffix in (".m4a", ".mp4"):
        if audio.tags is None:
            audio.add_tags()
        atom_map = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
                    "genre": "\xa9gen", "date": "\xa9day"}
        for field, atom in atom_map.items():
            if fields.get(field):
                audio.tags[atom] = _as_list(fields[field])
        if fields.get("bpm"):
            audio.tags["tmpo"] = [int(fields["bpm"])]
        freeform_map = {"label": "LABEL", "key": "initialkey", "isrc": "ISRC", **SOURCE_TAG_FIELDS}
        for field, name in freeform_map.items():
            if fields.get(field):
                audio.tags[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(fields[field].encode("utf-8"))]
        if image_bytes:
            cover_format = MP4Cover.FORMAT_PNG if image_mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio.tags["covr"] = [MP4Cover(image_bytes, imageformat=cover_format)]
        audio.save()
        return bool(image_bytes)

    # No standard cover-art support in this format - text tags only.
    easy_audio = MutagenFile(file_path, easy=True)
    if easy_audio is None:
        raise RuntimeError("unrecognised audio file")
    if easy_audio.tags is None:
        easy_audio.add_tags()
    for field in ("title", "artist", "album", "genre"):
        if fields.get(field):
            easy_audio[field] = _as_list(fields[field])
    easy_audio.save()
    return False


def download_image(url: str, any_format=False):
    """Returns (bytes, mime_type) or (None, None) on failure or if the
    response isn't a JPEG/PNG image. any_format=True also accepts other
    image types (e.g. YouTube's webp thumbnails, before converting them),
    using the server's Content-Type as the mime."""
    if requests is None or not url:
        return None, None
    host = urlparse(url).netloc
    if host in _BLOCKED_IMAGE_HOSTS:
        return None, None
    try:
        resp = http_get(url, headers=BEATPORT_HEADERS, timeout=15)
        if resp.status_code in (401, 403):
            # Some image servers (Deezer's, on some connections) refuse every
            # request - stop asking for the rest of the session.
            _BLOCKED_IMAGE_HOSTS.add(host)
        resp.raise_for_status()
    except Exception:
        return None, None
    mime = sniff_image_mime(resp.content)
    if not mime:
        if not any_format or not resp.content:
            return None, None
        mime = resp.headers.get("Content-Type", "").split(";")[0].strip()
    return resp.content, mime


# ---------- Audio format conversion ----------
# ffmpeg does the audio; tags + cover art are then copied across with
# mutagen (same writer as the Metadata Editor), since ffmpeg's own metadata
# mapping drops things like cover art, key and label between formats.

CONVERT_TARGETS = ["mp3", "m4a", "flac", "wav", "aiff"]
CONVERT_SOURCES = ["Any format", "m4a", "mp3", "flac", "wav", "aiff", "ogg", "opus", "aac", "wma"]
CONVERT_BITRATES = ["320", "256", "192", "128"]  # kbps; only used for mp3 / m4a targets
LOSSLESS_FORMATS = {"flac", "wav", "aiff"}
CONVERT_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))
CONVERTED_FOLDER_PREFIX = "Converted ("

CONVERT_COMPATIBILITY = {
    "mp3": "mp3 plays on everything, including older CDJs and car stereos.",
    "m4a": "m4a (AAC) plays in Rekordbox, Serato, Traktor and iTunes; some very old CDJs/players can't read it.",
    "flac": "flac works in Rekordbox, Serato and Traktor, but older CDJs (before the NXS2 generation) can't play it.",
    "wav": "wav plays everywhere, but tag and artwork support is patchy - some players and CDJs won't show "
           "title/artist/cover for wav files.",
    "aiff": "aiff plays on CDJs and all DJ software, with full tag and artwork support.",
}


# "Use for" presets on the Convert Audio tab: (target format, bitrate, tip).
# They only set the target format/bitrate - the source is left as picked.
CONVERT_PRESET_CUSTOM = "Custom"
CONVERT_PRESETS = {
    "Audio CD burning": (
        "mp3", "320",
        "Audio CDs hold 80 minutes (about 18-20 songs) whatever the file format - your burning program "
        "converts the files to CD audio itself. mp3 is accepted by every burning program; wav skips one "
        "round of quality loss if your program takes it."),
    "MP3 CD for car / CD player": (
        "mp3", "256",
        "An MP3 CD is a data disc of mp3 files - 700 MB holds about 6 hours at 256kbps (roughly 90 songs; "
        "more at lower bitrates). Plays in most car stereos and CD players made since the mid-2000s."),
    "Older CDJ / USB player": (
        "mp3", "320",
        "mp3 320kbps plays on every CDJ, XDJ, car USB port and media player."),
    "iPhone / iTunes / Apple Music": (
        "m4a", "256",
        "m4a (AAC) at 256kbps is what Apple sells - smaller than mp3 for the same quality, with full "
        "tag and artwork support."),
    "DJ software (from wav/flac) \u2192 aiff": (
        "aiff", None,
        "For LOSSLESS sources (wav/flac) only: aiff keeps the full quality and, unlike wav, shows "
        "title/artist/artwork properly in Rekordbox and on CDJs. Converting mp3/m4a to aiff gains nothing."),
    "Archive (smaller lossless)": (
        "flac", None,
        "flac keeps full lossless quality at about half the size of wav/aiff - good for storing "
        "originals. Not playable on older CDJs."),
}
AUDIO_CD_SECONDS = 80 * 60
DATA_CD_BYTES = 700 * 1000 * 1000


def cd_fit_summary(total_seconds, total_bytes, target):
    """How many audio CDs (80 min) and, for mp3/m4a, MP3/data CDs (700 MB)
    the converted songs fill. Returns a list of lines."""
    lines = []
    if total_seconds:
        minutes = total_seconds / 60
        discs = -(-total_seconds // AUDIO_CD_SECONDS)
        fits = "fits on 1 audio CD" if discs <= 1 else f"needs {discs:.0f} audio CDs"
        lines.append(f"As audio CDs (80 min each): {minutes:.0f} minutes of music - {fits}.")
    if total_bytes and target in ("mp3", "m4a"):
        discs = -(-total_bytes // DATA_CD_BYTES)
        fits = "fits on 1 MP3 CD" if discs <= 1 else f"needs {discs:.0f} MP3 CDs"
        lines.append(f"As MP3 CDs (700 MB each): ~{format_size(total_bytes)} - {fits}.")
    return lines


def format_size(num_bytes) -> str:
    """'850 MB' / '2.31 GB'."""
    if num_bytes >= 1e9:
        return f"{num_bytes / 1e9:.2f} GB"
    return f"{num_bytes / 1e6:.1f} MB" if num_bytes < 1e8 else f"{num_bytes / 1e6:.0f} MB"


def audio_kind(path: Path) -> str:
    """File format name from its extension, with .aif treated as aiff."""
    ext = path.suffix.lower().lstrip(".")
    return "aiff" if ext == "aif" else ext


def _mb_per_minute(target, bitrate):
    if target in ("mp3", "m4a"):
        return int(bitrate) * 60 / 8 / 1000
    if target == "flac":
        return 5.5   # typical 16-bit/44.1kHz music - varies with the track
    return 10.1      # wav/aiff, 16-bit/44.1kHz stereo


def conversion_warning(source, target, bitrate):
    """What converting source -> target does to quality and size, in plain
    words. Returns (text, severity) - severity 'warn' when the conversion
    costs quality or bloats files for no gain, 'ok' otherwise."""
    size = f"about {_mb_per_minute(target, bitrate):.1f} MB per minute"
    target_lossless = target in LOSSLESS_FORMATS

    if source == target:
        return f"Files are already {target} - pick a different format to convert to.", "warn"

    if source == "Any format":
        # Files already in the target format are skipped, so leave it out of the lists.
        lossless = "/".join(f for f in ("flac", "wav", "aiff") if f != target)
        lossy = "/".join(f for f in ("mp3", "m4a", "ogg") if f != target)
        if target_lossless:
            text = (f"Converting to {target} ({size}). {lossless} files convert with no quality change, "
                    f"but {lossy} files WON'T sound any better as {target} - they just get 3-10x bigger.")
        else:
            text = (f"Converting to {target} at {bitrate}kbps ({size}). {lossless} files convert cleanly, but "
                    f"{lossy} files are re-encoded lossy-to-lossy, which loses a little more detail and can "
                    f"never add quality back.")
        return text, "warn"

    source_lossless = source in LOSSLESS_FORMATS
    if target_lossless and not source_lossless:
        return (f"{source} -> {target}: NO quality gain. The files become several times bigger ({size}) but sound "
                f"exactly like the {source} originals - the detail {source} threw away can't be recovered. Only "
                f"worth it if a device or program requires {target}."), "warn"
    if target_lossless and source_lossless:
        return (f"{source} -> {target}: lossless to lossless, so the audio is identical - only file size ({size}) "
                f"and compatibility change."), "ok"
    if source_lossless:
        return (f"{source} -> {target} at {bitrate}kbps: files get much smaller ({size}, vs ~10 MB per minute for "
                f"wav). At 256-320kbps the loss is inaudible for most listening, but keep the lossless originals "
                f"if you play big club systems."), "ok"
    return (f"{source} -> {target}: lossy to lossy. Re-encoding throws away a little more detail every time and can "
            f"never add quality back, even at {bitrate}kbps ({size}). Use the highest bitrate, and keep your "
            f"originals if you might want them again."), "warn"


def find_convertible_files(folder: Path, source, target, recursive):
    """Audio files in folder matching the source format (or any format),
    excluding files already in the target format and anything inside a
    previous run's "Converted (...)" folder."""
    pattern = folder.rglob("*") if recursive else folder.glob("*")
    files = []
    for p in pattern:
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        kind = audio_kind(p)
        if kind == target or (source != "Any format" and kind != source):
            continue
        if any(part.startswith(CONVERTED_FOLDER_PREFIX) for part in p.relative_to(folder).parts[:-1]):
            continue
        files.append(p)
    return sorted(files)


def converted_path(src: Path, folder: Path, target, into_subfolder):
    """Where src's converted copy goes: next to it, or mirrored under
    <folder>/Converted (<target>)/ keeping the same subfolder layout."""
    if into_subfolder:
        out_dir = folder / f"{CONVERTED_FOLDER_PREFIX}{target})" / src.parent.relative_to(folder)
    else:
        out_dir = src.parent
    return out_dir / f"{src.stem}.{target}"


def _audio_info(path: Path):
    """(length_seconds, sample_rate, bits_per_sample, channels) - any may be None."""
    try:
        info = MutagenFile(path).info
    except Exception:
        return None, None, None, None
    return (getattr(info, "length", None), getattr(info, "sample_rate", None),
            getattr(info, "bits_per_sample", None), getattr(info, "channels", None))


def estimate_converted_size(path: Path, target, bitrate):
    """Rough output size in bytes (duration x target bitrate), or None."""
    length, rate, bits, channels = _audio_info(path)
    if not length:
        return None
    if target in ("mp3", "m4a"):
        return length * int(bitrate) * 1000 / 8
    pcm = length * (rate or 44100) * (channels or 2) * max(2, (bits or 16) // 8)
    return pcm * 0.6 if target == "flac" else pcm


def read_tags_and_cover(path: Path, audio=None):
    """Reads the tags embed_full_metadata can write (title, artist list,
    album, genre, date/year, bpm, label, key, isrc) plus the front cover
    from any supported format. Returns (fields, image_bytes, mime). Pass
    `audio` if the file has already been opened with mutagen."""
    fields, image, mime = {}, None, None
    if audio is None:
        audio = MutagenFile(path)
    tags = getattr(audio, "tags", None) if audio is not None else None
    if tags is None:
        return fields, None, None

    if isinstance(tags, ID3):
        frame_map = [("title", "TIT2"), ("artist", "TPE1"), ("album", "TALB"), ("genre", "TCON"),
                     ("date", "TDRC"), ("bpm", "TBPM"), ("label", "TPUB"), ("key", "TKEY"), ("isrc", "TSRC")]
        for field, frame_id in frame_map:
            frame = tags.get(frame_id)
            if frame is not None and frame.text:
                fields[field] = [str(t) for t in frame.text] if field == "artist" else str(frame.text[0])
        for field, name in SOURCE_TAG_FIELDS.items():
            frame = tags.get(f"TXXX:{name}")
            if frame is not None and frame.text:
                fields[field] = str(frame.text[0])
        pictures = tags.getall("APIC")
        picture = next((p for p in pictures if p.type == 3), pictures[0] if pictures else None)
        if picture is not None:
            image, mime = picture.data, picture.mime

    elif isinstance(tags, MP4Tags):
        atom_map = [("title", "\xa9nam"), ("artist", "\xa9ART"), ("album", "\xa9alb"), ("genre", "\xa9gen"),
                    ("date", "\xa9day")]
        for field, atom in atom_map:
            if tags.get(atom):
                fields[field] = [str(v) for v in tags[atom]] if field == "artist" else str(tags[atom][0])
        if tags.get("tmpo"):
            fields["bpm"] = str(tags["tmpo"][0])
        for field, name in (("label", "LABEL"), ("key", "initialkey"), ("isrc", "ISRC"),
                            *SOURCE_TAG_FIELDS.items()):
            value = tags.get(f"----:com.apple.iTunes:{name}")
            if value:
                fields[field] = bytes(value[0]).decode("utf-8", "replace")
        if tags.get("covr"):
            cover = tags["covr"][0]
            image = bytes(cover)
            mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"

    else:  # Vorbis comments - FLAC / Ogg / Opus
        key_map = [("title", ["title"]), ("artist", ["artist"]), ("album", ["album"]), ("genre", ["genre"]),
                   ("date", ["date"]), ("bpm", ["bpm"]), ("label", ["organization", "label"]),
                   ("key", ["initialkey", "key"]), ("isrc", ["isrc"]),
                   *((field, [name.lower()]) for field, name in SOURCE_TAG_FIELDS.items())]
        for field, keys in key_map:
            for k in keys:
                if k in tags and tags[k]:
                    fields[field] = list(tags[k]) if field == "artist" else str(tags[k][0])
                    break
        pictures = list(getattr(audio, "pictures", []) or [])
        if not pictures and "metadata_block_picture" in tags:
            for encoded in tags["metadata_block_picture"]:
                try:
                    pictures.append(Picture(base64.b64decode(encoded)))
                except Exception:
                    pass
        picture = next((p for p in pictures if p.type == 3), pictures[0] if pictures else None)
        if picture is not None:
            image, mime = picture.data, picture.mime

    if fields.get("date"):
        fields["year"] = fields["date"][:4]
    if image:
        mime = sniff_image_mime(image) or mime
        if mime not in ("image/jpeg", "image/png"):
            image, mime = None, None
    return fields, image, mime


def move_to_recycle_bin(path: Path):
    """Sends a file to the Recycle Bin / Trash (recoverable) - never a
    permanent delete. Uses send2trash (Windows, macOS, Linux); without it,
    the Windows API on Windows, and elsewhere the original is kept."""
    if send2trash is not None:
        send2trash(str(path))
        if path.exists():
            raise OSError(f"couldn't move to the {TRASH_NAME}")
        return
    if sys.platform != "win32":
        raise OSError("moving to the Trash needs the send2trash package (pip install send2trash) - original kept")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = str(path.resolve()) + "\0"  # list must be double-null terminated; ctypes adds the second
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted or path.exists():
        raise OSError(f"couldn't move to the Recycle Bin (error {result})")


def _ffmpeg_codec_args(target, bitrate, sample_rate, bits):
    high_res = bits is not None and bits > 16
    if target == "mp3":
        args = ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-id3v2_version", "3"]
    elif target == "m4a":
        args = ["-c:a", "aac", "-b:a", f"{bitrate}k"]
    elif target == "flac":
        # 16-bit unless the source really has more - otherwise ffmpeg makes
        # 24-bit flac from mp3/m4a, which is just bigger with nothing extra in it.
        args = ["-c:a", "flac"] + ([] if high_res else ["-sample_fmt", "s16"])
    elif target == "wav":
        args = ["-c:a", "pcm_s24le" if high_res else "pcm_s16le"]
    else:  # aiff
        args = ["-c:a", "pcm_s24be" if high_res else "pcm_s16be"]
    if target in ("mp3", "m4a") and sample_rate and sample_rate > 48000:
        args += ["-ar", "48000"]  # mp3/AAC encoders top out at 48kHz
    return args


class ConversionCancelled(Exception):
    pass


def convert_audio_file(src: Path, dest: Path, target, bitrate, running_procs, cancel_event, copy_audio=False):
    """Converts one file with ffmpeg into a temporary name next to dest,
    copies tags + cover across, checks the result's length matches the
    source, then renames it into place - so a failed or cancelled
    conversion never leaves a half-written file under the final name.
    copy_audio=True moves the audio across untouched instead of re-encoding
    it (.aac into m4a). Returns the source length in seconds (or None)."""
    length, rate, bits, _ = _audio_info(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.converting.{target}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
           "-map", "0:a:0", "-vn", "-map_metadata", "0",
           *(["-c:a", "copy"] if copy_audio else _ffmpeg_codec_args(target, bitrate, rate, bits)), str(tmp)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=_no_window_flags())
    except FileNotFoundError:
        raise RuntimeError(f"ffmpeg isn't installed or isn't on your PATH ({FFMPEG_INSTALL_HINT})")
    running_procs.add(proc)
    try:
        _, stderr = proc.communicate()
    finally:
        running_procs.discard(proc)

    try:
        if cancel_event.is_set():
            raise ConversionCancelled("Cancelled")
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"ffmpeg exited with code {proc.returncode}")

        fields, image, mime = read_tags_and_cover(src)
        if fields or image:
            embed_full_metadata(tmp, fields, image, mime)

        new_length = _audio_info(tmp)[0]
        if length and (not new_length or abs(new_length - length) > 2):
            raise RuntimeError(f"converted file is {format_duration(new_length)} long but the original is "
                               f"{format_duration(length)} - not keeping it")
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return length


class AudioConverter:
    """Converts a list of files CONVERT_WORKERS at a time on background
    threads. Callbacks: log(text), on_progress(done, total) and
    on_finished(summary dict) - the last two are run on the Tk thread."""

    def __init__(self, widget, files, folder, target, bitrate, into_subfolder, recycle_originals,
                 log, on_progress, on_finished):
        self.widget = widget
        self.files = files
        self.folder = folder
        self.target = target
        self.bitrate = bitrate
        self.into_subfolder = into_subfolder
        self.recycle_originals = recycle_originals
        self.log = log
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.cancel_event = threading.Event()
        self.running_procs = set()
        self._lock = threading.Lock()
        self._done = 0

    def _ui(self, fn):
        try:
            self.widget.after(0, fn)
        except (tk.TclError, RuntimeError):
            pass

    def start(self):
        threading.Thread(target=self._run_all, daemon=True).start()

    def cancel(self):
        self.cancel_event.set()
        for proc in list(self.running_procs):
            try:
                proc.kill()
            except OSError:
                pass

    def _run_all(self):
        summary = {"converted": 0, "existing": 0, "failed": 0, "cancelled": 0, "recycled": 0,
                   "recycle_failed": 0, "bytes_before": 0, "bytes_after": 0}
        with KEEP_AWAKE, ThreadPoolExecutor(max_workers=CONVERT_WORKERS) as pool:
            for outcome in pool.map(self._convert_one, self.files):
                for key, value in outcome.items():
                    summary[key] += value
        self._ui(lambda: self.on_finished(summary))

    def _convert_one(self, src):
        outcome = {}
        dest = converted_path(src, self.folder, self.target, self.into_subfolder)
        rel = src.relative_to(self.folder)
        try:
            if self.cancel_event.is_set():
                return {"cancelled": 1}
            if dest.exists():
                self.log(f"  Skipped (already exists): {dest.name}")
                return {"existing": 1}
            before = src.stat().st_size
            convert_audio_file(src, dest, self.target, self.bitrate, self.running_procs, self.cancel_event)
            after = dest.stat().st_size
            outcome.update(converted=1, bytes_before=before, bytes_after=after)
            self.log(f"  Converted: {rel}  ({before / 1e6:.1f} MB -> {after / 1e6:.1f} MB)")
            if self.recycle_originals:
                try:
                    move_to_recycle_bin(src)
                    outcome["recycled"] = 1
                except OSError as e:
                    outcome["recycle_failed"] = 1
                    self.log(f"  Kept original {rel} - {e}")
            return outcome
        except ConversionCancelled:
            return {"cancelled": 1}
        except Exception as e:
            if self.cancel_event.is_set():
                return {"cancelled": 1}
            self.log(f"  FAILED: {rel} - {e}")
            return {"failed": 1}
        finally:
            with self._lock:
                self._done += 1
                done = self._done
            self._ui(lambda: self.on_progress(done, len(self.files)))


# ---------- Small shared GUI helpers ----------

def browse_folder_into(entry, title):
    path = filedialog.askdirectory(title=title)
    if path:
        entry.delete(0, tk.END)
        entry.insert(0, path)


def make_path_row(parent, label_text, browse_fn, pad=None, placeholder="", info=None):
    """Builds a Label (+ info button) + Entry + Browse... row, returns the Entry widget."""
    if label_text or info:
        head = tk.Frame(parent); style_frame(head)
        head.pack(fill="x", pady=(2, 2))
        label = tk.Label(head, text=label_text)
        style_label(label)
        label.pack(side="left")
        if info:
            info_button(head, info, label_text.rstrip(":")).pack(side="left", padx=(4, 0))
    row = tk.Frame(parent)
    style_frame(row)
    row.pack(fill="x", pady=(0, 4))
    entry = ThemedEntry(row, placeholder=placeholder)
    entry.pack(side="left", fill="x", expand=True)
    btn = ThemedButton(row, text="Browse...", command=lambda: browse_fn(entry))
    btn.pack(side="left", padx=(6, 0))
    return entry


# ---------- Layout helpers: sections, info popovers, folds, status line ----------

_POPOVER = {"window": None, "anchor": None, "closed_at": 0.0}


def close_info_popover():
    window = _POPOVER["window"]
    if window is not None:
        _POPOVER["window"] = None
        _POPOVER["closed_at"] = time.monotonic()
        top, funcid = window._close_binding
        try:
            top.unbind("<Button-1>", funcid)
        except tk.TclError:
            pass
        try:
            window.destroy()
        except tk.TclError:
            pass


def show_info_popover(anchor, title, text):
    """A small themed panel under an info button explaining something. Closes
    on the next click anywhere in the window, on Escape, or on clicking the
    same button again."""
    already_open = _POPOVER["window"] is not None and _POPOVER["anchor"] is anchor
    reopening_same = (_POPOVER["anchor"] is anchor and time.monotonic() - _POPOVER["closed_at"] < 0.3)
    close_info_popover()
    if already_open:
        return  # same button again (e.g. from the keyboard) - just close it
    if reopening_same:
        return  # that click was the one that closed it - don't reopen
    _POPOVER["anchor"] = anchor
    pop = tk.Toplevel(anchor)
    pop.overrideredirect(True)
    pop.withdraw()
    border = tk.Frame(pop, bd=0, highlightthickness=1)
    register(lambda: border.configure(bg=THEME["ENTRY_BG"], highlightbackground=THEME["ACCENT"],
                                      highlightcolor=THEME["ACCENT"]))
    border.pack(fill="both", expand=True)
    if title:
        heading = tk.Label(border, text=title, font="TMSubheading", anchor="w", justify="left")
        register(lambda: heading.configure(bg=THEME["ENTRY_BG"], fg=THEME["FG"]))
        heading.pack(fill="x", padx=12, pady=(10, 2))
    body = tk.Label(border, text=text() if callable(text) else text, justify="left", anchor="w", wraplength=400)
    register(lambda: body.configure(bg=THEME["ENTRY_BG"], fg=THEME["FG"]))
    body.pack(fill="x", padx=12, pady=(2 if title else 10, 10))

    pop.update_idletasks()
    x = anchor.winfo_rootx()
    y = anchor.winfo_rooty() + anchor.winfo_height() + 4
    x = max(0, min(x, anchor.winfo_screenwidth() - pop.winfo_reqwidth() - 8))
    if y + pop.winfo_reqheight() > anchor.winfo_screenheight() - 40:
        y = anchor.winfo_rooty() - pop.winfo_reqheight() - 4
    pop.geometry(f"+{x}+{y}")
    top = anchor.winfo_toplevel()
    pop.transient(top)
    pop.deiconify()
    pop.lift()
    funcid = top.bind("<Button-1>", lambda e: close_info_popover(), add="+")
    pop._close_binding = (top, funcid)
    top.bind("<Escape>", lambda e: close_info_popover(), add="+")
    _POPOVER["window"] = pop


def info_button(parent, text, title=None):
    """A small "i" button that shows `text` (a string, or a function returning
    one) in a popover when clicked."""
    btn = ThemedButton(parent, text="\u24d8", style="Info.TButton", width=2)
    btn.configure(command=lambda: show_info_popover(btn, title, text))
    return btn


def make_section(parent, title=None, info=None, expand=False, pady=(10, 0)):
    """An outlined card with an optional heading and info button. Returns
    the inner frame to put the section's widgets in (its heading row is
    .header, for extra controls)."""
    card = tk.Frame(parent, bd=0, highlightthickness=1)
    register(lambda: card.configure(bg=THEME["BG"], highlightbackground=THEME["ROW_BORDER"],
                                    highlightcolor=THEME["ROW_BORDER"]))
    card.pack(fill="both" if expand else "x", expand=expand, padx=12, pady=pady)
    header = None
    if title or info:
        header = tk.Frame(card); style_frame(header)
        header.pack(fill="x", padx=10, pady=(8, 0))
        if title:
            heading = tk.Label(header, text=title, font="TMSubheading")
            style_label(heading)
            heading.pack(side="left")
        if info:
            info_button(header, info, title).pack(side="left", padx=(4, 0))
    body = tk.Frame(card); style_frame(body)
    body.pack(fill="both", expand=expand, padx=10, pady=(4, 10))
    body.header = header
    body.card = card
    return body


class Fold:
    """A "More options" toggle: .body is a frame shown only while open."""

    def __init__(self, parent, text="More options"):
        self.text = text
        self.row = tk.Frame(parent); style_frame(self.row)
        self.row.pack(fill="x", pady=(4, 0))
        self.button = ThemedButton(self.row, text=f"\u25b8  {text}", style="Info.TButton", command=self.toggle)
        self.button.pack(side="left")
        self.body = tk.Frame(parent); style_frame(self.body)
        self.open = False

    def toggle(self):
        self.open = not self.open
        if self.open:
            self.body.pack(fill="x", after=self.row)
        else:
            self.body.pack_forget()
        arrow = "\u25be" if self.open else "\u25b8"
        self.button.configure(text=f"{arrow}  {self.text}")


def make_link(parent, text, url):
    """A clickable link in the accent colour that opens `url` in the default
    browser (Windows, macOS and Linux), underlined while hovered."""
    link = tk.Label(parent, text=text, cursor="hand2", font="TkDefaultFont")
    register(lambda: link.configure(bg=THEME["BG"], fg=THEME["ACCENT"]))
    underline = tkfont.Font(link, font=link.cget("font"))
    underline.configure(underline=True)
    link.bind("<Button-1>", lambda e: webbrowser.open(url))
    link.bind("<Enter>", lambda e: link.configure(font=underline))
    link.bind("<Leave>", lambda e: link.configure(font="TkDefaultFont"))
    return link


def make_action_row(parent, text, command, extra_buttons=()):
    """The tab's main button (accent colour), any secondary buttons, a
    progress bar and a one-line status underneath. Returns
    (main button, [extra buttons], progress bar, status label)."""
    row = tk.Frame(parent); style_frame(row)
    row.pack(fill="x", padx=12, pady=(12, 0))
    main = ThemedButton(row, text=text, command=command, accent=True)
    main.pack(side="left")
    extras = []
    for label, cmd, state in extra_buttons:
        btn = ThemedButton(row, text=label, command=cmd, state=state)
        btn.pack(side="left", padx=(6, 0))
        extras.append(btn)
    progress = ttk.Progressbar(row, mode="determinate")
    progress.pack(side="left", fill="x", expand=True, padx=(12, 0))
    status = tk.Label(parent, text="", anchor="w", justify="left", wraplength=680)
    style_label(status, subtle=True)
    status.pack(fill="x", padx=14, pady=(4, 0))
    return main, extras, progress, status


def update_status_line(owner, message):
    """Shows the latest log message on a tab's one-line status label."""
    label = getattr(owner, "status_line", None)
    lines = [ln.strip() for ln in str(message).splitlines() if ln.strip()]
    if label is None or not lines:
        return
    text = lines[-1] if len(lines[-1]) <= 140 else lines[-1][:137] + "..."
    try:
        owner.after(0, lambda: label.configure(text=text))
    except (tk.TclError, RuntimeError):
        pass


def set_visible(widget, visible, **pack_options):
    """Shows or hides a widget (hidden options take no space, unlike greyed-out ones)."""
    if visible and not widget.winfo_manager():
        widget.pack(**pack_options)
    elif not visible and widget.winfo_manager():
        widget.pack_forget()


LOG_PANEL_MAX_LINES = 3000  # a tab's log panel keeps this many lines; the session log file keeps everything


class LogPanelMixin:
    """The log() every tab uses: writes the message to the session log file,
    shows it on the tab's status line, and adds it to the tab's log panel
    (trimmed to the last LOG_PANEL_MAX_LINES lines, so a 6000-song scan
    doesn't keep growing it). Safe to call from background threads."""

    def log(self, message: str):
        SESSION_LOG.write(log_source_name(self), message)
        update_status_line(self, message)

        def append():
            box = self.log_box
            box.configure(state="normal")
            box.insert(tk.END, message + "\n")
            lines = int(box.index("end-1c").split(".")[0])
            if lines > LOG_PANEL_MAX_LINES:
                box.delete("1.0", f"{lines - LOG_PANEL_MAX_LINES + 1}.0")
            box.see(tk.END)
            box.configure(state="disabled")
        try:
            self.after(0, append)
        except (tk.TclError, RuntimeError):
            pass  # tab closed while a background job was still reporting


class DownloadOptions:
    """What the user picked for YouTube downloads - read once on the Tk
    thread, then handed to worker threads (Tk variables aren't thread-safe)."""
    def __init__(self, fmt, bitrate, metadata_source):
        self.fmt = fmt
        self.bitrate = bitrate
        self.metadata_source = metadata_source  # Auto / Beatport / Deezer / iTunes, or None for playlist tags only

    def describe(self):
        if self.fmt == "mp3":
            text = f"mp3 {self.bitrate}kbps (re-encoded)"
        else:
            text = f"{self.fmt} (original YouTube audio, no re-encode)"
        return text + (f" + {self.metadata_source} tagging" if self.metadata_source else "")


def make_download_options(parent, pad=None, label_text="Format:"):
    """Builds the format / bitrate / metadata-tagging rows used on both
    tabs. Returns a zero-arg function that reads the current choices into a
    DownloadOptions - call it on the Tk thread."""
    row = tk.Frame(parent); style_frame(row)
    row.pack(fill="x", pady=(2, 0))
    l1 = tk.Label(row, text=label_text); style_label(l1)
    l1.pack(side="left", padx=(0, 8))

    format_var = tk.StringVar(value=YT_FORMATS[0])
    format_menu = ThemedOptionMenu(row, format_var, *YT_FORMATS)
    format_menu.pack(side="left")
    bitrate_frame = tk.Frame(row); style_frame(bitrate_frame)
    l2 = tk.Label(bitrate_frame, text="Bitrate:"); style_label(l2)
    l2.pack(side="left", padx=(14, 8))
    bitrate_var = tk.StringVar(value="320")
    bitrate_menu = ThemedOptionMenu(bitrate_frame, bitrate_var, *YT_BITRATES)
    bitrate_menu.pack(side="left")
    format_info = info_button(row, lambda: "\n\n".join(YT_FORMAT_NOTES.values()), "Download formats")
    format_info.pack(side="left", padx=(6, 0))

    def on_format_change(*_):
        # Bitrate only means anything when re-encoding to mp3 - m4a/opus keep
        # YouTube's own stream as-is.
        set_visible(bitrate_frame, format_var.get() == "mp3", side="left", before=format_info)
    format_var.trace_add("write", on_format_change)
    on_format_change()

    tag_row = tk.Frame(parent); style_frame(tag_row)
    tag_row.pack(fill="x", pady=(8, 0))
    l3 = tk.Label(tag_row, text="Tag downloads using:"); style_label(l3)
    l3.pack(side="left", padx=(0, 8))
    off_label = "Off (playlist tags + YouTube cover)"
    tag_labels = [METADATA_SOURCE_LABELS[k] for k in [METADATA_AUTO, *METADATA_AUTO_ORDER]] + [off_label]
    tag_var = tk.StringVar(value=METADATA_SOURCE_LABELS[METADATA_AUTO])
    tag_menu = ThemedOptionMenu(tag_row, tag_var, *tag_labels)
    tag_menu.pack(side="left")
    info_button(tag_row, "After downloading, TrackMatch looks each song up (Auto: Beatport first for electronic "
                         "music, then Deezer, then iTunes) and writes its cover art, genre, label, year - and on "
                         "Beatport BPM and key - into the file.\n\nOnly confident matches are used; anything else "
                         "keeps the playlist's own tags and the YouTube thumbnail as cover.",
                "Tagging downloads").pack(side="left", padx=(6, 0))
    if requests is None or BeautifulSoup is None:
        tag_var.set(off_label)
        tag_menu.configure(state="disabled")
    label_to_source = {label: key for key, label in METADATA_SOURCE_LABELS.items()}

    return lambda: DownloadOptions(format_var.get(), bitrate_var.get(), label_to_source.get(tag_var.get()))


class BatchDownloader:
    """"Download All Missing": for each (row idx, track) it searches
    YouTube, auto-picks the best-scoring result and downloads + tags it,
    DOWNLOAD_WORKERS at a time. Tracks whose best result isn't a confident
    match are left for a manual pick instead of guessing. All callbacks
    are invoked on the Tk thread via widget.after."""

    def __init__(self, widget, jobs, dest_dir, dl_opts, log, set_status, on_downloaded, on_finished):
        self.widget = widget
        self.jobs = jobs
        self.dest_dir = Path(dest_dir)
        self.dl_opts = dl_opts
        self.log = log
        self.set_status = set_status          # (idx, text) - must be thread-safe (window.set_row_status is)
        self.on_downloaded = on_downloaded    # (idx, path)
        self.on_finished = on_finished        # (counts dict)
        self.cancel_event = threading.Event()

    def _ui(self, fn):
        """Runs fn on the Tk thread - quietly dropped if the window has
        been closed in the meantime."""
        try:
            self.widget.after(0, fn)
        except (tk.TclError, RuntimeError):
            pass

    def start(self):
        for idx, _ in self.jobs:
            self.set_status(idx, "Queued...")
        threading.Thread(target=self._run_all, daemon=True).start()

    def cancel(self):
        self.cancel_event.set()

    def _run_all(self):
        self.log(f"\nDownloading {len(self.jobs)} tracks ({self.dl_opts.describe()}), "
                 f"{DOWNLOAD_WORKERS} at a time...")
        counts = {"downloaded": 0, "existing": 0, "manual": 0, "failed": 0, "cancelled": 0}
        with KEEP_AWAKE, ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            for outcome in pool.map(lambda job: self._run_one(*job), self.jobs):
                counts[outcome] += 1
        self.log(f"\nDownload All finished: {counts['downloaded']} downloaded, {counts['existing']} already "
                 f"in folder, {counts['manual']} need a manual pick, {counts['failed']} failed"
                 + (f", {counts['cancelled']} cancelled." if counts["cancelled"] else "."))
        self._ui(lambda: self.on_finished(counts))

    def _run_one(self, idx, track):
        if self.cancel_event.is_set():
            self.set_status(idx, "Cancelled.")
            return "cancelled"
        try:
            if track.get("youtube_url"):
                # From a YouTube playlist - that exact video is the one wanted, no search needed.
                seconds = (track["duration_ms"] / 1000) if track.get("duration_ms") else None
                best = {"url": track["youtube_url"], "title": track.get("youtube_title") or track["title"],
                        "uploader": "", "duration": format_duration(seconds), "duration_seconds": seconds,
                        "thumbnail": track.get("youtube_thumbnail", "")}
                self.log(f"  From the playlist: {track['artist']} - {track['title']} (\"{best['title']}\")")
            else:
                self.set_status(idx, "Searching YouTube...")
                query = f"{track['artist']} {track['title']}".strip()
                ranked = rank_youtube_results(track, search_youtube(query, max_results=YT_AUTO_SEARCH_RESULTS))
                if not ranked:
                    self.set_status(idx, "No YouTube results - try Search YouTube with a different query.")
                    return "manual"
                score, best = ranked[0]
                if score < YT_AUTO_PICK_MIN:
                    self.set_status(idx, f"No confident match (best {score:.0%}: \"{best['title']}\", "
                                         f"{best['duration']}) - use Search YouTube to pick one.")
                    return "manual"

                self.log(f"  Picked for {track['artist']} - {track['title']}: \"{best['title']}\" "
                         f"({best['uploader']}, {best['duration']}, {min(1.0, score):.0%})")
            path, status = fetch_youtube_track(
                track, best, self.dest_dir, self.dl_opts, self.log,
                progress=lambda text: self.set_status(idx, text), cancel_event=self.cancel_event,
            )
            self.set_status(idx, status)
            self._ui(lambda: self.on_downloaded(idx, path))
            return "existing" if status.startswith("Already") else "downloaded"
        except yt_dlp.utils.DownloadCancelled:
            self.set_status(idx, "Cancelled.")
            return "cancelled"
        except Exception as e:
            self.log(f"  Failed: {track['artist']} - {track['title']}: {e}")
            self.set_status(idx, f"Failed: {e}")
            return "failed"


def make_batch_controls(parent, start_fn, cancel_fn):
    """Download All / Cancel buttons for a review window's bottom bar.
    Returns (download_all_btn, cancel_btn)."""
    cancel_btn = ThemedButton(parent, text="Cancel Downloads", command=cancel_fn, state="disabled")
    cancel_btn.pack(side="left", padx=(6, 0))
    all_btn = ThemedButton(parent, text="Download All Selected", command=start_fn, accent=True)
    all_btn.pack(side="left", before=cancel_btn)
    return all_btn, cancel_btn


# ---------- Playlist sources (links, pasted lists, files) ----------
# Everything here turns some kind of playlist into the same list of track
# dicts read_playlist_csv returns: {"title", "artist", "album", "genre",
# "duration_ms"} - plus, for YouTube playlists, the exact video to download
# ("youtube_url" / "youtube_title" / "youtube_thumbnail").

PLAYLIST_SOURCE_HELP = (
    "Paste a playlist link - Spotify, Deezer, Apple Music, YouTube / YouTube Music - or browse to a file "
    "(CSV from Exportify / TuneMyMusic / Soundiiz, a .txt list, or an .m3u/.m3u8). Or click Paste Songs to "
    "paste songs copied straight from Spotify (select all + copy), song links, or \"Artist - Title\" lines."
)
SPOTIFY_LINK_LIMIT = 100  # Spotify's public player page only lists the first 100 songs of a playlist

_SPOTIFY_LIMITER = _RateLimiter(0.25)
_APPLE_LIMITER = _RateLimiter(0.5)

SPOTIFY_URL_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(playlist|album|track)/([A-Za-z0-9]+)")
DEEZER_URL_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?(playlist|album|track)/(\d+)")
APPLE_URL_RE = re.compile(r"music\.apple\.com/.+/(playlist|album|song)/")
YOUTUBE_HOST_RE = re.compile(r"(youtube\.com|youtu\.be|music\.youtube\.com|soundcloud\.com)")


def _get_page(url, limiter, service):
    """GET an HTML page with rate limiting and the same back-off/retry as
    _get_json. Returns the decoded text."""
    if requests is None:
        raise RuntimeError("Missing dependency. Run: pip install requests")
    for attempt in range(4):
        limiter.wait()
        resp = http_get(url, headers=BEATPORT_HEADERS, timeout=20)
        if (resp.status_code in (429, 403) or resp.status_code >= 500) and attempt < 3:
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.content.decode("utf-8", "replace")
    raise RuntimeError(f"{service} kept refusing requests - try again in a few minutes")


def _find_key(node, key):
    """First value stored under `key` anywhere in a nested JSON structure."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def _make_track(title, artist, album="", duration_ms=None, **extra):
    track = {"title": (title or "").strip(), "artist": (artist or "").strip(), "album": (album or "").strip(),
             "genre": "", "duration_ms": int(duration_ms) if duration_ms else None}
    track.update(extra)
    return track


# --- Spotify (public embed player pages - no login or API key) ---

def _spotify_embed(kind, spotify_id):
    if BeautifulSoup is None:
        raise RuntimeError("Missing dependency. Run: pip install beautifulsoup4")
    html = _get_page(f"https://open.spotify.com/embed/{kind}/{spotify_id}", _SPOTIFY_LIMITER, "Spotify")
    script = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        raise RuntimeError("Spotify's page didn't include the song list - the link may be private or invalid")
    return json.loads(script.string)


def load_spotify(kind, spotify_id, log):
    data = _spotify_embed(kind, spotify_id)
    entity = _find_key(data, "entity") or {}
    if kind == "track":
        artists = ", ".join(a.get("name", "") for a in entity.get("artists") or [] if a.get("name"))
        return [_make_track(entity.get("name") or entity.get("title"), artists,
                            duration_ms=entity.get("duration"))], entity.get("name") or "Spotify song"
    album = entity.get("name", "") if kind == "album" else ""
    tracks = [_make_track(t.get("title"), t.get("subtitle"), album, t.get("duration"))
              for t in _find_key(data, "trackList") or [] if t.get("title")]
    name = entity.get("name") or f"Spotify {kind}"
    if kind == "playlist" and len(tracks) >= SPOTIFY_LINK_LIMIT:
        log(f"NOTE: Spotify only shares the first {SPOTIFY_LINK_LIMIT} songs of a playlist link. If \"{name}\" "
            f"is longer, open it in Spotify, select all songs (Ctrl+A), copy (Ctrl+C) and use Paste Songs "
            f"instead - that way has no limit.")
    return tracks, name


# --- Deezer (public API - full track lists) ---

def load_deezer(kind, deezer_id, log):
    data = _get_json(f"{DEEZER_API}/{kind}/{deezer_id}", None, _DEEZER_LIMITER, "Deezer")
    if data.get("error"):
        raise RuntimeError(f"Deezer: {data['error'].get('message', 'not found')}")
    if kind == "track":
        return [_make_track(data.get("title"), (data.get("artist") or {}).get("name"),
                            (data.get("album") or {}).get("title"),
                            (data.get("duration") or 0) * 1000 or None)], data.get("title") or "Deezer song"
    album = data.get("title", "") if kind == "album" else ""
    tracks = []
    page = data.get("tracks") or {}
    while True:
        for t in page.get("data") or []:
            tracks.append(_make_track(t.get("title"), (t.get("artist") or {}).get("name"),
                                      album or (t.get("album") or {}).get("title"),
                                      (t.get("duration") or 0) * 1000 or None))
        if not page.get("next"):
            break
        page = _get_json(page["next"], None, _DEEZER_LIMITER, "Deezer")
    return tracks, data.get("title") or f"Deezer {kind}"


# --- Apple Music (public web page data) ---

def load_apple_music(url, log):
    if BeautifulSoup is None:
        raise RuntimeError("Missing dependency. Run: pip install beautifulsoup4")
    soup = BeautifulSoup(_get_page(url, _APPLE_LIMITER, "Apple Music"), "html.parser")
    script = soup.find("script", id="serialized-server-data")
    if not script or not script.string:
        raise RuntimeError("Apple Music's page didn't include the song list - the link may be private or invalid")
    songs = []

    def walk(node):
        if isinstance(node, dict):
            descriptor = node.get("contentDescriptor") or {}
            if descriptor.get("kind") == "song" and node.get("title"):
                songs.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(script.string))
    og_title = soup.find("meta", attrs={"property": "og:title"})
    name = re.sub(r"\s+on Apple Music$", "", og_title.get("content", "")) if og_title else "Apple Music playlist"
    tracks = []
    for s in songs:
        artist = s.get("artistName") or ", ".join(link.get("title", "") for link in s.get("subtitleLinks") or [])
        tracks.append(_make_track(s.get("title"), artist, duration_ms=s.get("duration")))
    return tracks, name


# --- YouTube / YouTube Music / SoundCloud playlists (yt-dlp) ---

VIDEO_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[][^)\]]*\b(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|mv|m/v|videoclip|clip)\b[^)\]]*[\)\]]",
    re.IGNORECASE)


def split_video_title(video_title, channel):
    """Best guess at (artist, title) from a video: 'Artist - Title (Official
    Video)' is the usual shape; otherwise the channel is the artist
    ('Artist - Topic' channels are YouTube Music's official uploads)."""
    title = VIDEO_TITLE_NOISE_RE.sub("", video_title or "").strip()
    channel = re.sub(r"\s*(- Topic|VEVO|Official)$", "", channel or "", flags=re.IGNORECASE).strip()
    for sep in (" - ", " – ", " — ", " | "):
        if sep in title:
            artist, song = (part.strip() for part in title.split(sep, 1))
            # Some uploads are "Title - Artist" - if the channel is named in the
            # second half but not the first, that half is the artist.
            channel_key = normalize(channel)
            if channel_key and channel_key in normalize(song) and channel_key not in normalize(artist):
                artist, song = song, artist
            return artist, song
    return channel, title


def load_video_playlist(url, log):
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")
    with make_ydl({"quiet": True, "no_warnings": True, "skip_download": True,
                   "extract_flat": "in_playlist"}) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or [info]) if e]
    tracks = []
    for e in entries:
        artist, title = split_video_title(e.get("title"), e.get("channel") or e.get("uploader"))
        thumbnails = e.get("thumbnails") or []
        video_url = e.get("webpage_url") or e.get("url") or ""
        if video_url and not video_url.startswith("http"):
            video_url = f"https://www.youtube.com/watch?v={e.get('id')}"
        tracks.append(_make_track(title, artist, duration_ms=(e.get("duration") or 0) * 1000 or None,
                                  youtube_url=video_url, youtube_title=e.get("title") or "",
                                  youtube_thumbnail=thumbnails[-1].get("url", "") if thumbnails else ""))
    return tracks, info.get("title") or "YouTube playlist"


# --- pasted text and files ---

LINE_PREFIX_RE = re.compile(r"^\s*(?:\d{1,2}:\d{2}(?::\d{2})?\s*[-–]?\s*|\d{1,3}[.)]\s+|[-*•]\s+)")


def parse_song_line(line):
    """'Artist - Title' (also with a leading '1.', '03:15' timestamp or
    bullet) -> (artist, title). A line with no separator is a title only."""
    line = LINE_PREFIX_RE.sub("", line).strip()
    for sep in (" - ", " – ", " — "):
        if sep in line:
            artist, title = line.split(sep, 1)
            return artist.strip(), title.strip()
    return "", line


def load_pasted_songs(text, log, progress=None):
    """Pasted lines can be Spotify song links (what Spotify copies with
    select all + copy), Deezer song links, or 'Artist - Title' lines - in
    any mix. Links are looked up; other lines are used as written."""
    tracks = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines, 1):
        spotify = SPOTIFY_URL_RE.search(line)
        deezer = DEEZER_URL_RE.search(line)
        try:
            if spotify:
                found, _ = load_spotify(spotify.group(1), spotify.group(2), log)
                tracks.extend(found)
            elif deezer:
                found, _ = load_deezer(deezer.group(1), deezer.group(2), log)
                tracks.extend(found)
            elif line.startswith("spotify:track:"):
                found, _ = load_spotify("track", line.split(":")[-1], log)
                tracks.extend(found)
            else:
                artist, title = parse_song_line(line)
                if title:
                    tracks.append(_make_track(title, artist))
        except Exception as e:
            log(f"  Couldn't read line {i} ({line[:60]}): {e}")
        if (spotify or deezer) and i % 25 == 0:
            log(f"  Looking up pasted songs... {i}/{len(lines)}")
        if progress:
            progress(i, len(lines))
    return tracks, "pasted songs"


def load_m3u(path: Path, log):
    tracks = []
    pending = None
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("#EXTINF:"):
                info = line[len("#EXTINF:"):]
                seconds, _, name = info.partition(",")
                artist, title = parse_song_line(name)
                try:
                    duration = int(float(seconds)) * 1000 if float(seconds) > 0 else None
                except ValueError:
                    duration = None
                pending = _make_track(title, artist, duration_ms=duration)
            elif line and not line.startswith("#"):
                if pending is None:
                    song = Path(line) if Path(line).is_absolute() else path.parent / line
                    title, artist = read_local_metadata(song) if song.exists() else (Path(line).stem, "")
                    if not song.exists() and " - " in title:
                        artist, title = parse_song_line(title)
                    pending = _make_track(title, artist)
                tracks.append(pending)
                pending = None
    return tracks, path.stem


def load_playlist_source(source, pasted, log, progress=None):
    """Reads a playlist from whatever the user gave - a link, a file path or
    pasted text. Returns (tracks, short description). Raises ValueError with
    a readable message for anything it can't use."""
    if pasted and pasted.strip():
        tracks, name = load_pasted_songs(pasted, log, progress)
    else:
        source = (source or "").strip().strip('"')
        if not source:
            raise ValueError("Paste a playlist link, browse to a playlist file, or use Paste Songs.")
        if re.match(r"https?://", source, re.IGNORECASE):
            if "link.deezer.com" in source or "deezer.page.link" in source:
                source = http_get(source, headers=BEATPORT_HEADERS, timeout=15, allow_redirects=True).url
            spotify, deezer = SPOTIFY_URL_RE.search(source), DEEZER_URL_RE.search(source)
            if spotify:
                tracks, name = load_spotify(spotify.group(1), spotify.group(2), log)
            elif deezer:
                tracks, name = load_deezer(deezer.group(1), deezer.group(2), log)
            elif APPLE_URL_RE.search(source):
                tracks, name = load_apple_music(source, log)
            elif YOUTUBE_HOST_RE.search(source):
                tracks, name = load_video_playlist(source, log)
            else:
                raise ValueError("That link isn't one TrackMatch can read. Supported: Spotify, Deezer, Apple Music, "
                                 "YouTube / YouTube Music and SoundCloud playlist links.")
        else:
            path = Path(source)
            if not path.is_file():
                raise ValueError(f"Couldn't find the file:\n{source}")
            suffix = path.suffix.lower()
            if suffix == ".csv":
                tracks, name = read_playlist_csv(path, lambda _m: None), path.stem
            elif suffix in (".m3u", ".m3u8"):
                tracks, name = load_m3u(path, log)
            elif suffix == ".txt":
                tracks, name = load_pasted_songs(path.read_text(encoding="utf-8-sig", errors="replace"), log,
                                                 progress)
                name = path.stem
            else:
                raise ValueError("Playlist files can be .csv, .txt, .m3u or .m3u8.")
    tracks = [t for t in tracks if t["title"]]
    if not tracks:
        raise ValueError("No songs found in that playlist.")
    log(f"Loaded \"{name}\": {len(tracks)} songs.")
    return tracks


class PlaylistSourceInput:
    """The "Playlist" row shared by the Download and Create Playlist tabs: an
    entry for a link or file path, Browse..., and Paste Songs... (which keeps
    the pasted text and shows a summary in the entry)."""

    PASTED_PREFIX = "[Pasted songs: "

    def __init__(self, parent, pad=None):
        row = tk.Frame(parent); style_frame(row)
        row.pack(fill="x", pady=(2, 0))
        self.entry = ThemedEntry(row, placeholder="Paste a Spotify, Deezer, Apple Music or YouTube link...")
        self.entry.pack(side="left", fill="x", expand=True)
        browse = ThemedButton(row, text="Browse...", command=self.browse)
        browse.pack(side="left", padx=(6, 0))
        paste = ThemedButton(row, text="Paste Songs...", command=self.open_paste_dialog)
        paste.pack(side="left", padx=(6, 0))
        self.parent = parent
        self.pasted = ""
        self.entry.bind("<KeyRelease>", lambda e: self._forget_paste_if_edited())

    def _forget_paste_if_edited(self):
        if self.pasted and not self.entry.get().startswith(self.PASTED_PREFIX):
            self.pasted = ""

    def browse(self):
        path = filedialog.askopenfilename(
            title="Select a playlist file",
            filetypes=[("Playlist files", "*.csv *.txt *.m3u *.m3u8"), ("All files", "*.*")])
        if path:
            self.pasted = ""
            self.entry.delete(0, tk.END)
            self.entry.insert(0, path)

    def open_paste_dialog(self):
        dialog = tk.Toplevel(self.parent)
        dialog.title("Paste Songs")
        dialog.geometry("640x480")
        style_window(dialog)
        set_window_icon(dialog, ICON_PATH)
        enable_dark_titlebar(dialog)
        dialog.transient(self.parent.winfo_toplevel())
        info = tk.Label(
            dialog, justify="left", anchor="w", wraplength=610,
            text="Paste one song per line. Any mix of:\n"
                 "  •  Songs copied from the Spotify app: open the playlist, click a song, Ctrl+A, then Ctrl+C\n"
                 "  •  Spotify or Deezer song links\n"
                 "  •  \"Artist - Title\" lines (numbering like \"1.\" and mix timestamps like \"03:15\" "
                 "are ignored)")
        style_label(info)
        info.pack(fill="x", padx=12, pady=(12, 6))
        text = ThemedScrolledText(dialog, wrap="none")
        style_scrolledtext(text)
        text.pack(fill="both", expand=True, padx=12)
        if self.pasted:
            text.insert("1.0", self.pasted)
        text.focus_set()
        bottom = tk.Frame(dialog); style_frame(bottom)
        bottom.pack(fill="x", padx=12, pady=12)

        def use():
            content = text.get("1.0", "end").strip()
            lines = [ln for ln in content.splitlines() if ln.strip()]
            self.pasted = content
            self.entry.delete(0, tk.END)
            if lines:
                self.entry.insert(0, f"{self.PASTED_PREFIX}{len(lines)} lines]")
            dialog.destroy()

        ok_btn = ThemedButton(bottom, text="Use These Songs", command=use, accent=True)
        ok_btn.pack(side="right")
        cancel_btn = ThemedButton(bottom, text="Cancel", command=dialog.destroy, width=10)
        cancel_btn.pack(side="right", padx=(0, 6))
        dialog.grab_set()

    def get(self):
        """(source text, pasted text) - read on the Tk thread."""
        return self.entry.get().strip(), self.pasted

    def is_empty(self):
        source, pasted = self.get()
        return not source and not pasted


# ---------- Tab 1: Download Missing Tracks ----------

class DownloadTab(LogPanelMixin, tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)

        playlist = make_section(self, "Playlist", PLAYLIST_SOURCE_HELP, pady=(12, 0))
        self.playlist_input = PlaylistSourceInput(playlist)

        folders = make_section(self, "Folders")
        self.check_dir_entry = make_path_row(
            folders, "Your music folder:",
            lambda e: browse_folder_into(e, "Select the folder to check for existing files"),
            placeholder="Songs already in here won't be downloaded again",
            info="The folder (and its subfolders) TrackMatch checks for songs you already have. Anything in the "
                 "playlist that isn't found here can be downloaded."
        )
        self.same_folder_var = tk.BooleanVar(value=True)
        same_chk = ThemedCheckbutton(folders, text="Save downloads into the same folder",
                                     variable=self.same_folder_var, command=self.toggle_same_folder)
        same_chk.pack(anchor="w", pady=(2, 0))
        self.dest_row = tk.Frame(folders); style_frame(self.dest_row)
        self.dest_entry = ThemedEntry(self.dest_row, placeholder="Where downloads should be saved")
        self.dest_entry.pack(side="left", fill="x", expand=True)
        dest_btn = ThemedButton(self.dest_row, text="Browse...",
                                command=lambda: browse_folder_into(self.dest_entry, "Select download destination folder"))
        dest_btn.pack(side="left", padx=(6, 0))

        options = make_section(self, "Download options")
        self.get_download_options = make_download_options(options)

        self.run_button, _, self.progress, self.status_line = make_action_row(
            self, "Check & Review", self.run_clicked)

        log_label = tk.Label(self, text="Log:"); style_label(log_label)
        log_label.pack(anchor="w", padx=12, pady=(8, 0))
        self.log_box = ThemedScrolledText(self, height=10, state="disabled", wrap="word")
        style_scrolledtext(self.log_box)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        register_log_panel(log_label, self.log_box)

        if not MutagenFile:
            self.log("WARNING: 'mutagen' is not installed. Run: pip install mutagen")
        if not yt_dlp:
            self.log("NOTE: 'yt-dlp' is not installed - downloading won't work until you "
                      "run: pip install yt-dlp (also requires ffmpeg on your PATH)")

    def toggle_same_folder(self):
        set_visible(self.dest_row, not self.same_folder_var.get(), fill="x", pady=(4, 0))

    def run_clicked(self):
        source = self.playlist_input.get()
        check_dir = self.check_dir_entry.get().strip()
        same_folder = self.same_folder_var.get()
        dest_dir = check_dir if same_folder else self.dest_entry.get().strip()

        if self.playlist_input.is_empty():
            messagebox.showerror("Missing info", "Paste a playlist link, browse to a playlist file, or use "
                                                 "Paste Songs.")
            return
        if not check_dir or not Path(check_dir).is_dir():
            messagebox.showerror("Missing info", "Choose a valid folder to check against.")
            return
        if not dest_dir:
            messagebox.showerror("Missing info", "Choose a download destination folder.")
            return
        if not MutagenFile:
            messagebox.showerror("Missing dependency", "Install requirements first:\npip install mutagen")
            return
        if not yt_dlp:
            messagebox.showerror("Missing dependency", "Install requirements first:\npip install yt-dlp\n"
                                                         "(also requires ffmpeg on your PATH)")
            return

        self.run_button.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        dl_opts = self.get_download_options()
        thread = threading.Thread(
            target=self.worker, args=(source, check_dir, dest_dir, dl_opts), daemon=True
        )
        thread.start()

    def worker(self, source, check_dir, dest_dir, dl_opts):
        try:
            self.log("Reading playlist...")
            tracks = load_playlist_source(*source, self.log)

            index = index_music_folder(Path(check_dir), self.log)

            self.log("Checking each track against the folder...")
            items = []
            for track in tracks:
                candidate, score = find_best_match(track, index) if index else (None, 0.0)
                items.append({"track": track, "candidate": candidate, "score": score})

            # Missing/worst matches first
            items.sort(key=lambda it: it["score"])
            found_count = sum(1 for it in items if it["score"] >= 0.75)
            self.log(f"\n{found_count}/{len(items)} tracks already look present. "
                     f"Opening review window to fetch the rest...")

            self.after(0, lambda: DownloadReviewWindow(self, items, Path(dest_dir), dl_opts, self.log))

        except Exception as e:
            self.log(f"ERROR: {e}")
            SESSION_LOG.exception(log_source_name(self), type(e), e, e.__traceback__)
            self.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.run_button.configure(state="normal"))


class PagedReviewMixin:
    """Pages of PAGE_SIZE rows for the Download and Playlist review windows.
    Building a block of controls for every song is what made big reviews
    slow (1000 songs took ~6s to open), so only the visible page has
    widgets. Everything about a row - its choices, ticks and status text -
    lives in variables and dicts that outlive its widgets. The window
    provides build_row(idx, parent) and on_page_shown() (optional)."""

    PAGE_SIZE = 100

    def make_review_area(self):
        """The scrolling list the rows go in, plus the page navigation bar."""
        self.page = 0
        self.row_status_text = {}   # idx -> latest status line
        self.include_state = {}     # idx -> "normal" / "disabled"
        self.status_labels = {}     # visible rows only
        self.include_checks = {}    # visible rows only
        self.page_count = max(1, -(-len(self.items) // self.PAGE_SIZE))

        self.nav = tk.Frame(self); style_frame(self.nav)
        if self.page_count > 1:
            self.nav.pack(fill="x", padx=10, pady=(6, 0))
            self.prev_btn = ThemedButton(self.nav, text="\u25c0  Previous", command=lambda: self.show_page(self.page - 1))
            self.prev_btn.pack(side="left")
            self.next_btn = ThemedButton(self.nav, text="Next  \u25b6", command=lambda: self.show_page(self.page + 1))
            self.next_btn.pack(side="right")
            self.page_label = tk.Label(self.nav, text="")
            style_label(self.page_label, subtle=True)
            self.page_label.pack(side="left", expand=True)

        area = tk.Frame(self); style_frame(area)
        area.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 10))
        self.canvas = tk.Canvas(area, borderwidth=0)
        style_canvas(self.canvas)
        self.scroll_frame = tk.Frame(self.canvas); style_frame(self.scroll_frame)
        scrollbar = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.scroll_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        make_wheel_scrollable(self.canvas)

    def show_page(self, page):
        self.page = max(0, min(page, self.page_count - 1))
        for child in self.scroll_frame.winfo_children():
            child.destroy()
        self.status_labels.clear()
        self.include_checks.clear()
        if hasattr(self, "on_page_cleared"):
            self.on_page_cleared()
        start = self.page * self.PAGE_SIZE
        end = min(start + self.PAGE_SIZE, len(self.items))
        for idx in range(start, end):
            self.build_row(idx, self.scroll_frame)
        self.canvas.yview_moveto(0)
        if self.page_count > 1:
            self.page_label.configure(text=f"Songs {start + 1}-{end} of {len(self.items)}  "
                                           f"(page {self.page + 1} of {self.page_count})")
            self.prev_btn.configure(state="normal" if self.page > 0 else "disabled")
            self.next_btn.configure(state="normal" if self.page < self.page_count - 1 else "disabled")

    def add_status_label(self, idx, row, **pack):
        label = tk.Label(row, text=self.row_status_text.get(idx, ""), justify="left", anchor="w", wraplength=760)
        style_label(label, subtle=True)
        label.pack(fill="x", padx=6, pady=(0, 4), **pack)
        self.status_labels[idx] = label

    def add_include_check(self, idx, parent, **pack):
        check = ThemedCheckbutton(parent, text="Include in Download All", variable=self.include_vars[idx],
                                  state=self.include_state.get(idx, "normal"))
        check.pack(side="left", **pack)
        self.include_checks[idx] = check

    def set_row_status(self, idx, text):
        """Thread-safe: remembers the row's status and shows it if the row is on screen."""
        def update():
            self.row_status_text[idx] = text
            label = self.status_labels.get(idx)
            if label is not None and label.winfo_exists():
                label.configure(text=text)
        try:
            self.after(0, update)
        except (tk.TclError, RuntimeError):
            pass  # window closed while a background job was still reporting

    def set_include_state(self, idx, state):
        self.include_state[idx] = state
        check = self.include_checks.get(idx)
        if check is not None and check.winfo_exists():
            check.configure(state=state)


class BatchDownloadMixin:
    """Download All / Cancel behaviour shared by both review windows. The
    window provides: items, dl_opts, log, include_vars / include_checks
    (idx -> BooleanVar / Checkbutton), download_all_btn, cancel_btn,
    set_row_status(idx, text), on_batch_downloaded(idx, path),
    download_dir() and row_done(idx)."""

    batch = None

    def download_all(self):
        if yt_dlp is None:
            messagebox.showerror("Missing dependency", "yt-dlp is not installed.\nRun: pip install yt-dlp\n"
                                                         "(also requires ffmpeg on your PATH)", parent=self)
            return
        jobs = [(i, self.items[i]["track"]) for i in range(len(self.items)) if self.include_vars[i].get()]
        if not jobs:
            messagebox.showinfo("Nothing selected", "Tick \"Include in Download All\" on the tracks to fetch.",
                                parent=self)
            return
        self.download_all_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        for i, _ in jobs:
            self.set_include_state(i, "disabled")
        self.batch = BatchDownloader(self, jobs, self.download_dir(), self.dl_opts, self.log,
                                     self.set_row_status, self.on_batch_downloaded, self.on_batch_finished)
        self.batch.start()

    def cancel_downloads(self):
        if self.batch:
            self.batch.cancel()
            self.cancel_btn.configure(state="disabled")
            self.log("Cancelling - downloads in progress will stop, queued ones won't start.")

    def on_batch_finished(self, counts):
        self.batch = None
        if not self.winfo_exists():
            return
        self.cancel_btn.configure(state="disabled")
        self.download_all_btn.configure(state="normal")
        for i in self.include_vars:
            if not self.row_done(i):
                self.set_include_state(i, "normal")
        messagebox.showinfo(
            "Download All finished",
            f"{counts['downloaded']} downloaded\n{counts['existing']} already in folder\n"
            f"{counts['manual']} need a manual pick (see the rows marked \"No confident match\")\n"
            f"{counts['failed']} failed" + (f"\n{counts['cancelled']} cancelled" if counts["cancelled"] else ""),
            parent=self,
        )

    def stop_downloads_first(self):
        """True if it's OK to close/finish now - asks first (and cancels)
        if a Download All is still running."""
        if not self.batch:
            return True
        if not messagebox.askyesno("Downloads running", "Downloads are still running. Cancel them?", parent=self):
            return False
        self.batch.cancel()
        return True

    def close(self):
        if self.stop_downloads_first():
            self.destroy()


class DownloadReviewWindow(PagedReviewMixin, BatchDownloadMixin, tk.Toplevel):
    """Lists every playlist track with its status against the checked folder
    (found / missing, with score). Download All Selected fetches every
    ticked row automatically (missing/uncertain rows are ticked by
    default); Search YouTube picks a result by hand for any single row.
    Nothing here builds a playlist - it's purely about filling gaps in
    your library."""

    def __init__(self, parent, items, dest_dir, dl_opts, log):
        super().__init__(parent)
        self.title("TrackMatch - Download Review")
        self.geometry("820x640")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)

        self.items = items
        self.dest_dir = dest_dir
        self.dl_opts = dl_opts
        self.log = log
        self.batch = None
        self.match_text = {}      # idx -> found / possible / not found line
        self.match_kinds = {}     # idx -> 'found'/'missing', read by the match label's theme closure
        self.match_labels = {}    # visible rows only
        self.include_vars = {}
        for i, item in enumerate(items):
            candidate, score = item["candidate"], item["score"]
            if candidate and score >= FOUND_THRESHOLD:
                self.match_text[i] = f"Found: \"{candidate['path'].name}\" (score {score:.2f})"
                self.match_kinds[i] = "found"
            elif candidate:
                self.match_text[i] = (f"Possible match: \"{candidate['path'].name}\" (score {score:.2f}) - "
                                      f"check carefully")
                self.match_kinds[i] = "missing"
            else:
                self.match_text[i] = "Not found in the folder"
                self.match_kinds[i] = "missing"
            self.include_vars[i] = tk.BooleanVar(value=score < FOUND_THRESHOLD)

        found = sum(1 for it in items if it["score"] >= FOUND_THRESHOLD)
        header = tk.Label(
            self,
            text=f"{found}/{len(items)} already look present (score >= {FOUND_THRESHOLD:.2f}). "
                 f"Missing/uncertain tracks are listed first and ticked for Download All - it picks the "
                 f"best YouTube result for each automatically, and leaves any without a confident match "
                 f"for you to pick with Search YouTube.\nSaving to: {dest_dir}   ({dl_opts.describe()})",
            justify="left", wraplength=790
        )
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 0))

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(side="bottom", fill="x", padx=10, pady=10)
        self.download_all_btn, self.cancel_btn = make_batch_controls(bottom, self.download_all, self.cancel_downloads)
        ThemedButton(bottom, text="Close", command=self.close, width=12).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.make_review_area()
        self.show_page(0)

    def on_page_cleared(self):
        self.match_labels.clear()

    def build_row(self, i, parent):
        track = self.items[i]["track"]
        row = tk.Frame(parent, relief="groove", borderwidth=1, highlightthickness=1)
        style_row_frame(row)
        row.pack(fill="x", pady=4, padx=2)
        title_label = tk.Label(row, text=f"{track['artist']} - {track['title']}", justify="left", anchor="w")
        style_label(title_label)
        title_label.pack(fill="x", padx=6, pady=(4, 0))
        match_label = tk.Label(row, text=self.match_text[i], justify="left", anchor="w")
        register(lambda lbl=match_label, idx=i: lbl.configure(
            bg=THEME["BG"], fg=THEME["FOUND_FG" if self.match_kinds[idx] == "found" else "MISSING_FG"]))
        match_label.pack(fill="x", padx=6, pady=(0, 2))
        self.match_labels[i] = match_label
        control_frame = tk.Frame(row); style_frame(control_frame)
        control_frame.pack(fill="x", padx=6, pady=(0, 6))
        ThemedButton(control_frame, text="Search YouTube",
                     command=lambda idx=i: self.search_youtube_for(idx)).pack(side="left")
        self.add_include_check(i, control_frame, padx=(10, 0))
        self.add_status_label(i, row)

    def download_dir(self):
        return self.dest_dir

    def row_done(self, idx):
        return self.match_kinds[idx] == "found"

    def search_youtube_for(self, idx):
        track = self.items[idx]["track"]
        query = f"{track['artist']} {track['title']}".strip()
        self.set_row_status(idx, "Searching YouTube...")

        def do_search():
            try:
                results = search_youtube(query, max_results=YouTubeResultsWindow.RESULTS_PER_PAGE)
                self.set_row_status(idx, "" if results else "No results found.")
                if results:
                    self.after(0, lambda: YouTubeResultsWindow(
                        self, idx, track, results, self.dl_opts, self.dest_dir, self.on_downloaded, self.log
                    ))
            except Exception as e:
                self.set_row_status(idx, f"Search failed: {e}")

        threading.Thread(target=do_search, daemon=True).start()

    def on_batch_downloaded(self, idx, file_path):
        self.on_downloaded(idx, file_path)

    def on_downloaded(self, idx, file_path):
        """Marks the row as found, the same as if it had been in the folder
        from the start, and takes it out of any later Download All."""
        self.match_kinds[idx] = "found"
        self.match_text[idx] = f"Found: \"{file_path.name}\" (downloaded)"
        label = self.match_labels.get(idx)
        if label is not None and label.winfo_exists():
            label.configure(text=self.match_text[idx], fg=THEME["FOUND_FG"])
        self.include_vars[idx].set(False)
        self.set_include_state(idx, "disabled")

# ---------- Tab 2: Create Playlist ----------

class PlaylistTab(tk.Frame):
    """Two modes: a playlist from a link, file or pasted list (CsvPlaylistPanel), or
    playlists built from a music folder's own tags (LibraryPlaylistPanel)."""

    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)
        switch_row = tk.Frame(self); style_frame(switch_row)
        switch_row.pack(fill="x", padx=10, pady=(8, 0))
        l_source = tk.Label(switch_row, text="Make playlists:", font="TMSubheading"); style_label(l_source)
        l_source.pack(side="left", padx=(0, 10))
        self.source_var = tk.StringVar(value="csv")
        for text, value in (("From a playlist (link, file or pasted songs)", "csv"),
                            ("From my music library (tags)", "library")):
            rb = ThemedRadiobutton(switch_row, text=text, variable=self.source_var, value=value,
                                command=self.show_panel)
            style_radiobutton(rb)
            rb.pack(side="left", padx=(0, 12))
        self.csv_panel = CsvPlaylistPanel(self)
        self.library_panel = LibraryPlaylistPanel(self)
        self.show_panel()

    def show_panel(self):
        library = self.source_var.get() == "library"
        self.csv_panel.pack_forget()
        self.library_panel.pack_forget()
        (self.library_panel if library else self.csv_panel).pack(fill="both", expand=True)


class CsvPlaylistPanel(LogPanelMixin, tk.Frame):
    """Create Playlist tab, "From a playlist" mode - any source PlaylistSourceInput accepts."""

    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)

        playlist = make_section(self, "Playlist", PLAYLIST_SOURCE_HELP)
        self.playlist_input = PlaylistSourceInput(playlist)

        folder = make_section(self, "Your music")
        self.music_dir_entry = make_path_row(
            folder, "Music folder to search:", lambda e: browse_folder_into(e, "Select your music folder"),
            placeholder="TrackMatch finds each playlist song in here",
            info="Every song in the playlist is matched to the best file in this folder (and its subfolders). "
                 "You check every match before anything is saved."
        )

        output = make_section(self, "Output")
        mode_row = tk.Frame(output); style_frame(mode_row)
        mode_row.pack(fill="x", pady=(2, 0))
        self.mode_var = tk.StringVar(value="m3u")
        for text, value in (("M3U8 playlist", "m3u"), ("Copy the songs into a folder", "copy")):
            ThemedRadiobutton(mode_row, text=text, variable=self.mode_var, value=value,
                              command=self.update_output_mode_ui).pack(side="left", padx=(0, 14))
        head = tk.Frame(output); style_frame(head)
        head.pack(fill="x", pady=(8, 2))
        self.output_label = tk.Label(head, text="Save playlist as:")
        style_label(self.output_label)
        self.output_label.pack(side="left")
        info_button(head, lambda: self.output_help(), "Output").pack(side="left", padx=(4, 0))
        out_frame = tk.Frame(output); style_frame(out_frame)
        out_frame.pack(fill="x")
        self.output_entry = ThemedEntry(out_frame)
        self.output_entry.pack(side="left", fill="x", expand=True)
        ThemedButton(out_frame, text="Browse...", command=self.browse_output).pack(side="left", padx=(6, 0))

        fold = Fold(output, "YouTube options for missing songs")
        self.get_download_options = make_download_options(fold.body)

        self.run_button, _, self.progress, self.status_line = make_action_row(
            self, "Match & Review", self.run_clicked)

        log_label = tk.Label(self, text="Log:"); style_label(log_label)
        log_label.pack(anchor="w", padx=12, pady=(8, 0))
        self.log_box = ThemedScrolledText(self, height=8, state="disabled", wrap="word")
        style_scrolledtext(self.log_box)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        register_log_panel(log_label, self.log_box)

        if not MutagenFile:
            self.log("WARNING: 'mutagen' is not installed. Run: pip install mutagen")
        if not yt_dlp:
            self.log("NOTE: 'yt-dlp' is not installed - the YouTube fallback option won't work until you "
                      "run: pip install yt-dlp (also requires ffmpeg on your PATH)")

    def output_help(self):
        if self.mode_var.get() == "m3u":
            return ("Saves an .m3u8 playlist. Its song paths are relative to the playlist file's location, so keep "
                    "the playlist and music folder together. Rekordbox: File > Import > Import Playlist.")
        return "Matched songs are copied (not moved) into this folder - your music folder is left untouched."

    def update_output_mode_ui(self):
        self.output_label.configure(text="Save playlist as:" if self.mode_var.get() == "m3u"
                                    else "Copy songs into folder:")

    def browse_output(self):
        if self.mode_var.get() == "m3u":
            path = filedialog.asksaveasfilename(
                title="Save playlist as",
                defaultextension=".m3u8",
                filetypes=[("M3U8 playlist", "*.m3u8"), ("M3U playlist", "*.m3u")],
            )
        else:
            path = filedialog.askdirectory(title="Select folder to copy matched files into")
        if path:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, path)

    def run_clicked(self):
        source = self.playlist_input.get()
        music_dir = self.music_dir_entry.get().strip()
        output_path = self.output_entry.get().strip()
        mode = self.mode_var.get()

        if self.playlist_input.is_empty():
            messagebox.showerror("Missing info", "Paste a playlist link, browse to a playlist file, or use "
                                                 "Paste Songs.")
            return
        if not music_dir or not Path(music_dir).is_dir():
            messagebox.showerror("Missing info", "Choose a valid music folder.")
            return
        if not output_path:
            label = "Choose where to save the playlist." if mode == "m3u" else "Choose a folder to copy files into."
            messagebox.showerror("Missing info", label)
            return
        if not MutagenFile:
            messagebox.showerror("Missing dependency", "Install requirements first:\npip install mutagen")
            return

        self.run_button.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        dl_opts = self.get_download_options()
        thread = threading.Thread(
            target=self.worker, args=(source, music_dir, output_path, mode, dl_opts), daemon=True
        )
        thread.start()

    def worker(self, source, music_dir, output_path, mode, dl_opts):
        try:
            output = Path(output_path)
            if mode == "m3u" and output.suffix.lower() not in (".m3u", ".m3u8"):
                output = output.with_suffix(".m3u8")

            self.log("Reading playlist...")
            tracks = load_playlist_source(*source, self.log)

            index = index_music_folder(Path(music_dir), self.log)
            if not index:
                self.log("No audio files found in that folder.")
                return

            self.log("Finding the best guess for each track...")
            items = []
            for track in tracks:
                candidate, score = find_best_match(track, index)
                items.append({"track": track, "candidate": candidate, "score": score})

            # Worst matches first, so the ones most likely to be wrong are what
            # you see (and fix) first, rather than buried at the bottom.
            items.sort(key=lambda it: it["score"])

            self.log(f"\nReady for review - {len(items)} tracks. Opening review window...")
            self.after(0, lambda: self.open_review(items, output, music_dir, len(tracks), mode, dl_opts))

        except Exception as e:
            self.log(f"ERROR: {e}")
            SESSION_LOG.exception(log_source_name(self), type(e), e, e.__traceback__)
            self.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.run_button.configure(state="normal"))

    def open_review(self, items, output, music_dir, total_tracks, mode, dl_opts):
        ReviewWindow(self, items, output, music_dir, total_tracks, self.finish_and_write, self.log,
                     mode, dl_opts)

    def finish_and_write(self, matches, output, total_tracks, mode):
        if mode == "m3u":
            output.parent.mkdir(parents=True, exist_ok=True)
            write_m3u(output, matches)
            self.log(f"\nDone. Saved {len(matches)}/{total_tracks} tracks to: {output}")
            messagebox.showinfo("Done", f"Saved {len(matches)}/{total_tracks} tracks.\nSaved to:\n{output}")
        else:
            self.log(f"\nCopying {len(matches)} matched files into: {output}")
            copied = copy_files_to_folder(output, matches, self.log)
            self.log(f"Done. Copied {copied}/{total_tracks} tracks to: {output}")
            messagebox.showinfo("Done", f"Copied {copied}/{total_tracks} tracks.\nInto:\n{output}")


class ReviewWindow(PagedReviewMixin, BatchDownloadMixin, tk.Toplevel):
    """Shows every track with its best-guess local match pre-filled, worst
    scores first, so nothing is written to disk until you've confirmed
    (or corrected) each one. Browse to fix a wrong guess, Search YouTube
    to fetch one that isn't in your library, Download All Selected to
    fetch every ticked one automatically, or Skip to leave it out."""

    def __init__(self, parent, items, output, music_dir, total_tracks, on_finish, log, mode, dl_opts):
        super().__init__(parent)
        self.title("TrackMatch - Review Tracks")
        self.geometry("800x600")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)

        self.items = items                # every track: [{"track","candidate","score"}, ...]
        self.output = output
        self.music_dir = music_dir
        self.total_tracks = total_tracks
        self.on_finish = on_finish
        self.log = log
        self.mode = mode
        self.dl_opts = dl_opts

        # Pre-fill each row with its best guess (empty if nothing was found at all)
        self.chosen_paths = [
            tk.StringVar(value=str(it["candidate"]["path"]) if it["candidate"] else "")
            for it in items
        ]
        # Weak guesses are left out unless picked by hand (Browse or a download un-skips them)
        self.skip_flags = [tk.BooleanVar(value=it["score"] < FOUND_THRESHOLD) for it in items]
        self.include_vars = {i: tk.BooleanVar(value=it["score"] < FOUND_THRESHOLD)
                             for i, it in enumerate(items)}
        self.downloaded = set()  # rows filled by a YouTube download

        header = tk.Label(
            self,
            text=f"Reviewing all {len(items)} tracks, worst matches first. Confirm, fix, or skip each one - "
                 f"nothing is saved until you click Finish. Weak guesses start skipped - untick Skip to keep one.",
            justify="left", wraplength=770
        )
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 0))

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(side="bottom", fill="x", padx=10, pady=10)
        self.download_all_btn, self.cancel_btn = make_batch_controls(bottom, self.download_all, self.cancel_downloads)
        ThemedButton(bottom, text="Finish & Save Playlist", command=self.finish, accent=True).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.make_review_area()
        self.show_page(0)

    def build_row(self, i, parent):
        item = self.items[i]
        track, candidate, score = item["track"], item["candidate"], item["score"]
        row = tk.Frame(parent, relief="groove", borderwidth=1, highlightthickness=1)
        style_row_frame(row)
        row.pack(fill="x", pady=4, padx=2)
        label_text = f"{track['artist']} - {track['title']}"
        if candidate:
            label_text += f"\nBest guess: \"{candidate['path'].name}\" (score {score:.2f})"
        else:
            label_text += "\nNo candidate found in the folder."
        row_label = tk.Label(row, text=label_text, justify="left", anchor="w")
        style_label(row_label)
        row_label.pack(fill="x", padx=6, pady=(4, 2))
        control_frame = tk.Frame(row); style_frame(control_frame)
        control_frame.pack(fill="x", padx=6, pady=(0, 6))
        ThemedEntry(control_frame, textvariable=self.chosen_paths[i]).pack(side="left", fill="x", expand=True)
        ThemedButton(control_frame, text="Browse...", command=lambda idx=i: self.browse_for(idx)).pack(side="left",
                                                                                                    padx=(6, 6))
        ThemedButton(control_frame, text="Search YouTube",
                     command=lambda idx=i: self.search_youtube_for(idx)).pack(side="left", padx=(0, 6))
        ThemedCheckbutton(control_frame, text="Skip", variable=self.skip_flags[i]).pack(side="left")
        self.add_include_check(i, control_frame, padx=(6, 0))
        self.add_status_label(i, row)

    def download_dir(self):
        return self.music_dir

    def row_done(self, idx):
        return idx in self.downloaded

    def on_batch_downloaded(self, idx, file_path):
        self.on_youtube_downloaded(idx, file_path)

    def search_youtube_for(self, idx):
        if yt_dlp is None:
            messagebox.showerror("Missing dependency", "yt-dlp is not installed.\nRun: pip install yt-dlp\n"
                                                         "(also requires ffmpeg on your PATH)")
            return

        track = self.items[idx]["track"]
        query = f"{track['artist']} {track['title']}".strip()
        self.set_row_status(idx, "Searching YouTube...")

        def do_search():
            try:
                results = search_youtube(query, max_results=YouTubeResultsWindow.RESULTS_PER_PAGE)
                self.set_row_status(idx, "" if results else "No results found.")
                if results:
                    self.after(0, lambda: YouTubeResultsWindow(
                        self, idx, track, results, self.dl_opts,
                        self.music_dir, self.on_youtube_downloaded, self.log
                    ))
            except Exception as e:
                self.set_row_status(idx, f"Search failed: {e}")

        threading.Thread(target=do_search, daemon=True).start()

    def on_youtube_downloaded(self, idx, file_path):
        self.chosen_paths[idx].set(str(file_path))
        self.skip_flags[idx].set(False)
        self.set_row_status(idx, f"Downloaded: {file_path.name}")
        self.downloaded.add(idx)
        self.include_vars[idx].set(False)
        self.set_include_state(idx, "disabled")

    def browse_for(self, idx):
        path = filedialog.askopenfilename(
            title="Select the matching audio file",
            initialdir=self.music_dir,
            filetypes=[("Audio files", " ".join(f"*{ext}" for ext in AUDIO_EXTENSIONS)), ("All files", "*.*")],
        )
        if path:
            self.chosen_paths[idx].set(path)
            self.skip_flags[idx].set(False)

    def finish(self):
        if not self.stop_downloads_first():
            return
        matches = []
        confirmed, skipped = 0, 0
        for i, item in enumerate(self.items):
            track = item["track"]
            if self.skip_flags[i].get():
                skipped += 1
                continue
            chosen = self.chosen_paths[i].get().strip()
            if chosen:
                matches.append({"path": Path(chosen), "title": track["title"], "artist": track["artist"]})
                confirmed += 1
            else:
                skipped += 1  # left blank and not explicitly skipped - treat as skipped

        self.log(f"Review complete: {confirmed} confirmed, {skipped} skipped.")
        self.destroy()
        self.on_finish(matches, self.output, self.total_tracks, self.mode)


class YouTubeResultsWindow(tk.Toplevel):
    """Shows YouTube search results for one track so the person can pick the
    right one, then downloads + tags it (see fetch_youtube_track). The
    best-scoring result (length, title/artist, official-audio uploads - see
    score_youtube_result) is pre-selected and highlighted. The saved file
    is named after the playlist's own Artist - Title, not the YouTube
    video's title. The search query can be edited and re-run, and more
    results can be pulled in without closing the window."""

    RESULTS_PER_PAGE = 5

    def __init__(self, parent, row_idx, track, results, dl_opts, dest_dir, on_downloaded, log):
        super().__init__(parent)
        self.title(f"YouTube results: {track['artist']} - {track['title']}")
        self.geometry("640x540")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)

        self.row_idx = row_idx
        self.track = track
        self.results = results
        self.dl_opts = dl_opts
        self.dest_dir = dest_dir
        self.on_downloaded = on_downloaded
        self.log = log
        self.result_count = max(len(results), self.RESULTS_PER_PAGE)

        playlist_seconds = (track["duration_ms"] / 1000) if track.get("duration_ms") else None
        header_text = f"Pick the correct video for:\n{track['artist']} - {track['title']}"
        if playlist_seconds is not None:
            header_text += f"\nPlaylist length: {format_duration(playlist_seconds)}"
        header = tk.Label(self, text=header_text, justify="left")
        style_label(header)
        header.pack(anchor="w", padx=10, pady=(10, 6))

        # Editable search query
        query_row = tk.Frame(self); style_frame(query_row)
        query_row.pack(fill="x", padx=10)
        l_query = tk.Label(query_row, text="Search query:")
        style_label(l_query, subtle=True)
        l_query.pack(side="left", padx=(0, 6))
        self.query_var = tk.StringVar(value=f"{track['artist']} {track['title']}".strip())
        query_entry = ThemedEntry(query_row, textvariable=self.query_var)
        query_entry.pack(side="left", fill="x", expand=True)
        query_entry.bind("<Return>", lambda e: self.new_search())
        search_btn = ThemedButton(query_row, text="Search", command=self.new_search)
        search_btn.pack(side="left", padx=(6, 0))

        # Scrollable results list
        canvas = tk.Canvas(self, borderwidth=0)
        style_canvas(canvas)
        self.options_frame = tk.Frame(canvas); style_frame(self.options_frame)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 0))
        canvas.create_window((0, 0), window=self.options_frame, anchor="nw")
        self.options_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        make_wheel_scrollable(canvas)

        self.selected_idx = tk.IntVar(value=0)
        self.render_results()

        more_row = tk.Frame(self); style_frame(more_row)
        more_row.pack(fill="x", padx=10, pady=(4, 0))
        self.more_btn = ThemedButton(more_row, text="Show 5 More Results", command=self.show_more)
        self.more_btn.pack(side="left")

        desired_name = sanitize_filename(f"{track['artist']} - {track['title']}")
        self.status_label = tk.Label(
            self,
            text=f"{dl_opts.describe()}  |  Will save as: {desired_name}.{dl_opts.fmt}",
            justify="left", wraplength=620
        )
        style_label(self.status_label, subtle=True)
        self.status_label.pack(anchor="w", padx=10, pady=(8, 0))

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        self.download_btn = ThemedButton(bottom, text="Download Selected", command=self.download_selected, accent=True)
        self.download_btn.pack(side="right")
        cancel_btn = ThemedButton(bottom, text="Cancel", command=self.destroy, width=10)
        cancel_btn.pack(side="right", padx=(0, 6))

    def render_results(self):
        """(Re)draws the radio button list from self.results in YouTube's
        order, with the best-scoring result pre-selected and highlighted."""
        for child in self.options_frame.winfo_children():
            child.destroy()

        scores = [score_youtube_result(self.track, r) for r in self.results]
        best_idx = max(range(len(scores)), key=lambda i: scores[i]) if scores else 0

        self.selected_idx.set(best_idx)
        for i, r in enumerate(self.results):
            text = f"{r['title']}\n{r['uploader']} • {r['duration']}  •  match {min(1.0, scores[i]):.0%}"
            is_best = (i == best_idx)
            if is_best:
                text += "  (best match)"
            rb = ThemedRadiobutton(self.options_frame, text=text, variable=self.selected_idx, value=i,
                                 justify="left", anchor="w", wraplength=580)
            style_radiobutton(rb, highlighted=is_best)
            rb.pack(fill="x", pady=4, anchor="w")

    def set_status(self, text):
        def update():
            self.status_label.configure(text=text)
        try:
            self.after(0, update)
        except (tk.TclError, RuntimeError):
            pass  # window closed mid-download

    def set_controls_state(self, state):
        def update():
            self.download_btn.configure(state=state)
            self.more_btn.configure(state=state)
        try:
            self.after(0, update)
        except (tk.TclError, RuntimeError):
            pass

    def new_search(self):
        """Re-runs the search with whatever is currently in the query box,
        resetting back to the first page of results."""
        query = self.query_var.get().strip()
        if not query:
            return
        self.result_count = self.RESULTS_PER_PAGE
        self.set_status("Searching...")
        self.set_controls_state("disabled")

        def do_search():
            try:
                results = search_youtube(query, max_results=self.result_count)
                self.results = results
                self.after(0, self.render_results)
                self.set_status("No results found." if not results else "")
            except Exception as e:
                self.set_status(f"Search failed: {e}")
            finally:
                self.set_controls_state("normal")

        threading.Thread(target=do_search, daemon=True).start()

    def show_more(self):
        """Re-runs the same search asking for RESULTS_PER_PAGE more results
        than before. yt-dlp's search isn't paginated, so this re-asks for a
        bigger batch - cheap now that search is a single flat request."""
        query = self.query_var.get().strip()
        if not query:
            return
        self.result_count += self.RESULTS_PER_PAGE
        self.set_status("Loading more results...")
        self.set_controls_state("disabled")

        def do_search():
            try:
                results = search_youtube(query, max_results=self.result_count)
                self.results = results
                self.after(0, self.render_results)
                self.set_status("")
            except Exception as e:
                self.set_status(f"Search failed: {e}")
            finally:
                self.set_controls_state("normal")

        threading.Thread(target=do_search, daemon=True).start()

    def download_selected(self):
        idx = self.selected_idx.get()
        if idx >= len(self.results):
            return
        video = self.results[idx]
        self.download_btn.configure(state="disabled")
        self.status_label.configure(text="Starting download...")

        def do_download():
            try:
                file_path, status = fetch_youtube_track(
                    self.track, video, Path(self.dest_dir), self.dl_opts, self.log, progress=self.set_status
                )
                self.after(0, lambda: self.on_downloaded(self.row_idx, file_path))
                self.after(0, lambda: self.on_downloaded_status(status))
                self.after(0, self.destroy)
            except Exception as e:
                self.log(f"  Download failed: {e}")
                self.set_status(f"Failed: {e}")
                self.set_controls_state("normal")

        threading.Thread(target=do_download, daemon=True).start()

    def on_downloaded_status(self, status):
        """Shows the final status (e.g. which source's tags were applied)
        on the parent review window's row, since this window then closes."""
        parent = self.master
        if hasattr(parent, "set_row_status"):
            parent.set_row_status(self.row_idx, status)


# ---------- Playlists from library tags (Create Playlist tab) ----------

UNKNOWN_GENRE = "Unknown Genre"
UNKNOWN_YEAR = "Unknown Year"
UNKNOWN_BPM = "Unknown BPM"
UNKNOWN_ARTIST = "Unknown Artist"
OTHER_GENRES = "Other Genres"

GROUP_GENRE = "Genre"
GROUP_GENRE_YEAR = "Genre + Year"
GROUP_YEAR = "Year"
GROUP_ARTIST = "Artist"
GROUP_BPM = "BPM range"
GROUP_GENRE_BPM = "Genre + BPM range"
GROUP_CHOICES = [GROUP_GENRE, GROUP_GENRE_YEAR, GROUP_YEAR, GROUP_ARTIST, GROUP_BPM, GROUP_GENRE_BPM]

YEARS_EVERY = "Every year individually"
YEARS_DECADES = "Decades only (80s, 90s, 2000s...)"
YEARS_FROM = "Individually from"
YEAR_MODES = [YEARS_FROM, YEARS_EVERY, YEARS_DECADES]


def _first_value(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip() if value is not None else ""


def read_library_tags(path: Path):
    """The tags playlist grouping uses - title, artist, genre, year, BPM -
    without loading cover art, so scanning thousands of files stays fast."""
    info = {"path": path, "title": "", "artist": "", "genre": "", "year": None, "bpm": None}
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        audio = None
    tags = getattr(audio, "tags", None) if audio is not None else None
    raw = {}
    if tags is not None:
        if isinstance(tags, ID3):  # wav/aiff have no "easy" mode - read the frames directly
            for field, frame_id in (("title", "TIT2"), ("artist", "TPE1"), ("genre", "TCON"),
                                    ("date", "TDRC"), ("bpm", "TBPM")):
                frame = tags.get(frame_id)
                if frame is not None and frame.text:
                    raw[field] = str(frame.text[0])
        else:
            for field in ("title", "artist", "genre", "date", "bpm"):
                try:
                    raw[field] = _first_value(tags.get(field))
                except (KeyError, ValueError):
                    pass

    info["title"] = raw.get("title") or ""
    info["artist"] = raw.get("artist") or ""
    if not info["title"]:
        stem = path.stem
        if " - " in stem:
            info["artist"], info["title"] = (info["artist"] or stem.split(" - ", 1)[0]), stem.split(" - ", 1)[1]
        else:
            info["title"] = stem
    # A genre tag holding several ("House; Tech House") uses the first one.
    # Not split on "/" - Beatport genres like "Dance / Pop" contain it.
    info["genre"] = (raw.get("genre") or "").split(";")[0].strip()
    year_match = re.match(r"\s*(\d{4})", raw.get("date") or "")
    if year_match and 1900 <= int(year_match.group(1)) <= 2100:
        info["year"] = int(year_match.group(1))
    try:
        bpm = float(raw.get("bpm") or 0)
        info["bpm"] = int(round(bpm)) if 40 <= bpm <= 250 else None
    except ValueError:
        pass
    return info


def genre_key(genre: str) -> str:
    """'Tech House', 'tech house' and 'Tech-House' all group together."""
    return re.sub(r"[\s\-_]+", " ", genre.lower()).strip()


def scan_library(folder: Path, recursive, progress=None):
    """Reads every audio file's tags. Returns (songs, genres) where genres
    is [(display name, count), ...] most common first - the display name
    being each genre's most-used spelling."""
    pattern = folder.rglob("*") if recursive else folder.glob("*")
    files = [p for p in pattern if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]
    songs = []
    done = [0]
    lock = threading.Lock()

    def read(path):
        song = read_library_tags(path)
        with lock:
            done[0] += 1
            if progress and done[0] % 50 == 0:
                progress(done[0], len(files))
        return song

    with ThreadPoolExecutor(max_workers=8) as pool:
        songs = list(pool.map(read, files))
    if progress:
        progress(len(files), len(files))

    spellings = {}
    for s in songs:
        if s["genre"]:
            key = genre_key(s["genre"])
            s["genre_key"] = key
            spellings.setdefault(key, {}).setdefault(s["genre"], 0)
            spellings[key][s["genre"]] += 1
        else:
            s["genre_key"] = ""
    genres = []
    for key, variants in spellings.items():
        display = max(variants.items(), key=lambda kv: kv[1])[0]
        genres.append((key, display, sum(variants.values())))
    genres.sort(key=lambda g: (-g[2], g[1].lower()))
    return songs, genres


def year_label(year, year_mode, from_year):
    if year is None:
        return UNKNOWN_YEAR
    if year_mode == YEARS_EVERY or (year_mode == YEARS_FROM and year >= from_year):
        return str(year)
    decade = year // 10 * 10
    return f"{decade % 100:02d}s" if decade < 2000 else f"{decade}s"


def bpm_label(bpm, step):
    if bpm is None:
        return UNKNOWN_BPM
    start = bpm // step * step
    return f"{start}-{start + step - 1} BPM"


def main_artist(artist: str) -> str:
    first = re.split(r"\s*(?:,|;|&|\bfeat\.?\s|\bft\.?\s|\bx\s)\s*", artist or "", maxsplit=1)[0].strip()
    return first or UNKNOWN_ARTIST


def left_out_reason(song, main_genres, include):
    """Why the Include options leave a song out of every playlist, or None.
    They filter songs for all groupings - e.g. with "Other Genres" off, even
    Year or Artist playlists only contain your ticked genres."""
    if not song["genre_key"]:
        if not include["unknown_genre"]:
            return "with no genre"
    elif song["genre_key"] not in main_genres and not include["other_genres"]:
        return "in other genres"
    if song["year"] is None and not include["unknown_year"]:
        return "with no year"
    if song["bpm"] is None and not include["unknown_bpm"]:
        return "with no BPM"
    return None


def count_left_out(songs, main_genres, include):
    counts = {}
    for song in songs:
        reason = left_out_reason(song, main_genres, include)
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def left_out_summary(counts):
    """'Leaving out 312 song(s): 290 in other genres, 22 with no genre', or ''."""
    total = sum(counts.values())
    if not total:
        return ""
    return f"Leaving out {total} song(s): " + ", ".join(f"{n} {reason}" for reason, n in counts.items())


def group_songs(songs, main_genres, genre_names, grouping, year_mode, from_year, bpm_step, genre_first,
                include=None):
    """Splits songs into playlists, skipping any the include options (see
    left_out_reason) leave out. Returns ({playlist name parts tuple: [songs]},
    {reason: songs left out}). Parts are in display order (genre first or
    year/BPM first) - joined with a space for a playlist/folder name, or used
    as nested folder levels."""
    include = include or LIBRARY_INCLUDE
    groups = {}
    left_out = {}
    for s in songs:
        reason = left_out_reason(s, main_genres, include)
        if reason:
            left_out[reason] = left_out.get(reason, 0) + 1
            continue
        if not s["genre_key"]:
            genre = UNKNOWN_GENRE
        elif s["genre_key"] in main_genres:
            genre = genre_names[s["genre_key"]]
        else:
            genre = OTHER_GENRES
        year = year_label(s["year"], year_mode, from_year)
        bpm = bpm_label(s["bpm"], bpm_step)

        if grouping == GROUP_GENRE:
            parts = (genre,)
        elif grouping == GROUP_YEAR:
            parts = (year,)
        elif grouping == GROUP_ARTIST:
            parts = (main_artist(s["artist"]),)
        elif grouping == GROUP_BPM:
            parts = (bpm,)
        elif grouping == GROUP_GENRE_YEAR:
            parts = (genre, year) if genre_first else (year, genre)
        else:  # GROUP_GENRE_BPM
            parts = (genre, bpm) if genre_first else (bpm, genre)
        groups.setdefault(parts, []).append(s)
    for members in groups.values():
        members.sort(key=lambda s: (s["artist"].lower(), s["title"].lower()))
    return groups, left_out


def playlist_name(parts):
    return " ".join(parts)


def write_library_playlists(groups, out_dir: Path, mode, nested, log, progress=None, cancel_event=None):
    """Writes one .m3u8 per group into out_dir, or copies each group's
    songs into its own folder (nested = one folder level per name part).
    Copies skip files already there with the same size, so re-running onto
    the same USB only copies what's new. Returns a summary dict."""
    summary = {"playlists": 0, "songs": 0, "copied": 0, "already": 0, "failed": 0}
    out_dir.mkdir(parents=True, exist_ok=True)
    total = sum(len(v) for v in groups.values())
    done = 0
    for parts, members in sorted(groups.items(), key=lambda kv: [p.lower() for p in kv[0]]):
        if cancel_event is not None and cancel_event.is_set():
            break
        name = playlist_name(parts)
        if mode == "m3u":
            path = out_dir / f"{sanitize_playlist_name(name)}.m3u8"
            write_m3u(path, members)
            log(f"  {path.name}: {len(members)} songs")
            done += len(members)
        else:
            folder = out_dir.joinpath(*(sanitize_playlist_name(p) for p in parts)) if nested \
                else out_dir / sanitize_playlist_name(name)
            folder.mkdir(parents=True, exist_ok=True)
            used = set()
            for s in members:
                if cancel_event is not None and cancel_event.is_set():
                    break
                src = s["path"]
                target = src.name
                counter = 2
                while target.lower() in used:
                    target = f"{src.stem} ({counter}){src.suffix}"
                    counter += 1
                used.add(target.lower())
                dest = folder / target
                try:
                    if dest.exists() and dest.stat().st_size == src.stat().st_size:
                        summary["already"] += 1
                    else:
                        shutil.copy2(src, dest)
                        summary["copied"] += 1
                except Exception as e:
                    summary["failed"] += 1
                    log(f"  Could not copy \"{src.name}\": {e}")
                done += 1
                if progress:
                    progress(done, total)
            log(f"  {folder.relative_to(out_dir)}: {len(members)} songs")
        summary["playlists"] += 1
        summary["songs"] += len(members)
        if progress:
            progress(done, total)
    return summary


class LibraryPlaylistPanel(LogPanelMixin, tk.Frame):
    """Create Playlist tab, "From my music library" mode: scan a folder's
    tags, pick main genres (the rest become "Other Genres"), group by genre /
    year / artist / BPM, preview, then write .m3u8 playlists or copy the
    songs into one folder per playlist."""

    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)
        self.songs = []
        self.genres = []       # [(key, display name, count)]
        self.busy = False

        folder = make_section(self, "Music folder",
                              "Builds playlists from the tags already in your files - genre, year, artist and "
                              "BPM - with no internet needed. Works best after tagging the folder in the Tagging tab.")
        self.folder_entry = make_path_row(folder, "", lambda e: browse_folder_into(e, "Select your music folder"),
                                          placeholder="The folder to make playlists from")
        scan_row = tk.Frame(folder); style_frame(scan_row)
        scan_row.pack(fill="x")
        self.scan_button = ThemedButton(scan_row, text="Scan Folder", command=self.scan_clicked)
        self.scan_button.pack(side="left")
        self.recursive_var = tk.BooleanVar(value=True)
        ThemedCheckbutton(scan_row, text="Include subfolders", variable=self.recursive_var).pack(side="left", padx=(10, 0))
        self.scan_status = tk.Label(scan_row, text="", anchor="w")
        style_label(self.scan_status, subtle=True)
        self.scan_status.pack(side="left", padx=(10, 0))

        genres = make_section(self, "Main genres",
                              "Each ticked genre gets its own playlists. Everything else goes into \"Other Genres\" - "
                              "or is left out, if you untick \"Other Genres\" under Include. Click a genre to tick or "
                              "untick it.\n\nThe Include options also apply to Year, Artist and BPM playlists, and "
                              "are remembered next time.")
        list_frame = tk.Frame(genres); style_frame(list_frame)
        list_frame.pack(fill="x")
        self.genre_list = tk.Listbox(list_frame, selectmode="multiple", height=6, activestyle="none",
                                     exportselection=False)
        style_listbox(self.genre_list)
        genre_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.genre_list.yview)
        self.genre_list.configure(yscrollcommand=genre_scroll.set)
        genre_scroll.pack(side="right", fill="y")
        self.genre_list.pack(side="left", fill="x", expand=True)
        self.genre_list.bind("<<ListboxSelect>>", lambda e: self.update_selection_label())

        pick_row = tk.Frame(genres); style_frame(pick_row)
        pick_row.pack(fill="x", pady=(6, 0))
        for text, fn in (("Top 10", lambda: self.select_top(10)), ("All", self.select_all), ("None", self.clear_selection)):
            ThemedButton(pick_row, text=text, command=fn).pack(side="left", padx=(0, 6))
        self.selection_label = tk.Label(pick_row, text="", anchor="w", justify="left", wraplength=430)
        style_label(self.selection_label, subtle=True)
        self.selection_label.pack(side="left", padx=(6, 0))

        include_row = tk.Frame(genres); style_frame(include_row)
        include_row.pack(fill="x", pady=(6, 0))
        l_include = tk.Label(include_row, text="Include:"); style_label(l_include)
        l_include.pack(side="left", padx=(0, 6))
        self.include_vars = {}
        for key, text in (("other_genres", f"\"{OTHER_GENRES}\""), ("unknown_genre", "No genre"),
                          ("unknown_year", "No year"), ("unknown_bpm", "No BPM")):
            var = tk.BooleanVar(value=LIBRARY_INCLUDE[key])
            ThemedCheckbutton(include_row, text=text, variable=var,
                              command=lambda k=key, v=var: (set_library_include(k, v.get()),
                                                            self.update_selection_label())).pack(side="left", padx=(0, 10))
            self.include_vars[key] = var
        self.left_out_label = tk.Label(genres, text="", anchor="w")
        register(lambda: self.left_out_label.configure(bg=THEME["BG"], fg=THEME["MISSING_FG"]))

        lists = make_section(self, "Playlists")
        group_row = tk.Frame(lists); style_frame(group_row)
        group_row.pack(fill="x")
        l_group = tk.Label(group_row, text="Make playlists by:"); style_label(l_group)
        l_group.pack(side="left", padx=(0, 8))
        self.grouping_var = tk.StringVar(value=GROUP_GENRE_YEAR)
        ThemedOptionMenu(group_row, self.grouping_var, *GROUP_CHOICES).pack(side="left")
        self.bpm_frame = tk.Frame(group_row); style_frame(self.bpm_frame)
        l_bpm = tk.Label(self.bpm_frame, text="Range:"); style_label(l_bpm)
        l_bpm.pack(side="left", padx=(14, 8))
        self.bpm_step_var = tk.StringVar(value="5")
        self.bpm_menu = ThemedOptionMenu(self.bpm_frame, self.bpm_step_var, "5", "10")
        self.bpm_menu.pack(side="left")
        l_bpm2 = tk.Label(self.bpm_frame, text="BPM"); style_label(l_bpm2)
        l_bpm2.pack(side="left", padx=(6, 0))

        self.year_row = tk.Frame(lists); style_frame(self.year_row)
        l_years = tk.Label(self.year_row, text="Years:"); style_label(l_years)
        l_years.pack(side="left", padx=(0, 8))
        self.year_mode_var = tk.StringVar(value=YEARS_FROM)
        self.year_menu = ThemedOptionMenu(self.year_row, self.year_mode_var, *YEAR_MODES)
        self.year_menu.pack(side="left")
        self.year_from_frame = tk.Frame(self.year_row); style_frame(self.year_from_frame)
        this_year = date.today().year
        self.from_year_var = tk.StringVar(value=str(this_year - 2))
        self.from_year_spin = ThemedSpinbox(self.year_from_frame, from_=1950, to=this_year + 1, width=6,
                                            textvariable=self.from_year_var)
        self.from_year_spin.pack(side="left", padx=(6, 0))
        self.year_hint = info_button(self.year_from_frame, "Years from this one onwards get their own playlists "
                                                           "(e.g. 2026, 2025, 2024). Older songs are grouped by "
                                                           "decade - 2010s, 2000s, 90s, 80s.", "Years")
        self.year_hint.pack(side="left", padx=(4, 0))

        self.name_row = tk.Frame(lists); style_frame(self.name_row)
        l_name = tk.Label(self.name_row, text="Names:"); style_label(l_name)
        l_name.pack(side="left", padx=(0, 8))
        self.genre_first_var = tk.BooleanVar(value=False)
        self.year_first_radio = ThemedRadiobutton(self.name_row, variable=self.genre_first_var, value=False,
                                                  command=self.update_options)
        self.genre_first_radio = ThemedRadiobutton(self.name_row, variable=self.genre_first_var, value=True,
                                                   command=self.update_options)
        for rb in (self.year_first_radio, self.genre_first_radio):
            rb.pack(side="left", padx=(0, 12))

        output = make_section(self, "Output")
        out_row = tk.Frame(output); style_frame(out_row)
        out_row.pack(fill="x")
        self.mode_var = tk.StringVar(value="m3u")
        for text, value in (("M3U8 playlists", "m3u"), ("Copy songs into folders", "copy")):
            ThemedRadiobutton(out_row, text=text, variable=self.mode_var, value=value,
                              command=self.update_options).pack(side="left", padx=(0, 14))
        self.layout_row = tk.Frame(output); style_frame(self.layout_row)
        l_layout = tk.Label(self.layout_row, text="Folders:"); style_label(l_layout)
        l_layout.pack(side="left", padx=(0, 8))
        self.nested_var = tk.BooleanVar(value=False)
        self.flat_radio = ThemedRadiobutton(self.layout_row, variable=self.nested_var, value=False,
                                            command=self.update_options)
        self.nested_radio = ThemedRadiobutton(self.layout_row, variable=self.nested_var, value=True,
                                              command=self.update_options)
        for rb in (self.flat_radio, self.nested_radio):
            rb.pack(side="left", padx=(0, 12))
        self.output_head = tk.Frame(output); style_frame(self.output_head)
        self.output_head.pack(fill="x", pady=(8, 2))
        self.output_label = tk.Label(self.output_head, text="")
        style_label(self.output_label)
        self.output_label.pack(side="left")
        info_button(self.output_head, lambda: self.output_help(), "Output").pack(side="left", padx=(4, 0))
        out_frame = tk.Frame(output); style_frame(out_frame)
        out_frame.pack(fill="x")
        self.output_entry = ThemedEntry(out_frame)
        self.output_entry.pack(side="left", fill="x", expand=True)
        ThemedButton(out_frame, text="Browse...",
                     command=lambda: browse_folder_into(self.output_entry, "Choose the output folder")
                     ).pack(side="left", padx=(6, 0))

        self.preview_button, _, self.progress, self.status_line = make_action_row(
            self, "Preview Playlists", self.preview_clicked)
        self.progress_label = tk.Label(self.progress.master, text="", width=10, anchor="e")
        style_label(self.progress_label, subtle=True)
        self.progress_label.pack(side="left", padx=(6, 0))

        self.log_box = ThemedScrolledText(self, height=4, state="disabled", wrap="word")
        style_scrolledtext(self.log_box)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(6, 8))
        register_log_panel(self.log_box)

        for var in (self.grouping_var, self.year_mode_var, self.from_year_var, self.bpm_step_var):
            var.trace_add("write", lambda *_: self.update_options())
        self.update_options()
        self.update_selection_label()

    def output_help(self):
        if self.mode_var.get() == "copy":
            return ("Songs are copied, not moved - your music folder is untouched. Songs already in the "
                    "destination (same name and size) are skipped, so re-running only copies what's new.")
        return ("One .m3u8 per playlist, with paths relative to the playlist file - keep playlists and music on "
                "the same drive. Rekordbox: File > Import > Import Playlist.")

    def from_year(self):
        try:
            return int(self.from_year_var.get())
        except ValueError:
            return date.today().year - 2

    def example_parts(self):
        """Playlist name parts for a made-up Tech House / 2026 / 125 BPM song."""
        genre_first = self.genre_first_var.get()
        grouping = self.grouping_var.get()
        year = year_label(date.today().year, self.year_mode_var.get(), self.from_year())
        bpm = bpm_label(126, int(self.bpm_step_var.get()))
        if grouping == GROUP_GENRE_YEAR:
            return ("Tech House", year) if genre_first else (year, "Tech House")
        if grouping == GROUP_GENRE_BPM:
            return ("Tech House", bpm) if genre_first else (bpm, "Tech House")
        return {GROUP_GENRE: ("Tech House",), GROUP_YEAR: (year,), GROUP_ARTIST: ("FISHER",),
                GROUP_BPM: (bpm,)}[grouping]

    def update_options(self):
        """Shows only the options that apply to the chosen grouping/output."""
        grouping = self.grouping_var.get()
        uses_year = grouping in (GROUP_GENRE_YEAR, GROUP_YEAR)
        combined = grouping in (GROUP_GENRE_YEAR, GROUP_GENRE_BPM)
        set_visible(self.bpm_frame, grouping in (GROUP_BPM, GROUP_GENRE_BPM), side="left")
        set_visible(self.year_row, uses_year, fill="x", pady=(8, 0), before=self.name_row
                    if self.name_row.winfo_manager() else None)
        set_visible(self.year_from_frame, self.year_mode_var.get() == YEARS_FROM, side="left")
        set_visible(self.name_row, combined, fill="x", pady=(8, 0))

        second = "2026" if grouping == GROUP_GENRE_YEAR else "125-129 BPM"
        self.year_first_radio.configure(text=f"{second} Tech House")
        self.genre_first_radio.configure(text=f"Tech House {second}")

        copy = self.mode_var.get() == "copy"
        parts = self.example_parts()
        self.flat_radio.configure(text=f"Flat  ({playlist_name(parts)}/)")
        self.nested_radio.configure(text=f"Nested  ({'/'.join(parts)}/)",
                                    state="normal" if len(parts) > 1 else "disabled")
        set_visible(self.layout_row, copy, fill="x", pady=(8, 0), before=self.output_head)
        self.output_label.configure(text="Copy songs into folder:" if copy else "Save playlists into folder:")

    # --- genres ---

    def scan_clicked(self):
        folder = self.folder_entry.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("Missing info", "Choose a valid music folder.")
            return
        if not MutagenFile:
            messagebox.showerror("Missing dependency", "Install requirements first:\npip install mutagen")
            return
        self.scan_button.configure(state="disabled")
        self.scan_status.configure(text="Reading tags...")
        recursive = self.recursive_var.get()

        def progress(done, total):
            try:
                self.after(0, lambda: self.scan_status.configure(text=f"Reading tags... {done}/{total}"))
            except (tk.TclError, RuntimeError):
                pass

        def run():
            try:
                songs, genres = scan_library(Path(folder), recursive, progress)
                self.after(0, lambda: self.scan_finished(songs, genres, Path(folder)))
            except Exception as e:
                self.after(0, lambda err=e: (self.scan_button.configure(state="normal"),
                                             self.scan_status.configure(text=""),
                                             messagebox.showerror("Error", str(err))))

        threading.Thread(target=run, daemon=True).start()

    def scan_finished(self, songs, genres, folder):
        self.songs, self.genres = songs, genres
        self.scan_button.configure(state="normal")
        no_genre = sum(1 for s in songs if not s["genre_key"])
        no_year = sum(1 for s in songs if s["year"] is None)
        no_bpm = sum(1 for s in songs if s["bpm"] is None)
        self.scan_status.configure(text=f"{len(songs)} songs, {len(genres)} genres")
        self.genre_list.delete(0, "end")
        for _key, name, count in genres:
            self.genre_list.insert("end", f"{name}  ({count})")
        self.select_top(10)
        self.log(f"Scanned {folder}: {len(songs)} songs, {len(genres)} genres. Missing tags - genre: {no_genre}, "
                 f"year: {no_year}, BPM: {no_bpm}.")

    def select_top(self, n):
        self.genre_list.selection_clear(0, "end")
        if self.genres:
            self.genre_list.selection_set(0, min(n, len(self.genres)) - 1)
        self.update_selection_label()

    def select_all(self):
        self.genre_list.selection_set(0, "end")
        self.update_selection_label()

    def clear_selection(self):
        self.genre_list.selection_clear(0, "end")
        self.update_selection_label()

    def update_selection_label(self):
        if not self.genres:
            self.selection_label.configure(text="Scan a folder to list its genres.")
            return
        picked = set(self.genre_list.curselection())
        other = sum(g[2] for i, g in enumerate(self.genres) if i not in picked)
        where = f"go to \"{OTHER_GENRES}\"" if LIBRARY_INCLUDE["other_genres"] else "are left out"
        self.selection_label.configure(text=f"{len(picked)} main genre(s) ticked - {other} songs in "
                                            f"{len(self.genres) - len(picked)} other genre(s) {where}")
        main_genres = {self.genres[i][0] for i in picked}
        summary = left_out_summary(count_left_out(self.songs, main_genres, LIBRARY_INCLUDE))
        self.left_out_label.configure(text=summary)
        set_visible(self.left_out_label, bool(summary), anchor="w", pady=(4, 0))

    # --- preview / create ---

    def preview_clicked(self):
        if not self.songs:
            messagebox.showinfo("Scan first", "Scan a music folder first.")
            return
        out = self.output_entry.get().strip()
        if not out:
            messagebox.showerror("Missing info", "Choose where to save the playlists / copy the songs.")
            return
        picked = set(self.genre_list.curselection())
        main_genres = {self.genres[i][0] for i in picked}
        genre_names = {key: name for key, name, _count in self.genres}
        grouping = self.grouping_var.get()
        groups, left_out = group_songs(
            self.songs, main_genres, genre_names, grouping, self.year_mode_var.get(), self.from_year(),
            int(self.bpm_step_var.get()),
            self.genre_first_var.get() or grouping not in (GROUP_GENRE_YEAR, GROUP_GENRE_BPM), LIBRARY_INCLUDE)
        if not groups:
            messagebox.showinfo("Nothing to create", "Every song is left out by the Include options "
                                                     "(and your ticked genres).\n\n" + left_out_summary(left_out))
            return
        mode = self.mode_var.get()
        nested = mode == "copy" and self.nested_var.get()
        LibraryPreviewWindow(self, groups, Path(out), mode, nested, left_out)

    def create(self, groups, out_dir, mode, nested):
        """Called by the preview window's Create button."""
        self.busy = True
        self.preview_button.configure(state="disabled")
        total = sum(len(v) for v in groups.values())
        self.progress.configure(maximum=max(total, 1), value=0)
        what = "Writing playlists" if mode == "m3u" else "Copying songs"
        self.log(f"\n{what} into {out_dir}...")

        def progress(done, total_songs):
            try:
                self.after(0, lambda: (self.progress.configure(value=done),
                                       self.progress_label.configure(text=f"{done}/{total_songs}")))
            except (tk.TclError, RuntimeError):
                pass

        def run():
            try:
                with KEEP_AWAKE:
                    summary = write_library_playlists(groups, out_dir, mode, nested, self.log, progress)
                self.after(0, lambda: self.create_finished(summary, out_dir, mode))
            except Exception as e:
                self.log(f"ERROR: {e}")
                SESSION_LOG.exception(log_source_name(self), type(e), e, e.__traceback__)
                self.after(0, lambda err=e: (self.create_finished(None, out_dir, mode),
                                             messagebox.showerror("Error", str(err))))

        threading.Thread(target=run, daemon=True).start()

    def create_finished(self, summary, out_dir, mode):
        self.busy = False
        self.preview_button.configure(state="normal")
        if summary is None:
            return
        if mode == "m3u":
            text = f"Created {summary['playlists']} playlists ({summary['songs']} songs) in:\n{out_dir}"
        else:
            text = (f"{summary['playlists']} folders: {summary['copied']} songs copied, "
                    f"{summary['already']} already there" + (f", {summary['failed']} failed" if summary["failed"]
                                                             else "") + f".\nIn: {out_dir}")
        self.log("Done. " + text.replace("\n", " "))
        messagebox.showinfo("Playlists created", text)


class LibraryPreviewWindow(tk.Toplevel):
    """Lists the playlists about to be created, with song counts, before
    anything is written."""

    def __init__(self, panel, groups, out_dir, mode, nested, left_out=None):
        super().__init__(panel)
        self.title("TrackMatch - Playlist Preview")
        self.geometry("620x560")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)
        self.panel, self.groups, self.out_dir, self.mode, self.nested = panel, groups, out_dir, mode, nested

        songs = sum(len(v) for v in groups.values())
        if mode == "m3u":
            what = f"{len(groups)} playlists (.m3u8) will be saved into:\n{out_dir}"
        else:
            size = sum(s["path"].stat().st_size for v in groups.values() for s in v if s["path"].exists())
            layout = "nested folders" if nested else "one folder per playlist"
            what = (f"{songs} songs ({format_size(size)}) will be copied into {len(groups)} playlists as {layout} "
                    f"in:\n{out_dir}")
        skipped = left_out_summary(left_out or {})
        header = tk.Label(self, text=f"{what}\n" + (f"{skipped} (Include options).\n" if skipped else "")
                                     + "\nNothing is written until you click Create.", justify="left",
                          wraplength=590)
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 6))

        table = tk.Frame(self); style_frame(table)
        table.pack(fill="both", expand=True, padx=10)
        tree = ttk.Treeview(table, columns=("name", "count"), show="headings")
        tree.heading("name", text="Folder" if mode == "copy" else "Playlist")
        tree.heading("count", text="Songs")
        tree.column("name", width=460, anchor="w")
        tree.column("count", width=80, anchor="e")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        for parts, members in sorted(groups.items(), key=lambda kv: [p.lower() for p in kv[0]]):
            label = "/".join(sanitize_playlist_name(p) for p in parts) + "/" if nested \
                else sanitize_playlist_name(playlist_name(parts)) + ("" if mode == "copy" else ".m3u8")
            tree.insert("", "end", values=(label, len(members)))

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        create_btn = ThemedButton(bottom, text="Create", command=self.create, accent=True)
        create_btn.pack(side="right")
        cancel_btn = ThemedButton(bottom, text="Cancel", command=self.destroy, width=10)
        cancel_btn.pack(side="right", padx=(0, 6))

    def create(self):
        self.panel.create(self.groups, self.out_dir, self.mode, self.nested)
        self.destroy()


# ---------- Tab 3: Metadata Editor ----------

class MetadataEditorTab(LogPanelMixin, tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)

        lookup = make_section(self, "Cover art & genre lookup",
                              "Looks up every song in a folder and fills in cover art, genre, label and year "
                              "(plus BPM and key from Beatport). Nothing is written to your files until you approve "
                              "the matches in the review window.", pady=(12, 0))
        self.folder_entry = make_path_row(lookup, "Music folder:",
                                          lambda e: browse_folder_into(e, "Select a folder of music to look up"),
                                          placeholder="The folder of songs to tag")
        source_row = tk.Frame(lookup); style_frame(source_row)
        source_row.pack(fill="x", pady=(4, 0))
        source_label = tk.Label(source_row, text="Look up on:"); style_label(source_label)
        source_label.pack(side="left", padx=(0, 8))
        self.source_labels = {METADATA_SOURCE_LABELS[k]: k for k in [METADATA_AUTO, *METADATA_AUTO_ORDER]}
        self.source_var = tk.StringVar(value=METADATA_SOURCE_LABELS[METADATA_AUTO])
        ThemedOptionMenu(source_row, self.source_var, *self.source_labels).pack(side="left")
        info_button(source_row, "Auto tries Beatport first - electronic music, and the only source with BPM and "
                                "key - then Deezer, then iTunes for everything else, stopping at the first "
                                "confident match.\n\nBeatport also lists a lot of mainstream music with its own "
                                "genre names; for a pop / hip-hop / rock folder, \"Deezer only\" gives more "
                                "natural genres and titles.", "Sources").pack(side="left", padx=(6, 0))

        fold = Fold(lookup)
        self.skip_tagged_var = tk.BooleanVar(value=False)
        ThemedCheckbutton(fold.body, text="Skip songs already tagged by TrackMatch",
                          variable=self.skip_tagged_var).pack(anchor="w", pady=(2, 0))
        self.only_missing_var = tk.BooleanVar(value=False)
        ThemedCheckbutton(fold.body, text="Only songs missing genre or cover art",
                          variable=self.only_missing_var).pack(anchor="w", pady=(2, 0))

        self.run_button, (self.stop_button,), self.progress, self.status_line = make_action_row(
            self, "Scan & Look Up", self.run_clicked, [("Stop", self.stop_clicked, "disabled")])
        self.stop_event = threading.Event()
        self.scan_status = tk.Label(self.progress.master, text="", width=22, anchor="e")
        style_label(self.scan_status, subtle=True)
        self.scan_status.pack(side="left", padx=(6, 0))
        self.mode_label = tk.Label(self, text="", justify="left", anchor="w")
        style_label(self.mode_label, subtle=True)
        self.mode_label.pack(anchor="w", padx=14, pady=(2, 0))
        SETTING_LISTENERS.append(self.update_mode_label)
        self.update_mode_label()

        names = make_section(self, "Fix file names",
                             "Renames songs to \"Artist - Title\" from their own tags - several artists are joined "
                             "with \", \", the same way Grab names downloads. You see every change first, and an "
                             "existing file is never overwritten.\n\n.m3u / .m3u8 playlists in the folder (and its "
                             "subfolders) are updated to the new names; playlists saved anywhere else keep the old "
                             "ones. Rekordbox, Serato and Traktor show renamed songs as missing until you relocate "
                             "them.\n\nThe new name comes from the tags, so tag any wrongly tagged songs first.")
        names_text = tk.Label(names, text="Gives every song in the music folder above an \"Artist - Title\" file "
                                          "name from its tags.", justify="left", anchor="w", wraplength=680)
        style_label(names_text, subtle=True)
        names_text.pack(fill="x")
        names_row = tk.Frame(names); style_frame(names_row)
        names_row.pack(fill="x", pady=(6, 0))
        self.names_button = ThemedButton(names_row, text="Check File Names", command=self.check_names_clicked)
        self.names_button.pack(side="left")
        self.names_status = tk.Label(names_row, text="", anchor="w")
        style_label(self.names_status, subtle=True)
        self.names_status.pack(side="left", padx=(8, 0))

        log_label = tk.Label(self, text="Log:"); style_label(log_label)
        log_label.pack(anchor="w", padx=12, pady=(8, 0))
        self.log_box = ThemedScrolledText(self, height=16, state="disabled", wrap="word")
        style_scrolledtext(self.log_box)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        register_log_panel(log_label, self.log_box)

        if not MutagenFile:
            self.log("WARNING: 'mutagen' is not installed. Run: pip install mutagen")
        if requests is None or BeautifulSoup is None:
            self.log("NOTE: 'requests' and/or 'beautifulsoup4' are not installed - metadata lookup "
                      "won't work until you run: pip install requests beautifulsoup4")

    def update_mode_label(self):
        mode = "on" if LARGE_LIBRARY_MODE else "off"
        self.mode_label.configure(text=f"Large library mode {mode}  \u00b7  Minimum match {METADATA_MIN_SCORE:.0%}"
                                       f"  \u00b7  change these in Settings")

    def check_names_clicked(self):
        folder = self.folder_entry.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("Missing info", "Choose a valid music folder.")
            return
        if not MutagenFile:
            messagebox.showerror("Missing dependency", "Install requirements first:\npip install mutagen")
            return
        self.names_button.configure(state="disabled")
        self.names_status.configure(text="Reading tags...")

        def progress(done, total):
            try:
                self.after(0, lambda: self.names_status.configure(text=f"Reading tags... {done}/{total}"))
            except (tk.TclError, RuntimeError):
                pass

        def run():
            try:
                items = scan_file_names(Path(folder), progress)
                self.after(0, lambda: self.names_checked(Path(folder), items))
            except Exception as e:
                self.after(0, lambda err=e: (self.names_button.configure(state="normal"),
                                             self.names_status.configure(text=""),
                                             messagebox.showerror("Error", str(err))))

        threading.Thread(target=run, daemon=True).start()

    def names_checked(self, folder, items):
        self.names_button.configure(state="normal")
        counts = {}
        for it in items:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        problems = len(items) - counts.get("rename", 0) - counts.get("ok", 0)
        summary = (f"{counts.get('rename', 0)} to rename, {counts.get('ok', 0)} already right"
                   + (f", {problems} can't be fixed" if problems else ""))
        self.names_status.configure(text=summary)
        self.log(f"\nChecked file names in {folder}: {summary}.")
        if not items:
            messagebox.showinfo("No songs", f"No audio files found in:\n{folder}")
        elif not counts.get("rename") and not problems:
            messagebox.showinfo("File names", f"All {len(items)} songs already have \"Artist - Title\" file names.")
        else:
            FileNameReviewWindow(self, folder, items, self.log)

    def run_clicked(self):
        folder = self.folder_entry.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("Missing info", "Choose a valid music folder.")
            return
        if not MutagenFile:
            messagebox.showerror("Missing dependency", "Install requirements first:\npip install mutagen")
            return
        if requests is None or BeautifulSoup is None:
            messagebox.showerror("Missing dependency",
                                  "Install requirements first:\npip install requests beautifulsoup4")
            return

        self.run_button.configure(state="disabled")
        self.scan_status.configure(text="Counting songs...")
        source = self.source_labels[self.source_var.get()]
        options = (source, self.skip_tagged_var.get(), self.only_missing_var.get())

        def count():
            files = sorted(p for p in Path(folder).rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)
            self.after(0, lambda: self.start_scan(Path(folder), files, *options))

        threading.Thread(target=count, daemon=True).start()

    def start_scan(self, folder, files, source, skip_tagged, only_missing):
        large = LARGE_LIBRARY_MODE
        if not files:
            self.scan_finished()
            messagebox.showinfo("No songs", f"No audio files found in:\n{folder}")
            return
        if not large and len(files) >= LARGE_FOLDER_WARNING:
            per_song = 1.5 if source == "Beatport" else 2.5
            hours = len(files) * per_song / 3600
            estimate = f"about {hours:.1f} hours" if hours >= 1 else f"about {len(files) * per_song / 60:.0f} minutes"
            choice = ask_choice(
                self, "Large folder",
                f"This folder has {len(files)} songs. Without Large Library Mode:\n\n"
                f"  • the scan runs one song at a time ({estimate} here)\n"
                f"  • nothing is saved until it finishes - closing the app or a crash loses all progress\n"
                f"  • the review window will be very slow with this many songs\n\n"
                f"Large Library Mode looks up 3 songs at a time, saves progress as it goes (so you can stop "
                f"and resume), and reviews everything in a compact table. You can change it any time in "
                f"Settings.",
                ["Turn on Large Library Mode", "Scan anyway", "Cancel"],
            )
            if choice == "Turn on Large Library Mode":
                set_large_library_mode(True)
                large = True
            elif choice != "Scan anyway":
                self.scan_finished()
                return

        self.stop_event = threading.Event()
        self.stop_button.configure(state="normal")
        self.progress.configure(mode="determinate", maximum=len(files), value=0)
        threading.Thread(target=self.worker, args=(folder, files, source, large, skip_tagged, only_missing),
                         daemon=True).start()

    def stop_clicked(self):
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.log("Stopping - songs already being looked up will finish, then the review opens with what's done."
                 + (" Progress is saved, so scanning this folder again resumes from here." if LARGE_LIBRARY_MODE
                    else ""))

    def scan_finished(self):
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.scan_status.configure(text="")

    def worker(self, folder, files, source, large, skip_tagged, only_missing):
        total = len(files)
        cache = LOOKUP_CACHE if large else None
        lock = threading.Lock()
        counts = {"done": 0, "skipped": 0, "saved": 0}
        started = time.monotonic()
        mode = "Large Library Mode - 3 at a time, progress saved" if large else "one at a time"
        self.log(f"\nLooking up {total} songs in {folder} on {METADATA_SOURCE_LABELS[source]} ({mode})...")

        def progress_tick():
            with lock:
                counts["done"] += 1
                done = counts["done"]
            elapsed = time.monotonic() - started
            remaining = (elapsed / done) * (total - done) if done else 0
            left = f"~{remaining / 60:.0f} min left" if remaining >= 60 else f"~{remaining:.0f}s left"
            text = f"{done}/{total}" + (f"  ·  {left}" if done < total else "")
            try:
                self.after(0, lambda: (self.progress.configure(value=done), self.scan_status.configure(text=text)))
            except (tk.TclError, RuntimeError):
                pass

        def process(numbered):
            i, path = numbered
            if self.stop_event.is_set():
                return None
            try:
                song = read_song_for_lookup(path)  # the only time the file is read
                if (skip_tagged or only_missing) and lookup_skip_reason(song, skip_tagged, only_missing):
                    with lock:
                        counts["skipped"] += 1
                    return None
                existing = song["existing"]
                title, artist = existing["title"], existing["artist"]
                cached = None
                if cache is not None:
                    key = cache.key(path, source)
                    cached = cache.get(key)
                if cached is not None:
                    results, tried = cached
                    with lock:
                        counts["saved"] += 1
                else:
                    errors = []
                    results, tried = lookup_metadata(source, artist, title, song["length"], self.log,
                                                     errors=errors)
                    # Only save complete answers - if a source failed (network drop, PC slept...),
                    # this song is looked up again next scan instead of being remembered as "not found".
                    if cache is not None and not errors:
                        cache.put(key, results, tried)
                if results and results[0]["score"] >= METADATA_MIN_SCORE:
                    outcome = f"{results[0]['source']} ({results[0]['score']:.0%})"
                elif results:
                    outcome = f"weak match only ({results[0]['score']:.0%}) - left for you to check"
                else:
                    outcome = f"not found on {', '.join(tried)}"
                self.log(f"[{i}/{total}] {artist} - {title}: {outcome}" + ("  (saved)" if cached else ""))
                return {
                    "path": path, "title": title, "artist": artist,
                    "existing_album": existing["album"], "existing_genre": existing["genre"],
                    "results": results, "tried": tried,
                }
            except Exception as e:
                self.log(f"[{i}/{total}] {path.name}: lookup failed - {e}")
                return None
            finally:
                progress_tick()

        try:
            with KEEP_AWAKE, ThreadPoolExecutor(max_workers=LOOKUP_WORKERS if large else 1) as pool:
                items = [it for it in pool.map(process, enumerate(files, 1)) if it]

            stopped = self.stop_event.is_set()
            confident = sum(1 for it in items if it["results"] and it["results"][0]["score"] >= METADATA_MIN_SCORE)
            found = sum(1 for it in items if it["results"])
            by_source = {}
            for it in items:
                if it["results"] and it["results"][0]["score"] >= METADATA_MIN_SCORE:
                    by_source[it["results"][0]["source"]] = by_source.get(it["results"][0]["source"], 0) + 1
            breakdown = ", ".join(f"{n} from {src}" for src, n in by_source.items())
            self.log(f"\n{'Stopped' if stopped else 'Done searching'}. {confident}/{len(items)} confident matches"
                     + (f" ({breakdown})" if breakdown else "") + f", {found - confident} weak only"
                     + (f", {counts['skipped']} skipped by your filters" if counts["skipped"] else "")
                     + (f", {counts['saved']} from saved lookups" if counts["saved"] else "") + ".")
            if items:
                # The per-row review gets slow past a couple of hundred songs - use the table then.
                review = LargeReviewWindow if large or len(items) > SMALL_REVIEW_LIMIT else MetadataReviewWindow
                self.after(0, lambda: review(self, items, self.log))
            else:
                self.after(0, lambda: messagebox.showinfo(
                    "Nothing to review", "No songs were looked up" + (" - all were skipped by your filters."
                                                                      if counts["skipped"] else ".")))
        except Exception as e:
            self.log(f"ERROR: {e}")
            SESSION_LOG.exception(log_source_name(self), type(e), e, e.__traceback__)
            self.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.after(0, self.scan_finished)
            self.after(0, lambda: self.progress.configure(value=0))


def read_name_tags(path: Path):
    """(artists, title) straight from the tags, every artist value kept. No
    file name fallback - the file name is what's being fixed."""
    artists, title = [], ""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        audio = None
    tags = getattr(audio, "tags", None) if audio is not None else None
    if tags is not None:
        if isinstance(tags, ID3):  # wav/aiff have no "easy" mode - read the frames directly
            artist_frame, title_frame = tags.get("TPE1"), tags.get("TIT2")
            artists = list(artist_frame.text) if artist_frame is not None else []
            title = title_frame.text[0] if title_frame is not None and title_frame.text else ""
        else:
            try:
                artists = list(tags.get("artist") or [])
                title = _first_value(tags.get("title"))
            except (KeyError, ValueError):
                pass
    return [str(a).strip() for a in artists if str(a).strip()], str(title).strip()


def tag_file_name(artists, title, extension):
    """The "Artist - Title" name a song's tags give it, named like Grab names
    downloads: several artists joined with ", ", then sanitize_filename. None
    when the tags have no artist or no title."""
    artists = [a.strip() for a in artists if a.strip()]
    title = title.strip()
    if not artists or not title:
        return None
    return sanitize_filename(f"{', '.join(artists)} - {title}") + extension


RENAME_PROBLEMS = {
    "no_tags": "no artist or title tag",
    "taken": "another file already has that name",
    "duplicate": "another song in this folder would get the same name",
}


def plan_renames(songs, taken):
    """songs: [(folder, file name, artists, title)]. taken: {folder: lowercased
    names of every file in it}. Returns (status, new name) per song - status
    "rename", "ok" (already right) or a RENAME_PROBLEMS key. Only the first of
    several songs wanting the same name gets it; a case-only change is fine."""
    plan, claimed = [], set()
    for folder, name, artists, title in songs:
        new = tag_file_name(artists, title, os.path.splitext(name)[1])
        if new is None:
            plan.append(("no_tags", None))
        elif new == name:
            plan.append(("ok", new))
        elif (folder, new.lower()) in claimed:
            plan.append(("duplicate", new))
        elif new.lower() != name.lower() and new.lower() in taken.get(folder, set()):
            plan.append(("taken", new))
        else:
            claimed.add((folder, new.lower()))
            plan.append(("rename", new))
    return plan


def scan_file_names(folder: Path, progress=None):
    """Reads the artist and title of every song in folder (and subfolders) and
    plans its new name. Returns [{"path", "status", "new"}] in path order."""
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS)
    taken = {d: {c.name.lower() for c in d.iterdir()} for d in {p.parent for p in files}}
    done = [0]
    lock = threading.Lock()

    def read(path):
        tags = read_name_tags(path)
        with lock:
            done[0] += 1
            if progress and done[0] % 50 == 0:
                progress(done[0], len(files))
        return tags

    with ThreadPoolExecutor(max_workers=8) as pool:
        tags = list(pool.map(read, files))
    plan = plan_renames([(p.parent, p.name, a, t) for p, (a, t) in zip(files, tags)], taken)
    return [{"path": p, "status": status, "new": new} for p, (status, new) in zip(files, plan)]


def update_playlist_entries(text, resolve, renamed):
    """Points a playlist's entries at renamed songs. resolve(entry) gives the
    key of the file an entry points at and renamed maps those keys to new
    names. Only the last part of a changed entry is swapped, so the rest of the
    line (relative or full path, slashes, line ending) stays as it was.
    Returns (new text, entries changed)."""
    lines, changed = [], 0
    for line in re.split(r"(?<=\n)", text):
        body = line.rstrip("\r\n")
        entry = body.strip()
        if entry and not entry.startswith("#"):
            new = renamed.get(resolve(entry))
            if new:
                cut = max(body.rfind("/"), body.rfind("\\")) + 1
                line = body[:cut] + new + line[len(body):]
                changed += 1
        lines.append(line)
    return "".join(lines), changed


def path_key(path):
    return os.path.normcase(os.path.normpath(path))


def update_playlists_for_renames(folder: Path, renamed, log):
    """Rewrites the .m3u / .m3u8 playlists in folder (and subfolders) that
    list a renamed song. renamed maps path_key(old path) to the new name.
    Keeps each playlist's encoding. Returns how many were updated."""
    updated = 0
    for playlist in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".m3u", ".m3u8")):
        try:
            raw = playlist.read_bytes()
            encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                encoding = "cp1252"  # an older .m3u saved in the Windows encoding
                text = raw.decode(encoding)

            def resolve(entry):
                path = Path(entry.replace("\\", "/"))
                return path_key(path if path.is_absolute() else playlist.parent / path)

            new_text, changed = update_playlist_entries(text, resolve, renamed)
            if changed:
                tmp = playlist.with_name(playlist.name + ".updating")
                tmp.write_bytes(new_text.encode(encoding))
                os.replace(tmp, playlist)
                updated += 1
                log(f"  Updated playlist {playlist.name}: {changed} song{plural(changed)}")
        except (OSError, UnicodeError) as e:
            log(f"  Couldn't update playlist {playlist.name}: {e}")
    return updated


def plural(n):
    return "" if n == 1 else "s"


class FileNameReviewWindow(tk.Toplevel):
    """Lists the songs whose file names would change (now -> new, each with
    a tick) and, under them, the ones that can't be fixed and why. Nothing
    is renamed until Rename is clicked."""

    TICK, UNTICK = "☑", "☐"

    def __init__(self, parent, folder, items, log):
        super().__init__(parent)
        self.title("TrackMatch - Fix File Names")
        self.geometry("900x620")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)
        self.folder, self.log = folder, log
        self.renames = [it for it in items if it["status"] == "rename"]
        self.problems = [it for it in items if it["status"] in RENAME_PROBLEMS]
        self.ticked = set(range(len(self.renames)))
        self.renaming = False

        ok = sum(1 for it in items if it["status"] == "ok")
        header = tk.Label(
            self,
            text=f"{len(self.renames)} song{plural(len(self.renames))} will get \"Artist - Title\" names from their tags"
                 + (f", {ok} already right" if ok else "")
                 + (f", {len(self.problems)} can't be fixed (listed at the bottom)" if self.problems else "")
                 + ". Click a row to untick it. Playlists in this folder are updated to the new names. Nothing "
                   "is renamed until you click Rename.",
            justify="left", wraplength=860)
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 6))

        table = tk.Frame(self); style_frame(table)
        table.pack(fill="both", expand=True, padx=10)
        self.tree = ttk.Treeview(table, columns=("tick", "file", "new"), show="headings", selectmode="extended")
        for key, title, width in (("tick", "", 30), ("file", "Now", 420), ("new", "New name", 420)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=key != "tick", anchor="center" if key == "tick" else "w")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        register(lambda: (self.tree.tag_configure("check", foreground=THEME["MISSING_FG"]),
                          self.tree.tag_configure("done", foreground=THEME["FOUND_FG"]),
                          self.tree.tag_configure("failed", foreground=THEME["MISSING_FG"])))
        for i, it in enumerate(self.renames):
            self.tree.insert("", "end", iid=str(i), values=(self.TICK, self.shown(it["path"]), it["new"]))
        for it in self.problems:
            new = f"{it['new']}  " if it["new"] else ""
            self.tree.insert("", "end", values=("", self.shown(it["path"]),
                                                f"{new}(left as is - {RENAME_PROBLEMS[it['status']]})"),
                             tags=("check",))
        self.tree.bind("<ButtonRelease-1>", self.clicked)
        self.tree.bind("<space>", lambda e: self.toggle(self.tree.selection()))

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        self.rename_btn = ThemedButton(bottom, text="", command=self.rename, accent=True)
        self.rename_btn.pack(side="left")
        self.all_btn = ThemedButton(bottom, text="Tick All", command=lambda: self.set_all(True))
        self.all_btn.pack(side="left", padx=(6, 0))
        self.none_btn = ThemedButton(bottom, text="Untick All", command=lambda: self.set_all(False))
        self.none_btn.pack(side="left", padx=(6, 0))
        self.status = tk.Label(bottom, text="", anchor="w")
        style_label(self.status, subtle=True)
        self.status.pack(side="left", fill="x", expand=True, padx=10)
        ThemedButton(bottom, text="Close", command=self.close, width=10).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.update_button()

    def shown(self, path):
        try:
            return path.relative_to(self.folder).as_posix()
        except ValueError:
            return path.name

    def update_button(self):
        self.rename_btn.configure(text=f"Rename {len(self.ticked)} Song{plural(len(self.ticked))}",
                                  state="normal" if self.ticked and not self.renaming else "disabled")

    def clicked(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell":
            row = self.tree.identify_row(event.y)
            if row:
                self.toggle([row])

    def toggle(self, rows):
        if self.renaming:
            return
        for row in rows:
            if row.isdigit() and "done" not in self.tree.item(row, "tags"):
                i = int(row)
                self.ticked.symmetric_difference_update({i})
                self.tree.set(row, "tick", self.TICK if i in self.ticked else self.UNTICK)
        self.update_button()

    def set_all(self, ticked):
        if self.renaming:
            return
        for i in range(len(self.renames)):
            if "done" in self.tree.item(str(i), "tags"):
                continue
            self.ticked.discard(i)
            if ticked:
                self.ticked.add(i)
            self.tree.set(str(i), "tick", self.TICK if ticked else self.UNTICK)
        self.update_button()

    def rename(self):
        jobs = sorted(self.ticked)
        if not jobs:
            return
        self.renaming = True
        self.update_button()
        self.status.configure(text="Renaming...")
        self.log(f"Renaming {len(jobs)} songs...")

        def run():
            renamed, failed = {}, 0
            for n, i in enumerate(jobs, 1):
                src = self.renames[i]["path"]
                dest = src.with_name(self.renames[i]["new"])
                try:
                    # samefile: a case-only change ("fisher - x" -> "FISHER - X") is the same file
                    if dest.exists() and not dest.samefile(src):
                        raise FileExistsError("a file with that name is already there")
                    src.rename(dest)
                    renamed[path_key(src)] = dest.name
                    self.renames[i]["path"] = dest
                    result = ("done", "✓", None)
                    self.log(f"  {src.name} -> {dest.name}")
                except OSError as e:
                    failed += 1
                    result = ("failed", "!", f"{dest.name}  (couldn't rename: {e})")
                    self.log(f"  Couldn't rename {src.name}: {e}")
                self.after(0, lambda i=i, r=result, n=n: self.row_done(i, r, n, len(jobs)))
            playlists = update_playlists_for_renames(self.folder, renamed, self.log) if renamed else 0
            text = (f"Renamed {len(renamed)} song{plural(len(renamed))}" + (f", {failed} failed" if failed else "")
                    + f", updated {playlists} playlist{plural(playlists)}.")
            self.log(text)
            self.after(0, lambda: self.finished(text))

        threading.Thread(target=run, daemon=True).start()

    def row_done(self, i, result, n, total):
        tag, tick, new = result
        self.ticked.discard(i)
        self.tree.item(str(i), tags=(tag,))
        self.tree.set(str(i), "tick", tick)
        if new:
            self.tree.set(str(i), "new", new)
        self.status.configure(text=f"Renaming... {n}/{total}")

    def finished(self, text):
        self.renaming = False
        self.status.configure(text=text)
        self.update_button()

    def close(self):
        if self.renaming:
            messagebox.showinfo("Still renaming", "Songs are still being renamed - this only takes a moment.",
                                parent=self)
            return
        self.destroy()


# ---------- Convert to m4a, downloading lossy songs again (Convert tab) ----------
# Songs that aren't m4a become m4a: converted with ffmpeg, or (lossy ones)
# downloaded again from YouTube, whose m4a is its own AAC stream.

UNIFY_TARGET = "m4a"
UNIFY_KEEP_KBPS = 256      # lossy songs at or above this start skipped - better than YouTube's audio
YT_AUDIO_KBPS = 128         # YouTube's m4a stream
UNIFY_LENGTH_SLACK = 5      # seconds a YouTube upload's length may differ by and still be the same recording
UNIFY_DOWNLOAD, UNIFY_CONVERT, UNIFY_SKIP = "Download", "Convert", "Skip"
UNIFY_PROBLEMS = {
    "taken": "an m4a with this name is already there",
    "duplicate": "another song here would become the same m4a",
}


def pick_youtube_match(artist, title, length, results):
    """(result, confident) for a song from YouTube search results: the best
    ranked result that is the same song (is_same_song) and recording (length
    within UNIFY_LENGTH_SLACK) and scores YT_AUTO_PICK_MIN, or else the best
    ranked result as a weak guess. (None, False) when there are no results."""
    track = {"title": title, "artist": artist, "duration_ms": length * 1000 if length else None}
    ranked = rank_youtube_results(track, results)
    for score, r in ranked:
        yt_artist, yt_title = split_video_title(r["title"], r.get("uploader"))
        channel = re.sub(r"\s*- Topic$", "", r.get("uploader") or "")
        secs = r.get("duration_seconds")
        if (score >= YT_AUTO_PICK_MIN
                and is_same_song(normalize(title), artist, normalize(yt_title),
                                 split_artists(yt_artist) | split_artists(channel))
                and (not length or (secs and abs(secs - length) <= UNIFY_LENGTH_SLACK))):
            return r, True
    return (ranked[0][1], False) if ranked else (None, False)


def unify_actions(item):
    """What a song can be turned into m4a by: lossless and .aac songs only
    convert (YouTube's audio is worse); lossy ones can also download."""
    if item["kind"] in LOSSLESS_FORMATS or item["kind"] == "aac" or not item.get("match"):
        return [UNIFY_CONVERT, UNIFY_SKIP]
    return [UNIFY_DOWNLOAD, UNIFY_CONVERT, UNIFY_SKIP]


def unify_default_action(item):
    if item["kind"] in LOSSLESS_FORMATS or item["kind"] == "aac":
        return UNIFY_CONVERT
    if item["kbps"] and item["kbps"] >= UNIFY_KEEP_KBPS:
        return UNIFY_SKIP
    return UNIFY_DOWNLOAD if item.get("match") and item.get("confident") else UNIFY_SKIP


def unify_note(item, action, bitrate):
    """(note, warn) shown next to a song for an action (converting at
    bitrate kbps) - warn when it costs quality or the match needs checking."""
    kind, kbps = item["kind"], item["kbps"]
    source = f"{kbps}kbps {kind}" if kbps and kind not in LOSSLESS_FORMATS else kind
    if action == UNIFY_DOWNLOAD:
        if not item.get("confident"):
            return "weak match - check it's the same version before replacing", True
        return f"YouTube m4a (~{YT_AUDIO_KBPS}kbps) replaces the {source}", False
    if action == UNIFY_CONVERT:
        if kind in LOSSLESS_FORMATS:
            return f"lossless - converts cleanly to {bitrate}kbps m4a", False
        if kind == "aac":
            return "already AAC - moved into m4a with no quality change", False
        return f"lossy to lossy - {source} re-encoded to {bitrate}kbps loses a little detail", True
    if kbps and kbps >= UNIFY_KEEP_KBPS and kind not in LOSSLESS_FORMATS:
        return f"left as is - {kbps}kbps is already better than YouTube's ~{YT_AUDIO_KBPS}kbps", False
    return f"left as is - {item.get('search_note') or 'not changed'}", False


def scan_formats(folder: Path, source="Any format", recursive=True, into_subfolder=False, progress=None,
                 cancel_event=None):
    """Every song in folder that isn't m4a (only source-format songs unless
    source is "Any format"), with its tags, bitrate and - for lossy songs -
    its YouTube match. dest is next to the original, or under a "Converted
    (m4a)" subfolder with into_subfolder. Keeps the computer awake. Returns
    item dicts in path order: path, dest, kind, kbps, length, artist, title,
    match, confident, search_note, status ("ok" or a UNIFY_PROBLEMS key) and
    the action it starts with."""
    with KEEP_AWAKE:
        return _scan_formats(folder, source, recursive, into_subfolder, progress, cancel_event)


def _scan_formats(folder, source, recursive, into_subfolder, progress, cancel_event):
    files = find_convertible_files(folder, source, UNIFY_TARGET, recursive)
    with ThreadPoolExecutor(max_workers=8) as pool:
        songs = list(pool.map(read_song_for_lookup, files))

    items, claimed = [], set()
    for path, song in zip(files, songs):
        dest = converted_path(path, folder, UNIFY_TARGET, into_subfolder)
        if dest.exists():
            status = "taken"
        elif path_key(dest) in claimed:
            status = "duplicate"
        else:
            status = "ok"
            claimed.add(path_key(dest))
        items.append({"path": path, "dest": dest, "kind": audio_kind(path),
                      "kbps": round(song["bitrate"] / 1000) if song.get("bitrate") else None,
                      "length": song["length"], "artist": song["existing"]["artist"],
                      "title": song["existing"]["title"], "match": None, "confident": False,
                      "search_note": "", "status": status})

    to_search = [it for it in items if it["status"] == "ok" and it["kind"] not in LOSSLESS_FORMATS
                 and it["kind"] != "aac"]
    lock, done = threading.Lock(), [0]

    def search(item):
        if yt_dlp is None:
            item["search_note"] = "yt-dlp isn't installed"
        elif cancel_event is not None and cancel_event.is_set():
            item["search_note"] = "not searched - the check was stopped"
        else:
            try:
                query = f"{item['artist']} {item['title']}".strip()
                results = search_youtube(query, max_results=YT_AUTO_SEARCH_RESULTS)
                item["match"], item["confident"] = pick_youtube_match(item["artist"], item["title"],
                                                                      item["length"], results)
                if not item["match"]:
                    item["search_note"] = "no YouTube results"
                elif not item["confident"]:
                    item["search_note"] = "no confident YouTube match"
            except Exception as e:
                item["search_note"] = f"YouTube search failed ({e})"
        with lock:
            done[0] += 1
            if progress:
                progress(f"Searching YouTube... {done[0]}/{len(to_search)}")

    if progress and to_search:
        progress(f"Searching YouTube... 0/{len(to_search)}")
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        list(pool.map(search, to_search))
    for it in items:
        it["action"] = unify_default_action(it) if it["status"] == "ok" else UNIFY_SKIP
    return items


def replace_with_m4a(item, bitrate, recycle, running_procs, cancel_event, log, progress=None):
    """Makes one song's m4a at item["dest"] by its action - converting at
    bitrate kbps, or downloading, which keeps the original's tags and cover
    (the YouTube thumbnail when it has none) - checks the length, then with
    recycle sends the original to the Recycle Bin. Returns a short status
    text."""
    src, dest = item["path"], item["dest"]
    if dest.exists():
        raise FileExistsError("an m4a with this name is already there")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if item["action"] == UNIFY_DOWNLOAD:
        temp_name = f"{src.stem}.downloading"
        leftover = src.parent / f"{sanitize_filename(temp_name)}.{UNIFY_TARGET}"
        if leftover.exists():
            leftover.unlink()  # from a run that stopped before tagging it
        tmp, _ = download_youtube_audio(item["match"]["url"], src.parent, UNIFY_TARGET, None, temp_name, log,
                                        progress, cancel_event)
        try:
            if progress:
                progress("Tagging...")
            fields, image, mime = read_tags_and_cover(src)
            if not image:
                image, mime = youtube_cover_art(item["match"].get("thumbnail", ""))
            embed_full_metadata(tmp, fields, image, mime)
            new_length = _audio_info(tmp)[0]
            if item["length"] and (not new_length or abs(new_length - item["length"]) > UNIFY_LENGTH_SLACK):
                raise RuntimeError(f"the download is {format_duration(new_length)} long but the song is "
                                   f"{format_duration(item['length'])} - not keeping it")
            tmp.replace(dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        done = "Downloaded"
    else:
        if progress:
            progress("Converting...")
        convert_audio_file(src, dest, UNIFY_TARGET, bitrate, running_procs, cancel_event,
                           copy_audio=item["kind"] == "aac")
        done = "Converted"
    if not recycle:
        return done
    try:
        move_to_recycle_bin(src)
        return f"{done} - original in the {TRASH_NAME}"
    except OSError as e:
        log(f"  Kept original {src.name} - {e}")
        return f"{done} - original kept ({e})"


class FormatReviewWindow(tk.Toplevel):
    """Lists the songs that aren't m4a with what happens to each (Download /
    Convert / Skip - click a row to change it) and why, and the ones that
    can't be made m4a at the bottom. Uses the Convert tab's bitrate, output
    place and Recycle Bin choice. Nothing changes until the main button is
    clicked."""

    def __init__(self, parent, folder, items, log, bitrate, into_subfolder, recycle):
        super().__init__(parent)
        self.title("TrackMatch - Convert to m4a")
        self.geometry("1150x640")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)
        self.folder, self.log = folder, log
        self.bitrate, self.into_subfolder, self.recycle = bitrate, into_subfolder, recycle
        self.items = [it for it in items if it["status"] == "ok"]
        self.problems = [it for it in items if it["status"] in UNIFY_PROBLEMS]
        self.running = False
        self.cancel_event = threading.Event()
        self.running_procs = set()

        where = (f"in a \"{CONVERTED_FOLDER_PREFIX}{UNIFY_TARGET})\" subfolder" if into_subfolder
                 else "next to the original")
        original = f"the original goes to the {TRASH_NAME}" if recycle else "the original is kept"
        playlists = "" if into_subfolder else " and playlists in this folder are updated"
        header = tk.Label(
            self,
            text=f"{len(self.items)} song{plural(len(self.items))} can become m4a"
                 + (f", {len(self.problems)} can't (listed at the bottom)" if self.problems else "")
                 + ". Click a row to switch between Download, Convert and Skip. Orange rows lose a little "
                   f"quality or need checking. Each m4a keeps the song's tags, cover art and file name and is "
                   f"saved {where}; {original}{playlists}. Nothing changes until you click "
                   f"{'Replace' if recycle else 'Make m4a'}.",
            justify="left", wraplength=1110)
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 6))

        table = tk.Frame(self); style_frame(table)
        table.pack(fill="both", expand=True, padx=10)
        columns = (("action", "Action", 100), ("file", "Song", 330), ("format", "Now", 90),
                   ("match", "YouTube match", 290), ("note", "", 320))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in columns], show="headings", selectmode="extended")
        for key, title, width in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=key in ("file", "match", "note"))
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        register(lambda: (self.tree.tag_configure("warn", foreground=THEME["MISSING_FG"]),
                          self.tree.tag_configure("skip", foreground=THEME["SUBTLE_FG"]),
                          self.tree.tag_configure("done", foreground=THEME["FOUND_FG"]),
                          self.tree.tag_configure("failed", foreground=THEME["MISSING_FG"])))
        for i, it in enumerate(self.items):
            self.tree.insert("", "end", iid=str(i), values=("", self.shown(it["path"]), self.format_text(it),
                                                            self.match_text(it), ""))
            self.show_action(i)
        for it in self.problems:
            self.tree.insert("", "end", values=("", self.shown(it["path"]), self.format_text(it), "",
                                                f"left as is - {UNIFY_PROBLEMS[it['status']]}"), tags=("warn",))
        self.tree.bind("<ButtonRelease-1>", self.clicked)
        self.tree.bind("<space>", lambda e: self.cycle(self.tree.selection()))

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        self.replace_btn = ThemedButton(bottom, text="", command=self.replace, accent=True)
        self.replace_btn.pack(side="left")
        self.defaults_btn = ThemedButton(bottom, text="Suggested", command=self.set_defaults)
        self.defaults_btn.pack(side="left", padx=(6, 0))
        self.convert_all_btn = ThemedButton(bottom, text="Convert All", command=self.convert_all)
        self.convert_all_btn.pack(side="left", padx=(6, 0))
        self.download_all_btn = ThemedButton(bottom, text="Download All Found", command=self.download_all_found)
        self.download_all_btn.pack(side="left", padx=(6, 0))
        self.skip_btn = ThemedButton(bottom, text="Skip All", command=self.skip_all)
        self.skip_btn.pack(side="left", padx=(6, 0))
        self.stop_btn = ThemedButton(bottom, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.status = tk.Label(bottom, text="", anchor="w")
        style_label(self.status, subtle=True)
        self.status.pack(side="left", fill="x", expand=True, padx=10)
        ThemedButton(bottom, text="Close", command=self.close, width=10).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.update_button()

    def shown(self, path):
        try:
            return path.relative_to(self.folder).as_posix()
        except ValueError:
            return path.name

    @staticmethod
    def format_text(it):
        return f"{it['kind']} {it['kbps']}k" if it["kbps"] and it["kind"] not in LOSSLESS_FORMATS else it["kind"]

    @staticmethod
    def match_text(it):
        m = it.get("match")
        if not m:
            return ""
        diff = ""
        if it["length"] and m.get("duration_seconds"):
            diff = f", {m['duration_seconds'] - it['length']:+.0f}s"
        return f"{m['title']} ({m.get('uploader') or '?'}, {m['duration']}{diff})"

    def pending(self):
        return [i for i, it in enumerate(self.items) if it["action"] != UNIFY_SKIP and not it.get("done")]

    def update_button(self):
        jobs = self.pending()
        verb = "Replace" if self.recycle else "Make m4a of"
        self.replace_btn.configure(text=f"{verb} {len(jobs)} Song{plural(len(jobs))}",
                                   state="normal" if jobs and not self.running else "disabled")
        for btn in (self.defaults_btn, self.convert_all_btn, self.download_all_btn, self.skip_btn):
            btn.configure(state="disabled" if self.running else "normal")
        self.stop_btn.configure(state="normal" if self.running else "disabled")

    def show_action(self, i):
        it = self.items[i]
        note, warn = unify_note(it, it["action"], self.bitrate)
        self.tree.set(str(i), "action", it["action"])
        self.tree.set(str(i), "note", note)
        self.tree.item(str(i), tags=("warn",) if warn else ("skip",) if it["action"] == UNIFY_SKIP else ())

    def clicked(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell":
            row = self.tree.identify_row(event.y)
            if row:
                self.cycle([row])

    def cycle(self, rows):
        if self.running:
            return
        for row in rows:
            if row.isdigit() and not self.items[int(row)].get("done"):
                it = self.items[int(row)]
                actions = unify_actions(it)
                it["action"] = actions[(actions.index(it["action"]) + 1) % len(actions)]
                self.show_action(int(row))
        self.update_button()

    def set_all(self, pick):
        if self.running:
            return
        for i, it in enumerate(self.items):
            if not it.get("done"):
                it["action"] = pick(it)
                self.show_action(i)
        self.update_button()

    def set_defaults(self):
        self.set_all(unify_default_action)

    def convert_all(self):
        self.set_all(lambda it: UNIFY_CONVERT)

    def download_all_found(self):
        # Confident matches only - weak guesses keep their action to be checked one by one.
        self.set_all(lambda it: UNIFY_DOWNLOAD if UNIFY_DOWNLOAD in unify_actions(it) and it.get("confident")
                     else it["action"])

    def skip_all(self):
        self.set_all(lambda it: UNIFY_SKIP)

    def stop(self):
        self.cancel_event.set()
        for proc in list(self.running_procs):
            try:
                proc.kill()
            except OSError:
                pass
        self.stop_btn.configure(state="disabled")
        self.status.configure(text="Stopping - finishing up...")

    def replace(self):
        jobs = self.pending()
        if not jobs:
            return
        self.running = True
        self.cancel_event = threading.Event()
        self.update_button()
        doing = "Replacing" if self.recycle else "Making m4a of"
        self.status.configure(text=f"{doing}...")
        self.log(f"{doing} {len(jobs)} songs ({self.bitrate}kbps when converted)...")
        lock, counts, renamed = threading.Lock(), {"done": 0, "ok": 0, "failed": 0, "cancelled": 0}, {}

        def set_note(i, text):
            try:
                self.after(0, lambda: self.tree.set(str(i), "note", text))
            except (tk.TclError, RuntimeError):
                pass

        def one(i):
            it = self.items[i]
            rel = self.shown(it["path"])
            outcome, result = "failed", None
            try:
                if self.cancel_event.is_set():
                    outcome, result = "cancelled", None
                else:
                    text = replace_with_m4a(it, self.bitrate, self.recycle, self.running_procs, self.cancel_event,
                                            self.log, lambda t: set_note(i, t))
                    with lock:
                        renamed[path_key(it["path"])] = it["dest"].name
                    it["done"] = True
                    outcome, result = "ok", ("done", "Done", text)
                    self.log(f"  {text}: {rel} -> {self.shown(it['dest'])}")
            except Exception as e:
                if self.cancel_event.is_set():
                    outcome, result = "cancelled", None
                else:
                    result = ("failed", "Failed", str(e))
                    self.log(f"  FAILED: {rel} - {e}")
            with lock:
                counts[outcome] += 1
                counts["done"] += 1
                n = counts["done"]
            self.after(0, lambda: self.row_done(i, result, n, len(jobs)))

        def run():
            with KEEP_AWAKE, ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                list(pool.map(one, jobs))
            # Copies in a "Converted (m4a)" subfolder leave the folder's playlists alone.
            update = renamed and not self.into_subfolder
            playlists = update_playlists_for_renames(self.folder, renamed, self.log) if update else 0
            text = (f"{'Replaced' if self.recycle else 'Made m4a of'} {counts['ok']} song{plural(counts['ok'])}"
                    + (f", {counts['failed']} failed" if counts["failed"] else "")
                    + (f", {counts['cancelled']} stopped" if counts["cancelled"] else "")
                    + (f", updated {playlists} playlist{plural(playlists)}." if not self.into_subfolder else "."))
            self.log(text)
            self.after(0, lambda: self.finished(text))

        threading.Thread(target=run, daemon=True).start()

    def row_done(self, i, result, n, total):
        if result is None:
            self.show_action(i)
        else:
            tag, action, note = result
            self.tree.item(str(i), tags=(tag,))
            self.tree.set(str(i), "action", action)
            self.tree.set(str(i), "note", note)
        self.status.configure(text=f"{'Replacing' if self.recycle else 'Making m4a'}... {n}/{total}")

    def finished(self, text):
        self.running = False
        self.status.configure(text=text)
        self.update_button()

    def close(self):
        if self.running:
            messagebox.showinfo("Still working", "Songs are still being made m4a - click Stop first, or wait for "
                                                 "them to finish.", parent=self)
            return
        self.destroy()


class MetadataReviewWindow(tk.Toplevel):
    """Lists every scanned file with its lookup matches, ranked by how
    closely they match the file's own title/artist. Each row has a picker
    with the top candidates plus "Don't change this file"; confident
    matches are pre-selected, weak ones default to "Don't change" so
    Apply All can't write another song's cover/genre into a file. Apply
    writes the selected match's tags + cover art into that file."""

    SKIP_CHOICE = "Don't change this file"

    def __init__(self, parent, items, log):
        super().__init__(parent)
        self.title("TrackMatch - Metadata Review")
        self.geometry("880x660")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)

        self.items = items
        self.log = log
        self.status_labels = {}
        self.apply_buttons = {}
        self.detail_labels = {}
        self.detail_kinds = {}    # idx -> 'found'/'missing', read by the detail label's theme closure
        self.choice_vars = {}     # idx -> StringVar holding the picker's label
        self.choice_lookup = {}   # idx -> {picker label: result dict}

        found = sum(1 for it in items if it["results"])
        confident = sum(1 for it in items if it["results"] and it["results"][0]["score"] >= METADATA_MIN_SCORE)
        header = tk.Label(
            self,
            text=f"{found}/{len(items)} tracks found results - {confident} confident matches are "
                 f"pre-selected. Weak matches (flagged below) default to \"{self.SKIP_CHOICE}\": pick the "
                 f"right result from the list if it's there. Apply writes Title (incl. mix), Artist, Album, "
                 f"Genre, Label, Year, BPM, Key, ISRC + cover art (whatever the source has) - its data is used "
                 f"where available, "
                 f"otherwise the file's existing tag is kept. Nothing happens until you click Apply (or Apply All).",
            justify="left", wraplength=840
        )
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 0))

        canvas = tk.Canvas(self, borderwidth=0)
        style_canvas(canvas)
        scroll_frame = tk.Frame(canvas); style_frame(scroll_frame)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=10, pady=10)
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        make_wheel_scrollable(canvas)

        for i, item in enumerate(items):
            row = tk.Frame(scroll_frame, relief="groove", borderwidth=1, highlightthickness=1)
            style_row_frame(row)
            row.pack(fill="x", pady=4, padx=2)

            title_label = tk.Label(row, text=f"{item['artist']} - {item['title']}    [{item['path'].name}]",
                                   justify="left", anchor="w", wraplength=820)
            style_label(title_label)
            title_label.pack(fill="x", padx=6, pady=(4, 0))

            detail_label = tk.Label(row, text="", justify="left", anchor="w", wraplength=820)
            self.detail_kinds[i] = "found"
            register(lambda lbl=detail_label, idx=i: lbl.configure(
                bg=THEME["BG"], fg=THEME["FOUND_FG" if self.detail_kinds[idx] == "found" else "MISSING_FG"]))
            detail_label.pack(fill="x", padx=6, pady=(0, 2))
            self.detail_labels[i] = detail_label

            control_frame = tk.Frame(row); style_frame(control_frame)
            control_frame.pack(fill="x", padx=6, pady=(0, 6))

            lookup = {}
            for n, r in enumerate(item["results"], 1):
                lookup[f"{n}. [{r['source']}] {r['artists']} - {r['title']}  ({r['score']:.0%})"] = r
            self.choice_lookup[i] = lookup

            best = item["results"][0] if item["results"] else None
            if best and best["score"] >= METADATA_MIN_SCORE:
                initial = next(iter(lookup))
            else:
                initial = self.SKIP_CHOICE
            var = tk.StringVar(value=initial)
            self.choice_vars[i] = var

            apply_btn = ThemedButton(control_frame, text="Apply", command=lambda idx=i: self.apply_item(idx))
            apply_btn.pack(side="left")
            self.apply_buttons[i] = apply_btn

            if lookup:
                picker = ThemedOptionMenu(control_frame, var, *lookup.keys(), self.SKIP_CHOICE,
                                       command=lambda _value, idx=i: self.refresh_row(idx))
                picker.pack(side="left", fill="x", expand=True, padx=(6, 0))

            status_label = tk.Label(row, text="", justify="left", anchor="w")
            style_label(status_label, subtle=True)
            status_label.pack(fill="x", padx=6, pady=(0, 4))
            self.status_labels[i] = status_label

            self.refresh_row(i)

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        close_btn = ThemedButton(bottom, text="Close", command=self.destroy, width=12)
        close_btn.pack(side="right")
        self.apply_all_btn = ThemedButton(bottom, text="Apply All Selected", command=self.apply_all)
        self.apply_all_btn.pack(side="right", padx=(0, 6))
        if found == 0:
            self.apply_all_btn.configure(state="disabled")

    def selected_result(self, idx):
        """The lookup result currently picked for row idx, or None for
        "Don't change". Must be called on the Tk main thread."""
        return self.choice_lookup[idx].get(self.choice_vars[idx].get())

    def refresh_row(self, idx):
        """Updates the detail line and Apply button to match the picker."""
        item = self.items[idx]
        chosen = self.selected_result(idx)
        label = self.detail_labels[idx]

        if not item["results"]:
            label.configure(text=f"None found on {', '.join(item.get('tried') or ['any source'])}")
            self.set_detail_kind(idx, "missing")
            self.apply_buttons[idx].configure(state="disabled")
            return

        if chosen is None:
            best = item["results"][0]
            if best["score"] < METADATA_MIN_SCORE:
                text = (f"Weak match only - best was {best['score']:.0%} similar. Check the list, or leave "
                        f"this file unchanged.")
            else:
                text = "Leaving this file unchanged."
            label.configure(text=text)
            self.set_detail_kind(idx, "missing")
            self.apply_buttons[idx].configure(state="disabled")
            return

        fields = self.resolve_fields(item, chosen)
        parts = [
            f"From {chosen['source']}: \"{', '.join(fields['artist'])} - {fields['title']}\"",
            f"Album: {fields['album'] or '(none)'}",
            f"Genre: {fields['genre'] or '(none)'}",
        ]
        for name, key in (("Label", "label"), ("Year", "year"), ("BPM", "bpm"), ("Key", "key")):
            if fields.get(key):
                parts.append(f"{name}: {fields[key]}")
        parts.append("cover art" if chosen.get("image_url") else "no cover art available")
        label.configure(text="  |  ".join(parts))
        self.set_detail_kind(idx, "found" if chosen["score"] >= METADATA_MIN_SCORE else "missing")
        self.apply_buttons[idx].configure(state="normal")

    def set_detail_kind(self, idx, kind):
        self.detail_kinds[idx] = kind
        self.detail_labels[idx].configure(fg=THEME["FOUND_FG" if kind == "found" else "MISSING_FG"])

    def resolve_fields(self, item, best):
        """The file's current tags, overridden by the lookup result wherever
        it has a value - see merge_metadata_fields."""
        return merge_metadata_fields(best, existing_fields_of(item))

    def set_row_status(self, idx, text):
        def update():
            if idx in self.status_labels:
                self.status_labels[idx].configure(text=text)
        self.after(0, update)

    def write_item(self, idx, chosen):
        """Downloads the cover and writes all tags for one row. Runs on a
        background thread; returns True/False for success."""
        item = self.items[idx]
        self.set_row_status(idx, "Downloading cover art and updating tags...")
        try:
            self.set_row_status(idx, apply_metadata_match(item, chosen, self.log))
            return True
        except Exception as e:
            self.log(f"  Could not write metadata to \"{item['path'].name}\": {e}")
            self.set_row_status(idx, f"Failed: {e}")
            self.after(0, lambda: self.apply_buttons[idx].configure(state="normal"))
            return False

    def apply_item(self, idx):
        chosen = self.selected_result(idx)
        if not chosen:
            return
        self.apply_buttons[idx].configure(state="disabled")
        threading.Thread(target=self.write_item, args=(idx, chosen), daemon=True).start()

    def apply_all(self):
        # Read every picker here on the main thread - Tk variables aren't
        # safe to touch from the worker thread.
        jobs = [(i, self.selected_result(i)) for i in range(len(self.items))]
        jobs = [(i, chosen) for i, chosen in jobs if chosen and str(self.apply_buttons[i]["state"]) == "normal"]
        if not jobs:
            messagebox.showinfo("Nothing to apply", "No rows have a match selected.", parent=self)
            return

        self.apply_all_btn.configure(state="disabled")
        for i, _ in jobs:
            self.apply_buttons[i].configure(state="disabled")
        self.log(f"\nApplying metadata to {len(jobs)} tracks...")

        def do_all():
            applied, failed = 0, 0
            with KEEP_AWAKE:
                outcomes = [self.write_item(i, chosen) for i, chosen in jobs]
            for ok in outcomes:
                if ok:
                    applied += 1
                else:
                    failed += 1
            self.log(f"\nApply All complete: {applied} updated, {failed} failed.")
            self.after(0, lambda: self.apply_all_btn.configure(state="normal"))
            self.after(0, lambda: messagebox.showinfo(
                "Apply All complete", f"{applied} track(s) updated.\n{failed} failed.", parent=self
            ))

        threading.Thread(target=do_all, daemon=True).start()


# ---------- Large library support (Metadata Editor) ----------

LARGE_FOLDER_WARNING = 500   # songs - above this, suggest Large Library Mode if it's off
SMALL_REVIEW_LIMIT = 150     # more songs than this always get the compact review table
# Bump when lookup parsing/scoring changes, so answers saved by an older
# version aren't reused (they'd carry the old mistakes).
LOOKUP_CACHE_VERSION = 2
LOOKUP_WORKERS = 3           # songs looked up at once in Large Library Mode


def existing_fields_of(item):
    """The tags a scanned file already had, as merge_metadata_fields expects."""
    return {"title": item["title"], "artist": item["artist"],
            "album": item.get("existing_album", ""), "genre": item.get("existing_genre", "")}


def apply_metadata_match(item, chosen, log):
    """Writes a lookup result's tags + cover art into a scanned file (the
    file's existing tags fill any gaps). Raises on failure. Returns a short
    status text for the GUI."""
    if chosen.get("source") == "Deezer" and not chosen.get("_enriched"):
        # Deezer alternatives only get their genre/label/date when picked.
        try:
            _enrich_deezer_result(chosen)
        except Exception as e:
            log(f"    Deezer details lookup failed: {e}")
    fields = merge_metadata_fields(chosen, existing_fields_of(item))
    image_bytes, image_mime = fetch_cover_art(chosen, log)
    if not image_bytes:
        log(f"  No cover art available for \"{item['path'].name}\" - writing text tags only.")
    cover_written = embed_full_metadata(item["path"], fields, image_bytes, image_mime)
    log(f"  Updated: {item['path'].name}")
    if cover_written:
        return "Applied - full metadata + cover art updated."
    if image_bytes:
        return f"Applied - metadata updated (cover art isn't supported for {item['path'].suffix} files)."
    return "Applied - metadata updated (no cover image was available)."


def read_song_for_lookup(path: Path):
    """Everything a Tagging scan needs from a file in ONE read: its current
    title/artist/album/genre (title/artist fall back to the filename, as in
    read_existing_tags), its TrackMatch source tag, whether it has cover art,
    and its length."""
    try:
        audio = MutagenFile(path)
    except Exception:
        audio = None
    try:
        fields, image, _ = read_tags_and_cover(path, audio) if audio is not None else ({}, None, None)
    except Exception:
        fields, image = {}, None
    artists = fields.get("artist") or []
    title, artist = fields.get("title") or "", artists[0] if artists else ""
    if not title:
        stem = path.stem
        if " - " in stem:
            guessed_artist, title = stem.split(" - ", 1)
            artist = artist or guessed_artist
        else:
            title = stem
    return {
        "existing": {"title": title, "artist": artist, "album": fields.get("album", ""),
                     "genre": fields.get("genre", "")},
        "source": fields.get("source"),
        "has_cover": bool(image),
        "length": getattr(getattr(audio, "info", None), "length", None),
        "bitrate": getattr(getattr(audio, "info", None), "bitrate", None),  # bits per second
    }


def lookup_skip_reason(song, skip_tagged, only_missing):
    """Why a file (from read_song_for_lookup) can be skipped by the scan's
    filters, or None to look it up."""
    if skip_tagged and song["source"] in METADATA_SEARCHERS:
        return "already tagged by TrackMatch"
    if only_missing and song["existing"]["genre"] and song["has_cover"]:
        return "already has genre and cover art"
    return None


class LookupCache:
    """Saved lookup answers for Large Library Mode, so a long scan can be
    stopped and resumed and re-runs don't ask the websites again. One JSON
    object per line, appended as each song is looked up - so a crash loses
    at most the song in progress. Kept until cleared in Settings. Keyed on
    the file's path, size and modified time (a changed file is looked up
    again) plus the source and minimum-match setting (which change what a
    lookup returns)."""

    def __init__(self, filename: str):
        self.filename = filename
        self._lock = threading.Lock()
        self._data = None

    @property
    def path(self) -> Path:
        return user_data_dir() / self.filename

    def _load(self):
        if self._data is not None:
            return
        self._data = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        self._data[entry["k"]] = (entry["r"], entry["t"])
                    except (ValueError, KeyError, TypeError):
                        continue  # a line cut short by a crash - just look that song up again
        except FileNotFoundError:
            pass

    @staticmethod
    def key(path: Path, source):
        st = path.stat()
        return (f"v{LOOKUP_CACHE_VERSION}|{source}|{METADATA_MIN_SCORE}|{path.resolve()}|"
                f"{st.st_size}|{st.st_mtime_ns}")

    def get(self, key):
        with self._lock:
            self._load()
            return self._data.get(key)

    def put(self, key, results, tried):
        with self._lock:
            self._load()
            self._data[key] = (results, tried)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"k": key, "r": results, "t": tried}) + "\n")
            except OSError:
                pass  # can't save - the scan still works, it just won't resume

    def stats(self):
        """(saved answers, size in bytes)."""
        with self._lock:
            self._load()
            size = self.path.stat().st_size if self.path.exists() else 0
            return len(self._data), size

    def clear(self):
        with self._lock:
            self._data = {}
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


LOOKUP_CACHE = LookupCache("trackmatch_lookup_cache.jsonl")


class ThemedMessages:
    """Drop-in for tkinter.messagebox, drawn in the app's own colours - the
    system message boxes are always light on Windows and look different on
    every platform. Enter = the main button, Escape = cancel / close."""

    ICONS = {"info": ("\u2139", "ACCENT"), "warning": ("\u26a0", "MISSING_FG"), "error": ("\u2716", "MISSING_FG"),
             "question": ("?", "ACCENT")}

    def _show(self, title, message, buttons, kind, options):
        parent = options.get("parent") or tk._default_root
        if parent is None:
            return buttons[-1][1]
        dialog = tk.Toplevel(parent)
        dialog.withdraw()
        dialog.title(title or "TrackMatch")
        style_window(dialog)
        set_window_icon(dialog, ICON_PATH)
        dialog.transient(parent.winfo_toplevel())
        dialog.resizable(False, False)
        result = {"value": buttons[-1][1]}  # closing the window = the last (cancel-type) button

        body = tk.Frame(dialog); style_frame(body)
        body.pack(fill="both", expand=True, padx=18, pady=(16, 8))
        glyph, color_key = self.ICONS[options.get("icon", kind)]
        icon = tk.Label(body, text=glyph, font="TMHeading")
        register(lambda: icon.configure(bg=THEME["BG"], fg=THEME[color_key]))
        icon.pack(side="left", anchor="n", padx=(0, 12))
        text = tk.Label(body, text=message or "", justify="left", anchor="w", wraplength=460)
        style_label(text)
        text.pack(side="left", fill="both", expand=True)

        row = tk.Frame(dialog); style_frame(row)
        row.pack(fill="x", padx=18, pady=(4, 16))

        def choose(value):
            result["value"] = value
            dialog.destroy()

        default_value = buttons[0][1]
        for label, value in reversed(buttons):
            btn = ThemedButton(row, text=label, accent=(value == default_value), command=lambda v=value: choose(v))
            btn.pack(side="right", padx=(6, 0))
        dialog.bind("<Return>", lambda e: choose(default_value))
        dialog.bind("<Escape>", lambda e: choose(buttons[-1][1]))
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(buttons[-1][1]))

        dialog.update_idletasks()
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + max(0, (top.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = top.winfo_rooty() + max(0, (top.winfo_height() - dialog.winfo_reqheight()) // 3)
        dialog.geometry(f"+{x}+{y}")
        dialog.deiconify()
        enable_dark_titlebar(dialog)
        dialog.grab_set()
        dialog.focus_set()
        parent.wait_window(dialog)
        return result["value"]

    def showinfo(self, title=None, message=None, **options):
        return self._show(title, message, [("OK", "ok")], "info", options)

    def showwarning(self, title=None, message=None, **options):
        return self._show(title, message, [("OK", "ok")], "warning", options)

    def showerror(self, title=None, message=None, **options):
        return self._show(title, message, [("OK", "ok")], "error", options)

    def askyesno(self, title=None, message=None, **options):
        return self._show(title, message, [("Yes", True), ("No", False)], "question", options)

    def askokcancel(self, title=None, message=None, **options):
        return self._show(title, message, [("OK", True), ("Cancel", False)], "question", options)


messagebox = ThemedMessages()


def ask_choice(parent, title, message, buttons):
    """Modal dialog with custom button labels. Returns the clicked label,
    or None if the window was closed."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    style_window(dialog)
    set_window_icon(dialog, ICON_PATH)
    enable_dark_titlebar(dialog)
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)
    result = {"choice": None}

    label = tk.Label(dialog, text=message, justify="left", wraplength=520)
    style_label(label)
    label.pack(anchor="w", padx=16, pady=(16, 12))
    row = tk.Frame(dialog); style_frame(row)
    row.pack(fill="x", padx=16, pady=(0, 16))

    def choose(text):
        result["choice"] = text
        dialog.destroy()

    for text in reversed(buttons):
        btn = ThemedButton(row, text=text, accent=(text == buttons[0]), command=lambda t=text: choose(t))
        btn.pack(side="right", padx=(6, 0))

    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.grab_set()
    dialog.focus_set()
    parent.wait_window(dialog)
    return result["choice"]


def candidate_label(r):
    extras = " | ".join(x for x in (r.get("genre"), r.get("year")) if x)
    return f"[{r['source']}] {r['artists']} - {r['title']}  ({r['score']:.0%})" + (f"  |  {extras}" if extras else "")


class CandidatePicker(tk.Toplevel):
    """Pick which lookup result (or none) to use for one file."""

    def __init__(self, parent, item, current, on_pick):
        super().__init__(parent)
        self.title(f"Choose a match: {item['path'].name}")
        self.geometry("760x360")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)
        self.transient(parent)
        self.item = item
        self.on_pick = on_pick

        header = tk.Label(self, text=f"{item['artist']} - {item['title']}\n{item['path']}",
                          justify="left", anchor="w", wraplength=730)
        style_label(header)
        header.pack(fill="x", padx=12, pady=(12, 6))

        self.options = [None] + list(item["results"])
        self.listbox = tk.Listbox(self, activestyle="none", height=8)
        register(lambda: self.listbox.configure(bg=THEME["ENTRY_BG"], fg=THEME["FG"], relief="flat",
                                                selectbackground=THEME["ACCENT"], selectforeground="#ffffff",
                                                highlightthickness=0))
        self.listbox.insert("end", "Don't change this file")
        for r in item["results"]:
            self.listbox.insert("end", candidate_label(r))
        self.listbox.pack(fill="both", expand=True, padx=12)
        index = 0 if current is None else 1 + next((i for i, r in enumerate(item["results"]) if r is current), 0)
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self.listbox.bind("<Double-1>", lambda e: self.ok())

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=12, pady=12)
        ok_btn = ThemedButton(bottom, text="Use This", command=self.ok, accent=True)
        ok_btn.pack(side="right")
        cancel_btn = ThemedButton(bottom, text="Cancel", command=self.destroy, width=10)
        cancel_btn.pack(side="right", padx=(0, 6))
        self.grab_set()

    def ok(self):
        selection = self.listbox.curselection()
        if selection:
            self.on_pick(self.options[selection[0]])
        self.destroy()


class LargeReviewWindow(tk.Toplevel):
    """Large Library Mode's review: one table row per file instead of a
    block of controls each, filtered to what needs attention. Confident
    matches (at or above the Minimum match setting) are pre-selected;
    double-click any row to pick a different result or "Don't change"."""

    FILTER_CHECK = "Needs checking"
    FILTER_CONFIDENT = "Confident"
    FILTER_ALL = "All"
    COLUMNS = [("file", "File", 230), ("match", "Will write", 300), ("source", "Source", 70),
               ("score", "Match", 55), ("genre", "Genre", 120), ("year", "Year", 50), ("action", "Status", 150)]

    def __init__(self, parent, items, log):
        super().__init__(parent)
        self.title("TrackMatch - Metadata Review (Large Library)")
        self.geometry("1040x680")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)

        self.items = items
        self.log = log
        self.choice = {}
        self.status = {}
        for i, it in enumerate(items):
            confident = bool(it["results"]) and it["results"][0]["score"] >= METADATA_MIN_SCORE
            self.choice[i] = it["results"][0] if confident else None
            self.status[i] = ""
        self.applying = False
        self.stop_applying = threading.Event()

        confident = sum(1 for c in self.choice.values() if c is not None)
        header = tk.Label(
            self,
            text=f"{len(items)} songs looked up: {confident} confident matches (at or above {METADATA_MIN_SCORE:.0%}) "
                 f"are selected, the other {len(items) - confident} are set to \"Don't change\". Double-click a row "
                 f"to pick a different result. Nothing is written until you click Apply.",
            justify="left", wraplength=1000
        )
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 4))

        filter_row = tk.Frame(self); style_frame(filter_row)
        filter_row.pack(fill="x", padx=10)
        l_show = tk.Label(filter_row, text="Show:"); style_label(l_show)
        l_show.pack(side="left", padx=(0, 6))
        self.filter_var = tk.StringVar(value=self.FILTER_CHECK if confident < len(items) else self.FILTER_ALL)
        self.filter_buttons = {}
        for name in (self.FILTER_CHECK, self.FILTER_CONFIDENT, self.FILTER_ALL):
            rb = ThemedRadiobutton(filter_row, variable=self.filter_var, value=name, command=self.refresh)
            style_radiobutton(rb)
            rb.pack(side="left", padx=(0, 12))
            self.filter_buttons[name] = rb

        table = tk.Frame(self); style_frame(table)
        table.pack(fill="both", expand=True, padx=10, pady=6)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings", selectmode="extended")
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=key in ("file", "match"), anchor="w")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        register(lambda: (self.tree.tag_configure("confident", foreground=THEME["FOUND_FG"]),
                          self.tree.tag_configure("check", foreground=THEME["MISSING_FG"]),
                          self.tree.tag_configure("done", foreground=THEME["SUBTLE_FG"])))
        self.tree.bind("<Double-1>", self.pick_for_row)
        self.tree.bind("<Return>", self.pick_for_row)

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        self.apply_all_btn = ThemedButton(bottom, text="Apply All Selected Matches", command=self.apply_all, accent=True)
        self.apply_all_btn.pack(side="left")
        self.apply_rows_btn = ThemedButton(bottom, text="Apply Highlighted Rows", command=self.apply_highlighted)
        self.apply_rows_btn.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.progress_label = tk.Label(bottom, text="", width=14, anchor="w")
        style_label(self.progress_label, subtle=True)
        self.progress_label.pack(side="left")
        close_btn = ThemedButton(bottom, text="Close", command=self.close, width=10)
        close_btn.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.refresh()

    # --- table ---

    def is_confident(self, i):
        results = self.items[i]["results"]
        return bool(results) and results[0]["score"] >= METADATA_MIN_SCORE

    def visible(self, i):
        f = self.filter_var.get()
        if f == self.FILTER_CHECK:
            return not self.is_confident(i)
        if f == self.FILTER_CONFIDENT:
            return self.is_confident(i)
        return True

    def row_values(self, i):
        item, chosen = self.items[i], self.choice[i]
        if chosen is not None:
            fields = merge_metadata_fields(chosen, existing_fields_of(item))
            match = f"{', '.join(fields['artist'])} - {fields['title']}"
            values = (item["path"].name, match, chosen["source"], f"{chosen['score']:.0%}",
                      fields["genre"], fields["year"])
        elif item["results"]:
            best = item["results"][0]
            values = (item["path"].name, f"(best: {best['artists']} - {best['title']})", best["source"],
                      f"{best['score']:.0%}", "", "")
        else:
            values = (item["path"].name, f"(nothing found on {', '.join(item.get('tried') or [])})", "", "", "", "")
        action = self.status[i] or ("Will apply" if chosen is not None else "Don't change")
        return (*values, action)

    def row_tag(self, i):
        if self.status[i].startswith("Applied"):
            return "done"
        return "confident" if self.choice[i] is not None else "check"

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i in range(len(self.items)):
            if self.visible(i):
                self.tree.insert("", "end", iid=str(i), values=self.row_values(i), tags=(self.row_tag(i),))
        counts = {self.FILTER_CHECK: sum(1 for i in range(len(self.items)) if not self.is_confident(i)),
                  self.FILTER_CONFIDENT: sum(1 for i in range(len(self.items)) if self.is_confident(i)),
                  self.FILTER_ALL: len(self.items)}
        for name, rb in self.filter_buttons.items():
            rb.configure(text=f"{name} ({counts[name]})")
        pending = sum(1 for i, c in self.choice.items() if c is not None and not self.status[i].startswith("Applied"))
        self.apply_all_btn.configure(text=f"Apply All Selected Matches ({pending})")

    def update_row(self, i):
        if self.tree.exists(str(i)):
            self.tree.item(str(i), values=self.row_values(i), tags=(self.row_tag(i),))

    def pick_for_row(self, _event=None):
        iid = self.tree.focus()
        if not iid or self.applying:
            return
        i = int(iid)
        if not self.items[i]["results"]:
            return

        def picked(result):
            self.choice[i] = result
            if not self.status[i].startswith("Applied"):
                self.status[i] = ""
            self.update_row(i)
            self.refresh_counts_only()

        CandidatePicker(self, self.items[i], self.choice[i], picked)

    def refresh_counts_only(self):
        pending = sum(1 for i, c in self.choice.items() if c is not None and not self.status[i].startswith("Applied"))
        self.apply_all_btn.configure(text=f"Apply All Selected Matches ({pending})")

    # --- applying ---

    def apply_all(self):
        self.start_apply([i for i, c in self.choice.items() if c is not None])

    def apply_highlighted(self):
        rows = [int(iid) for iid in self.tree.selection()]
        rows = [i for i in rows if self.choice[i] is not None]
        if not rows:
            messagebox.showinfo("Nothing to apply", "Highlight rows that have a match selected "
                                                    "(double-click a row to choose one).", parent=self)
            return
        self.start_apply(rows)

    def start_apply(self, rows):
        jobs = [(i, self.choice[i]) for i in rows if not self.status[i].startswith("Applied")]
        if not jobs:
            messagebox.showinfo("Nothing to apply", "Those rows have already been applied.", parent=self)
            return
        if self.applying:
            return
        self.applying = True
        self.apply_all_btn.configure(state="disabled")
        self.apply_rows_btn.configure(state="disabled")
        self.progress.configure(maximum=len(jobs), value=0)
        self.log(f"\nApplying metadata to {len(jobs)} songs...")
        counter = {"done": 0, "ok": 0, "failed": 0}
        lock = threading.Lock()

        def ui(fn):
            try:
                self.after(0, fn)
            except (tk.TclError, RuntimeError):
                pass

        def one(job):
            i, chosen = job
            if self.stop_applying.is_set():
                return  # window closed - leave the rest unchanged
            try:
                self.status[i] = apply_metadata_match(self.items[i], chosen, self.log)
                ok = True
            except Exception as e:
                self.status[i] = f"Failed: {e}"
                self.log(f"  Could not write metadata to \"{self.items[i]['path'].name}\": {e}")
                ok = False
            with lock:
                counter["done"] += 1
                counter["ok" if ok else "failed"] += 1
                done = counter["done"]
            ui(lambda: (self.update_row(i), self.progress.configure(value=done),
                        self.progress_label.configure(text=f"{done}/{len(jobs)}")))

        def run():
            with KEEP_AWAKE, ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
                list(pool.map(one, jobs))
            self.log(f"Apply complete: {counter['ok']} updated, {counter['failed']} failed.")
            ui(self.apply_finished(counter))

        threading.Thread(target=run, daemon=True).start()

    def apply_finished(self, counter):
        def finish():
            self.applying = False
            self.apply_all_btn.configure(state="normal")
            self.apply_rows_btn.configure(state="normal")
            self.refresh_counts_only()
            messagebox.showinfo("Apply complete", f"{counter['ok']} song(s) updated.\n{counter['failed']} failed.",
                                parent=self)
        return finish

    def close(self):
        if self.applying and not messagebox.askyesno(
                "Still applying", "Tags are still being written. Close anyway? (Songs already started will "
                                  "finish; the rest won't be changed.)", parent=self):
            return
        self.stop_applying.set()
        self.destroy()


# ---------- Tab 4: Convert Audio ----------

class ConvertAudioTab(LogPanelMixin, tk.Frame):
    """Converts a folder of songs to another format (see AudioConverter),
    with "Use for" presets, a live quality/compatibility note, and a
    confirmation showing sizes and how many CDs the result fills."""

    ABOUT = ("Makes copies of a folder of songs in another format, keeping tags and cover art. Most common uses:\n"
             "  \u2022  Burning CDs - mp3 works with every burning program, and \"MP3 CDs\" play in most car stereos "
             "and CD players\n"
             "  \u2022  Older CDJs, car USB ports and media players that only read mp3\n"
             "  \u2022  iPhone / iTunes / Apple Music - m4a\n"
             "  \u2022  DJ software with full tag & artwork support - aiff (from wav/flac)\n"
             "  \u2022  Archiving lossless originals in less space - flac\n\n"
             "Converting can't improve quality - the result is only ever as good as the source.")

    REDOWNLOAD_ABOUT = (
        "For m4a only. Instead of converting straight away, Convert first lists every song with what happens to "
        "it - you can change each one:\n\n"
        "•  Convert: flac / wav / aiff convert cleanly at the bitrate above. .aac files are moved into m4a "
        "with the audio untouched. mp3 / ogg / opus / wma can be converted too, but that's lossy to lossy - it "
        "loses a little more detail.\n"
        f"•  Download: gets the song again from YouTube as m4a (YouTube's own ~{YT_AUDIO_KBPS}kbps AAC, no "
        "re-encode). Only offered for lossy songs, and only picked for you when the upload is the same song and "
        f"length. Songs at {UNIFY_KEEP_KBPS}kbps or more start skipped - they're already better than YouTube's "
        "audio. A download that turns out a different length isn't kept.\n\n"
        "The new m4a keeps the song's tags and cover art and the same file name, saved where \"Save copies\" "
        "says. When saved next to the originals, .m3u / .m3u8 playlists in the folder are updated. Rekordbox, "
        "Serato and Traktor show replaced songs as missing, and cue points and beatgrids on the old files don't "
        "carry over. If YouTube asks you to sign in, set YouTube cookies in Settings.")

    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)

        use = make_section(self, "Convert songs", self.ABOUT, pady=(12, 0))
        preset_row = tk.Frame(use); style_frame(preset_row)
        preset_row.pack(fill="x")
        l0 = tk.Label(preset_row, text="Use for:"); style_label(l0)
        l0.pack(side="left", padx=(0, 8))
        self.preset_var = tk.StringVar(value="Audio CD burning")
        ThemedOptionMenu(preset_row, self.preset_var, *CONVERT_PRESETS, CONVERT_PRESET_CUSTOM,
                         command=lambda _v: self.apply_preset()).pack(side="left")
        info_button(preset_row, lambda: CONVERT_PRESETS[self.preset_var.get()][2]
                    if self.preset_var.get() in CONVERT_PRESETS else "Pick any source and target format below.",
                    "Use for").pack(side="left", padx=(6, 0))

        songs = make_section(self, "Songs")
        self.convert_folder_entry = make_path_row(songs, "Folder of songs:",
                                                  lambda e: browse_folder_into(e, "Select a folder of songs to convert"),
                                                  placeholder="The folder of songs to convert")
        fmt_row = tk.Frame(songs); style_frame(fmt_row)
        fmt_row.pack(fill="x", pady=(4, 0))
        l1 = tk.Label(fmt_row, text="Convert:"); style_label(l1)
        l1.pack(side="left", padx=(0, 8))
        self.convert_from_var = tk.StringVar(value="Any format")
        ThemedOptionMenu(fmt_row, self.convert_from_var, *CONVERT_SOURCES).pack(side="left")
        l2 = tk.Label(fmt_row, text="to"); style_label(l2)
        l2.pack(side="left", padx=8)
        self.convert_to_var = tk.StringVar(value="mp3")
        ThemedOptionMenu(fmt_row, self.convert_to_var, *CONVERT_TARGETS).pack(side="left")
        self.bitrate_frame = tk.Frame(fmt_row); style_frame(self.bitrate_frame)
        l3 = tk.Label(self.bitrate_frame, text="at"); style_label(l3)
        l3.pack(side="left", padx=8)
        self.convert_bitrate_var = tk.StringVar(value="320")
        self.convert_bitrate_menu = ThemedOptionMenu(self.bitrate_frame, self.convert_bitrate_var, *CONVERT_BITRATES)
        self.convert_bitrate_menu.pack(side="left")
        l4 = tk.Label(self.bitrate_frame, text="kbps"); style_label(l4)
        l4.pack(side="left", padx=(6, 0))
        self.convert_recursive_var = tk.BooleanVar(value=True)
        ThemedCheckbutton(songs, text="Include subfolders", variable=self.convert_recursive_var).pack(anchor="w",
                                                                                                    pady=(6, 0))
        redownload_box = tk.Frame(songs); style_frame(redownload_box)
        redownload_box.pack(fill="x")
        self.redownload_row = tk.Frame(redownload_box); style_frame(self.redownload_row)
        self.convert_redownload_var = tk.BooleanVar(value=False)
        ThemedCheckbutton(self.redownload_row, variable=self.convert_redownload_var,
                          text="Download again from YouTube instead of converting (lossy songs only)").pack(side="left")
        info_button(self.redownload_row, self.REDOWNLOAD_ABOUT, "Download again").pack(side="left", padx=(4, 0))
        warning_row = tk.Frame(songs); style_frame(warning_row)
        warning_row.pack(fill="x", pady=(6, 0))
        self.convert_warning = tk.Label(warning_row, text="", justify="left", anchor="w", wraplength=560)
        self.convert_warning_kind = "ok"
        register(lambda: self.convert_warning.configure(
            bg=THEME["BG"], fg=THEME["MISSING_FG" if self.convert_warning_kind == "warn" else "SUBTLE_FG"]))
        self.convert_warning.pack(side="left")
        info_button(warning_row, lambda: self.quality_help(), "Quality & compatibility").pack(side="left", padx=(4, 0))

        fold = Fold(songs)
        out_row = tk.Frame(fold.body); style_frame(out_row)
        out_row.pack(fill="x", pady=(2, 0))
        l5 = tk.Label(out_row, text="Save copies:"); style_label(l5)
        l5.pack(side="left", padx=(0, 8))
        self.convert_into_subfolder_var = tk.BooleanVar(value=True)
        self.subfolder_radio = ThemedRadiobutton(out_row, variable=self.convert_into_subfolder_var, value=True,
                                                 text="In a \"Converted (mp3)\" subfolder")
        self.subfolder_radio.pack(side="left")
        ThemedRadiobutton(out_row, variable=self.convert_into_subfolder_var, value=False,
                          text="Next to the originals").pack(side="left", padx=(12, 0))
        self.convert_recycle_var = tk.BooleanVar(value=False)
        ThemedCheckbutton(fold.body, variable=self.convert_recycle_var, wraplength=620,
                          text=f"Move each original to the {TRASH_NAME} once its copy has converted and checked "
                               f"out OK").pack(anchor="w", pady=(6, 0))

        self.convert_button, (self.convert_cancel_button,), self.convert_progress, self.status_line = make_action_row(
            self, "Convert", self.convert_clicked, [("Cancel", self.cancel_conversion, "disabled")])
        self.convert_count_label = tk.Label(self.convert_progress.master, text="", width=9, anchor="e")
        style_label(self.convert_count_label, subtle=True)
        self.convert_count_label.pack(side="left", padx=(6, 0))

        log_label = tk.Label(self, text="Log:"); style_label(log_label)
        log_label.pack(anchor="w", padx=12, pady=(8, 0))
        self.log_box = ThemedScrolledText(self, height=6, state="disabled", wrap="word")
        style_scrolledtext(self.log_box)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        register_log_panel(log_label, self.log_box)

        self.converter = None
        self.format_scan_cancel = None
        self._applying_preset = False
        for var in (self.convert_from_var, self.convert_to_var, self.convert_bitrate_var):
            var.trace_add("write", lambda *_: self.on_options_changed())
        self.apply_preset()

        if shutil.which("ffmpeg") is None:
            self.log("NOTE: ffmpeg isn't on your PATH - converting won't work until you install it "
                     f"({FFMPEG_INSTALL_HINT}).")

    def quality_help(self):
        text, _kind = conversion_warning(self.convert_from_var.get(), self.convert_to_var.get(),
                                         self.convert_bitrate_var.get())
        return (f"{text}\n\n{CONVERT_COMPATIBILITY[self.convert_to_var.get()]}\n\nConverted files are NEW files: "
                "Rekordbox / Serato / Traktor treat them as new tracks, so cue points, hot cues, beat grids, play "
                "counts and playlist entries stay with the old files, and .m3u8 playlists keep pointing at the old "
                "files. Existing files are never overwritten.")

    def apply_preset(self):
        """Fills in the target format/bitrate for the chosen "Use for"
        preset. Custom leaves them as they are."""
        name = self.preset_var.get()
        if name in CONVERT_PRESETS:
            target, bitrate, _tip = CONVERT_PRESETS[name]
            self._applying_preset = True
            try:
                self.convert_to_var.set(target)
                if bitrate:
                    self.convert_bitrate_var.set(bitrate)
            finally:
                self._applying_preset = False
        self.update_convert_hints()

    def on_options_changed(self):
        # Changing the format/bitrate by hand turns the preset into Custom.
        name = self.preset_var.get()
        if not self._applying_preset and name in CONVERT_PRESETS:
            target, bitrate, _tip = CONVERT_PRESETS[name]
            if self.convert_to_var.get() != target or (bitrate and self.convert_bitrate_var.get() != bitrate):
                self.preset_var.set(CONVERT_PRESET_CUSTOM)
        self.update_convert_hints()

    def update_convert_hints(self):
        """Bitrate only for mp3/m4a, and a one-line quality note (orange when the
        conversion costs quality or bloats files) - full detail is in its info button."""
        source, target = self.convert_from_var.get(), self.convert_to_var.get()
        set_visible(self.bitrate_frame, target in ("mp3", "m4a"), side="left")
        set_visible(self.redownload_row, target == UNIFY_TARGET, fill="x", pady=(6, 0))
        self.subfolder_radio.configure(text=f"In a \"{CONVERTED_FOLDER_PREFIX}{target})\" subfolder")
        text, kind = conversion_warning(source, target, self.convert_bitrate_var.get())
        self.convert_warning_kind = kind
        self.convert_warning.configure(text=text.split(". ")[0].rstrip("."),
                                       fg=THEME["MISSING_FG" if kind == "warn" else "SUBTLE_FG"])

    def convert_clicked(self):
        folder = self.convert_folder_entry.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("Missing info", "Choose a valid folder of songs to convert.")
            return
        if not MutagenFile:
            messagebox.showerror("Missing dependency", "Install requirements first:\npip install mutagen")
            return
        if shutil.which("ffmpeg") is None:
            messagebox.showerror("ffmpeg not found", "Converting needs ffmpeg on your PATH.\n"
                                                      f"Install it with:  {FFMPEG_INSTALL_HINT}")
            return
        source, target = self.convert_from_var.get(), self.convert_to_var.get()
        if source == target:
            messagebox.showerror("Same format", f"Those files are already {target}.")
            return
        opts = (Path(folder), source, target, self.convert_bitrate_var.get(), self.convert_recursive_var.get(),
                self.convert_into_subfolder_var.get(), self.convert_recycle_var.get())
        if target == UNIFY_TARGET and self.convert_redownload_var.get():
            self.start_format_scan(*opts)
            return

        self.convert_button.configure(state="disabled")
        self.convert_count_label.configure(text="Scanning...")
        threading.Thread(target=self.scan_for_conversion, args=opts, daemon=True).start()

    def start_format_scan(self, folder, source, target, bitrate, recursive, into_subfolder, recycle):
        """The "download again" route: reads every song, searches YouTube for
        the lossy ones, then opens FormatReviewWindow - nothing changes until
        it's confirmed there."""
        if yt_dlp is None:
            self.log("NOTE: yt-dlp isn't installed, so songs can only be converted, not downloaded again.")
        self.format_scan_cancel = threading.Event()
        self.convert_button.configure(state="disabled")
        self.convert_cancel_button.configure(state="normal")
        self.convert_count_label.configure(text="Scanning...")
        self.status_line.configure(text="Reading tags...")

        def progress(text):
            try:
                self.after(0, lambda: self.status_line.configure(text=text))
            except (tk.TclError, RuntimeError):
                pass

        def run():
            try:
                items = scan_formats(folder, source, recursive, into_subfolder, progress, self.format_scan_cancel)
                self.after(0, lambda: self.format_scan_finished(folder, items, bitrate, into_subfolder, recycle))
            except Exception as e:
                SESSION_LOG.exception(log_source_name(self), type(e), e, e.__traceback__)
                self.after(0, lambda err=e: (self.convert_cancel_button.configure(state="disabled"),
                                             self.conversion_scan_failed(err)))

        threading.Thread(target=run, daemon=True).start()

    def format_scan_finished(self, folder, items, bitrate, into_subfolder, recycle):
        self.convert_button.configure(state="normal")
        self.convert_cancel_button.configure(state="disabled")
        self.convert_count_label.configure(text="")
        counts = {}
        for it in items:
            counts[it["action"]] = counts.get(it["action"], 0) + 1
        stopped = " (stopped - some songs weren't searched)" if self.format_scan_cancel.is_set() else ""
        summary = (f"{len(items)} not m4a: {counts.get(UNIFY_DOWNLOAD, 0)} to download, "
                   f"{counts.get(UNIFY_CONVERT, 0)} to convert, {counts.get(UNIFY_SKIP, 0)} skipped" + stopped)
        self.log(f"\nChecked {folder} for m4a: {summary}.")
        if not items:
            messagebox.showinfo("Nothing to convert", f"No songs to make m4a found in:\n{folder}")
        else:
            FormatReviewWindow(self, folder, items, self.log, bitrate, into_subfolder, recycle)

    def scan_for_conversion(self, folder, source, target, bitrate, recursive, into_subfolder, recycle):
        """Background: finds the files, estimates sizes and total length,
        then asks for confirmation on the Tk thread."""
        try:
            files = find_convertible_files(folder, source, target, recursive)
            existing = sum(1 for f in files if converted_path(f, folder, target, into_subfolder).exists())
            size_before = sum(f.stat().st_size for f in files)
            estimates = [estimate_converted_size(f, target, bitrate) for f in files]
            size_after = sum(e for e in estimates if e)
            total_seconds = sum(length for length in (_audio_info(f)[0] for f in files) if length)
        except Exception as e:
            self.after(0, lambda err=e: self.conversion_scan_failed(err))
            return
        self.after(0, lambda: self.confirm_conversion(
            files, folder, source, target, bitrate, into_subfolder, recycle, size_before, size_after, existing,
            total_seconds))

    def conversion_scan_failed(self, err):
        self.convert_button.configure(state="normal")
        self.convert_count_label.configure(text="")
        messagebox.showerror("Error", str(err))

    def confirm_conversion(self, files, folder, source, target, bitrate, into_subfolder, recycle,
                           size_before, size_after, existing, total_seconds):
        self.convert_count_label.configure(text="")
        if not files:
            self.convert_button.configure(state="normal")
            which = "audio" if source == "Any format" else source
            messagebox.showinfo("Nothing to convert", f"No {which} files (other than {target}) found in:\n{folder}")
            return

        warning, _ = conversion_warning(source, target, bitrate)
        where = f"a \"{CONVERTED_FOLDER_PREFIX}{target})\" subfolder" if into_subfolder else "next to the originals"
        target_desc = f"{target} {bitrate}kbps" if target in ("mp3", "m4a") else target
        skip_note = (f" ({existing} already have a converted copy and will be skipped - existing files are never "
                     f"overwritten)" if existing else "")
        lines = [
            f"Convert {len(files)} file(s) to {target_desc}, saved in {where}?{skip_note}",
            "",
            f"Current size: {format_size(size_before)}  ->  estimated after: ~{format_size(size_after)} "
            f"(new copies{'' if recycle else ', on top of the originals'})",
            *cd_fit_summary(total_seconds, size_after, target),
            "",
            warning,
            "",
            "DJ software will see the converted files as new tracks - cue points, beat grids and playlists "
            "stay with the old files.",
        ]
        if recycle:
            lines += ["", f"Originals will be moved to the {TRASH_NAME} after each one converts successfully "
                          "(you can restore them from there)."]
        if not messagebox.askyesno("Convert audio files?", "\n".join(lines), icon="warning"):
            self.convert_button.configure(state="normal")
            return

        self.log(f"\nConverting {len(files)} file(s) from {folder} to {target_desc} "
                 f"({CONVERT_WORKERS} at a time)...")
        for line in cd_fit_summary(total_seconds, size_after, target):
            self.log(f"  {line}")
        self.convert_progress.configure(maximum=len(files), value=0)
        self.convert_count_label.configure(text=f"0/{len(files)}")
        self.convert_cancel_button.configure(state="normal")
        self.converter = AudioConverter(self, files, folder, target, bitrate, into_subfolder, recycle,
                                        self.log, self.on_conversion_progress, self.on_conversion_finished)
        self.converter.start()

    def on_conversion_progress(self, done, total):
        self.convert_progress.configure(value=done)
        self.convert_count_label.configure(text=f"{done}/{total}")

    def cancel_conversion(self):
        if self.format_scan_cancel is not None and not self.format_scan_cancel.is_set():
            self.format_scan_cancel.set()  # the review window still opens, with the songs searched so far
            self.convert_cancel_button.configure(state="disabled")
        if self.converter:
            self.converter.cancel()
            self.convert_cancel_button.configure(state="disabled")
            self.log("Cancelling conversion - files in progress are discarded, originals are untouched.")

    def on_conversion_finished(self, summary):
        self.converter = None
        self.convert_button.configure(state="normal")
        self.convert_cancel_button.configure(state="disabled")
        lines = [f"{summary['converted']} converted"]
        if summary["converted"]:
            lines[0] += (f" ({format_size(summary['bytes_before'])} of originals -> "
                         f"{format_size(summary['bytes_after'])} of copies)")
        if summary["existing"]:
            lines.append(f"{summary['existing']} skipped - a converted copy already existed")
        if summary["failed"]:
            lines.append(f"{summary['failed']} failed - see the log")
        if summary["cancelled"]:
            lines.append(f"{summary['cancelled']} cancelled")
        if summary["recycled"]:
            lines.append(f"{summary['recycled']} original(s) moved to the {TRASH_NAME}")
        if summary["recycle_failed"]:
            lines.append(f"{summary['recycle_failed']} original(s) couldn't be moved and were kept")
        self.log("Conversion finished: " + "; ".join(lines))
        messagebox.showinfo("Conversion finished", "\n".join(lines))


# ---------- Tab 5: Settings ----------

class SettingsTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)

        appearance = make_section(self, "Appearance", "Changes the whole program - including any open review "
                                                      "windows - and is remembered next time.", pady=(12, 0))
        radio_row = tk.Frame(appearance); style_frame(radio_row)
        radio_row.pack(fill="x")
        self.theme_var = tk.StringVar(value=THEME_NAME)
        for text, value in (("Dark", "dark"), ("Light", "light")):
            ThemedRadiobutton(radio_row, text=text, variable=self.theme_var, value=value,
                              command=self.on_theme_change).pack(side="left", padx=(0, 16))
        accent_row = tk.Frame(appearance); style_frame(accent_row)
        accent_row.pack(fill="x", pady=(8, 0))
        l_accent = tk.Label(accent_row, text="Accent colour:"); style_label(l_accent)
        l_accent.pack(side="left", padx=(0, 10))
        self.swatches = {}
        for name, color in ACCENT_PRESETS.items():
            swatch = tk.Label(accent_row, width=2, bg=color, cursor="hand2", highlightthickness=2)
            swatch.pack(side="left", padx=(0, 6))
            swatch.bind("<Button-1>", lambda e, c=color: self.choose_accent(c))
            self.swatches[color] = swatch
        ThemedButton(accent_row, text="Custom...", command=self.choose_custom_accent).pack(side="left", padx=(6, 0))
        register(self.refresh_swatches)

        cookies = make_section(self, "YouTube cookies",
                               "Some YouTube videos are age-restricted and can't be downloaded anonymously. Picking a "
                               "browser here lets TrackMatch borrow that browser's YouTube login to get past the age "
                               "check. Leave on \"None\" unless you actually hit that error - most videos don't need "
                               "this.\n\nYou need to be logged into YouTube in that browser. On some systems the "
                               "browser has to be closed for its cookies to be readable.")
        cookie_row = tk.Frame(cookies); style_frame(cookie_row)
        cookie_row.pack(fill="x")
        l_cookie = tk.Label(cookie_row, text="Use cookies from:"); style_label(l_cookie)
        l_cookie.pack(side="left", padx=(0, 8))
        current_display = next((opt for opt in COOKIE_BROWSER_OPTIONS if opt.lower() == COOKIE_BROWSER), "None")
        self.cookie_var = tk.StringVar(value=current_display)
        ThemedOptionMenu(cookie_row, self.cookie_var, *COOKIE_BROWSER_OPTIONS,
                         command=self.on_cookie_change).pack(side="left")

        tagging = make_section(self, "Tagging")
        large_row = tk.Frame(tagging); style_frame(large_row)
        large_row.pack(fill="x")
        self.large_var = tk.BooleanVar(value=LARGE_LIBRARY_MODE)
        ThemedCheckbutton(large_row, text="Large library mode", variable=self.large_var,
                          command=lambda: set_large_library_mode(self.large_var.get())).pack(side="left")
        info_button(large_row, f"For big folders (hundreds or thousands of songs): looks up {LOOKUP_WORKERS} songs "
                               f"at a time, saves each answer as it goes so a scan can be stopped and resumed, and "
                               f"reviews results in a compact table.\n\nWhen it's off you'll get a warning before "
                               f"scanning {LARGE_FOLDER_WARNING}+ songs.", "Large library mode"
                    ).pack(side="left", padx=(4, 0))
        match_row = tk.Frame(tagging); style_frame(match_row)
        match_row.pack(fill="x", pady=(8, 0))
        l_match = tk.Label(match_row, text="Minimum match:"); style_label(l_match)
        l_match.pack(side="left", padx=(0, 10))
        self.min_match_var = tk.DoubleVar(value=METADATA_MIN_SCORE)
        for value in MIN_MATCH_CHOICES:
            ThemedRadiobutton(match_row, text=f"{value:.0%}", variable=self.min_match_var, value=value,
                              command=lambda: set_min_match(self.min_match_var.get())).pack(side="left", padx=(0, 10))
        info_button(match_row, "How close a Beatport / Deezer / iTunes result must be to count as a confident match. "
                               "Confident matches are pre-selected in the review (and used automatically when "
                               "tagging downloads); anything lower is left as \"Don't change\" for you to check.\n\n"
                               "75% still requires the artist and version to match - higher settings also hold back "
                               "small spelling or length differences.", "Minimum match").pack(side="left")
        cover_row = tk.Frame(tagging); style_frame(cover_row)
        cover_row.pack(fill="x", pady=(8, 0))
        self.cover_fallback_var = tk.BooleanVar(value=COVER_FALLBACK)
        ThemedCheckbutton(cover_row, text="Find missing covers on iTunes", variable=self.cover_fallback_var,
                          command=lambda: set_cover_fallback(self.cover_fallback_var.get())).pack(side="left")
        info_button(cover_row, "When the matched source's cover can't be downloaded (Deezer's image server blocks "
                               "some connections), look the same album up on iTunes and use its artwork.\n\n"
                               "iTunes allows only about 20 lookups a minute, so with lots of songs this can slow "
                               "applying down (each album is only looked up once per session). Turn it off to skip "
                               "those covers - the songs keep whatever cover they already have.",
                    "Find missing covers").pack(side="left", padx=(4, 0))
        saved_row = tk.Frame(tagging); style_frame(saved_row)
        saved_row.pack(fill="x", pady=(8, 0))
        self.saved_label = tk.Label(saved_row, text="Saved lookups")
        style_label(self.saved_label)
        self.saved_label.pack(side="left", padx=(0, 10))
        ThemedButton(saved_row, text="Clear", command=self.clear_saved_lookups).pack(side="left")
        info_button(saved_row, "Large library mode keeps each song's lookup answer until you clear it, so "
                               "re-scanning a folder is instant. Clear them to make every song be looked up fresh. "
                               "(Songs you've changed since are always looked up again.)", "Saved lookups"
                    ).pack(side="left", padx=(4, 0))
        self.bind("<Map>", lambda e: self.refresh_saved_label())
        SETTING_LISTENERS.append(self.on_setting_changed)

        logs = make_section(self, "Logs")
        logs_row = tk.Frame(logs); style_frame(logs_row)
        logs_row.pack(fill="x")
        self.show_logs_var = tk.BooleanVar(value=SHOW_LOG_PANELS)
        ThemedCheckbutton(logs_row, text="Show log panels in each tab", variable=self.show_logs_var,
                          command=lambda: set_show_log_panels(self.show_logs_var.get())).pack(side="left")
        info_button(logs_row, f"Every session is logged to its own file in TrackMatch's \"logs\" folder - "
                              f"one file each time you open the app, kept for {LOG_RETENTION_DAYS} days - whether "
                              f"or not the panels are shown. Handy if something goes wrong.", "Logs"
                    ).pack(side="left", padx=(4, 0))
        ThemedButton(logs_row, text="Open logs folder", command=self.open_logs_folder).pack(side="right")

        about = tk.Frame(self); style_frame(about)
        about.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        credit = tk.Label(about, text=f"TrackMatch v{APP_VERSION}  \u00b7  Made by Basil-Black")
        style_label(credit, subtle=True)
        credit.pack(side="left")
        for text, url in (("GitHub profile", PROFILE_URL), ("TrackMatch on GitHub", REPO_URL)):
            make_link(about, text, url).pack(side="right", padx=(12, 0))

    def open_logs_folder(self):
        folder = SESSION_LOG.logs_folder()
        if not folder:
            messagebox.showerror("No logs folder", "TrackMatch couldn't create a logs folder next to the app "
                                                   "or in your AppData folder.")
            return
        try:
            open_folder(folder)
        except Exception as e:
            messagebox.showerror("Couldn't open folder", f"{folder}\n\n{e}")

    def on_setting_changed(self):
        self.large_var.set(LARGE_LIBRARY_MODE)
        self.min_match_var.set(METADATA_MIN_SCORE)

    def refresh_saved_label(self):
        def load():
            count, size = LOOKUP_CACHE.stats()
            text = f"Saved lookups: {count} song(s), {format_size(size)}" if count else "Saved lookups: none"
            try:
                self.after(0, lambda: self.saved_label.configure(text=text))
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=load, daemon=True).start()

    def clear_saved_lookups(self):
        count, _ = LOOKUP_CACHE.stats()
        if not count:
            messagebox.showinfo("Nothing to clear", "There are no saved lookups.")
            return
        if messagebox.askyesno("Clear saved lookups",
                               f"Forget the saved lookup answers for {count} song(s)? The next scan will look "
                               f"them all up again. Your music files aren't touched."):
            LOOKUP_CACHE.clear()
            self.refresh_saved_label()

    def refresh_swatches(self):
        """Ring around the chosen accent swatch (and none on the others)."""
        for color, swatch in self.swatches.items():
            chosen = color == ACCENT_COLOR
            swatch.configure(highlightbackground=THEME["FG"] if chosen else THEME["BG"],
                             highlightcolor=THEME["FG"] if chosen else THEME["BG"])

    def choose_accent(self, color):
        set_accent_color(color)

    def choose_custom_accent(self):
        _rgb, color = colorchooser.askcolor(color=ACCENT_COLOR, title="Choose an accent colour", parent=self)
        if color:
            set_accent_color(color)

    def on_theme_change(self):
        set_theme(self.theme_var.get())

    def on_cookie_change(self, selected):
        set_cookie_browser(selected)


# ---------- Main window ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        load_settings()  # picks up a previously-saved theme choice, if any
        setup_fonts(self)
        install_error_logging(self)
        SESSION_LOG.write("App", f"TrackMatch v{APP_VERSION} session started - "
                                 f"{'exe' if getattr(sys, 'frozen', False) else 'script'}, {sys.platform}, "
                                 f"Python {sys.version.split()[0]}, "
                                 f"yt-dlp {yt_dlp.version.__version__ if yt_dlp else 'not installed'}")

        self.title(f"TrackMatch v{APP_VERSION}")
        self.geometry("720x850")
        self.resizable(True, True)
        style_window(self)
        retheme_all()
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        download_tab = DownloadTab(notebook)
        playlist_tab = PlaylistTab(notebook)
        metadata_tab = MetadataEditorTab(notebook)
        convert_tab = ConvertAudioTab(notebook)
        settings_tab = SettingsTab(notebook)

        notebook.add(download_tab, text="Download")
        notebook.add(playlist_tab, text="Playlist")
        notebook.add(metadata_tab, text="Tagging")
        notebook.add(convert_tab, text="Convert")
        notebook.add(settings_tab, text="Settings")


if __name__ == "__main__":
    app = App()
    app.mainloop()