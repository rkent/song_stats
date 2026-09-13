#!/usr/bin/env python3
"""Generate song-usage statistics PDFs from Planning Center data.

Reads everything from the Planning Center Services API (no data files) and
produces these PDFs in output/:

  * ByTitle.pdf       - every (non-Christmas) song, sorted alphabetically
  * ByAllTime.pdf     - the same songs, sorted by all-time play count (desc)
  * ByRecentUsage.pdf - only songs played in the two most recent years, with
                        per-year columns and a combined "Recent" total
  * RecentMonths.pdf  - songs from recent plans, with a column per month
                        and a combined "Total"
  * NeverPlayed.pdf   - catalog songs with no recorded plays
  * Christmas.pdf     - the Christmas songs excluded from the other reports
  * NotPlayed4Years.pdf - songs not scheduled in the last STALE_YEARS years

Most rows show the all-time play count, period columns, and the date the song
was last scheduled.

API data used:
  * /services/v2/songs ... the song catalog (titles, themes, last scheduled
                                        date). Requires CLIENT_ID and
                                        CLIENT_SECRET, a Planning Center
                                        personal access token
                                        (https://api.planningcenteronline.com/oauth/applications),
                                        set in the environment or a .env file.
  * /service_types/.../plans and .../items ... the Sunday Morning Worship
                                        Services plans and their song items,
                                        used for:
                                          - the current year's Plans counts,
                                            and the YEARLY_COLUMNS - 1
                                            preceding years' (completed past
                                            years are cached in
                                            usage_cache.json, since that data
                                            never changes; only the current
                                            year is fetched live);
                                          - the all-time total, which sums
                                            every year's Plans counts from
                                            EARLIEST_YEAR through the current
                                            year;
                                          - the Recent Months report, for the
                                            current month plus the
                                            RECENT_MONTHS_COUNT - 1 preceding
                                            full months.

Songs are matched between the catalog and the usage data by TITLE only.
Christmas songs are excluded, and duplicate titles in the catalog are collapsed
into a single row.
"""

from __future__ import annotations

import functools
import html
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

load_dotenv()

# --- Configuration ----------------------------------------------------------

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
SITE_DIR = ROOT / "_site"

PREPARED_BY = "Kent James"

PCO_API_BASE = "https://api.planningcenteronline.com/services/v2"
# The Planning Center service type whose plans feed the Recent Months report.
SERVICE_TYPE_NAME = "Sunday Morning Worship Services"
# Recent Months covers the current (partial) month plus this many preceding
# full calendar months.
RECENT_MONTHS_COUNT = 6
# Number of trailing years (including the current one) shown as columns in
# ByTitle/ByAllTime/etc.
YEARLY_COLUMNS = 5
# First year with Sunday Morning Worship Services plans in Planning Center;
# the all-time total sums every year's API usage from here through the
# current year.
EARLIEST_YEAR = 2014
# Completed past years' usage never changes, so it's cached here instead of
# being refetched from the API on every run.
USAGE_CACHE_FILE = ROOT / "usage_cache.json"


# Christmas-specific themes that, alongside a "Christmas" tag, confirm a song
# really is a Christmas song (as opposed to a worship song with a stray tag).
CHRISTMAS_SECONDARY = {
    "seasonal", "advent", "birth", "manger", "incarnation", "epiphany", "noel",
    "nativity", "bethlehem", "emmanuel", "magi", "wise men", "shepherd", "angels",
}
# A "Christmas"-tagged song with more themes than this, and no secondary
# Christmas theme, is treated as a worship song that merely carries a stray tag.
MAX_UNRELATED_THEMES = 9


def is_christmas(themes: str) -> bool:
    """Return True if a song's Themes field marks it as a Christmas song.

    The catalog tags songs with a comma-separated Themes list. Some non-Christmas
    worship songs carry a stray "Christmas" tag buried in a long, unrelated theme
    list (e.g. "Hosanna (Praise Is Rising)", "Revelation Song"). To spare those, a
    song counts as Christmas only when it has the "Christmas" tag AND either has a
    short theme list (<= MAX_UNRELATED_THEMES) or also carries a secondary
    Christmas-specific theme. This correctly classifies all tagged songs in the
    current catalog.
    """
    tags = [t.strip() for t in themes.split(",") if t.strip()]
    if not any(t.lower() == "christmas" for t in tags):
        return False
    if len(tags) <= MAX_UNRELATED_THEMES:
        return True
    return any(t.lower() in CHRISTMAS_SECONDARY for t in tags)


# --- Loading -----------------------------------------------------------------

def parse_scheduled_datetime(value: str | None) -> date | None:
    """Parse a last_scheduled_at value like '2025-04-13T12:00:00Z' into a date."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


@functools.lru_cache(maxsize=1)
def pco_auth() -> tuple[str, str]:
    """Return the (CLIENT_ID, CLIENT_SECRET) HTTP Basic Auth credentials.

    These are a Planning Center personal access token
    (https://api.planningcenteronline.com/oauth/applications), set in the
    environment or a .env file.
    """
    app_id = os.environ.get("CLIENT_ID")
    secret = os.environ.get("CLIENT_SECRET")
    if not app_id or not secret:
        sys.exit(
            "Error: CLIENT_ID and CLIENT_SECRET must be set (in the environment or "
            "a .env file) to a Planning Center personal access token. Create one "
            "at https://api.planningcenteronline.com/oauth/applications."
        )
    return app_id, secret


def fetch_all_pages(url: str, params: dict) -> list[dict]:
    """GET url and follow JSON:API "next" links, returning all data entries."""
    results = []
    while url:
        resp = requests.get(url, params=params, auth=pco_auth())
        resp.raise_for_status()
        payload = resp.json()
        results.extend(payload["data"])
        url = payload.get("links", {}).get("next")
        params = None  # the "next" link already carries the query params
    return results


@functools.lru_cache(maxsize=1)
def fetch_songs() -> tuple[dict, ...]:
    """Fetch every non-hidden song from the Planning Center Services API.

    Results are cached for the life of the process since several reports need
    the full catalog.
    """
    songs = fetch_all_pages(f"{PCO_API_BASE}/songs", {"per_page": 100})
    return tuple(s for s in songs if not s["attributes"].get("hidden"))


def load_catalog(christmas=False):
    """Load the song catalog into {title: {"id", "last_scheduled"}}.

    By default only non-Christmas songs are returned; pass christmas=True to get
    only the Christmas songs instead. Duplicate titles are collapsed into one
    entry, keeping the most recent Last Scheduled date and the smallest Id (the
    Id increases with creation order, so the smallest marks the earliest-added).
    """
    catalog: dict[str, dict] = {}
    for song in fetch_songs():
        attrs = song["attributes"]
        title = (attrs.get("title") or "").strip()
        if not title or is_christmas(attrs.get("themes") or "") != christmas:
            continue
        scheduled = parse_scheduled_datetime(attrs.get("last_scheduled_at"))
        try:
            song_id = int(song["id"])
        except (KeyError, ValueError, TypeError):
            song_id = None
        if title in catalog:
            entry = catalog[title]
            existing = entry["last_scheduled"]
            if scheduled and (existing is None or scheduled > existing):
                entry["last_scheduled"] = scheduled
            if song_id is not None and (entry["id"] is None or song_id < entry["id"]):
                entry["id"] = song_id
        else:
            catalog[title] = {"id": song_id, "last_scheduled": scheduled}
    return catalog


def load_christmas_titles() -> set[str]:
    """Return the set of catalog titles classified as Christmas songs."""
    titles: set[str] = set()
    for song in fetch_songs():
        attrs = song["attributes"]
        if is_christmas(attrs.get("themes") or ""):
            titles.add((attrs.get("title") or "").strip())
    return titles


def add_months(d: date, delta: int) -> date:
    """Return the first of the month `delta` months from d's month."""
    total = d.year * 12 + (d.month - 1) + delta
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


@functools.lru_cache(maxsize=1)
def fetch_service_type_id(name: str) -> str:
    """Look up a Planning Center service type's id by name."""
    for st in fetch_all_pages(f"{PCO_API_BASE}/service_types", {"per_page": 100}):
        if st["attributes"]["name"] == name:
            return st["id"]
    sys.exit(f"Error: no Planning Center service type named {name!r} found.")


def fetch_recent_plans(service_type_id: str, start: date, end: date) -> list[dict]:
    """Plans for a service type with a sort_date in [start, end]."""
    return fetch_all_pages(
        f"{PCO_API_BASE}/service_types/{service_type_id}/plans",
        {
            "per_page": 100,
            "order": "sort_date",
            "filter": "after,before",
            "after": start.isoformat(),
            # "before" is an exclusive bound, so push it past `end`.
            "before": (end + timedelta(days=1)).isoformat(),
        },
    )


@functools.lru_cache(maxsize=None)
def fetch_plan_song_titles(service_type_id: str, plan_id: str) -> frozenset[str]:
    """Distinct song titles scheduled in one plan.

    Cached since fetch_year_usage() and load_service_songs() both fetch items
    for plans in overlapping date ranges.
    """
    items = fetch_all_pages(
        f"{PCO_API_BASE}/service_types/{service_type_id}/plans/{plan_id}/items",
        {"per_page": 100},
    )
    return frozenset(
        item["attributes"]["title"].strip()
        for item in items
        if item["attributes"].get("item_type") == "song" and item["attributes"].get("title")
    )


def fetch_year_usage(year: int) -> dict[str, int]:
    """Count Plans per song title for a calendar year, from the API.

    Mirrors the "Song Usage Report" CSVs: for each song, the number of Sunday
    Morning Worship Services plans it was scheduled in during the year.
    """
    service_type_id = fetch_service_type_id(SERVICE_TYPE_NAME)
    plans = fetch_recent_plans(service_type_id, date(year, 1, 1), date(year, 12, 31))
    counts: dict[str, int] = {}
    for plan in plans:
        for title in fetch_plan_song_titles(service_type_id, plan["id"]):
            counts[title] = counts.get(title, 0) + 1
    return counts


def load_service_songs(months: int = RECENT_MONTHS_COUNT):
    """Summarize recent Sunday plans' song usage by month.

    Covers the current (partial) month plus the (months - 1) preceding full
    calendar months. Returns (month_labels, [{title, months}]) where
    month_labels are the covered months in chronological order (e.g.
    ["Apr", "May", "Jun"]) and months is the per-month count of services that
    song was scheduled in, aligned to month_labels.
    """
    today = date.today()
    start = add_months(date(today.year, today.month, 1), -(months - 1))

    service_type_id = fetch_service_type_id(SERVICE_TYPE_NAME)
    plans = fetch_recent_plans(service_type_id, start, today)

    month_labels: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    for plan in plans:
        sort_date = parse_scheduled_datetime(plan["attributes"]["sort_date"])
        month_label = f"{sort_date:%b}"
        if month_label not in month_labels:
            month_labels.append(month_label)
        for title in fetch_plan_song_titles(service_type_id, plan["id"]):
            counts.setdefault(title, {})[month_label] = (
                counts.get(title, {}).get(month_label, 0) + 1)

    songs = [
        {"title": title, "months": [by_month.get(m, 0) for m in month_labels]}
        for title, by_month in counts.items()
    ]
    return month_labels, songs


def load_usage_cache() -> dict[int, dict[str, int]]:
    """Load cached {year: {title: plans}} for completed past years."""
    if not USAGE_CACHE_FILE.exists():
        return {}
    with USAGE_CACHE_FILE.open(encoding="utf-8") as f:
        raw = json.load(f)
    return {int(year): counts for year, counts in raw.items()}


def save_usage_cache(cache: dict[int, dict[str, int]]) -> None:
    with USAGE_CACHE_FILE.open("w", encoding="utf-8") as f:
        json.dump({str(year): counts for year, counts in cache.items()}, f,
                  indent=2, sort_keys=True)


def ensure_year_cache(current_year: int) -> dict[int, dict[str, int]]:
    """Ensure every completed year from EARLIEST_YEAR through current_year - 1
    is in the on-disk usage cache, fetching any missing ones from the API, and
    return the full cache."""
    cache = load_usage_cache()
    for year in range(EARLIEST_YEAR, current_year):
        if year not in cache:
            print(f"Fetching {year} usage from the API to seed the cache "
                  f"(one-time)...", file=sys.stderr)
            cache[year] = fetch_year_usage(year)
            save_usage_cache(cache)  # save incrementally in case of interruption
    return cache


def discover_reports():
    """Compute the all-time usage total and the per-year usage columns.

    Returns (all_time_counts, [(year, counts), ...]) with years sorted
    ascending. Everything comes from the Planning Center API: the all-time
    total sums every year's Plans counts from EARLIEST_YEAR through the
    current year, and the yearly columns cover the most recent YEARLY_COLUMNS
    of those years. Completed past years are cached in USAGE_CACHE_FILE since
    that data never changes; only the current year is fetched live every run.
    """
    current_year = date.today().year
    current_year_counts = fetch_year_usage(current_year)
    cache = ensure_year_cache(current_year)

    all_time: dict[str, int] = {}
    for year in range(EARLIEST_YEAR, current_year):
        for title, plans in cache[year].items():
            all_time[title] = all_time.get(title, 0) + plans
    for title, plans in current_year_counts.items():
        all_time[title] = all_time.get(title, 0) + plans

    yearly = [(year, cache[year])
              for year in range(current_year - YEARLY_COLUMNS + 1, current_year)]
    yearly.append((current_year, current_year_counts))
    return all_time, yearly


# --- Building rows -----------------------------------------------------------

def build_rows(catalog, all_time, yearly):
    """Assemble the table rows.

    Each row is a dict with: title, id, all_time, years (list aligned to year
    order), last_scheduled (date or None).
    """
    rows = []
    for title, info in catalog.items():
        rows.append({
            "title": title,
            "id": info["id"],
            "all_time": all_time.get(title, 0),
            "years": [counts.get(title, 0) for _, counts in yearly],
            "last_scheduled": info["last_scheduled"],
        })
    return rows


# Number of trailing years that count as "recent" for the By Recent Usage report.
RECENT_YEAR_COUNT = 2

# A song last scheduled more than this many years ago is "not played" recently.
STALE_YEARS = 4


def recent_indices(yearly):
    """Indices into a row's "years" list for the most recent reports."""
    n = len(yearly)
    return list(range(max(0, n - RECENT_YEAR_COUNT), n))


def recent_usage(row, indices):
    """Total plays for a row across the recent years."""
    return sum(row["years"][i] for i in indices)


# --- PDF rendering -----------------------------------------------------------

PAGE_W, PAGE_H = letter
MARGIN = 0.6 * inch
TOP = PAGE_H - MARGIN
ROW_H = 14.5
FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_SIZE = 9.5
HEADER_SIZE = 12


NUMERIC_START = MARGIN + 2.5 * inch  # right edge of the first numeric column
NUMERIC_STEP = 0.64 * inch           # spacing between right-aligned numeric columns
MONTH_NUMERIC_STEP = 0.4 * inch      # narrower spacing for small monthly counts
LAST_SCHED_GAP = 0.6 * inch          # gap before the left-aligned Last Scheduled
LAST_SCHED_WIDTH = 1.0 * inch        # space reserved for the Last Scheduled text


def make_columns(year_labels, year_indices=None, combined_label=None,
                 fill_title=False, id_column=False, numeric_step=NUMERIC_STEP):
    """Build the column specs for a report.

    Each spec is a dict with: label, align, get(row)->str, and (for the title) a
    title flag. Columns are always Title, AllTime, the selected year columns, an
    optional combined column, then Last Scheduled.

    year_indices selects which entries of row["years"] to show (default: all);
    pass [] to show no year columns.
    combined_label, if given, adds a column summing those selected years.
    id_column adds a song Id column after AllTime.
    fill_title right-justifies the numeric block against the right margin so the
    Title column expands to use all remaining width (useful when a report has
    few columns).
    numeric_step overrides the spacing between right-aligned numeric columns;
    narrower for reports whose numeric columns hold only small counts (e.g.
    monthly totals), so the Title column keeps most of the width.
    """
    if year_indices is None:
        year_indices = list(range(len(year_labels)))

    cols = [
        {"label": "Title", "align": "left", "title": True,
         "get": lambda r: r["title"]},
        {"label": "AllTime", "align": "right",
         "get": lambda r: str(r["all_time"])},
    ]
    if id_column:
        cols.append({"label": "Id", "align": "right",
                     "get": lambda r: str(r["id"]) if r.get("id") is not None else ""})
    for i in year_indices:
        cols.append({"label": year_labels[i], "align": "right",
                     "get": (lambda r, i=i: str(r["years"][i]))})
    if combined_label:
        cols.append({"label": combined_label, "align": "right",
                     "get": (lambda r: str(sum(r["years"][i] for i in year_indices)))})
    cols.append({"label": "Last Scheduled", "align": "left",
                 "get": lambda r: (f"{r['last_scheduled']:%Y-%m-%d}"
                                   if r["last_scheduled"] else "")})

    # Where the first numeric column sits. By default it is a fixed distance from
    # the left margin; with fill_title we instead push the whole numeric block to
    # the right so its trailing Last Scheduled text ends at the right margin.
    numeric_start = NUMERIC_START
    if fill_title:
        n_right = sum(1 for col in cols if col["align"] == "right")
        numeric_start = (PAGE_W - MARGIN - LAST_SCHED_WIDTH - LAST_SCHED_GAP
                         - (n_right - 1) * numeric_step)

    # Assign x positions: title at the left margin, right-aligned numeric columns
    # stepping across, and the trailing left-aligned column after a gap.
    x = None
    for col in cols:
        if col.get("title"):
            col["x"] = MARGIN
        elif col["align"] == "right":
            x = numeric_start if x is None else x + numeric_step
            col["x"] = x
        else:  # trailing left-aligned column
            col["x"] = (x if x is not None else numeric_start) + LAST_SCHED_GAP
    return cols


def draw_page_header(c, subtitle, page_num, prepared_date):
    c.setFont(FONT, HEADER_SIZE)
    c.drawString(MARGIN, TOP, subtitle)
    c.drawCentredString(PAGE_W / 2, TOP,
                        f"Prepared by {PREPARED_BY} {prepared_date:%-m/%-d/%Y}")
    c.drawRightString(PAGE_W - MARGIN, TOP, f"Page {page_num}")


def draw_cells(c, cols, get_text, y, get_font=None):
    """Draw one row of cells. get_text(col) returns the string for that column.

    get_font(col), if given, returns a (font_name, size) tuple to set per cell.
    """
    for col in cols:
        if get_font is not None:
            c.setFont(*get_font(col))
        text = get_text(col)
        if col["align"] == "right":
            c.drawRightString(col["x"], y, text)
        else:
            c.drawString(col["x"], y, text)


def draw_table_header(c, cols, y):
    c.setFont(FONT_BOLD, FONT_SIZE)
    draw_cells(c, cols, lambda col: col["label"], y)
    line_y = y - 3
    c.setLineWidth(1)
    c.line(MARGIN, line_y, PAGE_W - MARGIN, line_y)
    return line_y - ROW_H + 2


def truncate_to_width(c, text, max_width):
    while text and c.stringWidth(text, FONT, FONT_SIZE) > max_width:
        text = text[:-1]
    return text


def draw_row(c, cols, row, y, title_max_width):
    # A flagged row gets its title drawn in bold (e.g. "new" songs).
    bold_title = row.get("bold_title", False)

    def cell(col):
        text = col["get"](row)
        if col.get("title"):
            text = truncate_to_width(c, text, title_max_width)
        return text

    def font(col):
        if col.get("title") and bold_title:
            return (FONT_BOLD, FONT_SIZE)
        return (FONT, FONT_SIZE)

    draw_cells(c, cols, cell, y, get_font=font)
    # Light separator under the row.
    c.setLineWidth(0.25)
    c.setStrokeGray(0.75)
    c.line(MARGIN, y - 3.5, PAGE_W - MARGIN, y - 3.5)
    c.setStrokeGray(0)


def render_pdf(path, subtitle, rows, cols, prepared_date, footer=None):
    """Render one report. footer, if given, is a line or list of lines drawn
    in bold below the table."""
    c = canvas.Canvas(str(path), pagesize=letter)
    # The Title column must not run into the next column.
    next_x = next(col["x"] for col in cols if not col.get("title"))
    title_max_width = next_x - MARGIN - 0.4 * inch

    page_num = 1
    draw_page_header(c, subtitle, page_num, prepared_date)
    y = draw_table_header(c, cols, TOP - 0.35 * inch)

    for row in rows:
        if y < MARGIN:
            c.showPage()
            page_num += 1
            draw_page_header(c, subtitle, page_num, prepared_date)
            y = draw_table_header(c, cols, TOP - 0.35 * inch)
        draw_row(c, cols, row, y, title_max_width)
        y -= ROW_H

    if footer:
        for line in [footer] if isinstance(footer, str) else footer:
            if y < MARGIN:
                c.showPage()
                page_num += 1
                draw_page_header(c, subtitle, page_num, prepared_date)
                y = draw_table_header(c, cols, TOP - 0.35 * inch)
            c.setFont(FONT_BOLD, FONT_SIZE)
            c.drawString(MARGIN, y - 2, line)
            y -= ROW_H

    c.showPage()
    c.save()


# --- HTML rendering -----------------------------------------------------------

STYLE_CSS = """\
:root { color-scheme: light dark; }
body {
  font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  max-width: 900px; margin: 2rem auto; padding: 0 1rem;
}
header { margin-bottom: 1.5rem; }
h1 { margin-bottom: 0.25rem; }
.meta { color: #767676; font-size: 0.9rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { padding: 0.35rem 0.6rem; border-bottom: 1px solid #ccc; text-align: left; }
th.num, td.num { text-align: right; }
thead th { border-bottom: 2px solid; }
.footer-note { font-weight: bold; margin-top: 1rem; }
ul.reports {
  list-style: none; padding: 0; display: grid; gap: 1rem;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
}
.card { border: 1px solid #ccc; border-radius: 8px; padding: 1rem; }
.card h2 { margin: 0 0 0.4rem; font-size: 1.05rem; }
.card p { margin: 0.3rem 0 0; }
.card .count { color: #767676; font-size: 0.85rem; }
a { color: #0645ad; text-decoration: none; }
a:hover { text-decoration: underline; }
@media (prefers-color-scheme: dark) {
  a { color: #8ab4f8; }
}
"""


def render_html(path: Path, subtitle: str, rows, cols, prepared_date, footer=None):
    """Render one report as a static HTML page, using the same column specs
    (label, align, get, and the title flag) that render_pdf uses."""

    def cell_html(col, row):
        text = html.escape(str(col["get"](row)))
        if col.get("title") and row.get("bold_title"):
            text = f"<strong>{text}</strong>"
        return text

    thead = "".join(
        f'<th class="{"num" if c["align"] == "right" else "text"}">'
        f'{html.escape(c["label"])}</th>'
        for c in cols
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td class="{"num" if c["align"] == "right" else "text"}">'
            f'{cell_html(c, row)}</td>'
            for c in cols
        ) + "</tr>"
        for row in rows
    )

    footer_html = ""
    if footer:
        lines = [footer] if isinstance(footer, str) else footer
        footer_html = "".join(f'<p class="footer-note">{html.escape(line)}</p>' for line in lines)

    path.write_text(f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(subtitle)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header>
<h1>{html.escape(subtitle)}</h1>
<p class="meta">Prepared by {html.escape(PREPARED_BY)} &middot; {prepared_date:%-m/%-d/%Y}
&middot; <a href="index.html">Home</a></p>
</header>
<table>
<thead><tr>{thead}</tr></thead>
<tbody>
{body}
</tbody>
</table>
{footer_html}
</body>
</html>
""", encoding="utf-8")


def render_report(name: str, subtitle: str, description: str, rows, cols, prepared_date,
                  footer=None) -> dict:
    """Render one report as both a PDF (in OUT_DIR) and an HTML page (in
    SITE_DIR), and return the metadata the home page needs to link to it."""
    render_pdf(OUT_DIR / f"{name}.pdf", subtitle, rows, cols, prepared_date, footer=footer)
    render_html(SITE_DIR / f"{name}.html", subtitle, rows, cols, prepared_date, footer=footer)
    return {"name": name, "title": subtitle, "description": description, "count": len(rows)}


def render_index(reports: list[dict], prepared_date) -> None:
    """Render the _site/index.html home page linking to each report."""
    cards = "".join(f"""\
<li class="card">
<h2><a href="{r['name']}.html">{html.escape(r['title'])}</a></h2>
<p>{html.escape(r['description'])}</p>
<p class="count">{r['count']} songs</p>
</li>
""" for r in reports)

    (SITE_DIR / "index.html").write_text(f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Creekside Song Stats</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header>
<h1>Creekside Song Stats</h1>
<p class="meta">Prepared by {html.escape(PREPARED_BY)} &middot; {prepared_date:%-m/%-d/%Y}</p>
</header>
<ul class="reports">
{cards}
</ul>
</body>
</html>
""", encoding="utf-8")


# --- Main --------------------------------------------------------------------

def main():
    catalog = load_catalog()
    all_time, yearly = discover_reports()
    rows = build_rows(catalog, all_time, yearly)
    year_labels = [str(year) for year, _ in yearly]
    prepared_date = date.today()

    OUT_DIR.mkdir(exist_ok=True)
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    reports = []

    # By Title and By All Time show every year column.
    full_cols = make_columns(year_labels)

    by_title = sorted(rows, key=lambda r: r["title"].lower())
    reports.append(render_report(
        "ByTitle", "By Title", "Every (non-Christmas) song, sorted alphabetically.",
        by_title, full_cols, prepared_date))

    by_all_time = sorted(rows, key=lambda r: (-r["all_time"], r["title"].lower()))
    reports.append(render_report(
        "ByAllTime", "By All Time", "The same songs, sorted by all-time play count.",
        by_all_time, full_cols, prepared_date))

    # By Recent Usage: only songs played in the most recent RECENT_YEAR_COUNT
    # years, sorted by their combined plays over those years (descending). Shows
    # only the recent year columns plus a combined "Recent" total.
    indices = recent_indices(yearly)
    recent_rows = [
        # "New" songs: every all-time play happened within the recent years.
        {**r, "bold_title": r["all_time"] == recent_usage(r, indices)}
        for r in rows if recent_usage(r, indices) > 0
    ]
    by_recent = sorted(recent_rows,
                       key=lambda r: (-recent_usage(r, indices), r["title"].lower()))
    recent_cols = make_columns(year_labels, year_indices=indices,
                               combined_label="Recent", fill_title=True)
    reports.append(render_report(
        "ByRecentUsage", "By Recent Usage",
        "Songs played in the two most recent years, sorted by recent usage.",
        by_recent, recent_cols, prepared_date,
        footer="Bold titles: every all-time play of the song happened "
               "within these recent years (i.e., new to the congregation)."))

    # Recent Months: like By Recent Usage, but the period columns are months from
    # the per-service grid, with a combined "Total" of those months. Monthly
    # counts reuse the row's "years" slot so make_columns/render_pdf apply as-is.
    christmas = load_christmas_titles()
    month_labels, service = load_service_songs()
    month_rows = []
    for s in service:
        if s["title"] in christmas:
            continue
        at = all_time.get(s["title"], 0)
        info = catalog.get(s["title"])
        month_rows.append({
            "title": s["title"],
            "all_time": at,
            "years": s["months"],
            "last_scheduled": info["last_scheduled"] if info else None,
            # "New" songs: every all-time play happened within the recent months.
            "bold_title": at == sum(s["months"]),
        })
    by_month = sorted(month_rows, key=lambda r: (-sum(r["years"]), r["title"].lower()))
    month_cols = make_columns(month_labels, combined_label="Total", fill_title=True,
                              numeric_step=MONTH_NUMERIC_STEP)
    reports.append(render_report(
        "RecentMonths", "Recent Months",
        "Songs from recent plans, with a column per month and a combined total.",
        by_month, month_cols, prepared_date,
        footer=[f"Total songs: {len(by_month)}",
                "Bold titles: every all-time play of the song happened "
                "within this report's months (i.e., new to the congregation)."]))

    # Never Played: catalog songs with no recorded plays, sorted by Id (which
    # increases with creation order, so this roughly orders by when they were
    # added). Songs lacking an Id sort last. No annual play counts; show the Id.
    never_played = sorted(
        [r for r in rows if r["all_time"] == 0],
        key=lambda r: (r["id"] is None, r["id"]))
    never_cols = make_columns(year_labels, year_indices=[], id_column=True,
                              fill_title=True)
    reports.append(render_report(
        "NeverPlayed", "Never Played", "Catalog songs with no recorded plays.",
        never_played, never_cols, prepared_date,
        footer=f"Total songs: {len(never_played)}"))

    # Christmas: the songs excluded from the other reports, most played first.
    christmas_rows = build_rows(load_catalog(christmas=True), all_time, yearly)
    by_christmas = sorted(christmas_rows,
                          key=lambda r: (-r["all_time"], r["title"].lower()))
    reports.append(render_report(
        "Christmas", "Christmas Songs",
        "The Christmas songs excluded from the other reports.",
        by_christmas, full_cols, prepared_date,
        footer=f"Total songs: {len(by_christmas)}"))

    # Not Played in N years: songs played at some point but not in the last
    # STALE_YEARS years (by Last Scheduled date), oldest first.
    cutoff = prepared_date.replace(year=prepared_date.year - STALE_YEARS)
    stale = sorted(
        [r for r in rows if r["last_scheduled"] and r["last_scheduled"] < cutoff],
        key=lambda r: r["last_scheduled"])
    reports.append(render_report(
        "NotPlayed4Years", f"Not Played Since {cutoff:%Y-%m-%d}",
        f"Songs not scheduled in the last {STALE_YEARS} years.",
        stale, full_cols, prepared_date,
        footer=f"Total songs: {len(stale)}"))

    render_index(reports, prepared_date)

    recent_labels = [year_labels[i] for i in indices]
    print(f"Wrote {len(rows)} songs to {OUT_DIR}/*.pdf and {SITE_DIR}/*.html:")
    print("  ByTitle")
    print("  ByAllTime")
    print(f"  ByRecentUsage ({len(by_recent)} songs, "
          f"recent years: {', '.join(recent_labels)})")
    print(f"  RecentMonths ({len(by_month)} songs, "
          f"months: {', '.join(month_labels)})")
    print(f"  NeverPlayed ({len(never_played)} songs)")
    print(f"  Christmas ({len(by_christmas)} songs)")
    print(f"  NotPlayed4Years ({len(stale)} songs, "
          f"last scheduled before {cutoff:%Y-%m-%d})")
    print(f"Year columns: {', '.join(year_labels)}")


if __name__ == "__main__":
    main()
