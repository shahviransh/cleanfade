# CleanFade

CleanFade is a Windows desktop app that lowers Spotify volume when profanity is detected in currently playing audio.

The project includes:

- A Python monitor engine (`spotify_duck.py`) that performs loopback capture, transcription, and volume ducking.
- A Tauri desktop wrapper (`src-tauri` + `src`) with start/stop controls and live logs.
- A sidecar build script that packages the Python engine into an executable and bundles it with the Tauri app.

## How it works

1. Capture your default speaker output via loopback.
2. Transcribe chunks with `faster-whisper`.
3. Match transcript text against profanity words.
4. Reduce Spotify session volume by a configured percentage.
5. Restore volume after the configured hold duration.

## Prerequisites

- Windows
- Python 3.12 recommended (required for sidecar packaging)
- Node.js 18+
- Rust toolchain
- Visual Studio C++ Build Tools (for Rust/Tauri on Windows)
- Spotify desktop app with active playback

Install Rust if needed:

```powershell
winget install Rustlang.Rustup
```

## Quick start (Tauri app)

```powershell
npm install
npm run tauri:dev
```

What this does:

1. Builds Python sidecar (`scripts/build_sidecar.ps1`).
2. Launches Tauri desktop app in dev mode.

## Build packaged desktop app

```powershell
npm run tauri:build
```

Output installers/bundles are generated under:

- `src-tauri/target/release/bundle`

## Sidecar packaging details

The script `scripts/build_sidecar.ps1`:

1. Creates/uses `.venv`.
2. Installs runtime dependencies + PyInstaller.
3. Builds one-file engine executable as `src-tauri/bin/cleanfade-engine.exe`.
4. Copies host-triple binary name required by Tauri bundling, for example:
   - `src-tauri/bin/cleanfade-engine-x86_64-pc-windows-msvc.exe`

If you have Python 3.14 as default, the script will stop and ask for Python 3.12 because PyInstaller + NumPy packaging is unstable on 3.14 in this workflow.

## CLI fallback (without Tauri)

You can still run engine-only mode directly:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python spotify_duck.py
```

If your default Python is 3.14, use Python 3.12 for the virtual environment:

```powershell
py -3.12 -m venv .venv
```

Common options:

```powershell
python spotify_duck.py --duck-percent 50 --hold-seconds 3 --model-size tiny.en
```

- `--duck-percent`: volume reduction percent (0-100)
- `--hold-seconds`: lowered-volume duration after detected profanity
- `--model-size`: whisper model (`tiny.en`, `base.en`, `small.en`)
- `--chunk-seconds`: chunk duration in seconds
- `--profanity-file`: optional custom words file (one word per line)

## Notes and limitations

- Detection is near real-time, not frame-perfect.
- Transcription errors can cause misses or false positives.
- Audio is captured from speaker loopback, not raw Spotify stream.
- Spotify must have an active audio session for volume control.

## Safety

- Start with a low duck value such as 25.
- Stopping the monitor restores baseline Spotify volume.
