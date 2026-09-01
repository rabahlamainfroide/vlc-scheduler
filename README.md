# VLC Scheduler

Automatically plays the next numbered video(s) from designated folders at scheduled times. State is persisted so playback resumes from where it left off across reboots.

## Features

- **Scheduled Playback**: Configure multiple folders with different playback times
- **Time-window scheduling**: Set a `start_time` + `end_time` and the scheduler automatically calculates how many episodes are needed to fill the slot. The overshoot is tracked so the next session seeks into the first episode to stay perfectly aligned across days
- **Mirror slots**: Mark a slot as `"mirror": "HH:MM"` to make it an exact replica of another slot — same episodes, same seek offset, no state advancement
- **Sequential Playback**: Plays numbered videos in order (001.mp4, 002.mp4, …); when a folder is exhausted it advances to the next folder in the list, wrapping back to the first after the last
- **Per-folder resume**: A slot with several folders remembers where it left each one, so coming back round resumes there — and picks up episodes added in the meantime — instead of restarting at episode 1
- **Multi-video batches**: Play N videos back-to-back per schedule slot, with a per-folder count
- **State Persistence**: Remembers the last played video, active folder, and session overshoot offset across restarts
- **Multiple Format Support**: MP4, AVI, MKV, MOV, WMV, FLV, and more
- **Auto VLC Detection**: Finds VLC automatically — no path configuration needed
- **Stale VLC Cleanup**: Kills any leftover VLC instance before starting a new one
- **Pre-play Hooks**: Run a shell command before each playback (e.g. reset screensaver, set volume)
- **Screen watchdog**: If VLC dies while a slot still owns the screen, the slot restarts and plays out the rest of its window instead of leaving a black screen until the next slot. Manual playback is never interrupted
- **Window-end commit**: A slot's position advances when its window closes, not when VLC happens to exit — so the last slot of the day, whose player runs on for hours with nothing scheduled behind it, cannot lose a night's progress to a restart
- **Startup Catch-up**: If the machine reboots mid-playback or while a scheduled slot was missed, the scheduler immediately plays that slot on startup instead of waiting for the next one
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
git clone https://github.com/rabahlamainfroide/vlc-scheduler.git ~/vlc-scheduler
cd ~/vlc-scheduler
```

### 3. Run the kiosk setup script

```bash
sudo bash setup_kiosk.sh
```

This single script handles everything:

- Installs `xorg`, `openbox`, `vlc`, `python3`, `python3-schedule`, `network-manager`, `unclutter`, `alsa-utils`, `intel-media-va-driver`
- Creates `/etc/X11/xorg.conf.d/10-no-blanking.conf` to persistently disable screen blanking
- Configures **auto-login** on tty1
- Configures **auto-start X** on login via `~/.bash_profile` (SSH sessions are excluded)
- Creates `~/.xinitrc` that runs the scheduler (with auto-restart on crash) and hides the mouse cursor

Screen blanking and DPMS power-save are disabled via both Xorg config and runtime `xset` — the display stays on.

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

`setup_kiosk.sh` installs and enables NetworkManager. Connect with:

```bash
# Scan
nmcli dev wifi list

# Connect
nmcli dev wifi connect "YourWiFiName" password "YourPassword"

# Assign a static IP so SSH address never changes
nmcli con mod "YourWiFiName" ipv4.addresses 192.168.1.50/24
nmcli con mod "YourWiFiName" ipv4.gateway 192.168.1.1
nmcli con mod "YourWiFiName" ipv4.dns "8.8.8.8 1.1.1.1"
nmcli con mod "YourWiFiName" ipv4.method manual
nmcli con up "YourWiFiName"
```

NetworkManager auto-reconnects on reboot — no manual `/etc/network/interfaces` editing needed.

### Optional: Harden SSH (key-only, no passwords)

First copy your public key from your workstation:

```bash
ssh-copy-id chak@192.168.1.50
# or manually append ~/.ssh/id_ed25519.pub to ~/.ssh/authorized_keys on the server
```

Then edit `/etc/ssh/sshd_config` on the server:

```
PermitRootLogin no
PasswordAuthentication no
```

```bash
sudo systemctl restart ssh
```

Open a second SSH session to confirm key login works before closing the first.

### Optional: Dynamic DNS (required if your ISP assigns a dynamic IP)

If your public IP changes, clients can't reach the server by IP. Use a free DDNS service like [DuckDNS](https://www.duckdns.org) to get a stable hostname (e.g. `mykiosk.duckdns.org`) that always points to your current IP.

**Install the DuckDNS updater as a cron job:**

```bash
mkdir -p ~/duckdns
cat > ~/duckdns/duck.sh <<'EOF'
echo url="https://www.duckdns.org/update?domains=YOURSUBDOMAIN&token=YOURTOKEN&ip=" | curl -k -o ~/duckdns/duck.log -K -
EOF
chmod +x ~/duckdns/duck.sh
```

Add to crontab (`crontab -e`):

```
*/5 * * * * ~/duckdns/duck.sh >/dev/null 2>&1
```

Replace `YOURSUBDOMAIN` and `YOURTOKEN` with the values from your DuckDNS account. The IP updates every 5 minutes.

Also forward UDP port `51820` on your router to the machine's local IP.

### Optional: WireGuard VPN server

Lets you reach the machine remotely without exposing SSH to the public internet — SSH only over the VPN tunnel.

**Install:**

```bash
sudo apt install wireguard
```

**Generate server keys:**

```bash
wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key
chmod 600 /etc/wireguard/server_private.key
```

**Create `/etc/wireguard/wg0.conf`:**

```ini
[Interface]
Address = 10.0.0.1/24
ListenPort = 51820
PrivateKey = <contents of server_private.key>
PostUp   = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

[Peer]
PublicKey = <client public key>
AllowedIPs = 10.0.0.2/32
```

Replace `eth0` with your actual network interface (`ip link` to check).

**Enable IP forwarding:**

```bash
echo "net.ipv4.ip_forward=1" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

**Start and enable at boot:**

```bash
sudo systemctl enable --now wg-quick@wg0
```

**Generate a client config** (run on the server, copy to client):

```bash
wg genkey | tee client_private.key | wg pubkey > client_public.key
```

```ini
[Interface]
Address = 10.0.0.2/24
PrivateKey = <contents of client_private.key>
DNS = 1.1.1.1

[Peer]
PublicKey = <contents of server_public.key>
Endpoint = mykiosk.duckdns.org:51820
AllowedIPs = 10.0.0.0/24
PersistentKeepalive = 25
```

`AllowedIPs = 10.0.0.0/24` is **split tunneling** — only traffic destined for the VPN subnet (`10.0.0.x`) goes through the tunnel; your regular internet traffic continues over your normal connection. This is what you want here: SSH into `10.0.0.1`, everything else is unaffected.

To route **all** traffic through the VPN instead (no split tunneling), change it to:

```ini
AllowedIPs = 0.0.0.0/0, ::/0
```

Add the client's public key to the `[Peer]` block in `wg0.conf`, then reload: `sudo wg syncconf wg0 <(wg-quick strip wg0)`.

**Client setup — install WireGuard and activate the tunnel:**

Save the config above as `wg0.conf` on the client, then:

*Linux (Debian/Ubuntu):*
```bash
sudo apt install wireguard
sudo cp wg0.conf /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
sudo wg-quick up wg0          # connect
sudo wg-quick down wg0        # disconnect
sudo systemctl enable wg-quick@wg0   # auto-connect at boot (optional)
```

*macOS (Homebrew):*
```bash
brew install wireguard-tools
sudo wg-quick up ./wg0.conf
sudo wg-quick down ./wg0.conf
```
Or install the **WireGuard** app from the Mac App Store — import the `.conf` file from the app.

*Windows:* Download and install the [WireGuard app](https://www.wireguard.com/install/), click **Import tunnel(s) from file**, and select your `wg0.conf`. Toggle the tunnel on/off from the app.

*Android / iOS:* Install the **WireGuard** app, tap **+**, and choose **Create from file or archive** to import the `.conf`, or scan a QR code. Generate a QR code from the server:

```bash
sudo apt install qrencode
qrencode -t ansiutf8 < wg0.conf
```

**Verify the tunnel is up:**

```bash
# On the client — should show handshake timestamp and traffic counters
sudo wg show

# Confirm you can reach the server over the VPN
ping 10.0.0.1

# Then SSH as normal, using the VPN IP
ssh chak@10.0.0.1
```

### Optional: Force audio output to HDMI

Find the HDMI device:

```bash
aplay -l
# Look for the HDMI entry — e.g. card 0, device 3: HDMI 0 [LG HDR 4K]
```

Create `/etc/asound.conf` with the card and device numbers:

```
defaults.pcm.card 0
defaults.pcm.device 3
defaults.ctl.card 0
```

Test:

```bash
speaker-test -c 2 -t wav
```

VLC picks up the ALSA default automatically — no VLC config needed.

### Optional: Control volume over SSH

```bash
# Set master volume to 80%
amixer set Master 80%

# Interactive mixer
alsamixer
```

### Optional: Mount an external hard drive at a fixed path

If your videos live on an external drive, mount it persistently so the path in `config.json` never changes between reboots.

Find the drive's UUID:

```bash
sudo blkid
# /dev/sdb1: UUID="FC3A-F72A" TYPE="vfat" ...
```

Add an entry to `/etc/fstab`:

```
UUID=FC3A-F72A  /home/chak/videos  vfat  defaults,nofail,x-systemd.automount,x-systemd.device-timeout=5  0  0
```

- `nofail` — boot succeeds even if the drive is absent
- `x-systemd.automount` — mount is deferred until first access (avoids blocking boot)
- `x-systemd.device-timeout=5` — gives up waiting for the device after 5 seconds

Create the mount point and apply:

```bash
sudo mkdir -p /home/chak/videos
sudo mount -a
```

### Optional: Debug an Android phone over USB

Lets you run `adb` commands on a plugged-in Android phone — the phone owner just plugs in the cable.

**One-time setup on the Debian machine:**

```bash
sudo apt install adb
```

Add a udev rule so `adb` works without `sudo`:

```bash
sudo tee /etc/udev/rules.d/51-android.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="*", MODE="0664", GROUP="plugdev"
EOF
sudo usermod -aG plugdev $USER
sudo udevadm control --reload-rules
```

Log out and back in for the group change to take effect.

**One-time setup on the phone** (do this once while you're with the phone):

1. Go to **Settings → About phone** and tap **Build number** 7 times to enable Developer Options
2. Go to **Settings → Developer Options** and enable **USB debugging**
3. Plug the phone into the machine and run:

```bash
adb devices
```

4. A prompt appears on the phone screen — tap **Allow** and check **Always allow from this computer**

From that point on, plugging the cable in is all that's needed. Verify with:

```bash
adb devices
# List of devices attached
# XXXXXXXX    device
```

**Useful commands:**

```bash
# Take a screenshot and pull it to the machine
adb shell screencap /sdcard/screen.png && adb pull /sdcard/screen.png

# Stream phone logs live
adb logcat

# Install an APK
adb install app.apk

# Copy a file from the phone
adb pull /sdcard/DCIM/photo.jpg .
```

### Optional: Download series episodes remotely with yt-dlp

Install yt-dlp (the apt package can be outdated — the binary from GitHub is always current):

```bash
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

**Download a playlist directly into a scheduler folder**, with numbering the scheduler can read:

```bash
yt-dlp \
  -o "/home/chak/videos/series_A/%(playlist_index)03d_%(title)s.%(ext)s" \
  --merge-output-format mp4 \
  "https://www.youtube.com/playlist?list=PLAYLIST_ID"
```

- `%(playlist_index)03d` — zero-padded episode number (001, 002, …) so the scheduler picks them up in order
- `--merge-output-format mp4` — ensures the output is always `.mp4`

**Run it from your laptop over SSH** so the download happens on the kiosk machine (no need to transfer files):

```bash
ssh chak@mykiosk.duckdns.org \
  "yt-dlp -o '/home/chak/videos/series_A/%(playlist_index)03d_%(title)s.%(ext)s' --merge-output-format mp4 'PLAYLIST_URL'"
```

**Keep yt-dlp up to date:**

```bash
sudo yt-dlp -U
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
      "end_time": "14:30",
      "folders": [
        {"path": "/home/user/videos/series_A"}
      ],
      "before_play": "xdg-screensaver reset"
    },
    {
      "time": "18:00",
      "mirror": "13:00",
      "before_play": "xdg-screensaver reset"
    },
    {
      "time": "19:00",
      "folders": [
        {"path": "/home/user/videos/series_B", "count": 3},
        {"path": "/home/user/videos/series_C", "count": 1}
      ]
    }
  ]
}
```

| Key                              | Description                                                                                     |
| -------------------------------- | ----------------------------------------------------------------------------------------------- |
| `vlc_path`                       | Path to VLC. `"auto"` detects it via `$PATH`                                                   |
| `status_port`                    | Port for the status HTTP endpoint (default: `8765`)                                             |
| `video_extensions`               | List of file extensions to recognise as videos                                                  |
| `schedules[].time`               | Start time in `HH:MM` 24-hour format                                                            |
| `schedules[].end_time`           | Optional end time in `HH:MM`. When set, `count` is ignored — the scheduler uses `ffprobe` to measure episode durations and selects however many episodes are needed to fill the window. Requires `ffmpeg` to be installed. |
| `schedules[].folders`            | Ordered list of folder objects. When a folder's videos are all played, the next folder is used  |
| `schedules[].folders[].path`     | Full path to the folder containing numbered videos                                              |
| `schedules[].folders[].count`    | Number of videos to play back-to-back (default: `1`). Ignored when `end_time` is set           |
| `schedules[].mirror`             | Optional. Set to the `time` of another slot to make this an exact replica of it. When set, all other fields except `before_play` are inherited from the referenced slot. |
| `schedules[].before_play`        | Optional shell command to run before launching VLC                                              |

**Backward-compatible formats** — the old `"folder"` string key and plain string lists still work:

```json
{ "time": "17:30", "folder": "/path/to/folder", "count": 2 }
{ "time": "17:30", "folders": ["/path/a", "/path/b"], "count": 1 }
```

### How time-window scheduling works

When `end_time` is set, the scheduler:

1. Reads durations of upcoming episodes via `ffprobe`
2. Selects the minimum number of full episodes whose total runtime covers the window (episodes always play to completion — no mid-episode cutoff)
3. Because episodes rarely divide the window exactly, the last episode typically runs a few seconds past `end_time`. This overshoot is saved as `resume_offset` in `playback_state.json`
4. At the next session the first episode is seeked forward by `resume_offset` seconds (VLC `:start-time` option), so the total content played each day stays aligned with the configured window

Install `ffmpeg` to enable this feature:

```bash
sudo apt install ffmpeg
```

### How mirror slots work

A mirror slot plays the exact same episodes as its primary slot, with the same seek offset. It never advances state — only the primary does.

**If the primary fires first:** the primary saves a `last_session` snapshot (folder, filenames, `resume_offset` used). When the mirror fires later, it reads that snapshot and replays identically.

**If the mirror fires first:** no snapshot exists yet, so the mirror computes the same selection the primary would (same state, same window) and plays it without writing anything to state. The primary then fires normally and advances the queue.

### How a slot with several folders rotates

Folders in one slot play as a single run: all of the first, then all of the
second, then back to the first. Each folder's position is remembered
separately, which matters because a folder is only ever left behind when it
runs out:

```
hs  ep1 … ep141   (finished)  ──▶  sk  ep1 … ep164   (finished)  ──▶  back to hs
                                                                       │
       hs remembered at ep141; 6 new episodes arrived meanwhile  ◀──────┘
       so it resumes at ep142 rather than replaying from ep1
```

If nothing new has been added, the folder genuinely has nothing left and is
played again from the beginning.

A slot with a **single** folder does the same thing on a smaller scale: when it
reaches the last episode it starts over from the first.

### How the screen watchdog works

A slot with an `end_time` owns the screen until that time. If VLC goes away
before then — a decoder crash, a display glitch, or a playlist that ran out
early — the slot is restarted after a 15 s pause and given whatever is left of
its window. Up to 3 restarts per slot, so an episode VLC cannot decode cannot
spin the screen all afternoon.

What happens to the rotation depends on why VLC stopped:

| VLC stopped because | Rotation | Restart plays |
|---|---|---|
| It crashed part-way through the batch | Not advanced | The same episodes again — nobody watched them |
| It finished everything it was given | Advanced | The next batch |
| The next slot took over | Advanced | Nothing — that slot owns the screen now |
| You started something by hand | Advanced | Nothing — see below |

A restart is only ever attempted when the screen is actually empty: before
relaunching, the watchdog checks whether any VLC is running. Playing episodes
by hand (`--play-now`, or launching VLC yourself) therefore stops the watchdog
from interfering, since it can see something is already on screen.

Slots without an `end_time` have no knowable end, so they are not supervised.

### When a slot's position is committed

A slot predicts where it will end up the moment it launches, but writes that
prediction to `pending_*` and only promotes it once there is evidence the
episodes were really on screen. Two things provide that evidence, whichever
comes first:

| | Commits when | Evidence |
|---|---|---|
| **Window-end timer** | The slot's `end_time` arrives | Our VLC is still running |
| **Exit watcher** | VLC exits | It got through the batch, or the next slot took over |

The timer is what makes the *last* slot of the day safe. Nothing is scheduled
behind it, so nobody kills its VLC at its `end_time` — it plays on until the
next morning's mirror takes the screen, some nine hours later. Hanging the
commit on that exit put the whole night at the mercy of a restart: stop the
scheduler at 23:04 and the waiting thread died still holding an uncommitted
position, so the next evening replayed the same two episodes.

If the scheduler is not running when the window closes, neither path fires. The
next startup repairs that, using the machine's boot time as the evidence
instead:

- **Up continuously since the session started** → VLC cannot have stopped,
  because killing the scheduler does not kill the player it spawned. The window
  was filled, so the position is committed.
- **Rebooted during the session** → VLC went down with it and the window was
  not filled. The position stays put and that batch replays, which is the whole
  point of deferring the commit.

Slots without an `end_time` have no window to close, so they keep the old
behaviour: their position is committed when the next slot takes the screen.

### When a folder cannot be read

Durations come from `ffprobe`. An episode it cannot read is charged the median
duration of the others in the same batch rather than 0 s — a free episode would
let the window selector keep taking files until the folder ran out, burning a
whole series in one session.

If *nothing* in the folder probes — an external drive that has not mounted yet,
typically — the slot plays nothing rather than advancing the rotation past
episodes nobody could watch, and retries every minute inside its window.

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

On startup the scheduler checks whether the most recently passed schedule slot was missed or interrupted (e.g. due to a power cut). If it was, that slot plays immediately rather than waiting for the next scheduled time. Normal restarts where all slots completed cleanly do nothing extra.

### Dry run

Preview what would play at each scheduled time without launching VLC:

```bash
python3 vlc_scheduler.py --dry-run
```

### Play now

Immediately trigger the next video(s) from a specific folder (picks up where state left off):

```bash
python3 vlc_scheduler.py --play-now /home/user/videos/folder01
```

### Play a specific file

Play one exact file right now, fullscreen, without touching playback state:

```bash
python3 vlc_scheduler.py --play-file /home/user/videos/folder01/003_episode.mp4
```

Useful for previewing a file or re-watching something without disrupting the scheduler's position.

### Peek at a scheduled time

Show which video(s) would be played at a given time without changing state:

```bash
python3 vlc_scheduler.py --peek 19:00
# Schedule 19:00  →  folder: /home/user/videos/series_B
#   003_episode.mp4
#   004_episode.mp4
#   005_episode.mp4
```

For a time-window schedule, durations and offset info are shown:

```bash
python3 vlc_scheduler.py --peek 13:00
# Schedule 13:00–14:30  →  folder: /home/user/videos/series_A
# Resume offset: 142.0s  |  Next session offset: 87.3s
#   004_episode.mp4  (1382s)
#   005_episode.mp4  (1401s)
#   006_episode.mp4  (1394s)
#   007_episode.mp4  (1365s)
```

### Simulate a playback

Advance the state as if the scheduler played once at a given time (no VLC launched):

```bash
python3 vlc_scheduler.py --advance 19:00
# Simulated playback at 19:00  →  folder: /home/user/videos/series_B
#   003_episode.mp4
#   ...
# State updated.
```

For a time-window schedule, the resume offset is calculated and saved too:

```bash
python3 vlc_scheduler.py --advance 13:00
# Simulated playback at 13:00–14:30  →  folder: /home/user/videos/series_A
#   004_episode.mp4
#   005_episode.mp4
#   006_episode.mp4
#   007_episode.mp4
# Resume offset saved: 87.3s
# State updated.
```

Useful for skipping videos or fast-forwarding through the sequence during testing.

### Shift a slot's position

Move a slot forward or backward by a number of episodes, without playing anything and without editing `playback_state.json` by hand:

```bash
python3 vlc_scheduler.py --shift 15:00 +2     # skip two episodes ahead
python3 vlc_scheduler.py --shift 15:00 -1     # go back one episode
```

```
# Slot 15:00  →  /home/user/videos/series_A
#   before:  EP28.mkv  @1981.8s
#   after:   EP30.mkv  @0.0s   (+2 episode(s))
#   resume_offset reset 1981.8s → 0.0s (pass --keep-offset to hold the slot's alignment)
# State updated.
# Next session at 15:00 would play (first seeked to 0.0s):
#   EP30.mkv
#   EP31.mkv
```

`SLOT` is a schedule time (`15:00`) or a folder path (`/home/user/videos/series_A`). Naming a mirror slot shifts the primary it replays, since mirrors hold no state of their own.

By default the resume offset is reset to `0`, so the target episode starts from the beginning — that is almost always what you want when correcting a slot by hand. Pass `--keep-offset` to move the episode while holding the slot's alignment with its time window:

```bash
python3 vlc_scheduler.py --shift 15:00 +2 --keep-offset
#   before:  EP28.mkv  @1981.8s
#   after:   EP30.mkv  @1981.8s
```

Add `--dry-run` to preview without writing. Movement is clamped at both ends of the slot rather than wrapping, and a slot whose folders are all exhausted rolls over to the first folder on its next session, as usual.

For a slot spanning several folders, the folders count as one continuous run, so stepping back off the first episode of one folder lands on the last episode of the previous one and moves `folder_index` with it.

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
      "end_time": "14:30",
      "folders": [{"path": "/home/user/videos/series_A", "count": 1}],
      "active_folder": "/home/user/videos/series_A",
      "last_played": "007_title.mp4",
      "resume_offset": 87.3
    },
    {
      "time": "19:00",
      "folders": [
        {"path": "/home/user/videos/series_B", "count": 3},
        {"path": "/home/user/videos/series_C", "count": 1}
      ],
      "active_folder": "/home/user/videos/series_B",
      "last_played": "003_title.mp4"
    },
    {
      "time": "08:00",
      "end_time": "09:00",
      "mirrors": "13:00",
      "folders": [{"path": "/home/user/videos/series_A", "count": 1}],
      "active_folder": "/home/user/videos/series_A",
      "last_played": "007_title.mp4",
      "resume_offset": 87.3
    }
  ]
}
```

`resume_offset` (seconds) only appears for time-window schedules and shows how far into the first episode of the next session VLC will seek.

A mirror slot holds no folders or state of its own, so its row reports the position of the primary it replays and names that primary in `mirrors`. The `time` and `end_time` stay the mirror's own.

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

**`'ffprobe' not found` on startup:**
`end_time` scheduling requires `ffprobe` (part of `ffmpeg`): `sudo apt install ffmpeg`.

**Choppy video playback:**
VLC uses OpenGL output (`--vout gl`) and hardware decoding (`--avcodec-hw any`) by default. Verify VA-API is working:
```bash
vainfo
sudo intel_gpu_top  # Render/3D should show activity during playback
```
If `vainfo` fails, install the driver: `sudo apt install intel-media-va-driver`.

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

**Mouse cursor visible on screen:**
Make sure `unclutter` is installed (`apt install unclutter`) and the `~/.xinitrc` was created by `setup_kiosk.sh`.

**No sound:**
Run `alsamixer` via SSH and check that Master/PCM channels are unmuted (press `M` to toggle). Use `aplay -l` to list sound devices.

**WiFi doesn't reconnect after reboot:**
Verify with `nmcli con show` — your connection should have `autoconnect: yes`. If not:

```bash
nmcli con mod "YourWiFiName" connection.autoconnect yes
```
