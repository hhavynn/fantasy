import assert from "node:assert/strict";
import { test } from "node:test";
import {
  PORTFOLIO_CONTRACT, exposureHref, filterHref, filterPlayers, heaviestByeWeek, injuryWord, leagueHref, leagueStatus, parseFilters,
  parsePortfolio, portfolioProblem, sortValue,
} from "./portfolio.ts";
import { sortRows } from "./sort.ts";

// Invented leagues, teams and players only.
const LG = (n: number) => `lg_${"invented0portfolio"}${n}`;
const league = (n: number, over: Record<string, unknown> = {}) => ({
  league_id: LG(n), provider: n % 2 ? "sleeper" : "espn", external_league_id: String(9000 + n), season: 2026, name: `Invented ${n}`,
  visibility: "private", status: "active", role: "viewer", pinned: false, perspective_team_id: "3", sync_state: "ready",
  sync_issue: null, data: { data_version: "v1", global_version: "g1", committed_at: "2026-10-20T12:00:00Z" }, ...over,
});
const exposure = (n: number, over: Record<string, unknown> = {}) => ({
  league_id: LG(n), provider: n % 2 ? "sleeper" : "espn", league_name: `Invented ${n}`, team_id: 3, team_name: `Team ${n}`,
  player_id: 101, slot: "WR", is_starter: true, reserve: false, injury_status: null, projection: 11.2, projection_origin: "provider",
  week: 8, bye_week: 9, synced_at: "2026-10-20T12:00:00Z", stale: false,
  identity: { status: "verified", gsis_id: "00-0000001", reason: null }, ...over,
});
const player = (key: string, name: string, leagues: number, over: Record<string, unknown> = {}) => ({
  key, name, position: "WR", nfl_team: "AAA", identity: { status: "verified", gsis_id: "00-0000001", reason: null },
  leagues, of: 3, teams: leagues, starters: 1, bye_week: 9, bye_reason: null,
  injury: { designated: 0, statuses: [] }, projections: { known: 1, missing: 0 },
  exposures: Array.from({ length: leagues }, (_, i) => exposure(i)), ...over,
});
const payload = (over: Record<string, unknown> = {}) => ({
  contract: PORTFOLIO_CONTRACT, generated_at: "2026-10-20T13:00:00Z", limit: 25, season: 2026,
  leagues: [
    { league: league(0), state: "ready", reason: null, stale: false, team: { team_id: 3, name: "Team 0" }, week: 8, final_week: 17,
      synced_at: "2026-10-20T12:00:00Z", roster_size: 16, projection: { source: "ESPN", available: true, reason: null, known: 15, total: 16 },
      provenance: { contract: "signal-provenance.v1", signals: [] } },
    { league: league(1), state: "needs_team", reason: "No team is saved for you in this league.", stale: false, team: null, week: null },
    { league: league(2), state: "ready", reason: null, stale: true, team: { team_id: 3, name: "Team 2" }, week: 8 },
  ],
  excluded: { count: 1, leagues: [{ league: league(5, { season: 2025 }), reason: "other_season" }] },
  players: [
    player("gsis:00-0000001", "Avery Example", 2),
    player("espn:202", "Blake Sample", 1, { position: "RB", bye_week: null, bye_reason: "Schedule unknown",
      injury: { designated: 1, statuses: ["OUT"] }, projections: { known: 0, missing: 1 },
      identity: { status: "espn_id", gsis_id: null, reason: "Joined by ESPN id" },
      exposures: [exposure(2, { player_id: 202, projection: null, projection_origin: "provider", league_name: "Zeta Invented" })] }),
    player("league:x:5", "Casey Placeholder", 1, { bye_week: 11, starters: 0 }),
  ],
  weeks: [
    { week: 8, leagues_playing: 2, bye: { exposures: 0, players: 0, leagues: 0, starters: 0, unknown: 1, names: [] },
      injuries: { exposures: 1, players: 1, leagues: 1, leagues_reporting: 2 } },
    { week: 9, leagues_playing: 2, bye: { exposures: 3, players: 2, leagues: 2, starters: 2, unknown: 1, names: ["Avery Example"] }, injuries: null },
    { week: 11, leagues_playing: 2, bye: { exposures: 1, players: 1, leagues: 1, starters: 0, unknown: 1, names: ["Casey Placeholder"] }, injuries: null },
  ],
  coverage: { leagues_ready: 2, leagues_total: 3, exposures: 4, players: 3, projections_known: 3, projections_missing: 1, bye_unknown: 1 },
  ...over,
});

test("parsePortfolio reads the contract and refuses anything else", () => {
  const p = parsePortfolio(payload());
  assert.ok(p);
  assert.equal(p.leagues.length, 3);
  assert.equal(p.excluded[0].reason, "other_season");
  assert.equal(p.players[1].exposures[0].projection, null);                    // unknown stays null
  assert.equal(p.players[1].exposures[0].projectionOrigin, null);              // no origin for a missing number
  assert.equal(p.players[1].byeWeek, null);
  assert.equal(p.weeks[1].injuries, null);
  assert.equal(p.coverage.leaguesReady, 2);
  assert.equal(parsePortfolio({ ...payload(), contract: "portfolio.v0" }), null);
  assert.equal(parsePortfolio(null), null);
  assert.equal(parsePortfolio({ contract: PORTFOLIO_CONTRACT }), null);
});

test("anything but an explicit false is not fresh", () => {
  const p = parsePortfolio(payload())!;
  assert.equal(p.leagues[0].stale, false);
  assert.equal(p.leagues[1].stale, false);
  assert.equal(p.leagues[2].stale, true);
  assert.equal(leagueStatus(p.leagues[2]).label, "Not fresh");
  assert.equal(leagueStatus(p.leagues[1]).label, "No team saved");
  assert.match(leagueStatus(p.leagues[1]).detail, /No team is saved/);
  const missing = parsePortfolio(payload({ leagues: [{ league: league(0), state: "ready" }] }))!;
  assert.equal(missing.leagues[0].stale, true);
});

test("an unknown identity status reads as not linked, never as verified", () => {
  const p = parsePortfolio(payload({ players: [player("k", "Dee", 1, { identity: { status: "trust-me", gsis_id: "00-0000009" } })] }))!;
  assert.equal(p.players[0].identity.status, "unverified");
});

test("search matches player, NFL team, league and team names; filters by position and shared", () => {
  const p = parsePortfolio(payload())!;
  const names = (f: Parameters<typeof filterPlayers>[1]) => filterPlayers(p.players, f).map((x) => x.name);
  assert.deepEqual(names({ q: "avery", pos: null, shared: false }), ["Avery Example"]);
  assert.deepEqual(names({ q: "zeta", pos: null, shared: false }), ["Blake Sample"]);
  assert.deepEqual(names({ q: "", pos: "RB", shared: false }), ["Blake Sample"]);
  assert.deepEqual(names({ q: "", pos: null, shared: true }), ["Avery Example"]);
  assert.deepEqual(names({ q: "nobody", pos: null, shared: false }), []);
});

test("parseFilters accepts only known positions and trims the query", () => {
  assert.deepEqual(parseFilters({ q: "  Avery  ", pos: "WR", shared: "1" }), { q: "Avery", pos: "WR", shared: true });
  assert.deepEqual(parseFilters({ pos: "<script>", q: "x".repeat(200) }), { q: "x".repeat(60), pos: null, shared: false });
});

test("a missing bye sorts last in both directions, not as week 0", () => {
  const p = parsePortfolio(payload())!;
  const asc = sortRows(p.players, (x) => sortValue(x, "bye"), "asc").map((x) => x.byeWeek);
  const desc = sortRows(p.players, (x) => sortValue(x, "bye"), "desc").map((x) => x.byeWeek);
  assert.deepEqual(asc, [9, 11, null]);
  assert.deepEqual(desc, [11, 9, null]);
  assert.deepEqual(sortRows(p.players, (x) => sortValue(x, "leagues"), "desc").map((x) => x.name)[0], "Avery Example");
});

test("links stay in their own league", () => {
  const p = parsePortfolio(payload())!;
  assert.equal(exposureHref(p.players[0].exposures[0]), `/l/${LG(0)}/players?id=101`);
  assert.equal(leagueHref(p.leagues[0]), `/l/${LG(0)}`);
  const syncing = parsePortfolio(payload({ leagues: [{ league: league(4, { sync_state: "first_sync_running" }), state: "not_ready", stale: false }] }))!;
  assert.equal(leagueHref(syncing.leagues[0]), `/leagues?league=${LG(4)}`);
});

test("filterHref changes one filter and keeps the sort", () => {
  assert.equal(filterHref({ sort: "bye", dir: "asc", pos: "WR" }, { pos: "RB" }), "?sort=bye&dir=asc&pos=RB");
  assert.equal(filterHref({ pos: "WR", q: "a" }, { pos: null, q: null }), "?");
});

test("the heaviest bye week is the one with the most roster spots on bye", () => {
  const p = parsePortfolio(payload())!;
  assert.equal(heaviestByeWeek(p.weeks)?.week, 9);
  assert.equal(heaviestByeWeek([]), null);
});

test("words for designations and problems", () => {
  assert.equal(injuryWord("INJURY_RESERVE"), "Injured reserve");
  assert.equal(injuryWord("SOMETHING_NEW"), "Something new");
  assert.equal(portfolioProblem({ status: 503, error: "x" }).tone, "caution");
  assert.doesNotMatch(portfolioProblem({ status: 500, error: "http://internal:8000 trace" }).body, /internal/);
});

test("an exposure whose league id is not a league id is dropped, never put into a link", () => {
  const bad = player("gsis:00-0000001", "Avery Example", 1, {
    exposures: [exposure(0), exposure(1, { league_id: "../../admin" }), exposure(2, { league_id: "lg_x" })],
  });
  const parsed = parsePortfolio(payload({ players: [bad] }));
  assert.ok(parsed);
  const ids = parsed.players[0].exposures.map((e) => e.leagueId);
  assert.deepEqual(ids, [LG(0)]);
});
