import assert from "node:assert/strict";
import test from "node:test";
import { compositionSeries, insufficientCount, parseTab, standingOrder, standoutTendencies, winShare } from "./manager-view.ts";

const cls = (dimension: string, label: string, confidence: string) => ({ dimension, label, confidence });

test("parseTab defaults to overview and ignores unknown values", () => {
  assert.equal(parseTab(undefined), "overview");
  assert.equal(parseTab("scouting"), "scouting");
  assert.equal(parseTab(["transactions", "scouting"]), "transactions");
  assert.equal(parseTab("nope"), "overview");
});

test("standoutTendencies keeps HIGH labels, most confident first, then display order", () => {
  const order = ["a", "b", "c", "d", "e"];
  const out = standoutTendencies({
    e: cls("e", "HIGH", "HIGH"),
    d: cls("d", "HIGH", "MEDIUM"),
    c: cls("c", "LOW", "HIGH"),
    b: cls("b", "HIGH", "HIGH"),
    a: cls("a", "INSUFFICIENT", "LOW"),
  }, order);
  assert.deepEqual(out.map((c) => c.dimension), ["b", "e", "d"]);
});

test("standoutTendencies respects the limit and accepts arrays", () => {
  const rows = ["a", "b", "c", "d", "e", "f"].map((d) => cls(d, "HIGH", "HIGH"));
  assert.equal(standoutTendencies(rows, ["a", "b", "c", "d", "e", "f"], 3).length, 3);
  assert.equal(standoutTendencies([cls("x", "MEDIUM", "HIGH")], []).length, 0);
});

test("insufficientCount counts INSUFFICIENT labels", () => {
  assert.equal(insufficientCount({ a: cls("a", "INSUFFICIENT", "LOW"), b: cls("b", "HIGH", "HIGH"), c: cls("c", "INSUFFICIENT", "LOW") }), 2);
  assert.equal(insufficientCount([]), 0);
});

test("compositionSeries lines up positions and periods; an absent row is a measured zero", () => {
  const rows = [
    { scoring_period: 2, position: "RB", n: 6, starters: 3 },
    { scoring_period: 1, position: "RB", n: 5, starters: 3 },
    { scoring_period: 1, position: "QB", n: 2, starters: 1 },
  ];
  const s = compositionSeries(rows, ["QB", "RB", "TE"]);
  assert.deepEqual(s.periods, [1, 2]);
  assert.deepEqual(s.series.map((x) => x.position), ["QB", "RB", "TE"]);
  assert.deepEqual(s.series[0].points, [2, 0]);
  assert.deepEqual(s.series[1].points, [5, 6]);
  assert.deepEqual(s.series[2].points, [0, 0]);
});

test("compositionSeries with no rows has no periods", () => {
  const s = compositionSeries([], ["QB"]);
  assert.deepEqual(s.periods, []);
  assert.deepEqual(s.series[0].points, []);
});

const team = (team_id: number, wins: number, losses: number, points_for: number | null, playoff_seed: number | null, ties = 0) =>
  ({ team_id, wins, losses, ties, points_for, playoff_seed });

test("standingOrder uses playoff_seed when every team has one, not team_id order", () => {
  const out = standingOrder([team(1, 3, 2, 500, 7), team(2, 4, 1, 400, 2), team(3, 5, 0, 600, 1), team(4, 1, 4, 300, 12)]);
  assert.deepEqual(out.map((t) => t.team_id), [3, 2, 1, 4]);
});

test("standingOrder falls back to win share then points for when a seed is missing", () => {
  const out = standingOrder([team(1, 2, 3, 700, null), team(2, 4, 1, 400, 2), team(3, 4, 1, 450, null), team(4, 3, 2, 300, null, 0)]);
  assert.deepEqual(out.map((t) => t.team_id), [3, 2, 4, 1]);
});

test("standingOrder counts a tie as half a win and breaks full ties by team_id; empty stays empty", () => {
  const out = standingOrder([team(5, 1, 1, 100, null, 2), team(4, 2, 2, 100, null), team(2, 3, 1, 90, null)]);
  assert.deepEqual(out.map((t) => t.team_id), [2, 4, 5]);
  assert.deepEqual(standingOrder([]), []);
});

test("winShare counts a tie as half and is missing before any game", () => {
  assert.equal(winShare({ wins: 3, losses: 1, ties: 0 }), 0.75);
  assert.equal(winShare({ wins: 1, losses: 1, ties: 2 }), 0.5);
  assert.equal(winShare({ wins: 0, losses: 0, ties: 0 }), null);
});
