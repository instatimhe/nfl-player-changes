"""The layer-1 guard, watched failing. Each synthetic leak below is invented; none is a
real identifier of anything."""
import json

from nflchanges import guard as G
from nflchanges import writer as W
from tests.helpers import T0, T1, WriterCase, feed_rows, mutate, state_obj, world

SYNTHETIC_LONG_ID = "123456789012345678"   # 18 digits, the shape of an account-style id


class TestGuard(WriterCase):
    def setUp(self):
        super().setUp()
        ps = world()
        self.baseline(ps)
        self.run_at(T1, state_obj(), feed_rows(mutate(ps, "101", injury_status="Out")))

    def problems(self):
        return G.check_paths(self.repo, G.all_data_paths(self.repo))

    def edit_json(self, rel, fn):
        path = self.repo / rel
        obj = json.loads(path.read_text())
        fn(obj)
        path.write_text(json.dumps(obj))

    def edit_first_line(self, pattern, fn):
        path = sorted(self.repo.glob(pattern))[0]
        lines = path.read_text().splitlines()
        obj = json.loads(lines[0])
        fn(obj)
        lines[0] = json.dumps(obj)
        path.write_text("\n".join(lines) + "\n")

    def test_clean_output_passes(self):
        self.assertEqual(self.problems(), [])

    def test_synthetic_long_id_in_free_text(self):
        self.edit_json("state/KC.json", lambda o: o["103"].update(injury_notes=f"see {SYNTHETIC_LONG_ID}"))
        self.assertTrue(any("digit run of 18" in p for p in self.problems()), self.problems())

    def test_synthetic_long_id_in_the_change_log(self):
        self.edit_first_line("log/*/*/*.jsonl", lambda o: o.update(new=f"x {SYNTHETIC_LONG_ID}"))
        self.assertTrue(any("digit run" in p for p in self.problems()))

    def test_unexpected_key_in_a_record(self):
        self.edit_json("state/KC.json", lambda o: o["103"].update(roster_id=77))
        self.assertTrue(any("key 'roster_id' not allowed" in p for p in self.problems()))

    def test_unexpected_key_in_a_log_line_and_a_run_stamp(self):
        self.edit_first_line("log/*/*/*.jsonl", lambda o: o.update(owner="x"))
        self.edit_first_line("runs/*/*/*.jsonl", lambda o: o.update(league="x"))
        ps = self.problems()
        self.assertTrue(any("log/" in p and "not allowed" in p for p in ps))
        self.assertTrue(any("runs/" in p and "not allowed" in p for p in ps))

    def test_copied_metadata_object(self):
        # what the feed's player object carries, copied verbatim
        self.edit_json("state/KC.json", lambda o: o["103"].update(
            metadata={"channel_id": "1113708856157000000", "rookie_year": "2020"}))
        ps = self.problems()
        self.assertTrue(any("key 'metadata' not allowed" in p for p in ps))
        self.assertTrue(any("digit run of 19" in p for p in ps))

    def test_raw_epoch_ms_stamp(self):
        self.edit_json("state/KC.json", lambda o: o["103"].update(news_updated=1790600405508))
        self.assertTrue(any("digit run of 13" in p for p in self.problems()))

    def test_long_player_id_and_bad_team(self):
        self.edit_json("state/KC.json", lambda o: o.update({"1234567": {"team": "KC"}}))
        self.edit_json("state/BUF.json", lambda o: next(iter(o.values())).update(team="XYZ"))
        ps = self.problems()
        self.assertTrue(any("player id is not 1-6 digits" in p for p in ps))
        self.assertTrue(any("team" in p for p in ps))

    def test_file_outside_the_data_paths(self):
        (self.repo / "notes.txt").write_text("hello")
        self.assertTrue(any("outside" in p for p in G.check_paths(self.repo, ["notes.txt"])))
        (self.repo / "state" / "extra.txt").write_text("x")
        self.assertTrue(any("not a path" in p for p in G.check_paths(self.repo, ["state/extra.txt"])))

    def test_problem_messages_never_echo_the_value(self):
        self.edit_json("state/KC.json", lambda o: o["103"].update(injury_notes=f"see {SYNTHETIC_LONG_ID}",
                                                                   status=SYNTHETIC_LONG_ID))
        self.assertFalse(any(SYNTHETIC_LONG_ID in p for p in self.problems()))


class TestGuardCli(WriterCase):
    def test_exit_code(self):
        self.baseline(world())
        self.assertEqual(G.main(["--repo", str(self.repo), "--all"]), 0)
        path = self.repo / "state" / "KC.json"
        path.write_text(path.read_text().replace('"team":"KC"', '"team":"KC","roster_id":77', 1))
        self.assertEqual(G.main(["--repo", str(self.repo), "--all"]), 1)
