TRACKMATCH v1.0.0
=================

What this does
--------------
TrackMatch turns playlists from Spotify, Deezer, Apple Music or YouTube
into a tidy local music library. It finds the songs you already have,
downloads the ones you don't, tags everything properly, converts formats
and builds playlists ready for Rekordbox or a USB stick.

Nothing is changed on your computer until you've reviewed it first.

Tip: the small (i) buttons next to headings and options explain what
they do - click one to read it, click anywhere to close it.


GETTING STARTED
---------------
1. Install ffmpeg (needed for downloading and converting):
     Windows: winget install ffmpeg
     Mac:     brew install ffmpeg
     Linux:   sudo apt install ffmpeg
   Then restart TrackMatch so it picks it up.
2. Run TrackMatch.exe. Keep icon.ico and icon.png in the same folder.
3. Pick a tab below and go.


PLAYLISTS YOU CAN USE
---------------------
Anywhere TrackMatch asks for a playlist you can:
  - Paste a link:
      Spotify playlist, album or song (playlists: first 100 songs)
      Deezer playlist, album or song (full playlists)
      Apple Music playlist or album
      YouTube / YouTube Music playlist (downloads use those exact videos)
  - Click Paste Songs and paste:
      songs copied from the Spotify app (open the playlist, click a song,
      press Ctrl+A then Ctrl+C) - this works for playlists of any length
      Spotify or Deezer song links
      "Artist - Title" lines (numbering like "1." and timestamps like
      "03:15" are ignored)
  - Click Browse and pick a file:
      .csv (from exportify.net, TuneMyMusic or Soundiiz), .txt, .m3u, .m3u8

No accounts or logins are needed.


THE FIVE TABS
-------------

1) DOWNLOAD
   Fills the gaps in your music folder.
     - Playlist          -> any of the playlist sources above
     - Your music folder -> songs already in here won't be downloaded again
     - Save downloads into the same folder -> untick to choose another one
     - Format            -> m4a (YouTube's original audio, no re-encoding),
                            mp3 (re-encoded, for older players) or opus
     - Tag downloads using -> Auto / Beatport / Deezer / iTunes / Off
   Click "Check & Review" to see every song and whether you already have
   it. "Download All Selected" fetches all the ticked songs, a few at a
   time, picking the best YouTube match for each. Songs without a
   confident match are left for you - use "Search YouTube" on that row to
   pick one yourself.

2) PLAYLIST
   Two modes, chosen at the top:

   From a playlist
     Matches every song in a playlist to the files in your music folder.
     A review window lists every song, worst matches first. For each one
     you can keep the best guess, Browse to a different file, Search
     YouTube to fetch it, or Skip it. "Download All Selected" fetches all
     missing songs at once. Nothing is saved until you click
     "Finish & Save Playlist". Output is an .m3u8 playlist, or the songs
     copied into a folder.

   From my music library
     Builds playlists from the tags already in your files.
       1. Pick your music folder and click "Scan Folder".
       2. Tick your main genres (Top 10 ticks the biggest ones).
          Everything else goes into "Other Genres".
       3. Choose "Make playlists by": Genre, Genre + Year, Year, Artist,
          BPM range, or Genre + BPM range.
       4. Years: "Individually from 2024" gives 2026, 2025, 2024 as their
          own playlists and groups older songs by decade (2010s, 90s...).
       5. Names: "2026 Tech House" or "Tech House 2026".
       6. Output: .m3u8 playlists, or copy the songs into flat folders
          ("2026 Tech House") or nested folders ("2026/Tech House").
       7. Click "Preview Playlists" to see exactly what will be made,
          then "Create".
     The "Include" boxes decide whether songs in other genres, or with no
     genre, year or BPM, are included. An orange line tells you how many
     songs are being left out. Your choices are remembered.
     Copying skips songs that are already in the destination, so running
     it again only copies what's new.

3) TAGGING
   Looks up every song in a folder and fills in cover art, genre, label,
   year and ISRC - and BPM and key from Beatport.
     - Look up on: Auto (Beatport, then Deezer, then iTunes) or one source.
       Beatport is best for electronic music. For pop / hip-hop / rock
       folders, "Deezer only" gives more natural genres and titles.
     - More options: skip songs already tagged by TrackMatch, or only
       look at songs missing a genre or cover art.
   Click "Scan & Look Up", then review. Confident matches are ticked
   automatically; weak matches or the wrong version (remix, live, cover)
   are left as "Don't change" for you to check. Nothing is written until
   you click Apply.
   Big folders: turn on Large library mode in Settings - 3 songs are
   looked up at a time, progress is saved so you can stop and resume, and
   the review is a compact table. Without it you'll get a warning before
   scanning 500+ songs.

4) CONVERT
   Converts a folder of songs between mp3, m4a, flac, wav and aiff,
   keeping tags and cover art.
     - Use for: Audio CD burning, MP3 CD for car / CD player, Older CDJ /
       USB player, iPhone / iTunes, DJ software (aiff from wav/flac),
       Archive (flac) - or Custom.
     - The orange/grey line under the formats says what the conversion
       does to quality; click its (i) for the full explanation.
     - More options: save copies in a "Converted (mp3)" subfolder or next
       to the originals, and optionally move originals to the Recycle
       Bin / Trash after each one converts successfully.
   Before anything runs you'll see how many files, the size before and
   after, and how many audio CDs (80 minutes each) or MP3 CDs (700 MB
   each) the result fills. Existing files are never overwritten.

5) SETTINGS
     - Dark / Light mode and accent colour
     - YouTube cookies - only for age-restricted videos
     - Large library mode, Minimum match (how close a lookup must be to
       count as confident), and clearing saved lookups
     - Show log panels in each tab, and Open logs folder
   At the bottom: the version number and links to TrackMatch on GitHub.


GOOD TO KNOW
------------
- .m3u8 playlists store paths RELATIVE to the playlist file (e.g.
  "Tracks/Song.mp3"), so they keep working when moved - as long as the
  music moves with them. Keep the playlist and music on the same drive,
  in the same positions relative to each other.
  Rekordbox: File > Import > Import Playlist.
- Tag and convert BEFORE importing into Rekordbox. Rekordbox reads tags
  on import, and a converted file is a new file to DJ software - cue
  points, beat grids and playlist entries stay with the old file.
- Converting can't improve quality. YouTube audio is about 128-160 kbps;
  for tracks you'll play on big systems, buy lossless or 320 kbps copies.
- Rekordbox works out BPM and key itself when it analyses tracks, so
  songs that TrackMatch couldn't find BPM/key for are fine.


FILES TRACKMATCH KEEPS (next to TrackMatch.exe)
-----------------------------------------------
  trackmatch_settings.json       your settings
  trackmatch_lookup_cache.jsonl  saved lookups (Large library mode) -
                                 clear them in Settings
  logs\                          one log file per session, kept 30 days


A NOTE ON YOUTUBE DOWNLOADS
---------------------------
Downloading audio from YouTube may violate YouTube's Terms of Service,
even for personal use. This feature is provided for convenience; use it
at your own discretion.


TROUBLESHOOTING
---------------
- Downloading or converting fails straight away:
    Check ffmpeg is installed (see Getting Started) and restart TrackMatch.

- "HTTP Error 403" on a YouTube download:
    TrackMatch retries once automatically. If it keeps happening, try
    again later, or set a browser under Settings > YouTube cookies.

- A lookup source (Beatport, Spotify, Apple Music links) stops working:
    Those sites are read through their public web pages, which can
    change. Try another source (Deezer / iTunes) and check for a newer
    version of TrackMatch.

- Songs matched to the wrong version/remix:
    Pick the right result from the list in the review window, or leave it
    as "Don't change". In the Playlist tab, use Browse or Search YouTube.

- Playlist "can't find files" after moving to a USB/other computer:
    Make sure the music folder was copied along with the .m3u8, and that
    their positions relative to each other haven't changed.

- Something else went wrong:
    Settings > Open logs folder. The newest log file has the details -
    include it when reporting a problem on GitHub.
