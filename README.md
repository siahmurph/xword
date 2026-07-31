# The Crossword Shelf

A small local web app for solving crosswords together — one of you opens it on the PC, the other connects from any browser on the home network (phone, laptop, Apple TV browser if it has one, etc.), and you fill the same grid live.

## What it does
- **Library ("shelf")** of puzzles stored in a local SQLite file
- **Import `.puz` files** by drag-and-drop — this is the low-friction path. Any `.puz` (AcrossLite format) imports with clues, grid, and answer key intact, no manual work.
- **Build a puzzle by hand** from a PDF or scanned book page: pick grid size, click to mark black squares, type in the clues (and optionally the answers if you have an answer key). No OCR guessing — you're just transcribing what you see, quickly.
- **Solve together live**: both of you can type into the same grid from different devices at once, synced instantly over WebSocket. Letters you type show in blue ink; letters your partner just typed briefly flash red before settling.
- **Check a letter** against the stored answer key (only works for puzzles that have one — all `.puz` imports do).

## Running it (Docker — recommended)

The app is containerized and meant to run always-on, e.g. on the Proxmox
Docker LXC.

```bash
docker compose up -d --build
```

This builds the image, starts the container with `restart: unless-stopped`
(comes back up on host reboot / crash), and publishes it on **port 8099**.
Puzzle data lives in `./data/crosswords.db` on the host (bind-mounted into
the container), and any `xword-dl nyt --authenticate` login token persists
in `./xword-dl-config/` — both survive container rebuilds.

Open `http://<lxc-ip>:8099` (or wire up a reverse-proxy domain, e.g.
`xword.hankvilles.com` in Nginx Proxy Manager pointed at
`<lxc-ip>:8099`).

To authenticate NYT downloads once the container is running:
```bash
docker compose exec crossword-shelf xword-dl nyt --authenticate
```

## Running it (without Docker)

You need Python 3.9+.

```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8099
```

`--host 0.0.0.0` is what makes it reachable from other devices on your network, not just this machine.

Then:
- Locally: open `http://localhost:8099`
- From another device (same wifi): find this machine's local IP and open `http://192.168.1.x:8099`

Puzzles and progress are stored in `crosswords.db` next to `main.py` (or wherever `DB_PATH` points) — back that file up if you want to keep your library safe.

## Where to get `.puz` files
Many crossword sites and apps export `.puz` directly (Across Lite format). If a puzzle you buy or download only offers a PDF, use the "Build a Puzzle" flow instead — it's built for exactly that case.

### Download from the web (built in)
The shelf page has a "Download from the web" panel powered by [xword-dl](https://github.com/thisisparker/xword-dl), a well-maintained open-source tool that fetches the latest (or a dated) `.puz` directly from ~35 outlets — Universal, USA Today, Washington Post, LA Times, The Atlantic, Puzzmo, Guardian, and more. Pick an outlet, optionally give a date, and it lands straight on your shelf. You can also paste a direct URL for sites with an embedded solver that xword-dl supports.

**New York Times puzzles** need a one-time login, since they're subscriber-only. On the server machine (the PowerSpec PC), run once from a terminal:
```bash
xword-dl nyt --authenticate
```
It'll prompt for your NYT username/password and store a token in `~/.config/xword-dl/xword-dl.yaml`. After that, the `nyt`, `nytm`, and `nytd` outlets in the app will work normally.

If a download fails, the app shows you xword-dl's actual error message (e.g. no puzzle for that date, or a site's page structure changed) rather than swallowing it.

## Notes / things you may want to extend
- No auth — anyone on your LAN with the URL can open it. Fine for a home network; if you ever expose this beyond your LAN, add a password.
- The WebSocket sync is a simple relay + last-write-wins on each cell; no conflict resolution beyond that, which is fine for two people casually filling a grid together.
- `main.py` is a single file — the numbering/clue-list logic is shared between `.puz` import and the manual builder, so if you want to add another import format later (e.g. `.ipuz` JSON), you can reuse `compute_numbering()` and `build_clue_lists()`.
