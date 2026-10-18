import assert from "node:assert/strict";
import { test } from "node:test";
import {
  absenceLabel, filterEntries, freshnessLabel, gameLabel, groupEntries, hoursAgo, injuryHref, parseInjuryFilters,
  rosterActions, rosterText, snapSeries, waiversHref, type InjuryEntry, type Roster, type Teammate,
} from "./injury-impact.ts";

const roster = (over: Partial<Roster> = {}): Roster => ({
  status: "ROSTERED", fantasy_team_id: 5, fantasy_team: "Team Five", is_mine: false, percent_owned: 40, ...over,
});

const mate = (id: string, over: Partial<Teammate> = {}): Teammate => ({
  gsis_id: id, espn_player_id: Number(id.replace(/\D/g, "")) || 1, full_name: id, position: "RB", depth_rank: 2,
  availability: "AVAILABLE", healthy: true, recent_volume_per_game: 5,
  observed: { with: null, without: null, delta_per_game: null },
  estimate: { share: 0.5, extra_per_game: 6, basis_volume_per_game: 5, basis_weeks: [4, 5, 6] },
  espn_projection: null, roster: roster(), ...over,
});

const entry = (id: string, over: Partial<InjuryEntry> = {}): InjuryEntry => ({
  gsis_id: id, espn_player_id: 1, full_name: id, position: "RB", team: "KC",
  status: { level: "OUT", sources: [], espn_injury_status: null, nfl_report_status: "Out", injury: null, practice_status: null },
  absence: { kind: "new", last_played_week: 6, consecutive_team_games_missed: 0 },
  workload: {
    games: 6, team_games: 6, last_played_week: 6, snap_share: 0.7, recent_snap_share: 0.7, recent_weeks: [3, 4, 5, 6],
    target_share: null, backfield_share: null, targets_per_game: 4, carries_per_game: 14, pass_attempts_per_game: null,
    xfp_per_game: 15, ppr_per_game: 14, role_trend: null,
    weekly: [{ week: 1, played: true, snap_share: 0.7, opportunities: 18 }, { week: 2, played: false, snap_share: null, opportunities: null }],
  },
  roster: roster(), espn_projection: null,
  game: { week: 7, opponent: "BUF", is_home: true, kickoff: null, started: false, on_bye: false },
  freshness: null,
  impact: { unit: "touches", unit_label: "carries + targets", vacated_per_game: 18, team_games_without: 0,
    clarity: { label: "no_history", text: "" }, teammates: [], receivers: [] },
  affects_my_roster: false, ...over,
});

test("filters: defaults, case-insensitive values, junk falls back", () => {
  assert.deepEqual(parseInjuryFilters(undefined), { pos: "ALL", status: "all" });
  assert.deepEqual(parseInjuryFilters({ pos: "rb", status: "OUT" }), { pos: "RB", status: "out" });
  assert.deepEqual(parseInjuryFilters({ pos: "K", status: "healthy" }), { pos: "ALL", status: "all" });
  assert.deepEqual(parseInjuryFilters({ pos: ["WR", "TE"] }), { pos: "WR", status: "all" });
});

test("filterEntries follows the API's position and status rules", () => {
  const q = entry("q", { position: "WR", status: { ...entry("x").status, level: "QUESTIONABLE" } });
  const d = entry("d", { status: { ...entry("x").status, level: "DOUBTFUL" } });
  const ir = entry("ir", { position: "TE", status: { ...entry("x").status, level: "IR" } });
  const all = [q, d, ir];
  assert.deepEqual(filterEntries(all, { pos: "ALL", status: "all" }).map((e) => e.gsis_id), ["q", "d", "ir"]);
  assert.deepEqual(filterEntries(all, { pos: "ALL", status: "out" }).map((e) => e.gsis_id), ["d", "ir"]);
  assert.deepEqual(filterEntries(all, { pos: "ALL", status: "questionable" }).map((e) => e.gsis_id), ["q"]);
  assert.deepEqual(filterEntries(all, { pos: "TE", status: "all" }).map((e) => e.gsis_id), ["ir"]);
});

test("href keeps the team and other params, null removes a key", () => {
  assert.equal(injuryHref({ team: "5", pos: "RB" }, { status: "out" }), "?team=5&pos=RB&status=out");
  assert.equal(injuryHref({ team: "5", pos: "RB" }, { pos: null }), "?team=5");
  assert.equal(injuryHref({}, { pos: null }), "?");
});

test("groups: upcoming, long-term, then kicked off or bye", () => {
  const g = groupEntries([
    entry("a"),
    entry("b", { absence: { kind: "long_term", last_played_week: 2, consecutive_team_games_missed: 4 } }),
    entry("c", { game: { week: 7, opponent: null, is_home: null, kickoff: null, started: false, on_bye: true } }),
    entry("d", { game: { week: 7, opponent: "BUF", is_home: false, kickoff: null, started: true, on_bye: false } }),
  ]);
  assert.deepEqual(g.upcoming.map((e) => e.gsis_id), ["a"]);
  assert.deepEqual(g.longTerm.map((e) => e.gsis_id), ["b"]);
  assert.deepEqual(g.done.map((e) => e.gsis_id), ["c", "d"]);
});

test("roster actions: my injured, my gainers, pickups ordered by the larger of estimate and observed gain", () => {
  const actions = rosterActions([
    entry("mine-out", { roster: roster({ is_mine: true }), impact: { ...entry("x").impact, teammates: [
      mate("my-backup", { roster: roster({ is_mine: true }) }),
      mate("fa-small", { estimate: { share: 0.1, extra_per_game: 1, basis_volume_per_game: 1, basis_weeks: [] }, roster: roster({ status: "FREE_AGENT" }) }),
      mate("fa-observed", { estimate: { share: 0.1, extra_per_game: 1, basis_volume_per_game: 1, basis_weeks: [] },
        observed: { with: null, without: null, delta_per_game: 9 }, roster: roster({ status: "FREE_AGENT" }) }),
      mate("wv-big", { estimate: { share: 0.6, extra_per_game: 7, basis_volume_per_game: 6, basis_weeks: [] }, roster: roster({ status: "WAIVERS" }) }),
      mate("fa-hurt", { healthy: false, roster: roster({ status: "FREE_AGENT" }) }),
    ] } }),
    entry("long", { absence: { kind: "long_term", last_played_week: 2, consecutive_team_games_missed: 4 },
      roster: roster({ is_mine: true }) }),
  ]);
  assert.deepEqual(actions.myInjured.map((a) => a.entry.gsis_id), ["mine-out"]);
  assert.deepEqual(actions.myGainers.map((a) => a.mate?.gsis_id), ["my-backup"]);
  assert.deepEqual(actions.pickups.map((a) => a.mate?.gsis_id), ["fa-observed", "wv-big"]);
});

test("a teammate helped by two absences keeps his strongest reason, not the first one listed", () => {
  const weak = mate("rb2", { estimate: { share: 0.3, extra_per_game: 2.5, basis_volume_per_game: 3, basis_weeks: [] },
    roster: roster({ status: "FREE_AGENT" }) });
  const strong = mate("rb2", { estimate: { share: 0.9, extra_per_game: 12, basis_volume_per_game: 9, basis_weeks: [] },
    roster: roster({ status: "FREE_AGENT" }) });
  const other = mate("wr3", { estimate: { share: 0.5, extra_per_game: 5, basis_volume_per_game: 4, basis_weeks: [] },
    roster: roster({ status: "WAIVERS" }) });
  const actions = rosterActions([
    entry("first-listed", { impact: { ...entry("x").impact, teammates: [weak, other] } }),
    entry("bigger-role", { impact: { ...entry("x").impact, teammates: [strong] } }),
  ]);
  assert.deepEqual(actions.pickups.map((a) => [a.mate?.gsis_id, a.entry.gsis_id]),
    [["rb2", "bigger-role"], ["wr3", "first-listed"]]);
  assert.equal(actions.pickups[0].mate?.estimate?.extra_per_game, 12);
});

test("a QB absence never produces pickups from the pro-rata split", () => {
  const qb = entry("qb", { position: "QB", impact: { ...entry("x").impact, unit: "pass_attempts",
    teammates: [mate("backup", { roster: roster({ status: "FREE_AGENT" }) })] } });
  assert.equal(rosterActions([qb]).pickups.length, 0);
});

test("labels", () => {
  assert.equal(absenceLabel({ kind: "new", last_played_week: 6, consecutive_team_games_missed: 0 }), "Played the team's last game (week 6)");
  assert.equal(absenceLabel({ kind: "ongoing", last_played_week: 4, consecutive_team_games_missed: 1 }), "Missed the last game (last played week 4)");
  assert.equal(absenceLabel({ kind: "ongoing", last_played_week: 3, consecutive_team_games_missed: 2 }), "Missed the last 2 games (last played week 3)");
  assert.equal(gameLabel(entry("a").game), "vs BUF");
  assert.equal(gameLabel({ ...entry("a").game, is_home: false }), "@ BUF");
  assert.equal(gameLabel({ ...entry("a").game, on_bye: true }), "Bye");
  assert.equal(gameLabel({ ...entry("a").game, opponent: null }), null);
  assert.deepEqual(rosterText(roster({ is_mine: true })), { text: "Your team", tone: "info" });
  assert.deepEqual(rosterText(roster({ status: "WAIVERS" })), { text: "Waivers", tone: "warn" });
  assert.equal(waiversHref("RB"), "/waivers?pos=RB");
  assert.equal(waiversHref(null), "/waivers");
  assert.equal(hoursAgo(0.4), "under 1h ago");
  assert.equal(hoursAgo(5.4), "5h ago");
  assert.equal(hoursAgo(72), "3d ago");
});

test("freshness label names the source and never calls missing data fresh", () => {
  assert.deepEqual(freshnessLabel(null), { text: "Not on the slate", detail: null, tone: "" });
  const f = { status: "missing" as const, reason: "", source_used: "none" as const, evidence_retrieved_at: null, hours_since_retrieval: null };
  assert.deepEqual(freshnessLabel(f), { text: "No report", detail: "No source", tone: "bad" });
  assert.deepEqual(freshnessLabel({ ...f, status: "fresh", source_used: "nflverse", hours_since_retrieval: 3 }),
    { text: "Fresh", detail: "NFL report · 3h ago", tone: "good" });
  assert.equal(freshnessLabel({ ...f, status: "stale", source_used: "espn_status", hours_since_retrieval: 30 }).tone, "warn");
});

test("snap series keeps misses as gaps, never zeros", () => {
  assert.deepEqual(snapSeries(entry("a").workload), [0.7, null]);
});
