import json
import sqlite3
import io
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Dict, List

import puz
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from numbering import compute_numbering
from pdf_import import PdfImportError, resolve_pdf_bytes, extract_puzzle_from_pdf

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "crosswords.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS puzzles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            author TEXT,
            width INTEGER,
            height INTEGER,
            black TEXT,      -- JSON list of black cell indices
            solution TEXT,   -- JSON list of letters, '' for black/unknown cells
            numbering TEXT,  -- JSON list of clue number per cell (0 if none)
            clues TEXT,      -- JSON {"across": [{"num":1,"clue":"..."}], "down": [...]}
            has_solution INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS solve_state (
            puzzle_id INTEGER PRIMARY KEY,
            fill TEXT,        -- JSON list of current filled letters
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


init_db()


def build_clue_lists(width, height, black, numbering, clue_text_map):
    """clue_text_map: {"A1": "clue", "D3": "clue", ...} -> across/down lists"""
    across, down = [], []
    for r in range(height):
        for c in range(width):
            i = r * width + c
            if i in black or numbering[i] == 0:
                continue
            num = numbering[i]
            starts_across = (c == 0 or (r * width + c - 1) in black) and (
                c + 1 < width and (i + 1) not in black
            )
            starts_down = (r == 0 or ((r - 1) * width + c) in black) and (
                r + 1 < height and (i + width) not in black
            )
            if starts_across:
                across.append({"num": num, "clue": clue_text_map.get(f"A{num}", ""), "cell": i})
            if starts_down:
                down.append({"num": num, "clue": clue_text_map.get(f"D{num}", ""), "cell": i})
    return across, down


# ---------- API: health check (used by Docker HEALTHCHECK) ----------
@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- API: list / get puzzles ----------
@app.get("/api/puzzles")
def list_puzzles():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, author, width, height, has_solution, created_at FROM puzzles ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/puzzles/{puzzle_id}")
def get_puzzle(puzzle_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM puzzles WHERE id=?", (puzzle_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Puzzle not found")
    state = conn.execute("SELECT fill FROM solve_state WHERE puzzle_id=?", (puzzle_id,)).fetchone()
    conn.close()
    d = dict(row)
    d["black"] = json.loads(d["black"])
    d["solution"] = json.loads(d["solution"])
    d["numbering"] = json.loads(d["numbering"])
    d["clues"] = json.loads(d["clues"])
    d["fill"] = json.loads(state["fill"]) if state else [""] * (d["width"] * d["height"])
    return d


@app.delete("/api/puzzles/{puzzle_id}")
def delete_puzzle(puzzle_id: int):
    conn = get_db()
    conn.execute("DELETE FROM puzzles WHERE id=?", (puzzle_id,))
    conn.execute("DELETE FROM solve_state WHERE puzzle_id=?", (puzzle_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- shared: insert a fully-prepared puzzle into the library ----------
def insert_puzzle(title, author, width, height, black, solution, numbering, across, down, has_solution) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO puzzles (title, author, width, height, black, solution, numbering, clues, has_solution) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            title,
            author,
            width,
            height,
            json.dumps(sorted(black)),
            json.dumps(solution),
            json.dumps(numbering),
            json.dumps({"across": across, "down": down}),
            has_solution,
        ),
    )
    puzzle_id = cur.lastrowid
    conn.execute(
        "INSERT INTO solve_state (puzzle_id, fill) VALUES (?, ?)",
        (puzzle_id, json.dumps([""] * (width * height))),
    )
    conn.commit()
    conn.close()
    return puzzle_id


# ---------- shared: store a parsed .puz into the library ----------
def store_puz_bytes(data: bytes, fallback_title: str) -> int:
    try:
        p = puz.load(data)
    except Exception as e:
        raise HTTPException(400, f"Could not parse .puz file: {e}")

    width, height = p.width, p.height
    black = set(i for i, ch in enumerate(p.fill) if ch == ".")
    solution = [("" if i in black else ch) for i, ch in enumerate(p.solution)]
    numbering = compute_numbering(width, height, black)

    numbering_obj = p.clue_numbering()
    clue_text_map = {}
    for cl in numbering_obj.across:
        clue_text_map[f"A{cl['num']}"] = cl["clue"]
    for cl in numbering_obj.down:
        clue_text_map[f"D{cl['num']}"] = cl["clue"]

    across, down = build_clue_lists(width, height, black, numbering, clue_text_map)

    return insert_puzzle(
        p.title or fallback_title, p.author or "", width, height,
        black, solution, numbering, across, down, has_solution=1,
    )


# ---------- API: import .puz (file upload) ----------
@app.post("/api/import-puz")
async def import_puz(file: UploadFile = File(...)):
    data = await file.read()
    puzzle_id = store_puz_bytes(data, file.filename)
    return {"id": puzzle_id}


# ---------- API: outlets supported by xword-dl ----------
XWORD_DL_OUTLETS = [
    {"keyword": "atl", "name": "The Atlantic", "date": True},
    {"keyword": "bill", "name": "Billboard", "date": False},
    {"keyword": "club", "name": "Crossword Club", "date": True},
    {"keyword": "db", "name": "The Daily Beast", "date": False},
    {"keyword": "pop", "name": "Daily Pop", "date": True},
    {"keyword": "std", "name": "Der Standard", "date": False},
    {"keyword": "grdc", "name": "Guardian Cryptic", "date": False},
    {"keyword": "grde", "name": "Guardian Everyman", "date": False},
    {"keyword": "grdp", "name": "Guardian Prize", "date": False},
    {"keyword": "grdq", "name": "Guardian Quick", "date": False},
    {"keyword": "grdu", "name": "Guardian Quiptic", "date": False},
    {"keyword": "grds", "name": "Guardian Speedy", "date": False},
    {"keyword": "grdw", "name": "Guardian Weekend", "date": False},
    {"keyword": "lat", "name": "Los Angeles Times", "date": True},
    {"keyword": "latm", "name": "LA Times Mini", "date": True},
    {"keyword": "nyt", "name": "New York Times (subscriber login required)", "date": True},
    {"keyword": "nytm", "name": "NYT Mini (subscriber login required)", "date": True},
    {"keyword": "nytd", "name": "NYT Midi (subscriber login required)", "date": True},
    {"keyword": "tny", "name": "The New Yorker", "date": True},
    {"keyword": "tnym", "name": "The New Yorker Mini", "date": True},
    {"keyword": "nd", "name": "Newsday", "date": True},
    {"keyword": "ever", "name": "Observer Everyman", "date": False},
    {"keyword": "spdy", "name": "Observer Speedy", "date": False},
    {"keyword": "prince", "name": "Daily Princetonian", "date": True},
    {"keyword": "prince-mini", "name": "Daily Princetonian Mini", "date": True},
    {"keyword": "pzm", "name": "Puzzmo", "date": True},
    {"keyword": "pzmb", "name": "Puzzmo Big", "date": True},
    {"keyword": "sdp", "name": "Simply Daily Puzzles", "date": True},
    {"keyword": "sdpc", "name": "Simply Daily Puzzles Cryptic", "date": True},
    {"keyword": "sdpq", "name": "Simply Daily Puzzles Quick", "date": True},
    {"keyword": "uni", "name": "Universal", "date": True},
    {"keyword": "usa", "name": "USA Today", "date": True},
    {"keyword": "vox", "name": "Vox", "date": False},
    {"keyword": "vult", "name": "Vulture 10x10", "date": True},
    {"keyword": "wal", "name": "The Walrus", "date": False},
    {"keyword": "wp", "name": "Washington Post", "date": True},
]


@app.get("/api/outlets")
def list_outlets():
    return XWORD_DL_OUTLETS


# ---------- API: fetch a puzzle from the web via xword-dl ----------
@app.post("/api/download-puz")
def download_puz(payload: Dict = Body(...)):
    """
    payload: {"source": "nyt" | "https://..." , "date": "9/22/25" (optional)}
    `source` is either an outlet keyword or a full URL to an embedded solver page,
    matching what xword-dl accepts on the command line.
    """
    source = (payload.get("source") or "").strip()
    date = (payload.get("date") or "").strip()
    if not source:
        raise HTTPException(400, "Provide an outlet keyword or a puzzle URL.")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = os.path.join(tmpdir, "download.puz")
        cmd = ["xword-dl", source, "-o", out_path]
        if date:
            cmd += ["--date", date]
        try:
            result = subprocess.run(
                cmd, cwd=tmpdir, capture_output=True, text=True, timeout=60
            )
        except FileNotFoundError:
            raise HTTPException(
                500,
                "xword-dl isn't installed on this server. Run: pip install xword-dl",
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "xword-dl timed out trying to reach the outlet.")

        if result.returncode != 0 or not os.path.exists(out_path):
            msg = (result.stderr or result.stdout or "Unknown error").strip()
            msg = msg.splitlines()[-1] if msg else "Download failed"
            raise HTTPException(400, f"xword-dl couldn't fetch that puzzle: {msg}")

        with open(out_path, "rb") as f:
            data = f.read()

    puzzle_id = store_puz_bytes(data, source)
    return {"id": puzzle_id}


# ---------- API: manual builder save ----------
# ---------- API: scrape a puzzle link into a builder draft (no OCR) ----------
@app.post("/api/scrape-puzzle")
def scrape_puzzle(payload: Dict = Body(...)):
    """
    payload: {"url": "https://..."} — either a direct PDF link or a page
    that links to one. Returns a draft {title, width, height, black, clues}
    for the manual builder to pre-fill; nothing is saved to the library yet.
    Only works for PDFs with a real vector-drawn grid + text layer (i.e.
    not scanned/photographed pages) — raises a clear error otherwise.
    """
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Provide a puzzle URL.")
    try:
        pdf_bytes = resolve_pdf_bytes(url)
        draft = extract_puzzle_from_pdf(pdf_bytes)
    except PdfImportError as e:
        raise HTTPException(400, str(e))
    return draft


@app.post("/api/puzzles")
def create_puzzle(payload: Dict = Body(...)):
    title = payload.get("title", "Untitled")
    author = payload.get("author", "")
    width = payload["width"]
    height = payload["height"]
    black = set(payload["black"])
    solution_in = payload.get("solution", [""] * (width * height))
    clue_text_map = payload.get("clues", {})  # {"A1": "text", "D3": "text"}

    numbering = compute_numbering(width, height, black)
    across, down = build_clue_lists(width, height, black, numbering, clue_text_map)

    has_solution = 1 if any(ch for i, ch in enumerate(solution_in) if i not in black) else 0
    solution = [("" if i in black else (solution_in[i] or "")) for i in range(width * height)]

    puzzle_id = insert_puzzle(
        title, author, width, height,
        black, solution, numbering, across, down, has_solution,
    )
    return {"id": puzzle_id}


@app.post("/api/puzzles/{puzzle_id}/reset")
def reset_puzzle(puzzle_id: int):
    conn = get_db()
    row = conn.execute("SELECT width, height FROM puzzles WHERE id=?", (puzzle_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Puzzle not found")
    conn.execute(
        "UPDATE solve_state SET fill=?, updated_at=CURRENT_TIMESTAMP WHERE puzzle_id=?",
        (json.dumps([""] * (row["width"] * row["height"])), puzzle_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- WebSocket: live collaborative fill ----------
class Room:
    def __init__(self):
        self.connections: List[WebSocket] = []


rooms: Dict[int, Room] = {}


@app.websocket("/ws/{puzzle_id}")
async def ws_endpoint(websocket: WebSocket, puzzle_id: int):
    await websocket.accept()
    room = rooms.setdefault(puzzle_id, Room())
    room.connections.append(websocket)
    try:
        while True:
            msg = await websocket.receive_json()
            # expected: {"type": "fill", "cell": int, "letter": "A"} or {"type":"cursor", ...}
            if msg.get("type") == "fill":
                cell = msg["cell"]
                letter = msg.get("letter", "")
                conn = get_db()
                row = conn.execute("SELECT fill FROM solve_state WHERE puzzle_id=?", (puzzle_id,)).fetchone()
                fill = json.loads(row["fill"]) if row else []
                if cell < len(fill):
                    fill[cell] = letter
                conn.execute(
                    "UPDATE solve_state SET fill=?, updated_at=CURRENT_TIMESTAMP WHERE puzzle_id=?",
                    (json.dumps(fill), puzzle_id),
                )
                conn.commit()
                conn.close()
            # broadcast to everyone (including sender, for simple ack) except do a simple relay
            dead = []
            for ws in room.connections:
                if ws is websocket:
                    continue
                try:
                    await ws.send_json(msg)
                except Exception:
                    dead.append(ws)
            for d in dead:
                room.connections.remove(d)
    except WebSocketDisconnect:
        if websocket in room.connections:
            room.connections.remove(websocket)


# ---------- static frontend ----------
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
