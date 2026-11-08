# CleanFade

CleanFade is a desktop app for Windows, macOS, and Linux that lowers Spotify volume when profanity is detected in currently playing audio.

The project includes:

- A Python monitor engine (`spotify_duck.py`) that performs loopback capture, transcription, lyrics alignment, and volume ducking.
- A Tauri desktop wrapper (`src-tauri` + `src`) with start/stop controls and live logs.
- A cross-platform sidecar build script (`scripts/build_sidecar.py`) that packages the Python engine and bundles it with Tauri.

## How it works

1. Capture your default speaker output via loopback (or a microphone / virtual audio device).
2. Transcribe chunks with `faster-whisper`.
3. Match transcript text against profanity words.
4. Optionally align transcript to synced lyrics to estimate song position and pre-duck before profane lines.
5. Reduce Spotify volume by a configured percentage.
6. Restore volume after the configured hold duration.

### Platform volume backends

| OS | Volume control | Loopback notes |
| --- | --- | --- |
| Windows | per-app session volume via `pycaw` | Native WASAPI loopback |
| macOS | Spotify AppleScript (`osascript`) | Install [BlackHole](https://existential.audio/blackhole/) (or similar) and select it with `--input-device`, or route Spotify through a Multi-Output Device |
| Linux | PulseAudio/PipeWire `pactl` sink-input volume (`playerctl` fallback) | Pulse/PipeWire monitor sources; install `pulseaudio-utils` / `pipewire-pulse` |

## Prerequisites

Shared on every OS:

- Python 3.12 recommended for sidecar packaging (3.10–3.12 supported; 3.13+ blocked)
- Node.js 18+
- Rust toolchain
- Spotify desktop app with active playback
- Optional Conda (Miniconda/Anaconda). If Conda is on `PATH`, packaging uses the `cleanfade` env by default.

### Windows requirements

- Windows 10/11 (x64)
- [Visual Studio C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) (for Rust/Tauri)
- WebView2 (usually already installed on Windows 10/11)
- Native WASAPI loopback (no extra audio driver needed)
- `pycaw` / `comtypes` from `requirements.txt` for Spotify session volume

Install Rust:

```powershell
winget install Rustlang.Rustup
```

### macOS requirements

- macOS 11+ (Intel or Apple Silicon)
- Xcode Command Line Tools:

```bash
xcode-select --install
```

- [BlackHole](https://existential.audio/blackhole/) (or Soundflower / Loopback) for system-audio capture
  - Create a Multi-Output Device that includes your speakers + BlackHole, play Spotify through it, then set `--input-device BlackHole` (or the matching device name)
- Grant **Microphone** access and **Automation** access for Spotify when macOS prompts
- Volume ducking uses built-in `osascript` (AppleScript) against Spotify

Install Rust:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

### Linux requirements

- Modern distro with X11 or Wayland
- PulseAudio or PipeWire (with Pulse compatibility) for capture + volume control
- `pactl` required for Spotify sink-input volume (`playerctl` works as a fallback)
- Tauri/WebKitGTK build packages

Debian/Ubuntu example:

```bash
sudo apt-get update
sudo apt-get install -y \
  libwebkit2gtk-4.1-dev \
  libjavascriptcoregtk-4.1-dev \
  libgtk-3-dev \
  libayatana-appindicator3-dev \
  librsvg2-dev \
  patchelf \
  libasound2-dev \
  portaudio19-dev \
  pulseaudio-utils
# optional fallback volume backend:
# sudo apt-get install -y playerctl
```

Fedora example:

```bash
sudo dnf install -y \
  webkit2gtk4.1-devel \
  gtk3-devel \
  libappindicator-gtk3-devel \
  librsvg2-devel \
  alsa-lib-devel \
  portaudio-devel \
  pipewire-utils
# optional fallback volume backend:
# sudo dnf install -y playerctl
```

Arch example:

```bash
sudo pacman -S --needed \
  webkit2gtk-4.1 \
  gtk3 \
  libappindicator-gtk3 \
  librsvg \
  portaudio \
  pipewire-pulse
# optional fallback volume backend:
# sudo pacman -S --needed playerctl
```

Install Rust:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

## Quick start (Tauri app)

```bash
npm install
npm run tauri:dev
```

What this does:

1. Builds Python sidecar (`scripts/build_sidecar.py`).
2. Launches Tauri desktop app in dev mode.

## Build packaged desktop app

```bash
npm run tauri:build
```

Output installers/bundles are generated under:

- `src-tauri/target/release/bundle`

## Sidecar packaging details

The script `scripts/build_sidecar.py`:

1. Uses Conda environment `cleanfade` when Conda is available (override with `CLEANFADE_CONDA_ENV`), otherwise the current Python (`CLEANFADE_PYTHON` can force a binary).
2. Creates that Conda environment with Python 3.12 if missing.
3. Installs runtime dependencies + PyInstaller.
4. Builds one-file engine executable as `src-tauri/bin/cleanfade-engine` (`.exe` on Windows).
5. Copies host-triple binary name required by Tauri bundling, for example:
   - `src-tauri/bin/cleanfade-engine-x86_64-pc-windows-msvc.exe`
   - `src-tauri/bin/cleanfade-engine-aarch64-apple-darwin`
   - `src-tauri/bin/cleanfade-engine-x86_64-unknown-linux-gnu`

If the selected Python is 3.13+, the build script stops and asks for Python 3.12.

## CLI fallback (without Tauri)

```bash
python -m pip install -r requirements.txt
python spotify_duck.py
```

Windows Conda example:

```powershell
conda create -n cleanfade python=3.12 -y
conda run -n cleanfade python -m pip install -r requirements.txt
conda run -n cleanfade python spotify_duck.py
```

Common options:

```bash
python spotify_duck.py --duck-percent 50 --hold-seconds 3 --model-size tiny.en
```

- `--duck-percent`: volume reduction percent (0-100)
- `--hold-seconds`: lowered-volume duration after detected profanity
- `--model-size`: whisper model (`tiny`, `tiny.en`, `base`, `base.en`, `small`, `small.en`, `medium`, `medium.en`)
- `--chunk-seconds`: chunk duration in seconds
- `--profanity-file`: optional custom words file (one word per line)
- `--input-source`: `loopback` or `microphone`
- `--input-device`: optional partial device name for capture selection
- `--lyrics-mode` / `--no-lyrics-mode`: enable or disable lyrics-assisted pre-ducking
- `--log-transcript` / `--no-log-transcript`: print recognized text chunks
- `--list-devices`: print capture devices and exit

## Tokenless Lyrics Workflow (CSV + LRCLib)

CleanFade supports a tokenless lyrics-cache workflow using third-party lyrics data from LRCLib and playlist exports.

- Spotify users: export playlist CSV with Exportify.
- Other platforms (Apple Music, YouTube Music, etc.): export playlist CSV with TuneMyMusic.

Run live monitor without waiting for full lyrics download (CSV prefetch starts in background):

```bash
python spotify_duck.py --import-playlist-csv Exportify.csv --import-playlist-csv TuneMyMusic.csv
```

Or run prefetch-only mode (blocking) to warm cache first:

```bash
python spotify_duck.py --prefetch-only --import-playlist-csv Exportify.csv --import-playlist-csv TuneMyMusic.csv
```

Notes:

- No Spotify API token is required for CSV workflow.
- During normal runs, CSV prefetch happens in a background thread and monitoring starts immediately.
- Cache is stored in `.lyrics_cache/`.
- New cache entries include `lyrics_lines` with timestamped lyric text; older entries may only include `profanity_timestamps_ms` and are upgraded over time.

## Notes and limitations

- Detection is near real-time, not frame-perfect.
- Transcription errors can cause misses or false positives.
- Audio is captured from speaker loopback / virtual devices, not the raw Spotify stream.
- Spotify must have an active playback session for volume control.
- On macOS, grant Automation access so CleanFade can change Spotify volume.
- Lyrics alignment quality depends on available synced lyrics and transcription accuracy.

## Safety

- Start with a low duck value such as 25.
- Stopping the monitor restores baseline Music volume.
