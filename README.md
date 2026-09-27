# Playlist Music Downloader

A desktop utility for building a local music library from playlist CSV files. It searches for tracks, downloads audio through `yt-dlp`, writes metadata and cover art, and organizes the results for offline listening.

## Features

- Imports playlist CSV files
- Searches for tracks and retries common matching variations
- Downloads audio and applies artist, album, genre, artwork, and track metadata
- Organizes output by playlist and reports failures for review

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install yt-dlp mutagen pillow
python gui_downloader.py
```

## Privacy and rights

The app can use a local `cookies.txt` session file, which is intentionally ignored by Git. Keep playlists, downloaded audio, cookies, and failure reports local. Use it only with content you have the right to download and keep.

## Status

Windows-focused personal tool, prepared for local development.
