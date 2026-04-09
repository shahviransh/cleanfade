# CleanFade

CleanFade is a Windows desktop app that lowers Spotify volume when profanity is detected in currently playing audio.

The project includes:

- A Python monitor engine (`spotify_duck.py`) that performs loopback capture, transcription, lyrics alignment, and volume ducking.
- A Tauri desktop wrapper (`src-tauri` + `src`) with start/stop controls and live logs.
- A sidecar build script (`scripts/build_sidecar.ps1`) that packages the Python engine and bundles it with Tauri.

## How it works

1. Capture your default speaker output via loopback.
2. Transcribe chunks with `faster-whisper`.
3. Match transcript text against profanity words.
4. Optionally align transcript to synced lyrics to estimate song position and pre-duck before profane lines.
5. Reduce Spotify session volume by a configured percentage.
6. Restore volume after the configured hold duration.

## Prerequisites

- Windows
- Conda (Miniconda or Anaconda) on PATH for sidecar packaging
- Python 3.12 in the Conda environment used for packaging
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

1. Uses Conda environment `cleanfade` by default (or `CLEANFADE_CONDA_ENV` if set).
2. Creates that environment with Python 3.12 if missing.
3. Installs runtime dependencies + PyInstaller via `conda run`.
4. Builds one-file engine executable as `src-tauri/bin/cleanfade-engine.exe`.
5. Copies host-triple binary name required by Tauri bundling, for example:
   - `src-tauri/bin/cleanfade-engine-x86_64-pc-windows-msvc.exe`

If the selected Conda env is Python 3.13+, the build script stops and asks for Python 3.12.

## CLI fallback (without Tauri)

You can still run engine-only mode directly:

```powershell
conda create -n cleanfade python=3.12 -y
conda run -n cleanfade python -m pip install -r requirements.txt
conda run -n cleanfade python spotify_duck.py
```

If you prefer local venv for engine-only testing:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python spotify_duck.py
```

Common options:

```powershell
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

## Tokenless Lyrics Workflow (CSV + LRCLib)

CleanFade supports a tokenless lyrics-cache workflow using third-party lyrics data from LRCLib and playlist exports.

- Spotify users: export playlist CSV with Exportify.
- Other platforms (Apple Music, YouTube Music, etc.): export playlist CSV with TuneMyMusic.

Run live monitor without waiting for full lyrics download (CSV prefetch starts in background):

```powershell
python spotify_duck.py --import-playlist-csv Exportify.csv --import-playlist-csv TuneMyMusic.csv
```

Or run prefetch-only mode (blocking) to warm cache first:

```powershell
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
- Audio is captured from speaker loopback, not raw Spotify stream.
- Spotify must have an active audio session for volume control.
- Lyrics alignment quality depends on available synced lyrics and transcription accuracy.

## Safety

- Start with a low duck value such as 25.
- Stopping the monitor restores baseline Spotify volume.
