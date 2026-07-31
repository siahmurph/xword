# Handoff: The Crossword Shelf

Local-network web app for two people to co-solve crosswords live. Built with
FastAPI + SQLite + vanilla JS, no build step, no auth. This doc is written for
picking the project back up in Claude Code.

## Stack
- **Backend**: FastAPI (`main.py`, single file), SQLite via stdlib `sqlite3` (no ORM)
- **Realtime**: raw WebSocket, one `/ws/{puzzle_id}` room per puzzle, in-memory
  connection list (`rooms: Dict[int, Room]` in `main.py`) — not persisted, resets
  on server restart, which is fine since fills are also written to SQLite on
  every message
- **Frontend**: static HTML/CSS/JS served straight out of `static/` via
  `StaticFiles(html=True)` mounted at `/` — no framework, no bundler
- **.puz parsing**: `puzpy` (`import puz`)
- **Web download**: shells out to the `xword-dl` CLI as a subprocess (does not
  use its Python API directly — deliberate, since the CLI is the documented/
  stable interface and its internal API has changed across versions before)

## Run it

**Docker (production deployment)**: `docker compose up -d --build`. Runs on
port **8099**, `restart: unless-stopped`. `Dockerfile` + `docker-compose.yml`
at repo root. DB and xword-dl auth token are bind-mounted (`./data`,
`./xword-dl-config`) so they survive rebuilds — see `README.md` for the full
walkthrough. Deployed target per `domain-info.md` is the Proxmox Docker LXC
(`192.168.1.130`), alongside NPM/Pi-hole/the *arr stack; nothing else on that
host uses port 8099. `main.py` reads `DB_PATH` from the environment (falls
back to `./crosswords.db` for bare `python -m uvicorn` runs); the Dockerfile
sets it to `/data/crosswords.db`.

**Bare-metal (dev)**:
```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8099
```
`crosswords.db` is created next to `main.py` on first run. `.gitignore` /
`.dockerignore` both exist and exclude it, plus `data/` and
`xword-dl-config/`.

**Not validated in-sandbox**: the Docker build was written and syntax-checked
here, but this sandbox's network egress blocks PyPI, so `docker compose up
--build` itself hasn't actually been run end-to-end. Run it once on the LXC
and confirm `/api/health` responds before considering this done.

## File map
```
main.py              all backend routes + DB init + numbering algorithm
static/index.html    library / "shelf" page: .puz upload, xword-dl download panel, catalog
static/builder.html  3-step manual puzzle builder (dims -> black squares -> clues)
static/solve.html    the actual solving grid + clue lists + WebSocket client
static/style.css     shared styles, "newsprint + ballpoint pen" theme, CSS vars at top
requirements.txt
Dockerfile            python:3.11-slim, installs deps, serves on :8099, has a HEALTHCHECK
docker-compose.yml    restart: unless-stopped, binds :8099, mounts ./data + ./xword-dl-config
.dockerignore / .gitignore
README.md            user-facing setup/usage doc (non-technical framing)
HANDOFF.md           this file
domain-info.md        home network / LXC / reverse-proxy reference (not app-specific)
```

## Data model (SQLite)
```sql
puzzles(
  id, title, author, width, height,
  black       -- JSON list[int]: flat cell indices that are black squares
  solution    -- JSON list[str]: flat, '' for black cells or unknown letters
  numbering   -- JSON list[int]: clue number per flat cell index, 0 if none
  clues       -- JSON {"across":[{"num","clue","cell"}], "down":[...]}
  has_solution -- 0/1, drives whether "Check letter" works
  created_at
)
solve_state(
  puzzle_id PRIMARY KEY,
  fill        -- JSON list[str]: current collaborative fill state, flat
  updated_at
)
```
All grids are flattened row-major: `cell_index = row * width + col`. This
convention is used everywhere (backend and both frontend pages) — don't mix in
a `[row][col]` 2D representation without converting.

## Key backend functions (`main.py`)
- `compute_numbering(width, height, black_set) -> list[int]` — standard
  crossword numbering algorithm, cell numbered if it starts an across and/or
  down entry. Reimplemented in JS in `builder.html` (`computeNumbering()`) for
  the live preview during puzzle building — **if you change the algorithm,
  change it in both places.**
- `build_clue_lists(...)` — turns numbering + a `{"A1": "clue text", "D3": ...}`
  map into the `{"across":[...], "down":[...]}` shape stored in `clues`
- `store_puz_bytes(data, fallback_title)` — the single ingestion point for any
  `.puz` bytes, used by both the file-upload endpoint and the xword-dl
  download endpoint. If you add another import source (e.g. `.ipuz`), give it
  its own parser that produces the same `(black, solution, numbering, clues)`
  shape and call `insert_puzzle(...)` — the actual DB-insert logic is
  factored out there (used by `store_puz_bytes` and the manual builder's
  `create_puzzle` route), so a third import path just needs to call it too

## API surface
| Route | Method | Purpose |
|---|---|---|
| `/api/health` | GET | `{"ok": true}` — used by the Docker `HEALTHCHECK` |
| `/api/puzzles` | GET | list library (metadata only) |
| `/api/puzzles/{id}` | GET | full puzzle incl. current `fill` |
| `/api/puzzles/{id}` | DELETE | remove from shelf |
| `/api/puzzles/{id}/reset` | POST | clear fill state |
| `/api/puzzles` | POST | manual builder save (see `builder.html` payload shape) |
| `/api/import-puz` | POST (multipart) | upload a `.puz` file |
| `/api/outlets` | GET | static list of xword-dl-supported outlets (`XWORD_DL_OUTLETS` in `main.py`) |
| `/api/download-puz` | POST | `{"source": "<outlet keyword or URL>", "date": "<optional>"}` — runs `xword-dl` as a subprocess |
| `/ws/{puzzle_id}` | WS | `{"type":"fill","cell":int,"letter":str}` in both directions; server persists then relays to all other connections in the room |

## Known gotchas / things future-me should know
- **`puzpy` version got silently downgraded** from 0.6.1 to 0.2.6 when
  `xword-dl` was installed (it pins an older `puzpy`). Both versions were
  tested against the app's usage (`puz.load`, `.clue_numbering()`) and behave
  identically for our purposes, but if you bump `xword-dl` and something
  breaks, check `pip show puzpy` first.
- **Live outlet downloads were never tested against the real internet** — the
  sandbox this was built in only allows a fixed domain allowlist, and none of
  the crossword outlets are on it. Everything downstream of "xword-dl produced
  a valid .puz" is tested (via a synthetic round-trip file); the CLI
  invocation itself, error-message plumbing, and the outlet keyword list are
  from xword-dl's docs and haven't been exercised against e.g. `uni` or `nyt`
  on live network. Test this first on real hardware.
- **NYT requires `xword-dl nyt --authenticate` once, in a real terminal**,
  before the `nyt`/`nytm`/`nytd` outlets will work from the app. There's no
  in-app flow for this (deliberately — it's an interactive username/password
  prompt, not something to build into a web form). Config lands in
  `~/.config/xword-dl/xword-dl.yaml` on whatever machine runs the server.
- **No auth on the app itself.** Fine for a home LAN; if this ever gets
  exposed outside it, add at minimum HTTP basic auth in front of it.
- **WebSocket sync is last-write-wins per cell**, no conflict resolution
  beyond that. Fine for two people; would need real thought if extended to
  more concurrent solvers.
- Grid rendering in `solve.html` and `builder.html` both hardcode `36px` cells
  — if you add a settings/zoom feature, that's the constant to parameterize.

## Suggested next steps (not started)
- `.ipuz` import (JSON-based alternative to `.puz`, some sites only offer this)
- Puzzle metadata edit (rename/re-tag after import)
- A "who's online" indicator in the solve view (the WebSocket room already
  knows connection count — just isn't surfaced to the client)
- Persist xword-dl's suggested filename/date metadata instead of falling back
  to the outlet keyword as `fallback_title` when `.puz` has no title set
