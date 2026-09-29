import datetime as dt
import json
import subprocess
import os

from nflchanges import record as R
from nflchanges import writer as W
from tests.helpers import (T0, T1, T2, T3, T_SLOT2, T_SLOT2B, T_SLOT2C, T_SLOT2D, WriterCase,
                           feed_rows, mutate, player, state_obj, world)


class TestBaseline(WriterCase):
    def test_baseline_needs_both_sources_in_one_run(self):
        ps = world()
        out = self.run_at(T0, state_obj(), feed_rows(ps), None)   # player list missing
        self.assertEqual(out["stamp"]["status"], "failed")
        self.assertFalse((self.repo / "state").exists())
        self.assertIsNone(json.loads((self.repo / "latest.json").read_text())["baseline_at"])
        out = self.run_at(T1, state_obj(), feed_rows(ps), ps)
        self.assertEqual(out["stamp"]["status"], "baseline")
        self.assertEqual(out["lines"], [])
        self.assertEqual(len(W.load_state(self.repo)), 6)   # the lineman is out of scope

    def test_state_is_sharded_by_team_and_omits_nulls(self):
        self.baseline(world())
        self.assertEqual(sorted(p.stem for p in (self.repo / "state").glob("*.json")),
                         ["BUF", "DET", "KC"])
        rec = W.load_state(self.repo)["101"]
        self.assertNotIn("injury_status", rec)
        self.assertEqual(rec["news_updated"], R.iso_ms(1790600000000))


class TestChanges(WriterCase):
    def test_feed_change_names_its_source_and_bounds(self):
        ps = world()
        self.baseline(ps)
        ps2 = mutate(ps, "101", injury_status="Questionable", injury_body_part="Knee")
        out = self.run_at(T1, state_obj(), feed_rows(ps2))
        ch = sorted((ln["field"], ln["old"], ln["new"], ln["source"], tuple(ln["detected_between"]))
                    for ln in out["lines"])
        self.assertEqual(ch, [
            ("injury_body_part", None, "Knee", "projections_feed", (T0, T1)),
            ("injury_status", None, "Questionable", "projections_feed", (T0, T1)),
        ])
        self.assertEqual(out["stamp"]["status"], "ok")

    def test_player_list_field_is_bracketed_by_the_previous_list_read(self):
        ps = world()
        self.baseline(ps)                                    # list read at T0 (07:00 slot)
        self.run_at(T1, state_obj(), feed_rows(ps))          # list not due
        ps2 = mutate(ps, "102", status="Inactive", depth_chart_order=2)
        self.run_at(T2, state_obj(), feed_rows(ps2), ps2)    # not due: recorded, never read
        self.assertEqual(self.log_lines(), [])
        out = self.run_at(T_SLOT2, state_obj(), feed_rows(ps2), ps2)
        got = {(ln["field"], ln["source"], tuple(ln["detected_between"])) for ln in out["lines"]}
        self.assertEqual(got, {("status", "players_nfl", (T0, T_SLOT2)),
                               ("depth_chart_order", "players_nfl", (T0, T_SLOT2))})

    def test_no_change_run_still_writes_a_stamp(self):
        ps = world()
        self.baseline(ps)
        out = self.run_at(T1, state_obj(), feed_rows(ps))
        self.assertEqual(out["lines"], [])
        self.assertEqual([r["run_at"] for r in self.run_lines()], [T0, T1])
        self.assertEqual(self.run_lines()[-1]["status"], "ok")

    def test_upper_bound_is_when_the_reads_finished(self):
        ps = world()
        self.baseline(ps)
        done = "2026-10-06T07:36:10Z"
        out = self.run_at(T1, state_obj(), feed_rows(mutate(ps, "101", injury_status="Out")),
                          completed_at=done)
        self.assertEqual(out["lines"][0]["detected_between"], [T0, done])
        self.assertEqual(out["stamp"]["observed_until"], done)
        # and the next run's lower bound is this run's START, the conservative end
        out = self.run_at(T2, state_obj(), feed_rows(mutate(ps, "101", injury_status="IR")))
        self.assertEqual(out["lines"][0]["detected_between"], [T1, T2])


class TestFailedRuns(WriterCase):
    def _assert_failed(self, out, code):
        self.assertEqual(out["stamp"]["status"], "failed")
        self.assertTrue(any(code in r for r in out["stamp"]["reasons"]), out["stamp"]["reasons"])
        self.assertEqual(out["lines"], [])

    def test_missing_feed_is_a_failed_run_and_the_next_success_spans_back(self):
        ps = world()
        self.baseline(ps)
        ps2 = mutate(ps, "103", team="BUF")
        self._assert_failed(self.run_at(T1, state_obj(), None), "not_recorded")
        self.assertEqual(W.load_state(self.repo)["103"]["team"], "KC")   # untouched
        out = self.run_at(T2, state_obj(), feed_rows(ps2))
        self.assertEqual([(ln["field"], ln["detected_between"]) for ln in out["lines"]],
                         [("team", [T0, T2])])
        self.assertEqual([r["status"] for r in self.run_lines()], ["baseline", "failed", "ok"])

    def test_every_failure_kind(self):
        ps = world()
        self.baseline(ps)
        self._assert_failed(self.run_at(T1, state_obj(), []), "empty")
        self._assert_failed(self.run_at(T2, state_obj(week=5), feed_rows(ps, week=6)), "week_mismatch")
        bad = feed_rows(ps)
        del bad[0]["player"]["injury_status"]
        self._assert_failed(self.run_at(T3, state_obj(), bad), "shape")
        self._assert_failed(self.run_at("2026-10-06T09:05:00Z", state_obj(season_type="post"),
                                        feed_rows(ps)), "not_regular_season")
        self._assert_failed(self.run_at("2026-10-06T09:35:00Z", None, feed_rows(ps)), "not_recorded")
        # the player list unreachable when due: feed changes still land, the run is FAILED
        out = self.run_at(T_SLOT2, state_obj(), feed_rows(mutate(ps, "101", injury_status="Out")), None)
        self.assertEqual(out["stamp"]["status"], "failed")
        self.assertIn("players_nfl: not_recorded", out["stamp"]["reasons"])
        self.assertEqual([ln["field"] for ln in out["lines"]], ["injury_status"])
        self.assertEqual(out["lines"][0]["detected_between"], [T0, T_SLOT2])

    def test_size_floors_fire(self):
        W.MIN_FEED_TEAMED_ROWS, W.MIN_PLAYERS_ENTRIES = 500, 5000
        with self.assertRaises(W.SourceUnavailable) as e:
            W.validate_feed(feed_rows(world()), 2026, 5)
        self.assertEqual(e.exception.code, "too_few_rows")
        with self.assertRaises(W.SourceUnavailable) as e:
            W.validate_players(world())
        self.assertEqual(e.exception.code, "too_few_rows")

    def test_player_list_attempts_are_capped_per_slot(self):
        ps = world()
        self.baseline(ps)
        for at in (T_SLOT2, T_SLOT2B, T_SLOT2C):
            out = self.run_at(at, state_obj(), feed_rows(ps), None)
            self.assertIn("players_nfl: not_recorded", out["stamp"]["reasons"])
        out = self.run_at(T_SLOT2D, state_obj(), feed_rows(ps), ps)
        self.assertTrue(any("attempts_exhausted" in r for r in out["stamp"]["reasons"]))
        self.assertEqual(out["stamp"]["sources"]["players_nfl"]["result"], "failed")


class TestDroppedRunsAndFlipFlops(WriterCase):
    """Three states; the middle run is executed but never committed (its output is thrown
    away, exactly as a run whose push was lost)."""

    def _three(self, v0, v1, v2):
        ps = world()
        ps0 = mutate(ps, "101", injury_status=v0)
        self.baseline(ps0)
        snap = self.snapshot()
        self.run_at(T1, state_obj(), feed_rows(mutate(ps, "101", injury_status=v1)))
        self.restore(snap)                                      # dropped
        return self.run_at(T2, state_obj(), feed_rows(mutate(ps, "101", injury_status=v2)))

    def test_dropped_run_loses_precision_not_the_change(self):
        out = self._three(None, "Questionable", "Questionable")
        self.assertEqual([(ln["old"], ln["new"], ln["detected_between"]) for ln in out["lines"]],
                         [(None, "Questionable", [T0, T2])])
        self.assertEqual([r["run_at"] for r in self.run_lines()], [T0, T2])

    def test_true_flip_flop_collapses(self):
        out = self._three("Questionable", "Out", "Questionable")
        self.assertEqual(out["lines"], [])

    def test_net_change_through_an_intermediate_value_is_one_line(self):
        out = self._three(None, "Questionable", "Out")
        self.assertEqual([(ln["old"], ln["new"]) for ln in out["lines"]], [(None, "Out")])

    def test_committed_change_then_revert_is_two_lines(self):
        ps = world()
        self.baseline(ps)
        self.run_at(T1, state_obj(), feed_rows(mutate(ps, "101", injury_status="Out")))
        self.run_at(T2, state_obj(), feed_rows(ps))
        got = [(ln["old"], ln["new"], ln["detected_between"]) for ln in self.log_lines()]
        self.assertEqual(got, [(None, "Out", [T0, T1]), ("Out", None, [T1, T2])])


class TestScope(WriterCase):
    def test_absent_from_feed_is_not_an_exit(self):
        ps = world()
        self.baseline(ps)
        out = self.run_at(T1, state_obj(), feed_rows(ps, omit=("104",)))
        self.assertEqual(out["lines"], [])
        self.assertIn("104", W.load_state(self.repo))
        latest = json.loads((self.repo / "latest.json").read_text())
        self.assertEqual(latest["feed_absent"], {"104": T0})

    def test_feed_absent_player_is_covered_by_the_list_with_the_right_bound(self):
        ps = world()
        self.baseline(ps, omit=("104",))                       # never in the feed
        self.run_at(T1, state_obj(), feed_rows(ps, omit=("104",)))
        ps2 = mutate(ps, "104", injury_status="Out")
        out = self.run_at(T_SLOT2, state_obj(), feed_rows(ps2, omit=("104",)), ps2)
        self.assertEqual([(ln["field"], ln["source"], ln["detected_between"]) for ln in out["lines"]],
                         [("injury_status", "players_nfl", [T0, T_SLOT2])])

    def test_player_returning_to_the_feed_is_bracketed_from_his_last_observation(self):
        ps = world()
        self.baseline(ps)
        self.run_at(T1, state_obj(), feed_rows(ps, omit=("104",)))
        self.run_at(T2, state_obj(), feed_rows(ps, omit=("104",)))
        out = self.run_at(T3, state_obj(), feed_rows(mutate(ps, "104", injury_status="Out")))
        self.assertEqual(out["lines"][0]["detected_between"], [T0, T3])

    def test_exits_are_logged_once_then_dropped(self):
        ps = world()
        self.baseline(ps)
        ps2 = mutate(mutate(ps, "101", team=None), "102", fantasy_positions=["OL"], position="OL")
        out = self.run_at(T1, state_obj(), feed_rows(ps2))
        self.assertEqual(sorted((ln["kind"], ln["player_id"], ln["reason"]) for ln in out["lines"]),
                         [("exit", "101", "team_to_null"), ("exit", "102", "position_out_of_scope")])
        self.assertNotIn("101", W.load_state(self.repo))
        out = self.run_at(T2, state_obj(), feed_rows(ps2))
        self.assertEqual(out["lines"], [])

    def test_id_vanished_needs_both_sources_to_lack_him(self):
        ps = world()
        self.baseline(ps)
        gone = {k: v for k, v in ps.items() if k != "105"}
        out = self.run_at(T1, state_obj(), feed_rows(gone))     # feed only: carried
        self.assertEqual(out["lines"], [])
        out = self.run_at(T_SLOT2, state_obj(), feed_rows(gone), gone)
        self.assertEqual([(ln["kind"], ln["player_id"], ln["reason"]) for ln in out["lines"]],
                         [("exit", "105", "id_vanished")])

    def test_enter_then_fill(self):
        ps = world()
        self.baseline(ps)
        ps2 = dict(ps, **{"150": player("150", team="DET", pos="TE")})
        out = self.run_at(T1, state_obj(), feed_rows(ps2))
        self.assertEqual([(ln["kind"], ln["player_id"], ln["source"]) for ln in out["lines"]],
                         [("enter", "150", "projections_feed")])
        self.assertNotIn("full_name", out["lines"][0]["record"])
        out = self.run_at(T_SLOT2, state_obj(), feed_rows(ps2), ps2)
        self.assertEqual([(ln["kind"], ln["player_id"]) for ln in out["lines"]], [("fill", "150")])
        self.assertEqual(out["lines"][0]["record"]["full_name"], "Test Player 150")

    def test_list_only_player_enters_from_the_list(self):
        ps = world()
        self.baseline(ps)
        ps2 = dict(ps, **{"151": player("151", team="BUF", pos="RB")})
        out = self.run_at(T_SLOT2, state_obj(), feed_rows(ps), ps2)
        self.assertEqual([(ln["kind"], ln["player_id"], ln["source"]) for ln in out["lines"]],
                         [("enter", "151", "players_nfl")])


class TestWhitelist(WriterCase):
    def test_metadata_and_unknown_keys_are_never_copied(self):
        ps = world()
        ps["101"]["some_new_upstream_key"] = "x"
        self.baseline(ps)
        for path in self.repo.rglob("*.json*"):
            text = path.read_text()
            self.assertNotIn("metadata", text)
            self.assertNotIn("some_new_upstream_key", text)
            self.assertNotIn("123456789012345678901", text)
        rec = W.load_state(self.repo)["101"]
        self.assertLessEqual(set(rec), set(R.ALL_FIELDS))


class TestScheduling(WriterCase):
    def test_slots(self):
        at = lambda s: R.parse_iso(s)
        self.assertEqual(W.current_slot(at("2026-10-06T06:59:00Z")).isoformat(), "2026-10-05T22:00:00+00:00")
        self.assertEqual(W.current_slot(at("2026-10-06T07:00:00Z")).isoformat(), "2026-10-06T07:00:00+00:00")
        latest = {"sources": {"players_nfl": {"last_ok_at": "2026-10-06T07:05:00Z"}}}
        self.assertFalse(W.players_plan(latest, at("2026-10-06T14:29:00Z"))[0])
        self.assertTrue(W.players_plan(latest, at("2026-10-06T14:35:00Z"))[0])
        self.assertTrue(W.players_plan(None, at("2026-10-06T14:35:00Z"))[0])

    def test_at_most_three_reads_a_day_when_every_run_succeeds(self):
        ps = world()
        self.baseline(ps, at="2026-10-06T00:05:00Z")
        start = R.parse_iso("2026-10-06T00:35:00Z")
        reads = 0
        for i in range(48):
            at = (start + dt.timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            out = self.run_at(at, state_obj(), feed_rows(ps), ps)
            reads += out["stamp"]["sources"]["players_nfl"]["result"] == "ok"
        self.assertEqual(reads, 3)

    def test_backup_trigger_skips_when_the_last_run_is_recent(self):
        ps = world()
        self.baseline(ps)
        out = self.run_at("2026-10-06T07:20:00Z", state_obj(), feed_rows(ps), skip_if_within_min=25)
        self.assertEqual(out["skipped"], "recent")
        self.assertEqual(len(self.run_lines()), 1)
        out = self.run_at("2026-10-06T07:50:00Z", state_obj(), feed_rows(ps), skip_if_within_min=25)
        self.assertEqual(out["stamp"]["status"], "ok")

    def test_a_run_older_than_the_committed_one_writes_nothing(self):
        ps = world()
        self.baseline(ps)
        out = self.run_at("2026-10-06T07:00:00Z", state_obj(), feed_rows(ps))
        self.assertEqual(out["skipped"], "stale")
        self.assertIsNone(W.commit_message(out))


class TestCommittedStateOnly(WriterCase):
    def test_refuses_uncommitted_data_files(self):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], env=env, check=True,
                                        capture_output=True)
        git("init", "-q", "-b", "main")
        ps = world()
        self.baseline(ps)
        with self.assertRaises(W.Malfunction):
            self.run_at(T1, state_obj(), feed_rows(ps))
        git("add", "-A")
        git("commit", "-qm", "baseline")
        self.assertEqual(self.run_at(T1, state_obj(), feed_rows(ps))["stamp"]["status"], "ok")


if __name__ == "__main__":
    import unittest
    unittest.main()
