#!/usr/bin/env python3
"""
VLC Scheduler
Plays the next numbered video(s) from a designated folder at scheduled times.
State (last played index per folder) is persisted in playback_state.json.
"""

import argparse
import contextlib
import datetime
import fcntl
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
BASE_DIR        = Path(__file__).parent
CONFIG_FILE     = BASE_DIR / "config.json"
STATE_FILE      = BASE_DIR / "playback_state.json"
STATE_TMP_FILE  = BASE_DIR / "playback_state.json.tmp"
STATE_LOCK_FILE = BASE_DIR / "playback_state.lock"
LOG_FILE        = BASE_DIR / "vlc_scheduler.log"

# A VLC session shorter than this never corresponds to a real slot (the
# shortest configured window is an hour), so it means VLC crashed or was quit
# moments after launch.  Such a session does not confirm a rotation.
ABNORMAL_EXIT_SECONDS = 60.0

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
_state_lock = threading.Lock()

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

    schedule_times = {e["time"] for e in config.get("schedules", [])}
    for entry in config.get("schedules", []):
        if "mirror" in entry:
            ref = entry["mirror"]
            if ref not in schedule_times:
                errors.append(f"  Schedule {entry['time']}: mirror target '{ref}' not found")
            continue

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
    """Write the state file atomically: a crash mid-write cannot truncate it."""
    with open(STATE_TMP_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(STATE_TMP_FILE, STATE_FILE)


@contextlib.contextmanager
def state_transaction():
    """Load the state, yield it for mutation, then save it atomically.

    EVERY writer must go through this.  The state file is rewritten whole, so
    a writer that snapshots it early and saves later silently reverts every
    key another writer touched in between — which is exactly how a slot's
    launch used to erase the previous slot's rotation commit.

    Held under a thread lock (for _on_vlc_exit threads inside this process)
    and an flock (for a second process, e.g. --play-now, run by hand while the
    daemon is up).  The lock lives in its own file because save_state()
    replaces the state file's inode, which would drop an flock taken on it.
    """
    with _state_lock:
        with open(STATE_LOCK_FILE, "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                state = load_state()
                yield state
                save_state(state)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)


# ── Video selection ───────────────────────────────────────────────────────────

def _natural_sort_key(path: Path):
    """Natural-sort: '2.webm' < '10.webm', 'ep02.mkv' < 'ep10.mkv'."""
    parts = re.split(r"(\d+)", path.stem)
    return [int(c) if c.isdigit() else c.lower() for c in parts]


def _time_to_seconds(t: str) -> int:
    """Convert 'HH:MM' to seconds since midnight."""
    h, m = t.split(":")
    return int(h) * 3600 + int(m) * 60


def _window_seconds(entry: dict) -> Optional[float]:
    """Length of a slot's play window in seconds, or None if it has no end_time."""
    end_time = entry.get("end_time")
    if not end_time:
        return None
    try:
        ws = _time_to_seconds(end_time) - _time_to_seconds(entry["time"])
    except (ValueError, KeyError):
        return None
    return float(ws) if ws > 0 else None


def _find_schedule(config: dict, ident: str) -> tuple:
    """Resolve 'HH:MM' or a folder path to the slot that owns the rotation.

    Returns (primary_entry, mirror_entry_or_None).  A mirror slot has no
    folders of its own -- it replays whatever the primary it points at played
    -- so state operations on a mirror have to be redirected to that primary.
    """
    entry = next((e for e in config.get("schedules", []) if e["time"] == ident), None)
    if entry is None:
        entry = next(
            (e for e in config.get("schedules", [])
             if "mirror" not in e
             and ident in [fe["path"] for fe in get_folder_entries(e)]),
            None,
        )
    if entry is None:
        return None, None
    if "mirror" in entry:
        primary = next(
            (e for e in config.get("schedules", []) if e["time"] == entry["mirror"]), None
        )
        return primary, entry
    return entry, None


def _slot_sequence(folder_entries: list, extensions: list) -> list:
    """[(folder_index, Path), ...] in the order this slot plays them.

    A slot's folders are played as one concatenated run -- all of folder 0,
    then all of folder 1 -- so flattening them gives a single line to move
    along, and stepping back off the front of one folder lands on the tail of
    the one before it.
    """
    ext_set = {e.lower() for e in extensions}
    sequence = []
    for index, fe in enumerate(folder_entries):
        folder = Path(fe["path"])
        if not folder.exists():
            log.warning(f"Folder not found, skipping: {fe['path']}")
            continue
        videos = sorted(
            [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in ext_set],
            key=_natural_sort_key,
        )
        sequence.extend((index, v) for v in videos)
    return sequence


def _state_to_cursor(sequence: list, folder_index: int, last_played: Optional[str]) -> int:
    """Position in `sequence` of the episode this slot would play next."""
    if last_played is not None:
        for i, (fi, video) in enumerate(sequence):
            if fi == folder_index and video.name == last_played:
                return i + 1
        log.warning(
            f"last_played {last_played!r} is no longer in folder {folder_index} — "
            f"treating the slot as sitting at that folder's first episode"
        )
    for i, (fi, _video) in enumerate(sequence):
        if fi == folder_index:
            return i
    return 0


def _cursor_to_state(sequence: list, cursor: int) -> tuple:
    """Inverse of _state_to_cursor: (folder_index, last_played) for a position.

    last_played is the episode *before* the cursor within the same folder, or
    None when the cursor sits on a folder's first episode -- which is how the
    selectors spell "start of this folder".  A cursor past the end means the
    last folder is exhausted, which is what makes the next session roll over.
    """
    if cursor >= len(sequence):
        folder_index, video = sequence[-1]
        return folder_index, video.name
    folder_index, _video = sequence[cursor]
    if cursor > 0 and sequence[cursor - 1][0] == folder_index:
        return folder_index, sequence[cursor - 1][1].name
    return folder_index, None


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

        # Pick enough videos, from next_index to the end of the folder, to fill
        # window_seconds.  Do NOT wrap back to the start of the same folder: if
        # the folder runs out first, selection stops there so last_played lands
        # on the true final video, which correctly signals "folder exhausted"
        # and lets the next session advance to the next folder in the list.
        # The first video only contributes (duration - resume_offset) of content
        # because VLC will seek into it.
        total          = len(videos)
        selected       = []
        durations      = []
        total_duration = 0.0
        for idx in range(next_index, total):
            video = videos[idx]
            dur   = get_video_duration(video)
            selected.append(video)
            durations.append(dur)
            total_duration += dur
            # Effective content played = total_duration - resume_offset
            if total_duration - resume_offset >= window_seconds:
                break

        new_resume_offset = max(0.0, total_duration - resume_offset - window_seconds)

        episode_list = ", ".join(
            f"{v.name} ({d:.0f}s)" for v, d in zip(selected, durations)
        )
        log.info(
            f"Window {window_seconds/60:.0f}m: {len(selected)} episode(s) selected"
            f" — {episode_list}"
            f" — total {total_duration:.0f}s, offset_in={resume_offset:.1f}s"
            f" → content {total_duration - resume_offset:.0f}s"
            f", offset_out={new_resume_offset:.1f}s"
        )
        return selected, folder_index, folder_path, resume_offset, new_resume_offset

    log.error("No playable videos found in any configured folder.")
    return [], 0, folder_entries[0]["path"], 0.0, 0.0


# ── VLC process teardown ──────────────────────────────────────────────────────

def kill_vlc() -> None:
    """Terminate all VLC processes and block until they are gone."""
    global _active_proc

    # Terminate the handle we have (if any)
    if _active_proc is not None:
        try:
            if _active_proc.poll() is None:
                _active_proc.terminate()
                log.info(f"Sent SIGTERM to tracked VLC process (pid={_active_proc.pid})")
        except Exception:
            log.exception("Error terminating tracked VLC process")
        _active_proc = None

    # Kill any other VLC processes by name (survives restarts / multiple instances)
    try:
        result = subprocess.run(["pkill", "vlc"], capture_output=True)
        if result.returncode == 0:
            log.info("pkill vlc: signalled running VLC processes")
        elif result.returncode != 1:
            log.warning(f"pkill vlc returned unexpected code {result.returncode}")
    except FileNotFoundError:
        log.warning("pkill not found — falling back to killall")
        try:
            subprocess.run(["killall", "vlc"], capture_output=True)
        except Exception:
            log.exception("killall vlc also failed")
    except Exception:
        log.exception("pkill vlc raised an unexpected error")

    # Wait up to 5 s for VLC to actually exit
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if subprocess.run(["pgrep", "vlc"], capture_output=True).returncode != 0:
            log.info("VLC has terminated.")
            return
        time.sleep(0.3)

    # Still alive — escalate to SIGKILL
    log.warning("VLC did not exit after 5 s — sending SIGKILL")
    try:
        subprocess.run(["pkill", "-9", "vlc"], capture_output=True)
    except Exception:
        log.exception("pkill -9 vlc failed")

    time.sleep(1.0)
    if subprocess.run(["pgrep", "vlc"], capture_output=True).returncode == 0:
        log.error("VLC still running after SIGKILL — proceeding anyway")
    else:
        log.info("VLC forcefully terminated.")


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
                window_seconds: Optional[float] = None,
                is_mirror: bool = False,
                wait_for_exit: bool = False) -> None:
    global _active_proc

    state     = load_state()
    state_key = folder_entries[0]["path"]

    if is_mirror:
        # Mirror slot: replay the previous calendar day's primary session.
        # If the primary has already fired today, its session is in last_session_* but
        # we want yesterday's — stored in prev_session_*.  If the primary hasn't fired
        # today yet, last_session_* already holds yesterday's data and is correct.
        entry_state = state.get(state_key, {})
        if isinstance(entry_state, str):
            entry_state = {}

        today_str = datetime.date.today().isoformat()
        primary_fired_today = entry_state.get("last_session_date") == today_str

        if primary_fired_today:
            last_folder   = entry_state.get("prev_session_folder")
            last_names    = entry_state.get("prev_session_videos", [])
            resume_offset = float(entry_state.get("prev_session_resume_offset", 0.0))
        else:
            last_folder   = entry_state.get("last_session_folder")
            last_names    = entry_state.get("last_session_videos", [])
            resume_offset = float(entry_state.get("last_session_resume_offset", 0.0))

        if last_names and last_folder:
            log.info(f"[MIRROR] Replaying previous day session — {len(last_names)} episode(s) from {last_folder}")
            videos = [Path(last_folder) / name for name in last_names]
        else:
            log.info("[MIRROR] No previous day session recorded yet — computing same selection as primary")
            if window_seconds is not None:
                videos, _, _, resume_offset, _ = \
                    get_next_videos_for_window(folder_entries, state, extensions, window_seconds)
            else:
                videos, _, _ = get_next_videos(folder_entries, state, extensions)
                resume_offset = 0.0
    else:
        # get_next_videos_for_window/get_next_videos only ever read the live
        # folder_index/last_played/resume_offset fields, which are only
        # advanced once a session is confirmed (see _on_vlc_exit below and
        # the comment above new_state further down).  If the previous
        # session for this state_key never got to confirm — scheduler or
        # machine died before VLC exited, e.g. a power cut — those fields
        # are still exactly where they were before that attempt, so this
        # call naturally re-selects the same interrupted batch instead of
        # skipping past it.
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
        prefix = "[DRY RUN][MIRROR] " if is_mirror else "[DRY RUN] "
        log.info(f"{prefix}Would launch VLC → {names}")
        if window_seconds is not None and not is_mirror:
            log.info(
                f"[DRY RUN] offset_in={resume_offset:.1f}s"
                f", offset_out={new_resume_offset:.1f}s"
                + (" (first episode seeked)" if resume_offset > 0 else "")
            )
        elif resume_offset > 0:
            log.info(f"[DRY RUN] First episode seeked to {resume_offset:.1f}s")
        return

    prefix = "[MIRROR] " if is_mirror else ""
    log.info(f"{prefix}Launching VLC → {names}")
    if resume_offset > 0:
        log.info(f"First episode seeked to {resume_offset:.1f}s")

    kill_vlc()

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

        if is_mirror:
            log.info("[MIRROR] State unchanged — primary slot manages progression")
            return

        launched_at = time.monotonic()
        today_str   = datetime.date.today().isoformat()

        if window_seconds is not None and new_resume_offset > 0:
            # Last selected episode was interrupted when the slot window ended (VLC
            # is killed by the next slot).  Save state so the SAME episode is
            # replayed next session from the point where it was cut off, not skipped.
            last_ep_dur      = get_video_duration(videos[-1])
            seek_into_last   = max(0.0, last_ep_dur - new_resume_offset)
            # Find the true predecessor of the interrupted episode in the sorted
            # folder list.  videos[-2] is wrong when the selection loop wrapped
            # around to index 0: it would be the last file in the folder, which
            # triggers a false exhaustion on the next session and skips the episode.
            # Setting last_played=None when the interrupted episode is first (idx 0)
            # causes next_index=0, so the episode is re-selected with the saved offset.
            ext_set_scan  = {e.lower() for e in extensions}
            all_sorted    = sorted(
                [f for f in Path(folder_path).iterdir()
                 if f.is_file() and f.suffix.lower() in ext_set_scan],
                key=_natural_sort_key,
            )
            interrupted_idx  = next(
                (i for i, f in enumerate(all_sorted) if f.name == videos[-1].name), -1
            )
            last_played_save = (
                all_sorted[interrupted_idx - 1].name if interrupted_idx > 0 else None
            )
            resume_offset_save = round(seek_into_last, 3)
            log.info(
                f"Last episode interrupted — next session resumes {videos[-1].name}"
                f" at {seek_into_last:.1f}s"
            )
        else:
            last_played_save   = videos[-1].name
            resume_offset_save = 0.0
            if window_seconds is not None:
                log.info("Next session starts from beginning (no overshoot)")

        session_at = datetime.datetime.now().isoformat()

        # Re-read the state under the lock rather than reusing the snapshot
        # taken at the top of this function.  kill_vlc() above has just made
        # the previous slot's VLC exit, so that slot's _on_vlc_exit thread is
        # committing its rotation right about now.  The file is rewritten
        # whole, so saving the stale snapshot here would revert that commit —
        # which is what froze every folder on the same episodes.
        with state_transaction() as fresh_state:
            old_state = fresh_state.get(state_key, {})
            if isinstance(old_state, str):
                old_state = {}

            # Archive the previous session so mirrors can always replay yesterday's run.
            # Only rotate when the date changes to avoid overwriting prev with today's.
            old_date = old_state.get("last_session_date")
            if old_date and old_date != today_str and old_state.get("last_session_videos"):
                prev = {
                    "prev_session_folder":        old_state["last_session_folder"],
                    "prev_session_videos":        old_state["last_session_videos"],
                    "prev_session_resume_offset": old_state.get("last_session_resume_offset", 0.0),
                }
            else:
                prev = {
                    k: old_state[k] for k in (
                        "prev_session_folder", "prev_session_videos", "prev_session_resume_offset"
                    ) if k in old_state
                }

            # folder_index / last_played / resume_offset are left at their
            # last CONFIRMED values (carried over from old_state) — NOT
            # advanced yet.  The predicted post-session values go into
            # pending_* and are only promoted to the live fields by
            # _finalize_pending(), once _on_vlc_exit confirms VLC actually
            # exited.  This is what lets a hard interruption (e.g. a power
            # failure) resume by re-playing the in-flight batch instead of
            # silently skipping past it.
            new_state = {
                **prev,
                "folder_index":               old_state.get("folder_index", 0),
                "last_played":                old_state.get("last_played"),
                "last_session_date":          today_str,
                "last_session_at":            session_at,
                "session_completed":          False,
                "last_session_folder":        folder_path,
                "last_session_videos":        [v.name for v in videos],
                "last_session_resume_offset": round(resume_offset, 3),
                "pending_folder_index":       folder_index,
                "pending_last_played":        last_played_save,
            }
            if window_seconds is not None:
                new_state["resume_offset"]         = old_state.get("resume_offset", 0.0)
                new_state["pending_resume_offset"] = resume_offset_save
            fresh_state[state_key] = new_state

        if wait_for_exit:
            # One-shot invocation (--play-now): confirm the session in the
            # foreground.  A daemon thread would die the instant main() returns,
            # leaving pending_* unpromoted and this folder frozen forever.
            _on_vlc_exit(_active_proc, state_key, session_at, launched_at)
        else:
            threading.Thread(
                target=_on_vlc_exit,
                args=(_active_proc, state_key, session_at, launched_at),
                daemon=True,
            ).start()

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


# ── Startup catch-up ─────────────────────────────────────────────────────────

def _finalize_pending(entry: dict) -> None:
    """Promote a session's predicted rotation (pending_*) to the live fields.

    Only called from _on_vlc_exit once VLC has actually, observably exited —
    naturally or via a deliberate kill_vlc() from the next slot. Mutates
    entry in place.
    """
    if "pending_folder_index" in entry:
        entry["folder_index"] = entry.pop("pending_folder_index")
    if "pending_last_played" in entry:
        entry["last_played"] = entry.pop("pending_last_played")
    if "pending_resume_offset" in entry:
        entry["resume_offset"] = entry.pop("pending_resume_offset")
    entry["session_completed"] = True


def _apply_manual_advance(state: dict, state_key: str, folder_index: int,
                          last_played: str, resume_offset: Optional[float] = None) -> None:
    """Force the live rotation fields forward for --advance.

    Merges into the existing entry instead of replacing it, so the
    last_session_*/prev_session_* history mirrors depend on survives.  Any
    pending_* left over is dropped: it belongs to a session that is now
    superseded, and promoting it later would undo this advance.
    """
    entry = state.get(state_key, {})
    if not isinstance(entry, dict):
        entry = {}
    for key in ("pending_folder_index", "pending_last_played", "pending_resume_offset"):
        entry.pop(key, None)
    entry["folder_index"]      = folder_index
    entry["last_played"]       = last_played
    entry["session_completed"] = True
    if resume_offset is not None:
        entry["resume_offset"] = resume_offset
    state[state_key] = entry


def _on_vlc_exit(proc: subprocess.Popen, state_key: str, session_at: str,
                 launched_at: float) -> None:
    """Confirm the session once VLC has actually exited.

    Waits without a timeout on purpose: the wait must end when VLC ends, never
    on a clock.  A slot that plays past its window (the 21:00 one runs until
    the 06:00 mirror kills it) used to trip a 3 h timeout and get marked
    confirmed while it was still playing.

    session_at is the last_session_at value written when this session started.
    The write is skipped if a newer session has already replaced it, which
    prevents a finishing slot from clobbering the 'completed=False' marker
    written by a back-to-back slot that started at the same instant.
    """
    proc.wait()

    elapsed = time.monotonic() - launched_at
    if elapsed < ABNORMAL_EXIT_SECONDS:
        # No configured slot is this short, so VLC crashed or was quit right
        # after launch.  Leaving pending_* unpromoted makes the next session
        # replay this batch instead of rotating past content nobody watched.
        log.warning(
            f"VLC exited after only {elapsed:.1f}s — treating {state_key} as "
            f"interrupted; rotation NOT advanced, this batch will replay."
        )
        return

    try:
        with state_transaction() as state:
            entry = state.get(state_key, {})
            if isinstance(entry, dict) and entry.get("last_session_at") == session_at:
                _finalize_pending(entry)
                state[state_key] = entry
                log.info(
                    f"Session confirmed for {state_key} after {elapsed/60:.0f}m — "
                    f"rotation advanced to last_played={entry.get('last_played')!r}"
                )
            else:
                log.info(
                    f"Session for {state_key} superseded by a newer one — "
                    f"leaving rotation to it."
                )
    except Exception:
        log.exception("Error marking session complete")


def startup_catchup(config: dict, vlc_path: str, extensions: list) -> None:
    """On startup, play the most recently missed or interrupted schedule slot."""
    now   = datetime.datetime.now()
    state = load_state()

    candidates = []
    for entry in config.get("schedules", []):
        if "mirror" in entry:
            continue
        h, m = entry["time"].split(":")
        today_dt     = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        scheduled_dt = today_dt if today_dt <= now else today_dt - datetime.timedelta(days=1)
        candidates.append((scheduled_dt, entry))

    for scheduled_dt, entry in sorted(candidates, key=lambda x: x[0], reverse=True):
        fes       = get_folder_entries(entry)
        state_key = fes[0]["path"]
        es        = state.get(state_key, {})
        if isinstance(es, str):
            es = {}

        last_at = es.get("last_session_at")
        if not last_at:
            continue  # never played before — nothing to catch up

        last_dt   = datetime.datetime.fromisoformat(last_at)
        completed = es.get("session_completed", True)

        missed      = last_dt < scheduled_dt
        interrupted = (not missed) and not completed

        if missed or interrupted:
            reason = "missed" if missed else "interrupted"
            log.info(
                f"Startup catch-up: slot {entry['time']} was {reason} "
                f"(last played {last_at}, slot was {scheduled_dt.strftime('%Y-%m-%d %H:%M')})"
            )
            play_videos(fes, vlc_path, extensions, entry.get("before_play"),
                        _window_seconds(entry))
            return

    log.info("Startup: no missed slots — resuming normal schedule.")


# ── Schedule registration ─────────────────────────────────────────────────────

def _register_schedules(config: dict) -> None:
    """Clear all jobs and re-register from config."""
    schedule.clear()
    vlc_path   = detect_vlc(config.get("vlc_path", "auto"))
    extensions = config.get("video_extensions", DEFAULT_CONFIG["video_extensions"])

    for entry in config["schedules"]:
        t           = entry["time"]
        before_play = entry.get("before_play")

        # Mirror slot: delegate folder/window config to the referenced primary slot
        if "mirror" in entry:
            ref_time  = entry["mirror"]
            ref_entry = next((e for e in config["schedules"] if e["time"] == ref_time), None)
            if ref_entry is None:
                log.error(f"  Schedule {t}: mirror target '{ref_time}' not found — skipping")
                continue
            fes         = get_folder_entries(ref_entry)
            window_seconds = _window_seconds(ref_entry)
            if before_play is None:
                before_play = ref_entry.get("before_play")
            log.info(f"  Registered  {t}  →  mirror of {ref_time}  ({fes[0]['path']})")
            schedule.every().day.at(t).do(
                play_videos, fes, vlc_path, extensions, before_play, window_seconds, True
            )
            continue

        end_time       = entry.get("end_time")
        fes            = get_folder_entries(entry)
        window_seconds = _window_seconds(entry)

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
        "--shift", nargs=2, metavar=("SLOT", "N"),
        help="Move a slot's position by N episodes without playing anything. "
             "SLOT is a schedule time (HH:MM) or a folder path; N is signed, "
             "e.g. --shift 15:00 +2  or  --shift 15:00 -1",
    )
    parser.add_argument(
        "--keep-offset", action="store_true",
        help="With --shift, keep the current resume_offset instead of resetting "
             "it to 0 (keeps the slot aligned to its time window)",
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
        window_secs  = _window_seconds(entry) if entry else None
        # wait_for_exit: block until VLC ends so the session is confirmed before
        # this process goes away.  Without it the rotation is left pending and
        # this folder replays the same episodes on every future run.
        play_videos(fes, vlc_path, extensions, (entry or {}).get("before_play"),
                    window_secs, wait_for_exit=True)
        return

    # --peek: show next video(s) for a scheduled time without changing state
    if args.peek:
        entry, mirror = _find_schedule(config, args.peek)
        if not entry:
            print(f"No schedule found for: {args.peek}")
            sys.exit(1)
        if mirror:
            print(f"{args.peek} mirrors {entry['time']} — showing {entry['time']}.")
        fes      = get_folder_entries(entry)
        state    = load_state()
        end_time = entry.get("end_time")
        ws       = _window_seconds(entry)
        if ws is not None:
            videos, _, folder_path, resume_offset, new_resume_offset = \
                get_next_videos_for_window(fes, state, extensions, ws)
            print(f"Schedule {entry['time']}–{end_time}  →  folder: {folder_path}")
            print(f"Resume offset: {resume_offset:.1f}s  |  Next session offset: {new_resume_offset:.1f}s")
        else:
            videos, _, folder_path = get_next_videos(fes, state, extensions)
            print(f"Schedule {entry['time']}  →  folder: {folder_path}")
        for v in videos:
            dur = f"  ({get_video_duration(v):.0f}s)" if end_time else ""
            print(f"  {v.name}{dur}")
        return

    # --advance: advance state as if one playback ran at a scheduled time
    if args.advance:
        entry, mirror = _find_schedule(config, args.advance)
        if not entry:
            print(f"No schedule found for: {args.advance}")
            sys.exit(1)
        if mirror:
            print(f"{args.advance} mirrors {entry['time']} — advancing {entry['time']}.")
        fes      = get_folder_entries(entry)
        state    = load_state()
        end_time = entry.get("end_time")
        ws       = _window_seconds(entry)
        if ws is not None:
            videos, folder_index, folder_path, resume_offset, new_resume_offset = \
                get_next_videos_for_window(fes, state, extensions, ws)
            if not videos:
                print("No videos to advance.")
                sys.exit(1)
            with state_transaction() as st:
                _apply_manual_advance(st, fes[0]["path"], folder_index,
                                      videos[-1].name, round(new_resume_offset, 3))
            print(f"Simulated playback at {args.advance}–{end_time}  →  folder: {folder_path}")
            for v in videos:
                print(f"  {v.name}")
            print(f"Resume offset saved: {new_resume_offset:.1f}s")
        else:
            videos, folder_index, folder_path = get_next_videos(fes, state, extensions)
            if not videos:
                print("No videos to advance.")
                sys.exit(1)
            with state_transaction() as st:
                _apply_manual_advance(st, fes[0]["path"], folder_index, videos[-1].name)
            print(f"Simulated playback at {args.advance}  →  folder: {folder_path}")
            for v in videos:
                print(f"  {v.name}")
        print("State updated.")
        return

    # --shift: move a slot forward/backward by N episodes, without playing
    if args.shift:
        ident, raw_delta = args.shift
        try:
            delta = int(raw_delta)
        except ValueError:
            print(f"N must be a whole number of episodes, got: {raw_delta!r}")
            sys.exit(1)

        entry, mirror = _find_schedule(config, ident)
        if not entry:
            print(f"No schedule found for: {ident}")
            sys.exit(1)
        if mirror:
            print(f"{ident} mirrors {entry['time']} — mirrors replay whatever the "
                  f"primary played, so shifting {entry['time']} instead.")

        fes       = get_folder_entries(entry)
        state_key = fes[0]["path"]
        sequence  = _slot_sequence(fes, extensions)
        if not sequence:
            print(f"No video files in this slot's folder(s): "
                  f"{', '.join(fe['path'] for fe in fes)}")
            sys.exit(1)

        entry_state = load_state().get(state_key, {})
        if isinstance(entry_state, str):
            entry_state = {"folder_index": 0, "last_played": entry_state}
        folder_index = entry_state.get("folder_index", 0) % len(fes)
        last_played  = entry_state.get("last_played")
        offset       = float(entry_state.get("resume_offset", 0.0))

        cursor = _state_to_cursor(sequence, folder_index, last_played)
        target = max(0, min(len(sequence), cursor + delta))
        if target != cursor + delta:
            edge = "the end" if delta > 0 else "the start"
            print(f"Clamped at {edge}: this slot holds {len(sequence)} episode(s).")
        if target == cursor:
            print("Nothing to do — the slot is already there.")
            return

        new_index, new_last = _cursor_to_state(sequence, target)
        new_offset = offset if args.keep_offset else 0.0

        def _where(pos, off):
            at = sequence[pos][1].name if pos < len(sequence) else "(exhausted — will roll over)"
            return f"{at}  @{off:.1f}s"

        print(f"Slot {entry['time']}  →  {state_key}")
        print(f"  before:  {_where(cursor, offset)}")
        print(f"  after:   {_where(target, new_offset)}   ({target - cursor:+d} episode(s))")
        if offset > 0 and not args.keep_offset:
            print(f"  resume_offset reset {offset:.1f}s → 0.0s "
                  f"(pass --keep-offset to hold the slot's alignment)")

        if _dry_run:
            print("[DRY RUN] State not written.")
            return

        with state_transaction() as st:
            _apply_manual_advance(st, state_key, new_index, new_last,
                                  None if args.keep_offset else 0.0)
        print("State updated.")

        # Show the real selection rather than asserting it: this runs the same
        # selector the scheduler will run at the slot's next firing.
        fresh = load_state()
        ws    = _window_seconds(entry)
        if ws is not None:
            videos, _, _, off_in, _ = get_next_videos_for_window(fes, fresh, extensions, ws)
        else:
            videos, _, _ = get_next_videos(fes, fresh, extensions)
            off_in = 0.0
        print(f"Next session at {entry['time']} would play "
              f"(first seeked to {off_in:.1f}s):")
        for v in videos:
            print(f"  {v.name}")
        return

    # --play-file: play one specific file immediately and exit
    if args.play_file:
        file_path = Path(args.play_file)
        if not file_path.is_file():
            print(f"File not found: {args.play_file}")
            sys.exit(1)
        kill_vlc()
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
    startup_catchup(config, vlc_path, extensions)
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
