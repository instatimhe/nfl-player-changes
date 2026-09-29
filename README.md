# nfl-player-changes

A change log of public NFL player facts, checked every 30 minutes during the regular
season: team, position, injury designation, roster status and depth-chart position, for
players on an NFL team at QB, RB, WR, TE or K.

**Data source: [Sleeper](https://sleeper.com)'s public API** (`api.sleeper.app` and
`api.sleeper.com`). Sleeper's API is free to use for non-commercial purposes. This repo is
a **non-commercial, personal project**. It republishes a small subset of the player facts
Sleeper already serves publicly, with a record of when each one changed. It is not
affiliated with or endorsed by Sleeper, and it adds no data of its own.

## What this repo is, and what it isn't

- It **is** a record of *when a change was first seen*, bracketed between two checks.
- It **isn't** a source of news or of when anything actually happened. Every change carries
  `detected_between: [earlier check, later check]`, never a single "changed at" time.
- It **isn't** complete. Anything that changed and changed back between two checks is not
  recorded (see "Flip-flops"). Sleeper's own data can lag the real world, especially depth
  charts.
- It holds **no fantasy-league data**: no league, team, roster, manager or user identifiers
  of any kind. Only NFL facts. A structural guard (below) blocks any commit that could carry
  one.

## Files

| path | what |
|---|---|
| `state/<TEAM>.json` | the current state: one player per line, `"player_id": {fields}`. Null fields are omitted. |
| `log/YYYY/MM/DD.jsonl` | one line per change seen by a run on that UTC day |
| `runs/YYYY/MM/DD.jsonl` | one line per run, **including runs that found nothing and runs that failed** |
| `latest.json` | the last run's time and status, and each source's last successful read |

A run commits all four together, in one commit, every time. A commit that says "0 lines" is
evidence that the sources were read and nothing had changed. A missing commit is not
evidence of anything.

### Fields

From the weekly projections feed (every run): `team`, `position`, `injury_status`,
`injury_body_part`, `injury_notes`, `injury_start_date`, `news_updated`, `team_changed_at`.
From the full player list (up to 3 times a day): `full_name`, `status`, `active`,
`depth_chart_position`, `depth_chart_order`, `practice_participation`,
`practice_description`. Timestamps are ISO 8601 UTC strings.

### Change-log lines

An illustrative line (not a real change):

```json
{"kind":"change","player_id":"7021","team":"PIT","field":"injury_status","old":"Questionable","new":"Out","source":"projections_feed","detected_between":["2026-09-29T05:46:18Z","2026-09-29T06:16:40Z"]}
```

- `kind`: `change` (one field), `enter` (a player came into scope; `record` holds his fields),
  `exit` (left scope: `reason` is `team_to_null`, `position_out_of_scope` or `id_vanished`;
  `record` holds his last state; he is then dropped), or `fill` (the first full-list read of
  a player who entered between reads).
- `source`: which read saw it, `projections_feed` or `players_nfl`. A change seen by the
  full-list read is bracketed by the previous full-list read, so it says "within the last
  several hours", not "within 30 minutes".
- `detected_between`: `[lower, upper]`. `lower` is when the source that reports the change
  last saw the old value (the start of that read); `upper` is when this run's reads finished.

## Failed runs

A source that can't be read is a **failed run**, recorded in `runs/` with the reason, never
a quiet no-change run. A failure is a missing or empty feed, a feed for a different week or
season than Sleeper's NFL state names, a response of the wrong shape, or the full list being
unreachable when it is due. The failed source's last-success time doesn't move, so the next
successful read's `detected_between` spans the gap. A skipped or dropped run works the same
way: every run diffs against the last *committed* state, so a gap costs timing precision,
never a change.

Outside the regular season Sleeper's weekly feed doesn't exist, so every run records a
failed run with `not_regular_season`. That is expected.

## Flip-flops

A field that is back at its committed value by the next committed run produces no line: it
is collapsed. A field that moved and did not return is one line, old to new, even if it
passed through other values in between. A change committed by one run and reverted by the
next is two lines; nothing committed is ever rewritten.

## The guard

`nflchanges/guard.py` runs before every commit and blocks it on any problem: a run of 12 or
more digits anywhere in a data file, a key that is not on the allow-list for its file, a
value of the wrong type, a team code outside the 32 NFL teams, a player id longer than 6
digits, or a file outside `state/`, `log/`, `runs/` and `latest.json`.

## Running it

Standard-library Python 3.9+, no dependencies.

```sh
python3 -m nflchanges.writer --repo .                       # one live run
python3 -m nflchanges.writer --repo . --recorded DIR --at 2026-09-29T06:44:30Z
python3 -m nflchanges.guard --repo . --all
python3 -m unittest discover -s tests -t .
```

`.github/workflows/write.yml` runs the writer: triggered by `workflow_dispatch` from an
external scheduler at minutes 5 and 35, with GitHub's `schedule` at minutes 20 and 50 as a
backup that writes nothing if the last run is under 25 minutes old.
