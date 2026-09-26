#!/usr/bin/env python3
"""
TrackMatch

Four tabs:

  1) Download Missing Tracks
     Pick a playlist CSV (from exportify.net) and a folder to check against.
     Anything not already in that folder can be fetched from YouTube via
     yt-dlp and saved into a chosen destination folder (defaults to the
     same folder you checked against).

  2) Create Playlist
     Pick a playlist CSV and a music folder, then either write an .m3u8
     (paths relative to the playlist's own location, for portability to
     USB drives / Rekordbox / other devices) or copy the matched files
     into a folder. Every track is shown for review - worst matches
     first - before anything is written.

  3) Metadata Editor
     Pick a folder of music and search Beatport for cover art, genre,
     label, year, BPM and key for each file. Works best with EDM/electronic tracks, since
     Beatport's catalog is dance-music focused - other genres will mostly
     come back "None found". Nothing is written until you approve it per
     file in the review window.

  4) Settings
     Toggle dark/light mode for the whole program, including any open
     review/results windows. Saved to a small settings file next to this
     script so your choice persists between runs.

Requirements:
    pip install mutagen yt-dlp requests beautifulsoup4

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
import sys
import threading
import tkinter as tk
import unicodedata
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import APIC, TALB, TBPM, TCON, TDRC, TIT2, TKEY, TPE1, TPUB, TSRC
    from mutagen.flac import Picture
    from mutagen.mp4 import MP4Cover, MP4FreeForm
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


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".aac"}

YT_FORMATS = ["mp3", "m4a", "opus", "flac"]
YT_BITRATES = ["320", "256", "192", "128", "96"]  # kbps; ignored by lossless formats like flac

# Set this to an .ico (preferred) or .png file sitting next to this script
# to change the window/title-bar icon. Leave as None to use the default.
ICON_PATH = "icon.ico" #"my_icon.ico"

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

THEMED_WIDGETS = []  # list of zero-arg callables that reapply current THEME colors


def register(apply_fn):
    """Runs apply_fn now and remembers it so retheme_all() can re-run it later.
    Widgets that get destroyed are simply skipped (TclError) rather than
    cleaned out of the list - harmless, just a few dead closures over time."""
    apply_fn()
    THEMED_WIDGETS.append(apply_fn)


def retheme_all():
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TProgressbar", background=THEME["ACCENT"], troughcolor=THEME["TROUGH"],
                     bordercolor=THEME["BG"], lightcolor=THEME["ACCENT"], darkcolor=THEME["ACCENT"])
    style.configure("TNotebook", background=THEME["BG"], borderwidth=0)
    style.configure("TNotebook.Tab", background=THEME["BUTTON_BG"], foreground=THEME["FG"],
                     padding=(14, 8), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", THEME["BG"])],
              foreground=[("selected", THEME["FG"])])

    for apply_fn in list(THEMED_WIDGETS):
        try:
            apply_fn()
        except tk.TclError:
            pass  # widget was destroyed - nothing to do


def set_theme(name: str):
    global THEME_NAME
    preset = DARK_THEME if name == "dark" else LIGHT_THEME
    THEME.clear()
    THEME.update(preset)
    THEME_NAME = name
    retheme_all()
    save_settings()


def set_cookie_browser(browser_display_name: str):
    global COOKIE_BROWSER
    COOKIE_BROWSER = browser_display_name.lower()
    save_settings()


def settings_file_path() -> Path:
    return app_base_dir() / "trackmatch_settings.json"


def load_settings():
    global THEME_NAME, COOKIE_BROWSER
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
    except Exception:
        pass  # no settings file yet, or it's malformed - just use the defaults


def save_settings():
    try:
        settings_file_path().write_text(
            json.dumps({"theme": THEME_NAME, "cookie_browser": COOKIE_BROWSER}), encoding="utf-8"
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


def style_entry(widget):
    register(lambda: widget.configure(
        bg=THEME["ENTRY_BG"], fg=THEME["FG"], insertbackground=THEME["FG"], relief="flat",
        highlightthickness=1, highlightbackground=THEME["ROW_BORDER"], highlightcolor=THEME["ACCENT"]
    ))


def style_button(widget):
    register(lambda: widget.configure(
        bg=THEME["BUTTON_BG"], fg=THEME["FG"], activebackground=THEME["BUTTON_ACTIVE_BG"],
        activeforeground=THEME["FG"], relief="flat", highlightthickness=0
    ))


def style_frame(widget):
    register(lambda: widget.configure(bg=THEME["BG"]))


def style_canvas(widget):
    register(lambda: widget.configure(bg=THEME["BG"], highlightthickness=0))


def style_row_frame(widget):
    """A bordered row (used in review lists) - background + border color."""
    register(lambda: widget.configure(bg=THEME["BG"], highlightbackground=THEME["ROW_BORDER"]))


def style_checkbutton(widget):
    register(lambda: widget.configure(
        bg=THEME["BG"], fg=THEME["FG"], selectcolor=THEME["ENTRY_BG"], activebackground=THEME["BG"],
        activeforeground=THEME["FG"], highlightthickness=0
    ))


def style_radiobutton(widget, highlighted=False):
    def apply():
        fg = THEME["FOUND_FG"] if highlighted else THEME["FG"]
        widget.configure(bg=THEME["BG"], fg=fg, selectcolor=THEME["ENTRY_BG"], activebackground=THEME["BG"],
                          activeforeground=fg, highlightthickness=0)
    register(apply)


def style_scrolledtext(widget):
    register(lambda: widget.configure(bg=THEME["ENTRY_BG"], fg=THEME["FG"], insertbackground=THEME["FG"],
                                       relief="flat"))


def style_optionmenu(widget, menu):
    def apply():
        widget.configure(bg=THEME["BUTTON_BG"], fg=THEME["FG"], activebackground=THEME["BUTTON_ACTIVE_BG"],
                          activeforeground=THEME["FG"], highlightthickness=0, relief="flat")
        menu.configure(bg=THEME["ENTRY_BG"], fg=THEME["FG"])
    register(apply)


def add_credit_footer(window):
    """Adds a small 'Made by Basil-Black' strip pinned to the bottom of a
    window. Must be called BEFORE any other widgets are packed into the
    window, since pack() reserves this bottom sliver first and lets
    everything else fill the remaining space above it."""
    footer = tk.Label(window, text="Made by Basil-Black", font=("", 8))
    style_label(footer, subtle=True)
    footer.pack(side="bottom", pady=(2, 6))
    return footer


def app_base_dir() -> Path:
    """Folder the script/exe lives in - works whether running as a .py or a
    PyInstaller-built .exe (where sys.executable is the exe's own path)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


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
    path = Path(icon_path)
    if not path.is_absolute():
        path = app_base_dir() / path
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


def index_music_folder(music_dir: Path, log):
    log(f"Scanning {music_dir} for audio files...")
    all_files = [p for p in music_dir.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS]
    log(f"Found {len(all_files)} audio files. Reading metadata...")

    index = []
    for i, filepath in enumerate(all_files, 1):
        title, artist = read_local_metadata(filepath)
        index.append({
            "path": filepath,
            "title": title,
            "artist": artist,
            "norm_title": normalize(title),
            "artist_set": split_artists(artist),
        })
        if i % 200 == 0:
            log(f"  ...{i}/{len(all_files)}")

    return index


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

    best_candidate, best_score = None, 0.0
    for candidate in index:
        score = score_match(norm_title, target_artist_set, candidate)
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
            rel_path = os.path.relpath(Path(m["path"]).resolve(), base_dir)
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

    def warning(self, msg):
        self.log(f"  [yt-dlp] {msg}")

    def error(self, msg):
        self.log(f"  [yt-dlp] ERROR: {msg}")


def _cookie_opts():
    """Returns {'cookiesfrombrowser': (browser,)} if a browser is set in Settings,
    else {}. Lets yt-dlp authenticate as you for age-restricted videos."""
    if COOKIE_BROWSER and COOKIE_BROWSER != "none":
        return {"cookiesfrombrowser": (COOKIE_BROWSER,)}
    return {}


def search_youtube(query: str, max_results: int = 5):
    """Returns up to max_results dicts: {'title','uploader','duration','duration_seconds','url'}.
    Does not download anything."""
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    ydl_opts.update(_cookie_opts())
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)

    results = []
    for entry in info.get("entries", []) or []:
        if not entry:
            continue
        results.append({
            "title": entry.get("title", "Unknown title"),
            "uploader": entry.get("uploader", "Unknown channel"),
            "duration": format_duration(entry.get("duration")),
            "duration_seconds": entry.get("duration"),
            "url": entry.get("webpage_url") or entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
        })
    return results


def download_youtube_audio(video_url: str, dest_dir: Path, fmt: str, bitrate: str, desired_name: str, log):
    """Downloads + extracts audio via yt-dlp/ffmpeg into dest_dir, saved as
    "<desired_name>.<fmt>" (i.e. named after the playlist's Artist - Title,
    not the YouTube video's own title). Embeds metadata + thumbnail as cover
    art. Returns the final file Path."""
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(desired_name)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_dir / f"{safe_name}.%(ext)s"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": fmt, "preferredquality": bitrate},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logger": _YtDlpLogger(log),
    }
    ydl_opts.update(_cookie_opts())

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(video_url, download=True)

    final_path = dest_dir / f"{safe_name}.{fmt}"
    if not final_path.exists():
        # Fallback in case yt-dlp/ffmpeg produced a slightly different name
        candidates = sorted(dest_dir.glob(f"{safe_name}*.{fmt}"))
        if candidates:
            final_path = candidates[0]
        else:
            raise RuntimeError(f"Download finished but couldn't locate the output file (expected {final_path.name}).")

    return final_path


def write_local_tags(file_path: Path, title: str, artist: str, album: str, genre: str, log):
    """Overwrites the Title/Artist/Album/Genre tags on a downloaded file with
    the playlist's own CSV data, since yt-dlp's FFmpegMetadata step pulls its
    tags from YouTube's video title / channel name, which is often wrong
    (e.g. channel name instead of the real artist)."""
    if MutagenFile is None:
        return
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        audio["title"] = [title]
        if artist:
            audio["artist"] = [artist]
        if album:
            audio["album"] = [album]
        if genre:
            audio["genre"] = [genre]
        audio.save()
    except Exception as e:
        log(f"  Could not update tags on \"{file_path.name}\": {e}")


# ---------- Beatport metadata lookup ----------
# Beatport has no public API, so this reads the __NEXT_DATA__ JSON blob
# embedded in their search page's HTML (the same technique used by the
# standalone Beatport Cover Finder tool). Catalog is dance/electronic
# focused - other genres will mostly come back with no results.

BEATPORT_SEARCH_URL = "https://www.beatport.com/search/tracks?q={query}"
BEATPORT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
BEATPORT_COVER_SIZE = 500  # px, square - Beatport's CDN resizes the release artwork to this

# A Beatport result has to score at least this well against the file's own
# title/artist to be pre-selected in the review window. Anything lower is
# shown for manual picking but defaults to "don't change", so Apply All
# never writes another song's cover/genre into a file.
BEATPORT_MIN_SCORE = 0.75

# Beatport tags same-named artists with a disambiguator, e.g. "FISHER (OZ)" -
# stripped before comparing against local tags, which never have it.
ARTIST_DISAMBIGUATOR_RE = re.compile(r"\s*\([^()]*\)")


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
        if name and name not in artist_names:  # Beatport sometimes lists the same artist twice
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

    date = (t.get("publish_date") or t.get("release_date") or "")[:10]
    bpm = t.get("bpm")
    track_id = t.get("track_id")

    return {
        "title": full_title or "Unknown title",
        "base_title": base_title,
        "mix_name": mix_name,
        "artists": ", ".join(artist_names) or "Unknown artist",
        "artist_list": artist_names or ["Unknown artist"],
        "label": _beatport_name(t.get("label"), "label_name"),
        "genre": genre,
        "release": _beatport_name(release_val, "release_name"),
        "date": date,
        "year": date[:4],
        "bpm": str(round(bpm)) if isinstance(bpm, (int, float)) and bpm > 0 else "",
        "key": beatport_key_to_tag(t.get("key_name") or ""),
        "isrc": (t.get("isrc") or "").strip(),
        "image_url": image_url,
        "url": f"https://www.beatport.com/track/-/{track_id}" if track_id else "",
    }


def _fetch_beatport_tracks(query: str, dbg):
    url = BEATPORT_SEARCH_URL.format(query=requests.utils.quote(query))
    resp = requests.get(url, headers=BEATPORT_HEADERS, timeout=15)
    dbg(f"GET {url} -> HTTP {resp.status_code}, {len(resp.text)} bytes")
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


def score_beatport_result(artist: str, title: str, result) -> float:
    """0..1 similarity between a local file's artist/title and a Beatport
    result, using the same title+artist weighting as playlist matching,
    plus a small bonus/penalty for the mix name so 'X (Extended Mix)' on
    disk prefers Beatport's Extended over its Radio Edit or a remix."""
    candidate = {
        "norm_title": normalize(result["base_title"]),
        "artist_set": split_artists(ARTIST_DISAMBIGUATOR_RE.sub("", result["artists"])),
    }
    target_artists = split_artists(ARTIST_DISAMBIGUATOR_RE.sub("", artist or ""))
    if target_artists:
        score = score_match(normalize(title), target_artists, candidate)
    else:
        # Nothing to compare artists against (untagged file with no
        # "Artist - Title" filename). Title alone can't tell apart different
        # songs with the same name, so cap it below BEATPORT_MIN_SCORE -
        # the user has to pick the right one by hand.
        score = min(best_ratio(normalize(title), candidate["norm_title"]), BEATPORT_MIN_SCORE - 0.05)

    remix_words = ("remix", "rework", "bootleg", "vip", "edit", "dub")
    local_mix = " ".join(m.strip("() ").lower() for m in MIX_SUFFIX_RE.findall(title or ""))
    remote_mix = result["mix_name"].lower()
    local_is_remix = any(w in local_mix for w in remix_words)
    remote_is_remix = any(w in remote_mix for w in remix_words)
    if remote_is_remix and not local_is_remix:
        # File doesn't mention a remix/edit, so one is probably the wrong version.
        score -= 0.1
    if local_mix and remote_mix:
        score += 0.1 * (difflib.SequenceMatcher(None, local_mix, remote_mix).ratio() - 0.5)
    # Deliberately not clamped to 1.0 here - search_beatport sorts on the raw
    # value so a matching mix name still breaks ties between perfect matches.
    return max(0.0, score)


def search_beatport(artist: str, title: str, max_results: int = 5, log=None):
    """Searches Beatport and returns up to max_results result dicts, ranked
    best-first by how well each matches the given artist/title (each dict
    has a 'score' 0..1 - see score_beatport_result - plus 'title',
    'artists', 'label', 'genre', 'release', 'date', 'year', 'bpm', 'key',
    'isrc', 'image_url', 'url'). Beatport's own ordering is by popularity,
    not closeness, so its first hit is often a different track entirely.
    Returns [] if nothing was found or the page couldn't be parsed - callers
    should treat that as "None found", not an error."""
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
    for r in results:
        r["score"] = score_beatport_result(artist, title, r)
    results.sort(key=lambda r: r["score"], reverse=True)
    for r in results:
        r["score"] = min(1.0, r["score"])
    if results:
        dbg(f"{len(track_list)} results, best match {results[0]['score']:.2f}: "
            f"{results[0]['artists']} - {results[0]['title']}")
    return results[:max_results]


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
    for field, keys in key_map.items():
        value = fields.get(field)
        if value:
            for k in keys:
                tags[k] = _as_list(value)


def embed_full_metadata(file_path: Path, fields: dict, image_bytes, image_mime: str):
    """Writes the text tags in `fields` (title, artist, album, genre, label,
    date, year, bpm, key, isrc - empty/missing values leave that tag alone;
    artist is a list, saved as one tag value per artist)
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
        freeform_map = {"label": "LABEL", "key": "initialkey", "isrc": "ISRC"}
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


def download_image(url: str):
    """Returns (bytes, mime_type) or (None, None) on failure or if the
    response isn't a JPEG/PNG image."""
    if requests is None or not url:
        return None, None
    try:
        resp = requests.get(url, headers=BEATPORT_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception:
        return None, None
    mime = sniff_image_mime(resp.content)
    if not mime:
        return None, None
    return resp.content, mime


# ---------- Small shared GUI helpers ----------

def browse_file_into(entry, filetypes, title):
    path = filedialog.askopenfilename(title=title, filetypes=filetypes)
    if path:
        entry.delete(0, tk.END)
        entry.insert(0, path)


def browse_folder_into(entry, title):
    path = filedialog.askdirectory(title=title)
    if path:
        entry.delete(0, tk.END)
        entry.insert(0, path)


def make_path_row(parent, label_text, browse_fn, pad):
    """Builds a Label + Entry + Browse... row, returns the Entry widget."""
    l = tk.Label(parent, text=label_text)
    style_label(l)
    l.pack(anchor="w", **pad)
    row = tk.Frame(parent)
    style_frame(row)
    row.pack(fill="x", **pad)
    entry = tk.Entry(row)
    style_entry(entry)
    entry.pack(side="left", fill="x", expand=True)
    btn = tk.Button(row, text="Browse...", command=lambda: browse_fn(entry))
    style_button(btn)
    btn.pack(side="left", padx=(6, 0))
    return entry


def make_exportify_note(parent, pad):
    note = tk.Label(
        parent,
        text="Export your playlist at exportify.net (log in with Spotify, click Export), "
             "then point this tool at the downloaded CSV below.",
        justify="left", wraplength=640
    )
    style_label(note, subtle=True)
    note.pack(anchor="w", **pad)


def make_format_bitrate_row(parent, pad, label_text="Download format:"):
    """Builds the format + bitrate OptionMenu row used on both tabs.
    Returns (format_var, bitrate_var)."""
    row = tk.Frame(parent); style_frame(row)
    row.pack(fill="x", padx=10, pady=(10, 0))
    l1 = tk.Label(row, text=label_text); style_label(l1)
    l1.pack(side="left", padx=(0, 10))

    format_var = tk.StringVar(value="mp3")
    format_menu = tk.OptionMenu(row, format_var, *YT_FORMATS)
    style_optionmenu(format_menu, format_menu["menu"])
    format_menu.pack(side="left", padx=(0, 16))

    l2 = tk.Label(row, text="Bitrate (kbps):"); style_label(l2)
    l2.pack(side="left", padx=(0, 10))

    bitrate_var = tk.StringVar(value="320")
    bitrate_menu = tk.OptionMenu(row, bitrate_var, *YT_BITRATES)
    style_optionmenu(bitrate_menu, bitrate_menu["menu"])
    bitrate_menu.pack(side="left")

    return format_var, bitrate_var


# ---------- Tab 1: Download Missing Tracks ----------

class DownloadTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)
        pad = {"padx": 10, "pady": 6}

        make_exportify_note(self, pad)

        self.csv_entry = make_path_row(
            self, "Playlist CSV file:",
            lambda e: browse_file_into(e, [("CSV files", "*.csv"), ("All files", "*.*")], "Select playlist CSV"),
            pad
        )

        self.check_dir_entry = make_path_row(
            self, "Folder to check against:",
            lambda e: browse_folder_into(e, "Select the folder to check for existing files"),
            pad
        )

        # Destination
        dest_label = tk.Label(self, text="Download destination:")
        style_label(dest_label)
        dest_label.pack(anchor="w", **pad)

        self.same_folder_var = tk.BooleanVar(value=True)
        same_chk = tk.Checkbutton(
            self, text="Same as the folder I'm checking against", variable=self.same_folder_var,
            command=self.toggle_same_folder
        )
        style_checkbutton(same_chk)
        same_chk.pack(anchor="w", padx=10)

        dest_row = tk.Frame(self); style_frame(dest_row)
        dest_row.pack(fill="x", **pad)
        self.dest_entry = tk.Entry(dest_row, state="disabled")
        style_entry(self.dest_entry)
        self.dest_entry.pack(side="left", fill="x", expand=True)
        dest_btn = tk.Button(dest_row, text="Browse...",
                              command=lambda: browse_folder_into(self.dest_entry, "Select download destination folder"))
        style_button(dest_btn)
        dest_btn.pack(side="left", padx=(6, 0))

        # YouTube format/bitrate
        self.yt_format_var, self.yt_bitrate_var = make_format_bitrate_row(self, pad)

        # Run
        run_frame = tk.Frame(self); style_frame(run_frame)
        run_frame.pack(fill="x", **pad)
        self.run_button = tk.Button(run_frame, text="Check & Review", command=self.run_clicked, width=15)
        style_button(self.run_button)
        self.run_button.pack(side="left")
        self.progress = ttk.Progressbar(run_frame, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))

        # Log
        log_label = tk.Label(self, text="Log:"); style_label(log_label)
        log_label.pack(anchor="w", **pad)
        self.log_box = scrolledtext.ScrolledText(self, height=14, state="disabled", wrap="word")
        style_scrolledtext(self.log_box)
        self.log_box.pack(fill="both", expand=True, **pad)

        if not MutagenFile:
            self.log("WARNING: 'mutagen' is not installed. Run: pip install mutagen")
        if not yt_dlp:
            self.log("NOTE: 'yt-dlp' is not installed - downloading won't work until you "
                      "run: pip install yt-dlp (also requires ffmpeg on your PATH)")

    def toggle_same_folder(self):
        if self.same_folder_var.get():
            self.dest_entry.configure(state="disabled")
        else:
            self.dest_entry.configure(state="normal")

    def log(self, message: str):
        def append():
            self.log_box.configure(state="normal")
            self.log_box.insert(tk.END, message + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state="disabled")
        self.after(0, append)

    def run_clicked(self):
        csv_path = self.csv_entry.get().strip()
        check_dir = self.check_dir_entry.get().strip()
        same_folder = self.same_folder_var.get()
        dest_dir = check_dir if same_folder else self.dest_entry.get().strip()

        if not csv_path or not Path(csv_path).is_file():
            messagebox.showerror("Missing info", "Choose a valid playlist CSV file.")
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
        self.progress.start(10)
        yt_format = self.yt_format_var.get()
        yt_bitrate = self.yt_bitrate_var.get()
        thread = threading.Thread(
            target=self.worker, args=(csv_path, check_dir, dest_dir, yt_format, yt_bitrate), daemon=True
        )
        thread.start()

    def worker(self, csv_path, check_dir, dest_dir, yt_format, yt_bitrate):
        try:
            self.log("Reading playlist CSV...")
            tracks = read_playlist_csv(Path(csv_path), self.log)

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

            self.after(0, lambda: DownloadReviewWindow(
                self, items, Path(dest_dir), yt_format, yt_bitrate, self.log
            ))

        except Exception as e:
            self.log(f"ERROR: {e}")
            self.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.run_button.configure(state="normal"))


class DownloadReviewWindow(tk.Toplevel):
    """Lists every playlist track with its status against the checked folder
    (found / missing, with score). For each one you can search YouTube and
    download it straight into the destination folder. Nothing here builds
    a playlist - it's purely about filling gaps in your library."""

    FOUND_THRESHOLD = 0.75

    def __init__(self, parent, items, dest_dir, yt_format, yt_bitrate, log):
        super().__init__(parent)
        self.title("TrackMatch - Download Review")
        self.geometry("800x600")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)
        add_credit_footer(self)

        self.items = items
        self.dest_dir = dest_dir
        self.yt_format = yt_format
        self.yt_bitrate = yt_bitrate
        self.log = log
        self.status_labels = {}

        found = sum(1 for it in items if it["score"] >= self.FOUND_THRESHOLD)
        header = tk.Label(
            self,
            text=f"{found}/{len(items)} already look present (score >= {self.FOUND_THRESHOLD:.2f}). "
                 f"Missing/uncertain tracks are listed first. Use Search YouTube to fetch any of them "
                 f"into:\n{dest_dir}",
            justify="left", wraplength=770
        )
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 0))

        canvas = tk.Canvas(self, borderwidth=0)
        style_canvas(canvas)
        scroll_frame = tk.Frame(canvas); style_frame(scroll_frame)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=10, pady=10)
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        for i, item in enumerate(items):
            track, candidate, score = item["track"], item["candidate"], item["score"]
            row = tk.Frame(scroll_frame, relief="groove", borderwidth=1, highlightthickness=1)
            style_row_frame(row)
            row.pack(fill="x", pady=4, padx=2)

            title_label = tk.Label(row, text=f"{track['artist']} - {track['title']}", justify="left", anchor="w")
            style_label(title_label)
            title_label.pack(fill="x", padx=6, pady=(4, 0))

            if candidate and score >= self.FOUND_THRESHOLD:
                status_text = f"Found: \"{candidate['path'].name}\" (score {score:.2f})"
                status_kind = "found"
            elif candidate:
                status_text = f"Possible match: \"{candidate['path'].name}\" (score {score:.2f}) - check carefully"
                status_kind = "missing"
            else:
                status_text = "Not found in the folder"
                status_kind = "missing"
            status_label = tk.Label(row, text=status_text, justify="left", anchor="w")
            style_status_label(status_label, status_kind)
            status_label.pack(fill="x", padx=6, pady=(0, 2))

            control_frame = tk.Frame(row); style_frame(control_frame)
            control_frame.pack(fill="x", padx=6, pady=(0, 6))
            yt_btn = tk.Button(control_frame, text="Search YouTube", command=lambda idx=i: self.search_youtube_for(idx))
            style_button(yt_btn)
            yt_btn.pack(side="left")

            progress_label = tk.Label(row, text="", justify="left", anchor="w")
            style_label(progress_label, subtle=True)
            progress_label.pack(fill="x", padx=6, pady=(0, 4))
            self.status_labels[i] = progress_label

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        close_btn = tk.Button(bottom, text="Close", command=self.destroy, width=12)
        style_button(close_btn)
        close_btn.pack(side="right")

    def set_row_status(self, idx, text):
        def update():
            if idx in self.status_labels:
                self.status_labels[idx].configure(text=text)
        self.after(0, update)

    def search_youtube_for(self, idx):
        track = self.items[idx]["track"]
        query = f"{track['artist']} {track['title']}".strip()
        self.set_row_status(idx, "Searching YouTube...")

        def do_search():
            try:
                results = search_youtube(query, max_results=5)
                self.set_row_status(idx, "" if results else "No results found.")
                if results:
                    self.after(0, lambda: YouTubeResultsWindow(
                        self, idx, track, results, self.yt_format, self.yt_bitrate,
                        self.dest_dir, self.on_downloaded, self.log
                    ))
            except Exception as e:
                self.set_row_status(idx, f"Search failed: {e}")

        threading.Thread(target=do_search, daemon=True).start()

    def on_downloaded(self, idx, file_path):
        self.set_row_status(idx, f"Downloaded: {file_path.name}")


# ---------- Tab 2: Create Playlist ----------

class PlaylistTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)
        pad = {"padx": 10, "pady": 6}

        make_exportify_note(self, pad)

        self.csv_entry = make_path_row(
            self, "Playlist CSV file:",
            lambda e: browse_file_into(e, [("CSV files", "*.csv"), ("All files", "*.*")], "Select playlist CSV"),
            pad
        )

        self.music_dir_entry = make_path_row(
            self, "Music folder to search:",
            lambda e: browse_folder_into(e, "Select your music folder"),
            pad
        )

        # Output mode
        mode_row = tk.Frame(self); style_frame(mode_row)
        mode_row.pack(fill="x", padx=10, pady=(6, 0))
        l_mode = tk.Label(mode_row, text="Output type:"); style_label(l_mode)
        l_mode.pack(side="left", padx=(0, 10))

        self.mode_var = tk.StringVar(value="m3u")
        radio_m3u = tk.Radiobutton(
            mode_row, text="Create M3U playlist", variable=self.mode_var, value="m3u",
            command=self.update_output_mode_ui
        )
        radio_copy = tk.Radiobutton(
            mode_row, text="Copy files into a folder", variable=self.mode_var, value="copy",
            command=self.update_output_mode_ui
        )
        for rb in (radio_m3u, radio_copy):
            style_radiobutton(rb)
            rb.pack(side="left", padx=(0, 12))

        # Output location
        self.output_label = tk.Label(self, text="Save playlist as:")
        style_label(self.output_label)
        self.output_label.pack(anchor="w", **pad)
        out_frame = tk.Frame(self); style_frame(out_frame)
        out_frame.pack(fill="x", **pad)
        self.output_entry = tk.Entry(out_frame); style_entry(self.output_entry)
        self.output_entry.pack(side="left", fill="x", expand=True)
        b3 = tk.Button(out_frame, text="Browse...", command=self.browse_output); style_button(b3)
        b3.pack(side="left", padx=(6, 0))

        self.rel_note = tk.Label(
            self,
            text="Playlist paths are saved relative to this file's location, so keep the "
                 "playlist and music folder together (e.g. both on the same USB).",
            justify="left", wraplength=640
        )
        style_label(self.rel_note, subtle=True)
        self.rel_note.pack(anchor="w", padx=10, pady=(0, 6))

        # YouTube fallback options (used when a track can't be found locally, from the review window)
        self.yt_format_var, self.yt_bitrate_var = make_format_bitrate_row(
            self, pad, label_text="YouTube fallback format:"
        )

        yt_note = tk.Label(
            self,
            text="Used in the review window if you choose to fetch a missing track from YouTube.",
            justify="left"
        )
        style_label(yt_note, subtle=True)
        yt_note.pack(anchor="w", padx=10, pady=(6, 6))

        # Run
        run_frame = tk.Frame(self); style_frame(run_frame)
        run_frame.pack(fill="x", **pad)
        self.run_button = tk.Button(run_frame, text="Run", command=self.run_clicked, width=15)
        style_button(self.run_button)
        self.run_button.pack(side="left")
        self.progress = ttk.Progressbar(run_frame, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))

        # Log
        log_label = tk.Label(self, text="Log:"); style_label(log_label)
        log_label.pack(anchor="w", **pad)
        self.log_box = scrolledtext.ScrolledText(self, height=14, state="disabled", wrap="word")
        style_scrolledtext(self.log_box)
        self.log_box.pack(fill="both", expand=True, **pad)

        if not MutagenFile:
            self.log("WARNING: 'mutagen' is not installed. Run: pip install mutagen")
        if not yt_dlp:
            self.log("NOTE: 'yt-dlp' is not installed - the YouTube fallback option won't work until you "
                      "run: pip install yt-dlp (also requires ffmpeg on your PATH)")

    def update_output_mode_ui(self):
        if self.mode_var.get() == "m3u":
            self.output_label.configure(text="Save playlist as:")
            self.rel_note.configure(
                text="Playlist paths are saved relative to this file's location, so keep the "
                     "playlist and music folder together (e.g. both on the same USB)."
            )
        else:
            self.output_label.configure(text="Copy files into folder:")
            self.rel_note.configure(
                text="Matched songs will be copied (not moved) into this folder. "
                     "Your original music folder is left untouched."
            )

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

    def log(self, message: str):
        def append():
            self.log_box.configure(state="normal")
            self.log_box.insert(tk.END, message + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state="disabled")
        self.after(0, append)

    def run_clicked(self):
        csv_path = self.csv_entry.get().strip()
        music_dir = self.music_dir_entry.get().strip()
        output_path = self.output_entry.get().strip()
        mode = self.mode_var.get()

        if not csv_path or not Path(csv_path).is_file():
            messagebox.showerror("Missing info", "Choose a valid playlist CSV file.")
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
        self.progress.start(10)
        thread = threading.Thread(
            target=self.worker, args=(csv_path, music_dir, output_path, mode), daemon=True
        )
        thread.start()

    def worker(self, csv_path, music_dir, output_path, mode):
        try:
            output = Path(output_path)
            if mode == "m3u" and output.suffix.lower() not in (".m3u", ".m3u8"):
                output = output.with_suffix(".m3u8")

            self.log("Reading playlist CSV...")
            tracks = read_playlist_csv(Path(csv_path), self.log)

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
            yt_format = self.yt_format_var.get()
            yt_bitrate = self.yt_bitrate_var.get()
            self.after(0, lambda: self.open_review(items, output, music_dir, len(tracks), mode, yt_format, yt_bitrate))

        except Exception as e:
            self.log(f"ERROR: {e}")
            self.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.run_button.configure(state="normal"))

    def open_review(self, items, output, music_dir, total_tracks, mode, yt_format, yt_bitrate):
        ReviewWindow(self, items, output, music_dir, total_tracks, self.finish_and_write, self.log,
                     mode, yt_format, yt_bitrate)

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


class ReviewWindow(tk.Toplevel):
    """Shows every track with its best-guess local match pre-filled, worst
    scores first, so nothing is written to disk until you've confirmed
    (or corrected) each one. Browse to fix a wrong guess, Search YouTube
    to fetch one that isn't in your library, or Skip to leave it out."""

    def __init__(self, parent, items, output, music_dir, total_tracks, on_finish, log, mode,
                 yt_format, yt_bitrate):
        super().__init__(parent)
        self.title("TrackMatch - Review Tracks")
        self.geometry("800x600")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)
        add_credit_footer(self)

        self.items = items                # every track: [{"track","candidate","score"}, ...]
        self.output = output
        self.music_dir = music_dir
        self.total_tracks = total_tracks
        self.on_finish = on_finish
        self.log = log
        self.mode = mode
        self.yt_format = yt_format
        self.yt_bitrate = yt_bitrate

        # Pre-fill each row with its best guess (empty if nothing was found at all)
        self.chosen_paths = [
            tk.StringVar(value=str(it["candidate"]["path"]) if it["candidate"] else "")
            for it in items
        ]
        self.skip_flags = [tk.BooleanVar(value=False) for _ in items]
        self.status_labels = {}  # idx -> Label, for showing search/download progress per row

        header = tk.Label(
            self,
            text=f"Reviewing all {len(items)} tracks, worst matches first. Confirm, fix, or skip each one - "
                 f"nothing is saved until you click Finish.",
            justify="left", wraplength=770
        )
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 0))

        canvas = tk.Canvas(self, borderwidth=0)
        style_canvas(canvas)
        scroll_frame = tk.Frame(canvas); style_frame(scroll_frame)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=10, pady=10)
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        for i, item in enumerate(items):
            track, candidate, score = item["track"], item["candidate"], item["score"]
            row = tk.Frame(scroll_frame, relief="groove", borderwidth=1, highlightthickness=1)
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
            path_entry = tk.Entry(control_frame, textvariable=self.chosen_paths[i])
            style_entry(path_entry)
            path_entry.pack(side="left", fill="x", expand=True)
            browse_btn = tk.Button(control_frame, text="Browse...", command=lambda idx=i: self.browse_for(idx))
            style_button(browse_btn)
            browse_btn.pack(side="left", padx=(6, 6))
            yt_btn = tk.Button(control_frame, text="Search YouTube", command=lambda idx=i: self.search_youtube_for(idx))
            style_button(yt_btn)
            yt_btn.pack(side="left", padx=(0, 6))
            skip_chk = tk.Checkbutton(control_frame, text="Skip this track", variable=self.skip_flags[i])
            style_checkbutton(skip_chk)
            skip_chk.pack(side="left")

            status_label = tk.Label(row, text="", justify="left", anchor="w")
            style_label(status_label, subtle=True)
            status_label.pack(fill="x", padx=6, pady=(0, 4))
            self.status_labels[i] = status_label

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        finish_btn = tk.Button(bottom, text="Finish & Save Playlist", command=self.finish, width=20)
        style_button(finish_btn)
        finish_btn.pack(side="right")

    def set_row_status(self, idx, text):
        def update():
            if idx in self.status_labels:
                self.status_labels[idx].configure(text=text)
        self.after(0, update)

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
                results = search_youtube(query, max_results=5)
                self.set_row_status(idx, "" if results else "No results found.")
                if results:
                    self.after(0, lambda: YouTubeResultsWindow(
                        self, idx, track, results, self.yt_format, self.yt_bitrate,
                        self.music_dir, self.on_youtube_downloaded, self.log
                    ))
            except Exception as e:
                self.set_row_status(idx, f"Search failed: {e}")

        threading.Thread(target=do_search, daemon=True).start()

    def on_youtube_downloaded(self, idx, file_path):
        self.chosen_paths[idx].set(str(file_path))
        self.skip_flags[idx].set(False)
        self.set_row_status(idx, f"Downloaded: {file_path.name}")

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
    right one, then downloads/extracts the audio via yt-dlp. The saved file
    is named after the playlist's own Artist - Title, not the YouTube
    video's title. The search query can be edited and re-run, and more
    results can be pulled in without closing the window."""

    RESULTS_PER_PAGE = 5

    def __init__(self, parent, row_idx, track, results, yt_format, yt_bitrate, dest_dir, on_downloaded, log):
        super().__init__(parent)
        self.title(f"YouTube results: {track['artist']} - {track['title']}")
        self.geometry("640x520")
        style_window(self)
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)
        add_credit_footer(self)

        self.row_idx = row_idx
        self.track = track
        self.results = results
        self.yt_format = yt_format
        self.yt_bitrate = yt_bitrate
        self.dest_dir = dest_dir
        self.on_downloaded = on_downloaded
        self.log = log
        self.result_count = max(len(results), self.RESULTS_PER_PAGE)

        playlist_seconds = (track["duration_ms"] / 1000) if track.get("duration_ms") else None
        self.playlist_seconds = playlist_seconds
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
        query_entry = tk.Entry(query_row, textvariable=self.query_var)
        style_entry(query_entry)
        query_entry.pack(side="left", fill="x", expand=True)
        query_entry.bind("<Return>", lambda e: self.new_search())
        search_btn = tk.Button(query_row, text="Search", command=self.new_search)
        style_button(search_btn)
        search_btn.pack(side="left", padx=(6, 0))

        # Scrollable results list
        canvas = tk.Canvas(self, borderwidth=0)
        style_canvas(canvas)
        self.options_frame = tk.Frame(canvas); style_frame(self.options_frame)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 0))
        canvas.create_window((0, 0), window=self.options_frame, anchor="nw")
        self.options_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.selected_idx = tk.IntVar(value=0)
        self.render_results()

        more_row = tk.Frame(self); style_frame(more_row)
        more_row.pack(fill="x", padx=10, pady=(4, 0))
        self.more_btn = tk.Button(more_row, text="Show 5 More Results", command=self.show_more)
        style_button(self.more_btn)
        self.more_btn.pack(side="left")

        desired_name = sanitize_filename(f"{track['artist']} - {track['title']}")
        self.status_label = tk.Label(
            self,
            text=f"Format: {yt_format}  |  Bitrate: {yt_bitrate}kbps  |  "
                 f"Will save as: {desired_name}.{yt_format}",
            justify="left", wraplength=620
        )
        style_label(self.status_label, subtle=True)
        self.status_label.pack(anchor="w", padx=10, pady=(8, 0))

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        self.download_btn = tk.Button(bottom, text="Download Selected", command=self.download_selected, width=18)
        style_button(self.download_btn)
        self.download_btn.pack(side="right")
        cancel_btn = tk.Button(bottom, text="Cancel", command=self.destroy, width=10)
        style_button(cancel_btn)
        cancel_btn.pack(side="right", padx=(0, 6))

    def render_results(self):
        """(Re)draws the radio button list from self.results, highlighting
        whichever result's duration is closest to the playlist track's own
        length - purely visual, doesn't affect the default selection."""
        for child in self.options_frame.winfo_children():
            child.destroy()

        closest_idx = None
        if self.playlist_seconds is not None:
            best_diff = None
            for i, r in enumerate(self.results):
                secs = r.get("duration_seconds")
                if secs is None:
                    continue
                diff = abs(secs - self.playlist_seconds)
                if best_diff is None or diff < best_diff:
                    best_diff, closest_idx = diff, i

        self.selected_idx.set(0)
        for i, r in enumerate(self.results):
            text = f"{r['title']}\n{r['uploader']} \u2022 {r['duration']}"
            is_closest = (i == closest_idx)
            if is_closest:
                text += "  (closest length match)"
            rb = tk.Radiobutton(self.options_frame, text=text, variable=self.selected_idx, value=i,
                                 justify="left", anchor="w", wraplength=580)
            style_radiobutton(rb, highlighted=is_closest)
            rb.pack(fill="x", pady=4, anchor="w")

    def set_status(self, text):
        def update():
            self.status_label.configure(text=text)
        self.after(0, update)

    def set_controls_state(self, state):
        def update():
            self.download_btn.configure(state=state)
            self.more_btn.configure(state=state)
        self.after(0, update)

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
        than before. yt-dlp's search isn't paginated, so this just re-asks
        for a bigger batch and re-renders the (now longer) full list."""
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
        url = self.results[idx]["url"]
        self.download_btn.configure(state="disabled")
        self.status_label.configure(text="Downloading...")

        def do_download():
            try:
                dest_dir = Path(self.dest_dir)
                desired_name = f"{self.track['artist']} - {self.track['title']}"
                file_path = download_youtube_audio(
                    url, dest_dir, self.yt_format, self.yt_bitrate, desired_name, self.log
                )
                write_local_tags(
                    file_path,
                    title=self.track["title"],
                    artist=self.track["artist"],
                    album=self.track.get("album", ""),
                    genre=self.track.get("genre", ""),
                    log=self.log,
                )
                self.log(f"  Downloaded: {file_path.name}")
                self.after(0, lambda: self.on_downloaded(self.row_idx, file_path))
                self.after(0, self.destroy)
            except Exception as e:
                self.log(f"  Download failed: {e}")
                self.after(0, lambda err=e: self.status_label.configure(text=f"Failed: {err}"))
                self.after(0, lambda: self.download_btn.configure(state="normal"))

        threading.Thread(target=do_download, daemon=True).start()


# ---------- Tab 3: Metadata Editor ----------

class MetadataEditorTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)
        pad = {"padx": 10, "pady": 6}

        heading = tk.Label(self, text="Beatport Cover & Genre Lookup", font=("", 11, "bold"))
        style_label(heading)
        heading.pack(anchor="w", padx=10, pady=(10, 4))

        note = tk.Label(
            self,
            text="Works best with EDM/electronic tracks - Beatport's catalog is dance-music "
                 "focused, so other genres will often come back \"None found\". Nothing is written "
                 "to your files until you approve it per track in the review window.",
            justify="left", wraplength=640
        )
        style_label(note, subtle=True)
        note.pack(anchor="w", padx=10, pady=(0, 10))

        self.folder_entry = make_path_row(
            self, "Music folder to scan:",
            lambda e: browse_folder_into(e, "Select a folder of music to look up"),
            pad
        )

        run_frame = tk.Frame(self); style_frame(run_frame)
        run_frame.pack(fill="x", **pad)
        self.run_button = tk.Button(run_frame, text="Scan & Search Beatport", command=self.run_clicked, width=22)
        style_button(self.run_button)
        self.run_button.pack(side="left")
        self.progress = ttk.Progressbar(run_frame, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))

        log_label = tk.Label(self, text="Log:"); style_label(log_label)
        log_label.pack(anchor="w", **pad)
        self.log_box = scrolledtext.ScrolledText(self, height=16, state="disabled", wrap="word")
        style_scrolledtext(self.log_box)
        self.log_box.pack(fill="both", expand=True, **pad)

        if not MutagenFile:
            self.log("WARNING: 'mutagen' is not installed. Run: pip install mutagen")
        if requests is None or BeautifulSoup is None:
            self.log("NOTE: 'requests' and/or 'beautifulsoup4' are not installed - Beatport lookup "
                      "won't work until you run: pip install requests beautifulsoup4")

    def log(self, message: str):
        def append():
            self.log_box.configure(state="normal")
            self.log_box.insert(tk.END, message + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state="disabled")
        self.after(0, append)

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
        self.progress.start(10)
        thread = threading.Thread(target=self.worker, args=(folder,), daemon=True)
        thread.start()

    def worker(self, folder):
        try:
            folder_path = Path(folder)
            self.log(f"Scanning {folder_path} for audio files...")
            files = [p for p in folder_path.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS]
            self.log(f"Found {len(files)} audio files.")

            items = []
            for i, filepath in enumerate(files, 1):
                existing = read_existing_tags(filepath)
                title, artist = existing["title"], existing["artist"]
                self.log(f"[{i}/{len(files)}] Searching Beatport: {artist} - {title}")
                try:
                    results = search_beatport(artist, title, log=self.log)
                except Exception as e:
                    self.log(f"  Search failed: {e}")
                    results = []
                items.append({
                    "path": filepath, "title": title, "artist": artist,
                    "existing_album": existing["album"], "existing_genre": existing["genre"],
                    "results": results,
                })

            found = sum(1 for it in items if it["results"])
            self.log(f"\nDone searching. {found}/{len(items)} tracks got at least one Beatport match.")
            self.after(0, lambda: MetadataReviewWindow(self, items, self.log))

        except Exception as e:
            self.log(f"ERROR: {e}")
            self.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.run_button.configure(state="normal"))


class MetadataReviewWindow(tk.Toplevel):
    """Lists every scanned file with its Beatport matches, ranked by how
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
        add_credit_footer(self)

        self.items = items
        self.log = log
        self.status_labels = {}
        self.apply_buttons = {}
        self.detail_labels = {}
        self.detail_kinds = {}    # idx -> 'found'/'missing', read by the detail label's theme closure
        self.choice_vars = {}     # idx -> StringVar holding the picker's label
        self.choice_lookup = {}   # idx -> {picker label: result dict}

        found = sum(1 for it in items if it["results"])
        confident = sum(1 for it in items if it["results"] and it["results"][0]["score"] >= BEATPORT_MIN_SCORE)
        header = tk.Label(
            self,
            text=f"{found}/{len(items)} tracks found Beatport results - {confident} confident matches are "
                 f"pre-selected. Weak matches (flagged below) default to \"{self.SKIP_CHOICE}\": pick the "
                 f"right result from the list if it's there. Apply writes Title (incl. mix), Artist, Album, "
                 f"Genre, Label, Year, BPM, Key, ISRC + cover art - Beatport's data is used where available, "
                 f"otherwise the file's existing tag is kept. Nothing happens until you click Apply (or Apply All).",
            justify="left", wraplength=840
        )
        style_label(header, subtle=True)
        header.pack(anchor="w", padx=10, pady=(10, 0))

        canvas = tk.Canvas(self, borderwidth=0)
        style_canvas(canvas)
        scroll_frame = tk.Frame(canvas); style_frame(scroll_frame)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=10, pady=10)
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

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
                lookup[f"{n}. {r['artists']} - {r['title']}  ({r['score']:.0%})"] = r
            self.choice_lookup[i] = lookup

            best = item["results"][0] if item["results"] else None
            if best and best["score"] >= BEATPORT_MIN_SCORE:
                initial = next(iter(lookup))
            else:
                initial = self.SKIP_CHOICE
            var = tk.StringVar(value=initial)
            self.choice_vars[i] = var

            apply_btn = tk.Button(control_frame, text="Apply", command=lambda idx=i: self.apply_item(idx))
            style_button(apply_btn)
            apply_btn.pack(side="left")
            self.apply_buttons[i] = apply_btn

            if lookup:
                picker = tk.OptionMenu(control_frame, var, *lookup.keys(), self.SKIP_CHOICE,
                                       command=lambda _value, idx=i: self.refresh_row(idx))
                style_optionmenu(picker, picker["menu"])
                picker.pack(side="left", fill="x", expand=True, padx=(6, 0))

            status_label = tk.Label(row, text="", justify="left", anchor="w")
            style_label(status_label, subtle=True)
            status_label.pack(fill="x", padx=6, pady=(0, 4))
            self.status_labels[i] = status_label

            self.refresh_row(i)

        bottom = tk.Frame(self); style_frame(bottom)
        bottom.pack(fill="x", padx=10, pady=10)
        close_btn = tk.Button(bottom, text="Close", command=self.destroy, width=12)
        style_button(close_btn)
        close_btn.pack(side="right")
        self.apply_all_btn = tk.Button(bottom, text="Apply All Selected", command=self.apply_all)
        style_button(self.apply_all_btn)
        self.apply_all_btn.pack(side="right", padx=(0, 6))
        if found == 0:
            self.apply_all_btn.configure(state="disabled")

    def selected_result(self, idx):
        """The Beatport result currently picked for row idx, or None for
        "Don't change". Must be called on the Tk main thread."""
        return self.choice_lookup[idx].get(self.choice_vars[idx].get())

    def refresh_row(self, idx):
        """Updates the detail line and Apply button to match the picker."""
        item = self.items[idx]
        chosen = self.selected_result(idx)
        label = self.detail_labels[idx]

        if not item["results"]:
            label.configure(text="None found on Beatport")
            self.set_detail_kind(idx, "missing")
            self.apply_buttons[idx].configure(state="disabled")
            return

        if chosen is None:
            best = item["results"][0]
            if best["score"] < BEATPORT_MIN_SCORE:
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
            f"Will write: \"{', '.join(fields['artist'])} - {fields['title']}\"",
            f"Album: {fields['album'] or '(none)'}",
            f"Genre: {fields['genre'] or '(none)'}",
        ]
        for name, key in (("Label", "label"), ("Year", "year"), ("BPM", "bpm"), ("Key", "key")):
            if fields.get(key):
                parts.append(f"{name}: {fields[key]}")
        parts.append("cover art" if chosen.get("image_url") else "no cover art available")
        label.configure(text="  |  ".join(parts))
        self.set_detail_kind(idx, "found" if chosen["score"] >= BEATPORT_MIN_SCORE else "missing")
        self.apply_buttons[idx].configure(state="normal")

    def set_detail_kind(self, idx, kind):
        self.detail_kinds[idx] = kind
        self.detail_labels[idx].configure(fg=THEME["FOUND_FG" if kind == "found" else "MISSING_FG"])

    def resolve_fields(self, item, best):
        """Beatport's value wins for each field when it has one; otherwise
        keep whatever was already in the file, so applying a match never
        blanks out good existing data. Label/date/BPM/key/ISRC are
        Beatport-only - an empty value just leaves that tag untouched."""
        return {
            "title": best.get("title") or item["title"],
            "artist": best.get("artist_list") or ([item["artist"]] if item["artist"] else []),
            "album": best.get("release") or item.get("existing_album", ""),
            "genre": best.get("genre") or item.get("existing_genre", ""),
            "label": best.get("label", ""),
            "date": best.get("date", ""),
            "year": best.get("year", ""),
            "bpm": best.get("bpm", ""),
            "key": best.get("key", ""),
            "isrc": best.get("isrc", ""),
        }

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
            fields = self.resolve_fields(item, chosen)
            image_bytes, image_mime = download_image(chosen.get("image_url", ""))
            if chosen.get("image_url") and not image_bytes:
                self.log(f"  Couldn't download cover art for \"{item['path'].name}\" - writing text tags only.")
            cover_written = embed_full_metadata(item["path"], fields, image_bytes, image_mime)
            if cover_written:
                self.set_row_status(idx, "Applied - full metadata + cover art updated.")
            elif image_bytes:
                self.set_row_status(idx, f"Applied - metadata updated (cover art isn't supported "
                                         f"for {item['path'].suffix} files).")
            else:
                self.set_row_status(idx, "Applied - metadata updated (no cover image was available).")
            self.log(f"  Updated: {item['path'].name}")
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
            messagebox.showinfo("Nothing to apply", "No rows have a Beatport match selected.", parent=self)
            return

        self.apply_all_btn.configure(state="disabled")
        for i, _ in jobs:
            self.apply_buttons[i].configure(state="disabled")
        self.log(f"\nApplying Beatport metadata to {len(jobs)} tracks...")

        def do_all():
            applied, failed = 0, 0
            for i, chosen in jobs:
                if self.write_item(i, chosen):
                    applied += 1
                else:
                    failed += 1
            self.log(f"\nApply All complete: {applied} updated, {failed} failed.")
            self.after(0, lambda: self.apply_all_btn.configure(state="normal"))
            self.after(0, lambda: messagebox.showinfo(
                "Apply All complete", f"{applied} track(s) updated.\n{failed} failed.", parent=self
            ))

        threading.Thread(target=do_all, daemon=True).start()


# ---------- Tab 4: Settings ----------

class SettingsTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        style_frame(self)
        pad = {"padx": 10, "pady": 6}

        heading = tk.Label(self, text="Appearance", font=("", 11, "bold"))
        style_label(heading)
        heading.pack(anchor="w", padx=10, pady=(16, 4))

        note = tk.Label(
            self,
            text="Switches the whole program - including any open review or YouTube results "
                 "windows - and is remembered next time you open TrackMatch.",
            justify="left", wraplength=600
        )
        style_label(note, subtle=True)
        note.pack(anchor="w", padx=10, pady=(0, 10))

        self.theme_var = tk.StringVar(value=THEME_NAME)
        radio_row = tk.Frame(self); style_frame(radio_row)
        radio_row.pack(anchor="w", padx=10)

        dark_rb = tk.Radiobutton(radio_row, text="Dark mode", variable=self.theme_var, value="dark",
                                  command=self.on_theme_change)
        light_rb = tk.Radiobutton(radio_row, text="Light mode", variable=self.theme_var, value="light",
                                   command=self.on_theme_change)
        for rb in (dark_rb, light_rb):
            style_radiobutton(rb)
            rb.pack(side="left", padx=(0, 16))

        cookie_heading = tk.Label(self, text="YouTube Cookies", font=("", 11, "bold"))
        style_label(cookie_heading)
        cookie_heading.pack(anchor="w", padx=10, pady=(24, 4))

        cookie_note = tk.Label(
            self,
            text="Some YouTube videos are age-restricted and can't be downloaded anonymously. "
                 "Picking a browser here lets yt-dlp borrow that browser's YouTube login to get "
                 "past the age check. Leave on \"None\" unless you actually hit that error - "
                 "most videos don't need this.\n\n"
                 "Requires being logged into YouTube in that browser. On some systems the browser "
                 "may need to be closed for its cookies to be readable.",
            justify="left", wraplength=600
        )
        style_label(cookie_note, subtle=True)
        cookie_note.pack(anchor="w", padx=10, pady=(0, 10))

        cookie_row = tk.Frame(self); style_frame(cookie_row)
        cookie_row.pack(anchor="w", padx=10)
        l_cookie = tk.Label(cookie_row, text="Use cookies from:")
        style_label(l_cookie)
        l_cookie.pack(side="left", padx=(0, 10))

        current_display = next(
            (opt for opt in COOKIE_BROWSER_OPTIONS if opt.lower() == COOKIE_BROWSER), "None"
        )
        self.cookie_var = tk.StringVar(value=current_display)
        cookie_menu = tk.OptionMenu(cookie_row, self.cookie_var, *COOKIE_BROWSER_OPTIONS,
                                     command=self.on_cookie_change)
        style_optionmenu(cookie_menu, cookie_menu["menu"])
        cookie_menu.pack(side="left")

    def on_theme_change(self):
        set_theme(self.theme_var.get())

    def on_cookie_change(self, selected):
        set_cookie_browser(selected)


# ---------- Main window ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        load_settings()  # picks up a previously-saved theme choice, if any

        self.title("TrackMatch")
        self.geometry("700x780")
        self.resizable(True, True)
        style_window(self)
        retheme_all()
        set_window_icon(self, ICON_PATH)
        enable_dark_titlebar(self)
        add_credit_footer(self)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        download_tab = DownloadTab(notebook)
        playlist_tab = PlaylistTab(notebook)
        metadata_tab = MetadataEditorTab(notebook)
        settings_tab = SettingsTab(notebook)

        notebook.add(download_tab, text="Download Missing Tracks")
        notebook.add(playlist_tab, text="Create Playlist")
        notebook.add(metadata_tab, text="Metadata Editor")
        notebook.add(settings_tab, text="Settings")


if __name__ == "__main__":
    app = App()
    app.mainloop()