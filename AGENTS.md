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
- `fill_proposals_playlist` (empties and refills `<playlist> - propuestas`)
- `apply_proposals_review` (adds to the source playlist and empties the proposals playlist)

Everything else, including `recommend_for_playlist`, `review_proposals`,
`get_artist_new_releases` and `get_unheard_top_tracks`, is read-only.

## Proposals cycle

1. `recommend_for_playlist` → show the numbered list → on "yes", `fill_proposals_playlist`.
2. The user listens to `<playlist> - propuestas` and likes the songs they want.
3. On "revisa": `review_proposals` → show `to_add` and `to_discard`.
4. On "yes": `apply_proposals_review` with exactly those lists. Discards go to the local
   `descartes.json` (gitignored) and are never recommended again.

When the user says they already know (or don't want) some recommended songs, confirm the list and
call `discard_songs` with those song objects. It only writes `descartes.json`, not the account.

New tools that write must say so in their `description` and be added to this list.

## Secrets

`cookie.txt`, `browser.json`, `oauth.json`: never read them aloud, print them or commit them
(they are in `.gitignore`).
