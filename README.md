# VLC Scheduler

Automatically plays the next numbered video from designated folders at scheduled times. State is persisted so playback resumes from where it left off.

## Features

- **Scheduled Playback**: Configure multiple folders with different playback times
- **Sequential Playback**: Automatically plays numbered videos in order (e.g., 001.mp4, 002.mp4, etc.)
- **State Persistence**: Remembers the last played video per folder, even after restarts
- **Multiple Format Support**: Handles MP4, AVI, MKV, MOV, WMV, FLV, and more
- **Logging**: All activities logged to file and console
- **Linux Autostart**: Optional systemd user service for automatic startup on login

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
  "vlc_path": "/usr/bin/vlc",
  "video_extensions": [".mp4", ".avi", ".mkv", ".mov", ".wmv"],
  "schedules": [
    {
      "time": "13:00",
      "folder": "/home/user/videos/folder01"
    },
    {
      "time": "19:00",
      "folder": "/home/user/videos/folder02"
    }
  ]
}
```

### Configuration Options

- **vlc_path**: Full path to the VLC executable (usually `/usr/bin/vlc`)
- **video_extensions**: List of video file extensions to recognize
- **schedules**: Array of scheduled playback times and folders
  - **time**: Playback time in HH:MM format (24-hour)
  - **folder**: Full path to folder containing numbered videos

## Usage

### Run Scheduler

```bash
python vlc_scheduler.py
```

The scheduler will run continuously and play videos at the configured times. Press `Ctrl+C` to stop.

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

**Missing schedule module error:**

- Ensure virtual environment is activated before running
- Run `pip install -r requirements.txt`

**VLC not found:**

- Check VLC is installed: `which vlc`
- Verify the `vlc_path` in `config.json` matches the output of `which vlc`

**Videos not playing:**

- Check folder permissions
- Ensure videos have supported extensions listed in config
- Verify folder path exists and contains numbered videos

## License

[Add your license here]

## Contributing

Contributions welcome! Feel free to submit issues and pull requests.
