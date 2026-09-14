#!/usr/bin/env python3
"""
TrackMatch

Three tabs:

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

  3) Settings
     Toggle dark/light mode for the whole program, including any open
     review/results windows. Saved to a small settings file next to this
     script so your choice persists between runs.

Requirements:
    pip install mutagen yt-dlp

yt-dlp also needs ffmpeg on your PATH for audio extraction/conversion.

Custom title bar icon:
    Put an .ico file (Windows) next to this script and set ICON_PATH below
    to its filename, e.g. ICON_PATH = "my_icon.ico".
"""

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
except ImportError:
    MutagenFile = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aac"}

YT_FORMATS = ["mp3", "m4a", "opus", "flac"]
YT_BITRATES = ["320", "256", "192", "128", "96"]  # kbps; ignored by lossless formats like flac

# Set this to an .ico (preferred) or .png file sitting next to this script
# to change the window/title-bar icon. Leave as None to use the default.
ICON_PATH = "icon.ico"

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
            self.after(0, lambda: messagebox.showerror("Error", str(e)))
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
            self.after(0, lambda: messagebox.showerror("Error", str(e)))
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
                self.after(0, lambda: self.status_label.configure(text=f"Failed: {e}"))
                self.after(0, lambda: self.download_btn.configure(state="normal"))

        threading.Thread(target=do_download, daemon=True).start()


# ---------- Tab 3: Settings ----------

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

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        download_tab = DownloadTab(notebook)
        playlist_tab = PlaylistTab(notebook)
        settings_tab = SettingsTab(notebook)

        notebook.add(download_tab, text="Download Missing Tracks")
        notebook.add(playlist_tab, text="Create Playlist")
        notebook.add(settings_tab, text="Settings")


if __name__ == "__main__":
    app = App()
    app.mainloop()