import argparse
import logging
import math
import os
import re
import signal
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

# Keep COM in MTA mode so pycaw/comtypes matches thread state set by other audio libs.
sys.coinit_flags = 0

import numpy as np
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
    pattern = r"\\b(?:" + "|".join(escaped) + r")\\b"
    return re.compile(pattern, re.IGNORECASE)


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


class SpotifyVolumeController:
    def __init__(self) -> None:
        try:
            from pycaw.pycaw import AudioUtilities
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
    segments, _ = model.transcribe(
        chunk,
        language=language,
        beam_size=1,
        best_of=1,
        vad_filter=True,
        condition_on_previous_text=False,
        without_timestamps=True,
    )
    return " ".join(segment.text.strip() for segment in segments if segment.text).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Duck Spotify volume when profanity is detected in currently playing audio."
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
        default=0.01,
        help="Skip transcription for very quiet chunks below this RMS level.",
    )
    parser.add_argument(
        "--model-size",
        type=str,
        default="base.en",
        choices=["tiny", "tiny.en", "base", "base.en", "small", "small.en"],
        help="faster-whisper model size.",
    )
    parser.add_argument("--language", type=str, default="en", help="Whisper language code.")
    parser.add_argument(
        "--profanity-file",
        type=Path,
        default=None,
        help="Optional text file (one word per line) to extend the profanity list.",
    )
    return parser.parse_args()


def find_loopback_mic():
    default_speaker = sc.default_speaker()
    if default_speaker is None:
        raise RuntimeError("No default speaker found on this machine.")

    loopback = default_speaker.microphone(include_loopback=True)
    if loopback is None:
        raise RuntimeError("No loopback capture device found. Verify your Windows audio setup.")

    return loopback


def main() -> int:
    args = parse_args()
    configure_huggingface_runtime()

    words = set(DEFAULT_BAD_WORDS)
    words.update(load_custom_words(args.profanity_file))
    if not words:
        raise RuntimeError("No profanity words configured.")

    profanity_pattern = build_word_regex(words)
    controller = SpotifyVolumeController()

    print(f"Loading Whisper model: {args.model_size}", flush=True)
    model = WhisperModel(args.model_size, device="cpu", compute_type="int8")

    print("Using default speaker loopback as input.", flush=True)
    loopback_mic = find_loopback_mic()

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

    try:
        while running:
            captured = loopback_mic.record(
                numframes=chunk_frames,
                samplerate=args.sample_rate,
                channels=1,
            )

            chunk = captured.reshape(-1).astype(np.float32)

            controller.restore_if_due()

            if rms_level(chunk) < args.min_rms:
                continue

            transcript = transcribe_chunk(model, chunk, args.language).lower()
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