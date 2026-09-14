#!/usr/bin/env python3
"""Render a weekly song-sets JSON file (as saved under song_sets/, see
CLAUDE.md's "Suggesting songs for upcoming weeks" section) as an HTML page and
a PDF, for handing a suggested lineup to someone who isn't reading JSON.

Usage:
    python render_song_sets.py song_sets/2026-09-14.json
    python render_song_sets.py                 # uses the most recently
                                                 # modified file in song_sets/

Input JSON shape:
    {
      "generated_date": "2026-09-14",
      "prepared_by": "Kent James",
      "sets": [
        {
          "date": "2026-09-20",
          "oldness_score": 73,
          "theme": null,
          "songs": [
            {"title": "...", "all_time": 3, "recent_6mo": 3, "keys": "G, Bb",
             "last_scheduled": "2026-04-19", "reason": "...", "stretch": false},
            ...
          ]
        },
        ...
      ]
    }

Writes output/SongSets-<generated_date>.pdf and _site/SongSets-<generated_date>.html.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from song_stats import OUT_DIR, SITE_DIR, STYLE_CSS

ROOT = Path(__file__).resolve().parent
SETS_DIR = ROOT / "song_sets"

PAGE_W, PAGE_H = letter
MARGIN = 0.75 * inch
FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_ITALIC = "Helvetica-Oblique"


def latest_set_file() -> Path:
    """The song_sets/*.json file for the most recent generated_date.

    Sorted by filename (YYYY-MM-DD.json) rather than filesystem mtime, since
    mtimes aren't reliable after a git clone/checkout.
    """
    files = sorted(SETS_DIR.glob("*.json")) if SETS_DIR.exists() else []
    if not files:
        sys.exit(
            f"Error: no *.json files found in {SETS_DIR}. Generate one first "
            f"(ask Claude to suggest songs for upcoming weeks, which saves a "
            f"song_sets/<date>.json per CLAUDE.md), or pass a JSON file path "
            f"directly: python render_song_sets.py <path>.json"
        )
    return files[-1]


def load_sets(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def song_detail_line(song: dict) -> str:
    """The secondary text for one song: counts, key(s), last played, reason."""
    parts = []
    if song.get("all_time") is not None:
        parts.append(f"all-time {song['all_time']}")
    if song.get("recent_6mo") is not None:
        parts.append(f"6mo {song['recent_6mo']}")
    if song.get("keys"):
        parts.append(f"key {song['keys']}")
    if song.get("last_scheduled"):
        parts.append(f"last played {song['last_scheduled']}")
    detail = ", ".join(parts)
    reason = song.get("reason", "")
    if detail and reason:
        return f"{detail} — {reason}"
    return detail or reason


# --- PDF rendering ------------------------------------------------------------

def render_pdf(path: Path, data: dict) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    prepared_by = data.get("prepared_by", "")
    generated_date = data.get("generated_date", "")

    def new_page():
        y = PAGE_H - MARGIN
        c.setFont(FONT_BOLD, 16)
        c.drawString(MARGIN, y, "Suggested Song Sets")
        c.setFont(FONT, 9)
        c.drawRightString(PAGE_W - MARGIN, y, f"Prepared by {prepared_by} {generated_date}")
        return y - 0.4 * inch

    y = new_page()

    for wk in data.get("sets", []):
        needed = 0.5 * inch + len(wk.get("songs", [])) * 0.35 * inch
        if y - needed < MARGIN:
            c.showPage()
            y = new_page()

        c.setFont(FONT_BOLD, 13)
        header = wk.get("date", "")
        c.drawString(MARGIN, y, header)
        c.setFont(FONT, 10)
        score = wk.get("oldness_score")
        if score is not None:
            c.drawRightString(PAGE_W - MARGIN, y, f"Oldness score: {score}")
        y -= 0.22 * inch
        if wk.get("theme"):
            c.setFont(FONT_ITALIC, 10)
            c.drawString(MARGIN, y, f"Theme: {wk['theme']}")
            y -= 0.22 * inch
        y -= 0.05 * inch

        for i, song in enumerate(wk.get("songs", []), start=1):
            if y < MARGIN + 0.4 * inch:
                c.showPage()
                y = new_page()
            title = song.get("title", "")
            if song.get("stretch"):
                title += "  (stretch pick)"
            c.setFont(FONT_BOLD, 10.5)
            c.drawString(MARGIN + 0.2 * inch, y, f"{i}. {title}")
            y -= 0.18 * inch
            detail = song_detail_line(song)
            if detail:
                c.setFont(FONT, 9)
                c.drawString(MARGIN + 0.4 * inch, y, detail)
                y -= 0.18 * inch
            y -= 0.06 * inch

        y -= 0.25 * inch

    c.showPage()
    c.save()


# --- HTML rendering ------------------------------------------------------------

SETS_STYLE = """\
.week { margin-bottom: 2rem; }
.week h2 { margin-bottom: 0.1rem; }
.week .score { color: #767676; font-size: 0.9rem; }
.week .theme { font-style: italic; color: #555; margin: 0.2rem 0 0.6rem; }
ol.songs { padding-left: 1.4rem; }
ol.songs li { margin-bottom: 0.5rem; }
.song-title { font-weight: 600; }
.stretch-tag {
  font-size: 0.75rem; font-weight: normal; color: #a15c00;
  border: 1px solid #a15c00; border-radius: 4px; padding: 0.05rem 0.35rem;
  margin-left: 0.4rem;
}
.song-detail { color: #555; font-size: 0.9rem; }
@media (prefers-color-scheme: dark) {
  .stretch-tag { color: #e0a94a; border-color: #e0a94a; }
}
"""


def render_html(path: Path, data: dict) -> None:
    prepared_by = data.get("prepared_by", "")
    generated_date = data.get("generated_date", "")

    weeks_html = []
    for wk in data.get("sets", []):
        songs_html = []
        for song in wk.get("songs", []):
            stretch = (' <span class="stretch-tag">stretch pick</span>'
                      if song.get("stretch") else "")
            detail = song_detail_line(song)
            detail_html = f'<div class="song-detail">{html.escape(detail)}</div>' if detail else ""
            songs_html.append(f"""\
<li><span class="song-title">{html.escape(song.get("title", ""))}</span>{stretch}
{detail_html}</li>""")

        score = wk.get("oldness_score")
        score_html = (f'<p class="score">Oldness score: {score}</p>'
                     if score is not None else "")
        theme_html = (f'<p class="theme">Theme: {html.escape(wk["theme"])}</p>'
                     if wk.get("theme") else "")

        weeks_html.append(f"""\
<section class="week">
<h2>{html.escape(wk.get("date", ""))}</h2>
{score_html}
{theme_html}
<ol class="songs">
{"".join(songs_html)}
</ol>
</section>""")

    path.write_text(f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Suggested Song Sets</title>
<link rel="stylesheet" href="style.css">
<style>
{SETS_STYLE}
</style>
</head>
<body>
<header>
<h1>Suggested Song Sets</h1>
<p class="meta">Prepared by {html.escape(prepared_by)} &middot; {html.escape(generated_date)}
&middot; <a href="index.html">Home</a></p>
</header>
{"".join(weeks_html)}
</body>
</html>
""", encoding="utf-8")


def main():
    if len(sys.argv) > 1:
        json_path = Path(sys.argv[1])
    else:
        json_path = latest_set_file()
    if not json_path.exists():
        sys.exit(f"Error: {json_path} not found.")

    data = load_sets(json_path)
    stem = f"SongSets-{data.get('generated_date', json_path.stem)}"

    OUT_DIR.mkdir(exist_ok=True)
    SITE_DIR.mkdir(exist_ok=True)
    if not (SITE_DIR / "style.css").exists():
        (SITE_DIR / "style.css").write_text(STYLE_CSS, encoding="utf-8")

    pdf_path = OUT_DIR / f"{stem}.pdf"
    html_path = SITE_DIR / f"{stem}.html"
    render_pdf(pdf_path, data)
    render_html(html_path, data)

    n_weeks = len(data.get("sets", []))
    print(f"Read {json_path}")
    print(f"Wrote {n_weeks} weeks to {pdf_path} and {html_path}")


if __name__ == "__main__":
    main()
