# VLC Scheduler

Automatically plays the next numbered video(s) from designated folders at scheduled times. State is persisted so playback resumes from where it left off across reboots.

## Features

- **Scheduled Playback**: Configure multiple folders with different playback times
- **Sequential Playback**: Plays numbered videos in order (001.mp4, 002.mp4, …), wrapping around when the last one is reached
- **Multi-video batches**: Play N videos back-to-back per schedule slot
- **State Persistence**: Remembers the last played video per folder across restarts
- **Multiple Format Support**: MP4, AVI, MKV, MOV, WMV, FLV, and more
- **Auto VLC Detection**: Finds VLC automatically — no path configuration needed
- **Stale VLC Cleanup**: Kills any leftover VLC instance before starting a new one
- **Pre-play Hooks**: Run a shell command before each playback (e.g. reset screensaver, set volume)
- **Config Hot-reload**: Edit `config.json` while running — changes take effect within 30 s
- **Status Endpoint**: Live JSON status at `http://127.0.0.1:8765/`
- **Dry-run Mode**: Preview what would play without launching VLC
- **Play-now CLI**: Trigger a folder immediately from the command line

---

## Kiosk Deployment (Debian 13 Minimal)

This is the primary deployment target: a headless Debian machine that boots directly into the scheduler with no desktop environment.

### 1. Install Debian 13 "Trixie"

Use the netinstall ISO. In `tasksel`, select **SSH server only** — nothing else.

### 2. Connect via SSH and install the project

```bash
sudo apt install git
git clone <repository-url> ~/vlc-scheduler
cd ~/vlc-scheduler
```

### 3. Run the kiosk setup script

```bash
sudo bash setup_kiosk.sh
```

This single script handles everything:
- Installs `xorg`, `vlc`, `python3`, `python3-schedule`
- Configures **auto-login** on tty1
- Configures **auto-start X** on login via `~/.bash_profile`
- Creates `~/.xinitrc` that runs the scheduler (with auto-restart on crash)

Screen blanking and DPMS power-save are disabled automatically — the display stays on.

### 4. Configure your schedule

```bash
nano ~/vlc-scheduler/config.json
```

Set the correct folder paths and times (see [Configuration](#configuration) below).

### 5. Reboot

```bash
sudo reboot
```

**Boot sequence after setup:**
1. Machine powers on
2. Debian boots → auto-login on tty1
3. `~/.bash_profile` detects tty1 → runs `startx`
4. `~/.xinitrc` starts → runs `vlc_scheduler.py` in a restart loop
5. VLC plays videos at scheduled times, fullscreen

### Optional: WiFi setup

If using WiFi instead of ethernet:

```bash
sudo apt install wpasupplicant wireless-tools
```

Find your interface name (`ip a`), then edit `/etc/network/interfaces`:

```
auto wlan0
iface wlan0 inet static
  address 192.168.1.50
  netmask 255.255.255.0
  gateway 192.168.1.1
  dns-nameservers 8.8.8.8
  wpa-ssid "NetworkName"
  wpa-psk "NetworkPassword"
```

```bash
sudo systemctl restart networking
```

### Optional: Auto power-on after power loss

Enable **"AC Power Recovery"** in the BIOS (Dell: press F2 on boot). The machine will turn itself back on after any power outage.

---

## Configuration

Edit `config.json`:

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

| Key | Description |
|-----|-------------|
| `vlc_path` | Path to VLC. `"auto"` detects it via `$PATH` |
| `status_port` | Port for the status HTTP endpoint (default: `8765`) |
| `video_extensions` | List of file extensions to recognise as videos |
| `schedules[].time` | Playback time in `HH:MM` 24-hour format |
| `schedules[].folder` | Full path to the folder containing numbered videos |
| `schedules[].count` | Number of videos to play back-to-back (default: `1`) |
| `schedules[].before_play` | Optional shell command to run before launching VLC |

Changes to `config.json` are picked up automatically within 30 seconds — no restart needed.

---

## Video Organisation

Name files with leading numbers so they sort correctly:

```
folder01/
  ├── 001_title.mp4
  ├── 002_title.mp4
  └── 003_title.mp4
```

Any numeric prefix works: `001`, `01`, `1`, `ep01`, etc.

---

## Usage

### Run manually (for testing)

```bash
cd ~/vlc-scheduler
python3 vlc_scheduler.py
```

### Dry run

Preview what would play at each scheduled time without launching VLC:

```bash
python3 vlc_scheduler.py --dry-run
```

### Play now

Immediately trigger the next video(s) from a specific folder:

```bash
python3 vlc_scheduler.py --play-now /home/user/videos/folder01
```

### Status endpoint

```bash
curl http://127.0.0.1:8765/
```

```json
{
  "vlc_running": true,
  "schedules": [
    {
      "time": "13:00",
      "folder": "/home/user/videos/folder01",
      "count": 1,
      "last_played": "003_title.mp4"
    }
  ]
}
```

---

## File Structure

```
vlc-scheduler/
├── vlc_scheduler.py       Main scheduler
├── config.json            Schedule configuration
├── playback_state.json    Tracks playback position per folder (auto-created)
├── vlc_scheduler.log      Log file (auto-created)
└── setup_kiosk.sh         Full kiosk setup for Debian minimal
```

---

## Troubleshooting

**Missing `schedule` module:**
Run `sudo apt install python3-schedule`.

**VLC not found:**
`which vlc` — confirm it's installed and on `$PATH`. Or set `vlc_path` explicitly in `config.json`.

**Videos not playing:**
- Check that the folder path in `config.json` exists and is readable
- Use `--dry-run` to verify the scheduler sees the correct files

**Screen goes blank:**
The kiosk setup disables DPMS automatically via `~/.xinitrc`. If you set up manually, run:
```bash
xset s off && xset -dpms && xset s noblank
```

**Status endpoint not responding:**
Change `status_port` in `config.json` if port 8765 is in use.
