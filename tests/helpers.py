"""Synthetic inputs for the writer's tests. Every player here is invented: ids are small
made-up numbers and names are 'Test Player N'. Nothing here refers to any real league."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from nflchanges import record as R
from nflchanges import writer as W
from nflchanges.sources import RecordedSource

T0 = "2026-10-06T07:05:00Z"   # a slot run (07:00 slot)
T1 = "2026-10-06T07:35:00Z"
T2 = "2026-10-06T08:05:00Z"
T3 = "2026-10-06T08:35:00Z"
T_SLOT2 = "2026-10-06T14:35:00Z"  # first run after the 14:30 slot
T_SLOT2B = "2026-10-06T15:05:00Z"
T_SLOT2C = "2026-10-06T15:35:00Z"
T_SLOT2D = "2026-10-06T16:05:00Z"

MS = 1790600000000  # an epoch-ms stamp; the writer must turn it into an ISO string


def player(pid, team="KC", pos="WR", **kw):
    p = {"full_name": f"Test Player {pid}", "first_name": "Test", "last_name": f"Player {pid}",
         "team": team, "position": pos, "fantasy_positions": [pos],
         "status": "Active", "active": True,
         "injury_status": None, "injury_body_part": None, "injury_notes": None,
         "injury_start_date": None, "news_updated": MS, "team_changed_at": None,
         "depth_chart_position": pos, "depth_chart_order": 1,
         "practice_participation": None, "practice_description": None,
         "metadata": {"channel_id": "123456789012345678901", "rookie_year": "2020"}}
    p.update(kw)
    return p


def world(n=6):
    """A small player universe: ids 101..(100+n), all in scope, plus one lineman out of scope."""
    ps = {str(100 + i): player(str(100 + i), team=("KC", "BUF", "DET")[i % 3]) for i in range(1, n + 1)}
    ps["900"] = player("900", team="KC", pos="OL")
    return ps


def feed_rows(players, week=5, season="2026", omit=()):
    rows = []
    for pid, p in players.items():
        if pid in omit:
            continue
        pl = {k: p.get(k) for k in R.FEED_FIELDS + ("fantasy_positions", "first_name",
                                                    "last_name", "metadata")}
        rows.append({"player_id": pid, "week": week, "season": season,
                     "season_type": "regular", "team": p.get("team"), "player": pl,
                     "stats": {}})
    return rows


def state_obj(week=5, season_type="regular"):
    return {"season": "2026", "week": week, "season_type": season_type}


class WriterCase(unittest.TestCase):
    """A temp repo dir (no .git: the committed-state check is exercised separately), with
    the size floors lowered so tiny synthetic worlds pass validation."""

    def setUp(self):
        self._floors = (W.MIN_FEED_TEAMED_ROWS, W.MIN_PLAYERS_ENTRIES)
        W.MIN_FEED_TEAMED_ROWS, W.MIN_PLAYERS_ENTRIES = 1, 1
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.n = 0

    def tearDown(self):
        W.MIN_FEED_TEAMED_ROWS, W.MIN_PLAYERS_ENTRIES = self._floors
        shutil.rmtree(self.tmp)

    def tick(self, state=None, feed=None, players=None, completed_at=None):
        self.n += 1
        d = self.tmp / f"tick{self.n}"
        d.mkdir()
        for name, obj in (("state.json", state), ("feed.json", feed), ("players_nfl.json", players)):
            if obj is not None:
                (d / name).write_text(json.dumps(obj))
        if completed_at:
            (d / "meta.json").write_text(json.dumps({"completed_at": completed_at}))
        return RecordedSource(d)

    def run_at(self, at, state=None, feed=None, players=None, repo=None, **kw):
        return W.run(repo or self.repo, self.tick(state, feed, players, kw.pop("completed_at", None)),
                     at, **kw)

    def baseline(self, ps, at=T0, omit=()):
        out = self.run_at(at, state_obj(), feed_rows(ps, omit=omit), ps)
        self.assertEqual(out["stamp"]["status"], "baseline")
        return out

    def log_lines(self, repo=None):
        out = []
        for f in sorted((repo or self.repo).glob("log/*/*/*.jsonl")):
            out += [json.loads(x) for x in f.read_text().splitlines()]
        return out

    def run_lines(self, repo=None):
        out = []
        for f in sorted((repo or self.repo).glob("runs/*/*/*.jsonl")):
            out += [json.loads(x) for x in f.read_text().splitlines()]
        return out

    def snapshot(self):
        """Copy of the repo dir: used to emulate a run that is never committed."""
        dst = self.tmp / f"snap{self.n}"
        shutil.copytree(self.repo, dst)
        return dst

    def restore(self, snap):
        shutil.rmtree(self.repo)
        shutil.copytree(snap, self.repo)


def mutate(ps, pid, **kw):
    out = copy.deepcopy(ps)
    out[pid].update(kw)
    return out
