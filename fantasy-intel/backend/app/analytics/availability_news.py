"""Owner-curated availability news for live production runs (the projection engine doc, section
"Availability news").

No synced source carries a reported injury timeline ("out 6-10 weeks") or a reinstatement the nflverse weekly
rosters have not published yet (a player removed from the Commissioner's Exempt List is still EXE there until the
next roster file). Without them a rest-of-season run projects an injured starter at full strength from the next
week on (ESPN OUT describes the next game only) and withholds a reinstated one for every remaining week. This
module turns one reviewed file of such facts (availability_news.json, next to this module) into per-week
availability the live run applies:

  out     from `from_week` the player misses every game before `return_window[0]`; in the window his P(plays)
          ramps uniformly over his team's games (P(back by the k-th of n window games) = k / n); after it he is
          assumed back. Without a window he is out for the rest of the season.
  active  from `from_week` he is on the active roster whatever the latest synced weekly roster says.

The probability is the reported range read as a uniform return date, not a fitted estimate; every affected row is
flagged with the entry's note. Training never reads this module: the artifact, its approval and its evidence are
unchanged, and the run's content digest moves only when an entry changes what a row says.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

NEWS_VERSION = "news-0.1.0"
DEFAULT_PATH = Path(__file__).with_name("availability_news.json")
SCHEMA = "availability-news/v1"
KINDS = ("out", "active")
OVERRIDDEN = "availability news overridden by the synced report"   # flag prefix of a row the live run kept


@dataclass(frozen=True)
class Entry:
    season: int
    gsis_id: str
    name: str
    team: str
    kind: str
    from_week: int
    return_window: tuple[int, int] | None
    reported_on: str
    note: str
    espn_player_id: int | None = None


@dataclass(frozen=True)
class NewsState:
    """What the news says about one player in one target week. p: P(plays), 0 = out, 1 = available."""
    gsis_id: str
    kind: str
    p: float
    reason: str


def parse(data: dict) -> list[Entry]:
    if data.get("schema") != SCHEMA:
        raise ValueError(f"availability news: schema {data.get('schema')!r}, expected {SCHEMA!r}")
    out, seen = [], set()
    for raw in data.get("entries") or []:
        win = raw.get("return_window")
        e = Entry(season=int(raw["season"]), gsis_id=str(raw["gsis_id"]), name=str(raw["name"]), team=str(raw["team"]),
                  kind=str(raw["kind"]), from_week=int(raw["from_week"]),
                  return_window=None if win is None else (int(win[0]), int(win[1])),
                  reported_on=str(raw["reported_on"]), note=str(raw.get("note") or ""),
                  espn_player_id=raw.get("espn_player_id"))
        if e.kind not in KINDS:
            raise ValueError(f"availability news for {e.name}: kind {e.kind!r} is not one of {KINDS}")
        if e.return_window is not None:
            if e.kind != "out":
                raise ValueError(f"availability news for {e.name}: only an 'out' entry has a return window")
            if not e.from_week <= e.return_window[0] <= e.return_window[1]:
                raise ValueError(f"availability news for {e.name}: return window {e.return_window} must start at or "
                                 f"after from_week {e.from_week} and end at or after its start")
        if (e.season, e.gsis_id) in seen:
            raise ValueError(f"availability news for {e.name}: more than one entry for {e.season}")
        seen.add((e.season, e.gsis_id))
        out.append(e)
    return out


def load(path: Path | None = None) -> list[Entry]:
    """The entries of the news file; a missing file is no news."""
    p = DEFAULT_PATH if path is None else path
    return parse(json.loads(p.read_text())) if p.exists() else []


def state(entry: Entry, week: int, team_weeks: list[int] | None) -> NewsState | None:
    """The entry's statement about target week `week`, or None when it says nothing about that week.
    `team_weeks`: the regular-season weeks the player's team plays (the window ramp counts games, so a bye in the
    window is not a step)."""
    if week < entry.from_week:
        return None
    src = f"availability news ({entry.reported_on}): {entry.note}".rstrip(": ")
    if entry.kind == "active":
        return NewsState(entry.gsis_id, "active", 1.0, f"{src} Treated as on the active roster from week "
                                                       f"{entry.from_week}.")
    if entry.return_window is None:
        return NewsState(entry.gsis_id, "out", 0.0, f"{src} Out for the rest of the season.")
    a, b = entry.return_window
    if week < a:
        return NewsState(entry.gsis_id, "out", 0.0, f"{src} Out before week {a} (reported return weeks {a}-{b}).")
    if week > b:
        return NewsState(entry.gsis_id, "returned", 1.0, f"{src} Assumed back after the reported return weeks {a}-{b}.")
    games = sorted(w for w in (team_weeks or range(a, b + 1)) if a <= w <= b)
    done = sum(1 for w in games if w <= week)
    p = round(done / len(games), 4) if games else 1.0
    return NewsState(entry.gsis_id, "return_window", p,
                     f"{src} Reported return weeks {a}-{b}: P(back by week {week}) {p:.0%} ({done} of {len(games)} "
                     "team games in the window, return date read as uniform).")


def for_week(entries: list[Entry], season: int, week: int, team_weeks: dict[str, list[int]]) -> dict[str, NewsState]:
    """{gsis_id: NewsState} of every entry that says something about (season, week)."""
    out = {}
    for e in entries:
        if e.season != season:
            continue
        s = state(e, week, team_weeks.get(e.team))
        if s is not None:
            out[e.gsis_id] = s
    return out


__all__ = ["NEWS_VERSION", "OVERRIDDEN", "DEFAULT_PATH", "Entry", "NewsState", "parse", "load", "state", "for_week"]
