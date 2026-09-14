#!/usr/bin/env python3
"""Generate a PDF ranking of songs by all-time usage from usage_cache.json.

Reads only usage_cache.json (a dict of {year: {song title: play count}}),
sums each song's usage across all years, and writes output/AllTimeRanking.pdf
with the songs sorted most-used first. Columns: rank, title, total usage, and
a running total of usage for all songs at or above that rank.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

CACHE_PATH = Path(__file__).parent / "usage_cache.json"
OUTPUT_PATH = Path(__file__).parent / "output" / "AllTimeRanking.pdf"
PREPARED_BY = "Kent James"

PAGE_W, PAGE_H = letter
MARGIN = 0.6 * inch
TOP = PAGE_H - MARGIN
ROW_H = 14.5
FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_SIZE = 9.5
HEADER_SIZE = 12

RANK_X = MARGIN + 0.35 * inch
TITLE_X = MARGIN + 0.6 * inch
USAGE_X = PAGE_W - MARGIN - 1.1 * inch
RUNNING_X = PAGE_W - MARGIN
TITLE_MAX_WIDTH = USAGE_X - TITLE_X - 0.3 * inch


def load_song_totals() -> list[tuple[str, int]]:
    """Sum each song's usage across all years, sorted by total (desc)."""
    with CACHE_PATH.open() as f:
        yearly = json.load(f)

    totals: dict[str, int] = {}
    for counts in yearly.values():
        for title, count in counts.items():
            totals[title] = totals.get(title, 0) + count

    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def truncate_to_width(c, text, max_width):
    while text and c.stringWidth(text, FONT, FONT_SIZE) > max_width:
        text = text[:-1]
    return text


def draw_page_header(c, page_num, prepared_date):
    c.setFont(FONT, HEADER_SIZE)
    c.drawString(MARGIN, TOP, "Songs by All-Time Usage")
    c.drawCentredString(PAGE_W / 2, TOP,
                        f"Prepared by {PREPARED_BY} {prepared_date:%-m/%-d/%Y}")
    c.drawRightString(PAGE_W - MARGIN, TOP, f"Page {page_num}")


def draw_table_header(c, y):
    c.setFont(FONT_BOLD, FONT_SIZE)
    c.drawString(RANK_X, y, "#")
    c.drawString(TITLE_X, y, "Title")
    c.drawRightString(USAGE_X, y, "Usage")
    c.drawRightString(RUNNING_X, y, "Running Total")
    line_y = y - 3
    c.setLineWidth(1)
    c.line(MARGIN, line_y, PAGE_W - MARGIN, line_y)
    return line_y - ROW_H + 2


def draw_row(c, y, rank, title, usage, running_total):
    c.setFont(FONT, FONT_SIZE)
    c.drawString(RANK_X, y, str(rank))
    c.drawString(TITLE_X, y, truncate_to_width(c, title, TITLE_MAX_WIDTH))
    c.drawRightString(USAGE_X, y, str(usage))
    c.drawRightString(RUNNING_X, y, str(running_total))
    c.setLineWidth(0.25)
    c.setStrokeGray(0.75)
    c.line(MARGIN, y - 3.5, PAGE_W - MARGIN, y - 3.5)
    c.setStrokeGray(0)


def render_pdf(path: Path, ranked_songs: list[tuple[str, int]], prepared_date: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)

    page_num = 1
    draw_page_header(c, page_num, prepared_date)
    y = draw_table_header(c, TOP - 0.35 * inch)

    running_total = 0
    for rank, (title, usage) in enumerate(ranked_songs, start=1):
        running_total += usage
        if y < MARGIN:
            c.showPage()
            page_num += 1
            draw_page_header(c, page_num, prepared_date)
            y = draw_table_header(c, TOP - 0.35 * inch)
        draw_row(c, y, rank, title, usage, running_total)
        y -= ROW_H

    c.showPage()
    c.save()


def main():
    ranked_songs = load_song_totals()
    render_pdf(OUTPUT_PATH, ranked_songs, date.today())
    print(f"Wrote {OUTPUT_PATH} ({len(ranked_songs)} songs)")


if __name__ == "__main__":
    main()
