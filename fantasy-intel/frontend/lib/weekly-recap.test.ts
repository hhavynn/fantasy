import assert from "node:assert/strict";
import test from "node:test";
import {
  backendPath, joinNames, parseRecapQuery, pointsText, recapHref, recordText, scoreBars, sideResult, tradeLines, weekLabel,
  type WrMatchup,
} from "./weekly-recap.ts";

const team = (team_id: number, points: number | null) => ({ team_id, name: `Team ${team_id}`, abbrev: null, points });
const game = (over: Partial<WrMatchup> = {}): WrMatchup => ({
  matchup_id: 1, matchup_period: 1, playoff_tier: null, home_team: team(1, 120), away_team: team(2, 100),
  winner: "HOME", is_tie: false, margin: 20, ...over,
});

test("parseRecapQuery keeps valid values and falls back on anything else", () => {
  assert.deepEqual(parseRecapQuery({}), { week: null, players: "top", moves: "adds" });
  assert.deepEqual(parseRecapQuery({ week: "4", players: "bench", moves: "trades" }), { week: 4, players: "bench", moves: "trades" });
  assert.deepEqual(parseRecapQuery({ week: ["3", "9"], players: "nope", moves: "" }), { week: 3, players: "top", moves: "adds" });
  for (const bad of ["0", "-1", "2.5", "abc"]) assert.equal(parseRecapQuery({ week: bad }).week, null);
});

test("backendPath sends only the week", () => {
  assert.equal(backendPath({ week: null, players: "bench", moves: "drops" }), "/api/weekly-recap");
  assert.equal(backendPath({ week: 5, players: "top", moves: "adds" }), "/api/weekly-recap?week=5");
});

test("recapHref drops default tabs and applies the patch", () => {
  const q = { week: 4, players: "top", moves: "adds" } as const;
  assert.equal(recapHref(q), "/weekly-recap?week=4");
  assert.equal(recapHref(q, { moves: "trades" }), "/weekly-recap?week=4&moves=trades");
  assert.equal(recapHref({ ...q, players: "bench" }, { week: 2 }), "/weekly-recap?week=2&players=bench");
  assert.equal(recapHref({ week: null, players: "top", moves: "adds" }), "/weekly-recap");
});

test("weekLabel covers one week, a multi-week period and unknown weeks", () => {
  assert.equal(weekLabel([5], 5), "Week 5");
  assert.equal(weekLabel([15, 16], 15), "Weeks 15–16");
  assert.equal(weekLabel([], 7), "Period 7");
  assert.equal(weekLabel(null, 7), "Period 7");
});

test("recordText shows ties only when there are some", () => {
  assert.equal(recordText({ wins: 4, losses: 1, ties: 0 }), "4-1");
  assert.equal(recordText({ wins: 3, losses: 1, ties: 1 }), "3-1-1");
});

test("joinNames reads naturally", () => {
  assert.equal(joinNames([]), "");
  assert.equal(joinNames(["A"]), "A");
  assert.equal(joinNames(["A", "B"]), "A and B");
  assert.equal(joinNames(["A", "B", "C"]), "A, B and C");
});

test("pointsText keeps a measured zero and marks missing", () => {
  assert.equal(pointsText(0), "0.00");
  assert.equal(pointsText(131.456), "131.46");
  assert.equal(pointsText(null), "—");
});

test("sideResult gives W/L for each side and T for a tie", () => {
  assert.equal(sideResult(game(), "home"), "W");
  assert.equal(sideResult(game(), "away"), "L");
  assert.equal(sideResult(game({ winner: "AWAY" }), "away"), "W");
  assert.equal(sideResult(game({ winner: "TIE", is_tie: true }), "home"), "T");
});

test("tradeLines lists what each side received", () => {
  const t = {
    trade_id: "x", processed_at: null, scoring_period: 4, placed_by: "transaction" as const,
    sides: [
      { team_id: 1, team_name: "Alpha", received: [{ player_id: 1, player_name: "Ann", position: "QB" }, { player_id: 2, player_name: "Bo", position: "RB" }] },
      { team_id: null, team_name: null, received: [{ player_id: 3, player_name: "Cy", position: "WR" }] },
    ],
  };
  assert.deepEqual(tradeLines(t), ["Alpha gets Ann, Bo", "Unknown team gets Cy"]);
});

test("scoreBars sorts high to low, keeps missing scores null and highlights winners", () => {
  const bars = scoreBars([game(), game({ matchup_id: 2, home_team: team(3, null), away_team: team(4, 150) })], new Set([4]));
  assert.deepEqual(bars.map((b) => [b.label, b.value, b.tone]), [
    ["Team 4", 150, "default"], ["Team 1", 120, "muted"], ["Team 2", 100, "muted"], ["Team 3", null, "muted"],
  ]);
});

test("the command palette finds the recap page by name", async () => {
  const { NAV_GROUPS } = await import("./navigation.ts");
  const { pageItems, rankItems } = await import("./palette.ts");
  assert.equal(rankItems("recap", pageItems(NAV_GROUPS))[0]?.href, "/weekly-recap");
});
