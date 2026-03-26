#!/usr/bin/env python3
"""
VLC Scheduler
Plays the next numbered video from a designated folder at scheduled times.
State (last played index per folder) is persisted in playback_state.json.
"""

import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import schedule
except ImportError:
    print("Missing dependency. Activate the venv and run:  pip install -r requirements.txt")
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

# ── Default configuration (written on first run) ──────────────────────────────
DEFAULT_CONFIG = {
    "vlc_path": "C:/Program Files/VideoLAN/VLC/vlc.exe",
    "video_extensions": [
        ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv",
        ".m4v", ".mpg", ".mpeg", ".webm", ".ts", ".vob", ".mts", ".m2ts",
    ],
    "schedules": [
        {"time": "17:30", "folder": str(BASE_DIR / "folder01")},
        {"time": "19:00", "folder": str(BASE_DIR / "folder02")},
        {"time": "21:00", "folder": str(BASE_DIR / "folder03")},
    ],
}


# ── Config / state helpers ────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log.info("Created default config.json — edit it to customise paths.")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


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
    """
    Natural-sort key: splits the stem into text/number chunks so that
    '2.webm' < '10.webm' and 'ep02.mkv' < 'ep10.mkv'.
    Falls back gracefully for files with no digits at all.
    """
    parts = re.split(r"(\d+)", path.stem)
    return [int(c) if c.isdigit() else c.lower() for c in parts]


def get_next_video(folder_path: str, state: dict, extensions: list):
    """
    Scan *folder_path* for any video file whose extension is in *extensions*.
    Files are sorted with natural ordering (handles leading/embedded numbers).
    The last-played file name is stored in *state*; playback wraps around once
    all files have been played.
    Returns (sort_position, Path) or None if the folder is empty / missing.
    """
    folder = Path(folder_path)
    if not folder.exists():
        log.error(f"Folder not found: {folder_path}")
        return None

    ext_set = {e.lower() for e in extensions}
    videos  = sorted(
        [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in ext_set],
        key=_natural_sort_key,
    )

    if not videos:
        log.error(f"No video files found in: {folder_path}")
        return None

    last_played_name = state.get(folder_path)          # filename string or None

    # Find position after the last-played file
    next_index = 0
    if last_played_name:
        for i, f in enumerate(videos):
            if f.name == last_played_name:
                next_index = i + 1
                break

    if next_index >= len(videos):
        log.info(f"All videos played — wrapping back to the first for: {folder_path}")
        next_index = 0

    chosen = videos[next_index]
    return next_index, chosen


# ── Playback ──────────────────────────────────────────────────────────────────

def play_video(folder_path: str, vlc_path: str, extensions: list) -> None:
    state  = load_state()
    result = get_next_video(folder_path, state, extensions)
    if not result:
        return

    video_index, video_file = result
    log.info(f"Launching VLC  →  {video_file.name}  (index {video_index})")

    try:
        subprocess.Popen(
            [
                vlc_path,
                "--fullscreen",
                "--play-and-exit",
                "--no-video-title-show",
                str(video_file),
            ]
        )
        # Persist new state only after a successful launch (store filename)
        state[folder_path] = video_file.name
        save_state(state)
    except FileNotFoundError:
        log.error(f"VLC executable not found at: {vlc_path}  — update config.json")
    except Exception:
        log.exception("Unexpected error while launching VLC")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("VLC Scheduler starting")

    config     = load_config()
    vlc_path   = config["vlc_path"]
    extensions = config.get("video_extensions", DEFAULT_CONFIG["video_extensions"])

    for entry in config["schedules"]:
        t      = entry["time"]    # "HH:MM"
        folder = entry["folder"]
        log.info(f"  Registered  {t}  →  {folder}")
        schedule.every().day.at(t).do(play_video, folder, vlc_path, extensions)

    log.info("Scheduler running — waiting for scheduled times.")

    while True:
        schedule.run_pending()
        time.sleep(30)   # check every 30 seconds


if __name__ == "__main__":
    main()
