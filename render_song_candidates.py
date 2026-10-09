#!/usr/bin/env python3
"""Render a single-week song candidate JSON file as an HTML page."""

from __future__ import annotations

import html
import json
import shutil
import sys
from pathlib import Path

from song_stats import SITE_DIR, load_catalog, song_url

ROOT = Path(__file__).resolve().parent
CANDIDATE_DIR = ROOT / "song_candidates"


def latest_candidate_file() -> Path | None:
    files = sorted(CANDIDATE_DIR.glob("*.json")) if CANDIDATE_DIR.exists() else []
    return files[-1] if files else None


def load_song_ids() -> dict[str, int]:
    ids = {}
    for catalog in (load_catalog(), load_catalog(christmas=True)):
        ids.update({title: info["id"] for title, info in catalog.items()
                    if info["id"] is not None})
    return ids


def render_html(path: Path, data: dict) -> None:
    ids = load_song_ids()

    def render_songs(songs: list[dict]) -> str:
        items = []
        for song in songs:
            title = song.get("title", "")
            escaped_title = html.escape(title)
            url = song_url(ids.get(title))
            if url:
                escaped_title = f'<a href="{html.escape(url)}">{escaped_title}</a>'
            details = [
                f"All-time: {song.get('all_time', '')}",
                f"Recent 6 months: {song.get('recent_6mo', '')}",
            ]
            if song.get("keys"):
                details.append(f"Keys: {song['keys']}")
            if song.get("last_scheduled"):
                details.append(f"Last scheduled: {song['last_scheduled']}")
            reason = html.escape(song.get("reason", ""))
            reason_html = f"; {reason}" if reason else ""
            items.append(
                f"<li><strong>{escaped_title}</strong> "
                f"<span class=\"song-detail\">"
                f"{html.escape(' | '.join(details))}{reason_html}</span></li>"
            )
        return "\n".join(items)

    proposed = render_songs(data.get("proposed_songs", []))
    alternatives = render_songs(data.get("alternatives", []))
    alternatives_html = (
        f"<section><h2>Alternatives</h2><ol>{alternatives}</ol></section>"
        if alternatives else ""
    )
    score = data.get("oldness_score")
    score_html = f"<p>Oldness score: {score}</p>" if score is not None else ""
    generated_date = html.escape(data.get("generated_date", ""))
    service_date = html.escape(data.get("service_date", ""))

    path.write_text(f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Next Week Song Suggestions</title>
<link rel="stylesheet" href="style.css">
<link rel="icon" href="favicon.ico">
<style>
section {{ margin-bottom: 2rem; }}
li {{ margin-bottom: 0.6rem; }}
.song-detail {{ color: #555; font-size: 0.9rem; }}
</style>
</head>
<body>
<header>
<h1>Next Week Song Suggestions</h1>
<p class="meta">Service date {service_date} &middot; Generated {generated_date}
&middot; <a href="index.html">Home</a></p>
</header>
<section>
<h2>Proposed Lineup</h2>
{score_html}
<ol>{proposed}</ol>
</section>
{alternatives_html}
</body>
</html>
""", encoding="utf-8")


def summary(data: dict, path: Path) -> dict:
    return {
        "name": "SongCandidates",
        "generated_date": data.get("generated_date", ""),
        "service_date": data.get("service_date", ""),
    }


def main() -> None:
    latest_path = latest_candidate_file()
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_path
    if path is None:
        sys.exit(f"Error: no candidate JSON files found in {CANDIDATE_DIR}.")
    if not path.exists():
        sys.exit(f"Error: {path} not found.")

    data = json.loads(path.read_text(encoding="utf-8"))
    SITE_DIR.mkdir(exist_ok=True)
    if not (SITE_DIR / "style.css").exists():
        from song_stats import STYLE_CSS
        (SITE_DIR / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    if not (SITE_DIR / "favicon.ico").exists():
        from song_stats import ICON_FILE
        shutil.copyfile(ICON_FILE, SITE_DIR / "favicon.ico")

    output = SITE_DIR / f"{summary(data, path)['name']}.html"
    render_html(output, data)
    print(f"Read {path}")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()