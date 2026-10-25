"""Sleeper adapter: the existing read-only Sleeper foundation (`app/sleeper`) behind `LeagueProvider`.

Source mode (`SLEEPER_SOURCE`, read through `get_settings().sleeper_source`):
  * disabled (default) - the adapter is off. Live Sleeper import stays off until Sleeper's terms for this
                         use are confirmed and the deployment is approved; see
                         docs/multileague/provider-capability-matrix.md.
  * fixture            - recorded-shape files under `fixture_data/` through the real `SleeperClient`
                         (provenance `fixture:...`, `data_origin == "fixture"`). Works without network.
  * live               - the real API at api.sleeper.app through `SleeperClient`.

Reuse: parsing is `app.sleeper.models`, the schedule arithmetic is `app.sleeper.normalize`, and player
identity is `app.sleeper.id_map` (ID-map evidence, never names). This module only translates.

Access model: Sleeper's API needs no credentials and a league is readable by anyone who has its id. So
`LeagueSettings.is_public` is True, meaning "the provider serves it without credentials" - not that the
league is listed anywhere, and not that the connecting user owns it. `LeagueAccess.credential` is ignored
and never stored.

Capability decisions that are not obvious:
  * STANDINGS is DERIVED: Sleeper has no standings endpoint; we order the rosters' win/loss/points record.
  * FREE_AGENTS is DERIVED: Sleeper has no free-agent endpoint. We list every active NFL player at a
    fantasy position that is not on any roster of this league, from the (cached, once-a-day) player list.
    Players on waivers or locked by the league's own rules are not distinguished.
  * PROJECTIONS is UNSUPPORTED: the documented API has no projections endpoint.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import weakref
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, TypeVar

from ...sleeper.client import (InvalidLeagueId, SleeperClient, SleeperError, SleeperHTTPError, SleeperRateLimited,
                               check_league_id)
from ...sleeper.constants import WAIVER_TYPE_FAAB, nflverse_team
from ...sleeper.id_map import PlayerMapping, SleeperIdMap, load_id_map, map_players
from ...sleeper.models import (SleeperLeague, SleeperPlayer, SleeperRoster, SleeperUser, parse_league,
                               parse_players, parse_rosters, parse_users, sid)
from ...sleeper.league import rostered_player_ids
from ...sleeper.normalize import LEAGUE_TYPES, schedule_weeks
from ..capabilities import Capability, CapabilityState, CapabilityUnavailable, ProviderCapabilities, Support
from ..context import LeagueKey, Provider
from ..identity import Confidence, IdentityLink, IdentityMethod, ProviderPlayerId
from ..models import (FLEX_POSITIONS, SUPERFLEX_POSITIONS, LeagueSettings, LineupRules, LineupSlot, PlayoffSettings,
                      Provenance, Roster, RosterEntry, ScoringRules, SlotKind, Sourced, Team, TradeSettings,
                      WaiverSettings, CANONICAL_STATS)
from .base import (FreeAgent, LeagueAccess, LeagueNotFound, LeagueProvider, LeagueSummary, Matchup, ProviderError,
                   ProviderUnavailable, Standing, Transaction)
from .fixtures import fixture_transport, load_fixture_id_map

# Shown to visitors verbatim (the /leagues host card), so it names no settings or values. For operators: Sleeper is
# enabled by SLEEPER_SOURCE=live (the real API, once its terms for this use are confirmed and the deployment approved)
# or SLEEPER_SOURCE=fixture (recorded demo data only); see the module docstring and app/config.py.
DISABLED_REASON = (
    "Live Sleeper import is off: Sleeper's API terms for this use have not been confirmed and this deployment has "
    "not been approved for it yet.")

T = TypeVar("T")
_SOURCES = ("disabled", "fixture", "live")
_HANDLE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_FANTASY_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})

# ---- lineup -----------------------------------------------------------------------------------

# Sleeper roster_positions entry -> (canonical code, eligible canonical positions). Eligibility is
# spelled out so FLEX and SUPER_FLEX can never be mistaken for one another.
_STARTER_SLOTS: dict[str, tuple[str, frozenset[str]]] = {
    "QB": ("QB", frozenset({"QB"})),
    "RB": ("RB", frozenset({"RB"})),
    "WR": ("WR", frozenset({"WR"})),
    "TE": ("TE", frozenset({"TE"})),
    "K": ("K", frozenset({"K"})),
    "DEF": ("DST", frozenset({"DST"})),
    "FLEX": ("FLEX", FLEX_POSITIONS),
    "SUPER_FLEX": ("SUPERFLEX", SUPERFLEX_POSITIONS),
    "WRRB_FLEX": ("WRRB_FLEX", frozenset({"RB", "WR"})),
    "REC_FLEX": ("REC_FLEX", frozenset({"WR", "TE"})),
    "DL": ("DL", frozenset({"DL"})),
    "LB": ("LB", frozenset({"LB"})),
    "DB": ("DB", frozenset({"DB"})),
    "IDP_FLEX": ("IDP_FLEX", frozenset({"DL", "LB", "DB"})),
}
_BENCH = "BN"


def slot_for(provider_code: str) -> tuple[str, frozenset[str]]:
    """Canonical (code, eligibility) for a Sleeper starting slot. A slot this adapter has never seen keeps
    its own label as both code and sole eligible position, so it stays visible instead of being guessed."""
    return _STARTER_SLOTS.get(provider_code, (provider_code, frozenset({provider_code})))


def canonical_position(position: str | None) -> str | None:
    return None if not position else ("DST" if position == "DEF" else position)


def lineup_rules(league: SleeperLeague) -> LineupRules:
    counts = Counter(p for p in league.roster_positions if p != _BENCH)
    slots = [LineupSlot(code=slot_for(p)[0], count=n, kind=SlotKind.STARTER, eligible=slot_for(p)[1],
                        provider_code=p) for p, n in counts.items()]
    bench = sum(1 for p in league.roster_positions if p == _BENCH)
    if bench:
        slots.append(LineupSlot("BN", bench, SlotKind.BENCH, frozenset(), "BN"))
    reserve = _count(league.settings.get("reserve_slots"))
    if reserve:
        slots.append(LineupSlot("IR", reserve, SlotKind.INJURED_RESERVE, frozenset(), "reserve"))
    taxi = _count(league.settings.get("taxi_slots"))
    if taxi:
        slots.append(LineupSlot("TAXI", taxi, SlotKind.TAXI, frozenset(), "taxi"))
    return LineupRules(tuple(slots))


def _count(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


# ---- scoring ----------------------------------------------------------------------------------

# Sleeper scoring_settings key -> canonical stat. Anything not listed (bonuses, distance kickers, IDP,
# defense) is kept verbatim in `provider_rules` and named in `unmapped`.
_SCORING_MAP: dict[str, str] = {
    "pass_yd": "pass_yd", "pass_td": "pass_td", "pass_int": "pass_int", "pass_2pt": "pass_2pt",
    "pass_sack": "pass_sack",
    "rush_yd": "rush_yd", "rush_td": "rush_td", "rush_2pt": "rush_2pt",
    "rec": "rec", "rec_yd": "rec_yd", "rec_td": "rec_td", "rec_2pt": "rec_2pt",
    "bonus_rec_te": "rec_te_bonus",
    "fum_lost": "fum_lost", "fum_rec_td": "fum_rec_td",
    "fgm": "fg_made", "fgmiss": "fg_miss", "xpm": "xp_made", "xpmiss": "xp_miss",
}
assert set(_SCORING_MAP.values()) <= CANONICAL_STATS


def scoring_rules(league: SleeperLeague) -> ScoringRules:
    """The league's rules verbatim, plus the subset we can name canonically. A missing `rec` key stays
    missing (`points_per_reception` is then None); nothing defaults to PPR."""
    verbatim: dict[str, float] = {}
    unmapped: list[str] = []
    canonical: dict[str, float] = {}
    for key, raw in league.scoring_settings.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            unmapped.append(key)           # not a points value; not representable in provider_rules
            continue
        verbatim[key] = float(raw)
        if key in _SCORING_MAP:
            canonical[_SCORING_MAP[key]] = float(raw)
        else:
            unmapped.append(key)
    return ScoringRules(provider_rules=verbatim, canonical=canonical, unmapped=tuple(unmapped))


# ---- settings, teams, rosters ------------------------------------------------------------------

def _waivers(settings: Mapping[str, Any]) -> WaiverSettings:
    wtype = settings.get("waiver_type")
    if wtype is None:
        return WaiverSettings(kind=None)
    clear = settings.get("waiver_clear_days")
    clear_days = clear if isinstance(clear, int) and not isinstance(clear, bool) else None
    if wtype == WAIVER_TYPE_FAAB:
        budget = settings.get("waiver_budget")
        return WaiverSettings("faab", budget if isinstance(budget, int) and not isinstance(budget, bool) else None,
                              clear_days=clear_days)
    kind = {0: "rolling", 1: "reverse_standings"}.get(wtype)
    return WaiverSettings(kind, clear_days=clear_days)


def _trades(settings: Mapping[str, Any]) -> TradeSettings:
    deadline = settings.get("trade_deadline")
    week = deadline if isinstance(deadline, int) and not isinstance(deadline, bool) and deadline > 0 else None
    review = settings.get("trade_review_days")
    return TradeSettings(deadline_week=week,
                         review_days=review if isinstance(review, int) and not isinstance(review, bool) else None)


def _playoffs(league: SleeperLeague) -> PlayoffSettings:
    s = league.settings
    sched = schedule_weeks(league)
    teams = s.get("playoff_teams")
    start = s.get("playoff_week_start")
    return PlayoffSettings(
        teams=teams if isinstance(teams, int) and teams > 0 else None,
        start_week=start if isinstance(start, int) and start > 0 else None,
        last_regular_season_week=sched["last_regular_season_week"],
        week_per_round={0: 1, 2: 2}.get(s.get("playoff_round_type")))     # 1 = longer final: no single value


def settings_from(key: LeagueKey, league: SleeperLeague) -> LeagueSettings:
    return LeagueSettings(
        key=key, name=league.name, team_count=league.total_rosters, lineup=lineup_rules(league),
        scoring=scoring_rules(league), waivers=_waivers(league.settings), trades=_trades(league.settings),
        playoffs=_playoffs(league), league_type=LEAGUE_TYPES.get(league.settings.get("type")),
        is_public=True)       # served without credentials to anyone holding the id; see module docstring


def _manager_ids(r: SleeperRoster) -> tuple[str, ...]:
    return tuple(f"sleeper:{o}" for o in dict.fromkeys(x for x in (r.owner_id, *r.co_owners) if x))


def teams_from(key: LeagueKey, users: list[SleeperUser], rosters: list[SleeperRoster]) -> list[Team]:
    by_id = {u.user_id: u for u in users}
    out = []
    for r in rosters:
        owners = [o for o in dict.fromkeys(x for x in (r.owner_id, *r.co_owners) if x)]
        primary = by_id.get(r.owner_id) if r.owner_id else None
        name = (primary.team_name or primary.display_name) if primary else None
        out.append(Team(
            key=key, external_team_id=str(r.roster_id), name=name or f"Team {r.roster_id}",
            manager_ids=_manager_ids(r),
            manager_display_names=tuple(by_id[o].display_name for o in owners
                                        if o in by_id and by_id[o].display_name),
            wins=r.wins, losses=r.losses, ties=r.ties, points_for=r.points_for))
    return out


def _ppid(raw_id: str) -> ProviderPlayerId:
    try:
        return ProviderPlayerId(Provider.SLEEPER, raw_id)
    except ValueError:
        raise ProviderUnavailable("Sleeper returned a malformed player id") from None


def identity_link(player: ProviderPlayerId, m: PlayerMapping) -> IdentityLink:
    """ID-map evidence -> contract IdentityLink. Unresolved players keep their reason; nothing is matched by name."""
    try:
        if m.method == "ff_playerids":
            return IdentityLink(player, m.gsis_id, None, IdentityMethod.ID_MAP, Confidence.HIGH, m.notes)
        if m.method == "sleeper_gsis":
            return IdentityLink(player, m.gsis_id, None, IdentityMethod.PROVIDER_GSIS, Confidence.MEDIUM, m.notes)
        if m.method == "team_defense" and m.nfl_team:
            return IdentityLink(player, None, m.nfl_team, IdentityMethod.TEAM_DEFENSE, Confidence.HIGH, m.notes)
        if m.method == "conflict":
            return IdentityLink(player, None, None, IdentityMethod.CONFLICT, Confidence.NONE, m.notes)
    except ValueError:                        # e.g. a published gsis id that is not 00-NNNNNNN
        return IdentityLink(player, None, None, IdentityMethod.UNMATCHED, Confidence.NONE,
                            "Source gsis id is malformed; left unresolved")
    reason = m.notes or "No id-based evidence"
    return IdentityLink(player, None, None, IdentityMethod.UNMATCHED, Confidence.NONE, reason)


def _starting_codes(league: SleeperLeague) -> list[str]:
    return [slot_for(p)[0] for p in league.roster_positions if p != _BENCH]


def rosters_from(key: LeagueKey, league: SleeperLeague, rosters: list[SleeperRoster],
                 players: dict[str, SleeperPlayer], idmap: SleeperIdMap) -> list[Roster]:
    report = map_players(rostered_player_ids(rosters), players, idmap, log_unmatched=False)
    codes = _starting_codes(league)
    out = []
    for r in rosters:
        entries: list[RosterEntry] = []
        placed: set[str] = set()

        def entry(pid: str, kind: SlotKind, slot: str | None) -> None:
            p = players.get(pid)
            ppid = _ppid(pid)
            entries.append(RosterEntry(
                player=ppid, slot_kind=kind, slot_code=slot,
                name=p.full_name if p else None,
                position=canonical_position(p.position) if p else None,
                nfl_team=nflverse_team(p.team) if p else None,
                identity=identity_link(ppid, report.mappings[pid])))
            placed.add(pid)

        for i, pid in enumerate(r.starters):
            if pid is not None and pid not in placed:           # None = an empty starting slot: no player
                entry(pid, SlotKind.STARTER, codes[i] if i < len(codes) else None)
        for pid in r.reserve:
            if pid not in placed:
                entry(pid, SlotKind.INJURED_RESERVE, None)
        for pid in r.taxi:
            if pid not in placed:
                entry(pid, SlotKind.TAXI, None)
        for pid in r.players:
            if pid not in placed:
                entry(pid, SlotKind.BENCH, None)
        out.append(Roster(key=key, external_team_id=str(r.roster_id), entries=tuple(entries)))
    return out


def standings_from(rosters: list[SleeperRoster]) -> list[Standing]:
    """Win/loss/points order of the rosters. Sleeper publishes no standings, so rank is our ordering
    (wins, then ties, then points for); division and league tiebreakers are not applied."""
    known = any(r.wins is not None for r in rosters)

    def order(r: SleeperRoster):
        return (-(r.wins or 0), -(r.ties or 0), r.losses or 0, -(r.points_for or 0.0), r.roster_id)

    ranked = sorted(rosters, key=order)
    return [Standing(external_team_id=str(r.roster_id), rank=i if known else None, wins=r.wins, losses=r.losses,
                     ties=r.ties, points_for=r.points_for)
            for i, r in enumerate(ranked, 1)]


def matchups_from(raw: list[dict], week: int) -> list[Matchup]:
    groups: dict[str, list[dict]] = {}
    for row in raw:
        rid = row.get("roster_id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            raise ValueError("matchup row without an integer roster_id")
        mid = sid(row.get("matchup_id"))
        groups.setdefault(mid if mid is not None else f"unpaired-{rid}", []).append(row)
    out = []
    for mid, rows in groups.items():
        out.append(Matchup(
            week=week, external_matchup_id=mid, team_ids=tuple(str(r["roster_id"]) for r in rows),
            points=tuple(float(r["points"]) if isinstance(r.get("points"), (int, float))
                         and not isinstance(r.get("points"), bool) else None for r in rows)))
    return out


def transactions_from(raw: list[dict], week: int) -> list[Transaction]:
    """Completed transactions only: a failed waiver claim must not look like an add. Draft picks and
    FAAB amounts are not carried (the contract has no field for them)."""
    out = []
    for t in raw:
        if t.get("status") != "complete":
            continue
        tid = sid(t.get("transaction_id"))
        if tid is None:
            continue

        def moves(field: str) -> dict[str, str]:
            return {_ppid(str(pid)).namespaced: str(rid) for pid, rid in (t.get(field) or {}).items()}

        leg = t.get("leg")
        out.append(Transaction(
            external_id=tid, kind=str(t.get("type") or "unknown"),
            week=leg if isinstance(leg, int) and not isinstance(leg, bool) else week,
            team_ids=tuple(str(r) for r in (t.get("roster_ids") or [])),
            adds=moves("adds"), drops=moves("drops")))
    return out


def free_agents_from(all_players: Mapping[str, Mapping], rostered: set[str]) -> list[FreeAgent]:
    out = []
    for pid, raw in all_players.items():
        if pid in rostered or raw.get("position") not in _FANTASY_POSITIONS or raw.get("active") is not True:
            continue
        if raw.get("position") != "DEF" and not sid(raw.get("team")):
            continue                                            # no NFL team: retired or unsigned
        p = ProviderPlayerId(Provider.SLEEPER, pid) if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", pid) else None
        if p is None:
            continue
        name = raw.get("full_name") or " ".join(x for x in (raw.get("first_name"), raw.get("last_name")) if x) or None
        out.append(FreeAgent(player=p.namespaced, name=name, position=canonical_position(raw.get("position"))))
    return sorted(out, key=lambda f: f.player)


# ---- error translation -------------------------------------------------------------------------

def _guard(fn: Callable[[], T]) -> T:
    """Run a provider call; failures become sanitized ProviderError subclasses. Original messages (URLs,
    bodies) are dropped, and the replacement is raised after the handler finished, so the original
    exception is neither chained (`__cause__`) nor kept as `__context__`."""
    failure: ProviderError | None = None
    try:
        return fn()
    except (ProviderError, CapabilityUnavailable):
        raise
    except InvalidLeagueId:
        failure = LeagueNotFound("Sleeper has no NFL league with this id")
    except SleeperRateLimited:
        failure = ProviderUnavailable("Sleeper is rate limiting requests; try again later")
    except SleeperHTTPError as e:
        failure = ProviderUnavailable(f"Sleeper returned HTTP {e.status}")
    except OSError:
        failure = ProviderUnavailable("Sleeper could not be reached")
    except (SleeperError, ValueError, KeyError, TypeError, AttributeError):
        failure = ProviderUnavailable("Sleeper returned an unexpected response")
    except Exception:                            # noqa: BLE001 - never let upstream text through
        failure = ProviderUnavailable("Sleeper request failed unexpectedly")
    raise failure


def _guard_idmap(load: Callable[[], SleeperIdMap]) -> SleeperIdMap:
    try:
        return load()
    except Exception:                            # network, csv/parquet, a missing optional dependency
        failure = ProviderUnavailable("The player id map is unavailable; rosters cannot be linked yet")
    raise failure


# ---- the adapter -------------------------------------------------------------------------------

def _default_source() -> str:
    from ...config import get_settings
    return str(getattr(get_settings(), "sleeper_source", "disabled") or "disabled").strip().lower()


class SleeperProvider(LeagueProvider):
    provider = Provider.SLEEPER

    def __init__(self, client: SleeperClient | None = None, idmap: SleeperIdMap | None = None, *,
                 source: str | None = None, clock: Callable[[], datetime] | None = None):
        src = (source if source is not None else _default_source()).strip().lower()
        self.source = src if src in _SOURCES else "disabled"
        self._client = client
        self._idmap = idmap
        self._clock = clock or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    # -- configuration --------------------------------------------------------------------------

    @property
    def data_origin(self) -> str | None:
        """"live" | "fixture" | None (disabled): what a LeagueView should report as `data_origin`."""
        return self.source if self.source in ("live", "fixture") else None

    @property
    def client(self) -> SleeperClient:
        if self._client is None:
            if self.source == "fixture":
                # A temp cache dir: fixture mode must never write into (or read from) the live data dir.
                cache = tempfile.mkdtemp(prefix="sleeper-fixture-")
                weakref.finalize(self, shutil.rmtree, cache, True)       # removed with the adapter
                self._client = SleeperClient(transport=fixture_transport(), cache_dir=cache)
            else:
                self._client = SleeperClient()
        return self._client

    def _id_map(self) -> SleeperIdMap:
        if self._idmap is None:
            self._idmap = _guard_idmap(load_fixture_id_map if self.source == "fixture" else load_id_map)
        return self._idmap

    def capabilities(self) -> ProviderCapabilities:
        enabled = self.source in ("fixture", "live")
        return ProviderCapabilities(
            provider=Provider.SLEEPER, enabled=enabled, reason=None if enabled else DISABLED_REASON,
            states=_states(self.source))

    # -- provenance -----------------------------------------------------------------------------

    def _prov(self, *paths: str) -> Provenance:
        prefix = "fixture:sleeper:" if self.source == "fixture" else "sleeper:"
        return Provenance(Provider.SLEEPER, prefix + ",".join(paths), self._clock())

    # -- fetching -------------------------------------------------------------------------------

    def _league(self, key: LeagueKey) -> SleeperLeague:
        """The parsed league, checked against the requested season."""
        lid = check_league_id(key.external_league_id)
        league = _guard(lambda: parse_league(self.client.league(lid)))
        if league.season is not None and league.season != key.season:
            raise LeagueNotFound(f"Sleeper league is for season {league.season}, not {key.season}")
        return league

    def _raw(self, path: str):
        """GET a path the typed client has no method for, through its cache, retry and error handling."""
        return self.client.get_json(path)

    def _list(self, path: str) -> list:
        raw = self._raw(path) or []
        if not isinstance(raw, list):
            raise ValueError("expected a list")
        return raw

    def discover_leagues(self, access: LeagueAccess, *, user_handle: str, season: int) -> Sourced[list[LeagueSummary]]:
        self.require(Capability.DISCOVER_LEAGUES)
        handle = str(user_handle).strip()
        if not _HANDLE.fullmatch(handle):
            raise LeagueNotFound("Sleeper user not found")
        if not isinstance(season, int) or isinstance(season, bool):
            raise ValueError("season must be an int")

        def fetch():
            user = self._raw(f"/user/{handle}")
            user_id = sid(user.get("user_id")) if isinstance(user, dict) else None
            if user_id is None:
                raise LeagueNotFound("Sleeper user not found")
            return user_id, self._list(f"/user/{user_id}/leagues/nfl/{season}")

        user_id, raw = _guard(fetch)
        out = []
        for row in raw:
            lid = sid(row.get("league_id")) if isinstance(row, dict) else None
            if lid is None or not lid.isdigit():
                continue
            teams = row.get("total_rosters")
            out.append(LeagueSummary(key=LeagueKey(Provider.SLEEPER, lid, season), name=row.get("name"),
                                     team_count=teams if isinstance(teams, int) else None, is_public=True))
        return Sourced(out, self._prov(f"/user/{user_id}/leagues/nfl/{season}"))

    def fetch_settings(self, key: LeagueKey, access: LeagueAccess) -> Sourced[LeagueSettings]:
        self.require(Capability.LEAGUE_SETTINGS)
        league = self._league(key)
        return Sourced(settings_from(key, league), self._prov(f"/league/{key.external_league_id}"))

    def fetch_scoring(self, key: LeagueKey, access: LeagueAccess) -> Sourced[ScoringRules]:
        self.require(Capability.SCORING_RULES)
        league = self._league(key)
        return Sourced(scoring_rules(league), self._prov(f"/league/{key.external_league_id}"))

    def fetch_teams(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Team]]:
        self.require(Capability.TEAMS)
        self._league(key)
        lid = check_league_id(key.external_league_id)
        rosters, users = _guard(lambda: (parse_rosters(self.client.league_rosters(lid)),
                                         parse_users(self.client.league_users(lid))))
        return Sourced(teams_from(key, users, rosters), self._prov(f"/league/{lid}/rosters", f"/league/{lid}/users"))

    def fetch_rosters(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Roster]]:
        self.require(Capability.ROSTERS)
        league = self._league(key)
        lid = check_league_id(key.external_league_id)

        def fetch():
            rosters = parse_rosters(self.client.league_rosters(lid))
            wanted = set(rostered_player_ids(rosters))
            return rosters, (parse_players(self.client.players(), only=wanted) if wanted else {})

        rosters, players = _guard(fetch)
        built = rosters_from(key, league, rosters, players, self._id_map())
        return Sourced(built, self._prov(f"/league/{lid}/rosters", "/players/nfl"))

    def fetch_matchups(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[list[Matchup]]:
        self.require(Capability.MATCHUPS)
        week = _week(week)
        self._league(key)
        lid = check_league_id(key.external_league_id)
        built = _guard(lambda: matchups_from(self._list(f"/league/{lid}/matchups/{week}"), week))
        return Sourced(built, self._prov(f"/league/{lid}/matchups/{week}"))

    def fetch_standings(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Standing]]:
        self.require(Capability.STANDINGS)
        self._league(key)
        lid = check_league_id(key.external_league_id)
        rosters = _guard(lambda: parse_rosters(self.client.league_rosters(lid)))
        return Sourced(standings_from(rosters), self._prov(f"/league/{lid}/rosters"))

    def fetch_free_agents(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[FreeAgent]]:
        self.require(Capability.FREE_AGENTS)
        self._league(key)
        lid = check_league_id(key.external_league_id)
        rosters, everyone = _guard(lambda: (parse_rosters(self.client.league_rosters(lid)), self.client.players()))
        built = free_agents_from(everyone, set(rostered_player_ids(rosters)))
        return Sourced(built, self._prov("/players/nfl", f"/league/{lid}/rosters"))

    def fetch_transactions(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[list[Transaction]]:
        self.require(Capability.TRANSACTIONS)
        week = _week(week)
        self._league(key)
        lid = check_league_id(key.external_league_id)
        built = _guard(lambda: transactions_from(self._list(f"/league/{lid}/transactions/{week}"), week))
        return Sourced(built, self._prov(f"/league/{lid}/transactions/{week}"))

    def fetch_projections(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[dict[str, float]]:
        self.require(Capability.PROJECTIONS)       # UNSUPPORTED: always raises CapabilityUnavailable
        raise AssertionError("unreachable: PROJECTIONS is not offered")      # pragma: no cover


def _week(week: int) -> int:
    if not isinstance(week, int) or isinstance(week, bool) or not 1 <= week <= 22:
        raise ValueError("week must be an integer from 1 to 22")
    return week


def _states(source: str) -> dict[Capability, CapabilityState]:
    S = Support
    return {
        Capability.DISCOVER_LEAGUES: CapabilityState(S.SUPPORTED, (
            "GET /user/{username}, then /user/{user_id}/leagues/nfl/{season}. A Sleeper username is public: "
            "listing a user's leagues proves nothing about who is asking.")),
        Capability.LEAGUE_SETTINGS: CapabilityState(S.SUPPORTED, "GET /league/{league_id}; roster slots, waiver, trade and playoff settings."),
        Capability.TEAMS: CapabilityState(S.SUPPORTED, "Teams are Sleeper rosters; external team id is the roster_id."),
        Capability.ROSTERS: CapabilityState(S.SUPPORTED, (
            "Rosters from /league/{id}/rosters plus the player list; GSIS links come from the DynastyProcess ID map "
            "and Sleeper's own gsis_id, never from names.")),
        Capability.SCORING_RULES: CapabilityState(S.SUPPORTED, "scoring_settings kept verbatim; known keys also mapped to canonical stats."),
        Capability.MATCHUPS: CapabilityState(S.SUPPORTED, "GET /league/{id}/matchups/{week}; Sleeper cannot tell an unplayed week from 0 points."),
        Capability.STANDINGS: CapabilityState(S.DERIVED, (
            "Sleeper has no standings endpoint; ranked by wins, ties, points for from the rosters' records. "
            "League tiebreakers and divisions are not applied.")),
        Capability.FREE_AGENTS: CapabilityState(S.DERIVED, (
            "Sleeper has no free-agent endpoint. Derived as the active NFL players at QB/RB/WR/TE/K/DEF who are "
            "on no roster in this league; players on waivers or locked by league rules are not distinguished.")),
        Capability.TRANSACTIONS: CapabilityState(S.SUPPORTED, "GET /league/{id}/transactions/{week}; completed transactions only, draft picks and FAAB bids not carried."),
        Capability.PROJECTIONS: CapabilityState(S.UNSUPPORTED, "Sleeper's documented API publishes no projections."),
        Capability.PRIVATE_LEAGUES: CapabilityState(S.UNSUPPORTED, (
            "Sleeper leagues are readable by id; Sleeper has no per-user private access model in its public API.")),
    }
