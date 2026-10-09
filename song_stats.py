#!/usr/bin/env python3
"""Generate song-usage statistics PDFs from Planning Center data.

Reads everything from the Planning Center Services API (no data files) and
Produces these reports in output/ (PDF) and docs/ (HTML plus a JSON version of
each report, easier to consume programmatically than scraping the HTML):

  * ByTitle.pdf       - every (non-Christmas) song, sorted alphabetically
  * ByAllTime.pdf     - the same songs, sorted by all-time play count (desc)
  * ByRecentUsage.pdf - only songs played in the two most recent years, with
                        per-year columns and a combined "Recent" total
  * RecentMonths.pdf  - songs from recent plans, with a column per month
                        and a combined "Total"
  * NeverPlayed.pdf   - catalog songs with no recorded plays
  * Christmas.pdf     - the Christmas songs excluded from the other reports
  * NotPlayed4Years.pdf - songs not scheduled in the last STALE_YEARS years
  * SongKeys.pdf      - every song with a recorded key, sorted alphabetically,
                        listing each key it's been played in and how many
                        times, most-used key first
  * OldnessScores.html - per-song historical usage and the oldness score for
                         the 8 most recent service sets

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
                                            full months;
                                          - the SongKeys report, from each
                                            song item's key_name attribute
                                            (the key it was actually played
                                            in on that date), also cached in
                                            usage_cache.json alongside the
                                            play counts.

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
SITE_DIR = ROOT / "docs"

PREPARED_BY = "Kent James"

PCO_API_BASE = "https://api.planningcenteronline.com/services/v2"
# Planning Center's song-viewer URL (not part of the API), for linking titles.
PCO_SONG_URL_BASE = "https://services.planningcenteronline.com/songs"
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


def song_url(song_id: int | None) -> str | None:
    """The Planning Center song-viewer URL for a song id, or None if unknown."""
    return f"{PCO_SONG_URL_BASE}/{song_id}/" if song_id is not None else None


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
def fetch_plan_song_items(service_type_id: str, plan_id: str) -> tuple[dict, ...]:
    """Song items (item_type == "song", with a title) scheduled in one plan, as
    {"title", "key_name"} dicts (key_name is "" when no key was recorded).

    Cached since fetch_year_usage(), fetch_year_keys(), and load_service_songs()
    all fetch items for plans in overlapping date ranges.
    """
    items = fetch_all_pages(
        f"{PCO_API_BASE}/service_types/{service_type_id}/plans/{plan_id}/items",
        {"per_page": 100},
    )
    return tuple(
        {"title": item["attributes"]["title"].strip(),
         "key_name": (item["attributes"].get("key_name") or "").strip()}
        for item in items
        if item["attributes"].get("item_type") == "song" and item["attributes"].get("title")
    )


def fetch_plan_song_titles(service_type_id: str, plan_id: str) -> frozenset[str]:
    """Distinct song titles scheduled in one plan."""
    return frozenset(item["title"]
                     for item in fetch_plan_song_items(service_type_id, plan_id))


def plan_service_date(plan: dict) -> date:
    service_date = parse_scheduled_datetime(
        plan["attributes"].get("sort_date"))
    if service_date is None:
        raise ValueError(f"Plan {plan.get('id')} has no valid sort_date")
    return service_date


def load_recent_song_sets(service_type_id: str, as_of: date,
                          limit: int = 8) -> list[dict]:
    """Load the most recent plans with songs, including the next 90 days."""
    oldest_date = date(EARLIEST_YEAR, 1, 1)
    search_end = as_of + timedelta(days=90)
    plans = fetch_recent_plans(service_type_id, oldest_date, search_end)
    dated_plans = sorted(
        ((plan_service_date(plan), plan) for plan in plans),
        key=lambda pair: pair[0],
        reverse=True,
    )
    sets = []
    for service_date, plan in dated_plans:
        songs = tuple(dict.fromkeys(
            item["title"] for item in
            fetch_plan_song_items(service_type_id, plan["id"])))
        if songs:
            sets.append({"service_date": service_date, "songs": songs})
            if len(sets) == limit:
                break
    return sets


def calculate_set_oldness(song_sets: list[dict], all_time: dict[str, int],
                          later_plan_usage: list[tuple[date, frozenset[str]]]
                          ) -> list[dict]:
    """Calculate each set's song counts using only plans before its service
    week. `all_time` includes this year through year end, so later plan uses
    must be subtracted for each set."""
    scored_sets = []
    for song_set in song_sets:
        service_date = song_set["service_date"]
        week_start = service_date - timedelta(days=service_date.weekday())
        songs = []
        score = 0
        for title in song_set["songs"]:
            later_uses = sum(
                title in plan_songs
                for plan_date, plan_songs in later_plan_usage
                if plan_date >= week_start
            )
            prior_uses = all_time.get(title, 0) - later_uses
            if prior_uses < 0:
                raise ValueError(
                    f"Later scheduled uses for {title!r} exceed its all-time "
                    f"count before {service_date}")
            songs.append({"title": title, "prior_uses": prior_uses})
            score += prior_uses
        scored_sets.append({
            "service_date": service_date,
            "oldness_score": score,
            "songs": songs,
        })
    return scored_sets


def build_recent_set_oldness(all_time: dict[str, int], as_of: date) -> list[dict]:
    """Build oldness scores for the 8 most recent service plans with songs."""
    service_type_id = fetch_service_type_id(SERVICE_TYPE_NAME)
    song_sets = load_recent_song_sets(service_type_id, as_of)
    if not song_sets:
        return []

    first_week = min(
        song_set["service_date"] -
        timedelta(days=song_set["service_date"].weekday())
        for song_set in song_sets)
    through_year_end = date(as_of.year, 12, 31)
    later_plan_usage = []
    if first_week <= through_year_end:
        later_plans = fetch_recent_plans(
            service_type_id, first_week, through_year_end)
        for plan in later_plans:
            service_date = plan_service_date(plan)
            if first_week <= service_date <= through_year_end:
                later_plan_usage.append((
                    service_date,
                    fetch_plan_song_titles(service_type_id, plan["id"]),
                ))
    return calculate_set_oldness(song_sets, all_time, later_plan_usage)


def fetch_plan_song_keys(service_type_id: str, plan_id: str) -> list[tuple[str, str]]:
    """(title, key_name) for every song item in one plan that has a key set."""
    return [(item["title"], item["key_name"])
            for item in fetch_plan_song_items(service_type_id, plan_id)
            if item["key_name"]]


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


def fetch_year_keys(year: int) -> dict[str, dict[str, int]]:
    """{title: {key: count}} of key usage for a calendar year, from the API.

    Shares fetch_plan_song_items()'s cache with fetch_year_usage(), so calling
    both for the same year costs no extra API requests.
    """
    service_type_id = fetch_service_type_id(SERVICE_TYPE_NAME)
    plans = fetch_recent_plans(service_type_id, date(year, 1, 1), date(year, 12, 31))
    counts: dict[str, dict[str, int]] = {}
    for plan in plans:
        for title, key_name in fetch_plan_song_keys(service_type_id, plan["id"]):
            by_key = counts.setdefault(title, {})
            by_key[key_name] = by_key.get(key_name, 0) + 1
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


def load_usage_cache() -> dict[int, dict]:
    """Load cached {year: {"usage": {title: plans}, "keys": {title: {key: count}}}}
    for completed past years."""
    if not USAGE_CACHE_FILE.exists():
        return {}
    with USAGE_CACHE_FILE.open(encoding="utf-8") as f:
        raw = json.load(f)
    return {int(year): entry for year, entry in raw.items()}


def save_usage_cache(cache: dict[int, dict]) -> None:
    with USAGE_CACHE_FILE.open("w", encoding="utf-8") as f:
        json.dump({str(year): entry for year, entry in cache.items()}, f,
                  indent=2, sort_keys=True)


def ensure_year_cache(current_year: int) -> dict[int, dict]:
    """Ensure every completed year from EARLIEST_YEAR through current_year - 1
    has {"usage": ..., "keys": ...} in the on-disk cache, fetching whatever is
    missing from the API, and return the full cache.

    A cache written before the "keys" column existed stores the year's usage
    dict directly (not wrapped in {"usage": ..., "keys": ...}); that's
    recognized as a legacy entry, reused as the usage half, and upgraded in
    place instead of refetching usage that's already on disk.
    """
    cache = load_usage_cache()
    for year in range(EARLIEST_YEAR, current_year):
        entry = cache.get(year)
        legacy_usage = None
        if entry is not None and not ("usage" in entry and "keys" in entry):
            legacy_usage, entry = entry, None
        if entry is None:
            usage = legacy_usage
            if usage is None:
                print(f"Fetching {year} usage from the API to seed the cache "
                      f"(one-time)...", file=sys.stderr)
                usage = fetch_year_usage(year)
            print(f"Fetching {year} keys from the API to seed the cache "
                  f"(one-time)...", file=sys.stderr)
            keys = fetch_year_keys(year)
            cache[year] = {"usage": usage, "keys": keys}
            save_usage_cache(cache)  # save incrementally in case of interruption
    return cache


def discover_reports():
    """Compute the all-time usage and key totals and the per-year usage columns.

    Returns (all_time_counts, [(year, counts), ...], all_time_keys) with years
    sorted ascending. Everything comes from the Planning Center API: the
    all-time total sums every year's Plans counts from EARLIEST_YEAR through
    the current year, all_time_keys sums each song's {key: count} the same
    way, and the yearly columns cover the most recent YEARLY_COLUMNS of those
    years. Completed past years are cached in USAGE_CACHE_FILE since that data
    never changes; only the current year is fetched live every run.
    """
    current_year = date.today().year
    current_year_counts = fetch_year_usage(current_year)
    current_year_keys = fetch_year_keys(current_year)
    cache = ensure_year_cache(current_year)

    all_time: dict[str, int] = {}
    all_time_keys: dict[str, dict[str, int]] = {}
    for year in range(EARLIEST_YEAR, current_year):
        for title, plans in cache[year]["usage"].items():
            all_time[title] = all_time.get(title, 0) + plans
        for title, by_key in cache[year]["keys"].items():
            dest = all_time_keys.setdefault(title, {})
            for key_name, count in by_key.items():
                dest[key_name] = dest.get(key_name, 0) + count
    for title, plans in current_year_counts.items():
        all_time[title] = all_time.get(title, 0) + plans
    for title, by_key in current_year_keys.items():
        dest = all_time_keys.setdefault(title, {})
        for key_name, count in by_key.items():
            dest[key_name] = dest.get(key_name, 0) + count

    yearly = [(year, cache[year]["usage"])
              for year in range(current_year - YEARLY_COLUMNS + 1, current_year)]
    yearly.append((current_year, current_year_counts))
    return all_time, yearly, all_time_keys


# --- Building rows -----------------------------------------------------------

def top_keys(by_key: dict[str, int]) -> list[str]:
    """A song's keys, most-played first (ties broken alphabetically)."""
    return [key for key, _ in sorted(by_key.items(), key=lambda kv: (-kv[1], kv[0]))]


def format_keys_summary(by_key: dict[str, int]) -> str:
    """" (Key1, Key2)" for a song's top two most-played keys, with a trailing
    ellipsis if it's been played in more keys than that, or "" if it has no
    recorded key. Meant to be appended to a title for display."""
    if not by_key:
        return ""
    keys = top_keys(by_key)
    shown = keys[:2] + (["…"] if len(keys) > 2 else [])
    return f" ({', '.join(shown)})"


def build_rows(catalog, all_time, yearly, all_time_keys):
    """Assemble the table rows.

    Each row is a dict with: title, id, all_time, years (list aligned to year
    order), last_scheduled (date or None), and title_display (title plus a
    " (Key1, Key2, ...)" summary of its top played keys, for the Title column).
    """
    rows = []
    for title, info in catalog.items():
        rows.append({
            "title": title,
            "title_display": title + format_keys_summary(all_time_keys.get(title, {})),
            "id": info["id"],
            "all_time": all_time.get(title, 0),
            "years": [counts.get(title, 0) for _, counts in yearly],
            "last_scheduled": info["last_scheduled"],
        })
    return rows


def build_key_rows(all_time_keys: dict[str, dict[str, int]], catalog: dict) -> list[dict]:
    """One row per song with a recorded key, sorted alphabetically, listing
    every key it's been played in with a count (most-used key first)."""
    rows = []
    for title in sorted(all_time_keys, key=str.lower):
        keys = [{"key": key, "count": all_time_keys[title][key]}
                for key in top_keys(all_time_keys[title])]
        keys_str = ", ".join(f"{k['key']} ({k['count']})" for k in keys)
        info = catalog.get(title)
        rows.append({"title": title, "keys_str": keys_str, "keys": keys,
                     "id": info["id"] if info else None})
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
    title flag. Also included: key, a short machine-readable field name, and
    json(row)->value, returning the column's data in its natural type (int,
    ISO date string, or None) rather than get()'s display-formatted string;
    both are used by render_json(). Columns are always Title, AllTime, the
    selected year columns, an optional combined column, then Last Scheduled.

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
        {"label": "Title", "align": "left", "title": True, "key": "title",
         "get": lambda r: r.get("title_display", r["title"]),
         "json": lambda r: r["title"]},
        {"label": "AllTime", "align": "right", "key": "all_time",
         "get": lambda r: str(r["all_time"]),
         "json": lambda r: r["all_time"]},
    ]
    if id_column:
        cols.append({"label": "Id", "align": "right", "key": "id",
                     "get": lambda r: str(r["id"]) if r.get("id") is not None else "",
                     "json": lambda r: r.get("id")})
    for i in year_indices:
        cols.append({"label": year_labels[i], "align": "right", "key": year_labels[i],
                     "get": (lambda r, i=i: str(r["years"][i])),
                     "json": (lambda r, i=i: r["years"][i])})
    if combined_label:
        cols.append({"label": combined_label, "align": "right", "key": combined_label.lower(),
                     "get": (lambda r: str(sum(r["years"][i] for i in year_indices))),
                     "json": (lambda r: sum(r["years"][i] for i in year_indices))})
    cols.append({"label": "Last Scheduled", "align": "left", "key": "last_scheduled",
                 "get": lambda r: (f"{r['last_scheduled']:%Y-%m-%d}"
                                   if r["last_scheduled"] else ""),
                 "json": lambda r: (r["last_scheduled"].isoformat()
                                    if r["last_scheduled"] else None)})

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


def make_key_columns():
    """Column specs for the SongKeys report: Title, then a wide left-aligned
    Keys Played column (truncated to the remaining page width)."""
    keys_x = MARGIN + 2.9 * inch
    return [
        {"label": "Title", "align": "left", "title": True, "key": "title",
         "get": lambda r: r["title"], "json": lambda r: r["title"], "x": MARGIN},
        {"label": "Keys Played (times)", "align": "left", "key": "keys",
         "get": lambda r: r["keys_str"], "json": lambda r: r["keys"],
         "x": keys_x, "max_width": PAGE_W - MARGIN - keys_x},
    ]


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
        elif col.get("max_width") is not None:
            text = truncate_to_width(c, text, col["max_width"])
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
.candidate-heart {
  color: #c62828; font-size: 0.8em; margin-left: 0.2rem;
  text-decoration: none;
}
.candidate-heart:hover, .candidate-heart:focus-visible {
  opacity: 0.8; text-decoration: none;
}
.oldness-set { margin-top: 2rem; }
.oldness-score { font-size: 1.1rem; font-weight: bold; }
.song-sets-card {
  border: 1px solid #a15c00; border-radius: 8px; padding: 1rem;
  margin-top: 1.5rem;
}
.song-sets-card h2 { margin: 0 0 0.4rem; font-size: 1.05rem; }
.song-sets-card .ai-disclaimer {
  font-style: italic; color: #a15c00; margin: 0.3rem 0; font-size: 0.9rem;
}
.song-sets-card .count { color: #767676; font-size: 0.85rem; margin: 0.3rem 0 0; }
@media (prefers-color-scheme: dark) {
  a { color: #8ab4f8; }
  .candidate-heart { color: #ff6b6b; }
  .song-sets-card { border-color: #e0a94a; }
  .song-sets-card .ai-disclaimer { color: #e0a94a; }
}
"""


def render_html(path: Path, subtitle: str, rows, cols, prepared_date, footer=None):
    """Render one report as a static HTML page, using the same column specs
    (label, align, get, and the title flag) that render_pdf uses."""

    def cell_html(col, row):
        text = html.escape(str(col["get"](row)))
        if col.get("title") and row.get("bold_title"):
            text = f"<strong>{text}</strong>"
        if col.get("title"):
            url = song_url(row.get("id"))
            if url:
                text = f'<a href="{html.escape(url)}">{text}</a>'
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


def render_json(path: Path, subtitle: str, rows, cols, prepared_date, footer=None) -> None:
    """Render one report as JSON: {title, prepared_by, prepared_date, footer,
    rows: [{col.key: col.json(row), ...}, ...]}.

    Each column contributes its value under col["key"] using col["json"] (falls
    back to col["get"]'s display string if a column has no "json"). A row whose
    title was bold in the PDF/HTML (a song new to the congregation within the
    report's period) gets an added "new": true.
    """
    footer_lines = ([footer] if isinstance(footer, str) else footer) if footer else []

    def row_json(row):
        d = {col["key"]: col.get("json", col["get"])(row) for col in cols}
        if row.get("bold_title"):
            d["new"] = True
        return d

    data = {
        "title": subtitle,
        "prepared_by": PREPARED_BY,
        "prepared_date": prepared_date.isoformat(),
        "footer": footer_lines,
        "count": len(rows),
        "rows": [row_json(row) for row in rows],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def render_report(name: str, subtitle: str, description: str, rows, cols, prepared_date,
                  footer=None) -> dict:
    """Render one report as a PDF (in OUT_DIR), and as HTML and JSON pages (in
    SITE_DIR), and return the metadata the home page needs to link to it."""
    render_pdf(OUT_DIR / f"{name}.pdf", subtitle, rows, cols, prepared_date, footer=footer)
    render_html(SITE_DIR / f"{name}.html", subtitle, rows, cols, prepared_date, footer=footer)
    render_json(SITE_DIR / f"{name}.json", subtitle, rows, cols, prepared_date, footer=footer)
    return {"name": name, "title": subtitle, "description": description, "count": len(rows)}


def render_oldness_report(path: Path, scored_sets: list[dict], prepared_date: date) -> None:
    sections = []
    for song_set in scored_sets:
        rows = "".join(
            f"<tr><td>{html.escape(song['title'])}</td>"
            f'<td class="num">{song["prior_uses"]}</td></tr>'
            for song in song_set["songs"]
        )
        sections.append(f"""\
<section class="oldness-set">
<h2>{song_set['service_date']:%Y-%m-%d}</h2>
<p class="oldness-score">Oldness score: {song_set['oldness_score']}</p>
<table>
<thead><tr><th>Song</th><th class="num">Uses before this set week</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</section>""")
    sets_html = "".join(sections) or "<p>No scheduled song sets found.</p>"

    path.write_text(f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recent Set Oldness Scores</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header>
<h1>Recent Set Oldness Scores</h1>
<p class="meta">Prepared by {html.escape(PREPARED_BY)} &middot; {prepared_date:%-m/%-d/%Y}
&middot; <a href="index.html">Home</a></p>
</header>
<p>Populated plans through the next 90 days are eligible. Each song's count
includes Sunday Morning Worship plans before the Monday that set's week began.
Uses from that week onward are excluded. The set's oldness score is the sum of
the listed counts.</p>
{sets_html}
</body>
</html>
""", encoding="utf-8")


def render_index(reports: list[dict], prepared_date) -> None:
    """Render the docs/index.html home page linking to each report."""
    cards = "".join(f"""\
<li class="card">
<h2><a href="{r['name']}.html">{html.escape(r['title'])}</a></h2>
<p>{html.escape(r['description'])}</p>
<p class="count">{r['count']} {r.get('count_unit', 'songs')}</p>
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
<p class="meta">Prepared by {html.escape(PREPARED_BY)}
<a class="candidate-heart" href="SongCandidates.html" aria-label="Song candidates"
title="Song candidates">&#9829;</a> &middot; {prepared_date:%-m/%-d/%Y}</p>
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
    all_time, yearly, all_time_keys = discover_reports()
    rows = build_rows(catalog, all_time, yearly, all_time_keys)
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
            "title_display": s["title"] + format_keys_summary(all_time_keys.get(s["title"], {})),
            "all_time": at,
            "years": s["months"],
            "last_scheduled": info["last_scheduled"] if info else None,
            "id": info["id"] if info else None,
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
    christmas_rows = build_rows(load_catalog(christmas=True), all_time, yearly, all_time_keys)
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

    # Song Keys: every song with a recorded key, sorted alphabetically, listing
    # each key it's been played in and how many times, most-used key first.
    key_rows = build_key_rows(all_time_keys, catalog)
    reports.append(render_report(
        "SongKeys", "Songs by Key",
        "Every song with a recorded key, listing each key it's been played in "
        "and how many times, most-used key first.",
        key_rows, make_key_columns(), prepared_date))

    oldness_sets = build_recent_set_oldness(all_time, prepared_date)
    render_oldness_report(SITE_DIR / "OldnessScores.html", oldness_sets, prepared_date)
    reports.append({
        "name": "OldnessScores",
        "title": "Recent Set Oldness Scores",
        "description": "Historical song usage and oldness scores for the most recent service sets.",
        "count": len(oldness_sets),
        "count_unit": "sets",
    })

    candidate_summary = None
    import render_song_candidates
    candidate_path = render_song_candidates.latest_candidate_file()
    if candidate_path:
        candidate_data = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate_summary = render_song_candidates.summary(candidate_data, candidate_path)
        candidate_html = SITE_DIR / f"{candidate_summary['name']}.html"
        render_song_candidates.render_html(candidate_html, candidate_data)

    render_index(reports, prepared_date)

    recent_labels = [year_labels[i] for i in indices]
    print(f"Wrote {len(rows)} songs to {OUT_DIR}/*.pdf and {SITE_DIR}/*.html,*.json:")
    print("  ByTitle")
    print("  ByAllTime")
    print(f"  ByRecentUsage ({len(by_recent)} songs, "
          f"recent years: {', '.join(recent_labels)})")
    print(f"  OldnessScores ({len(oldness_sets)} sets)")
    print(f"  RecentMonths ({len(by_month)} songs, "
          f"months: {', '.join(month_labels)})")
    print(f"  NeverPlayed ({len(never_played)} songs)")
    print(f"  Christmas ({len(by_christmas)} songs)")
    print(f"  NotPlayed4Years ({len(stale)} songs, "
          f"last scheduled before {cutoff:%Y-%m-%d})")
    print(f"  SongKeys ({len(key_rows)} songs)")
    print(f"Year columns: {', '.join(year_labels)}")


if __name__ == "__main__":
    main()
