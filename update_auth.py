#!/usr/bin/env python3
"""
Simple cookie updater for YouTube Music MCP Server.
Just paste your raw cookie string into cookie.txt and run this script.

NOTE: The MCP server now auto-detects cookie.txt on startup, so running
this script manually is optional.  It's still useful for:
  - Validating cookies before starting the server
  - Regenerating browser.json on demand
"""

import hashlib
import json
import os
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
COOKIE_PATH = SCRIPT_DIR / "cookie.txt"
BROWSER_PATH = SCRIPT_DIR / "browser.json"


def _extract_sapisid(cookie_string: str) -> str:
    """Extract SAPISID value from a raw cookie header string."""
    for part in cookie_string.split(";"):
        part = part.strip()
        if part.startswith("SAPISID="):
            return part.split("=", 1)[1]
    raise ValueError(
        "'SAPISID' not found in cookie string. "
        "Make sure you copied the complete 'cookie:' header value."
    )


def _generate_sapisidhash(sapisid: str) -> str:
    """Generate a SAPISIDHASH authorization value."""
    timestamp = int(time.time())
    hash_input = f"{timestamp} {sapisid} https://music.youtube.com"
    sha1 = hashlib.sha1(hash_input.encode()).hexdigest()
    return f"SAPISIDHASH {timestamp}_{sha1}"


def _cookie_sanity_check(cookie_string: str) -> None:
    """Verify the cookie string contains the minimum required cookies."""
    for key in ("SAPISID=", "SID="):
        if key not in cookie_string:
            raise ValueError(
                f"'{key.rstrip('=')}' not found in cookie string. "
                "Make sure you copied the complete 'cookie:' header value."
            )


def update_auth() -> bool:
    """Read cookie.txt → generate browser.json → test auth."""

    # ── 1. Read cookie.txt ──────────────────────────────
    if not COOKIE_PATH.exists():
        print("❌ cookie.txt not found!\n")
        print("To update authentication:")
        print("  1. Go to music.youtube.com (logged in)")
        print("  2. Press F12 → Network tab → Refresh page")
        print("  3. Click any request → Headers → copy the 'cookie:' value")
        print(f"  4. Paste it into: {COOKIE_PATH}")
        print("  5. Run this script again\n")
        return False

    cookie_string = COOKIE_PATH.read_text().strip()

    if not cookie_string:
        print("❌ cookie.txt is empty!")
        return False

    # ── 2. Validate ─────────────────────────────────────
    try:
        _cookie_sanity_check(cookie_string)
    except ValueError as e:
        print(f"❌ {e}")
        return False

    # ── 3. Write browser.json ───────────────────────────
    # ytmusicapi requires the SAPISIDHASH authorization header
    # to classify the file as browser auth (not OAuth).
    sapisid = _extract_sapisid(cookie_string)
    authorization = _generate_sapisidhash(sapisid)

    browser_data = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "X-Goog-AuthUser": os.environ.get("YTMUSIC_AUTHUSER", "0"),  # account slot when several are signed in
        "x-origin": "https://music.youtube.com",
        "Cookie": cookie_string,
        "authorization": authorization,
    }

    BROWSER_PATH.write_text(json.dumps(browser_data, indent=2))
    BROWSER_PATH.chmod(0o600)
    print(f"✅ Written {BROWSER_PATH}")

    # ── 4. Quick auth test ──────────────────────────────
    print("\nTesting authentication...")
    try:
        from ytmusicapi import YTMusic

        yt = YTMusic(str(BROWSER_PATH))
        yt.get_library_songs(limit=1)
        print("✅ Authentication works! Library access confirmed.")
    except Exception as e:
        print(f"⚠️  Warning: {e}")
        print("browser.json was created but the test failed.")
        print("The cookies may be stale — try copying fresh ones.")
        return False

    print("\n🎉 Done! Restart your MCP server to use the new authentication.")
    print(f"   (cookie.txt is kept at {COOKIE_PATH} for future re-runs)")
    return True


if __name__ == "__main__":
    print("🔐 YouTube Music Authentication Updater\n")
    update_auth()
