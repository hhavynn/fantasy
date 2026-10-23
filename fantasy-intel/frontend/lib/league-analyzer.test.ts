import assert from "node:assert/strict";
import { test } from "node:test";
import {
  apiPath, errorState, knownView, leadsText, ordinal, rankText, readView, signedText, sortForPosition, targetHref, tradesWithHref, unknownLabel,
  viewHref, visibleGroups, type LaCell, type LaGroup, type LaTeam,
} from "./league-analyzer.ts";
import { plural } from "./plural.ts";

const cell = (rank: number | null, status: LaCell["status"] = rank === null ? "unknown" : "ranked"): LaCell => ({
  status, points: null, per_slot: null, above_replacement: null, vs_median: null, score: null, rank, of: 3, reason: null,
});
const team = (team_id: number, rb: LaCell): LaTeam => ({
  team_id, name: `Team ${team_id}`, abbrev: null, is_mine: false, record: { wins: 1, losses: 1, ties: 0 },
  power: { rank: team_id, score: null, lineup_strength: null, unranked_reason: null },
  lineup: { projected_per_week: null, known_per_week: null, complete: false },
  positions: { RB: rb }, strongest: [], weakest: [], caveats: [],
});

test("the view reads only known values from the URL", () => {
  assert.deepEqual(readView({}), { vs: null, horizon: "ros", pos: null, kdst: true });
  assert.deepEqual(readView({ vs: "4", horizon: "week", pos: "D/ST", kdst: "hold" }), { vs: 4, horizon: "week", pos: "D/ST", kdst: false });
  assert.deepEqual(readView({ vs: "x", horizon: "season", pos: "<b>", kdst: "1" }), { vs: null, horizon: "ros", pos: null, kdst: true });
});

test("backend and page URLs leave defaults out and keep the compare team distinct", () => {
  const v = readView({ vs: "4", horizon: "week", kdst: "hold" });
  assert.equal(apiPath(null, readView({})), "/api/league-analyzer");
  assert.equal(apiPath(2, v), "/api/league-analyzer?team_id=2&vs=4&horizon=week&kdst_streaming=false");
  assert.equal(viewHref(null, readView({})), "/league-analyzer");
  assert.equal(viewHref(2, v, { pos: "RB" }), "/league-analyzer?team=2&vs=4&horizon=week&pos=RB&kdst=hold");
  assert.equal(viewHref(4, v), "/league-analyzer?team=4&horizon=week&kdst=hold", "a team is never compared with itself");
  assert.equal(viewHref(2, v, { vs: null, horizon: "ros", kdst: true }), "/league-analyzer?team=2");
});

test("links go to trade discovery, aimed at a position only where its targeted lens can", () => {
  assert.equal(tradesWithHref(2, 5), "/trades/discover?team=2&opponent=5");
  assert.equal(targetHref(2, "TE"), "/trades/discover?team=2&mode=targeted&pos=TE");
  assert.equal(targetHref(2, "FLEX"), null);
  assert.equal(targetHref(2, "D/ST"), null);
});

test("unknown cells sort after every ranked team and are never called last", () => {
  const teams = [team(1, cell(null)), team(2, cell(2)), team(3, cell(1))];
  assert.deepEqual(sortForPosition(teams, "RB").map((t) => t.team_id), [3, 2, 1]);
  assert.deepEqual(sortForPosition(teams, null).map((t) => t.team_id), [1, 2, 3]);
  assert.equal(rankText(cell(null)), null);
  assert.equal(rankText(cell(2)), "2nd of 3");
  assert.equal(unknownLabel(cell(null)), "Unknown");
  assert.equal(unknownLabel(cell(null, "unverified")), "Unverified");
});

test("number formatting is signed and never invents a value", () => {
  assert.equal(signedText(2.44), "+2.4");
  assert.equal(signedText(-0.81), "-0.8");
  assert.equal(signedText(-0.01), "0.0");
  assert.equal(signedText(null), null);
  assert.equal(ordinal(11), "11th");
  assert.equal(ordinal(22), "22nd");
});

test("a position filter that names no group of this league shows every group", () => {
  const g = (slot: string): LaGroup => ({ slot, count: 1, eligible_positions: [slot], streamable: false, teams_ranked: 0,
    replacement_per_slot: null, replacement_note: null, league_median_per_slot: null, league_best_per_slot: null, unknown_teams: [] });
  const groups = [g("QB"), g("RB")];
  assert.deepEqual(visibleGroups(groups, "RB").map((x) => x.slot), ["RB"]);
  assert.deepEqual(visibleGroups(groups, "OP").map((x) => x.slot), ["QB", "RB"]);
});

test("the compare summary counts leads in words", () => {
  const side = (name: string) => ({ name }) as never;
  assert.equal(leadsText({ a: side("A"), b: side("B"), leads: { a: 3, b: 1, even: 1 } }, plural),
    "A leads at 3 positions, B at 1, even at 1");
  assert.equal(leadsText({ a: side("A"), b: side("B"), leads: { a: 1, b: 4, even: 0 } }, plural), "A leads at 1 position, B at 4");
});

test("a position filter this league does not have is cleared, so the title, chips and links agree", () => {
  const groups = [{ slot: "QB" }, { slot: "RB" }, { slot: "FLEX" }];
  const stale = readView({ pos: "OP", vs: "3" });
  assert.equal(stale.pos, "OP", "syntactically valid, so readView keeps it");
  assert.deepEqual(knownView(stale, groups), { vs: 3, horizon: "ros", pos: null, kdst: true });
  assert.equal(viewHref(2, knownView(stale, groups)), "/league-analyzer?team=2&vs=3");
  const rb = readView({ pos: "RB" });
  assert.equal(knownView(rb, groups), rb);
});

test("every failure state comes from the route's error code, never from its wording", () => {
  const view = readView({ vs: "99" });
  const cases: [{ status?: number; code?: string; kind?: string }, number | null, ReturnType<typeof errorState>][] = [
    [{ status: 404, code: "unknown_team" }, 99, { kind: "unknown_team", team: 99 }],
    // a valid focus and an unknown compared team: the error names the compared team and the focus is kept
    [{ status: 404, code: "unknown_vs" }, 2, { kind: "unknown_vs", vs: 99 }],
    [{ status: 404, code: "unknown_teams" }, 98, { kind: "unknown_teams", team: 98, vs: 99 }],
    [{ status: 422, code: "same_team" }, 2, { kind: "same_team" }],
    [{ status: 404, code: "no_own_team" }, null, { kind: "no_own_team" }],
    [{ status: 503, code: "not_synced" }, null, { kind: "not_synced" }],
    // no code: the league's own 404 (private or unknown league), first sync, offline, an older backend
    [{ status: 404 }, 2, { kind: "backend" }],
    [{ status: 409 }, null, { kind: "backend" }],
    [{ kind: "offline" }, null, { kind: "backend" }],
    [{ status: 404, code: "something_new" }, 2, { kind: "backend" }],
  ];
  for (const [res, team, want] of cases) assert.deepEqual(errorState(res, team, view), want, JSON.stringify(res));
});
