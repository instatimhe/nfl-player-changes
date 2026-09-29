"""Workflow invariants for a PUBLIC repo: no pull_request triggers (a fork could run
them), write permission only on the writer job, a concurrency group, the sync-to-tip step
before the writer, and backup schedule minutes that differ from the dispatch minutes."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
DISPATCH_MINUTES = {5, 35}   # the external scheduler's minutes (maintainer's setup notes)


def strip_comments(text):
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


def top_level_on_block(text):
    m = re.search(r"^on:\n((?:[ \t]+.*\n|\n)*)", text, re.M)
    return m.group(1) if m else ""


class TestWorkflows(unittest.TestCase):
    def test_no_pull_request_trigger_anywhere(self):
        files = sorted(WF.glob("*.yml"))
        self.assertGreaterEqual(len(files), 2)
        for f in files:
            self.assertNotRegex(strip_comments(f.read_text()), r"pull_request", f.name)

    def test_comment_stripping_keeps_a_real_trigger(self):
        self.assertRegex(strip_comments("on:\n  pull_request_target:  # no\n"), r"pull_request")

    def test_writer_triggers_are_dispatch_and_schedule_only(self):
        text = (WF / "write.yml").read_text()
        keys = set(re.findall(r"^  ([a-z_]+):", top_level_on_block(text), re.M))
        self.assertEqual(keys, {"workflow_dispatch", "schedule"})

    def test_write_permission_only_on_the_job(self):
        text = (WF / "write.yml").read_text()
        self.assertRegex(text, r"(?m)^permissions: \{\}$")
        self.assertEqual(len(re.findall(r"contents: write", text)), 1)
        self.assertRegex(text, r"\n  write:\n(?:    .*\n|\n)*?    permissions:\n      contents: write\n")
        self.assertRegex((WF / "tests.yml").read_text(), r"(?m)^permissions:\n  contents: read$")

    def test_concurrency_group(self):
        self.assertRegex((WF / "write.yml").read_text(),
                         r"concurrency:\n  group: writer\n  cancel-in-progress: false")

    def test_sync_to_tip_precedes_the_writer(self):
        text = (WF / "write.yml").read_text()
        sync = text.index("git reset --hard FETCH_HEAD")
        self.assertLess(sync, text.index("python3 -m nflchanges.writer"))

    def test_backup_minutes_differ_from_dispatch_minutes(self):
        crons = re.findall(r"cron: '([^']+)'", (WF / "write.yml").read_text())
        self.assertEqual(len(crons), 1)
        minutes = {int(m) for m in crons[0].split()[0].split(",")}
        self.assertTrue(minutes)
        self.assertFalse(minutes & DISPATCH_MINUTES)

    def test_guard_runs_before_every_commit(self):
        script = (ROOT / ".github" / "scripts" / "commit_and_push.sh").read_text()
        loop = script[script.index("while true"):]
        self.assertLess(loop.index("nflchanges.guard"), loop.index("git commit"))
        self.assertNotIn("--force", script)
        self.assertNotIn("push -f", script)


if __name__ == "__main__":
    unittest.main()
