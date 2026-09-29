"""The public record: which fields are kept, who owns each one, and how files are encoded.

Everything written to this repo is built from the whitelists below. A field that is not
named here is never copied out of a source object, so a new upstream key (or the source's
`metadata` object, which carries long numeric ids) cannot reach a committed file.
"""
import datetime as dt
import json

SCHEMA_VERSION = 1

# Fields read from the weekly projections feed's embedded `player` object, every run.
FEED_FIELDS = (
    "team", "position",
    "injury_status", "injury_body_part", "injury_notes", "injury_start_date",
    "news_updated", "team_changed_at",
)
# Fields read from the full player list, at most a few times a day (see writer.SLOTS_UTC).
PLAYERS_FIELDS = (
    "full_name", "status", "active",
    "depth_chart_position", "depth_chart_order",
    "practice_participation", "practice_description",
)
ALL_FIELDS = FEED_FIELDS + PLAYERS_FIELDS
# Upstream sends these as epoch milliseconds (13 digits). They are written as ISO strings.
STAMP_FIELDS = ("news_updated", "team_changed_at")

SOURCE_FEED = "projections_feed"
SOURCE_PLAYERS = "players_nfl"
SOURCES = (SOURCE_FEED, SOURCE_PLAYERS)

# Scope: on an NFL team AND listed at a fantasy skill position or K. Team defenses are
# out (they carry no injury, depth or status state).
SCOPE_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K"})

NFL_TEAMS = frozenset(
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LAC LAR LV "
    "MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WAS".split())


def iso_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def iso_ms(ms):
    """Epoch milliseconds -> '2026-09-29T06:44:30.123Z'. Never emit the raw number."""
    return (dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")


def in_scope(obj):
    """obj: a source player object (feed `player` or a /players/nfl entry)."""
    return bool(obj.get("team")) and bool(set(obj.get("fantasy_positions") or []) & SCOPE_POSITIONS)


def pick(obj, fields):
    """The whitelisted fields of a source object, nulls omitted, stamps as ISO strings."""
    out = {}
    for f in fields:
        v = obj.get(f)
        if v is None:
            continue
        if f in STAMP_FIELDS:
            if not isinstance(v, (int, float)):
                raise ValueError(f"{f} is not epoch ms: {type(v).__name__}")
            v = iso_ms(v)
        out[f] = v
    return out


def pid_key(pid):
    return (len(pid), pid)


def encode_shard(recs):
    """One player per line (readable diffs, good delta compression), keys sorted.
    Byte-identical to the layout R50 item 0 measured."""
    body = ",\n".join(
        f"{json.dumps(pid)}:{json.dumps(recs[pid], sort_keys=True, separators=(',', ':'))}"
        for pid in sorted(recs, key=pid_key))
    return ("{\n" + body + "\n}\n").encode()


def json_line(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"
