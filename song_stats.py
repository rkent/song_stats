#!/usr/bin/env python3
"""Generate song-usage statistics PDFs from Planning Center data.

Reads the song catalog from the Planning Center Services API and the usage
exports in data/ and produces these PDFs in output/:

  * ByTitle.pdf       - every (non-Christmas) song, sorted alphabetically
  * ByAllTime.pdf     - the same songs, sorted by all-time play count (desc)
  * ByRecentUsage.pdf - only songs played in the two most recent years, with
                        per-year columns and a combined "Recent" total
  * RecentMonths.pdf  - songs from the per-service grid, with a column per month
                        and a combined "Total"
  * NeverPlayed.pdf   - catalog songs with no recorded plays
  * Christmas.pdf     - the Christmas songs excluded from the other reports
  * NotPlayed4Years.pdf - songs not scheduled in the last STALE_YEARS years

Most rows show the all-time play count, period columns, and the date the song
was last scheduled.

Data sources:
  * Planning Center Services API (/services/v2/songs) ... the song catalog
                                        (titles, themes, last scheduled date).
                                        Requires CLIENT_ID and CLIENT_SECRET, a
                                        Planning Center personal access token
                                        (https://api.planningcenteronline.com/oauth/applications),
                                        set in the environment or a .env file.
  * data/Song Usage Report(... to ...).csv . one per period; a report whose
                                        start and end years differ is treated
                                        as the all-time total, one whose years
                                        match is treated as that single year's
                                        column.
  * Planning Center Services API (/service_types/.../plans and .../items) ...
                                        the Sunday Morning Worship Services
                                        plans and their song items for the
                                        current month plus the RECENT_MONTHS_COUNT - 1
                                        preceding full months, summarized by
                                        month.

Songs are matched between the catalog and the usage data by TITLE only.
Christmas songs are excluded, and duplicate titles in the catalog are collapsed
into a single row.
"""

from __future__ import annotations

import csv
import functools
import os
import re
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
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"

PREPARED_BY = "Kent James"

PCO_API_BASE = "https://api.planningcenteronline.com/services/v2"
# The Planning Center service type whose plans feed the Recent Months report.
SERVICE_TYPE_NAME = "Sunday Morning Worship Services"
# Recent Months covers the current (partial) month plus this many preceding
# full calendar months.
RECENT_MONTHS_COUNT = 6
# Matches "Song Usage Report(01_01_2012 to 12_31_2025).csv" and captures years.
USAGE_RE = re.compile(r"Song Usage Report\((\d\d)_(\d\d)_(\d{4}) to (\d\d)_(\d\d)_(\d{4})\)")


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


def fetch_plan_song_titles(service_type_id: str, plan_id: str) -> set[str]:
    """Distinct song titles scheduled in one plan."""
    items = fetch_all_pages(
        f"{PCO_API_BASE}/service_types/{service_type_id}/plans/{plan_id}/items",
        {"per_page": 100},
    )
    return {
        item["attributes"]["title"].strip()
        for item in items
        if item["attributes"].get("item_type") == "song" and item["attributes"].get("title")
    }


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


def load_usage(path: Path) -> dict[str, int]:
    """Load a usage report into {title: plans_count}."""
    counts: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            title = row["Song Name"].strip()
            try:
                plans = int(row["Plans"])
            except (KeyError, ValueError):
                plans = 0
            # Same title may appear more than once; accumulate to be safe.
            counts[title] = counts.get(title, 0) + plans
    return counts


def discover_reports():
    """Find the all-time report and the per-year reports in data/.

    Returns (all_time_counts, [(year, counts), ...]) with years sorted ascending.

    The all-time report (a span of multiple years, e.g. 2012-2025) may end before
    the latest yearly report. Any yearly report for a year *after* the all-time
    report's span is added into the all-time totals, so AllTime stays complete
    without needing the multi-year export re-run every year. (Years inside the
    span are already covered by it and are not re-added, avoiding double counting.)
    """
    all_time: dict[str, int] | None = None
    all_time_end: int | None = None
    yearly: list[tuple[int, dict[str, int]]] = []
    for path in sorted(DATA_DIR.glob("Song Usage Report*.csv")):
        if path.name.endswith(".old.csv"):
            continue
        m = USAGE_RE.search(path.name)
        if not m:
            continue
        start_year, end_year = int(m.group(3)), int(m.group(6))
        counts = load_usage(path)
        if start_year == end_year:
            yearly.append((start_year, counts))
        else:
            if all_time is not None:
                print(f"Warning: multiple all-time reports found; using {path.name}",
                      file=sys.stderr)
            all_time = counts
            all_time_end = end_year
    if all_time is None:
        sys.exit("Error: no all-time usage report (span of multiple years) found in data/.")
    # Fold in yearly reports beyond the all-time report's span.
    for year, counts in yearly:
        if year > all_time_end:
            for title, plans in counts.items():
                all_time[title] = all_time.get(title, 0) + plans
    yearly.sort(key=lambda x: x[0])
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
LAST_SCHED_GAP = 0.6 * inch          # gap before the left-aligned Last Scheduled
LAST_SCHED_WIDTH = 1.0 * inch        # space reserved for the Last Scheduled text


def make_columns(year_labels, year_indices=None, combined_label=None,
                 fill_title=False, id_column=False):
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
                         - (n_right - 1) * NUMERIC_STEP)

    # Assign x positions: title at the left margin, right-aligned numeric columns
    # stepping across, and the trailing left-aligned column after a gap.
    x = None
    for col in cols:
        if col.get("title"):
            col["x"] = MARGIN
        elif col["align"] == "right":
            x = numeric_start if x is None else x + NUMERIC_STEP
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
        if y < MARGIN:
            c.showPage()
            page_num += 1
            draw_page_header(c, subtitle, page_num, prepared_date)
            y = draw_table_header(c, cols, TOP - 0.35 * inch)
        c.setFont(FONT_BOLD, FONT_SIZE)
        c.drawString(MARGIN, y - 2, footer)

    c.showPage()
    c.save()


# --- Main --------------------------------------------------------------------

def main():
    catalog = load_catalog()
    all_time, yearly = discover_reports()
    rows = build_rows(catalog, all_time, yearly)
    year_labels = [str(year) for year, _ in yearly]
    prepared_date = date.today()

    OUT_DIR.mkdir(exist_ok=True)

    # By Title and By All Time show every year column.
    full_cols = make_columns(year_labels)

    by_title = sorted(rows, key=lambda r: r["title"].lower())
    render_pdf(OUT_DIR / "ByTitle.pdf", "By Title", by_title, full_cols, prepared_date)

    by_all_time = sorted(rows, key=lambda r: (-r["all_time"], r["title"].lower()))
    render_pdf(OUT_DIR / "ByAllTime.pdf", "By All Time", by_all_time, full_cols, prepared_date)

    # By Recent Usage: only songs played in the most recent RECENT_YEAR_COUNT
    # years, sorted by their combined plays over those years (descending). Shows
    # only the recent year columns plus a combined "Recent" total.
    indices = recent_indices(yearly)
    recent_rows = [r for r in rows if recent_usage(r, indices) > 0]
    by_recent = sorted(recent_rows,
                       key=lambda r: (-recent_usage(r, indices), r["title"].lower()))
    recent_cols = make_columns(year_labels, year_indices=indices,
                               combined_label="Recent", fill_title=True)
    render_pdf(OUT_DIR / "ByRecentUsage.pdf", "By Recent Usage", by_recent,
               recent_cols, prepared_date)

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
    month_cols = make_columns(month_labels, combined_label="Total", fill_title=True)
    render_pdf(OUT_DIR / "RecentMonths.pdf", "Recent Months", by_month,
               month_cols, prepared_date,
               footer=f"Total songs: {len(by_month)}")

    # Never Played: catalog songs with no recorded plays, sorted by Id (which
    # increases with creation order, so this roughly orders by when they were
    # added). Songs lacking an Id sort last. No annual play counts; show the Id.
    never_played = sorted(
        [r for r in rows if r["all_time"] == 0],
        key=lambda r: (r["id"] is None, r["id"]))
    never_cols = make_columns(year_labels, year_indices=[], id_column=True,
                              fill_title=True)
    render_pdf(OUT_DIR / "NeverPlayed.pdf", "Never Played", never_played,
               never_cols, prepared_date, footer=f"Total songs: {len(never_played)}")

    # Christmas: the songs excluded from the other reports, most played first.
    christmas_rows = build_rows(load_catalog(christmas=True), all_time, yearly)
    by_christmas = sorted(christmas_rows,
                          key=lambda r: (-r["all_time"], r["title"].lower()))
    render_pdf(OUT_DIR / "Christmas.pdf", "Christmas Songs", by_christmas,
               full_cols, prepared_date, footer=f"Total songs: {len(by_christmas)}")

    # Not Played in N years: songs played at some point but not in the last
    # STALE_YEARS years (by Last Scheduled date), oldest first.
    cutoff = prepared_date.replace(year=prepared_date.year - STALE_YEARS)
    stale = sorted(
        [r for r in rows if r["last_scheduled"] and r["last_scheduled"] < cutoff],
        key=lambda r: r["last_scheduled"])
    render_pdf(OUT_DIR / "NotPlayed4Years.pdf",
               f"Not Played Since {cutoff:%Y-%m-%d}", stale,
               full_cols, prepared_date, footer=f"Total songs: {len(stale)}")

    recent_labels = [year_labels[i] for i in indices]
    print(f"Wrote {len(rows)} songs to:")
    print(f"  {OUT_DIR / 'ByTitle.pdf'}")
    print(f"  {OUT_DIR / 'ByAllTime.pdf'}")
    print(f"  {OUT_DIR / 'ByRecentUsage.pdf'} ({len(by_recent)} songs, "
          f"recent years: {', '.join(recent_labels)})")
    print(f"  {OUT_DIR / 'RecentMonths.pdf'} ({len(by_month)} songs, "
          f"months: {', '.join(month_labels)})")
    print(f"  {OUT_DIR / 'NeverPlayed.pdf'} ({len(never_played)} songs)")
    print(f"  {OUT_DIR / 'Christmas.pdf'} ({len(by_christmas)} songs)")
    print(f"  {OUT_DIR / 'NotPlayed4Years.pdf'} ({len(stale)} songs, "
          f"last scheduled before {cutoff:%Y-%m-%d})")
    print(f"Year columns: {', '.join(year_labels)}")


if __name__ == "__main__":
    main()
