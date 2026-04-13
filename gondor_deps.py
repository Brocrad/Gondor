#!/usr/bin/env python3
"""
Central dependency maintenance for Gondor (pip / requirements.txt).
Used by optional auto-update on bot startup and by update_ytdlp.py.
"""

import os
import subprocess
import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parent


def requirements_path() -> Path:
    return project_root() / "requirements.txt"


def upgrade_from_requirements(timeout_sec: int = 600) -> bool:
    """pip install --upgrade -r requirements.txt using the current interpreter."""
    req = requirements_path()
    if not req.is_file():
        print(f"⚠️ gondor_deps: missing {req}")
        return False
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "-r",
        str(req),
    ]
    try:
        print(f"📦 pip: {' '.join(cmd)}")
        r = subprocess.run(cmd, timeout=timeout_sec)
        if r.returncode != 0:
            print(f"⚠️ pip exited with code {r.returncode}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"⚠️ pip timed out after {timeout_sec}s")
        return False
    except OSError as e:
        print(f"⚠️ pip failed: {e}")
        return False


def upgrade_ytdlp_only(timeout_sec: int = 300) -> bool:
    """Backward-compatible: only yt-dlp (same as legacy update_ytdlp.py)."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
    try:
        print(f"📦 pip: {' '.join(cmd)}")
        subprocess.run(cmd, check=True, timeout=timeout_sec)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"⚠️ yt-dlp upgrade failed: {e}")
        return False


def maybe_auto_update_dependencies() -> None:
    """
    If GONDOR_AUTO_UPDATE_DEPS is 1/true/on, run upgrade_from_requirements().
    Set GONDOR_AUTO_UPDATE_YTDLP_ONLY=1 to only upgrade yt-dlp (lighter).
    """
    if os.environ.get("GONDOR_SKIP_DEP_UPDATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return
    flag = os.environ.get("GONDOR_AUTO_UPDATE_DEPS", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return
    only_ytdlp = os.environ.get("GONDOR_AUTO_UPDATE_YTDLP_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if only_ytdlp:
        print("📦 GONDOR_AUTO_UPDATE_DEPS: upgrading yt-dlp only")
        upgrade_ytdlp_only()
    else:
        print("📦 GONDOR_AUTO_UPDATE_DEPS: upgrading from requirements.txt")
        upgrade_from_requirements()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Gondor dependency maintenance")
    p.add_argument(
        "mode",
        nargs="?",
        default="requirements",
        choices=("requirements", "ytdlp"),
        help="requirements = full requirements.txt; ytdlp = yt-dlp only",
    )
    args = p.parse_args()
    ok = (
        upgrade_ytdlp_only()
        if args.mode == "ytdlp"
        else upgrade_from_requirements()
    )
    sys.exit(0 if ok else 1)
