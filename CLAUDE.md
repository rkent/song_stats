# song_stats

Generates song-usage reports for Creekside's Sunday Morning Worship Services from
Planning Center data. See [song_stats.py](song_stats.py)'s module docstring for the
full list of reports and how they're built; [all_time_ranking.py](all_time_ranking.py)
is a separate, simpler all-time-usage ranking script. Weekly song suggestions (see
below) are saved as JSON under `song_candidates/`; [render_song_candidates.py](render_song_candidates.py)
turns those JSON files into HTML pages.

## Single-week song candidate list

Use this workflow when the user asks for songs for one upcoming week (e.g.
"Suggest songs for the next week"). This is a ranked shortlist, not a request
to prepare multiple weeks.

1. Regenerate the reports with `python song_stats.py` unless the user says the
   existing `docs/`/`output/` reports are fresh enough.
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
   needed). Use this shape:
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
   once in its corresponding array. Then run `python render_song_candidates.py
   song_candidates/<file>.json` to render the HTML page.

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
