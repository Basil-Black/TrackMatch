# TrackMatch

A desktop app that matches a Spotify playlist against your local music library, fills gaps by downloading from YouTube, and builds a portable playlist, built with Rekordbox/USB DJ workflows in mind.

## What it does

**Tab 1 — Download Missing Tracks**
Check a playlist CSV against a folder of local files. Anything missing can be fetched from YouTube (via `yt-dlp`) straight into your library, tagged correctly with the playlist's own Title/Artist/Album/Genre data.

**Tab 2 — Create Playlist**
Match a playlist CSV against your music folder and either:
- Generate an `.m3u8` with paths relative to the playlist file itself (portable across USB drives / different computers), or
- Copy the matched files into a folder

Every track is shown for manual review, worst matches first, before anything is written to disk.

**Tab 3 — Settings**
Toggle dark/light mode for the whole app.

## Requirements

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/) on your system PATH

```bash
pip install -r requirements.txt
```

## Usage

1. Export your playlist as a CSV using [Exportify](https://exportify.net) (log in with Spotify, click Export)
2. Run the app:
```bash
   python spotify_playlist_to_m3u.py
```
3. Point it at your CSV and music folder, and go

## Building a standalone .exe

```bash
pyinstaller --onefile --windowed --icon=my_icon.ico --name "TrackMatch" spotify_playlist_to_m3u.py
```

## A note on YouTube downloads

This tool uses `yt-dlp` to fetch audio from YouTube for tracks missing from your local library. Downloading audio from YouTube may violate YouTube's Terms of Service, even for personal use — use at your own discretion.

## License

MIT (or your choice)
