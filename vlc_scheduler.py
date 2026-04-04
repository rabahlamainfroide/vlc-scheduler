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
        {"time": "17:30", "folder": str(BASE_DIR / "folder01"), "count": 1},
        {"time": "19:00", "folder": str(BASE_DIR / "folder02"), "count": 1},
        {"time": "21:00", "folder": str(BASE_DIR / "folder03"), "count": 1},
    ],
}


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

    for entry in config.get("schedules", []):
        folder = Path(entry.get("folder", ""))
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


def get_next_videos(folder_path: str, state: dict, extensions: list, count: int = 1) -> list:
    """
    Return up to *count* video Paths to play next in natural order, wrapping
    around the folder if needed.  Returns [] if the folder is missing or empty.
    """
    folder = Path(folder_path)
    if not folder.exists():
        log.error(f"Folder not found: {folder_path}")
        return []

    ext_set = {e.lower() for e in extensions}
    videos  = sorted(
        [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in ext_set],
        key=_natural_sort_key,
    )

    if not videos:
        log.error(f"No video files found in: {folder_path}")
        return []

    last_played_name = state.get(folder_path)
    next_index = 0
    if last_played_name:
        for i, f in enumerate(videos):
            if f.name == last_played_name:
                next_index = i + 1
                break

    if next_index >= len(videos):
        log.info(f"All videos played — wrapping back to the first for: {folder_path}")
        next_index = 0

    total    = len(videos)
    selected = [videos[(next_index + offset) % total] for offset in range(count)]
    return selected


# ── Hooks ─────────────────────────────────────────────────────────────────────

def _run_hook(cmd: str) -> None:
    """Run an optional shell hook, logging errors without crashing."""
    try:
        result = subprocess.run(cmd, shell=True, timeout=10)
        if result.returncode != 0:
            log.warning(f"Hook exited {result.returncode}: {cmd!r}")
    except subprocess.TimeoutExpired:
        log.warning(f"Hook timed out: {cmd!r}")
    except Exception:
        log.exception(f"Hook failed: {cmd!r}")


# ── Playback ──────────────────────────────────────────────────────────────────

def play_videos(folder_path: str, vlc_path: str, extensions: list,
                count: int = 1, before_play: Optional[str] = None) -> None:
    global _active_proc

    state  = load_state()
    videos = get_next_videos(folder_path, state, extensions, count)
    if not videos:
        return

    names = ", ".join(v.name for v in videos)

    if _dry_run:
        log.info(f"[DRY RUN] Would launch VLC → {names}")
        return

    log.info(f"Launching VLC → {names}")

    # Kill any still-running VLC from a previous scheduled slot
    if _active_proc is not None and _active_proc.poll() is None:
        log.info("Terminating previous VLC instance before starting new one.")
        _active_proc.terminate()
        try:
            _active_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _active_proc.kill()

    # Pre-play hook (e.g. disable screensaver)
    if before_play:
        log.info(f"Running before_play hook: {before_play!r}")
        _run_hook(before_play)

    try:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        _active_proc = subprocess.Popen(
            [
                vlc_path,
                "--fullscreen",
                "--play-and-exit",
                "--no-video-title-show",
                "--vout", "xcb_xv",
                *(str(v) for v in videos),
            ],
            env=env,
        )
        # Persist state: record the last video in the batch as the new position
        state[folder_path] = videos[-1].name
        save_state(state)
    except FileNotFoundError:
        log.error(f"VLC executable not found at: {vlc_path}  — update config.json")
    except Exception:
        log.exception("Unexpected error while launching VLC")


# ── Status HTTP endpoint ──────────────────────────────────────────────────────

class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        state   = load_state()
        payload = {
            "vlc_running": _active_proc is not None and _active_proc.poll() is None,
            "schedules": [
                {
                    "time":        entry["time"],
                    "folder":      entry["folder"],
                    "count":       entry.get("count", 1),
                    "last_played": state.get(entry["folder"]),
                }
                for entry in _current_config.get("schedules", [])
            ],
        }
        body = json.dumps(payload, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress per-request access log noise


def _start_status_server(port: int) -> None:
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _StatusHandler)
        log.info(f"Status endpoint: http://127.0.0.1:{port}/")
        server.serve_forever()
    except OSError as e:
        log.warning(f"Could not start status server on port {port}: {e}")


# ── Schedule registration ─────────────────────────────────────────────────────

def _register_schedules(config: dict) -> None:
    """Clear all jobs and re-register from config."""
    schedule.clear()
    vlc_path   = detect_vlc(config.get("vlc_path", "auto"))
    extensions = config.get("video_extensions", DEFAULT_CONFIG["video_extensions"])

    for entry in config["schedules"]:
        t           = entry["time"]
        folder      = entry["folder"]
        count       = entry.get("count", 1)
        before_play = entry.get("before_play")
        log.info(f"  Registered  {t}  →  {folder}  (count={count})")
        schedule.every().day.at(t).do(
            play_videos, folder, vlc_path, extensions, count, before_play
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
        # Reuse per-folder settings from config if the folder is listed there
        entry = next(
            (e for e in config["schedules"] if e["folder"] == args.play_now),
            {},
        )
        play_videos(
            args.play_now,
            vlc_path,
            extensions,
            entry.get("count", 1),
            entry.get("before_play"),
        )
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
