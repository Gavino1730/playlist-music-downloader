import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import queue
import csv
import json
import urllib.request
from urllib.parse import quote
from pathlib import Path
from datetime import datetime
from io import BytesIO
import time
import shutil

import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPUB, TRCK
from PIL import Image

BASE = Path(__file__).parent
PLAYLISTS = BASE / "playlists"
MUSIC = Path(r"C:\Users\gavin\Documents\Coding\DJ\music")
COOKIES = BASE / "cookies.txt"


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def safe_filename(artist, track):
    return "".join(c for c in f"{artist} - {track}" if c.isalnum() or c in " -.").strip().rstrip(".")


def primary_artist(artist):
    return (artist or "Unknown").split(";")[0].strip() or "Unknown"


def folder_name(csv_name):
    return csv_name.replace("_", " ").replace(".csv", "").strip()


def _base_opts():
    """Common yt-dlp options (cookies + JS runtime)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"node": {}},
        "sleep_interval_requests": 1,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 1,
    }
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    return opts


def _search_variants(artist, track):
    artist = (artist or "").strip()
    track = (track or "").strip()
    candidates = []
    seen = set()

    def add(value):
        value = " ".join((value or "").split())
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    primary_artist = artist.split(";")[0].strip()
    main_track = track.split(" - ")[0].strip()
    main_track = main_track.split(" (feat.")[0].strip()
    main_track = main_track.split(" feat.")[0].strip()

    add(f"{primary_artist} {track}")
    add(f"{primary_artist} {main_track}")
    add(f"{primary_artist} {main_track} official")
    return candidates


def search_yt(artist, track):
    search_yt.last_error = "No search result"
    search_yt.rate_limited = False
    for variant in _search_variants(artist, track):
        try:
            opts = _base_opts()
            opts["noplaylist"] = True
            opts["extract_flat"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                r = ydl.extract_info(f"ytsearch1:{variant}", download=False)
                entries = r.get("entries") if isinstance(r, dict) else []
                if entries:
                    for v in entries:
                        if not v:
                            continue
                        if v.get("live_status") in ("was_live", "is_live"):
                            continue
                        vid = v.get("id")
                        if vid:
                            return f"https://www.youtube.com/watch?v={vid}"
        except Exception as exc:
            search_yt.last_error = str(exc).splitlines()[0]
            msg = str(exc).lower()
            if "429" in msg or "rate-limited" in msg or "403" in msg:
                search_yt.rate_limited = True
                return None
            else:
                time.sleep(1)
    return None


search_yt.last_error = "No search result"
search_yt.rate_limited = False


def download_song(url, out_dir, filename):
    download_song.last_error = "Download failed"
    try:
        opts = _base_opts()
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3",
                                "preferredquality": "192"}],
            "outtmpl": str(out_dir / filename),
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        mp3 = out_dir / f"{filename}.mp3"
        if mp3.exists():
            return mp3
        download_song.last_error = "yt-dlp completed without creating an MP3"
    except Exception as exc:
        download_song.last_error = str(exc).splitlines()[0]
    return None


download_song.last_error = "Download failed"


def get_cover(artist, album):
    try:
        url = f"https://itunes.apple.com/search?term={quote(f'{artist} {album}')}&media=music&entity=album&limit=1"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            art = data["results"][0].get("artworkUrl600") if data.get("results") else None
            if art:
                with urllib.request.urlopen(art, timeout=5) as r2:
                    return r2.read()
    except Exception:
        pass
    return None


def tag(mp3_path, artist, track, album, cover_data, *, genre="", release_date="", label="", track_number=""):
    try:
        artists = [value.strip() for value in artist.split(";") if value.strip()]
        artists = artists or [artist.strip() or "Unknown"]
        try:
            tags = ID3(str(mp3_path))
        except ID3NoHeaderError:
            tags = ID3()
        tags.clear()
        tags.add(TIT2(encoding=3, text=[track]))
        tags.add(TPE1(encoding=3, text=artists))
        tags.add(TPE2(encoding=3, text=[artists[0]]))
        tags.add(TALB(encoding=3, text=[album]))
        if genre:
            tags.add(TCON(encoding=3, text=[genre]))
        if release_date:
            tags.add(TDRC(encoding=3, text=[release_date]))
        if label:
            tags.add(TPUB(encoding=3, text=[label]))
        if track_number:
            tags.add(TRCK(encoding=3, text=[track_number]))
        if cover_data:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))
        tags.save(str(mp3_path), v2_version=3)
    except Exception as exc:
        raise RuntimeError(f"Could not tag {mp3_path.name}: {exc}") from exc


def save_cover(out_dir, filename, data):
    try:
        Image.open(BytesIO(data)).save(out_dir / f"{filename}_cover.jpg", "JPEG", quality=90)
    except Exception:
        pass


# ── worker thread ────────────────────────────────────────────────

def worker(q, playlists, stop_event, *, sync=False):
    stats = {"ok": 0, "skip": 0, "fail": 0}
    failed = []  # list of (out_dir, artist, track, album, fname)

    try:
        if sync and not stop_event.is_set():
            _sync_playlists(q, playlists, stop_event)
        _do_work(q, playlists, stop_event, stats, failed)
        # ── retry failed songs ──
        if failed and not stop_event.is_set():
            _retry_failed(q, stop_event, stats, failed)
        # ── save any remaining failures to CSV ──
        if failed:
            _save_failed_csv(q, failed)
    except Exception as e:
        q.put(("log", f"\n[ERROR] Unexpected crash: {e}"))
    finally:
        q.put(("log", f"\n===  DONE  |  Downloaded {stats['ok']}  |  Skipped {stats['skip']}  |  Failed {stats['fail']}  ==="))
        q.put(("finished", None))


def _sync_playlists(q, playlists, stop_event):
    """Sync Music folders with CSV playlists: delete removed songs, move relocated ones."""
    q.put(("log", "\n===  SYNCING  ==="))
    q.put(("now_playing", {"artist": "Syncing", "track": "Checking playlists\u2026",
                            "album": "", "num": 0, "of": 0}))

    # Build a map: fname -> list of playlist folder names where it should exist
    expected = {}  # {fname: set(folder_names)}
    playlist_fnames = {}  # {folder_name: set(fnames)}
    for name, csv_path in playlists:
        songs = read_csv(csv_path)
        fnames = set()
        for row in songs:
            artist = (row.get("Artist Name(s)") or "Unknown").strip()
            track = (row.get("Track Name") or "Unknown").strip()
            fn = safe_filename(primary_artist(artist), track)
            fnames.add(fn)
            expected.setdefault(fn, set()).add(name)
        playlist_fnames[name] = fnames

    moved = 0
    deleted = 0

    for name, _csv_path in playlists:
        if stop_event.is_set():
            break
        out_dir = MUSIC / name
        if not out_dir.exists():
            continue
        wanted = playlist_fnames.get(name, set())

        # scan every mp3 in this folder
        for mp3 in list(out_dir.glob("*.mp3")):
            if stop_event.is_set():
                break
            fname = mp3.stem  # filename without .mp3
            if fname in wanted:
                continue  # still belongs here

            # song no longer in this playlist — check if it moved to another
            dest_folders = expected.get(fname, set())
            moved_it = False
            for dest_name in dest_folders:
                dest_dir = MUSIC / dest_name
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_mp3 = dest_dir / mp3.name
                if not dest_mp3.exists():
                    # move mp3
                    shutil.move(str(mp3), str(dest_mp3))
                    # move cover if exists
                    cover = out_dir / f"{fname}_cover.jpg"
                    if cover.exists():
                        shutil.move(str(cover), str(dest_dir / cover.name))
                    q.put(("log", f"[MOVE] {fname}  →  {dest_name}/"))
                    moved += 1
                    moved_it = True
                    break

            if not moved_it:
                # not in any selected playlist anymore — delete it
                mp3.unlink(missing_ok=True)
                cover = out_dir / f"{fname}_cover.jpg"
                cover.unlink(missing_ok=True)
                q.put(("log", f"[DEL]  {fname}  (removed from {name})"))
                deleted += 1

    summary = f"Sync complete: {moved} moved, {deleted} deleted"
    q.put(("log", f"\n===  {summary}  ==="))
    q.put(("now_playing", {"artist": "Sync done", "track": summary,
                            "album": "", "num": 0, "of": 0}))


def _do_work(q, playlists, stop_event, stats, failed):
    # count total songs
    all_songs = []
    for name, csv_path in playlists:
        songs = read_csv(csv_path)
        all_songs.append((name, songs))

    total = sum(len(s) for _, s in all_songs)
    done = 0
    t_start = time.time()
    q.put(("max", total))
    q.put(("total_playlists", len(all_songs)))
    q.put(("t_start", t_start))

    playlist_idx = 0
    for name, songs in all_songs:
        if stop_event.is_set():
            break
        playlist_idx += 1
        out_dir = MUSIC / name
        out_dir.mkdir(parents=True, exist_ok=True)
        q.put(("log", f"\n===  {name}  ({len(songs)} songs)  ==="))
        q.put(("playlist_info", {"name": name, "idx": playlist_idx,
                                  "total": len(all_songs), "songs_in_pl": len(songs)}))

        # ── fast bulk-skip: scan folder once, separate existing from needed ──
        existing_mp3s = {f.stem for f in out_dir.glob("*.mp3")} if out_dir.exists() else set()
        to_download = []
        for row in songs:
            artist = (row.get("Artist Name(s)") or "Unknown").strip()
            track = (row.get("Track Name") or "Unknown").strip()
            album = (row.get("Album Name") or "Unknown").strip()
            fname = safe_filename(primary_artist(artist), track)
            if fname in existing_mp3s:
                stats["skip"] += 1
            else:
                to_download.append((artist, track, album, fname,
                                    row.get("Genres") or "",
                                    row.get("Release Date") or "",
                                    row.get("Record Label") or ""))

        skipped = len(songs) - len(to_download)
        done += skipped
        if skipped:
            q.put(("log", f"[SKIP] {skipped} already downloaded"))
            q.put(("progress", done))
            q.put(("timing", {"remaining": 0, "avg": 0, "done": done, "total": total}))
            q.put(("stats", dict(stats)))

        # ── download only what's missing ──
        song_in_pl = skipped
        for song_number, (artist, track, album, fname, genre, release_date, label) in enumerate(to_download, 1):
            if stop_event.is_set():
                break
            song_in_pl += 1

            q.put(("now_playing", {"artist": artist, "track": track,
                                    "album": album, "num": song_in_pl,
                                    "of": len(songs)}))

            ok = _try_download(q, out_dir, artist, track, album, fname,
                               genre=genre, release_date=release_date,
                               label=label, track_number=str(song_number))
            if ok:
                stats["ok"] += 1
            else:
                stats["fail"] += 1
                failed.append((out_dir, artist, track, album, fname,
                               genre, release_date, label, str(song_number)))

            done += 1
            elapsed = time.time() - t_start
            dl_count = stats["ok"] + stats["fail"]
            avg = elapsed / dl_count if dl_count else 0
            remaining = avg * (total - done)
            q.put(("progress", done))
            q.put(("timing", {"remaining": remaining,
                               "avg": avg, "done": done, "total": total}))
            q.put(("stats", dict(stats)))

        if not stop_event.is_set():
            q.put(("playlist_done", name))


def _try_download(q, out_dir, artist, track, album, fname, *,
                  genre="", release_date="", label="", track_number=""):
    """Search + download + tag a single song. Returns True on success."""
    for attempt in range(2):
        url = search_yt(artist, track)
        if url:
            result = download_song(url, out_dir, fname)
            if result:
                cover = get_cover(artist, album)
                try:
                    tag(result, artist, track, album, cover,
                        genre=genre, release_date=release_date,
                        label=label, track_number=track_number)
                except RuntimeError as exc:
                    q.put(("log", f"[FAIL] {artist} - {track}  ({exc})"))
                    result.unlink(missing_ok=True)
                    return False
                if cover:
                    save_cover(out_dir, fname, cover)
                q.put(("log", f"[OK]   {artist} - {track}"))
                return True
            q.put(("log", f"[FAIL] {artist} - {track}  ({download_song.last_error})"))
            if "rate-limit" in download_song.last_error.lower() or "429" in download_song.last_error:
                return False
            time.sleep(2)
        else:
            q.put(("log", f"[FAIL] {artist} - {track}  (search: {search_yt.last_error})"))
            if search_yt.rate_limited:
                return False
            time.sleep(2)
    return False


MAX_RETRIES = 2

def _retry_failed(q, stop_event, stats, failed):
    """Retry every failed song up to MAX_RETRIES extra times."""
    to_retry = list(failed)
    for attempt in range(1, MAX_RETRIES + 1):
        if not to_retry or stop_event.is_set():
            break
        q.put(("log", f"\n===  RETRY {attempt}/{MAX_RETRIES}  ({len(to_retry)} songs)  ==="))
        q.put(("now_playing", {"artist": "Retrying", "track": f"Attempt {attempt}",
                                "album": f"{len(to_retry)} songs", "num": 0, "of": 0}))
        still_failed = []
        for i, (out_dir, artist, track, album, fname, genre,
            release_date, label, track_number) in enumerate(to_retry, 1):
            if stop_event.is_set():
                still_failed.extend(to_retry[i - 1:])
                break
            q.put(("now_playing", {"artist": artist, "track": track,
                                    "album": f"retry {attempt}", "num": i,
                                    "of": len(to_retry)}))
            q.put(("log", f"[RETRY] {artist} - {track}  (attempt {attempt})"))
            ok = _try_download(q, out_dir, artist, track, album, fname,
                               genre=genre, release_date=release_date,
                               label=label, track_number=track_number)
            if ok:
                stats["ok"] += 1
                stats["fail"] -= 1
                q.put(("stats", dict(stats)))
            else:
                still_failed.append((out_dir, artist, track, album, fname,
                                     genre, release_date, label, track_number))
        to_retry = still_failed
    # update the shared failed list to only what truly failed
    failed.clear()
    failed.extend(to_retry)


def _save_failed_csv(q, failed):
    """Write remaining failures to failed.csv so they can be inspected."""
    path = BASE / "failed.csv"
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Artist", "Track", "Album", "Folder"])
            for out_dir, artist, track, album, _fname, _genre, _release_date, _label, _track_number in failed:
                w.writerow([artist, track, album, out_dir.name])
        q.put(("log", f"\n[INFO] {len(failed)} failed songs saved to failed.csv"))
    except Exception as e:
        q.put(("log", f"\n[ERROR] Could not write failed.csv: {e}"))



# ── helpers ──────────────────────────────────────────────────────

def _fmt_time(secs):
    if secs < 0:
        return "--:--"
    h, m = divmod(int(secs), 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── colour palette ───────────────────────────────────────────────

BG       = "#11111b"   # deepest base
BG2      = "#1e1e2e"   # panel background
BG3      = "#232336"   # raised card background
FG       = "#cdd6f4"   # main text
FG_BRIGHT = "#ffffff"  # high-emphasis text
ACCENT   = "#89b4fa"   # blue accent
GREEN    = "#a6e3a1"
GREEN_DK = "#2d4a3e"   # dark green for button bg
YELLOW   = "#f9e2af"
RED      = "#f38ba8"
RED_DK   = "#4a2d38"   # dark red for button bg
DIM      = "#6c7086"
SURFACE  = "#313244"
SURFACE2 = "#3b3b54"   # lighter surface for hover
BORDER   = "#45475a"   # subtle borders

# ── GUI ──────────────────────────────────────────────────────────

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Music Downloader")
        self.root.geometry("960x780")
        self.root.minsize(720, 600)
        self.root.configure(bg=BG)
        self.stop_event = threading.Event()
        self.q = queue.Queue()
        self.running = False
        self._pulse_on = False
        self._style()
        self.build()
        self.load_playlists()
        self.root.mainloop()

    # ── theming ─────────────────────────────────────────────────
    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".",       background=BG, foreground=FG, font=("Segoe UI", 10))
        s.configure("TFrame",  background=BG)
        s.configure("TLabel",  background=BG, foreground=FG)
        s.configure("TButton", background=SURFACE, foreground=FG, padding=(12, 5),
                    font=("Segoe UI", 10, "bold"), borderwidth=0)
        s.map("TButton",
              background=[("active", SURFACE2), ("disabled", BG2)],
              foreground=[("active", FG_BRIGHT), ("disabled", DIM)])
        # ── Start button (green)
        s.configure("Start.TButton", background=GREEN_DK, foreground=GREEN,
                    padding=(18, 8), font=("Segoe UI", 11, "bold"), borderwidth=0)
        s.map("Start.TButton",
              background=[("active", "#3d6a50"), ("disabled", BG2)],
              foreground=[("active", FG_BRIGHT), ("disabled", DIM)])
        # ── Stop button (red)
        s.configure("Stop.TButton", background=RED_DK, foreground=RED,
                    padding=(18, 8), font=("Segoe UI", 11, "bold"), borderwidth=0)
        s.map("Stop.TButton",
              background=[("active", "#6a2d42"), ("disabled", BG2)],
              foreground=[("active", FG_BRIGHT), ("disabled", DIM)])
        s.configure("TLabelframe",       background=BG2, foreground=ACCENT)
        s.configure("TLabelframe.Label", background=BG2, foreground=ACCENT,
                    font=("Segoe UI", 10, "bold"))
        # progress bar — tall and prominent
        s.configure("pointed.Horizontal.TProgressbar",
                    troughcolor=SURFACE, background=ACCENT,
                    bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT,
                    thickness=22)

    # ── layout ──────────────────────────────────────────────────
    def build(self):
        f = tk.Frame(self.root, bg=BG, padx=16, pady=12)
        f.pack(fill="both", expand=True)

        # ── title row
        hdr = tk.Frame(f, bg=BG)
        hdr.pack(fill="x", pady=(0, 10))
        tk.Label(hdr, text="\u266b", font=("Segoe UI", 22), bg=BG, fg=ACCENT).pack(side="left")
        tk.Label(hdr, text="Music Downloader", font=("Segoe UI", 20, "bold"),
                 bg=BG, fg=FG_BRIGHT).pack(side="left", padx=(8, 0))
        # Action buttons live in the header — always visible
        self.btn_stop = ttk.Button(hdr, text="\u25a0  Stop", command=self.stop,
                                   state="disabled", style="Stop.TButton")
        self.btn_stop.pack(side="right", padx=(6, 0))
        self.btn_start = ttk.Button(hdr, text="\u25b6  Start Download", command=self.start,
                                    style="Start.TButton")
        self.btn_start.pack(side="right")

        # ═══ PROGRESS — prominent, central ═══════════════════════
        prog_card = tk.Frame(f, bg=BG3, padx=14, pady=10,
                             highlightbackground=BORDER, highlightthickness=1)
        prog_card.pack(fill="x", pady=(0, 8))
        prog_top = tk.Frame(prog_card, bg=BG3)
        prog_top.pack(fill="x", pady=(0, 6))
        tk.Label(prog_top, text="PROGRESS", font=("Segoe UI", 9, "bold"),
                 bg=BG3, fg=DIM).pack(side="left")
        self.var_pct = tk.StringVar(value="0%")
        self.lbl_pct = tk.Label(prog_top, textvariable=self.var_pct,
                                font=("Segoe UI", 18, "bold"), bg=BG3, fg=ACCENT)
        self.lbl_pct.pack(side="right")
        self.var_progress_detail = tk.StringVar(value="")
        tk.Label(prog_top, textvariable=self.var_progress_detail,
                 font=("Segoe UI", 10), bg=BG3, fg=FG).pack(side="right", padx=(0, 12))
        self.pbar = ttk.Progressbar(prog_card, mode="determinate",
                                    style="pointed.Horizontal.TProgressbar", length=400)
        self.pbar.pack(fill="x")

        # ── middle row: playlists (left) + now playing (right)
        mid = tk.Frame(f, bg=BG)
        mid.pack(fill="x", pady=(0, 6))
        mid.columnconfigure(0, weight=3)
        mid.columnconfigure(1, weight=2)

        # ── PLAYLISTS panel
        lf = tk.Frame(mid, bg=BG2, padx=10, pady=8,
                      highlightbackground=BORDER, highlightthickness=1)
        lf.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        tk.Label(lf, text="PLAYLISTS", font=("Segoe UI", 9, "bold"),
                 bg=BG2, fg=DIM).pack(anchor="w", pady=(0, 4))

        self.listbox = tk.Listbox(lf, height=6, selectmode="multiple",
                                  font=("Segoe UI", 10), bg=SURFACE, fg=FG,
                                  selectbackground=ACCENT, selectforeground=BG,
                                  activestyle="none",
                                  highlightthickness=0, bd=0, relief="flat")
        self.listbox.pack(fill="both", expand=True)
        # alternating row colours + hover
        self.listbox.bind("<Motion>", self._on_listbox_motion)
        self.listbox.bind("<Leave>",  self._on_listbox_leave)
        self._lb_hover_idx = None

        bf = tk.Frame(lf, bg=BG2)
        bf.pack(fill="x", pady=(6, 0))
        for txt, cmd in [("Select All", self.sel_all), ("Deselect All", self.desel_all)]:
            ttk.Button(bf, text=txt, command=cmd).pack(side="left", padx=(0, 4))

        # sync toggle
        self.sync_var = tk.BooleanVar(value=True)
        self.sync_cb = tk.Checkbutton(
            bf, text="Sync", variable=self.sync_var,
            font=("Segoe UI", 9, "bold"), bg=BG2, fg=ACCENT,
            activebackground=BG2, activeforeground=ACCENT,
            selectcolor=SURFACE, highlightthickness=0, bd=0)
        self.sync_cb.pack(side="right", padx=(4, 0))

        # ── NOW PLAYING panel
        np = tk.Frame(mid, bg=BG3, padx=14, pady=10,
                      highlightbackground=BORDER, highlightthickness=1)
        np.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        np_hdr = tk.Frame(np, bg=BG3)
        np_hdr.pack(fill="x", pady=(0, 6))
        tk.Label(np_hdr, text="NOW PLAYING", font=("Segoe UI", 9, "bold"),
                 bg=BG3, fg=DIM).pack(side="left")
        self._pulse_dot = tk.Label(np_hdr, text="\u25cf", font=("Segoe UI", 10),
                                    bg=BG3, fg=BG3)  # hidden initially
        self._pulse_dot.pack(side="left", padx=(6, 0))

        self.var_track  = tk.StringVar(value="Waiting\u2026")
        self.var_artist = tk.StringVar(value="")
        self.var_album  = tk.StringVar(value="")
        self.var_plname = tk.StringVar(value="")

        tk.Label(np, textvariable=self.var_track, font=("Segoe UI", 14, "bold"),
                 bg=BG3, fg=FG_BRIGHT, anchor="w", wraplength=280).pack(fill="x")
        tk.Label(np, textvariable=self.var_artist, font=("Segoe UI", 10),
                 bg=BG3, fg=FG, anchor="w").pack(fill="x", pady=(2, 0))
        tk.Label(np, textvariable=self.var_album, font=("Segoe UI", 10, "italic"),
                 bg=BG3, fg=DIM, anchor="w").pack(fill="x")
        # spacer
        tk.Frame(np, bg=BG3, height=4).pack(fill="x")
        tk.Label(np, textvariable=self.var_plname, font=("Segoe UI", 9, "bold"),
                 bg=BG3, fg=ACCENT, anchor="w").pack(fill="x")

        # ═══ STATS GRID ══════════════════════════════════════════
        sg = tk.Frame(f, bg=BG2, padx=10, pady=8,
                      highlightbackground=BORDER, highlightthickness=1)
        sg.pack(fill="x", pady=(0, 6))
        tk.Label(sg, text="STATISTICS", font=("Segoe UI", 9, "bold"),
                 bg=BG2, fg=DIM).grid(row=0, column=0, sticky="w", columnspan=4, pady=(0, 4))

        self.stat_vars = {}
        labels = [
            ("Songs Done",  "done",      GREEN),
            ("Remaining",   "remaining", YELLOW),
            ("Downloaded",  "ok",        GREEN),
            ("Skipped",     "skip",      DIM),
            ("Failed",      "fail",      RED),
            ("Elapsed",     "elapsed",   FG),
            ("ETA",         "eta",       ACCENT),
            ("Avg / Song",  "avg",       FG),
        ]
        for i, (label, key, colour) in enumerate(labels):
            col = i % 4
            row = 1 + i // 4
            # each stat in its own mini-card
            cell = tk.Frame(sg, bg=BG3, padx=10, pady=6,
                            highlightbackground=BORDER, highlightthickness=1)
            cell.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            sg.columnconfigure(col, weight=1)
            tk.Label(cell, text=label.upper(), font=("Segoe UI", 8, "bold"),
                     bg=BG3, fg=DIM).pack(anchor="w")
            v = tk.StringVar(value="0" if key not in ("elapsed", "eta", "avg") else "--:--")
            self.stat_vars[key] = v
            tk.Label(cell, textvariable=v, font=("Segoe UI", 18, "bold"),
                     bg=BG3, fg=colour).pack(anchor="w")

        # ═══ LOG ════════════════════════════════════════════════
        logf = tk.Frame(f, bg=BG2, padx=8, pady=6,
                        highlightbackground=BORDER, highlightthickness=1)
        logf.pack(fill="both", expand=True, pady=(0, 4))
        log_hdr = tk.Frame(logf, bg=BG2)
        log_hdr.pack(fill="x", pady=(0, 4))
        tk.Label(log_hdr, text="LOG", font=("Segoe UI", 9, "bold"),
                 bg=BG2, fg=DIM).pack(side="left")
        self._activity_dot = tk.Label(log_hdr, text="\u25cf", font=("Segoe UI", 8),
                                       bg=BG2, fg=BG2)
        self._activity_dot.pack(side="left", padx=(6, 0))

        self.log = scrolledtext.ScrolledText(
            logf, font=("Consolas", 9), state="disabled", wrap="word",
            bg=SURFACE, fg=FG, insertbackground=FG, highlightthickness=0,
            bd=0, relief="flat", spacing1=1, spacing3=1)
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("ok",   foreground=GREEN)
        self.log.tag_config("fail", foreground=RED)
        self.log.tag_config("skip", foreground=DIM)
        self.log.tag_config("head", foreground=ACCENT, font=("Consolas", 9, "bold"))
        self.log.tag_config("move", foreground=YELLOW)
        self.log.tag_config("dele", foreground=RED)

        # ── status bar
        self.status = tk.StringVar(value="Ready")
        tk.Label(f, textvariable=self.status, font=("Segoe UI", 9),
                 bg=BG, fg=DIM, anchor="w").pack(fill="x", pady=(4, 0))

    # ── playlist listbox hover highlighting ─────────────────────
    def _color_listbox_rows(self):
        for i in range(self.listbox.size()):
            bg = SURFACE if i % 2 == 0 else BG2
            self.listbox.itemconfigure(i, background=bg, foreground=FG)

    def _on_listbox_motion(self, event):
        idx = self.listbox.nearest(event.y)
        if idx == self._lb_hover_idx:
            return
        # restore previous
        if self._lb_hover_idx is not None and self._lb_hover_idx < self.listbox.size():
            prev_bg = SURFACE if self._lb_hover_idx % 2 == 0 else BG2
            self.listbox.itemconfigure(self._lb_hover_idx, background=prev_bg)
        # apply hover
        if 0 <= idx < self.listbox.size():
            self.listbox.itemconfigure(idx, background=SURFACE2)
        self._lb_hover_idx = idx

    def _on_listbox_leave(self, _):
        if self._lb_hover_idx is not None and self._lb_hover_idx < self.listbox.size():
            prev_bg = SURFACE if self._lb_hover_idx % 2 == 0 else BG2
            self.listbox.itemconfigure(self._lb_hover_idx, background=prev_bg)
        self._lb_hover_idx = None

    # ── pulsing activity dot ────────────────────────────────────
    def _start_pulse(self):
        self._pulse_on = True
        self._do_pulse()

    def _stop_pulse(self):
        self._pulse_on = False
        self._pulse_dot.config(fg=BG3)
        self._activity_dot.config(fg=BG2)

    def _do_pulse(self):
        if not self._pulse_on:
            return
        cur = self._pulse_dot.cget("fg")
        nxt = GREEN if cur != GREEN else BG3
        self._pulse_dot.config(fg=nxt)
        self._activity_dot.config(fg=nxt)
        self.root.after(600, self._do_pulse)

    def load_playlists(self):
        self.playlist_data = {}
        if not PLAYLISTS.exists():
            return
        for p in sorted(PLAYLISTS.glob("*.csv")):
            name = folder_name(p.name)
            self.playlist_data[name] = p
            self.listbox.insert("end", f"  {name}")
        self._color_listbox_rows()

    def sel_all(self):
        self.listbox.select_set(0, "end")

    def desel_all(self):
        self.listbox.select_clear(0, "end")

    def _get_selected_names(self):
        return [self.listbox.get(i).strip() for i in self.listbox.curselection()]

    def append_log(self, text):
        tag = None
        if text.startswith("[OK]"):     tag = "ok"
        elif text.startswith("[FAIL]"): tag = "fail"
        elif text.startswith("[SKIP]"): tag = "skip"
        elif text.startswith("[MOVE]"): tag = "move"
        elif text.startswith("[DEL]"):  tag = "dele"
        elif text.startswith("==="):    tag = "head"
        elif text.startswith("[RETRY]"): tag = "move"  # yellow for retries too
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def start(self):
        sel = self._get_selected_names()
        if not sel:
            messagebox.showwarning("Nothing selected", "Pick at least one playlist.")
            return

        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.pbar["value"] = 0
        self._total = 0
        self._total_pl = 0
        self.var_pct.set("0%")
        self.var_progress_detail.set("")
        self.var_track.set("Starting\u2026")
        self.var_artist.set("")
        self.var_album.set("")
        self.var_plname.set("")
        for k, v in self.stat_vars.items():
            v.set("--:--" if k in ("elapsed", "eta", "avg") else "0")
        self.status.set("Downloading\u2026")
        self.stop_event.clear()
        self.running = True
        self._t_start = None          # set by worker via queue
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.listbox.config(state="disabled")
        self._start_pulse()

        pairs = [(n, self.playlist_data[n]) for n in sel]
        do_sync = self.sync_var.get()
        threading.Thread(target=worker,
                         args=(self.q, pairs, self.stop_event),
                         kwargs={"sync": do_sync}, daemon=True).start()
        self.poll()
        self._tick_elapsed()

    def stop(self):
        self.stop_event.set()
        self.btn_stop.config(state="disabled")
        self.append_log("\nStopping...")

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.append_log(val)
                elif kind == "max":
                    self.pbar["maximum"] = val
                    self._total = val
                elif kind == "total_playlists":
                    self._total_pl = val
                elif kind == "progress":
                    self.pbar["value"] = val
                    pct = int(val / self._total * 100) if self._total else 0
                    self.var_pct.set(f"{pct}%")
                    self.var_progress_detail.set(f"{val} / {self._total} songs")
                elif kind == "t_start":
                    self._t_start = val
                elif kind == "timing":
                    self.stat_vars["done"].set(str(val["done"]))
                    self.stat_vars["remaining"].set(str(val["total"] - val["done"]))
                    self.stat_vars["eta"].set(_fmt_time(val["remaining"]))
                    self.stat_vars["avg"].set(_fmt_time(val["avg"]))
                elif kind == "stats":
                    self.stat_vars["ok"].set(str(val["ok"]))
                    self.stat_vars["skip"].set(str(val["skip"]))
                    self.stat_vars["fail"].set(str(val["fail"]))
                    self.status.set(
                        f"Downloaded {val['ok']}  \u00b7  Skipped {val['skip']}  \u00b7  Failed {val['fail']}")
                elif kind == "now_playing":
                    self.var_track.set(val["track"])
                    self.var_artist.set(val["artist"])
                    self.var_album.set(val["album"])
                elif kind == "playlist_info":
                    self.var_plname.set(
                        f"{val['name']}  ({val['idx']}/{val['total']})")
                elif kind == "playlist_done":
                    self._mark_playlist_done(val)
                elif kind == "finished":
                    self.done()
                    return
        except queue.Empty:
            pass
        if self.running:
            self.root.after(100, self.poll)

    def _mark_playlist_done(self, name):
        """Put a checkmark next to a completed playlist in the listbox."""
        for i in range(self.listbox.size()):
            if self.listbox.get(i).strip().lstrip("\u2713 ").strip() == name:
                self.listbox.delete(i)
                self.listbox.insert(i, f"  \u2713 {name}")
                self.listbox.itemconfigure(i, foreground=GREEN)
                break

    def _tick_elapsed(self):
        """Update the elapsed clock every second while running."""
        if self._t_start is not None:
            self.stat_vars["elapsed"].set(_fmt_time(time.time() - self._t_start))
        if self.running:
            self.root.after(1000, self._tick_elapsed)

    def done(self):
        self.running = False
        self._stop_pulse()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.listbox.config(state="normal")
        self.var_pct.set("100%" if self._total else "0%")
        self.stat_vars["remaining"].set("0")
        self.stat_vars["eta"].set("0:00")
        self.status.set("Finished  \u2714")
        self.var_track.set("Done!")
        self.var_artist.set("")
        self.var_album.set("")


if __name__ == "__main__":
    App()
