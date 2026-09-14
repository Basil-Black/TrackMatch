TRACKMATCH
===========

What this does
---------------
TrackMatch takes a Spotify playlist and helps you build a matching local
music collection out of it - either as an .m3u8 playlist file (for use on
a Rekordbox USB or any player that reads M3U) or as a folder of copied
files. It can also fetch any tracks you don't already have from YouTube.

It does NOT pull anything from Spotify directly - it works from a CSV
export of your playlist (see Step 1 below).


STEP 1 - Export your playlist from Spotify
--------------------------------------------
1. Go to https://exportify.net in a browser
2. Log in with your Spotify account
3. Find the playlist you want
4. Click "Export" - this downloads a .csv file (usually to your
   Downloads folder)

You'll use this same CSV file in both tabs below.


THE THREE TABS
-----------------

1) DOWNLOAD MISSING TRACKS
   Checks a playlist CSV against a folder you already have, and lets you
   fetch anything missing straight from YouTube.
     - Playlist CSV file   -> the .csv from Step 1
     - Folder to check against -> your existing music folder
     - Download destination -> where fetched tracks get saved (defaults
       to the same folder you're checking against - untick the box to
       choose somewhere else)
     - Download format / bitrate -> mp3 at 320kbps by default
   Click "Check & Review" to open a list of every track with its status
   (found / possibly found / not found). Use "Search YouTube" on any row
   to pick a result and download it - downloaded files are named and
   tagged using the CSV's own Title/Artist/Album/Genre, not YouTube's.
   This tab does not create a playlist - it's purely for filling gaps in
   your library.

2) CREATE PLAYLIST
   Matches a playlist CSV against a music folder and builds the actual
   output.
     - Playlist CSV file   -> the .csv from Step 1
     - Music folder to search -> your (now hopefully more complete)
       music library
     - Output type -> Create M3U playlist, or Copy files into a folder
     - Save location -> where the .m3u8 or copied files go
   Click "Run" to open a review window listing every track in the
   playlist, worst matches first, each pre-filled with its best guess.
   For each row you can leave it as-is, Browse to a different file,
   Search YouTube to fetch one on the spot, or check Skip to leave it
   out. Nothing is saved until you click "Finish & Save Playlist".

3) SETTINGS
   Toggle Dark mode / Light mode for the whole program, including any
   review or YouTube-results windows already open. Your choice is saved
   and remembered next time you open TrackMatch.


IMPORTANT - Keep the playlist and music together
----------------------------------------------------
The .m3u8 file stores paths RELATIVE to its own location (e.g.
"Tracks/Song.mp3", not a full C:\...\Song.mp3 path). This is what makes
it portable across USB drives and different computers.

This means: whenever you copy the playlist to a USB drive or another
device, you must copy the music folder along with it, keeping the same
folder structure relative to the .m3u8 file. If you move just the
.m3u8 on its own, it will not be able to find the songs.

Best practice: save the .m3u8 either directly inside your music folder,
or in the folder just above it.


REQUIREMENTS
---------------
- ffmpeg must be installed and on your system PATH for the YouTube
  download feature to work (matching/playlist creation without
  downloading does not need ffmpeg).
    Windows: winget install ffmpeg
    Mac:     brew install ffmpeg
  After installing, fully restart the app (and your terminal, if you're
  running from source) so it picks up the updated PATH.


A NOTE ON YOUTUBE DOWNLOADS
-------------------------------
Downloading audio from YouTube may violate YouTube's Terms of Service,
even for personal use. This feature is provided for convenience; use it
at your own discretion.


TROUBLESHOOTING
------------------
- "Missing dependency" error on launch:
    This shouldn't happen with the .exe (everything is bundled in).
    If you see it, you're probably running the .py file directly
    without the required Python packages installed
    (pip install mutagen yt-dlp).

- YouTube search/download fails immediately:
    Check that ffmpeg is installed and on your PATH (see Requirements).

- A track's best guess looks right but wasn't auto-matched confidently:
    Every track goes through manual review now, so just confirm it as-is
    in the review window - no need to change anything.

- Songs matched to the wrong version/remix:
    Use Browse in the review window to manually pick the correct file,
    or Search YouTube to fetch the right one.

- Playlist "can't find files" after moving to a USB/other computer:
    Make sure the music folder was copied along with the .m3u8, and
    that their relative positions to each other haven't changed.

- Downloaded file has the wrong Artist/Title/Album:
    This shouldn't happen - TrackMatch overwrites YouTube's own metadata
    with the playlist's CSV data after every download. If you still see
    this, please check the CSV itself has the correct data for that
    track.
