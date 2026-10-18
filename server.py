#!/usr/bin/env python3
"""
YouTube Music MCP Server

A Model Context Protocol (MCP) server bridging YouTube Music to AI assistants.
Implements tools, resources, and prompts for music discovery, library management,
playlist creation, mood-based exploration, and personalized recommendations.

Tools  (15): search, library stats, top artists, similar songs (real radio),
             recommendations (async), create/list/get/add playlists,
             smart playlist builder, mood explorer, charts, history insights,
             server info
Resources (3): library://songs  library://artists  library://playlists
Prompts   (3): weekly-discovery-mix  mood-based-playlist  artist-deep-dive
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import ytmusicapi
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)
from ytmusicapi import YTMusic

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ytmusic-mcp")


# ─────────────────────────────────────────────
# Custom Exceptions
# ─────────────────────────────────────────────
class AuthError(RuntimeError):
    """Authentication file missing or invalid."""


class SearchError(RuntimeError):
    """Search returned no usable results."""


class PlaylistError(RuntimeError):
    """Playlist create/update operation failed."""


# ─────────────────────────────────────────────
# TTL Cache  (lightweight, no external deps)
# ─────────────────────────────────────────────
_cache: dict[str, tuple[Any, float]] = {}
CACHE_TTL = 300  # seconds (5 min)


def _cache_get(key: str) -> Any | None:
    if key in _cache:
        value, ts = _cache[key]
        if time.monotonic() - ts < CACHE_TTL:
            return value
        del _cache[key]
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (value, time.monotonic())


def _cache_invalidate(prefix: str = "") -> None:
    for k in list(_cache):
        if k.startswith(prefix):
            del _cache[k]


# ─────────────────────────────────────────────
# Paths (relative to this script, not cwd)
# ─────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_OAUTH_PATH = _SCRIPT_DIR / "oauth.json"
_BROWSER_PATH = _SCRIPT_DIR / "browser.json"
_COOKIE_PATH = _SCRIPT_DIR / "cookie.txt"


# ─────────────────────────────────────────────
# Cookie → browser.json helper
# ─────────────────────────────────────────────
def _extract_sapisid(cookie_string: str) -> str:
    """Extract SAPISID value from a raw cookie header string."""
    for part in cookie_string.split(";"):
        part = part.strip()
        if part.startswith("SAPISID="):
            return part.split("=", 1)[1]
    raise AuthError(
        "SAPISID not found in cookie string. "
        "Copy the complete 'cookie:' header value from DevTools."
    )


def _generate_sapisidhash(sapisid: str) -> str:
    """Generate a SAPISIDHASH authorization value.

    ytmusicapi requires this header to classify the auth file as browser auth.
    """
    timestamp = int(time.time())
    hash_input = f"{timestamp} {sapisid} https://music.youtube.com"
    sha1 = hashlib.sha1(hash_input.encode()).hexdigest()
    return f"SAPISIDHASH {timestamp}_{sha1}"


def _build_browser_json_from_cookie(cookie_path: Path, browser_path: Path) -> None:
    """Read raw cookie string from cookie.txt and write browser.json.

    Generates a fresh SAPISIDHASH (required by ytmusicapi to detect browser auth).
    """
    cookie_string = cookie_path.read_text().strip()
    if not cookie_string:
        raise AuthError("cookie.txt exists but is empty.")

    # Sanity-check: must contain known YouTube cookies
    if "SAPISID=" not in cookie_string or "SID=" not in cookie_string:
        raise AuthError(
            "cookie.txt does not look like a valid YouTube cookie string "
            "(missing SAPISID or SID). Copy the full cookie header value "
            "from your browser's DevTools Network tab."
        )

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

    browser_path.write_text(json.dumps(browser_data, indent=2))
    browser_path.chmod(0o600)
    log.info("Generated %s from %s", browser_path.name, cookie_path.name)


# ─────────────────────────────────────────────
# YTMusic Singleton
# ─────────────────────────────────────────────
_ytmusic: YTMusic | None = None
_auth_method: str = "none"


def get_yt() -> YTMusic:
    global _ytmusic, _auth_method
    if _ytmusic is not None:
        return _ytmusic

    try:
        # Priority 1: OAuth (auto-refreshing tokens)
        if _OAUTH_PATH.exists():
            _ytmusic = YTMusic(str(_OAUTH_PATH))
            _auth_method = "oauth"
            log.info("Authenticated via OAuth (%s)", _OAUTH_PATH)

        # Priority 2: cookie.txt present → always (re)generate browser.json
        # This ensures fresh cookies always win over a potentially stale browser.json.
        elif _COOKIE_PATH.exists():
            log.info("Found %s — generating browser.json…", _COOKIE_PATH)
            _build_browser_json_from_cookie(_COOKIE_PATH, _BROWSER_PATH)
            _ytmusic = YTMusic(str(_BROWSER_PATH))
            _auth_method = "browser (from cookie.txt)"
            log.info("Authenticated via cookie.txt → browser.json")

        # Priority 3: existing browser.json (no cookie.txt alongside it)
        elif _BROWSER_PATH.exists():
            _ytmusic = YTMusic(str(_BROWSER_PATH))
            _auth_method = "browser"
            log.info("Authenticated via browser cookies (%s)", _BROWSER_PATH)

        else:
            raise AuthError(
                "No auth file found. Place a cookie.txt (easiest), "
                "browser.json, or oauth.json in the project directory — see README.md."
            )
        return _ytmusic
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError(f"Failed to initialize YTMusic: {exc}") from exc


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _artist_name(artist: str | dict) -> str:
    if isinstance(artist, str):
        return artist
    if isinstance(artist, dict):
        return artist.get("name", "Unknown Artist")
    return "Unknown Artist"


def _song_to_dict(song: dict) -> dict:
    """Normalize a YTMusic song/track dict into a clean JSON-friendly dict."""
    artists = song.get("artists") or []
    album_obj = song.get("album") or {}
    return {
        "title": song.get("title", "Unknown"),
        "artist": _artist_name(artists[0]) if artists else "Unknown Artist",
        "album": album_obj.get("name") if isinstance(album_obj, dict) else None,
        "videoId": song.get("videoId"),
        "duration": song.get("duration") or song.get("duration_seconds"),
    }


def _json_response(data: Any) -> list[TextContent]:
    """Return a single TextContent with JSON-serialized data."""
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]


def _get_library_songs_cached() -> list[dict]:
    cached = _cache_get("library_songs")
    if cached is not None:
        log.debug("Cache hit: library_songs")
        return cached
    yt = get_yt()
    log.info("Fetching full library (uncached)…")
    songs = yt.get_library_songs(limit=None)
    _cache_set("library_songs", songs)
    return songs


def _get_library_playlists_cached() -> list[dict]:
    cached = _cache_get("library_playlists")
    if cached is not None:
        return cached
    yt = get_yt()
    playlists = yt.get_library_playlists(limit=None)
    _cache_set("library_playlists", playlists)
    return playlists


def _artist_counts(songs: list[dict]) -> Counter:
    counts: Counter = Counter()
    for song in songs:
        artists = song.get("artists") or []
        if artists:
            counts[_artist_name(artists[0])] += 1
    return counts


# ─────────────────────────────────────────────
# Tool Handlers
# ─────────────────────────────────────────────

async def tool_get_liked_songs_count() -> list[TextContent]:
    songs = _get_library_songs_cached()
    return _json_response({"total_songs": len(songs)})


async def tool_get_library_stats(detailed: bool = False) -> list[TextContent]:
    songs = _get_library_songs_cached()
    total = len(songs)
    counts = _artist_counts(songs)
    unique_artists = len(counts)
    playlists = _get_library_playlists_cached()

    result: dict[str, Any] = {
        "total_songs": total,
        "unique_artists": unique_artists,
        "total_playlists": len(playlists),
    }

    if detailed and total > 0:
        avg = total / unique_artists if unique_artists else 0
        result["avg_songs_per_artist"] = round(avg, 1)
        result["top_artists"] = [
            {"artist": artist, "songs": cnt, "percentage": round(cnt / total * 100, 1)}
            for artist, cnt in counts.most_common(5)
        ]

    return _json_response(result)


async def tool_search_music(query: str, filter_type: str = "all", limit: int = 10) -> list[TextContent]:
    yt = get_yt()
    filter_map = {
        "songs": "songs", "albums": "albums", "artists": "artists",
        "playlists": "playlists", "videos": "videos", "all": None,
    }
    search_filter = filter_map.get(filter_type)
    results = yt.search(query, filter=search_filter, limit=limit)

    items = []
    for item in results:
        entry: dict[str, Any] = {
            "resultType": item.get("resultType", "other"),
            "title": item.get("title") or item.get("artist", "Unknown"),
            "videoId": item.get("videoId"),
            "browseId": item.get("browseId"),
        }
        artists = item.get("artists") or []
        if artists:
            entry["artist"] = _artist_name(artists[0])
        album_obj = item.get("album") or {}
        if isinstance(album_obj, dict) and album_obj.get("name"):
            entry["album"] = album_obj["name"]
        if item.get("playlistId"):
            entry["playlistId"] = item["playlistId"]
        items.append(entry)

    return _json_response({"query": query, "filter": filter_type, "results": items})


async def tool_get_top_artists(limit: int = 10) -> list[TextContent]:
    songs = _get_library_songs_cached()
    if not songs:
        return _json_response({"total_songs": 0, "artists": []})

    counts = _artist_counts(songs)
    total = len(songs)
    top = counts.most_common(limit)

    artists = [
        {"rank": i, "artist": artist, "songs": cnt, "percentage": round(cnt / total * 100, 1)}
        for i, (artist, cnt) in enumerate(top, 1)
    ]
    return _json_response({"total_songs": total, "artists": artists})


async def tool_find_similar_songs(query: str, limit: int = 10) -> list[TextContent]:
    """Uses YTMusic's real watch playlist / radio — genuine similarity engine."""
    yt = get_yt()

    # 1. Find the seed song
    results = yt.search(query, filter="songs", limit=5)
    seed = next((r for r in results if r.get("resultType") == "song"), None)
    if not seed:
        raise SearchError(f'No song found matching "{query}".')

    seed_title = seed.get("title", "Unknown")
    seed_artists = seed.get("artists") or []
    seed_artist = _artist_name(seed_artists[0]) if seed_artists else "Unknown"
    video_id = seed.get("videoId")

    if not video_id:
        raise SearchError(f'Found "{seed_title}" but it has no video ID.')

    # 2. Get real YouTube Music radio/watch playlist for this song
    watch = yt.get_watch_playlist(videoId=video_id, radio=True, limit=limit + 1)
    tracks = watch.get("tracks", [])
    # Remove the seed song itself
    similar = [t for t in tracks if t.get("videoId") != video_id][:limit]

    return _json_response({
        "seed": {"title": seed_title, "artist": seed_artist, "videoId": video_id},
        "similar_songs": [_song_to_dict(t) for t in similar],
    })


async def tool_get_recommendations(count: int = 20) -> list[TextContent]:
    """Async-parallel recommendations from top 5 artists + mood categories."""
    songs = _get_library_songs_cached()
    if not songs:
        return _json_response({"based_on_artists": [], "recommendations": []})

    counts = _artist_counts(songs)
    top_artists = [a for a, _ in counts.most_common(5)]
    yt = get_yt()

    async def fetch_artist(artist: str) -> list[dict]:
        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, lambda: yt.search(artist, filter="songs", limit=12))
            return [r for r in results if r.get("resultType") == "song"]
        except Exception as exc:
            log.warning("Recommendation fetch failed for %s: %s", artist, exc)
            return []

    # Fetch all artists in parallel
    artist_results = await asyncio.gather(*[fetch_artist(a) for a in top_artists])

    seen: set[str] = set()
    recommendations: list[dict] = []
    for songs_list in artist_results:
        for song in songs_list:
            vid = song.get("videoId")
            if vid and vid not in seen:
                seen.add(vid)
                recommendations.append(song)
                if len(recommendations) >= count:
                    break
        if len(recommendations) >= count:
            break

    return _json_response({
        "based_on_artists": top_artists,
        "recommendations": [_song_to_dict(s) for s in recommendations],
    })


async def tool_create_playlist_from_songs(
    title: str,
    song_queries: list[str],
    description: str = "",
    privacy_status: str = "PRIVATE",
) -> list[TextContent]:
    yt = get_yt()

    found, not_found = [], []
    for query in song_queries:
        try:
            results = yt.search(query, filter="songs", limit=1)
            song = next((r for r in results if r.get("resultType") == "song"), None)
            if song and song.get("videoId"):
                found.append({"query": query, "song": song})
            else:
                not_found.append(query)
        except Exception as exc:
            log.warning("Search failed for '%s': %s", query, exc)
            not_found.append(query)

    if not found:
        return _json_response({
            "success": False,
            "error": "No songs found. Cannot create empty playlist.",
            "not_found_queries": not_found,
        })

    try:
        playlist_id = yt.create_playlist(title=title, description=description, privacy_status=privacy_status)
        video_ids = [item["song"]["videoId"] for item in found]
        yt.add_playlist_items(playlist_id, video_ids)
        _cache_invalidate("library_playlists")
    except Exception as exc:
        raise PlaylistError(f"Failed to create playlist: {exc}") from exc

    return _json_response({
        "success": True,
        "playlistId": playlist_id,
        "title": title,
        "description": description,
        "privacy_status": privacy_status,
        "added_songs": [
            {"query": item["query"], **_song_to_dict(item["song"])} for item in found
        ],
        "not_found_queries": not_found,
    })


async def tool_list_playlists(limit: int = 25) -> list[TextContent]:
    playlists = _get_library_playlists_cached()
    shown = playlists[:limit]
    items = [
        {
            "title": pl.get("title", "Unnamed"),
            "playlistId": pl.get("playlistId", ""),
            "count": pl.get("count"),
        }
        for pl in shown
    ]
    return _json_response({"total": len(playlists), "playlists": items})


async def tool_get_playlist_songs(playlist_id: str, limit: int = 50) -> list[TextContent]:
    yt = get_yt()
    playlist = yt.get_playlist(playlist_id, limit=limit)
    tracks = playlist.get("tracks", [])

    return _json_response({
        "playlistId": playlist_id,
        "title": playlist.get("title", "Playlist"),
        "track_count": len(tracks),
        "tracks": [_song_to_dict(t) for t in tracks],
    })


async def tool_add_songs_to_playlist(playlist_id: str, song_queries: list[str]) -> list[TextContent]:
    yt = get_yt()
    found, not_found = [], []

    for query in song_queries:
        try:
            results = yt.search(query, filter="songs", limit=1)
            song = next((r for r in results if r.get("resultType") == "song"), None)
            if song and song.get("videoId"):
                found.append({"query": query, "song": song})
            else:
                not_found.append(query)
        except Exception as exc:
            log.warning("Search failed for '%s': %s", query, exc)
            not_found.append(query)

    if not found:
        return _json_response({
            "success": False,
            "playlistId": playlist_id,
            "error": "No songs found to add.",
            "not_found_queries": not_found,
        })

    video_ids = [item["song"]["videoId"] for item in found]
    yt.add_playlist_items(playlist_id, video_ids)

    return _json_response({
        "success": True,
        "playlistId": playlist_id,
        "added_songs": [
            {"query": item["query"], **_song_to_dict(item["song"])} for item in found
        ],
        "not_found_queries": not_found,
    })


async def tool_build_smart_playlist(
    mood: str,
    genre: str = "",
    energy_level: str = "medium",
    count: int = 15,
    title: str = "",
    save_playlist: bool = False,
) -> list[TextContent]:
    """
    Agentic multi-step playlist builder.
    Step 1: Fetch mood categories from YTMusic.
    Step 2: Match requested mood/genre to a real YTMusic mood category.
    Step 3: Fetch playlist pool from that category.
    Step 4: Pick tracks from pool, filter by energy heuristic.
    Step 5: Optionally save as a new playlist.
    """
    yt = get_yt()
    steps: list[dict[str, Any]] = []

    # Step 1 — Fetch mood categories
    try:
        mood_cats = yt.get_mood_categories()
        steps.append({"step": 1, "action": "fetch_mood_categories", "status": "ok"})
    except Exception as exc:
        log.warning("get_mood_categories failed: %s", exc)
        mood_cats = {}
        steps.append({"step": 1, "action": "fetch_mood_categories", "status": "failed", "error": str(exc)})

    # Step 2 — Match mood keyword
    matched_params: str | None = None
    matched_label: str = ""
    target = (mood + " " + genre).lower()

    for section, categories in mood_cats.items():
        for cat in categories:
            cat_title = cat.get("title", "").lower()
            if any(word in cat_title for word in target.split() if len(word) > 2):
                matched_params = cat.get("params")
                matched_label = cat.get("title", "")
                break
        if matched_params:
            break

    if not matched_params:
        steps.append({"step": 2, "action": "match_mood", "status": "fallback", "detail": "No category match; using search"})
        try:
            results = yt.search(f"{mood} {genre} music playlist".strip(), filter="playlists", limit=5)
            playlist_pool = results[:3]
        except Exception:
            playlist_pool = []
    else:
        steps.append({"step": 2, "action": "match_mood", "status": "matched", "matched_category": matched_label})
        try:
            playlist_pool = yt.get_mood_playlists(matched_params)[:5]
            steps.append({"step": 3, "action": "fetch_mood_playlists", "status": "ok", "count": len(playlist_pool)})
        except Exception as exc:
            log.warning("get_mood_playlists failed: %s", exc)
            playlist_pool = []
            steps.append({"step": 3, "action": "fetch_mood_playlists", "status": "failed", "error": str(exc)})

    # Step 4 — Gather tracks from playlists
    all_tracks: list[dict] = []
    seen_vids: set[str] = set()

    for pl in playlist_pool:
        pl_id = pl.get("playlistId") or pl.get("browseId", "")
        if not pl_id:
            continue
        try:
            pl_data = yt.get_playlist(pl_id, limit=30)
            for track in pl_data.get("tracks", []):
                vid = track.get("videoId")
                if vid and vid not in seen_vids:
                    seen_vids.add(vid)
                    all_tracks.append(track)
                if len(all_tracks) >= count * 3:
                    break
        except Exception:
            continue
        if len(all_tracks) >= count * 3:
            break

    # Energy heuristic
    energy_filter: list[dict] = []
    for track in all_tracks:
        dur = track.get("duration_seconds") or 0
        if energy_level == "high" and (dur < 210 or dur == 0):
            energy_filter.append(track)
        elif energy_level == "low" and dur > 240:
            energy_filter.append(track)
        else:
            energy_filter.append(track)

    selected = energy_filter[:count]
    steps.append({"step": 4, "action": "sample_and_filter", "status": "ok", "tracks_selected": len(selected)})

    result: dict[str, Any] = {
        "mood": mood,
        "genre": genre or None,
        "energy_level": energy_level,
        "steps": steps,
        "tracks": [_song_to_dict(t) for t in selected],
    }

    # Optional save
    if save_playlist and selected:
        pl_title = title or f"{mood.title()} {genre.title()} Mix".strip()
        pl_desc = f"Smart playlist: {mood} {genre}, {energy_level} energy. Created by YouTube Music MCP."
        try:
            video_ids = [t["videoId"] for t in selected if t.get("videoId")]
            pl_id = yt.create_playlist(title=pl_title, description=pl_desc, privacy_status="PRIVATE")
            yt.add_playlist_items(pl_id, video_ids)
            _cache_invalidate("library_playlists")
            result["saved_playlist"] = {"playlistId": pl_id, "title": pl_title}
            steps.append({"step": 5, "action": "save_playlist", "status": "ok", "playlistId": pl_id})
        except Exception as exc:
            result["saved_playlist"] = None
            steps.append({"step": 5, "action": "save_playlist", "status": "failed", "error": str(exc)})

    return _json_response(result)


async def tool_explore_moods() -> list[TextContent]:
    """Fetch all available mood & genre categories from YouTube Music."""
    yt = get_yt()
    try:
        mood_cats = yt.get_mood_categories()
    except Exception as exc:
        raise SearchError(f"Could not fetch mood categories: {exc}") from exc

    sections: list[dict[str, Any]] = []
    for section, categories in mood_cats.items():
        sections.append({
            "section": section,
            "categories": [cat.get("title", "Unknown") for cat in categories],
        })

    return _json_response({"mood_categories": sections})


async def tool_get_charts(country: str = "ZZ") -> list[TextContent]:
    """Get global or country-specific music charts."""
    yt = get_yt()
    try:
        charts = yt.get_charts(country=country)
    except Exception as exc:
        raise SearchError(f"Could not fetch charts: {exc}") from exc

    result: dict[str, Any] = {"country": country}

    # videos is a list of playlist references (e.g. "Daily Top Music Videos")
    videos = charts.get("videos", [])
    if isinstance(videos, list) and videos:
        result["trending_playlists"] = [
            {"title": v.get("title", "Unknown"), "playlistId": v.get("playlistId")}
            for v in videos[:10] if isinstance(v, dict)
        ]
    elif isinstance(videos, dict):
        items = videos.get("items", [])[:10]
        result["trending_playlists"] = [
            {"title": v.get("title", "Unknown"), "playlistId": v.get("playlistId")}
            for v in items if isinstance(v, dict)
        ]

    # artists is a flat list of artist dicts
    artists_data = charts.get("artists", [])
    if isinstance(artists_data, list) and artists_data:
        result["trending_artists"] = [
            {
                "rank": a.get("rank", i),
                "name": a.get("title", "Unknown"),
                "browseId": a.get("browseId"),
                "subscribers": a.get("subscribers"),
                "trend": a.get("trend"),
            }
            for i, a in enumerate(artists_data[:20], 1) if isinstance(a, dict)
        ]
    elif isinstance(artists_data, dict):
        items = artists_data.get("items", [])[:20]
        result["trending_artists"] = [
            {"rank": i, "name": a.get("title", "Unknown")}
            for i, a in enumerate(items, 1) if isinstance(a, dict)
        ]

    return _json_response(result)


async def tool_get_listening_insights() -> list[TextContent]:
    """Analyze listening history to surface patterns and insights."""
    yt = get_yt()

    try:
        history = yt.get_history()
    except Exception as exc:
        return _json_response({"error": f"Could not fetch history: {exc}", "hint": "History requires authentication."})

    if not history:
        return _json_response({"total_tracks": 0, "top_artists": [], "top_albums": [], "diversity_score": 0})

    # Analyze
    artist_counts: Counter = Counter()
    album_counts: Counter = Counter()

    for track in history:
        artists = track.get("artists") or []
        if artists:
            artist_counts[_artist_name(artists[0])] += 1
        album_obj = track.get("album") or {}
        album = album_obj.get("name", "") if isinstance(album_obj, dict) else ""
        if album:
            album_counts[album] += 1

    total = len(history)
    unique_artists = len(artist_counts)
    diversity = unique_artists / total * 100 if total else 0
    mood_word = "varied" if diversity > 50 else ("balanced" if diversity > 25 else "focused")

    top_artist_name = artist_counts.most_common(1)[0][0] if artist_counts else None
    top_artist_plays = artist_counts.most_common(1)[0][1] if artist_counts else 0
    if top_artist_plays > total * 0.2:
        insight = f"You're in a {top_artist_name} phase — {top_artist_plays} of your last {total} plays."
    else:
        insight = f"Your recent listening is eclectic — spread across {unique_artists} artists."

    return _json_response({
        "total_tracks": total,
        "unique_artists": unique_artists,
        "diversity_score": round(diversity, 1),
        "diversity_label": mood_word,
        "top_artists": [{"artist": a, "plays": c} for a, c in artist_counts.most_common(5)],
        "top_albums": [{"album": a, "plays": c} for a, c in album_counts.most_common(3)],
        "insight": insight,
    })


# ── Recommendation helpers ─────────────────────

# YouTube answers HTTP 429 when ~20 artists are fetched at once; keep parallelism low.
_YT_POOL = ThreadPoolExecutor(max_workers=3)

# Marker words only count inside brackets or after " - ", so "Alive" or "Live Your Life" pass.
# Remixes are allowed: in reggaeton they are often official tracks with extra artists.
_VARIANT_RE = re.compile(
    r"(?:[(\[]|\s-\s)[^)\]]*\b(live|en vivo|sped[\s-]?up|slowed|reverb|nightcore|radio edit|clean)\b",
    re.IGNORECASE,
)

_DISCARDS_PATH = _SCRIPT_DIR / "descartes.json"


def _is_variant(title: str | None) -> bool:
    """True for live / sped up / slowed / clean versions."""
    return bool(_VARIANT_RE.search(title or ""))


def _norm(text: str | None) -> str:
    """Lowercase without punctuation, so 'Tito "El Bambino"' == 'Tito El Bambino'."""
    return re.sub(r"\W+", "", (text or "").lower())


_VIDEO_TAG_RE = re.compile(
    r"[(\[][^)\]]*\b(video|v[ií]deo|oficial|official|visualizer|lyrics?|letra|audio)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)


def _track_keys(song: dict) -> set[str]:
    """Loose identities of a song, one per credited artist, so that the audio track and its music
    video match ("PARADIXE" by Feid & Pirlo == "PARADIXE" by Pirlo,
    "TRATRATRA - ANMI, JUSEPH (Video Oficial)" == "TRATRATRA")."""
    artists = [_norm(a) for a in song.get("artists") or [song.get("artist")] if _norm(a)]
    title = _VIDEO_TAG_RE.sub("", song.get("title") or "")
    if " - " in title:
        # Music video titles often read "Artist - Title" or "Title - Artist, Artist".
        left, right = title.split(" - ", 1)
        if any(a in _norm(left) for a in artists):
            title = right
        elif any(a in _norm(right) for a in artists):
            title = left
    core = _norm(title)
    return {f"{core}|{a}" for a in artists} or {f"{core}|"}


def _rec_song(track: dict) -> dict:
    """_song_to_dict plus every credited artist (needed to tell collaborations apart)."""
    return {**_song_to_dict(track), "artists": [_artist_name(a) for a in track.get("artists") or []]}


def _plays(track: dict) -> float:
    """'3.4M plays' → 3400000.0; 0 when unknown."""
    m = re.match(r"([\d.,]+)\s*([KMB]?)", track.get("views") or "")
    if not m:
        return 0
    return float(m.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]


def _release_date(yt: YTMusic, video_id: str) -> date | None:
    """Upload date of a track; the album endpoint only gives the year."""
    try:
        mf = yt.get_song(video_id).get("microformat", {}).get("microformatDataRenderer", {})
        raw = mf.get("uploadDate") or mf.get("publishDate")
        return date.fromisoformat(raw[:10]) if raw else None
    except Exception as exc:
        log.warning("No release date for %s: %s", video_id, exc)
        return None


def _resolve_artist_id(yt: YTMusic, artist: str) -> str:
    """Artist name → channelId, preferring an exact name match."""
    results = [r for r in yt.search(artist, filter="artists", limit=5) if r.get("browseId")]
    if not results:
        raise SearchError(f'No artist found matching "{artist}".')
    exact = next((r for r in results if _norm(r.get("artist")) == _norm(artist)), None)
    return (exact or results[0])["browseId"]


def _new_releases(yt: YTMusic, artist: dict, months: int) -> list[dict]:
    """Albums and singles on the artist page released within the last `months`."""
    cutoff = date.today() - timedelta(days=months * 30)
    releases = []
    for kind in ("singles", "albums"):
        for rel in (artist.get(kind) or {}).get("results", []):
            year = str(rel.get("year") or "")
            if not year.isdigit() or int(year) < cutoff.year or not rel.get("browseId"):
                continue
            album = yt.get_album(rel["browseId"])
            tracks = album.get("tracks", [])
            released = None
            if int(year) == cutoff.year and tracks and tracks[0].get("videoId"):
                # Only the boundary year needs an exact date: one extra request per release.
                released = _release_date(yt, tracks[0]["videoId"])
                if released and released < cutoff:
                    continue
            title = album.get("title") or rel.get("title")
            releases.append({
                "title": title,
                "type": album.get("type") or kind[:-1].title(),
                "year": int(year),
                "date": released.isoformat() if released else None,
                "browseId": rel["browseId"],
                "tracks": [{**_rec_song(t), "album": title, "plays": _plays(t)} for t in tracks],
            })
    return releases


def _known_tracks(yt: YTMusic) -> tuple[set[str], set[str]]:
    """(videoIds, track keys) of everything the user already knows: library, liked songs,
    history, every library playlist and the local discard list (descartes.json)."""
    cached = _cache_get("known_tracks")
    if cached is not None:
        return cached
    songs = list(_get_library_songs_cached())
    songs += yt.get_liked_songs(limit=None).get("tracks", [])
    try:
        songs += yt.get_history()
    except Exception as exc:
        log.warning("History unavailable, skipping: %s", exc)
    for pl in _get_library_playlists_cached():
        if pl.get("playlistId") in (None, "LM", "SE"):
            continue
        try:
            songs += yt.get_playlist(pl["playlistId"], limit=None).get("tracks", [])
        except Exception as exc:
            log.warning("Skipping playlist %s: %s", pl.get("title"), exc)
    normalized = [_rec_song(s) for s in songs]
    if _DISCARDS_PATH.exists():
        normalized += json.loads(_DISCARDS_PATH.read_text())
    known = (
        {s["videoId"] for s in normalized if s.get("videoId")},
        set().union(*(_track_keys(s) for s in normalized)),
    )
    _cache_set("known_tracks", known)
    return known


def _unheard_top_tracks(yt: YTMusic, artist: dict, limit: int, known: tuple[set[str], set[str]]) -> list[dict]:
    """Artist's top songs, in popularity order, minus anything in `known`."""
    songs = artist.get("songs") or {}
    tracks = songs.get("results", [])
    if songs.get("browseId"):
        # The artist page only shows ~5; the "Songs" playlist has the full ranked list.
        tracks = yt.get_playlist(songs["browseId"], limit=limit * 3 + 20).get("tracks") or tracks
    ids, keys = known
    unheard = []
    for t in tracks:
        song = _rec_song(t)
        if song["videoId"] in ids or _track_keys(song) & keys:
            continue
        unheard.append(song)
        if len(unheard) >= limit:
            break
    return unheard


def _interleave(per_artist: list[list[dict]], cap: int, seen_ids: set[str], seen_keys: set[str]) -> list[dict]:
    """Round-robin one song per artist until `cap`, skipping (and recording) duplicates."""
    queues = [list(q) for q in per_artist]
    picked: list[dict] = []
    while len(picked) < cap and any(queues):
        for queue in queues:
            while queue:
                song = queue.pop(0)
                keys = _track_keys(song)
                if song["videoId"] in seen_ids or keys & seen_keys:
                    continue
                seen_ids.add(song["videoId"])
                seen_keys.update(keys)
                picked.append(song)
                break
            if len(picked) >= cap:
                break
    return picked


async def tool_get_artist_new_releases(artist: str, months: int = 12) -> list[TextContent]:
    yt = get_yt()
    data = yt.get_artist(_resolve_artist_id(yt, artist))
    return _json_response({
        "artist": data.get("name", artist),
        "months": months,
        "releases": _new_releases(yt, data, months),
    })


async def tool_get_unheard_top_tracks(artist: str, limit: int = 10) -> list[TextContent]:
    yt = get_yt()
    data = yt.get_artist(_resolve_artist_id(yt, artist))
    return _json_response({
        "artist": data.get("name", artist),
        "tracks": _unheard_top_tracks(yt, data, limit, _known_tracks(yt)),
    })


async def tool_recommend_for_playlist(
    playlist_id: str,
    months: int = 12,
    max_novedades: int = 12,
    max_hits: int = 12,
    include_variants: bool = False,
    max_artists: int = 20,
) -> list[TextContent]:
    yt = get_yt()
    playlist = yt.get_playlist(playlist_id, limit=None)
    tracks = playlist.get("tracks", [])

    artist_counts: Counter = Counter()
    artist_names: dict[str, str] = {}
    for t in tracks:
        first = (t.get("artists") or [{}])[0]
        if isinstance(first, dict) and first.get("id"):
            artist_counts[first["id"]] += 1
            artist_names[first["id"]] = first.get("name") or ""
    # ponytail: caps artists to bound API calls (~3 + releases per artist); raise max_artists for big playlists
    artist_ids = [a for a, _ in artist_counts.most_common(max_artists)]
    known_ids, known_keys = _known_tracks(yt)

    def eligible(song: dict, artist: str) -> bool:
        """Unknown, not a live/sped up/clean version, and the artist is credited on it."""
        return bool(
            song["videoId"]
            and song["videoId"] not in known_ids
            and not _track_keys(song) & known_keys
            and (include_variants or not _is_variant(song["title"]))
            and _norm(artist) in {_norm(a) for a in song["artists"]}
        )

    def fetch(channel_id: str) -> tuple[str, list[dict], list[dict]]:
        name = artist_names.get(channel_id) or channel_id
        try:
            try:
                data = yt.get_artist(channel_id)
            except KeyError:
                # Plain YouTube channels (not YT Music artist pages) fail to parse; retry by name.
                data = yt.get_artist(_resolve_artist_id(yt, name))
            name = data.get("name", name)
            new: list[dict] = []
            for rel in _new_releases(yt, data, months):
                songs = [s for s in rel["tracks"] if eligible(s, name)]
                if rel["type"] != "Single":
                    # Albums / EPs: only their 2 most played tracks.
                    songs = sorted(songs, key=lambda s: s["plays"], reverse=True)[:2]
                new += [
                    {**s, "release": rel["title"], "release_type": rel["type"], "date": rel["date"] or str(rel["year"])}
                    for s in songs
                ]
            top = [s for s in _unheard_top_tracks(yt, data, 10, (known_ids, known_keys)) if eligible(s, name)]
            return name, new, top
        except Exception as exc:
            log.warning("Recommendation fetch failed for %s (%s): %s", name, channel_id, exc)
            return name, [], []

    loop = asyncio.get_running_loop()
    results = await asyncio.gather(*[loop.run_in_executor(_YT_POOL, fetch, cid) for cid in artist_ids])

    in_playlist = [_rec_song(t) for t in tracks]
    seen_ids = {s["videoId"] for s in in_playlist if s["videoId"]}
    seen_keys = set().union(*(_track_keys(s) for s in in_playlist))
    novedades = _interleave([new for _, new, _ in results], max_novedades, seen_ids, seen_keys)
    hits = _interleave([top for _, _, top in results], max_hits, seen_ids, seen_keys)

    return _json_response({
        "playlistId": playlist_id,
        "title": playlist.get("title", "Playlist"),
        "artists_considered": [name for name, _, _ in results],
        "novedades": novedades,
        "hits_que_no_conoces": hits,
    })


# ── Proposals playlist cycle ───────────────────
# recommend → fill "<title> - propuestas" → user likes songs on their phone → review → apply.

_PROPOSALS_SUFFIX = " - propuestas"


def _playlist_title(yt: YTMusic, playlist_id: str) -> str:
    for pl in _get_library_playlists_cached():
        if pl.get("playlistId") == playlist_id:
            return pl.get("title") or playlist_id
    return yt.get_playlist(playlist_id, limit=1).get("title") or playlist_id


def _find_proposals(yt: YTMusic, playlist_id: str) -> tuple[str, str | None]:
    """(proposals title, proposals playlistId or None if it doesn't exist yet)."""
    title = _playlist_title(yt, playlist_id) + _PROPOSALS_SUFFIX
    found = next((p["playlistId"] for p in _get_library_playlists_cached() if p.get("title") == title), None)
    return title, found


def _empty_playlist(yt: YTMusic, playlist_id: str, tracks: list[dict]) -> None:
    items = [t for t in tracks if t.get("videoId") and t.get("setVideoId")]
    if items:
        yt.remove_playlist_items(playlist_id, items)


def _add_discards(songs: list[dict]) -> int:
    """Append songs (normalized dicts) to descartes.json, skipping ones already there. Returns how many."""
    discards = json.loads(_DISCARDS_PATH.read_text()) if _DISCARDS_PATH.exists() else []
    already = {d.get("videoId") for d in discards}
    new = [s for s in songs if s.get("videoId") and s["videoId"] not in already]
    new = list({s["videoId"]: s for s in new}.values())
    _DISCARDS_PATH.write_text(json.dumps(discards + new, ensure_ascii=False, indent=1))
    _cache_invalidate("known_tracks")
    return len(new)


def _review(yt: YTMusic, playlist_id: str) -> tuple[str, list[dict], list[dict], list[dict]]:
    """(proposals id, proposals tracks, liked → add, rest → discard)."""
    title, proposals_id = _find_proposals(yt, playlist_id)
    if not proposals_id:
        raise PlaylistError(f'There is no "{title}" playlist yet. Use fill_proposals_playlist first.')
    tracks = yt.get_playlist(proposals_id, limit=None).get("tracks", [])
    in_source = {t.get("videoId") for t in yt.get_playlist(playlist_id, limit=None).get("tracks", [])}
    liked = [t for t in tracks if t.get("likeStatus") == "LIKE"]
    to_add = [_song_to_dict(t) for t in liked if t.get("videoId") not in in_source]
    to_discard = [_song_to_dict(t) for t in tracks if t.get("likeStatus") != "LIKE"]
    return proposals_id, tracks, to_add, to_discard


async def tool_fill_proposals_playlist(playlist_id: str, video_ids: list[str]) -> list[TextContent]:
    yt = get_yt()
    title, proposals_id = _find_proposals(yt, playlist_id)
    try:
        if proposals_id:
            _empty_playlist(yt, proposals_id, yt.get_playlist(proposals_id, limit=None).get("tracks", []))
            yt.add_playlist_items(proposals_id, video_ids, duplicates=False)
            created = False
        else:
            result = yt.create_playlist(
                title, f"Propuestas para {_playlist_title(yt, playlist_id)} (YouTube Music MCP)",
                privacy_status="PRIVATE", video_ids=video_ids,
            )
            if not isinstance(result, str):
                raise PlaylistError(f"create_playlist returned {result}")
            proposals_id, created = result, True
            _cache_invalidate("library_playlists")
    except PlaylistError:
        raise
    except Exception as exc:
        raise PlaylistError(f"Failed to fill {title}: {exc}") from exc
    _cache_invalidate("known_tracks")
    return _json_response({
        "success": True,
        "proposalsPlaylistId": proposals_id,
        "title": title,
        "created": created,
        "songs": len(video_ids),
    })


async def tool_review_proposals(playlist_id: str) -> list[TextContent]:
    yt = get_yt()
    proposals_id, tracks, to_add, to_discard = _review(yt, playlist_id)
    return _json_response({
        "proposalsPlaylistId": proposals_id,
        "total": len(tracks),
        "to_add": to_add,
        "to_discard": to_discard,
    })


async def tool_apply_proposals_review(
    playlist_id: str, add_video_ids: list[str], discard_video_ids: list[str]
) -> list[TextContent]:
    yt = get_yt()
    proposals_id, tracks, _, _ = _review(yt, playlist_id)
    by_id = {t.get("videoId"): t for t in tracks}
    try:
        if add_video_ids:
            yt.add_playlist_items(playlist_id, add_video_ids, duplicates=False)
        discarded = _add_discards([_rec_song(by_id[v]) for v in discard_video_ids if v in by_id])
        _empty_playlist(yt, proposals_id, tracks)
    except Exception as exc:
        raise PlaylistError(f"Failed to apply review: {exc}") from exc
    _cache_invalidate("known_tracks")
    return _json_response({
        "success": True,
        "added": len(add_video_ids),
        "discarded": discarded,
        "proposals_emptied": True,
    })


async def tool_discard_songs(songs: list[dict]) -> list[TextContent]:
    for song in songs:
        if not isinstance(song, dict) or not song.get("videoId") or not song.get("title"):
            raise ValueError("each song needs at least videoId and title")
    clean = [
        {
            "title": song["title"],
            "artist": song.get("artist") or (song.get("artists") or [None])[0],
            "artists": song.get("artists") or ([song["artist"]] if song.get("artist") else []),
            "album": song.get("album"),
            "videoId": song["videoId"],
            "duration": song.get("duration"),
        }
        for song in songs
    ]
    added = _add_discards(clean)
    return _json_response({"success": True, "discarded": added, "already_discarded": len(clean) - added})


async def tool_get_server_info() -> list[TextContent]:
    cached_songs = _cache_get("library_songs")
    lib_size = len(cached_songs) if cached_songs else None
    cached_pls = _cache_get("library_playlists")
    pl_size = len(cached_pls) if cached_pls else None

    return _json_response({
        "version": "2.0.0",
        "auth_method": _auth_method,
        "ytmusicapi_version": ytmusicapi.__version__,
        "cache_ttl_seconds": CACHE_TTL,
        "cached_library_songs": lib_size,
        "cached_playlists": pl_size,
        "capabilities": {
            "tools": len(TOOLS),
            "resources": 3,
            "prompts": 3,
            "features": ["async_parallel_search", "ttl_caching", "radio_similarity", "mood_genre_explorer", "charts", "listening_insights"],
        },
    })


# ─────────────────────────────────────────────
# MCP Server Setup
# ─────────────────────────────────────────────
app = Server("youtube-music-server")

# ── Tools ──────────────────────────────────────

TOOLS: list[Tool] = [
    Tool(
        name="get_liked_songs_count",
        description="Get the total count of songs in your YouTube Music library (bypasses display limit).",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_library_stats",
        description="Comprehensive library statistics: total songs, unique artists, playlists, and optional top-artist breakdown.",
        inputSchema={
            "type": "object",
            "properties": {
                "detailed": {
                    "type": "boolean",
                    "description": "Include detailed breakdown (top artists, avg songs per artist)",
                    "default": False,
                }
            },
            "required": [],
        },
    ),
    Tool(
        name="search_music",
        description="Search YouTube Music for songs, albums, artists, playlists, or videos.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "filter_type": {
                    "type": "string",
                    "description": "Filter results by type",
                    "enum": ["all", "songs", "albums", "artists", "playlists", "videos"],
                    "default": "all",
                },
                "limit": {"type": "number", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_top_artists",
        description="Get your top artists ranked by song count in your library, with visual progress bars.",
        inputSchema={
            "type": "object",
            "properties": {"limit": {"type": "number", "description": "Number of artists", "default": 10}},
            "required": [],
        },
    ),
    Tool(
        name="find_similar_songs",
        description="Find songs genuinely similar to a given track using YouTube Music's real radio engine.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Song name or 'artist - song'"},
                "limit": {"type": "number", "description": "Number of similar songs", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_recommendations",
        description="Get personalized recommendations based on your top 5 library artists, fetched in parallel.",
        inputSchema={
            "type": "object",
            "properties": {"count": {"type": "number", "description": "Number of recommendations", "default": 20}},
            "required": [],
        },
    ),
    Tool(
        name="create_playlist_from_songs",
        description="Create a new YouTube Music playlist and add songs to it by search query. WRITES to the user's account: only call after showing the exact song list and getting explicit user confirmation.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Playlist title"},
                "song_queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of song search queries",
                },
                "description": {"type": "string", "description": "Playlist description", "default": ""},
                "privacy_status": {
                    "type": "string",
                    "enum": ["PRIVATE", "PUBLIC", "UNLISTED"],
                    "default": "PRIVATE",
                },
            },
            "required": ["title", "song_queries"],
        },
    ),
    Tool(
        name="list_playlists",
        description="List all playlists in your YouTube Music library with song counts and IDs.",
        inputSchema={
            "type": "object",
            "properties": {"limit": {"type": "number", "description": "Max playlists to show", "default": 25}},
            "required": [],
        },
    ),
    Tool(
        name="get_playlist_songs",
        description="Get the songs in a specific playlist by playlist ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "playlist_id": {"type": "string", "description": "Playlist ID (from list_playlists)"},
                "limit": {"type": "number", "description": "Max songs to return", "default": 50},
            },
            "required": ["playlist_id"],
        },
    ),
    Tool(
        name="add_songs_to_playlist",
        description="Add songs to an existing playlist by search queries. WRITES to the user's account: only call after showing the exact song list and getting explicit user confirmation.",
        inputSchema={
            "type": "object",
            "properties": {
                "playlist_id": {"type": "string", "description": "Target playlist ID"},
                "song_queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Songs to search and add",
                },
            },
            "required": ["playlist_id", "song_queries"],
        },
    ),
    Tool(
        name="build_smart_playlist",
        description=(
            "Agentic multi-step playlist builder. Matches your mood/genre to real YouTube Music categories, "
            "samples tracks, applies energy filtering, and optionally saves as a playlist. "
            "Steps: fetch categories → match mood → pull playlists → sample tracks → energy filter → (optional) save. "
            "save_playlist=true WRITES to the user's account: only call after showing the exact song list and getting explicit user confirmation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mood": {"type": "string", "description": "Mood or vibe (e.g. 'chill', 'workout', 'focus', 'sad', 'happy')"},
                "genre": {"type": "string", "description": "Optional genre (e.g. 'pop', 'hip-hop', 'jazz')", "default": ""},
                "energy_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Energy level filter",
                    "default": "medium",
                },
                "count": {"type": "number", "description": "Number of tracks to include", "default": 15},
                "title": {"type": "string", "description": "Custom playlist title (optional)", "default": ""},
                "save_playlist": {
                    "type": "boolean",
                    "description": "Save the result as a new YouTube Music playlist",
                    "default": False,
                },
            },
            "required": ["mood"],
        },
    ),
    Tool(
        name="explore_moods",
        description="Discover all available Moods & Genres categories on YouTube Music. Use these with build_smart_playlist.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_charts",
        description="Get global or country-specific music charts from YouTube Music (trending songs and artists).",
        inputSchema={
            "type": "object",
            "properties": {
                "country": {
                    "type": "string",
                    "description": "ISO 3166-1 Alpha-2 country code (e.g. 'US', 'GB', 'IN'). Default 'ZZ' = global.",
                    "default": "ZZ",
                }
            },
            "required": [],
        },
    ),
    Tool(
        name="get_listening_insights",
        description="Analyze your listening history to surface patterns: top recent artists, albums, diversity score, and behavioural insights.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_server_info",
        description="Get metadata about this MCP server: auth method, version, cache state, and capabilities.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_artist_new_releases",
        description=(
            "Read-only. List an artist's singles and albums released in the last N months, each with its "
            "full tracklist, videoIds and play counts. Releases from the boundary year are checked against "
            "their exact upload date."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "artist": {"type": "string", "description": "Artist name, e.g. 'Rosalía'"},
                "months": {"type": "number", "description": "Look-back window in months", "default": 12},
            },
            "required": ["artist"],
        },
    ),
    Tool(
        name="get_unheard_top_tracks",
        description=(
            "Read-only. An artist's most popular songs, in popularity order, excluding anything the user "
            "already knows (library, liked songs, history, any of their playlists, or discarded before). "
            "Use it to find an "
            "artist's hits the user doesn't know yet."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "artist": {"type": "string", "description": "Artist name"},
                "limit": {"type": "number", "description": "Max tracks to return", "default": 10},
            },
            "required": ["artist"],
        },
    ),
    Tool(
        name="recommend_for_playlist",
        description=(
            "Read-only. Recommend songs for an existing playlist based on the artists already in it "
            "(most frequent first). Returns two separate lists, each round-robin across artists: "
            "'novedades' (from those artists' releases in the last N months: every track of a single, "
            "only the 2 most played of an album/EP) and 'hits_que_no_conoces' (their top songs). "
            "Every song is one the user doesn't know yet (not in library, liked songs, history, any of "
            "their playlists, or descartes.json), has the playlist artist credited (collaborations count), "
            "and appears only once. Live, sped up, slowed, reverb, nightcore and clean/radio edit versions "
            "are excluded unless include_variants=true; remixes are kept. "
            "This tool never modifies the playlist: to add anything, show the list to the user, get "
            "explicit confirmation, then call add_songs_to_playlist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "playlist_id": {"type": "string", "description": "Playlist ID (from list_playlists)"},
                "months": {"type": "number", "description": "Look-back window for new releases", "default": 12},
                "max_novedades": {"type": "number", "description": "Max songs in 'novedades'", "default": 12},
                "max_hits": {"type": "number", "description": "Max songs in 'hits_que_no_conoces'", "default": 12},
                "include_variants": {
                    "type": "boolean",
                    "description": "Include live/sped up/slowed/clean versions (only if the user asks)",
                    "default": False,
                },
                "max_artists": {
                    "type": "number",
                    "description": "Only consider the N most frequent artists in the playlist",
                    "default": 20,
                },
            },
            "required": ["playlist_id"],
        },
    ),
    Tool(
        name="fill_proposals_playlist",
        description=(
            "Step 2 of the proposals cycle (after recommend_for_playlist). Puts the given songs into the "
            "private playlist '<playlist title> - propuestas', creating it if needed; if it already exists "
            "it is EMPTIED first and refilled. The user then listens on their phone and likes the songs "
            "they want to keep. WRITES to the user's account: only call after showing the exact song list "
            "and getting explicit user confirmation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "playlist_id": {"type": "string", "description": "The SOURCE playlist the proposals are for"},
                "video_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "videoIds to propose (from recommend_for_playlist)",
                },
            },
            "required": ["playlist_id", "video_ids"],
        },
    ),
    Tool(
        name="review_proposals",
        description=(
            "Read-only. Step 3 of the proposals cycle: looks at '<playlist title> - propuestas' and splits "
            "it into 'to_add' (songs the user liked, not yet in the source playlist) and 'to_discard' "
            "(everything not liked). Show both lists to the user before apply_proposals_review."
        ),
        inputSchema={
            "type": "object",
            "properties": {"playlist_id": {"type": "string", "description": "The SOURCE playlist"}},
            "required": ["playlist_id"],
        },
    ),
    Tool(
        name="apply_proposals_review",
        description=(
            "Step 4 of the proposals cycle: adds add_video_ids to the source playlist, records "
            "discard_video_ids in the local discard list (they are never recommended again), and empties "
            "the proposals playlist. Pass exactly the lists from review_proposals that the user confirmed. "
            "WRITES to the user's account: only call after showing the exact song lists and getting "
            "explicit user confirmation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "playlist_id": {"type": "string", "description": "The SOURCE playlist"},
                "add_video_ids": {"type": "array", "items": {"type": "string"}, "description": "Songs to add"},
                "discard_video_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Songs to discard",
                },
            },
            "required": ["playlist_id", "add_video_ids", "discard_video_ids"],
        },
    ),
    Tool(
        name="discard_songs",
        description=(
            "Mark songs as discarded so they are never recommended again, e.g. when the user says "
            "'I already know these' or 'I don't want these' about songs from recommend_for_playlist. "
            "Pass the song objects as returned by recommend_for_playlist (title, artist(s) and videoId are "
            "what matter: matching also catches other versions of the same song, like its music video). "
            "Only writes the local descartes.json file; it does NOT touch the YouTube Music account. "
            "Confirm the exact list with the user before calling it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "songs": {
                    "type": "array",
                    "description": "Songs to discard",
                    "items": {
                        "type": "object",
                        "properties": {
                            "videoId": {"type": "string"},
                            "title": {"type": "string"},
                            "artist": {"type": "string"},
                            "artists": {"type": "array", "items": {"type": "string"}},
                            "album": {"type": "string"},
                        },
                        "required": ["videoId", "title"],
                    },
                },
            },
            "required": ["songs"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    log.info("Tool called: %s  args=%s", name, list((arguments or {}).keys()))
    args = arguments or {}
    try:
        match name:
            case "get_liked_songs_count":
                return await tool_get_liked_songs_count()
            case "get_library_stats":
                return await tool_get_library_stats(args.get("detailed", False))
            case "search_music":
                if not args.get("query"):
                    raise ValueError("query is required")
                return await tool_search_music(args["query"], args.get("filter_type", "all"), args.get("limit", 10))
            case "get_top_artists":
                return await tool_get_top_artists(args.get("limit", 10))
            case "find_similar_songs":
                if not args.get("query"):
                    raise ValueError("query is required")
                return await tool_find_similar_songs(args["query"], args.get("limit", 10))
            case "get_recommendations":
                return await tool_get_recommendations(args.get("count", 20))
            case "create_playlist_from_songs":
                if not args.get("title"):
                    raise ValueError("title is required")
                if not isinstance(args.get("song_queries"), list):
                    raise ValueError("song_queries must be a list")
                return await tool_create_playlist_from_songs(
                    args["title"], args["song_queries"],
                    args.get("description", ""), args.get("privacy_status", "PRIVATE"),
                )
            case "list_playlists":
                return await tool_list_playlists(args.get("limit", 25))
            case "get_playlist_songs":
                if not args.get("playlist_id"):
                    raise ValueError("playlist_id is required")
                return await tool_get_playlist_songs(args["playlist_id"], args.get("limit", 50))
            case "add_songs_to_playlist":
                if not args.get("playlist_id"):
                    raise ValueError("playlist_id is required")
                if not isinstance(args.get("song_queries"), list):
                    raise ValueError("song_queries must be a list")
                return await tool_add_songs_to_playlist(args["playlist_id"], args["song_queries"])
            case "build_smart_playlist":
                if not args.get("mood"):
                    raise ValueError("mood is required")
                return await tool_build_smart_playlist(
                    args["mood"], args.get("genre", ""), args.get("energy_level", "medium"),
                    args.get("count", 15), args.get("title", ""), args.get("save_playlist", False),
                )
            case "explore_moods":
                return await tool_explore_moods()
            case "get_charts":
                return await tool_get_charts(args.get("country", "ZZ"))
            case "get_listening_insights":
                return await tool_get_listening_insights()
            case "get_server_info":
                return await tool_get_server_info()
            case "get_artist_new_releases":
                if not args.get("artist"):
                    raise ValueError("artist is required")
                return await tool_get_artist_new_releases(args["artist"], int(args.get("months", 12)))
            case "get_unheard_top_tracks":
                if not args.get("artist"):
                    raise ValueError("artist is required")
                return await tool_get_unheard_top_tracks(args["artist"], int(args.get("limit", 10)))
            case "recommend_for_playlist":
                if not args.get("playlist_id"):
                    raise ValueError("playlist_id is required")
                return await tool_recommend_for_playlist(
                    args["playlist_id"], int(args.get("months", 12)), int(args.get("max_novedades", 12)),
                    int(args.get("max_hits", 12)), bool(args.get("include_variants", False)),
                    int(args.get("max_artists", 20)),
                )
            case "fill_proposals_playlist":
                if not args.get("playlist_id"):
                    raise ValueError("playlist_id is required")
                if not isinstance(args.get("video_ids"), list) or not args["video_ids"]:
                    raise ValueError("video_ids must be a non-empty list")
                return await tool_fill_proposals_playlist(args["playlist_id"], args["video_ids"])
            case "review_proposals":
                if not args.get("playlist_id"):
                    raise ValueError("playlist_id is required")
                return await tool_review_proposals(args["playlist_id"])
            case "apply_proposals_review":
                if not args.get("playlist_id"):
                    raise ValueError("playlist_id is required")
                if not isinstance(args.get("add_video_ids"), list) or not isinstance(args.get("discard_video_ids"), list):
                    raise ValueError("add_video_ids and discard_video_ids must be lists")
                return await tool_apply_proposals_review(
                    args["playlist_id"], args["add_video_ids"], args["discard_video_ids"]
                )
            case "discard_songs":
                if not isinstance(args.get("songs"), list) or not args["songs"]:
                    raise ValueError("songs must be a non-empty list")
                return await tool_discard_songs(args["songs"])
            case _:
                raise ValueError(f"Unknown tool: {name}")

    except (AuthError, SearchError, PlaylistError) as exc:
        log.error("Domain error in %s: %s", name, exc)
        return _json_response({"error": str(exc), "error_type": type(exc).__name__})
    except ValueError as exc:
        return _json_response({"error": str(exc), "error_type": "ValueError"})
    except Exception as exc:
        log.exception("Unexpected error in tool %s", name)
        return _json_response({"error": str(exc), "error_type": "UnexpectedError"})


# ── Resources ──────────────────────────────────

RESOURCES: list[Resource] = [
    Resource(
        uri="library://songs",  # type: ignore[arg-type]
        name="Liked Songs Library",
        description="All songs in your YouTube Music library as structured JSON. Cached for 5 minutes.",
        mimeType="application/json",
    ),
    Resource(
        uri="library://artists",  # type: ignore[arg-type]
        name="Artist Rankings",
        description="Your top artists ranked by song count as structured JSON.",
        mimeType="application/json",
    ),
    Resource(
        uri="library://playlists",  # type: ignore[arg-type]
        name="Playlists",
        description="All your YouTube Music playlists as structured JSON.",
        mimeType="application/json",
    ),
]


@app.list_resources()
async def list_resources() -> list[Resource]:
    return RESOURCES


@app.read_resource()
async def read_resource(uri: Any) -> ReadResourceResult:
    uri_str = str(uri)
    log.info("Resource read: %s", uri_str)

    if uri_str == "library://songs":
        songs = _get_library_songs_cached()
        data = [
            {
                "title": s.get("title"),
                "artist": _artist_name((s.get("artists") or [{}])[0]),
                "album": (s.get("album") or {}).get("name"),
                "videoId": s.get("videoId"),
            }
            for s in songs
        ]
        return ReadResourceResult(
            contents=[TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(data, indent=2))]
        )

    elif uri_str == "library://artists":
        songs = _get_library_songs_cached()
        counts = _artist_counts(songs)
        total = len(songs)
        data = [
            {"rank": i, "artist": a, "songs": c, "percentage": round(c / total * 100, 1)}
            for i, (a, c) in enumerate(counts.most_common(50), 1)
        ]
        return ReadResourceResult(
            contents=[TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(data, indent=2))]
        )

    elif uri_str == "library://playlists":
        playlists = _get_library_playlists_cached()
        data = [
            {"title": p.get("title"), "playlistId": p.get("playlistId"), "count": p.get("count")}
            for p in playlists
        ]
        return ReadResourceResult(
            contents=[TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(data, indent=2))]
        )

    else:
        raise ValueError(f"Unknown resource URI: {uri_str}")


# ── Prompts ────────────────────────────────────

PROMPTS: list[Prompt] = [
    Prompt(
        name="weekly-discovery-mix",
        description="Generate a personalised weekly discovery playlist conversation starter. Analyses your library and suggests an exploration strategy.",
        arguments=[
            PromptArgument(name="discovery_style", description="'familiar' (artist adjacents) or 'adventurous' (new genres)", required=False),
        ],
    ),
    Prompt(
        name="mood-based-playlist",
        description="Build a mood-based playlist through a guided conversation. Asks about mood, energy, and context.",
        arguments=[
            PromptArgument(name="mood", description="Starting mood (e.g. chill, energetic, focus, sad)", required=True),
            PromptArgument(name="duration_minutes", description="Desired playlist duration in minutes", required=False),
        ],
    ),
    Prompt(
        name="artist-deep-dive",
        description="Deep dive into a specific artist: explore their discography, find similar artists, and build a comprehensive listening experience.",
        arguments=[
            PromptArgument(name="artist_name", description="Artist to explore", required=True),
        ],
    ),
]


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return PROMPTS


@app.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    args = arguments or {}
    log.info("Prompt requested: %s  args=%s", name, args)

    if name == "weekly-discovery-mix":
        style = args.get("discovery_style", "familiar")
        style_desc = (
            "Lean towards artists similar to what's already in my library — adjacent but slightly new."
            if style == "familiar"
            else "Push me outside my comfort zone — completely new genres and artists I've never heard."
        )
        return GetPromptResult(
            description="Weekly discovery mix prompt",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"I want to create my weekly discovery mix. Style: {style} ({style_desc})\n\n"
                            "Please:\n"
                            "1. Use `get_library_stats` (detailed=true) to understand my library\n"
                            "2. Use `get_top_artists` to see who I listen to most\n"
                            "3. Use `get_recommendations` to get a seed list\n"
                            "4. Use `find_similar_songs` on 2-3 of my favourite tracks\n"
                            "5. Compile a 20-song Weekly Discovery Mix playlist\n"
                            "6. Show me the final list and ask before saving it with `create_playlist_from_songs` as 'Weekly Discovery — [date]'\n\n"
                            "Show me each step's reasoning as you go."
                        ),
                    ),
                )
            ],
        )

    elif name == "mood-based-playlist":
        mood = args.get("mood", "chill")
        duration = args.get("duration_minutes", "30")
        return GetPromptResult(
            description="Mood-based playlist builder prompt",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"I'm in a **{mood}** mood and want a ~{duration} minute playlist.\n\n"
                            "Please:\n"
                            "1. Use `explore_moods` to find matching YouTube Music mood categories\n"
                            f"2. Use `build_smart_playlist` with mood='{mood}' to generate a track list\n"
                            "3. Cross-reference with my library using `get_library_stats` — prioritise artists I already love\n"
                            "4. Refine the list and explain why each track fits the mood\n"
                            "5. Ask me if I want to save it as a playlist\n\n"
                            "Be conversational — this is a collaborative music curation session."
                        ),
                    ),
                )
            ],
        )

    elif name == "artist-deep-dive":
        artist = args.get("artist_name", "")
        if not artist:
            raise ValueError("artist_name is required for artist-deep-dive prompt")
        return GetPromptResult(
            description=f"Artist deep dive: {artist}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Let's do a deep dive on **{artist}**.\n\n"
                            "Please:\n"
                            f"1. Use `search_music` (filter_type='songs') to find their top tracks\n"
                            f"2. Use `search_music` (filter_type='albums') to map their discography\n"
                            f"3. Use `find_similar_songs` on their most iconic song to find similar artists\n"
                            "4. Check if any of their songs are in my library using `get_library_stats`\n"
                            "5. Give me a curated listening plan: 'Start here → then this → then explore'\n"
                            f"6. Propose a '{artist} Deep Dive' playlist and ask me before creating it\n\n"
                            "I want to understand their artistry, not just list songs."
                        ),
                    ),
                )
            ],
        )

    else:
        raise ValueError(f"Unknown prompt: {name}")


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
async def main() -> None:
    log.info("Starting YouTube Music MCP Server v2.0.0")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
