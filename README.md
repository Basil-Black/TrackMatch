# TrackMatch

A desktop app for DJs and music collectors. Turn playlists from Spotify, Deezer, Apple Music or YouTube into a tidy local library: find the songs you already have, download the ones you don't, tag everything properly, convert formats and build playlists ready for Rekordbox or a USB stick.

Nothing is changed on your computer until you've reviewed it first.

## What it does

The app has five tabs.

### Download
Give it a playlist and your music folder. TrackMatch checks which songs you already have and lists the rest.
- **Download All Selected** picks the best YouTube match for each missing song automatically (by length, title, artist and official uploads), downloads a few at a time and shows live progress. Songs without a confident match are left for you to pick with **Search YouTube**.
- Formats: **m4a** (YouTube's original audio, no re-encoding, the default), **mp3** (re-encoded, for older players) or **opus**.
- Downloads can be tagged automatically with cover art, genre, label and year from Beatport, Deezer or iTunes.

### Playlist
Two ways to make playlists:
- **From a playlist**: matches every song in a playlist to the files in your music folder, lets you review every match (worst first), then saves an `.m3u8` playlist or copies the songs into a folder. Missing songs can be fetched from YouTube right there.
- **From your music library**: reads the tags already in your files and builds playlists by **genre, year, genre + year, artist or BPM range**, for example `2026 Tech House`, `2025 House`, `Other Genres 2024`. Pick your main genres and everything else goes into "Other Genres". Recent years can each get their own playlist while older songs are grouped by decade (`2010s`, `90s`, `80s`). Output is one `.m3u8` per playlist, or the songs copied into flat or nested folders.

### Tagging
Looks up every song in a folder and fills in **cover art, genre, label, year and ISRC**, plus **BPM and key** from Beatport.
- **Auto** tries Beatport first (electronic music), then Deezer, then iTunes. Or pick a single source.
- Every match is reviewed before anything is written. Weak or wrong-version matches (remixes, live versions, covers) are never applied automatically.
- **Large library mode** (in Settings) handles thousands of songs: 3 lookups at a time, progress saved as it goes so scans can be stopped and resumed, and a compact review table.
- Each tagged file gets a hidden tag recording where its metadata came from, so already-tagged songs can be skipped next time.
- **Fix file names** renames songs to "Artist - Title" from their tags and updates the playlists in the folder.

### Convert
Converts a folder of songs between **mp3, m4a, flac, wav and aiff**, keeping tags and cover art.
- **Use for** presets: Audio CD burning, MP3 CD for car / CD player, older CDJ / USB player, iPhone / iTunes, DJ software (aiff from wav/flac), archiving (flac).
- Clear quality notes (lossy to lossy loses a little quality, lossy to lossless gains nothing) and a confirmation showing the size before and after, and how many audio CDs or MP3 CDs the result fills.
- Originals are never overwritten, and can optionally be moved to the Recycle Bin / Trash after a successful conversion.
- **Converting to m4a**: low-bitrate mp3s (and other lossy files) can be **downloaded again from YouTube** instead of re-encoded. You review every song first and pick Download, Convert or Skip (or Convert All / Download All Found); flac/wav/aiff and .aac are always converted. Tags, cover art and file names are kept, and playlists in the folder are updated when the m4a is saved next to the original.

### Settings
Dark or light mode, a custom **accent colour**, YouTube cookies (for age-restricted videos), Large library mode, the **minimum match** needed for automatic tagging, and whether to show the log panels.

## Playlist sources

Paste a link, browse to a file, or paste a list of songs. No accounts or API keys needed.

| Source | Notes |
|---|---|
| Spotify playlist / album / song link | Playlist links include the first 100 songs. For longer playlists, select all songs in the Spotify app (Ctrl+A), copy (Ctrl+C) and use **Paste Songs** |
| Deezer playlist / album / song link | Full playlists |
| Apple Music playlist / album link | |
| YouTube / YouTube Music playlist link | Downloads use those exact videos |
| Pasted songs | Spotify or Deezer song links, or "Artist - Title" lines (numbering and mix timestamps are ignored) |
| CSV file | From [Exportify](https://exportify.net), TuneMyMusic or Soundiiz |
| .txt / .m3u / .m3u8 file | |

## Requirements

**New to this?** [BUILDING.md](BUILDING.md) walks through everything step by step for Windows, macOS and Linux, including installing Python and ffmpeg.

**Prebuilt app (Windows):** download the latest `TrackMatch-vX.X.X.zip` from the [Releases page](https://github.com/Basil-Black/TrackMatch/releases/latest), extract the zip and run `TrackMatch.exe`. Keep `icon.ico` and `icon.png` next to it. If Windows shows "Windows protected your PC", click **More info** > **Run anyway** (this appears for any app that isn't signed with a paid certificate).

**Android:** there's also an Android version - get the APK from [TrackMatch-Android](https://github.com/Basil-Black/TrackMatch-Android/releases/latest).

**ffmpeg** is needed for downloading and converting (matching and playlists work without it):
- Windows: `winget install ffmpeg`
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg` (or your distro's package manager)

After installing ffmpeg, restart TrackMatch (and your terminal, if running from source).

**Running from source (Windows, macOS, Linux):**
- Python 3.10 or newer, with Tkinter (included with the python.org installers; on Linux you may need `sudo apt install python3-tk`)
- Then:
  ```bash
  pip install -r requirements.txt
  python trackmatch.py
  ```

The app is built and tested on Windows. It is written to work on macOS and Linux too, but hasn't been tested there yet, so please report anything that looks wrong.

## Good to know

- **`.m3u8` playlists use paths relative to the playlist file.** Keep the playlist and your music on the same drive, in the same positions relative to each other. Rekordbox imports them via *File > Import > Import Playlist*.
- **Tag and convert before importing into Rekordbox.** Rekordbox reads tags on import, and a converted file is a new file to DJ software: cue points, beat grids and playlist entries stay with the old one.
- **Converting can't improve quality.** Downloads from YouTube are about 128 to 160 kbps; for tracks you'll play on big systems, buy lossless or 320 kbps copies.
- **Lookups rely on public web pages** for Beatport, Spotify and Apple Music. If one of those sites changes, that source may stop working until TrackMatch is updated. Deezer, iTunes and YouTube use their official public interfaces.

## Where TrackMatch keeps its files

Next to `TrackMatch.exe` (or `trackmatch.py`), so a TrackMatch folder is portable. The Mac app uses `~/Library/Application Support/TrackMatch` instead, and if the app's folder can't be written to, TrackMatch uses your system's usual per-user folder.
- `trackmatch_settings.json`: your settings
- `trackmatch_lookup_cache.jsonl`: saved lookups from Large library mode (clear them in Settings)
- `logs/`: one log file per session, kept for 30 days (open it from Settings). Useful when reporting a problem

## Building the app yourself

Full beginner steps for each system are in [BUILDING.md](BUILDING.md). The short version, from the TrackMatch folder:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pyinstaller          # Windows: .venv\Scripts\python
.venv/bin/python -m PyInstaller --onefile --windowed --icon=icon.ico --name TrackMatch trackmatch.py
```

The app appears in `dist/`. Put `icon.ico` and `icon.png` next to it so the window icon loads. PyInstaller builds for the system it runs on, so build on macOS or Linux to get an app for those.

## A note on YouTube downloads

This tool uses `yt-dlp` to fetch audio from YouTube for tracks missing from your local library. Downloading audio from YouTube may violate YouTube's Terms of Service, even for personal use, use at your own discretion.

## License

Copyright (C) 2026 Basil-Black. TrackMatch is free software, released under the [GNU General Public License v3.0](LICENSE): you can use, change and share it, as long as anything you share based on it stays under the same license.
