"""ESPN adapter: translates ESPN's v3 fantasy payloads (via `app.espn.parse`) into the provider-neutral models.

ESPN has no documented public fantasy API and no third-party authorization (no OAuth). This adapter is
therefore enabled only for the two cases this deployment can defend:
  (a) the deployment owner's own leagues, read with the owner's ESPN session cookies from the server
      environment (`AccessMethod.OWNER_ENV`; the credential object exposes `espn_s2` / `swid`), and
  (b) leagues ESPN serves without cookies (public leagues), only when ESPN_PUBLIC_IMPORT_ENABLED is on
      (`AccessMethod.PUBLIC`).
Cookies are never collected from users, never stored here, never logged and never put in an error or
repr. Every error message below is a fixed string; upstream exception text is dropped.

Raw access is injectable: `client_factory(key, access)` returns an object with the methods of
`app.espn.client.EspnClient` (`league()`, `transactions(week)`, `player_pool(period)`,
`player_projections(week)`); tests pass fixtures from `tests/espn_fixture.py` and never touch the network.
Note: espn_api's request layer sets no HTTP timeout; a hung ESPN connection blocks until the OS gives up.

Identity: player ids are namespaced `espn:<id>`. ESPN->GSIS links live in the analytics database
(`player_id_map`), which this adapter deliberately does not open, so `RosterEntry.identity` is None here
and the caller resolves it (id-based, from that table) - never by name.

ESPN lineup slot ids (app.espn.constants.SLOT_MAP): 23 FLEX = {RB,WR,TE}; 7 OP = SUPERFLEX {QB,RB,WR,TE};
3 RB/WR and 5 WR/TE are their own two-position slots. Slots we cannot classify keep their own label.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from ...espn.constants import PROJECTION_LIMIT, SLOT_MAP
from ...espn.parse import anon_member, ms_to_dt, parse_league, parse_player_pool, parse_projection_week, parse_rosters, parse_transactions
from ..capabilities import Capability, CapabilityState, CapabilityUnavailable, ProviderCapabilities, Support
from ..context import AccessMethod, LeagueKey, Provider
from ..identity import ProviderPlayerId
from ..models import (CANONICAL_STATS, FLEX_POSITIONS, SUPERFLEX_POSITIONS, LeagueSettings, LineupRules, LineupSlot,
                      PlayoffSettings, Provenance, Roster, RosterEntry, ScoringRules, SlotKind, Sourced, Team,
                      TradeSettings, WaiverSettings)
from .base import (FreeAgent, LeagueAccess, LeagueNotFound, LeagueProvider, LeagueSummary, Matchup, ProviderError,
                   ProviderUnavailable, Standing, Transaction)

DISABLED_REASON = (
    "ESPN has no documented public fantasy API and no third-party authorization. Private leagues need the "
    "owner's espn_s2/SWID session cookies, which this service does not collect from users. ESPN import is "
    "available only for the deployment owner's own league (server-side credentials) or, when "
    "ESPN_PUBLIC_IMPORT_ENABLED is set, for leagues ESPN serves without credentials.")

T = TypeVar("T")
ClientFactory = Callable[[LeagueKey, LeagueAccess], Any]

# ---- lineup -----------------------------------------------------------------------------------

_POSITION = {"D/ST": "DST", "DT": "DL", "DE": "DL", "CB": "DB", "S": "DB"}      # ESPN position label -> canonical
_BENCH_SLOT, _IR_SLOT = 20, 21
_UNKNOWN_BENCH_SLOTS = {22}       # ESPN's blank-labelled slot; the existing parser also treats it as a non-starter

# ESPN lineupSlotId -> (canonical code, eligible canonical positions)
_STARTER_SLOTS: dict[int, tuple[str, frozenset[str]]] = {
    0: ("QB", frozenset({"QB"})), 2: ("RB", frozenset({"RB"})), 4: ("WR", frozenset({"WR"})),
    6: ("TE", frozenset({"TE"})), 17: ("K", frozenset({"K"})), 16: ("DST", frozenset({"DST"})),
    23: ("FLEX", FLEX_POSITIONS),
    7: ("SUPERFLEX", SUPERFLEX_POSITIONS),                      # ESPN "OP" (offensive player)
    3: ("WRRB_FLEX", frozenset({"RB", "WR"})), 5: ("REC_FLEX", frozenset({"WR", "TE"})),
    8: ("DT", frozenset({"DL"})), 9: ("DE", frozenset({"DL"})), 11: ("DL", frozenset({"DL"})),
    10: ("LB", frozenset({"LB"})), 12: ("CB", frozenset({"DB"})), 13: ("S", frozenset({"DB"})),
    14: ("DB", frozenset({"DB"})), 15: ("IDP_FLEX", frozenset({"DL", "LB", "DB"})),
}


def _slot_label(slot_id: int) -> str:
    return SLOT_MAP.get(slot_id, f"slot{slot_id}") or f"slot{slot_id}"


def slot_for(slot_id: int) -> tuple[str, frozenset[str]]:
    known = _STARTER_SLOTS.get(slot_id)
    if known:
        return known
    label = _slot_label(slot_id)                 # P, HC, TQB, ER, Rookie ...: kept visible, not guessed
    return label, frozenset({label})


def lineup_rules(raw_counts: dict) -> LineupRules:
    slots = []
    for key, count in raw_counts.items():
        try:
            sid, n = int(key), int(count)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        if sid == _BENCH_SLOT:
            slots.append(LineupSlot("BN", n, SlotKind.BENCH, frozenset(), "BE"))
        elif sid == _IR_SLOT:
            slots.append(LineupSlot("IR", n, SlotKind.INJURED_RESERVE, frozenset(), "IR"))
        elif sid in _UNKNOWN_BENCH_SLOTS:
            slots.append(LineupSlot(f"UNKNOWN_{sid}", n, SlotKind.BENCH, frozenset(), f"slot{sid}"))
        else:
            code, eligible = slot_for(sid)
            slots.append(LineupSlot(code, n, SlotKind.STARTER, eligible, _slot_label(sid)))
    return LineupRules(tuple(slots))


# ---- scoring ----------------------------------------------------------------------------------

# ESPN scoringItems statId -> canonical stat. Ids and names follow the espn-api package's PLAYER_STATS_MAP.
# Only unambiguous, flat per-unit rules are mapped; distance-bucketed kicking, bonuses and defence stay
# verbatim in provider_rules and are listed as unmapped. 41 and 53 are both "receptions" in that map;
# 53 is the id the existing ESPN code uses for PPR, so only 53 is mapped.
_SCORING_MAP: dict[int, str] = {
    3: "pass_yd", 4: "pass_td", 20: "pass_int", 19: "pass_2pt", 64: "pass_sack",
    24: "rush_yd", 25: "rush_td", 26: "rush_2pt",
    53: "rec", 42: "rec_yd", 43: "rec_td", 44: "rec_2pt",
    72: "fum_lost", 63: "fum_rec_td",
    83: "fg_made", 85: "fg_miss", 86: "xp_made", 88: "xp_miss",
}
assert set(_SCORING_MAP.values()) <= CANONICAL_STATS


def scoring_rules(items: list[dict]) -> ScoringRules:
    """ESPN rules keyed by stat id (verbatim). Per-slot `pointsOverrides` (how ESPN expresses things like a
    TE reception premium) are kept as `<statId>@slot<slotId>` entries and reported unmapped, so a consumer
    never reads the base `rec` value as applying to every position."""
    verbatim: dict[str, float] = {}
    canonical: dict[str, float] = {}
    unmapped: list[str] = []
    for item in items or []:
        stat, points = item.get("statId"), item.get("points")
        if not isinstance(stat, int) or isinstance(points, bool) or not isinstance(points, (int, float)):
            continue
        verbatim[str(stat)] = float(points)
        if stat in _SCORING_MAP:
            canonical[_SCORING_MAP[stat]] = float(points)
        else:
            unmapped.append(str(stat))
        for slot, value in (item.get("pointsOverrides") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                key = f"{stat}@slot{slot}"
                verbatim[key] = float(value)
                unmapped.append(key)
    return ScoringRules(provider_rules=verbatim, canonical=canonical, unmapped=tuple(unmapped))


# ---- translation ------------------------------------------------------------------------------

def canonical_position(label: str | None) -> str | None:
    return None if not label else _POSITION.get(label, label)


def _manager_id(member_id: Any) -> str | None:
    hashed = anon_member(member_id)               # "m_<12 hex of sha256>"; the raw SWID is never kept
    return f"espn:sha256:{hashed[2:]}" if isinstance(hashed, str) and hashed.startswith("m_") else None


def settings_from(key: LeagueKey, raw: dict, now: datetime) -> LeagueSettings:
    parsed = parse_league(raw, swid=None, my_team_id=None, synced_at=now)["league"]
    s = raw.get("settings") or {}
    is_public = s.get("isPublic")
    budget = parsed["faab_budget"]
    waivers = (WaiverSettings("faab", budget if isinstance(budget, int) else None)
               if parsed["is_faab"] else WaiverSettings(None))
    last_reg = parsed["reg_season_matchups"] if isinstance(parsed["reg_season_matchups"], int) else None
    return LeagueSettings(
        key=key, name=parsed["name"], team_count=parsed["size"],
        lineup=lineup_rules((s.get("rosterSettings") or {}).get("lineupSlotCounts") or {}),
        scoring=scoring_rules((s.get("scoringSettings") or {}).get("scoringItems") or []),
        waivers=waivers,
        trades=TradeSettings(deadline_at=parsed["trade_deadline"]),
        playoffs=PlayoffSettings(teams=parsed["playoff_teams"], start_week=last_reg + 1 if last_reg else None,
                                 last_regular_season_week=last_reg),
        league_type=None, is_public=is_public if isinstance(is_public, bool) else None)


def teams_from(key: LeagueKey, raw: dict, now: datetime) -> list[Team]:
    rows = parse_league(raw, swid=None, my_team_id=None, synced_at=now)["teams"]
    names = {m.get("id"): (m.get("displayName") or " ".join(x for x in (m.get("firstName"), m.get("lastName")) if x))
             for m in raw.get("members", [])}
    owners_by_team = {t["id"]: t.get("owners") or [] for t in raw.get("teams", [])}
    out = []
    for r in rows:
        owners = owners_by_team.get(r["team_id"], [])
        out.append(Team(
            key=key, external_team_id=str(r["team_id"]), name=r["name"],
            manager_ids=tuple(m for m in (_manager_id(o) for o in owners) if m),
            manager_display_names=tuple(n for n in (names.get(o) for o in owners) if n),
            wins=r["wins"], losses=r["losses"], ties=r["ties"], points_for=r["points_for"]))
    return out


def rosters_from(key: LeagueKey, raw: dict, now: datetime) -> list[Roster]:
    period = raw.get("scoringPeriodId") or 1
    parsed = parse_rosters(raw, league_id=raw.get("id"), season=raw.get("seasonId"), scoring_period=period,
                           synced_at=now)
    players = {p["espn_player_id"]: p for p in parsed["players"]}
    by_team: dict[int, list[RosterEntry]] = {t["id"]: [] for t in raw.get("teams", [])}
    for row in parsed["lineups"]:
        pid = row["espn_player_id"]
        if pid is None:
            continue
        slot = row["lineup_slot_id"]
        p = players.get(pid, {})
        if slot == _BENCH_SLOT or slot in _UNKNOWN_BENCH_SLOTS or slot is None:
            kind, code = SlotKind.BENCH, None
        elif slot == _IR_SLOT:
            kind, code = SlotKind.INJURED_RESERVE, None
        else:
            kind, code = SlotKind.STARTER, slot_for(slot)[0]
        by_team[row["team_id"]].append(RosterEntry(
            player=ProviderPlayerId(Provider.ESPN, str(pid)), slot_kind=kind, slot_code=code,
            name=p.get("full_name"), position=canonical_position(p.get("position")),
            nfl_team=p.get("pro_team"), identity=None))
    return [Roster(key=key, external_team_id=str(tid), entries=tuple(entries)) for tid, entries in by_team.items()]


def matchups_from(raw: dict, week: int, now: datetime) -> list[Matchup]:
    out = []
    for m in parse_league(raw, swid=None, my_team_id=None, synced_at=now)["matchups"]:
        if m["matchup_period"] != week:
            continue
        sides = [(m["home_team_id"], m["home_points"]), (m["away_team_id"], m["away_points"])]
        sides = [(t, p) for t, p in sides if t is not None]            # a bye has no away side
        out.append(Matchup(week=week, external_matchup_id=str(m["matchup_id"]),
                           team_ids=tuple(str(t) for t, _ in sides),
                           points=tuple(float(p) if isinstance(p, (int, float)) else None for _, p in sides)))
    return out


def standings_from(raw: dict, now: datetime) -> list[Standing]:
    rows = parse_league(raw, swid=None, my_team_id=None, synced_at=now)["teams"]
    out = [Standing(external_team_id=str(r["team_id"]), rank=r["playoff_seed"] if isinstance(r["playoff_seed"], int) else None,
                    wins=r["wins"], losses=r["losses"], ties=r["ties"], points_for=r["points_for"]) for r in rows]
    return sorted(out, key=lambda s: (s.rank is None, s.rank or 0, int(s.external_team_id)))


_TX_KIND = {"FREEAGENT": "free_agent", "WAIVER": "waiver", "TRADE_ACCEPT": "trade", "TRADE_UPHOLD": "trade"}


def transactions_from(raw: dict, key: LeagueKey, now: datetime) -> list[Transaction]:
    """Executed adds, drops and trades. Failed / pending / cancelled attempts and pure lineup moves are not
    transactions that changed a roster, so they are left out."""
    parsed = parse_transactions(raw, league_id=int(key.external_league_id), season=key.season, synced_at=now)
    items: dict[str, list[dict]] = {}
    for it in parsed["items"]:
        items.setdefault(it["transaction_id"], []).append(it)
    out = []
    for t in parsed["transactions"]:
        kind = _TX_KIND.get(t["type"])
        if kind is None or t["status"] != "EXECUTED":
            continue
        adds: dict[str, str] = {}
        drops: dict[str, str] = {}
        for it in items.get(t["transaction_id"], []):
            pid = it["espn_player_id"]
            if pid is None:
                continue
            ns = ProviderPlayerId(Provider.ESPN, str(pid)).namespaced
            if it["item_type"] in ("ADD", "TRADE") and (it["to_team_id"] or t["team_id"]) is not None:
                adds[ns] = str(it["to_team_id"] if it["to_team_id"] is not None else t["team_id"])
            if it["item_type"] in ("DROP", "TRADE") and (it["from_team_id"] or t["team_id"]) is not None:
                drops[ns] = str(it["from_team_id"] if it["from_team_id"] is not None else t["team_id"])
        if not adds and not drops:
            continue
        teams = dict.fromkeys(str(x) for x in [t["team_id"], *adds.values(), *drops.values()]
                              if x is not None and str(x) != "-1")
        out.append(Transaction(external_id=t["transaction_id"], kind=kind, week=t["scoring_period"],
                               team_ids=tuple(teams),
                               adds=adds, drops=drops))
    return out


# ---- the adapter -------------------------------------------------------------------------------

def _settings_flags() -> tuple[bool, bool]:
    from ...config import get_settings
    s = get_settings()
    return bool(getattr(s, "espn_public_import_enabled", False)), bool(getattr(s, "has_espn_credentials", False))


def _default_client(key: LeagueKey, access: LeagueAccess):
    from ...espn.client import EspnClient
    cred = access.credential if access.method is AccessMethod.OWNER_ENV else None
    return EspnClient(int(key.external_league_id), key.season, getattr(cred, "espn_s2", None),
                      getattr(cred, "swid", None))


class EspnProvider(LeagueProvider):
    provider = Provider.ESPN
    data_origin = "live"

    def __init__(self, client_factory: ClientFactory | None = None, *, public_import_enabled: bool | None = None,
                 owner_credentials_configured: bool | None = None, clock: Callable[[], datetime] | None = None):
        flags = None if public_import_enabled is not None and owner_credentials_configured is not None else _settings_flags()
        self.public_import_enabled = public_import_enabled if public_import_enabled is not None else flags[0]
        self.owner_credentials_configured = (owner_credentials_configured if owner_credentials_configured is not None
                                             else flags[1])
        self._factory = client_factory or _default_client
        self._clock = clock or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def capabilities(self) -> ProviderCapabilities:
        enabled = self.public_import_enabled or self.owner_credentials_configured
        return ProviderCapabilities(provider=Provider.ESPN, enabled=enabled,
                                    reason=None if enabled else DISABLED_REASON, states=_states())

    # -- plumbing -------------------------------------------------------------------------------

    def _client(self, key: LeagueKey, access: LeagueAccess):
        if access.method is AccessMethod.PUBLIC:
            if not self.public_import_enabled:
                raise ProviderUnavailable("ESPN public-league import is not enabled in this deployment")
        elif access.method is AccessMethod.OWNER_ENV:
            cred = access.credential
            if not (getattr(cred, "espn_s2", None) and getattr(cred, "swid", None)):
                raise ProviderUnavailable("The deployment owner's ESPN session is not configured")
        else:
            raise ProviderUnavailable("ESPN offers no OAuth or other third-party authorization")
        return self._factory(key, access)

    def _prov(self, source: str) -> Provenance:
        return Provenance(Provider.ESPN, f"espn:{source}", self._clock())

    def _build(self, fn, *args):
        """Run a pure translation; a payload that does not have the expected shape is a provider fault."""
        return _guard(lambda: fn(*args))

    def _league_raw(self, key: LeagueKey, access: LeagueAccess, client: Any = None) -> dict:
        client = client or self._client(key, access)
        raw = _guard(client.league)
        if not isinstance(raw, dict):
            raise ProviderUnavailable("ESPN returned an unexpected response")
        if raw.get("seasonId") not in (None, key.season) or str(raw.get("id", key.external_league_id)) != key.external_league_id:
            raise LeagueNotFound("ESPN returned a different league or season than requested")
        return raw

    # -- the interface --------------------------------------------------------------------------

    def discover_leagues(self, access: LeagueAccess, *, user_handle: str, season: int) -> Sourced[list[LeagueSummary]]:
        self.require(Capability.DISCOVER_LEAGUES)      # UNSUPPORTED: always raises
        raise AssertionError("unreachable")            # pragma: no cover

    def fetch_settings(self, key: LeagueKey, access: LeagueAccess) -> Sourced[LeagueSettings]:
        self.require(Capability.LEAGUE_SETTINGS)
        raw = self._league_raw(key, access)
        return Sourced(self._build(settings_from, key, raw, self._clock()), self._prov("mSettings"))

    def fetch_scoring(self, key: LeagueKey, access: LeagueAccess) -> Sourced[ScoringRules]:
        self.require(Capability.SCORING_RULES)
        raw = self._league_raw(key, access)
        return Sourced(self._build(settings_from, key, raw, self._clock()).scoring, self._prov("mSettings"))

    def fetch_teams(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Team]]:
        self.require(Capability.TEAMS)
        raw = self._league_raw(key, access)
        return Sourced(self._build(teams_from, key, raw, self._clock()), self._prov("mTeam"))

    def fetch_rosters(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Roster]]:
        self.require(Capability.ROSTERS)
        raw = self._league_raw(key, access)
        return Sourced(self._build(rosters_from, key, raw, self._clock()), self._prov("mRoster"))

    def fetch_matchups(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[list[Matchup]]:
        self.require(Capability.MATCHUPS)
        raw = self._league_raw(key, access)
        return Sourced(self._build(matchups_from, raw, week, self._clock()), self._prov("mMatchup"))

    def fetch_standings(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Standing]]:
        self.require(Capability.STANDINGS)
        raw = self._league_raw(key, access)
        return Sourced(self._build(standings_from, raw, self._clock()), self._prov("mStandings"))

    def fetch_free_agents(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[FreeAgent]]:
        self.require(Capability.FREE_AGENTS)
        client = self._client(key, access)
        league = self._league_raw(key, access, client)
        pool = _guard(lambda: client.player_pool(league.get("scoringPeriodId") or 1))
        rows = self._build(lambda: parse_player_pool(pool, season=key.season, synced_at=self._clock()))
        out = [FreeAgent(player=ProviderPlayerId(Provider.ESPN, str(r["espn_player_id"])).namespaced,
                         name=r["full_name"], position=canonical_position(r["position"]))
               for r in rows if r.get("availability_status") == "FREEAGENT"]
        return Sourced(out, self._prov("kona_player_info:FREEAGENT"))

    def fetch_transactions(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[list[Transaction]]:
        self.require(Capability.TRANSACTIONS)
        client = self._client(key, access)
        raw = _guard(lambda: client.transactions(week))
        return Sourced(self._build(transactions_from, raw, key, self._clock()), self._prov("mTransactions2"))

    def fetch_projections(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[dict[str, float]]:
        self.require(Capability.PROJECTIONS)
        client = self._client(key, access)
        raw = _guard(lambda: client.player_projections(week))
        parsed = self._build(lambda: parse_projection_week(
            raw, season=key.season, scoring_period=week, synced_at=self._clock(), rostered_ids=(),
            expected_rostered_ids=(), limit=PROJECTION_LIMIT))
        out = {ProviderPlayerId(Provider.ESPN, str(r["espn_player_id"])).namespaced: float(r["projected_points"])
               for r in parsed["rows"] if r["projected_points"] is not None}
        return Sourced(out, self._prov("kona_player_info:projections"))


def _guard(fn: Callable[[], T]) -> T:
    """Run an ESPN call or translation; failures become sanitized ProviderError subclasses with fixed text.
    The replacement is raised after the handler finished, so the original exception (whose text could echo
    request details) is neither chained nor kept as `__context__`."""
    failure: ProviderError | None = None
    try:
        return fn()
    except (ProviderError, CapabilityUnavailable):
        raise
    except Exception as exc:                          # noqa: BLE001 - classified below, text dropped
        name = type(exc).__name__
        if name == "ESPNAccessDenied":
            failure = LeagueNotFound("ESPN will not show this league without credentials this service does not hold")
        elif name == "ESPNInvalidLeague":
            failure = LeagueNotFound("ESPN has no NFL league with this id")
        elif isinstance(exc, OSError):                # requests' exceptions are OSErrors
            failure = ProviderUnavailable("ESPN could not be reached")
        elif isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)) or name == "ESPNUnknownError":
            failure = ProviderUnavailable("ESPN returned an unexpected response")
        else:                                         # anything else: still never let upstream text through
            failure = ProviderUnavailable("ESPN request failed unexpectedly")
    raise failure


def _states() -> dict[Capability, CapabilityState]:
    S = Support
    undocumented = "ESPN's v3 fantasy API is undocumented and may change without notice."
    return {
        Capability.DISCOVER_LEAGUES: CapabilityState(S.UNSUPPORTED, (
            "No documented league-discovery endpoint; leagues are connected by id. (The cookie-based fan API that "
            "lists a user's leagues is not used.)")),
        Capability.LEAGUE_SETTINGS: CapabilityState(S.SUPPORTED, f"mSettings view. {undocumented}"),
        Capability.TEAMS: CapabilityState(S.SUPPORTED, "mTeam view; manager ids are one-way hashes, never SWIDs."),
        Capability.ROSTERS: CapabilityState(S.SUPPORTED, (
            "mRoster view. Player ids are espn:<id>; GSIS identity links are resolved by the caller from the "
            "analytics player_id_map (id-based), not by this adapter.")),
        Capability.SCORING_RULES: CapabilityState(S.SUPPORTED, "scoringItems verbatim by stat id; per-slot overrides (e.g. TE premium) kept and reported unmapped."),
        Capability.MATCHUPS: CapabilityState(S.SUPPORTED, "mMatchup view; week = matchup period."),
        Capability.STANDINGS: CapabilityState(S.SUPPORTED, "mStandings view (playoff seed as rank)."),
        Capability.FREE_AGENTS: CapabilityState(S.SUPPORTED, "kona_player_info with FREEAGENT status; players on waivers are not included."),
        Capability.TRANSACTIONS: CapabilityState(S.SUPPORTED, "mTransactions2; executed adds, drops and trades only."),
        Capability.PROJECTIONS: CapabilityState(S.SUPPORTED, "ESPN's own per-week projection under the league's scoring (kona_player_info)."),
        Capability.PRIVATE_LEAGUES: CapabilityState(S.REQUIRES_AUTH, "owner deployment only; no approved third-party authorization method"),
    }
