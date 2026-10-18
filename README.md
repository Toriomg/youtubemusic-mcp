# YouTube Music MCP Server

<div align="center">

A Model Context Protocol (MCP) server that connects YouTube Music to AI assistants like Claude,
with a focus on **recommending songs for your existing playlists**.
Implements Tools, Resources and Prompts.

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![MCP](https://img.shields.io/badge/MCP-1.x-purple.svg)
![Tests](https://img.shields.io/badge/tests-75%20passing-brightgreen.svg)
![CI](https://github.com/Toriomg/youtubemusic-mcp/actions/workflows/ci.yml/badge.svg)

[Recommendations](#recommendations) • [Tools](#tools) • [Installation](#installation) • [Authentication](#authentication) • [Usage](#usage) • [Safety](#safety) • [Troubleshooting](#troubleshooting)

</div>

---

## Recommendations

`recommend_for_playlist` looks at the artists already in a playlist (most frequent first) and
returns two separate lists:

- **`novedades`**: songs from those artists' releases in the last 12 months. Every track of a
  single, only the 2 most played tracks of an album or EP.
- **`hits_que_no_conoces`**: the artists' most popular songs that you don't know yet.

Every recommended song:

- is **unknown to you**: not in your library, liked songs, history, any of your playlists, or
  your local discard list (`descartes.json`). Music video and audio versions of the same song
  count as the same song;
- has the playlist's artist **credited** (collaborations count, unrelated artists on the same
  page don't);
- is **not a live, sped up, slowed, reverb, nightcore or clean / radio edit version**, unless you
  ask for them (`include_variants=true`). Remixes are kept;
- appears **only once**, picked round-robin across artists (12 + 12 by default).

`get_artist_new_releases` and `get_unheard_top_tracks` do the same for a single artist.

### Proposals cycle

Instead of adding recommendations straight to your playlist, they go to a private
`<playlist> - propuestas` playlist that you listen to on your phone:

```
1. recommend_for_playlist   → you check the list and say "yes"
2. fill_proposals_playlist  → empties and refills "<playlist> - propuestas" (private)
3. You listen and like the songs you want to keep
4. review_proposals         → liked songs go to the playlist, the rest to the discard list
5. apply_proposals_review   → after your "yes": adds the liked ones, records the discards,
                              empties the proposals playlist
```

Songs you already know can be discarded directly with `discard_songs`. Discarded songs are
never recommended again. The list lives only on your machine, in `descartes.json` (gitignored);
it's plain JSON, so you can edit it by hand to undo a discard.

---

## Tools

**22 Tools · 3 Resources · 3 Prompts.** ✏️ = writes to your YouTube Music account (see [Safety](#safety)).

### Recommendations

| Tool                        | Description                                                           |
| --------------------------- | --------------------------------------------------------------------- |
| `recommend_for_playlist`    | Novedades + unknown hits for a playlist's artists                     |
| `get_artist_new_releases`   | An artist's singles and albums from the last N months, with tracks    |
| `get_unheard_top_tracks`    | An artist's top songs minus the ones you already know                 |
| `fill_proposals_playlist` ✏️ | Empty and refill `<playlist> - propuestas` with the chosen songs      |
| `review_proposals`          | Split the proposals playlist into liked (add) and the rest (discard)  |
| `apply_proposals_review` ✏️  | Add the liked songs, record discards, empty the proposals playlist    |
| `discard_songs`             | Never recommend these songs again (local file only)                   |

### Library, search and discovery

| Tool                           | Description                                                      |
| ------------------------------ | ---------------------------------------------------------------- |
| `get_liked_songs_count`        | Total song count in your library                                 |
| `get_library_stats`            | Songs, artists, playlists + optional breakdown                   |
| `search_music`                 | Search songs, albums, artists, playlists or videos               |
| `get_top_artists`              | Your artists ranked by song count                                |
| `find_similar_songs`           | Similar tracks via YouTube Music's radio                         |
| `get_recommendations`          | Songs from your top 5 library artists                            |
| `list_playlists`               | Your playlists with IDs and song counts                          |
| `get_playlist_songs`           | Songs in a playlist                                              |
| `explore_moods`                | YouTube Music's Moods & Genres categories                        |
| `get_charts`                   | Global or per-country charts                                     |
| `get_listening_insights`       | Patterns in your listening history                               |
| `get_server_info`              | Auth method, cache state, version                                |
| `create_playlist_from_songs` ✏️ | Create a playlist from song search queries                       |
| `add_songs_to_playlist` ✏️      | Add songs to a playlist by search queries                        |
| `build_smart_playlist` ✏️       | Mood/genre playlist builder; writes only with `save_playlist=true` |

### Resources and prompts

| Resource              | Description                     |
| --------------------- | ------------------------------- |
| `library://songs`     | Full library as JSON            |
| `library://artists`   | Artist rankings                 |
| `library://playlists` | All playlists                   |

| Prompt                 | Description                              |
| ---------------------- | ---------------------------------------- |
| `weekly-discovery-mix` | Guided weekly discovery workflow         |
| `mood-based-playlist`  | Mood → playlist session                  |
| `artist-deep-dive`     | Artist exploration + listening plan      |

---

## Installation

Requires [uv](https://docs.astral.sh/uv/) and a YouTube Music account.

```bash
gh repo clone Toriomg/youtubemusic-mcp
cd youtubemusic-mcp
uv sync
```

---

## Authentication

### Option A: cookie file (simplest)

1. Open [music.youtube.com](https://music.youtube.com) and log in. If several Google accounts are
   signed in, a private window with only the right one avoids mix-ups.
2. Open the developer tools (`F12`) → **Network**, enable the **XHR** filter and reload.
3. Click a `browse` or `account_menu` request to `music.youtube.com` →
   **Headers** → **Request headers**. In Firefox, toggle **Raw** to see the whole line.
4. Copy the full value after `Cookie:` (it must contain `SAPISID=`).
5. Paste it, on a single line, into `cookie.txt` in the project directory.
6. Check it works:

```bash
uv run update_auth.py
```

The server regenerates `browser.json` (permissions `600`) from `cookie.txt` on every start.
Cookies last a few months; when they expire, paste fresh ones into `cookie.txt`.
If you used a private window, close it **without logging out**, or the cookie stops working.

#### Several Google accounts in the cookie

The cookie includes every account signed in to the browser, and the server uses the first one by
default. To use another one, set `YTMUSIC_AUTHUSER` to its position (0, 1, 2…):

```bash
YTMUSIC_AUTHUSER=1 uv run update_auth.py
```

### Option B: OAuth

See [docs/OAUTH_SETUP.md](docs/OAUTH_SETUP.md).

`cookie.txt`, `browser.json`, `oauth.json` and `descartes.json` are in `.gitignore`. Never commit them.

---

## Usage

### Claude Code

```bash
claude mcp add ytmusic -s user -- uv --directory /absolute/path/to/youtubemusic-mcp run server.py
# with a non-default account:
claude mcp add ytmusic -s user -e YTMUSIC_AUTHUSER=1 -- uv --directory /absolute/path/to/youtubemusic-mcp run server.py
```

Restart Claude Code, then just ask, for example:

- "List my YouTube Music playlists"
- "Recommend songs for my playlist X"
- "What has Quevedo released this year?"

### Claude Desktop

Add to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
Windows: `%APPDATA%\Claude\`, Linux: `~/.config/Claude/`):

```json
{
  "mcpServers": {
    "youtube-music": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/youtubemusic-mcp", "run", "server.py"],
      "env": { "YTMUSIC_AUTHUSER": "0" }
    }
  }
}
```

See [docs/CLAUDE_SETUP.md](docs/CLAUDE_SETUP.md) for details.

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector uv run server.py
```

---

## Safety

No tool marked ✏️ may run until the assistant has shown you the exact list of changes (playlist
name and every song) and you have said yes. Approval for one list doesn't cover the next. The rule
is in [AGENTS.md](AGENTS.md) and in each write tool's description, which is what the model reads.

All the recommendation tools only read. `discard_songs` only writes the local `descartes.json`.

---

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check server.py tests/
```

75 tests, fully mocked: no network or account needed. CI runs the same on Python 3.10–3.12.

---

## Troubleshooting

- **The library or playlists come back empty.** The cookie is probably using another Google
  account. Check `get_server_info`, or try `YTMUSIC_AUTHUSER=1`, `2`… (see above).
- **`HTTP 429: Too Many Requests`.** YouTube throttled the requests. Wait a few minutes; the
  server already limits itself to 3 parallel artist lookups.
- **Authentication errors after a while.** The cookie expired: paste a fresh one into `cookie.txt`
  ([docs/AUTH_UPDATE.md](docs/AUTH_UPDATE.md)).
- **The server isn't detected.** Use absolute paths and restart the client after changing its config.

---

## Credits

Fork of [codeRisshi25/youtubemusic-mcp](https://github.com/codeRisshi25/youtubemusic-mcp).
MIT License, see [LICENSE](LICENSE). Copyright (c) 2025 Risshi Raj Sen.

Built with [ytmusicapi](https://github.com/sigma67/ytmusicapi) and [MCP](https://modelcontextprotocol.io/).
