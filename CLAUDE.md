# song_stats

Generates song-usage reports for Creekside's Sunday Morning Worship Services from
Planning Center data. See [song_stats.py](song_stats.py)'s module docstring for the
full list of reports and how they're built; [all_time_ranking.py](all_time_ranking.py)
is a separate, simpler all-time-usage ranking script. Weekly song suggestions (see
below) are saved as JSON under `song_sets/`; [render_song_sets.py](render_song_sets.py)
turns one of those JSON files into an HTML page and a PDF.

## Single-week song candidate list

Use this workflow when the user asks for songs for one upcoming week (e.g.
"Suggest songs for the next week"). This is a ranked shortlist, not a request
to prepare four weekly sets; use the next section for requests for multiple
weeks or sets.

1. Regenerate the reports with `python song_stats.py` unless the user says the
   existing `_site/`/`output/` reports are fresh enough.
2. Find the first upcoming Planning Center plan with no song items. Check plans
   from today forward via the API, ordered by date; choose the earliest plan
   with zero songs. A later populated plan must not delay the start. If the
   search window contains no empty plans, extend it until one is found.
3. Build eligible candidates for that plan using the hard constraints and
   defaults in the following section, evaluated against this plan's date.
   Honor `include_songs.txt` as a priority and allow its recency exception, but
   it does not override the 3-30 all-time range or `exclude_songs.txt`; report
   any conflict. User-named songs follow the explicit-request exceptions below.
4. Choose and present a coherent top five as the proposed lineup. Keep its
   oldness score (sum of the five all-time counts) between 30 and 120, aiming
   near 68; favor songs with solid usage in the last six months and a balanced
   mix of familiar and fresher songs. Then list two distinct, eligible
   alternatives for each proposed song (10 alternatives total), grouped by the
   proposed song they could replace. Choose options that keep a replacement
   lineup's score in range and near 68. If hard constraints leave fewer than
   two alternatives for a proposed song, explain the shortfall rather than
   relaxing a hard constraint. For each alternative, identify the proposed
   song it could replace, then give a concise fit-based reason similar to the
   proposed-song reasons. Do not mention oldness scores or score arithmetic in
   alternative reasons; use score only to guide selection and ordering.
5. For every song, include its title, all-time and recent-six-month play
   counts, most-played key(s), last-scheduled date, and a concise reason it
   fits. Read the recent-six-month count from `RecentMonths.json`'s `total`
   field (use 0 if the song is absent); do not use `ByRecentUsage.json`'s
   `recent` field as a six-month count. Clearly identify the top five as the
   proposed lineup and the rest as replacements, and include the computed
   oldness score for the top five.
6. Save the shortlist, including all alternatives presented, to
   `song_candidates/<today's date, YYYY-MM-DD>.json` (create the directory if
   needed). Keep this separate from `song_sets/`, which is reserved for
   multi-week sets. Use this shape:
   ```json
   {
     "generated_date": "YYYY-MM-DD",
     "service_date": "YYYY-MM-DD",
     "oldness_score": 68,
     "proposed_songs": [
       {"title": "...", "all_time": 3, "recent_6mo": 3, "keys": "G, Bb",
        "last_scheduled": "YYYY-MM-DD", "reason": "..."}
     ],
     "alternatives": [
       {"title": "...", "all_time": 3, "recent_6mo": 3, "keys": "G, Bb",
        "last_scheduled": "YYYY-MM-DD", "reason": "..."}
     ]
   }
   ```
   Include each proposed song and each alternative shown to the user exactly
   once in its corresponding array. Don't save this shortlist as a four-week
   song-set JSON. Then run `python render_song_candidates.py
   song_candidates/<file>.json` to render the HTML page and add its link to
   `_site/index.html`.

## Suggesting songs for upcoming weeks

When asked to suggest songs (e.g. "suggest 5 songs for the next few weeks"):

1. Regenerate the reports first (`python song_stats.py`) so the suggestions reflect
   current data, unless the user says the existing `_site/`/`output/` reports are
   fresh enough.
2. Find the next four upcoming plans with no songs entered in Planning Center.
  Start with the first upcoming empty plan, then continue forward, skipping
  any plans that already have songs until four empty weeks are selected. Do
  not wait until after the last populated future plan to begin. Check live via
  the API, e.g.:
   ```python
   from datetime import date, timedelta
   from song_stats import fetch_service_type_id, fetch_recent_plans, \
       fetch_plan_song_items, SERVICE_TYPE_NAME
   st = fetch_service_type_id(SERVICE_TYPE_NAME)
   for p in fetch_recent_plans(st, date.today(), date.today() + timedelta(days=90)):
       d = p["attributes"]["sort_date"][:10]
       print(d, len(fetch_plan_song_items(st, p["id"])), "songs")
   ```
    For example, if 10/4 has songs, 10/11, 10/18, and 10/25 are empty, and 11/1
    has songs, suggest 10/11, 10/18, 10/25, and then 11/8 if it is empty.
3. Pull candidates primarily from `_site/RecentMonths.json` and
   `_site/ByRecentUsage.json` — these show what the congregation currently knows.
   Cross-check `_site/SongKeys.json` for each candidate's usual key(s).
4. Forced inclusions: check for `include_songs.txt` in the project root (same
   one-title-per-line format as `exclude_songs.txt`; blank lines and lines
   starting with `#` are ignored). Every title listed there must appear
   exactly once somewhere across the 4 weeks being generated — period. This
   overrides the normal recency-based selection and, for that one occurrence,
   the 8-week no-repeat rule in step 4 below (a forced song may legitimately
   land less than 8 weeks after its last outing). It does not override the
   3-30 all-time play range or `exclude_songs.txt` by itself: if a title is on
   both `include_songs.txt` and `exclude_songs.txt`, or falls outside 3-30
   plays, skip it and flag the conflict to the user instead of forcing it in.
   However, if the user explicitly requests a named song, that direct request
   overrides the 3-30 play-count range and the 8-week recency rule for that
   song. Include it once, report its actual counts and dates, and label the
   exception clearly; an explicit request does not override `exclude_songs.txt`
   unless the user also explicitly asks to use an excluded song. Place each
   forced song in whichever week fits it best (thematically, or by oldness
   balance), never more than once across the set, then fill the remaining slots
   using the normal process below.
5. Hard constraints — never violate these:
   - Exclude any song with an all-time play count under 3 or over 30 (check
     the `all_time` field in `ByAllTime.json`/`ByRecentUsage.json` or sum
     `usage_cache.json`) — the eligible range is 3-30 plays, inclusive, unless
     the user explicitly requested that named song; in that case include it,
     preserve the actual count, and clearly flag the exception.
   - Exclude any song scheduled within the last 8 weeks — check `last_scheduled`
     (or `RecentMonths.json`) against the target date and skip anything inside
     that 8-week window, so no song repeats more often than every 8 weeks,
     unless the user explicitly requested that named song; clearly flag the
     exception.
   - Exclude any song (by title) listed in `exclude_songs.txt` (one title per
     line; blank lines and lines starting with `#` are ignored). Check this list
     before suggesting.
6. Apply these defaults unless the user says otherwise:
   - Favor songs with solid recent usage (played multiple times in the last 6
     months) over songs never played or not played in years — the goal is songs
     the congregation can sing confidently, not novelty.
   - Occasionally include one under-used or new-to-rotation song (rows with
     `"new": true` in `ByRecentUsage.json`, still subject to the 3-30 all-time
     range above — so not from `NeverPlayed.json`, which is all below that
     floor) if the user wants variety, but call it out explicitly as a
     "stretch" pick rather than mixing it in silently.
   - Skip Christmas songs (`Christmas.json`) unless the target date is in the
     Christmas season.
   - Note each suggested song's most-played key(s) from `SongKeys.json`'s
     `keys` list (most-played first) so the suggestion is immediately usable
     for planning.
7. Present exactly 5 songs (unless asked for a different number), each with:
   title, all-time/recent play count, most-played key, and a one-line reason it
   fits (e.g. "known but not recently played" / "congregation favorite").
8. Save the suggestion as JSON to `song_sets/<today's date, YYYY-MM-DD>.json`
   (creating the `song_sets/` directory if needed), shaped as:
   ```json
   {
     "generated_date": "YYYY-MM-DD",
     "prepared_by": "Kent James",
     "sets": [
       {
         "date": "YYYY-MM-DD",
         "oldness_score": 73,
         "theme": null,
         "songs": [
           {"title": "...", "all_time": 3, "recent_6mo": 3, "keys": "G, Bb",
            "last_scheduled": "YYYY-MM-DD", "reason": "...", "stretch": false}
         ]
       }
     ]
   }
   ```
   One entry in `sets` per week presented, in the same order shown to the user.
   `last_scheduled` is each song's `last_scheduled` value from the source
   report (`RecentMonths.json`/`ByRecentUsage.json`), not the target date.
   `theme` is the user's requested theme/topic for that week if any, else null.
   `stretch` is true only for the explicit "stretch" pick called out in step 5.
   After saving, mention that `python render_song_sets.py song_sets/<file>.json`
   will turn it into a shareable HTML page and PDF (in `_site/` and `output/`)
   — run it only if the user asks for those.
9. If the user gives a theme, sermon topic, or season, prioritize thematically
   fitting songs among the eligible candidates over pure usage stats. Each
   catalog song has a free-text "Themes" tag list from Planning Center (not
   shown in any report or cached in `usage_cache.json`) — fetch it live via
   `fetch_songs()` in `song_stats.py` (e.g. `python -c "from song_stats import
   fetch_songs; ..."`) and match candidates' `themes` attribute against the
   requested topic. Tagging is inconsistent (some songs have rich curated lists,
   others one word, some typos) — use it to narrow/prioritize, not as a hard
   filter, and don't assume a song lacking a matching tag is actually off-topic.

## Set "oldness" heuristic

For a proposed set of 5 songs, sum each song's all-time play count (the same
`all_time` field used in the hard constraint above). That sum is the set's
"oldness" score — lower means a fresher/newer-feeling set, higher means a more
familiar/well-known set. Keep each week's oldness score between 30 and 120,
but treat that as the outer bound, not the target: aim for something close to
68, and lean toward more lesser-known/fresher songs rather than defaulting to
the most familiar ones whenever the mix allows it. Mention the computed score
when presenting the set.

Don't fabricate play counts or dates — read them from the generated reports or
`usage_cache.json`, don't estimate.
