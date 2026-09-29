"""The workflow's commit step, run for real against a throwaway bare repo standing in for
GitHub: the baseline commit (no log/ yet), a rejected push recovered by re-running the
writer on the new tip from the saved inputs, a stale run that writes nothing, and the
guard blocking a push."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import player

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "commit_and_push.sh"
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
       "PYTHONDONTWRITEBYTECODE": "1"}


def big_world(injured=None):
    """Enough synthetic players to clear the writer's real size floors."""
    ps = {}
    for i in range(1, 5201):
        pid = str(1000 + i)
        teamed = i <= 600
        ps[pid] = player(pid, team=("KC", "BUF", "DET")[i % 3] if teamed else None)
    if injured:
        ps["1001"]["injury_status"] = injured
    return ps


def write_inputs(d, ps):
    d.mkdir(parents=True)
    rows = [{"player_id": pid, "week": 5, "season": "2026",
             "player": {k: p.get(k) for k in ("team", "position", "injury_status",
                                             "injury_body_part", "injury_notes",
                                             "injury_start_date", "news_updated",
                                             "team_changed_at", "fantasy_positions")}}
            for pid, p in ps.items()]
    (d / "state.json").write_text(json.dumps({"season": "2026", "week": 5, "season_type": "regular"}))
    (d / "feed.json").write_text(json.dumps(rows))
    (d / "players_nfl.json").write_text(json.dumps(ps))
    return d


class TestCommitScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin = self.tmp / "origin.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        seed.mkdir()
        for name in ("nflchanges", ".github"):
            shutil.copytree(ROOT / name, seed / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(ROOT / ".gitignore", seed / ".gitignore")
        self.git(seed, "init", "-q", "-b", "main")
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-qm", "code")
        self.git(seed, "push", "-q", self.url(), "HEAD:main")

    def url(self):
        return "file://" + str(self.origin)

    def git(self, cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, env=ENV, check=True, capture_output=True,
                              text=True).stdout

    def clone(self, name):
        d = self.tmp / name
        self.git(self.tmp, "clone", "-q", self.url(), str(d))
        return d

    def writer_then_commit(self, work, inputs, at):
        msg, summary = self.tmp / f"msg-{at}", self.tmp / f"summary-{at}"
        out = subprocess.run([sys.executable, "-m", "nflchanges.writer", "--repo", ".",
                              "--recorded", str(inputs), "--at", at, "--message-file", str(msg)],
                             cwd=work, env=ENV, capture_output=True, text=True, check=True)
        summary.write_text(out.stdout)
        return subprocess.run(["bash", str(SCRIPT), str(inputs), str(msg), str(summary)],
                              cwd=work, env=ENV, capture_output=True, text=True)

    def origin_log(self):
        return self.git(self.origin, "log", "--format=%s", "main").splitlines()

    def test_baseline_then_a_rejected_push_is_recovered_on_the_new_tip(self):
        inputs0 = write_inputs(self.tmp / "in0", big_world())
        a = self.clone("a")
        r = self.writer_then_commit(a, inputs0, "2026-10-06T07:05:00Z")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[0], "run 2026-10-06T07:05:00Z: baseline, 0 lines")

        b = self.clone("b")      # both runners now sit on the baseline
        inputs1 = write_inputs(self.tmp / "in1", big_world(injured="Questionable"))
        r = self.writer_then_commit(b, inputs1, "2026-10-06T07:35:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)

        # a still sits on the baseline: its push will be rejected
        inputs2 = write_inputs(self.tmp / "in2", big_world(injured="Out"))
        r = self.writer_then_commit(a, inputs2, "2026-10-06T08:05:00Z")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("push rejected", r.stdout)
        self.assertEqual(self.origin_log()[:2], ["run 2026-10-06T08:05:00Z: ok, 1 line",
                                                 "run 2026-10-06T07:35:00Z: ok, 1 line"])
        fresh = self.clone("check")
        lines = [json.loads(x) for x in (fresh / "log/2026/10/06.jsonl").read_text().splitlines()]
        self.assertEqual([(ln["old"], ln["new"], ln["detected_between"]) for ln in lines], [
            (None, "Questionable", ["2026-10-06T07:05:00Z", "2026-10-06T07:35:00Z"]),
            ("Questionable", "Out", ["2026-10-06T07:35:00Z", "2026-10-06T08:05:00Z"]),
        ])

    def test_a_run_older_than_the_tip_writes_nothing(self):
        inputs0 = write_inputs(self.tmp / "in0", big_world())
        a = self.clone("a")
        self.assertEqual(self.writer_then_commit(a, inputs0, "2026-10-06T07:05:00Z").returncode, 0)
        b = self.clone("b")
        self.assertEqual(self.writer_then_commit(b, inputs0, "2026-10-06T08:05:00Z").returncode, 0)
        r = self.writer_then_commit(a, inputs0, "2026-10-06T07:35:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a newer run is already committed", r.stdout)
        self.assertEqual(len(self.origin_log()), 3)   # code, 07:05, 08:05

    def test_the_guard_blocks_the_push(self):
        inputs0 = write_inputs(self.tmp / "in0", big_world())
        a = self.clone("a")
        msg, summary = self.tmp / "msg", self.tmp / "summary"
        out = subprocess.run([sys.executable, "-m", "nflchanges.writer", "--repo", ".",
                              "--recorded", str(inputs0), "--at", "2026-10-06T07:05:00Z",
                              "--message-file", str(msg)], cwd=a, env=ENV,
                             capture_output=True, text=True, check=True)
        summary.write_text(out.stdout)
        shard = a / "state" / "KC.json"
        shard.write_text(shard.read_text().replace('"team":"KC"', '"team":"KC","roster_id":77', 1))
        r = subprocess.run(["bash", str(SCRIPT), str(inputs0), str(msg), str(summary)],
                           cwd=a, env=ENV, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("roster_id", r.stderr)
        self.assertEqual(self.origin_log(), ["code"])


if __name__ == "__main__":
    unittest.main()
