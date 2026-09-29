"""Layer-1 leak guard: a structural allow-list over every data file the writer produces.
It runs before every commit and blocks it on any problem.

This repo is public and holds only public NFL facts. The guard enforces that by shape,
not by knowing what a leak looks like:
  - no run of 12 or more digits anywhere in a data file. Account-style ids elsewhere
    are 17-19 digits; player ids here are at most 6 digits, and every timestamp is an
    ISO string (at most 4 consecutive digits). A raw epoch-ms stamp (13 digits) fails too;
  - every key is on an allow-list for its file type, and every value has its type;
  - team codes are the 32 NFL teams; player ids are 1-6 digits;
  - nothing is written outside state/, log/, runs/ and latest.json.
It cannot recognise a leaked NAME in a free-text field (injury_notes, full_name). That
check needs to know the names, so it lives outside this repo (layer 2).

Run:
    python3 -m nflchanges.guard --repo . --changed   # files git reports as changed/new
    python3 -m nflchanges.guard --repo . --all       # every data file
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from . import record as R

DIGIT_RUN_LIMIT = 12
DATA_PREFIXES = ("state/", "log/", "runs/")
DATA_FILES = ("latest.json",)

_DIGITS = re.compile(r"\d+")
_PID = re.compile(r"\d{1,6}")
_ISO_S = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_ISO_MS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CODE = re.compile(r"[A-Z]{1,5}")
_WORDS = re.compile(r"[A-Za-z][A-Za-z ]{0,39}")
_REASON = re.compile(r"[A-Za-z0-9_:.()=, /-]{1,160}")
FREE_TEXT_MAX = 200

RECORD_TYPES = {
    "team": "team", "position": "code", "depth_chart_position": "code",
    "status": "words", "injury_status": "words", "practice_participation": "words",
    "injury_body_part": "text", "injury_notes": "text", "full_name": "text",
    "practice_description": "text",
    "injury_start_date": "date", "news_updated": "iso_ms", "team_changed_at": "iso_ms",
    "active": "bool", "depth_chart_order": "order",
}
assert set(RECORD_TYPES) == set(R.ALL_FIELDS)

LOG_KEYS = {
    "change": {"kind", "player_id", "team", "field", "old", "new", "source", "detected_between"},
    "enter": {"kind", "player_id", "team", "source", "detected_between", "record"},
    "exit": {"kind", "player_id", "team", "reason", "source", "detected_between", "record"},
    "fill": {"kind", "player_id", "team", "source", "detected_between", "record"},
}
EXIT_REASONS = {"team_to_null", "position_out_of_scope", "id_vanished"}
RUN_KEYS = {"run_at", "observed_until", "status", "trigger", "reasons", "sources", "lines", "tracked"}
RUN_SOURCE_KEYS = {"result", "week", "players", "detected_between", "error", "slot"}
LATEST_KEYS = {"schema_version", "last_run_at", "last_run_status", "baseline_at", "tracked",
               "sources", "feed_absent"}
LATEST_SOURCE_KEYS = {"last_ok_at", "last_attempt_at", "last_result", "week", "slot",
                      "slot_attempts"}
TRIGGERS = {"local", "workflow_dispatch", "schedule", "retry"}


def max_digit_run(text):
    return max((len(m) for m in _DIGITS.findall(text)), default=0)


def _value_ok(kind, v):
    if kind == "team":
        return v in R.NFL_TEAMS
    if kind == "code":
        return isinstance(v, str) and bool(_CODE.fullmatch(v))
    if kind == "words":
        return isinstance(v, str) and bool(_WORDS.fullmatch(v))
    if kind == "text":
        return isinstance(v, str) and len(v) <= FREE_TEXT_MAX and v.isprintable()
    if kind == "date":
        return isinstance(v, str) and bool(_DATE.fullmatch(v) or _ISO_MS.fullmatch(v))
    if kind == "iso_ms":
        return isinstance(v, str) and bool(_ISO_MS.fullmatch(v))
    if kind == "bool":
        return isinstance(v, bool)
    if kind == "order":
        return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 100
    return False


def _iso(v, allow_none=False):
    return (v is None and allow_none) or (isinstance(v, str) and bool(_ISO_S.fullmatch(v)))


def check_record(where, rec, problems):
    if not isinstance(rec, dict):
        problems.append(f"{where}: record is not an object")
        return
    for k, v in rec.items():
        if k not in RECORD_TYPES:
            problems.append(f"{where}: key {k!r} not allowed")
        elif not _value_ok(RECORD_TYPES[k], v):
            problems.append(f"{where}: {k} has a disallowed value")


def check_state(name, text, problems):
    team = Path(name).stem
    if team not in R.NFL_TEAMS:
        problems.append(f"{name}: shard name is not an NFL team")
    try:
        recs = json.loads(text)
    except json.JSONDecodeError:
        problems.append(f"{name}: not JSON")
        return
    if not isinstance(recs, dict):
        problems.append(f"{name}: not an object")
        return
    for pid, rec in recs.items():
        if not _PID.fullmatch(pid):
            problems.append(f"{name}: player id is not 1-6 digits")
        check_record(f"{name}:{pid}", rec, problems)
        if isinstance(rec, dict) and rec.get("team") != team:
            problems.append(f"{name}:{pid}: team does not match the shard")


def check_log_line(where, ln, problems):
    kind = ln.get("kind")
    if kind not in LOG_KEYS:
        problems.append(f"{where}: kind not allowed")
        return
    extra = set(ln) - LOG_KEYS[kind]
    if extra:
        problems.append(f"{where}: keys {sorted(extra)} not allowed")
    if not (isinstance(ln.get("player_id"), str) and _PID.fullmatch(ln["player_id"])):
        problems.append(f"{where}: player id is not 1-6 digits")
    if ln.get("team") is not None and ln.get("team") not in R.NFL_TEAMS:
        problems.append(f"{where}: team not an NFL team")
    if ln.get("source") not in R.SOURCES:
        problems.append(f"{where}: source not allowed")
    db = ln.get("detected_between")
    if not (isinstance(db, list) and len(db) == 2 and _iso(db[0], True) and _iso(db[1])):
        problems.append(f"{where}: detected_between malformed")
    if kind == "change":
        field = ln.get("field")
        if field not in RECORD_TYPES:
            problems.append(f"{where}: field not allowed")
        else:
            for side in ("old", "new"):
                v = ln.get(side)
                if v is not None and not _value_ok(RECORD_TYPES[field], v):
                    problems.append(f"{where}: {side} has a disallowed value")
    else:
        check_record(where, ln.get("record"), problems)
    if kind == "exit" and ln.get("reason") not in EXIT_REASONS:
        problems.append(f"{where}: exit reason not allowed")


def check_run_line(where, ln, problems):
    extra = set(ln) - RUN_KEYS
    if extra:
        problems.append(f"{where}: keys {sorted(extra)} not allowed")
    if not _iso(ln.get("run_at")) or not _iso(ln.get("observed_until", ln.get("run_at"))):
        problems.append(f"{where}: run_at malformed")
    if ln.get("status") not in ("baseline", "ok", "failed"):
        problems.append(f"{where}: status not allowed")
    if ln.get("trigger") not in TRIGGERS:
        problems.append(f"{where}: trigger not allowed")
    reasons = ln.get("reasons")
    if not (isinstance(reasons, list) and all(isinstance(r, str) and _REASON.fullmatch(r) for r in reasons)):
        problems.append(f"{where}: reasons malformed")
    srcs = ln.get("sources")
    if not (isinstance(srcs, dict) and set(srcs) <= set(R.SOURCES)):
        problems.append(f"{where}: sources malformed")
    else:
        for s, v in srcs.items():
            if not isinstance(v, dict) or set(v) - RUN_SOURCE_KEYS:
                problems.append(f"{where}: sources.{s} has keys not allowed")
            elif "error" in v and not (isinstance(v["error"], str) and _REASON.fullmatch(v["error"])):
                problems.append(f"{where}: sources.{s}.error malformed")
    lines = ln.get("lines")
    if not (isinstance(lines, dict) and set(lines) <= set(LOG_KEYS)
            and all(isinstance(n, int) for n in lines.values())):
        problems.append(f"{where}: lines malformed")


def check_latest(name, text, problems):
    try:
        lt = json.loads(text)
    except json.JSONDecodeError:
        problems.append(f"{name}: not JSON")
        return
    extra = set(lt) - LATEST_KEYS
    if extra:
        problems.append(f"{name}: keys {sorted(extra)} not allowed")
    for s, v in (lt.get("sources") or {}).items():
        if s not in R.SOURCES or not isinstance(v, dict) or set(v) - LATEST_SOURCE_KEYS:
            problems.append(f"{name}: sources.{s} malformed")
    for pid, when in (lt.get("feed_absent") or {}).items():
        if not _PID.fullmatch(pid) or not _iso(when):
            problems.append(f"{name}: feed_absent entry malformed")


def check_file(name, text):
    """Problems in one data file, given its repo-relative path and its text."""
    problems = []
    run = max_digit_run(text)
    if run >= DIGIT_RUN_LIMIT:
        problems.append(f"{name}: digit run of {run}")
    if name.startswith("state/") and name.endswith(".json") and name.count("/") == 1:
        check_state(name, text, problems)
    elif name == "latest.json":
        check_latest(name, text, problems)
    elif re.fullmatch(r"(log|runs)/\d{4}/\d{2}/\d{2}\.jsonl", name):
        check = check_log_line if name.startswith("log/") else check_run_line
        for i, raw in enumerate(text.splitlines(), 1):
            try:
                ln = json.loads(raw)
            except json.JSONDecodeError:
                problems.append(f"{name}:{i}: not JSON")
                continue
            if not isinstance(ln, dict):
                problems.append(f"{name}:{i}: not an object")
                continue
            check(f"{name}:{i}", ln, problems)
    else:
        problems.append(f"{name}: not a path the writer may write")
    return problems


def changed_paths(root):
    """Paths git reports as modified, added or untracked (not ignored)."""
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-uall"],
                         capture_output=True, text=True, check=True).stdout
    paths = []
    for line in out.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def all_data_paths(root):
    root = Path(root)
    out = [p for p in DATA_FILES if (root / p).exists()]
    for prefix in DATA_PREFIXES:
        d = root / prefix
        if d.exists():
            out += [str(p.relative_to(root)) for p in sorted(d.rglob("*")) if p.is_file()]
    return out


def check_paths(root, paths):
    root = Path(root)
    problems = []
    for name in paths:
        if not (name.startswith(DATA_PREFIXES) or name in DATA_FILES):
            problems.append(f"{name}: changed outside the writer's data paths")
            continue
        path = root / name
        if not path.exists():
            continue   # a deleted shard (a team with no tracked players left)
        problems += check_file(name, path.read_text())
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Layer-1 leak guard (blocking).")
    ap.add_argument("--repo", default=".")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--changed", action="store_true")
    g.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    paths = changed_paths(args.repo) if args.changed else all_data_paths(args.repo)
    problems = check_paths(args.repo, paths)
    for p in problems:
        print(f"GUARD: {p}", file=sys.stderr)
    print(f"guard: {len(paths)} file(s) checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
