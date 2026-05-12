#!/usr/bin/env python3
"""
VLC Scheduler
Plays the next numbered video(s) from a designated folder at scheduled times.
State (last played index per folder) is persisted in playback_state.json.
"""

import argparse
import http.server
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import schedule
except ImportError:
    print("Missing dependency. Run:  sudo apt install python3-schedule")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE  = BASE_DIR / "playback_state.json"
LOG_FILE    = BASE_DIR / "vlc_scheduler.log"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Runtime state ─────────────────────────────────────────────────────────────
_active_proc: Optional[subprocess.Popen] = None
_config_mtime: float = 0.0
_dry_run: bool = False
_current_config: dict = {}

# ── Default configuration (written on first run) ──────────────────────────────
DEFAULT_CONFIG = {
    "vlc_path": "auto",
    "status_port": 8765,
    "video_extensions": [
        ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv",
        ".m4v", ".mpg", ".mpeg", ".webm", ".ts", ".vob", ".mts", ".m2ts",
    ],
    "schedules": [
        {
            "time": "17:30",
            "end_time": "19:00",
            "folders": [
                {"path": str(BASE_DIR / "folder01")},
            ],
        },
        {
            "time": "19:00",
            "folders": [
                {"path": str(BASE_DIR / "folder02"), "count": 3},
                {"path": str(BASE_DIR / "folder03"), "count": 1},
            ],
        },
    ],
}


# ── Config helpers ────────────────────────────────────────────────────────────

def get_folder_entries(entry: dict) -> list:
    """Return a normalised list of {"path": str, "count": int} dicts for a
    schedule entry.  Supports three config shapes (old → new):

      1. {"folder": "/p", "count": 2}
         → [{"path": "/p", "count": 2}]

      2. {"folders": ["/p1", "/p2"], "count": 2}
         → [{"path": "/p1", "count": 2}, {"path": "/p2", "count": 2}]

      3. {"folders": [{"path": "/p1", "count": 2}, {"path": "/p2", "count": 1}]}
         → [{"path": "/p1", "count": 2}, {"path": "/p2", "count": 1}]
    """
    default_count = entry.get("count", 1)

    if "folders" in entry:
        raw = entry["folders"]
        if not isinstance(raw, list):
            raw = [raw]
        result = []
        for item in raw:
            if isinstance(item, dict):
                result.append({"path": str(item["path"]), "count": item.get("count", default_count)})
            else:
                result.append({"path": str(item), "count": default_count})
        return result

    return [{"path": str(entry.get("folder", "")), "count": default_count}]


# ── VLC detection ─────────────────────────────────────────────────────────────

def detect_vlc(configured_path: str) -> str:
    """
    Return a usable VLC executable path.
    'auto' → try PATH then common install locations.
    Otherwise validate the configured path exists.
    """
    if not configured_path or configured_path == "auto":
        found = shutil.which("vlc")
        if found:
            return found
        for candidate in ("/usr/bin/vlc", "/usr/local/bin/vlc", "/snap/bin/vlc"):
            if Path(candidate).exists():
                return candidate
        log.error("VLC not found. Install VLC or set 'vlc_path' in config.json.")
        sys.exit(1)

    path = Path(configured_path)
    if not path.exists():
        log.error(f"VLC not found at configured path: {configured_path}")
        sys.exit(1)
    return str(path)


# ── Config / state helpers ────────────────────────────────────────────────────

def load_config() -> dict:
    global _config_mtime
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log.info("Created default config.json — edit it to customise paths.")
    _config_mtime = CONFIG_FILE.stat().st_mtime
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def config_changed() -> bool:
    """Return True if config.json has been modified on disk since last load."""
    try:
        return CONFIG_FILE.stat().st_mtime != _config_mtime
    except OSError:
        return False


def validate_config(config: dict) -> bool:
    """
    Check that every scheduled folder exists and contains at least one video.
    Returns True if valid; logs errors and returns False otherwise.
    Pass strict=True callers may choose to sys.exit on False.
    """
    extensions = config.get("video_extensions", DEFAULT_CONFIG["video_extensions"])
    ext_set    = {e.lower() for e in extensions}
    errors     = []

    if any(e.get("end_time") for e in config.get("schedules", [])):
        if not shutil.which("ffprobe"):
            errors.append("  'ffprobe' not found — required for end_time scheduling. Install ffmpeg.")

    for entry in config.get("schedules", []):
        end_time = entry.get("end_time")
        if end_time:
            try:
                ws = _time_to_seconds(end_time) - _time_to_seconds(entry["time"])
                if ws <= 0:
                    errors.append(f"  Schedule {entry['time']}: end_time '{end_time}' must be after start time")
            except (ValueError, KeyError):
                errors.append(f"  Schedule {entry.get('time', '?')}: invalid end_time '{end_time}'")

        for fe in get_folder_entries(entry):
            folder = Path(fe["path"])
            if not folder.exists():
                errors.append(f"  Folder not found: {folder}")
                continue
            try:
                has_videos = any(
                    f.suffix.lower() in ext_set
                    for f in folder.iterdir()
                    if f.is_file()
                )
            except PermissionError:
                errors.append(f"  Permission denied reading: {folder}")
                continue
            if not has_videos:
                errors.append(f"  No supported video files in: {folder}")

    if errors:
        log.error("Config validation failed:\n" + "\n".join(errors))
        return False

    log.info("Config validation passed.")
    return True


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── Video selection ───────────────────────────────────────────────────────────

def _natural_sort_key(path: Path):
    """Natural-sort: '2.webm' < '10.webm', 'ep02.mkv' < 'ep10.mkv'."""
    parts = re.split(r"(\d+)", path.stem)
    return [int(c) if c.isdigit() else c.lower() for c in parts]


def _time_to_seconds(t: str) -> int:
    """Convert 'HH:MM' to seconds since midnight."""
    h, m = t.split(":")
    return int(h) * 3600 + int(m) * 60


def get_video_duration(path: Path) -> float:
    """Return video duration in seconds via ffprobe, or 0.0 on failure."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def get_next_videos(folder_entries: list, state: dict, extensions: list):
    """
    Return (videos, folder_index, folder_path) for the next batch of videos to
    play.  The number of videos is taken from folder_entries[folder_index]["count"].

    Advances through folder_entries when one folder is exhausted, wrapping back
    to index 0 after the last folder.

    State is keyed by folder_entries[0]["path"] and holds:
        {"folder_index": int, "last_played": str|None}
    Old string-valued state entries are migrated automatically.
    """
    state_key   = folder_entries[0]["path"]
    entry_state = state.get(state_key, {})

    # Migrate old format: {"key": "video.mp4"} → {"folder_index": 0, "last_played": "video.mp4"}
    if isinstance(entry_state, str):
        entry_state = {"folder_index": 0, "last_played": entry_state}

    folder_index = entry_state.get("folder_index", 0) % len(folder_entries)
    last_played  = entry_state.get("last_played")

    ext_set = {e.lower() for e in extensions}

    # Try each folder in sequence, starting from the current one
    for _ in range(len(folder_entries)):
        fe          = folder_entries[folder_index]
        folder_path = fe["path"]
        count       = fe["count"]
        folder      = Path(folder_path)

        if not folder.exists():
            log.error(f"Folder not found: {folder_path} — skipping to next")
            folder_index = (folder_index + 1) % len(folder_entries)
            last_played  = None
            continue

        videos = sorted(
            [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in ext_set],
            key=_natural_sort_key,
        )

        if not videos:
            log.error(f"No video files in: {folder_path} — skipping to next")
            folder_index = (folder_index + 1) % len(folder_entries)
            last_played  = None
            continue

        next_index = 0
        if last_played:
            for i, f in enumerate(videos):
                if f.name == last_played:
                    next_index = i + 1
                    break

        if next_index >= len(videos):
            # Current folder exhausted — advance to the next one
            next_folder_index = (folder_index + 1) % len(folder_entries)
            log.info(
                f"All videos played in {folder_path}"
                + (f" — advancing to {folder_entries[next_folder_index]['path']}"
                   if len(folder_entries) > 1 else " — wrapping back to first")
            )
            folder_index = next_folder_index
            last_played  = None
            continue

        total    = len(videos)
        selected = [videos[(next_index + offset) % total] for offset in range(count)]
        return selected, folder_index, folder_path

    log.error("No playable videos found in any configured folder.")
    return [], 0, folder_entries[0]["path"]


def get_next_videos_for_window(
    folder_entries: list, state: dict, extensions: list, window_seconds: float,
) -> tuple:
    """
    Select enough videos from the current folder to fill window_seconds of
    content, honouring resume_offset from a previous session.

    resume_offset is the number of seconds that were played beyond end_time in
    the last session.  The first video of this session is seeked forward by
    that amount, so the scheduler stays aligned with the configured time window
    across sessions.

    Returns (videos, folder_index, folder_path, resume_offset_used, new_resume_offset).
    new_resume_offset is the overshoot to carry into the next session (>=0).
    """
    state_key   = folder_entries[0]["path"]
    entry_state = state.get(state_key, {})
    if isinstance(entry_state, str):
        entry_state = {"folder_index": 0, "last_played": entry_state}

    folder_index  = entry_state.get("folder_index", 0) % len(folder_entries)
    last_played   = entry_state.get("last_played")
    resume_offset = float(entry_state.get("resume_offset", 0.0))

    ext_set = {e.lower() for e in extensions}

    for _ in range(len(folder_entries)):
        fe          = folder_entries[folder_index]
        folder_path = fe["path"]
        folder      = Path(folder_path)

        if not folder.exists():
            log.error(f"Folder not found: {folder_path} — skipping to next")
            folder_index  = (folder_index + 1) % len(folder_entries)
            last_played   = None
            resume_offset = 0.0
            continue

        videos = sorted(
            [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in ext_set],
            key=_natural_sort_key,
        )

        if not videos:
            log.error(f"No video files in: {folder_path} — skipping to next")
            folder_index  = (folder_index + 1) % len(folder_entries)
            last_played   = None
            resume_offset = 0.0
            continue

        next_index = 0
        if last_played:
            for i, f in enumerate(videos):
                if f.name == last_played:
                    next_index = i + 1
                    break

        if next_index >= len(videos):
            next_folder_index = (folder_index + 1) % len(folder_entries)
            log.info(
                f"All videos played in {folder_path}"
                + (f" — advancing to {folder_entries[next_folder_index]['path']}"
                   if len(folder_entries) > 1 else " — wrapping back to first")
            )
            folder_index  = next_folder_index
            last_played   = None
            resume_offset = 0.0
            continue

        # Pick enough videos (wrapping within the folder) to fill window_seconds.
        # The first video only contributes (duration - resume_offset) of content
        # because VLC will seek into it.
        total          = len(videos)
        selected       = []
        total_duration = 0.0
        for offset in range(total):
            idx   = (next_index + offset) % total
            video = videos[idx]
            dur   = get_video_duration(video)
            selected.append(video)
            total_duration += dur
            # Effective content played = total_duration - resume_offset
            if total_duration - resume_offset >= window_seconds:
                break

        new_resume_offset = max(0.0, total_duration - resume_offset - window_seconds)
        return selected, folder_index, folder_path, resume_offset, new_resume_offset

    log.error("No playable videos found in any configured folder.")
    return [], 0, folder_entries[0]["path"], 0.0, 0.0


# ── Hooks ─────────────────────────────────────────────────────────────────────

def _run_hook(cmd: str) -> None:
    """Run an optional shell hook, logging errors without crashing."""
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    try:
        result = subprocess.run(cmd, shell=True, timeout=10, env=env)
        if result.returncode != 0:
            log.warning(f"Hook exited {result.returncode}: {cmd!r}")
    except subprocess.TimeoutExpired:
        log.warning(f"Hook timed out: {cmd!r}")
    except Exception:
        log.exception(f"Hook failed: {cmd!r}")


# ── Playback ──────────────────────────────────────────────────────────────────

def play_videos(folder_entries: list, vlc_path: str, extensions: list,
                before_play: Optional[str] = None,
                window_seconds: Optional[float] = None) -> None:
    global _active_proc

    state = load_state()

    if window_seconds is not None:
        videos, folder_index, folder_path, resume_offset, new_resume_offset = \
            get_next_videos_for_window(folder_entries, state, extensions, window_seconds)
    else:
        videos, folder_index, folder_path = get_next_videos(folder_entries, state, extensions)
        resume_offset     = 0.0
        new_resume_offset = 0.0

    if not videos:
        return

    names = ", ".join(v.name for v in videos)

    if _dry_run:
        log.info(f"[DRY RUN] Would launch VLC → {names}")
        if resume_offset > 0:
            log.info(f"[DRY RUN] First video seeked to {resume_offset:.1f}s (carried from last session)")
        return

    log.info(f"Launching VLC → {names}")
    if resume_offset > 0:
        log.info(f"First episode seeked to {resume_offset:.1f}s (overshoot from last session)")

    # Kill any running VLC — use pkill so it works across process boundaries
    # (e.g. --play-now spawns a fresh process where _active_proc is always None)
    result = subprocess.run(["pkill", "vlc"], capture_output=True)
    if result.returncode == 0:
        log.info("Killed existing VLC instance.")
        time.sleep(1)

    # Pre-play hook (e.g. disable screensaver)
    if before_play:
        log.info(f"Running before_play hook: {before_play!r}")
        _run_hook(before_play)

    # Build the per-item VLC argument list.
    # `:start-time=X` is a per-item VLC option that applies only to the preceding file.
    vlc_items: list[str] = []
    for i, v in enumerate(videos):
        vlc_items.append(str(v))
        if i == 0 and resume_offset > 0:
            vlc_items.append(f":start-time={resume_offset:.3f}")

    try:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        _active_proc = subprocess.Popen(
            [
                vlc_path,
                "--fullscreen",
                "--play-and-exit",
                "--no-video-title-show",
                "--vout", "gl",
                "--avcodec-hw", "vaapi",
                *vlc_items,
            ],
            env=env,
            stderr=subprocess.DEVNULL,
        )
        state_key = folder_entries[0]["path"]
        new_state = {"folder_index": folder_index, "last_played": videos[-1].name}
        if window_seconds is not None:
            new_state["resume_offset"] = round(new_resume_offset, 3)
            log.info(
                f"Next session resume_offset: {new_resume_offset:.1f}s"
                + (" (first episode will be seeked)" if new_resume_offset > 0 else " (starts from beginning)")
            )
        state[state_key] = new_state
        save_state(state)
    except FileNotFoundError:
        log.error(f"VLC executable not found at: {vlc_path}  — update config.json")
    except Exception:
        log.exception("Unexpected error while launching VLC")


# ── Status HTTP endpoint ──────────────────────────────────────────────────────

class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        state   = load_state()
        def _schedule_status(entry):
            fes         = get_folder_entries(entry)
            state_key   = fes[0]["path"]
            entry_state = state.get(state_key, {})
            if isinstance(entry_state, str):
                entry_state = {"folder_index": 0, "last_played": entry_state}
            folder_index = entry_state.get("folder_index", 0) % len(fes)
            result = {
                "time":          entry["time"],
                "folders":       fes,
                "active_folder": fes[folder_index]["path"],
                "last_played":   entry_state.get("last_played"),
            }
            if entry.get("end_time"):
                result["end_time"]      = entry["end_time"]
                result["resume_offset"] = entry_state.get("resume_offset", 0.0)
            return result

        payload = {
            "vlc_running": _active_proc is not None and _active_proc.poll() is None,
            "schedules":   [_schedule_status(e) for e in _current_config.get("schedules", [])],
        }
        body = json.dumps(payload, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress per-request access log noise


class _ReusePortHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True
    allow_reuse_port    = True


def _start_status_server(port: int) -> None:
    for attempt in range(2):
        try:
            server = _ReusePortHTTPServer(("127.0.0.1", port), _StatusHandler)
            log.info(f"Status endpoint: http://127.0.0.1:{port}/")
            server.serve_forever()
            return
        except OSError:
            if attempt == 0:
                # Another instance is holding the port — release it and retry
                log.info(f"Port {port} in use — releasing it and retrying.")
                subprocess.run(["fuser", "-k", f"{port}/tcp"],
                               capture_output=True)
                time.sleep(1)
            else:
                log.warning(f"Could not start status server on port {port} — continuing without it.")


# ── Schedule registration ─────────────────────────────────────────────────────

def _register_schedules(config: dict) -> None:
    """Clear all jobs and re-register from config."""
    schedule.clear()
    vlc_path   = detect_vlc(config.get("vlc_path", "auto"))
    extensions = config.get("video_extensions", DEFAULT_CONFIG["video_extensions"])

    for entry in config["schedules"]:
        t           = entry["time"]
        end_time    = entry.get("end_time")
        fes         = get_folder_entries(entry)
        before_play = entry.get("before_play")

        window_seconds: Optional[float] = None
        if end_time:
            ws = _time_to_seconds(end_time) - _time_to_seconds(t)
            if ws > 0:
                window_seconds = float(ws)

        for fe in fes:
            if window_seconds is not None:
                log.info(f"  Registered  {t}–{end_time}  →  {fe['path']}  (window={int(window_seconds//60)}m, count=auto)")
            else:
                log.info(f"  Registered  {t}  →  {fe['path']}  (count={fe['count']})")
        schedule.every().day.at(t).do(
            play_videos, fes, vlc_path, extensions, before_play, window_seconds
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global _dry_run, _current_config

    parser = argparse.ArgumentParser(description="VLC Scheduler")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would play at each scheduled time without launching VLC",
    )
    parser.add_argument(
        "--play-now", metavar="FOLDER",
        help="Immediately play the next video(s) from FOLDER and exit",
    )
    parser.add_argument(
        "--peek", metavar="HH:MM",
        help="Show which video(s) would be played at the given scheduled time, without changing state",
    )
    parser.add_argument(
        "--advance", metavar="HH:MM",
        help="Simulate one playback at the given scheduled time: advance state without launching VLC",
    )
    parser.add_argument(
        "--play-file", metavar="FILE",
        help="Immediately play a specific video file and exit (does not affect playback state)",
    )
    args = parser.parse_args()
    _dry_run = args.dry_run

    log.info("=" * 60)
    log.info("VLC Scheduler starting" + (" (DRY RUN)" if _dry_run else ""))

    config          = load_config()
    _current_config = config

    # Validate on startup (skip folder checks in dry-run so it can be used
    # before folders are fully populated)
    if not _dry_run:
        if not validate_config(config):
            sys.exit(1)

    vlc_path   = detect_vlc(config.get("vlc_path", "auto"))
    extensions = config.get("video_extensions", DEFAULT_CONFIG["video_extensions"])
    log.info(f"VLC: {vlc_path}")

    # --play-now: fire immediately and exit
    if args.play_now:
        # Find matching schedule entry (by folder path appearing anywhere in folders list)
        entry = next(
            (e for e in config["schedules"]
             if args.play_now in [fe["path"] for fe in get_folder_entries(e)]),
            None,
        )
        fes          = get_folder_entries(entry) if entry else [{"path": args.play_now, "count": 1}]
        window_secs  = None
        if entry and entry.get("end_time"):
            ws = _time_to_seconds(entry["end_time"]) - _time_to_seconds(entry["time"])
            if ws > 0:
                window_secs = float(ws)
        play_videos(fes, vlc_path, extensions, (entry or {}).get("before_play"), window_secs)
        return

    # --peek: show next video(s) for a scheduled time without changing state
    if args.peek:
        entry = next((e for e in config["schedules"] if e["time"] == args.peek), None)
        if not entry:
            print(f"No schedule found for time: {args.peek}")
            sys.exit(1)
        fes      = get_folder_entries(entry)
        state    = load_state()
        end_time = entry.get("end_time")
        if end_time:
            ws = _time_to_seconds(end_time) - _time_to_seconds(entry["time"])
            videos, _, folder_path, resume_offset, new_resume_offset = \
                get_next_videos_for_window(fes, state, extensions, float(ws))
            print(f"Schedule {args.peek}–{end_time}  →  folder: {folder_path}")
            print(f"Resume offset: {resume_offset:.1f}s  |  Next session offset: {new_resume_offset:.1f}s")
        else:
            videos, _, folder_path = get_next_videos(fes, state, extensions)
            print(f"Schedule {args.peek}  →  folder: {folder_path}")
        for v in videos:
            dur = f"  ({get_video_duration(v):.0f}s)" if end_time else ""
            print(f"  {v.name}{dur}")
        return

    # --advance: advance state as if one playback ran at a scheduled time
    if args.advance:
        entry = next((e for e in config["schedules"] if e["time"] == args.advance), None)
        if not entry:
            print(f"No schedule found for time: {args.advance}")
            sys.exit(1)
        fes      = get_folder_entries(entry)
        state    = load_state()
        end_time = entry.get("end_time")
        if end_time:
            ws = _time_to_seconds(end_time) - _time_to_seconds(entry["time"])
            videos, folder_index, folder_path, resume_offset, new_resume_offset = \
                get_next_videos_for_window(fes, state, extensions, float(ws))
            if not videos:
                print("No videos to advance.")
                sys.exit(1)
            state[fes[0]["path"]] = {
                "folder_index": folder_index,
                "last_played":  videos[-1].name,
                "resume_offset": round(new_resume_offset, 3),
            }
            save_state(state)
            print(f"Simulated playback at {args.advance}–{end_time}  →  folder: {folder_path}")
            for v in videos:
                print(f"  {v.name}")
            print(f"Resume offset saved: {new_resume_offset:.1f}s")
        else:
            videos, folder_index, folder_path = get_next_videos(fes, state, extensions)
            if not videos:
                print("No videos to advance.")
                sys.exit(1)
            state[fes[0]["path"]] = {"folder_index": folder_index, "last_played": videos[-1].name}
            save_state(state)
            print(f"Simulated playback at {args.advance}  →  folder: {folder_path}")
            for v in videos:
                print(f"  {v.name}")
        print("State updated.")
        return

    # --play-file: play one specific file immediately and exit
    if args.play_file:
        file_path = Path(args.play_file)
        if not file_path.is_file():
            print(f"File not found: {args.play_file}")
            sys.exit(1)
        subprocess.run(["pkill", "vlc"], capture_output=True)
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        subprocess.Popen(
            [
                vlc_path,
                "--fullscreen",
                "--play-and-exit",
                "--no-video-title-show",
                "--vout", "gl",
                "--avcodec-hw", "vaapi",
                str(file_path),
            ],
            env=env,
            stderr=subprocess.DEVNULL,
        ).wait()
        return

    # Status endpoint in a background daemon thread
    port = config.get("status_port", 8765)
    threading.Thread(target=_start_status_server, args=(port,), daemon=True).start()

    _register_schedules(config)
    log.info("Scheduler running — waiting for scheduled times.")

    while True:
        schedule.run_pending()

        # Hot-reload: pick up config.json changes without restarting
        if config_changed():
            log.info("config.json changed — reloading schedules.")
            config          = load_config()
            _current_config = config
            if validate_config(config):
                _register_schedules(config)
            else:
                log.warning("Reload skipped — fix the errors in config.json.")

        time.sleep(30)


if __name__ == "__main__":
    main()
