"""The writer reads three public Sleeper endpoints and nothing else. A league, user or
roster endpoint anywhere in the code or workflows fails this test."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ("/league/", "/leagues/", "/user/", "/users/", "/roster")
ALLOWED_URL_PREFIXES = (
    "https://api.sleeper.app/v1/state/nfl",
    "https://api.sleeper.app/v1/players/nfl",
    "https://api.sleeper.com/projections/nfl/",
)
_URL = re.compile(r"https?://[^\s\"'`)]+")


def scanned_files():
    files = sorted((ROOT / "nflchanges").glob("*.py"))
    files += sorted((ROOT / ".github").rglob("*.yml")) + sorted((ROOT / ".github").rglob("*.sh"))
    return files


def forbidden_hits(text):
    return [f for f in FORBIDDEN if f in text]


def sleeper_urls(text):
    return [u for u in _URL.findall(text) if "sleeper" in u]


class TestSourceUrls(unittest.TestCase):
    def test_scans_something(self):
        names = {p.name for p in scanned_files()}
        self.assertIn("sources.py", names)
        self.assertIn("writer.py", names)
        self.assertIn("write.yml", names)

    def test_no_league_user_or_roster_path(self):
        for path in scanned_files():
            self.assertEqual(forbidden_hits(path.read_text()), [], path.name)

    def test_only_the_three_public_endpoints(self):
        found = []
        for path in scanned_files():
            for u in sleeper_urls(path.read_text()):
                found.append(u)
                self.assertTrue(u.startswith(ALLOWED_URL_PREFIXES), (path.name, u))
        self.assertGreaterEqual(len(found), 3)

    def test_the_check_fires(self):
        self.assertEqual(forbidden_hits('URL = "https://api.sleeper.app/v1/league/{id}/rosters"'),
                         ["/league/", "/roster"])
        self.assertEqual(forbidden_hits('"https://api.sleeper.app/v1/user/{id}"'), ["/user/"])


if __name__ == "__main__":
    unittest.main()
