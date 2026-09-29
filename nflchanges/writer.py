"""One writer run: read the sources, diff against the last COMMITTED state, write the new
state, the change log and a run stamp. Committing is the workflow's job, not this module's.

RULES (each has a test in tests/test_writer.py)
 1. Every run writes a run stamp, including a run that found no change, and a run whose
    source failed. A source failure is a FAILED run, never a no-change run: missing or
    empty feed, a feed whose week or season is not the one /state/nfl names, a shape
    assert failure, or the player list unreachable when it is due. A failed source's
    `last_ok_at` does not advance, so the next successful read's `detected_between`
    spans back to the last successful one.
 2. The diff base is the committed state on disk (the workflow resets to the branch tip
    first, and this module refuses to run over uncommitted data files). A dropped run
    therefore loses timing precision, never a change.
 3. Each change is stamped `detected_between: [lower, this run]`, never "changed at".
    `lower` is the last time the SOURCE THAT NOW REPORTS the field observed the old value:
    the feed's last successful run for feed fields, the player list's last successful
    pull for player-list fields (so a status change it reports reads "within the last
    pull interval", not "within 30 minutes"). Players the feed does not carry are tracked
    in latest.json's `feed_absent`, with the time their feed fields were last observed.
 4. Every change names its `source`: projections_feed or players_nfl. Field ownership is
    fixed (record.FEED_FIELDS / record.PLAYERS_FIELDS); the player list supplies feed
    fields only for players the feed does not carry, so two sources never alternate on
    one field.
 5. Flip-flops. A field that is back at its committed value when the next run is
    committed produces no line: it is collapsed, because nothing but committed state is
    ever diffed. A field that moved and did not return (A->B, or A->B->C across a
    dropped run) is ONE line, old=A new=C. A change committed at run k and reverted at
    run k+1 is two lines; nothing already committed is ever rewritten.
 6. Scope: on an NFL team and at a fantasy skill position or K. The tracked set is
    everyone in scope now plus everyone in the committed state. A player leaving scope
    gets one `exit` line and is dropped. A player the feed stops carrying is NOT an exit:
    his feed fields carry over until a source says otherwise.
 7. Only record.ALL_FIELDS are ever copied; the feed's `metadata` object never is.

FILES
  state/<TEAM>.json        one player per line
  log/YYYY/MM/DD.jsonl     one line per change (kinds: change, enter, exit, fill)
  runs/YYYY/MM/DD.jsonl    one line per run
  latest.json              the last run's summary and each source's last success
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

from . import record as R
from .sources import LiveSource, RecordedSource, SourceUnavailable

# /players/nfl is read on the first run at or after each of these UTC times, once per
# slot. Rationale: docs/change-feed-trigger.md in the private repo (proposed 2026-09-29).
SLOTS_UTC = ("07:00", "14:30", "22:00")
MAX_PLAYERS_ATTEMPTS_PER_SLOT = 3
# Shape floors. Measured 2026-09-29: the week-4 feed had 898 rows with a team (3,305
# rows in all) and /players/nfl had 12,229 entries. The floors sit far below both, so
# they catch a truncated or empty response, not ordinary churn.
MIN_FEED_TEAMED_ROWS = 500
MIN_PLAYERS_ENTRIES = 5000
DATA_PATHS = ("state", "log", "runs", "latest.json")
_PID = re.compile(r"\d{1,6}|[A-Z]{2,3}")


class Malfunction(Exception):
    """Not a source failure: the writer cannot run safely at all (exit 2, commit nothing)."""


# ---------------------------------------------------------------- committed state

def load_latest(root):
    path = Path(root) / "latest.json"
    if not path.exists():
        return None
    latest = json.loads(path.read_text())
    if latest.get("schema_version") != R.SCHEMA_VERSION:
        raise Malfunction(f"latest.json schema_version {latest.get('schema_version')!r}")
    return latest


def load_state(root):
    state = {}
    for path in sorted((Path(root) / "state").glob("*.json")):
        for pid, rec in json.loads(path.read_text()).items():
            if pid in state:
                raise Malfunction(f"player {pid} in two shards")
            state[pid] = rec
    return state


def assert_committed(root):
    root = Path(root)
    if not (root / ".git").exists():
        return
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", *DATA_PATHS],
                         capture_output=True, text=True, check=True).stdout
    if out.strip():
        raise Malfunction("uncommitted data files; the writer diffs only against committed "
                          "state:\n" + out)


# ---------------------------------------------------------------- source validation

def validate_state(st):
    if not isinstance(st, dict):
        raise SourceUnavailable("shape", "state is not an object")
    if st.get("season_type") != "regular":
        # Offseason, preseason and postseason: no regular-season weekly feed to read.
        raise SourceUnavailable("not_regular_season", f"season_type={st.get('season_type')}")
    week, season = st.get("week"), st.get("season")
    if not (isinstance(week, int) and 1 <= week <= 18):
        raise SourceUnavailable("shape", f"week={week!r}")
    if not (isinstance(season, str) and re.fullmatch(r"20\d\d", season)):
        raise SourceUnavailable("shape", "season")
    return int(season), week


def validate_feed(rows, season, week):
    """{player_id: player object} for a projections feed, or SourceUnavailable."""
    if not isinstance(rows, list):
        raise SourceUnavailable("shape", "feed is not a list")
    if not rows:
        raise SourceUnavailable("empty")
    out, teamed = {}, 0
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("player"), dict):
            raise SourceUnavailable("shape", "row without a player object")
        if r.get("week") != week or str(r.get("season")) != str(season):
            raise SourceUnavailable("week_mismatch",
                                    f"asked {season} week {week}, row has {r.get('season')} week {r.get('week')}")
        pid = r.get("player_id")
        if not (isinstance(pid, str) and _PID.fullmatch(pid)) or pid in out:
            raise SourceUnavailable("shape", "player_id")
        pl = r["player"]
        missing = [k for k in R.FEED_FIELDS + ("fantasy_positions",) if k not in pl]
        if missing:
            raise SourceUnavailable("shape", "player object lacks " + ",".join(missing))
        out[pid] = pl
        teamed += bool(pl.get("team"))
    if teamed < MIN_FEED_TEAMED_ROWS:
        raise SourceUnavailable("too_few_rows", f"{teamed} rows with a team")
    return out


def validate_players(d):
    if not isinstance(d, dict):
        raise SourceUnavailable("shape", "player list is not an object")
    if len(d) < MIN_PLAYERS_ENTRIES:
        raise SourceUnavailable("too_few_rows", f"{len(d)} entries")
    need = R.ALL_FIELDS + ("fantasy_positions",)
    for pid, p in d.items():
        if not isinstance(p, dict):
            raise SourceUnavailable("shape", "entry is not an object")
        if R.in_scope(p):
            missing = [k for k in need if k not in p]
            if missing:
                raise SourceUnavailable("shape", "entry lacks " + ",".join(missing))
    return d


# ---------------------------------------------------------------- schedule

def current_slot(now, slots=SLOTS_UTC):
    """The most recent slot boundary at or before `now` (a datetime)."""
    cands = []
    for day in (now.date(), now.date() - dt.timedelta(days=1)):
        for s in slots:
            h, m = map(int, s.split(":"))
            t = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=dt.timezone.utc)
            if t <= now:
                cands.append(t)
    return max(cands)


def players_plan(latest, now, slots=SLOTS_UTC):
    """(due, slot_iso, attempts_already_made_in_this_slot)."""
    slot = current_slot(now, slots)
    slot_iso = slot.strftime("%Y-%m-%dT%H:%M:%SZ")
    src = ((latest or {}).get("sources") or {}).get(R.SOURCE_PLAYERS) or {}
    last_ok = src.get("last_ok_at")
    due = last_ok is None or R.parse_iso(last_ok) < slot
    attempts = src.get("slot_attempts", 0) if src.get("slot") == slot_iso else 0
    return due, slot_iso, attempts


# ---------------------------------------------------------------- the diff

def build(prev, latest, feed, players, now_iso, until_iso=None):
    """New state and log lines.

    prev     committed state {pid: record}
    latest   committed latest.json (None before the baseline)
    feed     {pid: feed player object} if the feed was read OK this run, else None
    players  {pid: /players/nfl entry} if the list was read OK this run, else None
    now_iso  when this run started reading (the next run's lower bound)
    until_iso when its reads finished (this run's upper bound; default now_iso)
    """
    until = until_iso or now_iso
    srcs = (latest or {}).get("sources") or {}
    feed_lo = (srcs.get(R.SOURCE_FEED) or {}).get("last_ok_at")
    players_lo = (srcs.get(R.SOURCE_PLAYERS) or {}).get("last_ok_at")
    absent_prev = (latest or {}).get("feed_absent") or {}

    def feed_bound(pid):
        return absent_prev.get(pid) or feed_lo

    cands = set(prev)
    if feed is not None:
        cands |= {pid for pid, o in feed.items() if R.in_scope(o)}
    if players is not None:
        cands |= {pid for pid, o in players.items() if R.in_scope(o)}

    new, lines, absent = {}, [], {}
    for pid in sorted(cands, key=R.pid_key):
        old = prev.get(pid)
        f = feed.get(pid) if feed is not None else None
        p = players.get(pid) if players is not None else None

        # Which object speaks for the FEED fields this run.
        if f is not None:
            fsrc, fobj = R.SOURCE_FEED, f
        elif p is not None and (feed is not None or old is None or pid in absent_prev):
            fsrc, fobj = R.SOURCE_PLAYERS, p   # the feed does not carry him
        else:
            fsrc, fobj = None, None

        if fobj is not None and not R.in_scope(fobj):
            if old is not None:
                reason = "team_to_null" if not fobj.get("team") else "position_out_of_scope"
                lines.append({"kind": "exit", "player_id": pid, "team": old.get("team"),
                              "reason": reason, "source": fsrc,
                              "detected_between": [feed_bound(pid), until], "record": old})
            continue

        if fobj is None:
            if old is None:
                continue
            gone_from_feed = feed is not None and f is None
            if players is not None and p is None and gone_from_feed:
                seen = max(x for x in (feed_bound(pid), players_lo) if x)
                lines.append({"kind": "exit", "player_id": pid, "team": old.get("team"),
                              "reason": "id_vanished", "source": R.SOURCE_PLAYERS,
                              "detected_between": [seen, until], "record": old})
                continue
            # No source observed his feed fields this run: they carry over unchanged.
            rec = {k: old[k] for k in R.FEED_FIELDS if k in old}
            if gone_from_feed or pid in absent_prev:
                absent[pid] = absent_prev.get(pid) or feed_lo
        else:
            rec = R.pick(fobj, R.FEED_FIELDS)
            if fsrc == R.SOURCE_PLAYERS:
                absent[pid] = now_iso

        if p is not None:
            rec.update(R.pick(p, R.PLAYERS_FIELDS))
        elif old is not None:
            rec.update({k: old[k] for k in R.PLAYERS_FIELDS if k in old})
        new[pid] = rec

        team = rec.get("team")
        if old is None:
            lo = feed_lo if fsrc == R.SOURCE_FEED else players_lo
            lines.append({"kind": "enter", "player_id": pid, "team": team, "source": fsrc,
                          "detected_between": [lo, until], "record": rec})
            continue
        if p is not None and "full_name" not in old and "full_name" in rec:
            # First player-list read for a player who entered between pulls: a fill, not
            # a change (these fields had never been observed).
            lines.append({"kind": "fill", "player_id": pid, "team": team,
                          "source": R.SOURCE_PLAYERS, "detected_between": [players_lo, until],
                          "record": {k: rec[k] for k in R.PLAYERS_FIELDS if k in rec}})
            fields = R.FEED_FIELDS
        else:
            fields = R.ALL_FIELDS
        for k in fields:
            if old.get(k) != rec.get(k):
                is_feed = k in R.FEED_FIELDS
                lines.append({"kind": "change", "player_id": pid, "team": team, "field": k,
                              "old": old.get(k), "new": rec.get(k),
                              "source": fsrc if is_feed else R.SOURCE_PLAYERS,
                              "detected_between": [feed_bound(pid) if is_feed else players_lo,
                                                   until]})
    return new, lines, absent


# ---------------------------------------------------------------- files

def _day_path(root, kind, when):
    d = Path(root) / kind / when[:4] / when[5:7]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{when[8:10]}.jsonl"


def write_state(root, state):
    sd = Path(root) / "state"
    sd.mkdir(exist_ok=True)
    by = {}
    for pid, rec in state.items():
        by.setdefault(rec["team"], {})[pid] = rec
    files = {team: R.encode_shard(recs) for team, recs in by.items()}
    for f in sd.glob("*.json"):
        if f.stem not in files:
            f.unlink()
    for team, blob in files.items():
        path = sd / f"{team}.json"
        if not path.exists() or path.read_bytes() != blob:
            path.write_bytes(blob)


def append_lines(root, kind, when, lines):
    if not lines:
        return
    with open(_day_path(root, kind, when), "a") as fh:
        for line in lines:
            fh.write(R.json_line(line))


# ---------------------------------------------------------------- one run

def run(root, source, now_iso, trigger="local", slots=SLOTS_UTC, skip_if_within_min=None):
    """Returns a summary dict. Writes files unless the run is skipped or stale."""
    root = Path(root)
    assert_committed(root)
    latest = load_latest(root)
    now = R.parse_iso(now_iso)
    if latest and R.parse_iso(latest["last_run_at"]) >= now:
        return {"skipped": "stale", "detail": f"a run at {latest['last_run_at']} is already committed"}
    if (skip_if_within_min and latest
            and now - R.parse_iso(latest["last_run_at"]) < dt.timedelta(minutes=skip_if_within_min)):
        return {"skipped": "recent", "detail": f"last run {latest['last_run_at']}"}

    prev = load_state(root)
    baseline_needed = not (latest or {}).get("baseline_at")
    srcs_prev = (latest or {}).get("sources") or {}
    reasons = []
    stamp_sources = {}

    # --- feed (needs the week from /state/nfl)
    feed, week = None, None
    fprev = srcs_prev.get(R.SOURCE_FEED) or {}
    try:
        season, week = validate_state(source.state())
        feed = validate_feed(source.feed(season, week), season, week)
        stamp_sources[R.SOURCE_FEED] = {"result": "ok", "week": week, "players": len(feed),
                                        "detected_between": [fprev.get("last_ok_at"), now_iso]}
    except SourceUnavailable as e:
        code = f"{e.code} ({e.detail})" if e.detail else e.code
        reasons.append(f"{R.SOURCE_FEED}: {code}")
        stamp_sources[R.SOURCE_FEED] = {"result": "failed", "error": code, "week": week}

    # --- player list, when due
    players, attempted = None, False
    pprev = srcs_prev.get(R.SOURCE_PLAYERS) or {}
    due, slot_iso, attempts = players_plan(latest, now, slots)
    if not due:
        stamp_sources[R.SOURCE_PLAYERS] = {"result": "not_due", "slot": slot_iso}
    elif attempts >= MAX_PLAYERS_ATTEMPTS_PER_SLOT:
        code = f"attempts_exhausted ({attempts} in slot {slot_iso})"
        reasons.append(f"{R.SOURCE_PLAYERS}: {code}")
        stamp_sources[R.SOURCE_PLAYERS] = {"result": "failed", "error": code, "slot": slot_iso}
    else:
        attempts, attempted = attempts + 1, True
        try:
            players = validate_players(source.players())
            stamp_sources[R.SOURCE_PLAYERS] = {"result": "ok", "slot": slot_iso,
                                               "detected_between": [pprev.get("last_ok_at"), now_iso]}
        except SourceUnavailable as e:
            code = f"{e.code} ({e.detail})" if e.detail else e.code
            reasons.append(f"{R.SOURCE_PLAYERS}: {code}")
            stamp_sources[R.SOURCE_PLAYERS] = {"result": "failed", "error": code, "slot": slot_iso}

    # The upper bound is when the reads FINISHED: a response can reflect a change made
    # after the run started (a 15 MB player list takes a while). The lower bound stays the
    # previous run's start, which is conservative the same way.
    until_iso = getattr(source, "completed_at", None) or now_iso
    for v in stamp_sources.values():
        if "detected_between" in v:
            v["detected_between"][1] = until_iso

    # --- diff
    lines, new, absent = [], prev, (latest or {}).get("feed_absent") or {}
    if baseline_needed:
        if feed is not None and players is not None:
            new, _, absent = build({}, None, feed, players, now_iso, until_iso)
            absent = {pid: now_iso for pid in new if pid not in feed}
            status = "baseline"
        else:
            reasons.append("baseline: needs both sources in one run")
            status = "failed"
    else:
        if feed is not None or players is not None:
            new, lines, absent = build(prev, latest, feed, players, now_iso, until_iso)
        status = "failed" if reasons else "ok"

    # --- latest.json
    feed_ok, players_ok = feed is not None, players is not None
    if status == "failed" and baseline_needed:
        feed_ok = players_ok = False   # nothing was committed as a baseline
    feed_latest = dict(fprev)
    feed_latest.update(last_attempt_at=now_iso, last_result=stamp_sources[R.SOURCE_FEED]["result"])
    if feed_ok:
        feed_latest.update(last_ok_at=now_iso, week=week)
    players_latest = dict(pprev)
    players_latest["last_result"] = stamp_sources[R.SOURCE_PLAYERS]["result"]
    if due:
        players_latest.update(slot=slot_iso, slot_attempts=attempts)
    if attempted:
        players_latest["last_attempt_at"] = now_iso
    if players_ok:
        players_latest["last_ok_at"] = now_iso
    new_latest = {
        "schema_version": R.SCHEMA_VERSION,
        "last_run_at": now_iso,
        "last_run_status": status,
        "baseline_at": (latest or {}).get("baseline_at") or (now_iso if status == "baseline" else None),
        "tracked": len(new),
        "sources": {R.SOURCE_FEED: feed_latest, R.SOURCE_PLAYERS: players_latest},
        "feed_absent": dict(sorted(absent.items(), key=lambda kv: R.pid_key(kv[0]))),
    }
    counts = {}
    for ln in lines:
        counts[ln["kind"]] = counts.get(ln["kind"], 0) + 1
    stamp = {"run_at": now_iso, "status": status, "trigger": trigger, "reasons": reasons,
             "sources": stamp_sources, "lines": counts, "tracked": len(new)}
    if until_iso != now_iso:
        stamp["observed_until"] = until_iso

    if new is not prev:
        write_state(root, new)
    append_lines(root, "log", now_iso, lines)
    append_lines(root, "runs", now_iso, [stamp])
    (root / "latest.json").write_text(json.dumps(new_latest, indent=1, sort_keys=True) + "\n")
    return {"stamp": stamp, "lines": lines}


def commit_message(summary):
    if "skipped" in summary:
        return None
    s = summary["stamp"]
    n = sum(s["lines"].values())
    msg = f"run {s['run_at']}: {s['status']}, {n} line{'s' if n != 1 else ''}"
    if s["reasons"]:
        msg += "\n\n" + "\n".join(f"- {r}" for r in s["reasons"])
    return msg + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="One change-feed writer run.")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--recorded", help="read inputs from this directory instead of Sleeper")
    ap.add_argument("--save-inputs", help="also save each fetched response here (live mode)")
    ap.add_argument("--at", help="run time, ISO UTC (default now; recorded mode requires it)")
    ap.add_argument("--trigger", default="local")
    ap.add_argument("--skip-if-last-run-within", type=int, metavar="MIN",
                    help="write nothing if the committed last run is this recent (backup trigger)")
    ap.add_argument("--message-file", help="write the commit message here ('' if nothing to commit)")
    args = ap.parse_args(argv)
    if args.recorded and not args.at:
        ap.error("--recorded needs --at")
    source = RecordedSource(args.recorded) if args.recorded else LiveSource(args.save_inputs)
    try:
        summary = run(args.repo, source, args.at or R.iso_now(), trigger=args.trigger,
                      skip_if_within_min=args.skip_if_last_run_within)
    except Malfunction as e:
        print(f"MALFUNCTION: {e}", file=sys.stderr)
        return 2
    msg = commit_message(summary)
    if args.message_file:
        Path(args.message_file).write_text(msg or "")
    out = summary.get("stamp") or summary
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
