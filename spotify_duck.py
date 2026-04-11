import argparse
from collections import Counter
import csv
import hashlib
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Keep COM in MTA mode so pycaw/comtypes matches thread state set by other audio libs.
sys.coinit_flags = 0

import numpy as np
import requests
import soundcard as sc
from faster_whisper import WhisperModel


DEFAULT_BAD_WORDS = {
    "ass",
    "asshole",
    "bastard",
    "bitch",
    "bullshit",
    "damn",
    "dick",
    "fuck",
    "fucking",
    "hell",
    "motherfucker",
    "shit",
}

DEFAULT_BAD_WORD_VARIANTS = {
    "ass": {"asses"},
    "asshole": {"assholes"},
    "bastard": {"bastards"},
    "bitch": {"bitches"},
    "bullshit": {"bullshits", "bullshitting"},
    "damn": {"damned", "damns"},
    "dick": {"dicks"},
    "fuck": {"fucked", "fucker", "fuckers", "fucks"},
    "fucking": {"fuckin"},
    "hell": {"hells"},
    "motherfucker": {"motherfuckers"},
    "shit": {"shits", "shitty", "shitting"},
}

TOKEN_RE = re.compile(r"[a-z0-9']+")
VAD_ASSET_NAME = "silero_encoder_v5.onnx"
LARGE_WHISPER_MODELS = {"large-v1", "large-v2", "large-v3", "large-v3-turbo"}
FAST_START_BOOTSTRAP_MODEL = "small"
LOOPBACK_CHUNK_SECONDS_DEFAULT = 1.2
LOOPBACK_LYRICS_PREDUCK_SECONDS_DEFAULT = 1.25
LOOPBACK_LYRICS_POLL_SECONDS_DEFAULT = 0.6
ESTIMATION_TOKEN_HISTORY_MAX = 8
ESTIMATION_TOKEN_UNIQUE_MAX = 360
ESTIMATION_TOKEN_UNIQUE_PRUNED = 240

_VAD_FILTER_LOCK = threading.Lock()
_VAD_FILTER_STATE = {"enabled": True}


def _is_missing_vad_asset_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return VAD_ASSET_NAME.lower() in message or (
        "no_suchfile" in message and "faster_whisper" in message and "assets" in message
    )


def configure_huggingface_runtime() -> None:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    warnings.filterwarnings(
        "ignore",
        message=r"`huggingface_hub` cache-system uses symlinks by default.*",
        category=UserWarning,
    )

    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)


def build_word_regex(words: set[str]) -> re.Pattern[str]:
    escaped = sorted((re.escape(word) for word in words), key=len, reverse=True)
    pattern = r"\b(?:" + "|".join(escaped) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


def expand_profanity_words(words: set[str]) -> set[str]:
    expanded = set(words)
    for base_word, variants in DEFAULT_BAD_WORD_VARIANTS.items():
        if base_word in expanded:
            expanded.update(variants)
    return expanded


def load_custom_words(path: Path | None) -> set[str]:
    if path is None:
        return set()

    if not path.exists():
        raise FileNotFoundError(f"Word list file not found: {path}")

    words: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip().lower()
        if not candidate or candidate.startswith("#"):
            continue
        words.add(candidate)

    return words


def rms_level(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return math.sqrt(float(np.mean(np.square(samples))))


@dataclass
class DuckState:
    baseline_volume: float = 1.0
    is_ducked: bool = False
    ducked_until: float = 0.0


@dataclass
class LyricsMonitorState:
    current_track_id: str = ""
    current_track_name: str = ""
    spotify_track_id: str = ""
    profanity_timestamps_ms: list[int] = field(default_factory=list)
    lyrics_lines: list["LyricLine"] = field(default_factory=list)
    triggered_timestamps_ms: set[int] = field(default_factory=set)
    current_line_index: int = -1
    current_progress_ms: int = -1
    last_alignment_ms: int = -1
    progress_anchor_ms: int = -1
    progress_anchor_wall_time: float = 0.0
    no_match_chunks: int = 0
    lyrics_library: list["LyricsLibraryTrack"] = field(default_factory=list)
    ordered_track_ids: list[str] = field(default_factory=list)
    transcript_history: list[str] = field(default_factory=list)
    transcript_token_counts: Counter[str] = field(default_factory=Counter)
    last_poll_time: float = 0.0
    last_error: str = ""
    lyrics_fetch_request_id: int = 0
    lyrics_fetch_inflight_track_id: str = ""
    lyrics_fetch_result: "PendingLyricsFetchResult | None" = None
    lyrics_fetch_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class LyricLine:
    timestamp_ms: int
    text: str
    normalized: str
    token_set: set[str]
    has_profanity: bool


@dataclass
class TrackLyricsData:
    profanity_timestamps_ms: list[int]
    lyrics_lines: list[LyricLine]


@dataclass
class LyricsLibraryTrack:
    track_id: str
    track_name: str
    artist_name: str
    profanity_timestamps_ms: list[int]
    lyrics_lines: list[LyricLine]
    token_union: set[str] = field(default_factory=set)


@dataclass
class PendingLyricsFetchResult:
    request_id: int
    track_id: str
    track_name: str
    artist_name: str
    data: TrackLyricsData | None = None
    error: str = ""


class SpotifyVolumeController:
    def __init__(self) -> None:
        try:
            from pycaw.pycaw import AudioUtilities
        except ImportError as exc:
            raise RuntimeError(
                "Music volume control backend (pycaw) is unavailable on this platform. "
                "CleanFade currently supports Music volume ducking on Windows."
            ) from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) == -2147417850:
                raise RuntimeError(
                    "COM initialization conflict while loading pycaw. "
                    "Please relaunch CleanFade and avoid starting it from a host process "
                    "that forces STA COM mode."
                ) from exc
            raise

        self._audio_utilities = AudioUtilities
        self._state = DuckState()

    def _spotify_simple_volume(self):
        for session in self._audio_utilities.GetAllSessions():
            process = session.Process
            if process is None:
                continue
            process_name = process.name().lower()
            if process_name.startswith("spotify"):
                return session.SimpleAudioVolume
        return None

    def get_volume(self) -> float:
        simple_volume = self._spotify_simple_volume()
        if simple_volume is None:
            raise RuntimeError("Spotify audio session not found. Start playback and keep Spotify open.")
        return float(simple_volume.GetMasterVolume())

    def set_volume(self, level: float) -> None:
        simple_volume = self._spotify_simple_volume()
        if simple_volume is None:
            raise RuntimeError("Spotify audio session not found. Start playback and keep Spotify open.")
        bounded = max(0.0, min(1.0, level))
        simple_volume.SetMasterVolume(bounded, None)

    def duck(self, duck_percent: float, hold_seconds: float) -> None:
        now = time.time()
        if not self._state.is_ducked:
            self._state.baseline_volume = self.get_volume()

        reduction_factor = max(0.0, min(1.0, duck_percent / 100.0))
        target = self._state.baseline_volume * (1.0 - reduction_factor)
        self.set_volume(target)

        self._state.is_ducked = True
        self._state.ducked_until = now + hold_seconds

    def restore_if_due(self) -> None:
        if not self._state.is_ducked:
            return
        if time.time() < self._state.ducked_until:
            return

        self.set_volume(self._state.baseline_volume)
        self._state.is_ducked = False

    def restore_now(self) -> None:
        if not self._state.is_ducked:
            return
        self.set_volume(self._state.baseline_volume)
        self._state.is_ducked = False


def transcribe_chunk(
    model: WhisperModel,
    chunk: np.ndarray,
    language: str,
) -> str:
    with _VAD_FILTER_LOCK:
        use_vad_filter = bool(_VAD_FILTER_STATE.get("enabled", True))

    try:
        segments, _ = model.transcribe(
            chunk,
            language=language,
            beam_size=1,
            best_of=1,
            vad_filter=use_vad_filter,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
    except Exception as exc:
        if not use_vad_filter or not _is_missing_vad_asset_error(exc):
            raise

        with _VAD_FILTER_LOCK:
            if bool(_VAD_FILTER_STATE.get("enabled", True)):
                _VAD_FILTER_STATE["enabled"] = False
                print(
                    "[transcribe] warning: missing faster-whisper VAD assets in sidecar; continuing with VAD disabled.",
                    flush=True,
                )

        segments, _ = model.transcribe(
            chunk,
            language=language,
            beam_size=1,
            best_of=1,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
        )

    return " ".join(segment.text.strip() for segment in segments if segment.text).strip()


def configure_transcription_mode(input_source: str) -> None:
    normalized_source = input_source.strip().lower()
    use_vad_filter = normalized_source != "loopback"

    with _VAD_FILTER_LOCK:
        _VAD_FILTER_STATE["enabled"] = use_vad_filter

    if use_vad_filter:
        print("[transcribe] VAD filter enabled (microphone mode).", flush=True)
    else:
        print("[transcribe] VAD filter disabled for loopback/music capture.", flush=True)


def load_whisper_model(model_size: str) -> WhisperModel:
    model_load_started = time.time()
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    print(f"[model] ready model={model_size} in {time.time() - model_load_started:.1f}s", flush=True)
    return model


def _spotify_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _extract_track_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    item = payload.get("item")
    if not item:
        return None

    track_id = item.get("id")
    if not track_id:
        return None

    artists = item.get("artists") or []
    artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name"))

    return {
        "track_id": track_id,
        "track_name": item.get("name", ""),
        "artist_name": artist_name,
        "progress_ms": int(payload.get("progress_ms") or 0),
        "is_playing": bool(payload.get("is_playing")),
    }


def get_current_spotify_track(token: str) -> dict[str, Any] | None:
    response = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers=_spotify_headers(token),
        timeout=5,
    )

    if response.status_code == 204:
        # Fallback endpoint is sometimes more reliable depending on client/device state.
        response = requests.get(
            "https://api.spotify.com/v1/me/player",
            headers=_spotify_headers(token),
            timeout=5,
        )
        if response.status_code == 204:
            return None

    if response.status_code == 401:
        raise RuntimeError("Spotify token is invalid or expired.")

    if response.status_code == 403:
        raise RuntimeError(
            "Spotify token missing required scope. Add 'user-read-currently-playing' and 'user-read-playback-state'."
        )

    if response.status_code >= 400:
        raise RuntimeError(f"Spotify API returned {response.status_code}: {response.text[:200]}")

    payload = response.json()
    return _extract_track_payload(payload)


def fetch_synced_lyrics(track_name: str, artist_name: str) -> str | None:
    response = requests.get(
        "https://lrclib.net/api/search",
        params={"track_name": track_name, "artist_name": artist_name},
        timeout=8,
    )

    if response.status_code >= 400:
        return None

    entries = response.json()
    if not isinstance(entries, list):
        return None

    for entry in entries:
        synced = entry.get("syncedLyrics")
        if synced:
            return str(synced)

    return None


def normalize_lyric_text(value: str) -> str:
    lowered = value.lower()
    tokens = TOKEN_RE.findall(lowered)
    return " ".join(tokens)


def tokenize_lyric_text(value: str) -> set[str]:
    return set(TOKEN_RE.findall(value.lower()))


def parse_synced_lyrics_lines(synced_lyrics: str, profanity_pattern: re.Pattern[str]) -> list[LyricLine]:
    lines: list[LyricLine] = []
    line_pattern = re.compile(r"^\[(\d{1,2}):(\d{2}(?:\.\d+)?)\](.*)$")

    for raw_line in synced_lyrics.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = line_pattern.match(line)
        if not match:
            continue

        minutes = int(match.group(1))
        seconds = float(match.group(2))
        text = match.group(3).strip()
        if not text:
            continue

        normalized = normalize_lyric_text(text)
        if not normalized:
            continue

        timestamp_ms = int((minutes * 60 + seconds) * 1000)
        lines.append(
            LyricLine(
                timestamp_ms=timestamp_ms,
                text=text,
                normalized=normalized,
                token_set=tokenize_lyric_text(normalized),
                has_profanity=bool(profanity_pattern.search(normalized)),
            )
        )

    lines.sort(key=lambda item: item.timestamp_ms)
    return lines


def extract_profanity_timestamps_ms(lyrics_lines: list[LyricLine]) -> list[int]:
    timestamps = [line.timestamp_ms for line in lyrics_lines if line.has_profanity]

    return sorted(set(timestamps))


def _lyrics_cache_path(cache_dir: Path, track_id: str) -> Path:
    return cache_dir / f"{track_id}.json"


def _parse_cached_lyrics_lines(raw_lines: Any, profanity_pattern: re.Pattern[str]) -> list[LyricLine]:
    if not isinstance(raw_lines, list):
        return []

    parsed: list[LyricLine] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue

        timestamp = item.get("timestamp_ms")
        text = item.get("text")
        if not isinstance(timestamp, int) or not isinstance(text, str):
            continue

        normalized = normalize_lyric_text(text)
        if not normalized:
            continue

        parsed.append(
            LyricLine(
                timestamp_ms=timestamp,
                text=text,
                normalized=normalized,
                token_set=tokenize_lyric_text(normalized),
                has_profanity=bool(profanity_pattern.search(normalized)),
            )
        )

    parsed.sort(key=lambda item: item.timestamp_ms)
    return parsed


def load_cached_track_lyrics(
    cache_dir: Path,
    track_id: str,
    profanity_pattern: re.Pattern[str],
) -> TrackLyricsData | None:
    cache_file = _lyrics_cache_path(cache_dir, track_id)
    if not cache_file.exists():
        return None

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    lyrics_lines = _parse_cached_lyrics_lines(payload.get("lyrics_lines"), profanity_pattern)
    if lyrics_lines:
        return TrackLyricsData(
            profanity_timestamps_ms=extract_profanity_timestamps_ms(lyrics_lines),
            lyrics_lines=lyrics_lines,
        )

    timestamps = payload.get("profanity_timestamps_ms")
    if not isinstance(timestamps, list):
        return None

    normalized: list[int] = []
    for item in timestamps:
        if isinstance(item, int):
            normalized.append(item)

    return TrackLyricsData(profanity_timestamps_ms=normalized, lyrics_lines=[])


def save_cached_lyrics_timestamps(
    cache_dir: Path,
    track_id: str,
    track_name: str,
    artist_name: str,
    profanity_timestamps_ms: list[int],
    lyrics_lines: list[LyricLine],
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "track_id": track_id,
        "track_name": track_name,
        "artist_name": artist_name,
        "profanity_timestamps_ms": profanity_timestamps_ms,
        "lyrics_lines": [
            {
                "timestamp_ms": line.timestamp_ms,
                "text": line.text,
            }
            for line in lyrics_lines
        ],
    }
    cache_file = _lyrics_cache_path(cache_dir, track_id)
    temp_file = cache_file.with_name(
        f"{cache_file.name}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    )

    try:
        temp_file.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        os.replace(temp_file, cache_file)
    finally:
        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError:
            pass


def get_track_lyrics_data(
    track_id: str,
    track_name: str,
    artist_name: str,
    profanity_pattern: re.Pattern[str],
    cache_dir: Path,
) -> TrackLyricsData:
    cached = load_cached_track_lyrics(cache_dir, track_id, profanity_pattern)
    if cached is not None and cached.lyrics_lines:
        return cached

    # Legacy cache entries may only include profanity timestamps.
    # Refresh once to upgrade cache with full synced lyric lines.
    if cached is not None and not cached.lyrics_lines:
        synced_lyrics = fetch_synced_lyrics(track_name, artist_name)
        if not synced_lyrics:
            return cached

        lyrics_lines = parse_synced_lyrics_lines(synced_lyrics, profanity_pattern)
        profanity_timestamps_ms = extract_profanity_timestamps_ms(lyrics_lines)
        upgraded = TrackLyricsData(profanity_timestamps_ms=profanity_timestamps_ms, lyrics_lines=lyrics_lines)
        save_cached_lyrics_timestamps(
            cache_dir,
            track_id,
            track_name,
            artist_name,
            upgraded.profanity_timestamps_ms,
            upgraded.lyrics_lines,
        )
        return upgraded

    synced_lyrics = fetch_synced_lyrics(track_name, artist_name)
    if not synced_lyrics:
        empty_data = TrackLyricsData(profanity_timestamps_ms=[], lyrics_lines=[])
        save_cached_lyrics_timestamps(
            cache_dir,
            track_id,
            track_name,
            artist_name,
            empty_data.profanity_timestamps_ms,
            empty_data.lyrics_lines,
        )
        return empty_data

    lyrics_lines = parse_synced_lyrics_lines(synced_lyrics, profanity_pattern)
    profanity_timestamps_ms = extract_profanity_timestamps_ms(lyrics_lines)
    data = TrackLyricsData(profanity_timestamps_ms=profanity_timestamps_ms, lyrics_lines=lyrics_lines)
    save_cached_lyrics_timestamps(
        cache_dir,
        track_id,
        track_name,
        artist_name,
        data.profanity_timestamps_ms,
        data.lyrics_lines,
    )
    return data


def fetch_playlist_tracks(token: str, playlist_id: str) -> list[tuple[str, str, str]]:
    tracks: list[tuple[str, str, str]] = []
    offset = 0

    while True:
        response = requests.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            headers=_spotify_headers(token),
            params={"limit": 100, "offset": offset},
            timeout=8,
        )

        if response.status_code == 401:
            raise RuntimeError("Spotify token is invalid or expired.")
        if response.status_code >= 400:
            raise RuntimeError(f"Spotify playlist API returned {response.status_code}: {response.text[:200]}")

        payload = response.json()
        items = payload.get("items") or []
        for item in items:
            track = item.get("track") or {}
            track_id = track.get("id")
            if not track_id:
                continue

            track_name = track.get("name", "")
            artists = track.get("artists") or []
            artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name"))
            tracks.append((track_id, track_name, artist_name))

        if not payload.get("next"):
            break

        offset += len(items)
        if not items:
            break

    return tracks


def prefetch_playlist_lyrics(
    token: str,
    playlist_id: str,
    profanity_pattern: re.Pattern[str],
    cache_dir: Path,
) -> tuple[int, int]:
    tracks = fetch_playlist_tracks(token, playlist_id)
    with_profanity = 0

    for track_id, track_name, artist_name in tracks:
        data = get_track_lyrics_data(
            track_id=track_id,
            track_name=track_name,
            artist_name=artist_name,
            profanity_pattern=profanity_pattern,
            cache_dir=cache_dir,
        )
        if data.profanity_timestamps_ms:
            with_profanity += 1

    return len(tracks), with_profanity


def _first_artist_name(raw_artist: str) -> str:
    value = raw_artist.strip()
    if not value:
        return ""

    if ";" in value:
        return value.split(";", 1)[0].strip()

    return value


def _csv_field(row: dict[str, str], lower_to_real: dict[str, str], candidates: list[str]) -> str:
    for candidate in candidates:
        real_name = lower_to_real.get(candidate.lower())
        if not real_name:
            continue
        return (row.get(real_name) or "").strip()
    return ""


def _is_exportify_header(lower_to_real: dict[str, str]) -> bool:
    return "track name" in lower_to_real and "artist name(s)" in lower_to_real


def _is_tunemymusic_header(lower_to_real: dict[str, str]) -> bool:
    return "track name" in lower_to_real and "artist name" in lower_to_real


def parse_tracks_from_playlist_csv(csv_path: Path) -> list[tuple[str, str, str]]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Playlist export file not found: {csv_path}")

    tracks: list[tuple[str, str, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return tracks

        lower_to_real = {field.strip().lower(): field for field in reader.fieldnames}
        if not (_is_exportify_header(lower_to_real) or _is_tunemymusic_header(lower_to_real)):
            raise RuntimeError(
                "Unsupported CSV format. Expected Exportify or TuneMyMusic columns (Track name + Artist name)."
            )

        for row in reader:
            track_name = _csv_field(row, lower_to_real, ["Track name", "Track Name"])
            artist_name_raw = _csv_field(row, lower_to_real, ["Artist name", "Artist Name(s)"])
            spotify_id = _csv_field(row, lower_to_real, ["Spotify - id"]) 

            if not spotify_id:
                track_uri = _csv_field(row, lower_to_real, ["Track URI"])
                if track_uri.startswith("spotify:track:"):
                    spotify_id = track_uri.rsplit(":", 1)[-1]

            if not track_name:
                continue

            artist_name = _first_artist_name(artist_name_raw)
            tracks.append((track_name, artist_name, spotify_id))

    return tracks


def _csv_track_cache_key(track_name: str, artist_name: str, spotify_id: str) -> str:
    if spotify_id:
        return spotify_id

    digest = hashlib.sha1(f"{track_name.lower()}|{artist_name.lower()}".encode("utf-8")).hexdigest()
    return f"csv-{digest[:20]}"


def ordered_track_keys_from_csv_paths(csv_paths: list[Path]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    for csv_path in csv_paths:
        parsed_tracks = parse_tracks_from_playlist_csv(csv_path)
        for track_name, artist_name, spotify_id in parsed_tracks:
            cache_key = _csv_track_cache_key(track_name, artist_name, spotify_id)
            if cache_key in seen:
                continue
            seen.add(cache_key)
            ordered.append(cache_key)

    return ordered


def prefetch_csv_lyrics(
    csv_paths: list[Path],
    profanity_pattern: re.Pattern[str],
    cache_dir: Path,
) -> tuple[int, int, int]:
    unique_tracks: dict[str, tuple[str, str]] = {}

    for csv_path in csv_paths:
        parsed_tracks = parse_tracks_from_playlist_csv(csv_path)
        for track_name, artist_name, spotify_id in parsed_tracks:
            cache_key = _csv_track_cache_key(track_name, artist_name, spotify_id)
            if cache_key not in unique_tracks:
                unique_tracks[cache_key] = (track_name, artist_name)

    with_lyrics = 0
    with_profanity = 0

    for cache_key, (track_name, artist_name) in unique_tracks.items():
        data = get_track_lyrics_data(
            track_id=cache_key,
            track_name=track_name,
            artist_name=artist_name,
            profanity_pattern=profanity_pattern,
            cache_dir=cache_dir,
        )
        with_lyrics += 1
        if data.profanity_timestamps_ms:
            with_profanity += 1

    return len(unique_tracks), with_lyrics, with_profanity


def run_csv_prefetch_job(
    csv_paths: list[Path],
    profanity_pattern: re.Pattern[str],
    cache_dir: Path,
) -> None:
    try:
        total_tracks, with_lyrics, with_profanity = prefetch_csv_lyrics(
            csv_paths=csv_paths,
            profanity_pattern=profanity_pattern,
            cache_dir=cache_dir,
        )
        print(
            "[lyrics] csv-prefetch "
            f"tracks={total_tracks} "
            f"cached={with_lyrics} "
            f"tracks_with_profanity={with_profanity}",
            flush=True,
        )
    except (requests.RequestException, RuntimeError, OSError, ValueError) as exc:
        print(f"[lyrics] csv-prefetch warning: {exc}", flush=True)


def start_csv_prefetch_in_background(
    csv_paths: list[Path],
    profanity_pattern: re.Pattern[str],
    cache_dir: Path,
) -> threading.Thread:
    worker = threading.Thread(
        target=run_csv_prefetch_job,
        args=(csv_paths, profanity_pattern, cache_dir),
        name="lyrics-csv-prefetch",
        daemon=True,
    )
    worker.start()
    return worker


def line_match_score(transcript_normalized: str, transcript_tokens: set[str], line: LyricLine) -> float:
    if not transcript_tokens or not line.token_set:
        return 0.0

    overlap = transcript_tokens & line.token_set
    if not overlap:
        return 0.0

    overlap_ratio = len(overlap) / max(1, len(transcript_tokens))
    coverage_ratio = len(overlap) / max(1, len(line.token_set))

    phrase_bonus = 0.0
    if transcript_normalized and transcript_normalized in line.normalized:
        phrase_bonus = 0.25
    elif line.normalized and line.normalized in transcript_normalized:
        phrase_bonus = 0.15

    return overlap_ratio * 0.65 + coverage_ratio * 0.35 + phrase_bonus


def _clear_transcript_estimation_state(state: LyricsMonitorState) -> None:
    state.transcript_history.clear()
    state.transcript_token_counts.clear()


def _accumulate_transcript_tokens(state: LyricsMonitorState, transcript: str) -> None:
    normalized = normalize_lyric_text(transcript)
    if not normalized:
        return

    state.transcript_history.append(normalized)
    if len(state.transcript_history) > ESTIMATION_TOKEN_HISTORY_MAX:
        state.transcript_history = state.transcript_history[-ESTIMATION_TOKEN_HISTORY_MAX:]

    for token in TOKEN_RE.findall(normalized):
        if len(token) <= 1:
            continue
        previous = int(state.transcript_token_counts.get(token, 0))
        state.transcript_token_counts[token] = min(8, previous + 1)

    if len(state.transcript_token_counts) > ESTIMATION_TOKEN_UNIQUE_MAX:
        # Keep the most repeated tokens to preserve signal while bounding memory.
        state.transcript_token_counts = Counter(
            dict(state.transcript_token_counts.most_common(ESTIMATION_TOKEN_UNIQUE_PRUNED))
        )


def align_transcript_to_line(
    transcript: str,
    lyrics_lines: list[LyricLine],
    current_line_index: int,
    min_score: float,
) -> tuple[int, int, float] | None:
    transcript_normalized = normalize_lyric_text(transcript)
    transcript_tokens = tokenize_lyric_text(transcript_normalized)
    if not transcript_tokens or not lyrics_lines:
        return None

    if current_line_index >= 0:
        start = max(0, current_line_index - 8)
        end = min(len(lyrics_lines), current_line_index + 22)
        candidate_indices = range(start, end)
    else:
        candidate_indices = range(len(lyrics_lines))

    best_score = 0.0
    best_index = -1
    for index in candidate_indices:
        score = line_match_score(transcript_normalized, transcript_tokens, lyrics_lines[index])
        if score > best_score:
            best_score = score
            best_index = index

    if best_index < 0 or best_score < min_score:
        return None

    return best_index, lyrics_lines[best_index].timestamp_ms, best_score


def load_cached_lyrics_library(
    cache_dir: Path,
    profanity_pattern: re.Pattern[str],
    preferred_track_ids: list[str] | None = None,
) -> list[LyricsLibraryTrack]:
    if not cache_dir.exists():
        return []

    library: list[LyricsLibraryTrack] = []
    for cache_file in cache_dir.glob("*.json"):
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        track_id = str(payload.get("track_id") or cache_file.stem)
        track_name = str(payload.get("track_name") or track_id)
        artist_name = str(payload.get("artist_name") or "")
        lyrics_lines = _parse_cached_lyrics_lines(payload.get("lyrics_lines"), profanity_pattern)
        if not lyrics_lines:
            continue

        profanity_timestamps_ms = extract_profanity_timestamps_ms(lyrics_lines)
        token_union: set[str] = set()
        for line in lyrics_lines:
            token_union.update(line.token_set)

        library.append(
            LyricsLibraryTrack(
                track_id=track_id,
                track_name=track_name,
                artist_name=artist_name,
                profanity_timestamps_ms=profanity_timestamps_ms,
                lyrics_lines=lyrics_lines,
                token_union=token_union,
            )
        )

    if preferred_track_ids:
        ordered: list[LyricsLibraryTrack] = []
        seen_ids: set[str] = set()
        by_id = {track.track_id: track for track in library}

        for track_id in preferred_track_ids:
            candidate = by_id.get(track_id)
            if candidate is None or track_id in seen_ids:
                continue
            ordered.append(candidate)
            seen_ids.add(track_id)

        for candidate in library:
            if candidate.track_id in seen_ids:
                continue
            ordered.append(candidate)

        return ordered

    return library


def identify_track_from_transcript(
    transcript: str,
    library: list[LyricsLibraryTrack],
) -> tuple[LyricsLibraryTrack, int, int, float] | None:
    transcript_normalized = normalize_lyric_text(transcript)
    transcript_tokens = tokenize_lyric_text(transcript_normalized)
    if len(transcript_tokens) < 2 or not library:
        return None

    candidates: list[tuple[LyricsLibraryTrack, int]] = []
    for track in library:
        overlap_count = len(transcript_tokens & track.token_union)
        if overlap_count <= 0:
            continue
        candidates.append((track, overlap_count))

    if not candidates:
        return None

    # Keep stable order (which reflects user CSV priority), then prefer more token overlap.
    candidates.sort(key=lambda item: item[1], reverse=True)
    candidates = candidates[:48]

    best: tuple[LyricsLibraryTrack, int, int, float] | None = None
    second_best_score = 0.0

    for track, overlap_count in candidates:
        best_index = -1
        best_score = 0.0
        for index, line in enumerate(track.lyrics_lines):
            score = line_match_score(transcript_normalized, transcript_tokens, line)
            if score > best_score:
                best_score = score
                best_index = index

        if best_index < 0:
            continue

        overlap_bonus = min(0.12, overlap_count / max(1, len(transcript_tokens)) * 0.20)
        candidate_score = best_score + overlap_bonus
        candidate = (track, best_index, track.lyrics_lines[best_index].timestamp_ms, candidate_score)
        if best is None:
            best = candidate
            continue

        if candidate_score > best[3]:
            second_best_score = max(second_best_score, best[3])
            best = candidate
        else:
            second_best_score = max(second_best_score, candidate_score)

    if best is None:
        return None

    min_accept = 0.60 if len(transcript_tokens) >= 6 else 0.68
    if best[3] < min_accept:
        return None

    if second_best_score > 0 and (best[3] - second_best_score) < 0.07:
        return None

    return best


def identify_track_from_accumulated_tokens(
    transcript_token_counts: Counter[str],
    recent_transcript: str,
    library: list[LyricsLibraryTrack],
) -> tuple[LyricsLibraryTrack, int, int, float] | None:
    if not transcript_token_counts or not library:
        return None

    token_track_frequency: dict[str, int] = {}
    for track in library:
        for token in track.token_union:
            token_track_frequency[token] = token_track_frequency.get(token, 0) + 1

    token_weights: dict[str, float] = {}
    for token, count in transcript_token_counts.items():
        if count <= 0 or len(token) <= 1:
            continue

        track_frequency = token_track_frequency.get(token)
        if not track_frequency:
            continue

        token_weights[token] = min(float(count), 3.0) / float(track_frequency)

    if len(token_weights) < 3:
        return None

    total_weight = sum(token_weights.values())
    if total_weight <= 0.0:
        return None

    weighted_token_set = set(token_weights.keys())
    candidates: list[tuple[LyricsLibraryTrack, float]] = []
    for track in library:
        overlap_tokens = weighted_token_set & track.token_union
        if len(overlap_tokens) < 2:
            continue

        overlap_weight = sum(token_weights[token] for token in overlap_tokens)
        weighted_precision = overlap_weight / total_weight
        unique_precision = len(overlap_tokens) / max(1, len(weighted_token_set))
        overlap_bonus = min(0.18, len(overlap_tokens) / 18.0)
        candidate_score = weighted_precision * 0.78 + unique_precision * 0.12 + overlap_bonus
        candidates.append((track, candidate_score))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[1], reverse=True)
    best_track, best_score = candidates[0]
    second_best_score = candidates[1][1] if len(candidates) > 1 else 0.0

    min_accept = 0.34 if len(weighted_token_set) >= 10 else 0.42
    if best_score < min_accept:
        return None

    if second_best_score > 0 and (best_score - second_best_score) < 0.05:
        return None

    line_index = -1
    aligned_ms = -1
    if recent_transcript:
        alignment = align_transcript_to_line(
            transcript=recent_transcript,
            lyrics_lines=best_track.lyrics_lines,
            current_line_index=-1,
            min_score=0.40,
        )
        if alignment is not None:
            line_index, aligned_ms, _score = alignment

    if line_index < 0:
        best_line_index = -1
        best_line_score = 0.0
        for index, line in enumerate(best_track.lyrics_lines):
            overlap_weight = sum(token_weights.get(token, 0.0) for token in line.token_set)
            if overlap_weight <= 0.0:
                continue
            line_score = overlap_weight / max(1.0, math.sqrt(float(len(line.token_set))))
            if line_score > best_line_score:
                best_line_score = line_score
                best_line_index = index

        if best_line_index >= 0:
            line_index = best_line_index
            aligned_ms = best_track.lyrics_lines[best_line_index].timestamp_ms

    return best_track, line_index, aligned_ms, best_score


def _start_async_track_lyrics_fetch(
    state: LyricsMonitorState,
    track_id: str,
    track_name: str,
    artist_name: str,
    profanity_pattern: re.Pattern[str],
    cache_dir: Path,
) -> None:
    with state.lyrics_fetch_lock:
        state.lyrics_fetch_request_id += 1
        request_id = state.lyrics_fetch_request_id
        state.lyrics_fetch_inflight_track_id = track_id
        state.lyrics_fetch_result = None

    print(
        f"[lyrics] loading synced lyrics track={track_name} - {artist_name}",
        flush=True,
    )

    def worker() -> None:
        try:
            data = get_track_lyrics_data(
                track_id=track_id,
                track_name=track_name,
                artist_name=artist_name,
                profanity_pattern=profanity_pattern,
                cache_dir=cache_dir,
            )
            result = PendingLyricsFetchResult(
                request_id=request_id,
                track_id=track_id,
                track_name=track_name,
                artist_name=artist_name,
                data=data,
            )
        except (requests.RequestException, RuntimeError, OSError, ValueError) as exc:
            result = PendingLyricsFetchResult(
                request_id=request_id,
                track_id=track_id,
                track_name=track_name,
                artist_name=artist_name,
                error=str(exc),
            )

        with state.lyrics_fetch_lock:
            if request_id != state.lyrics_fetch_request_id:
                return
            state.lyrics_fetch_result = result
            state.lyrics_fetch_inflight_track_id = ""

    worker_thread = threading.Thread(
        target=worker,
        name="lyrics-track-fetch",
        daemon=True,
    )
    worker_thread.start()


def _consume_pending_track_lyrics_fetch(
    state: LyricsMonitorState,
) -> PendingLyricsFetchResult | None:
    with state.lyrics_fetch_lock:
        result = state.lyrics_fetch_result
        state.lyrics_fetch_result = None
    return result


def _apply_pending_track_lyrics_fetch(state: LyricsMonitorState) -> None:
    result = _consume_pending_track_lyrics_fetch(state)
    if result is None:
        return

    if result.track_id != state.spotify_track_id:
        return

    if result.error:
        if result.error != state.last_error:
            print(f"[lyrics] warning: {result.error}", flush=True)
            state.last_error = result.error
        return

    if result.data is None:
        return

    state.current_track_id = result.track_id
    state.current_track_name = f"{result.track_name} - {result.artist_name}".strip(" -")
    state.profanity_timestamps_ms = result.data.profanity_timestamps_ms
    state.lyrics_lines = result.data.lyrics_lines
    state.triggered_timestamps_ms.clear()
    state.current_line_index = -1
    state.last_alignment_ms = -1
    state.no_match_chunks = 0
    _clear_transcript_estimation_state(state)
    state.last_error = ""

    print(
        f"[lyrics] track={state.current_track_name} profane-lines={len(state.profanity_timestamps_ms)}",
        flush=True,
    )


def _set_progress_anchor(state: LyricsMonitorState, progress_ms: int, now: float | None = None) -> None:
    if progress_ms < 0:
        state.progress_anchor_ms = -1
        state.progress_anchor_wall_time = 0.0
        return

    state.progress_anchor_ms = progress_ms
    state.progress_anchor_wall_time = time.time() if now is None else now


def _estimated_progress_ms(state: LyricsMonitorState, now: float | None = None) -> int:
    progress_ms = state.current_progress_ms
    if state.progress_anchor_ms < 0 or state.progress_anchor_wall_time <= 0.0:
        return progress_ms

    ref_now = time.time() if now is None else now
    elapsed_ms = max(0, int((ref_now - state.progress_anchor_wall_time) * 1000.0))
    projected_ms = state.progress_anchor_ms + elapsed_ms
    if progress_ms < 0:
        return projected_ms

    return max(progress_ms, projected_ms)


def maybe_duck_from_lyrics(
    controller: SpotifyVolumeController,
    state: LyricsMonitorState,
    spotify_token: str,
    profanity_pattern: re.Pattern[str],
    cache_dir: Path,
    duck_percent: float,
    hold_seconds: float,
    preduck_ms: int,
    poll_seconds: float,
    transcript: str,
) -> None:
    transcript = transcript.strip().lower()

    if spotify_token:
        _apply_pending_track_lyrics_fetch(state)

    now = time.time()
    if spotify_token and (now - state.last_poll_time >= poll_seconds):
        state.last_poll_time = now
        current = get_current_spotify_track(spotify_token)
        if current and current.get("is_playing"):
            track_id = str(current["track_id"])
            track_name = str(current["track_name"])
            artist_name = str(current["artist_name"])
            state.current_progress_ms = int(current["progress_ms"])
            _set_progress_anchor(state, state.current_progress_ms, now)

            if state.spotify_track_id != track_id:
                state.spotify_track_id = track_id
                state.current_track_id = track_id
                state.current_track_name = f"{track_name} - {artist_name}"
                state.profanity_timestamps_ms = []
                state.lyrics_lines = []
                state.triggered_timestamps_ms.clear()
                state.current_line_index = -1
                state.last_alignment_ms = -1
                state.no_match_chunks = 0
                _clear_transcript_estimation_state(state)
                _set_progress_anchor(state, state.current_progress_ms, now)
                print(
                    "[lyrics] now-playing "
                    f"track={state.current_track_name} "
                    f"at={state.current_progress_ms / 1000.0:.2f}s",
                    flush=True,
                )

                cached = load_cached_track_lyrics(cache_dir, track_id, profanity_pattern)
                if cached is not None:
                    state.profanity_timestamps_ms = cached.profanity_timestamps_ms
                    state.lyrics_lines = cached.lyrics_lines
                    print(
                        "[lyrics] cache-hit "
                        f"track={state.current_track_name} "
                        f"profane-lines={len(state.profanity_timestamps_ms)}",
                        flush=True,
                    )

                _start_async_track_lyrics_fetch(
                    state=state,
                    track_id=track_id,
                    track_name=track_name,
                    artist_name=artist_name,
                    profanity_pattern=profanity_pattern,
                    cache_dir=cache_dir,
                )

    if not state.current_track_id:
        if spotify_token:
            return

        if not state.lyrics_library:
            state.lyrics_library = load_cached_lyrics_library(
                cache_dir,
                profanity_pattern,
                state.ordered_track_ids,
            )

        if transcript:
            _accumulate_transcript_tokens(state, transcript)

        search_text = " ".join(state.transcript_history[-3:])
        identified = identify_track_from_accumulated_tokens(
            transcript_token_counts=state.transcript_token_counts,
            recent_transcript=search_text,
            library=state.lyrics_library,
        )
        if identified is not None:
            track, line_index, aligned_ms, score = identified
            token_count = len(state.transcript_token_counts)
            state.current_track_id = track.track_id
            state.current_track_name = f"{track.track_name} - {track.artist_name}".strip(" -")
            state.profanity_timestamps_ms = track.profanity_timestamps_ms
            state.lyrics_lines = track.lyrics_lines
            state.triggered_timestamps_ms.clear()
            state.current_line_index = line_index
            state.current_progress_ms = aligned_ms
            state.last_alignment_ms = aligned_ms
            _set_progress_anchor(state, aligned_ms, now)
            state.no_match_chunks = 0
            _clear_transcript_estimation_state(state)
            print(
                f"[lyrics] identified track={state.current_track_name} at~{aligned_ms / 1000.0:.2f}s "
                f"score={score:.2f} tokens={token_count}",
                flush=True,
            )
        else:
            return

    alignment = None
    if transcript:
        alignment = align_transcript_to_line(
            transcript=transcript,
            lyrics_lines=state.lyrics_lines,
            current_line_index=state.current_line_index,
            min_score=0.50,
        )
    if alignment is not None:
        line_index, aligned_ms, _score = alignment
        state.current_line_index = line_index
        state.last_alignment_ms = aligned_ms
        state.current_progress_ms = aligned_ms
        _set_progress_anchor(state, aligned_ms, now)
        state.no_match_chunks = 0
    else:
        state.no_match_chunks += 1
        if not spotify_token and state.no_match_chunks >= 12:
            # Force re-identification when we repeatedly fail matching in tokenless mode.
            state.current_track_id = ""
            state.current_track_name = ""
            state.profanity_timestamps_ms = []
            state.lyrics_lines = []
            state.current_line_index = -1
            state.current_progress_ms = -1
            state.last_alignment_ms = -1
            _set_progress_anchor(state, -1, now)
            state.triggered_timestamps_ms.clear()
            _clear_transcript_estimation_state(state)
            state.no_match_chunks = 0
            return

    progress_ms = _estimated_progress_ms(state, now)
    if state.last_alignment_ms >= 0 and progress_ms < (state.last_alignment_ms - 4000):
        progress_ms = state.last_alignment_ms

    if progress_ms < 0:
        return

    # Add a small lead buffer to account for chunking/transcription latency.
    lead_padding_ms = 450 if spotify_token else 700
    past_due_window_ms = 900 if spotify_token else 700
    horizon_ms = progress_ms + preduck_ms + lead_padding_ms
    for timestamp_ms in state.profanity_timestamps_ms:
        if timestamp_ms in state.triggered_timestamps_ms:
            continue
        if (progress_ms - past_due_window_ms) <= timestamp_ms <= horizon_ms:
            controller.duck(duck_percent, hold_seconds)
            state.triggered_timestamps_ms.add(timestamp_ms)
            print(
                "[ducked-lyrics] "
                f"track={state.current_track_name} "
                f"at={timestamp_ms / 1000.0:.2f}s "
                f"progress={progress_ms / 1000.0:.2f}s "
                f"line-index={state.current_line_index}",
                flush=True,
            )
            break


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Duck Music volume when profanity is detected in currently playing audio."
    )
    parser.add_argument("--sample-rate", type=int, default=16000, help="Audio sample rate.")
    parser.add_argument("--chunk-seconds", type=float, default=2.0, help="Chunk length in seconds.")
    parser.add_argument(
        "--duck-percent",
        type=float,
        default=45.0,
        help="Percent volume reduction when profanity is detected (0-100).",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=2.5,
        help="Seconds to keep lowered volume after last detected profanity.",
    )
    parser.add_argument(
        "--min-rms",
        type=float,
        default=0.002,
        help="Skip transcription for very quiet chunks below this RMS level.",
    )
    parser.add_argument(
        "--model-size",
        type=str,
        default="medium",
        choices=["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3", "large-v3-turbo"],
        help="faster-whisper model size.",
    )
    parser.add_argument("--language", type=str, default="en", help="Whisper language code.")
    parser.add_argument(
        "--profanity-file",
        type=Path,
        default=None,
        help="Optional text file (one word per line) to extend the profanity list.",
    )
    parser.add_argument(
        "--input-source",
        type=str,
        default="loopback",
        choices=["loopback", "microphone"],
        help="Audio capture source: speaker loopback or physical microphone.",
    )
    parser.add_argument(
        "--input-device",
        type=str,
        default="",
        help="Optional partial device name for capture source selection.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print available speakers/microphones and exit.",
    )
    parser.add_argument(
        "--log-transcript",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Print recognized transcript for each non-empty chunk (use --no-log-transcript to disable).",
    )
    parser.add_argument(
        "--lyrics-mode",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use synced lyrics plus Spotify playback progress to pre-duck before profane lines.",
    )
    parser.add_argument(
        "--spotify-token",
        type=str,
        default="",
        help="Spotify Web API access token. If omitted, uses SPOTIFY_ACCESS_TOKEN env var.",
    )
    parser.add_argument(
        "--hf-token",
        type=str,
        default="",
        help="Hugging Face token for authenticated model downloads. If omitted, uses HF_TOKEN env var.",
    )
    parser.add_argument(
        "--playlist-id",
        type=str,
        default="",
        help="Optional playlist ID for lyrics prefetch.",
    )
    parser.add_argument(
        "--prefetch-playlist-lyrics",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Pre-cache lyrics for all tracks in --playlist-id.",
    )
    parser.add_argument(
        "--lyrics-preduck-seconds",
        type=float,
        default=0.8,
        help="How early to duck before a profane lyric timestamp.",
    )
    parser.add_argument(
        "--lyrics-poll-seconds",
        type=float,
        default=1.0,
        help="Polling interval for Spotify currently-playing endpoint when lyrics mode is enabled.",
    )
    parser.add_argument(
        "--lyrics-cache-dir",
        type=Path,
        default=Path(".lyrics_cache"),
        help="Directory to cache fetched lyrics metadata.",
    )
    parser.add_argument(
        "--import-playlist-csv",
        type=Path,
        action="append",
        default=[],
        help="Path to TuneMyMusic/Exportify playlist CSV. Repeat flag to import multiple files.",
    )
    parser.add_argument(
        "--prefetch-csv-lyrics",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Fetch and cache lyrics from tracks listed in --import-playlist-csv files.",
    )
    parser.add_argument(
        "--prefetch-only",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Exit after prefetch jobs (CSV/API) instead of starting live audio monitor.",
    )

    parsed = parser.parse_args()
    argv = sys.argv[1:]
    parsed.model_size_user_set = _argument_explicitly_set(argv, "--model-size")
    parsed.chunk_seconds_user_set = _argument_explicitly_set(argv, "--chunk-seconds")
    parsed.lyrics_preduck_seconds_user_set = _argument_explicitly_set(argv, "--lyrics-preduck-seconds")
    parsed.lyrics_poll_seconds_user_set = _argument_explicitly_set(argv, "--lyrics-poll-seconds")
    return parsed


def _argument_explicitly_set(argv: list[str], option_name: str) -> bool:
    if option_name in argv:
        return True

    option_prefix = f"{option_name}="
    return any(argument.startswith(option_prefix) for argument in argv)


def _normalize_name(value: str) -> str:
    return value.strip().lower()


def _find_by_name(items: list[Any], target_name: str) -> Any | None:
    target = _normalize_name(target_name)
    if not target:
        return None

    for item in items:
        item_name = _normalize_name(getattr(item, "name", ""))
        if target in item_name:
            return item

    return None


def list_capture_devices() -> None:
    print("Available speakers:", flush=True)
    for speaker in sc.all_speakers():
        print(f"  - {speaker.name}", flush=True)

    print("Available microphones:", flush=True)
    for mic in sc.all_microphones(include_loopback=False):
        print(f"  - {mic.name}", flush=True)

    print("Available loopback microphones:", flush=True)
    for mic in sc.all_microphones(include_loopback=True):
        if getattr(mic, "isloopback", False):
            print(f"  - {mic.name}", flush=True)


def find_microphone_input(preferred_name: str = ""):
    microphones = list(sc.all_microphones(include_loopback=False))
    selected = _find_by_name(microphones, preferred_name)
    if selected is not None:
        return selected

    default_mic = sc.default_microphone()
    if default_mic is not None:
        return default_mic

    raise RuntimeError("No microphone input found. Use --list-devices to inspect available devices.")


def _loopback_from_speaker(speaker: Any):
    if hasattr(speaker, "microphone"):
        try:
            return speaker.microphone(include_loopback=True)
        except (AttributeError, OSError, RuntimeError):
            return None

    speaker_id = getattr(speaker, "id", None)
    if speaker_id is None:
        return None

    try:
        return sc.get_microphone(speaker_id, include_loopback=True)
    except (OSError, RuntimeError):
        return None


def find_loopback_mic(preferred_name: str = ""):
    speakers = list(sc.all_speakers())
    selected_speaker = _find_by_name(speakers, preferred_name)
    if selected_speaker is None:
        selected_speaker = sc.default_speaker()

    if selected_speaker is None:
        raise RuntimeError("No speaker output found on this machine.")

    loopback = _loopback_from_speaker(selected_speaker)

    if loopback is None:
        loopback_mics = [mic for mic in sc.all_microphones(include_loopback=True) if getattr(mic, "isloopback", False)]
        loopback = _find_by_name(loopback_mics, preferred_name)
        if loopback is None:
            speaker_name = _normalize_name(getattr(selected_speaker, "name", ""))
            for mic in loopback_mics:
                mic_name = _normalize_name(getattr(mic, "name", ""))
                if speaker_name and speaker_name in mic_name:
                    loopback = mic
                    break

        if loopback is None and loopback_mics:
            loopback = loopback_mics[0]

    if loopback is None:
        raise RuntimeError(
            "No loopback capture device found. Use --list-devices and optionally set --input-device."
        )

    return loopback


def main() -> int:
    args = parse_args()

    hf_token = args.hf_token.strip() or os.environ.get("HF_TOKEN", "").strip()
    if hf_token:
        os.environ["HF_TOKEN"] = hf_token
        os.environ["HUGGINGFACE_HUB_TOKEN"] = hf_token

    configure_huggingface_runtime()

    if args.input_source == "loopback":
        if not bool(getattr(args, "chunk_seconds_user_set", False)) and args.chunk_seconds > LOOPBACK_CHUNK_SECONDS_DEFAULT:
            args.chunk_seconds = LOOPBACK_CHUNK_SECONDS_DEFAULT
            print(
                "[transcribe] loopback low-latency mode: using shorter audio chunks for faster lyric matching.",
                flush=True,
            )

        if not bool(getattr(args, "model_size_user_set", False)) and args.model_size not in {
            "tiny",
            "base",
            "small",
        }:
            args.model_size = FAST_START_BOOTSTRAP_MODEL
            print(
                "[model] loopback low-latency mode: using small model by default (override with --model-size).",
                flush=True,
            )

        if (
            not bool(getattr(args, "lyrics_preduck_seconds_user_set", False))
            and args.lyrics_preduck_seconds < LOOPBACK_LYRICS_PREDUCK_SECONDS_DEFAULT
        ):
            args.lyrics_preduck_seconds = LOOPBACK_LYRICS_PREDUCK_SECONDS_DEFAULT

        if (
            not bool(getattr(args, "lyrics_poll_seconds_user_set", False))
            and args.lyrics_poll_seconds > LOOPBACK_LYRICS_POLL_SECONDS_DEFAULT
        ):
            args.lyrics_poll_seconds = LOOPBACK_LYRICS_POLL_SECONDS_DEFAULT

    if args.list_devices:
        list_capture_devices()
        return 0

    csv_paths = [path for path in args.import_playlist_csv if str(path).strip()]
    if args.prefetch_csv_lyrics and not csv_paths:
        print(
            "[lyrics] warning: --prefetch-csv-lyrics ignored because no --import-playlist-csv files were provided.",
            flush=True,
        )
    csv_prefetch_enabled = bool(csv_paths)

    words = set(DEFAULT_BAD_WORDS)
    words.update(load_custom_words(args.profanity_file))
    words = expand_profanity_words(words)
    if not words:
        raise RuntimeError("No profanity words configured.")

    profanity_pattern = build_word_regex(words)
    controller = SpotifyVolumeController()
    spotify_token = args.spotify_token.strip() or os.environ.get("SPOTIFY_ACCESS_TOKEN", "").strip()
    lyrics_enabled = bool(args.lyrics_mode)
    lyrics_state = LyricsMonitorState()

    if csv_paths:
        lyrics_state.ordered_track_ids = ordered_track_keys_from_csv_paths(csv_paths)

    if lyrics_enabled:
        if spotify_token:
            print(
                "[lyrics] enabled: pre-ducking from Spotify playback + synced lyrics is active.",
                flush=True,
            )
        elif csv_prefetch_enabled:
            print(
                "[lyrics] tokenless mode: using playlist CSV imports + LRCLib cache.",
                flush=True,
            )
        else:
            print(
                "[lyrics] enabled but Spotify token is missing. "
                "Set SPOTIFY_ACCESS_TOKEN or pass --spotify-token.",
                flush=True,
            )

    if csv_prefetch_enabled:
        if args.prefetch_only:
            run_csv_prefetch_job(
                csv_paths=csv_paths,
                profanity_pattern=profanity_pattern,
                cache_dir=args.lyrics_cache_dir,
            )
        else:
            start_csv_prefetch_in_background(
                csv_paths=csv_paths,
                profanity_pattern=profanity_pattern,
                cache_dir=args.lyrics_cache_dir,
            )
            print(
                "[lyrics] csv-prefetch started in background; monitoring continues immediately.",
                flush=True,
            )

    if args.prefetch_playlist_lyrics:
        playlist_id = args.playlist_id.strip()
        if not spotify_token:
            raise RuntimeError("Spotify token is required for playlist lyrics prefetch.")
        if not playlist_id:
            raise RuntimeError("--playlist-id is required when --prefetch-playlist-lyrics is enabled.")

        total_tracks, profane_tracks = prefetch_playlist_lyrics(
            token=spotify_token,
            playlist_id=playlist_id,
            profanity_pattern=profanity_pattern,
            cache_dir=args.lyrics_cache_dir,
        )
        print(
            f"[lyrics] prefetched playlist tracks={total_tracks} tracks_with_profanity={profane_tracks}",
            flush=True,
        )

    if args.prefetch_only:
        return 0

    if lyrics_enabled and not spotify_token:
        lyrics_state.lyrics_library = load_cached_lyrics_library(
            args.lyrics_cache_dir,
            profanity_pattern,
            lyrics_state.ordered_track_ids,
        )
        if lyrics_state.lyrics_library:
            print(
                "[lyrics] Spotify API unavailable; using transcript-to-lyrics alignment from local cache.",
                flush=True,
            )
        elif csv_prefetch_enabled:
            print(
                "[lyrics] waiting for background CSV lyrics cache; transcript ducking stays active meanwhile.",
                flush=True,
            )
        else:
            print(
                "[lyrics] Spotify token missing and no cached synced lyrics found. "
                "Run with --prefetch-csv-lyrics + --import-playlist-csv first.",
                flush=True,
            )
            lyrics_enabled = False

    preduck_ms = max(0, int(args.lyrics_preduck_seconds * 1000))

    configure_transcription_mode(args.input_source)

    requested_model_size = args.model_size
    model_lock = threading.Lock()
    model_state: dict[str, Any] = {
        "active": None,
        "active_name": "",
        "loading_target": "",
    }

    print(f"Loading Whisper model: {requested_model_size}", flush=True)
    if requested_model_size in LARGE_WHISPER_MODELS:
        print(
            "[model] first run may download model weights and take several minutes.",
            flush=True,
        )

        bootstrap_model_size = FAST_START_BOOTSTRAP_MODEL
        if requested_model_size == FAST_START_BOOTSTRAP_MODEL:
            bootstrap_model_size = requested_model_size

        if bootstrap_model_size != requested_model_size:
            print(
                "[model] fast-start: booting with "
                f"{bootstrap_model_size}; {requested_model_size} will load in background.",
                flush=True,
            )

        active_model = load_whisper_model(bootstrap_model_size)
        with model_lock:
            model_state["active"] = active_model
            model_state["active_name"] = bootstrap_model_size
            model_state["loading_target"] = (
                requested_model_size if bootstrap_model_size != requested_model_size else ""
            )

        if bootstrap_model_size != requested_model_size:
            def upgrade_model_worker() -> None:
                try:
                    upgraded_model = load_whisper_model(requested_model_size)
                except Exception as exc:
                    print(
                        f"[model] warning: background load failed for {requested_model_size}: {exc}",
                        flush=True,
                    )
                    with model_lock:
                        model_state["loading_target"] = ""
                    return

                with model_lock:
                    model_state["active"] = upgraded_model
                    model_state["active_name"] = requested_model_size
                    model_state["loading_target"] = ""

                print(f"[model] switched to {requested_model_size}", flush=True)

            threading.Thread(
                target=upgrade_model_worker,
                name="whisper-model-upgrade",
                daemon=True,
            ).start()
    else:
        active_model = load_whisper_model(requested_model_size)
        with model_lock:
            model_state["active"] = active_model
            model_state["active_name"] = requested_model_size
            model_state["loading_target"] = ""

    input_name = args.input_device.strip()
    if args.input_source == "microphone":
        capture_device = find_microphone_input(input_name)
        print(f"Using microphone input: {capture_device.name}", flush=True)
    else:
        capture_device = find_loopback_mic(input_name)
        print(f"Using loopback input: {capture_device.name}", flush=True)

    running = True

    def handle_shutdown(_: int, __):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    print(
        "Running profanity monitor. Press Ctrl+C to stop. "
        f"Duck: {args.duck_percent:.0f}% | Hold: {args.hold_seconds:.1f}s",
        flush=True,
    )

    chunk_frames = int(args.sample_rate * args.chunk_seconds)
    if chunk_frames <= 0:
        raise ValueError("chunk-seconds and sample-rate produce invalid frame count.")

    low_rms_counter = 0
    empty_transcript_counter = 0

    try:
        while running:
            try:
                captured = capture_device.record(
                    numframes=chunk_frames,
                    samplerate=args.sample_rate,
                    channels=1,
                )
            except OSError as exc:
                if getattr(exc, "errno", None) == 22:
                    print(
                        "[input] capture device became unavailable (Errno 22). Stopping monitor.",
                        flush=True,
                    )
                    break
                raise

            chunk = captured.reshape(-1).astype(np.float32)

            controller.restore_if_due()

            current_rms = rms_level(chunk)
            if current_rms < args.min_rms:
                low_rms_counter += 1
                if low_rms_counter % 25 == 0:
                    print(
                        "[input] very low audio level "
                        f"(rms={current_rms:.5f}, min-rms={args.min_rms:.5f}). "
                        "Try --input-device or lower --min-rms.",
                        flush=True,
                    )
                continue

            low_rms_counter = 0

            with model_lock:
                current_model = model_state.get("active")

            if current_model is None:
                print("[model] warning: transcription model is unavailable.", flush=True)
                continue

            try:
                transcript = transcribe_chunk(current_model, chunk, args.language).lower()
            except OSError as exc:
                print(f"[transcribe] warning: {exc}", flush=True)
                continue

            if transcript and args.log_transcript:
                print(f"[heard] {transcript}", flush=True)

            if transcript:
                empty_transcript_counter = 0
            else:
                empty_transcript_counter += 1
                if empty_transcript_counter % 20 == 0:
                    print(
                        "[transcribe] no words detected yet; monitor is active. "
                        "Try lowering --min-rms or setting --input-device.",
                        flush=True,
                    )

            if lyrics_enabled:
                try:
                    maybe_duck_from_lyrics(
                        controller=controller,
                        state=lyrics_state,
                        spotify_token=spotify_token,
                        profanity_pattern=profanity_pattern,
                        cache_dir=args.lyrics_cache_dir,
                        duck_percent=args.duck_percent,
                        hold_seconds=args.hold_seconds,
                        preduck_ms=preduck_ms,
                        poll_seconds=max(0.2, args.lyrics_poll_seconds),
                        transcript=transcript,
                    )
                except (requests.RequestException, RuntimeError, OSError) as exc:
                    message = str(exc)
                    if message != lyrics_state.last_error:
                        print(f"[lyrics] warning: {message}", flush=True)
                        lyrics_state.last_error = message

            if not transcript:
                continue

            matches = sorted(set(match.group(0).lower() for match in profanity_pattern.finditer(transcript)))
            if not matches:
                continue

            controller.duck(args.duck_percent, args.hold_seconds)
            print(f"[ducked] detected={matches} text={transcript}", flush=True)

    finally:
        try:
            controller.restore_now()
        except (RuntimeError, OSError):
            pass

    print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(0)