"""
Import a crossword grid + clues from a PDF, without OCR.

Many crossword PDFs (anything exported from a construction tool rather than
photographed from a book) are vector documents: the grid is drawn as a set
of unit-square rectangles (filled black = black square) and the clue text
is a real, selectable text layer. Both can be read exactly via PyMuPDF —
no image rendering, no OpenCV, no Tesseract, and no risk of OCR misreads
in the grid layout, which is the part that's hardest to get right from a
rasterized image.

If a given PDF isn't built this way (e.g. it's a scan of a book page),
extract_puzzle_from_pdf raises PdfImportError with a message suitable to
show the user, who can fall back to the manual builder.
"""
import re
import statistics
from urllib.parse import urljoin

import fitz
import requests

from numbering import compute_numbering, numbering_directions

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class PdfImportError(Exception):
    pass


def _fetch(url: str, referer: str = None) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        resp = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException as e:
        raise PdfImportError(f"Couldn't reach {url}: {e}")
    if resp.status_code != 200:
        raise PdfImportError(f"{url} returned HTTP {resp.status_code}")
    return resp


def resolve_pdf_bytes(url: str) -> bytes:
    """Accepts either a direct PDF link or a page that links to one."""
    resp = _fetch(url)
    content_type = resp.headers.get("content-type", "")
    if "pdf" in content_type.lower() or url.lower().endswith(".pdf"):
        return resp.content

    html = resp.text
    match = re.search(r'href="([^"]+\.pdf)"', html, re.IGNORECASE)
    if not match:
        raise PdfImportError(
            "Couldn't find a link to a PDF on that page. "
            "Paste the direct PDF link instead."
        )
    pdf_url = urljoin(url, match.group(1))
    pdf_resp = _fetch(pdf_url, referer=url)
    return pdf_resp.content


def _is_black_fill(fill) -> bool:
    return fill is not None and sum(fill[:3]) / 3 < 0.3


def _is_grid_border_candidate(d) -> bool:
    return d.get("fill") is None and d.get("color") is not None


def extract_puzzle_from_pdf(pdf_bytes: bytes) -> dict:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise PdfImportError(f"Couldn't open that as a PDF: {e}")

    page = doc[0]
    drawings = page.get_drawings()

    black_rects = [
        d for d in drawings
        if _is_black_fill(d.get("fill")) and d["rect"].width < 40 and d["rect"].height < 40
    ]
    if len(black_rects) < 5:
        raise PdfImportError(
            "Couldn't find a vector-drawn grid in this PDF (no black squares "
            "detected). This importer only handles PDFs with a real vector "
            "grid, not scanned/photographed pages — use the manual builder "
            "for this one."
        )

    cell_w = statistics.median(d["rect"].width for d in black_rects)
    cell_h = statistics.median(d["rect"].height for d in black_rects)

    border_candidates = [d for d in drawings if _is_grid_border_candidate(d)]
    if not border_candidates:
        raise PdfImportError("Couldn't find the grid's outer border in this PDF.")

    # Multiple rects can look plausible by aspect ratio alone (a page's inner
    # content box, say). The one actually worth trusting is whichever one,
    # used as the grid origin, correctly places every black square we found
    # in-bounds — that's checked against real data instead of guessed at.
    def black_squares_for(bbox):
        w = max(1, round(bbox.width / cell_w))
        h = max(1, round(bbox.height / cell_h))
        cells = set()
        for d in black_rects:
            r = d["rect"]
            col = round((r.x0 - bbox.x0) / cell_w)
            row = round((r.y0 - bbox.y0) / cell_h)
            if 0 <= row < h and 0 <= col < w:
                cells.add(row * w + col)
        return w, h, cells

    best = max(border_candidates, key=lambda d: len(black_squares_for(d["rect"])[2]))
    width, height, black = black_squares_for(best["rect"])
    if width > 30 or height > 30:
        raise PdfImportError(
            f"Detected an implausible grid size ({width}x{height}) — "
            "the PDF's layout doesn't match what this importer expects."
        )
    if len(black) < len(black_rects):
        raise PdfImportError(
            f"Only matched {len(black)} of {len(black_rects)} black squares to "
            "the detected grid — this PDF's layout isn't a clean match for this "
            "importer. Use the manual builder for this one."
        )

    numbering = compute_numbering(width, height, black)
    across_nums, down_nums = numbering_directions(width, height, black, numbering)

    # Each grid cell's number is drawn as real embedded text too, not just
    # part of the vector grid — get_text() over the whole page would mix
    # those bare in-cell labels (e.g. a lone "2") in with the actual clue
    # list, corrupting the scan. Clue columns can sit beside the grid as
    # well as below it, so rather than clipping to one region, drop only
    # the individual words that fall inside the grid's own rectangle and
    # reconstruct lines from what's left, in original reading order.
    grid_rect = best["rect"]
    words = [w for w in page.get_text("words") if not grid_rect.contains(fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))]
    words.sort(key=lambda w: (w[5], w[6], w[7]))  # block_no, line_no, word_no
    clue_text_lines = []
    last_line_key = None
    for w in words:
        line_key = (w[5], w[6])
        if line_key != last_line_key:
            clue_text_lines.append(w[4])
            last_line_key = line_key
        else:
            clue_text_lines[-1] += " " + w[4]
    clues = _parse_clue_text("\n".join(clue_text_lines), across_nums, down_nums)
    title = (doc.metadata or {}).get("title") or ""

    return {"title": title, "width": width, "height": height, "black": sorted(black), "clues": clues}


_CLUE_START = re.compile(r"^(\d+)[.\)]?\s*(.*)$")


def _parse_clue_text(text: str, across_nums: set, down_nums: set) -> dict:
    """
    Rather than trying to reconstruct which text column is "across" vs.
    "down" — real crossword PDFs commonly lay clues out in several
    side-by-side columns, and PyMuPDF's text-extraction order doesn't
    reliably follow visual reading order across them — this uses the grid
    geometry (already known exactly, no OCR involved) to tell us which
    clue numbers are across-only, down-only, or both. Most numbers are
    single-direction, so a flat, order-independent scan for numbered lines
    unambiguously resolves them regardless of which column they came from.
    Only numbers that start both an across and a down entry are genuinely
    ambiguous from text alone; those get best-effort handling below.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    occurrences = {}  # number -> [text, text, ...] in page order
    current_num = None
    for line in lines:
        m = _CLUE_START.match(line)
        if m and int(m.group(1)) in (across_nums | down_nums):
            current_num = int(m.group(1))
            occurrences.setdefault(current_num, []).append(m.group(2))
        elif current_num is not None and not m:
            occurrences[current_num][-1] = (occurrences[current_num][-1] + " " + line).strip()
        else:
            current_num = None

    clues = {}
    for num in across_nums:
        vals = occurrences.get(num)
        if vals:
            clues[f"A{num}"] = vals[0]
    for num in down_nums:
        vals = occurrences.get(num)
        if not vals:
            continue
        # A number starting both directions needs its second text-layer
        # occurrence for "down" if we found two; otherwise fall back to
        # reusing the same text so the field isn't just left blank — the
        # user reviews this step before saving either way.
        clues[f"D{num}"] = vals[1] if len(vals) > 1 else vals[0]
    return clues
