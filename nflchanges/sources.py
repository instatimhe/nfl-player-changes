"""Where the writer's inputs come from: Sleeper's public NFL endpoints, or a recorded copy.

Three public endpoints, and no others. None of them is specific to any fantasy league:
  - the NFL state (current season and week),
  - the current week's projections feed, whose rows embed each player's team and injury
    state (read every run),
  - the full NFL player list (read at most a few times a day; Sleeper's docs ask that it be
    called sparingly).
tests/test_source_urls.py fails if any other Sleeper path appears in this package.

RecordedSource replays a directory of saved responses. It serves two purposes: the local
rehearsal, and the push-retry path in .github/scripts/commit_and_push.sh, which re-runs
the writer on a newer tip from the SAME inputs instead of fetching again.
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

from .record import iso_now

STATE_URL = "https://api.sleeper.app/v1/state/nfl"
PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
FEED_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
FEED_URL = ("https://api.sleeper.com/projections/nfl/{season}/{week}?season_type=regular"
            + "".join(f"&position[]={p}" for p in FEED_POSITIONS))
USER_AGENT = "nfl-player-changes-writer/1"
TIMEOUT_S = 90

INPUT_FILES = {"state": "state.json", "feed": "feed.json", "players": "players_nfl.json"}
META_FILE = "meta.json"   # {"completed_at": ISO}: when the saved reads finished


class SourceUnavailable(Exception):
    """A source could not be read. `code` is a short fixed label for the run stamp."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SourceUnavailable("http_error", f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceUnavailable("network_error", type(e).__name__) from None
    except json.JSONDecodeError:
        raise SourceUnavailable("not_json") from None


class LiveSource:
    def __init__(self, save_dir=None):
        self.save_dir = Path(save_dir) if save_dir else None
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)

    completed_at = None

    def _save(self, kind, data):
        self.completed_at = iso_now()
        if self.save_dir:
            (self.save_dir / INPUT_FILES[kind]).write_text(json.dumps(data))
            (self.save_dir / META_FILE).write_text(json.dumps({"completed_at": self.completed_at}))
        return data

    def state(self):
        return self._save("state", _get_json(STATE_URL))

    def feed(self, season, week):
        return self._save("feed", _get_json(FEED_URL.format(season=season, week=week)))

    def players(self):
        return self._save("players", _get_json(PLAYERS_URL))


class RecordedSource:
    """Serves state.json / feed.json / players_nfl.json from one directory. A missing file
    is SourceUnavailable('not_recorded'), exactly as a failed fetch would be."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.reads = []
        meta = self.dir / META_FILE
        self.completed_at = json.loads(meta.read_text()).get("completed_at") if meta.exists() else None

    def _load(self, kind):
        self.reads.append(kind)
        path = self.dir / INPUT_FILES[kind]
        if not path.exists():
            raise SourceUnavailable("not_recorded")
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            raise SourceUnavailable("not_json") from None

    def state(self):
        return self._load("state")

    def feed(self, season, week):
        return self._load("feed")

    def players(self):
        return self._load("players")
