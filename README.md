# VLC Scheduler

Automatically plays the next numbered video(s) from designated folders at scheduled times. State is persisted so playback resumes from where it left off.

## Features

- **Scheduled Playback**: Configure multiple folders with different playback times
- **Sequential Playback**: Automatically plays numbered videos in order (e.g., 001.mp4, 002.mp4, etc.)
- **Multi-video batches**: Play N videos back-to-back per schedule slot (great for short clips)
- **State Persistence**: Remembers the last played video per folder, even after restarts
- **Multiple Format Support**: Handles MP4, AVI, MKV, MOV, WMV, FLV, and more
- **Auto VLC Detection**: Finds VLC automatically — no path configuration needed
- **Stale VLC Cleanup**: Terminates any leftover VLC instance before starting a new one
- **Pre-play Hooks**: Run a shell command before each playback (e.g. disable screensaver)
- **Config Hot-reload**: Edit `config.json` while running — changes take effect within 30 s
- **Status Endpoint**: Live JSON status at `http://127.0.0.1:8765/`
- **Dry-run Mode**: Preview what would play without launching VLC
- **Play-now CLI**: Trigger a folder immediately from the command line
- **Logging**: All activity logged to file and console
- **Linux Autostart**: Systemd user service for automatic startup on login

## Installation

### 1. Clone/Download the Project

```bash
git clone <repository-url>
cd vlc-scheduler
```

### 2. Install VLC

```bash
# Debian/Ubuntu
sudo apt install vlc

# Fedora
sudo dnf install vlc

# Arch
sudo pacman -S vlc
```

### 3. Create Virtual Environment and Install Dependencies

```bash
bash setup_venv.sh
```

Or manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Edit `config.json` to set up your schedules:

```json
{
  "vlc_path": "auto",
  "status_port": 8765,
  "video_extensions": [".mp4", ".avi", ".mkv", ".mov", ".wmv"],
  "schedules": [
    {
      "time": "13:00",
      "folder": "/home/user/videos/folder01",
      "count": 1,
      "before_play": "xdg-screensaver reset"
    },
    {
      "time": "19:00",
      "folder": "/home/user/videos/folder02",
      "count": 3
    }
  ]
}
```

### Configuration Options

| Key | Description |
|-----|-------------|
| `vlc_path` | Path to VLC executable. `"auto"` detects it via `$PATH` |
| `status_port` | Port for the status HTTP endpoint (default: `8765`) |
| `video_extensions` | List of video file extensions to recognise |
| `schedules[].time` | Playback time in `HH:MM` 24-hour format |
| `schedules[].folder` | Full path to folder containing numbered videos |
| `schedules[].count` | Number of videos to play back-to-back (default: `1`) |
| `schedules[].before_play` | Optional shell command to run before launching VLC |

### Pre-play Hook Examples

```json
"before_play": "xdg-screensaver reset"
```

```json
"before_play": "amixer set Master 80%"
```

```json
"before_play": "notify-send 'VLC Scheduler' 'Starting playback'"
```

## Usage

### Run Scheduler

```bash
python vlc_scheduler.py
```

The scheduler will run continuously and play videos at the configured times. Press `Ctrl+C` to stop.

### Dry Run

Preview which video would play at each scheduled time without actually launching VLC:

```bash
python vlc_scheduler.py --dry-run
```

### Play Now

Immediately trigger the next video(s) from a specific folder:

```bash
python vlc_scheduler.py --play-now /home/user/videos/folder01
```

The folder respects the `count` and `before_play` settings configured for that folder.

### Status Endpoint

While the scheduler is running, query live status:

```bash
curl http://127.0.0.1:8765/
```

Example response:

```json
{
  "vlc_running": true,
  "schedules": [
    {
      "time": "13:00",
      "folder": "/home/user/videos/folder01",
      "count": 1,
      "last_played": "003_intro.mp4"
    }
  ]
}
```

### Config Hot-reload

Edit and save `config.json` at any time while the scheduler is running. Changes are detected automatically within 30 seconds — no restart needed. Invalid configs are logged and skipped without disrupting the running scheduler.

### Autostart Setup (systemd)

Register the scheduler as a systemd user service so it starts automatically on login:

```bash
bash setup_autostart.sh
```

Or directly:

```bash
python setup_autostart.py           # install
python setup_autostart.py --remove  # uninstall
```

Useful service commands:

```bash
systemctl --user status vlc-scheduler
systemctl --user stop   vlc-scheduler
systemctl --user start  vlc-scheduler
journalctl --user -u vlc-scheduler -f
```

## File Structure

- `vlc_scheduler.py` — Main scheduler script
- `config.json` — Configuration file
- `playback_state.json` — Tracks current playback position per folder
- `vlc_scheduler.log` — Log file (auto-created)
- `requirements.txt` — Python dependencies
- `setup_venv.sh` — Virtual environment setup
- `setup_autostart.sh` — Autostart setup wrapper
- `setup_autostart.py` — Autostart setup script (systemd)

## Video Organization

Videos should be named with leading numbers for sequential playback:

```
folder01/
  ├── 001_video_name.mp4
  ├── 002_video_name.mp4
  ├── 003_video_name.mp4
  └── ...
```

## Troubleshooting

**Missing `schedule` module error:**
Activate the virtual environment and run `pip install -r requirements.txt`.

**VLC not found:**
Run `which vlc` to confirm VLC is installed and on `$PATH`. If it's in a non-standard location, set `vlc_path` explicitly in `config.json`.

**Videos not playing:**
- Check folder permissions
- Ensure videos have supported extensions listed in config
- Use `--dry-run` to verify the scheduler sees the correct files

**Status endpoint not responding:**
Change `status_port` in `config.json` if port 8765 is already in use.

## License

[Add your license here]

## Contributing

Contributions welcome! Feel free to submit issues and pull requests.
