# AGENTS.md

YouTube Music MCP server (`server.py`, ytmusicapi + mcp). Setup and tests use uv:

```bash
uv sync --extra dev
uv run pytest
```

## Rule: no writes to the account without confirmation

Never call a tool that modifies the user's YouTube Music account without first showing the
exact list of changes (playlist name plus every song) and getting an explicit "yes" in the
same conversation. Approval for one list does not cover the next one.

These tools write to the account:

- `create_playlist_from_songs`
- `add_songs_to_playlist`
- `build_smart_playlist` with `save_playlist=true` (with `false`, the default, it only reads)

Everything else, including `recommend_for_playlist`, `get_artist_new_releases` and
`get_unheard_top_tracks`, is read-only.

New tools that write must say so in their `description` and be added to this list.

## Secrets

`cookie.txt`, `browser.json`, `oauth.json`: never read them aloud, print them or commit them
(they are in `.gitignore`).
