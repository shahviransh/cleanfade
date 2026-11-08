#!/usr/bin/env python3
"""Build the CleanFade Python engine sidecar for the current host triple."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "requirements.txt"
PYTHON_SCRIPT = REPO_ROOT / "spotify_duck.py"
BIN_DIR = REPO_ROOT / "src-tauri" / "bin"
BUILD_ROOT = REPO_ROOT / ".pybuild"
WORK_PATH = BUILD_ROOT / "work"
SPEC_PATH = BUILD_ROOT / "spec"


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=REPO_ROOT, env=env, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def detect_target_triple() -> str:
    for key in ("TAURI_ENV_TARGET_TRIPLE", "CLEANFADE_TARGET_TRIPLE"):
        value = os.environ.get(key, "").strip()
        if value:
            return re.sub(r"[^A-Za-z0-9._-]", "", value)

    try:
        rustc_info = subprocess.check_output(["rustc", "-vV"], text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.CalledProcessError):
        rustc_info = ""

    match = re.search(r"(?m)^\s*host:\s*([A-Za-z0-9._-]+)\s*$", rustc_info)
    if match:
        return match.group(1)

    system = sys.platform
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
    if system == "win32":
        return "x86_64-pc-windows-msvc"
    if system == "darwin":
        return "aarch64-apple-darwin" if machine in {"arm64", "aarch64"} else "x86_64-apple-darwin"
    return "x86_64-unknown-linux-gnu"


def resolve_python_launcher() -> tuple[list[str], str]:
    """Return (command_prefix, label) used to invoke Python for packaging."""
    forced = os.environ.get("CLEANFADE_PYTHON", "").strip()
    if forced:
        return [forced], forced

    conda = shutil.which("conda")
    if conda:
        conda_env = os.environ.get("CLEANFADE_CONDA_ENV", "").strip() or "cleanfade"
        try:
            env_list = json.loads(
                subprocess.check_output([conda, "env", "list", "--json"], text=True, encoding="utf-8", errors="replace")
            )
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Failed to inspect Conda environments: {exc}") from exc

        env_exists = any(Path(path).name == conda_env for path in env_list.get("envs", []))
        if not env_exists:
            run([conda, "create", "-y", "-n", conda_env, "python=3.12"])

        version_raw = subprocess.check_output(
            [conda, "run", "-n", conda_env, "python", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
        major, minor = (int(part) for part in version_raw.split(".", 1))
        if (major, minor) >= (3, 13):
            raise SystemExit(
                f"Conda environment '{conda_env}' is using Python {version_raw}. "
                "Use Python 3.12 for sidecar packaging."
            )

        return [conda, "run", "-n", conda_env, "python"], f"conda:{conda_env}"

    version = sys.version_info
    if version >= (3, 13):
        raise SystemExit(
            f"Current Python is {version.major}.{version.minor}. "
            "Use Python 3.12 for sidecar packaging, or set CLEANFADE_PYTHON."
        )
    if version < (3, 10):
        raise SystemExit("Python 3.10+ is required for sidecar packaging.")

    return [sys.executable], sys.executable


def main() -> int:
    if not REQUIREMENTS.exists():
        raise SystemExit(f"Missing requirements file: {REQUIREMENTS}")
    if not PYTHON_SCRIPT.exists():
        raise SystemExit(f"Missing engine script: {PYTHON_SCRIPT}")

    python_cmd, python_label = resolve_python_launcher()
    print(f"Using Python launcher: {python_label}", flush=True)

    run([*python_cmd, "-m", "pip", "install", "--upgrade", "pip"])
    run([*python_cmd, "-m", "pip", "install", "-r", str(REQUIREMENTS), "pyinstaller"])

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    WORK_PATH.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.mkdir(parents=True, exist_ok=True)

    pyinstaller_args = [
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "cleanfade-engine",
        "--distpath",
        str(BIN_DIR),
        "--workpath",
        str(WORK_PATH),
        "--specpath",
        str(SPEC_PATH),
        "--collect-data",
        "faster_whisper",
        "--collect-submodules",
        "faster_whisper",
        str(PYTHON_SCRIPT),
    ]
    if sys.platform == "win32":
        pyinstaller_args.insert(3, "--noconsole")

    run([*python_cmd, "-m", "PyInstaller", *pyinstaller_args])

    built_binary = BIN_DIR / ("cleanfade-engine.exe" if sys.platform == "win32" else "cleanfade-engine")
    if not built_binary.exists():
        alt = BIN_DIR / "cleanfade-engine"
        if alt.exists():
            built_binary = alt
        else:
            raise SystemExit(f"Expected sidecar binary was not created at {BIN_DIR}")

    if sys.platform != "win32":
        built_binary.chmod(built_binary.stat().st_mode | 0o111)

    target_triple = detect_target_triple() or "unknown"
    target_binary = BIN_DIR / f"{built_binary.stem}-{target_triple}{built_binary.suffix}"
    shutil.copy2(built_binary, target_binary)
    if sys.platform != "win32":
        target_binary.chmod(target_binary.stat().st_mode | 0o111)

    print(f"Built sidecar: {built_binary}")
    print(f"Copied sidecar for Tauri bundle: {target_binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
