#!/usr/bin/env python3
"""
Update yt-dlp (delegates to gondor_deps). For full project deps use:
  python gondor_deps.py
or set GONDOR_AUTO_UPDATE_DEPS=1 to upgrade from requirements.txt on bot start.
"""

import subprocess
import sys

from gondor_deps import upgrade_ytdlp_only


def update_ytdlp():
    """Update yt-dlp to the latest version"""
    print("🔄 Updating yt-dlp...")
    ok = upgrade_ytdlp_only()
    if not ok:
        return False
    try:
        version_result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"🎯 yt-dlp version: {version_result.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Could not read version: {e}")
    return True


if __name__ == "__main__":
    print("🚀 Gondor yt-dlp Update Script")
    print("=" * 40)
    print("💡 For all dependencies: pip install -U -r requirements.txt")
    print("   or: python -c \"from gondor_deps import upgrade_from_requirements; upgrade_from_requirements()\"")
    print("=" * 40)
    if update_ytdlp():
        print("\n✅ Update completed! Restart the bot if it is running.")
    else:
        print("\n❌ Update failed. Try: pip install --upgrade yt-dlp")
