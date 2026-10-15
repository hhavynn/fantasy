import assert from "node:assert/strict";
import test from "node:test";
import { backendQuery, cutlineIndex, parseRaceQuery, raceHref, weeksText, withCutline, withOverride } from "./playoff-view.ts";

test("parseRaceQuery reads team, base and overrides; ignores malformed entries", () => {
  const q = parseRaceQuery({ team: "6", base: "actual", o: ["9:home", "bad", "10:tie", ":x"] });
  assert.equal(q.team, "6");
  assert.equal(q.base, "actual");
  assert.deepEqual(q.o, { "9": "home", "10": "tie" });
});

test("parseRaceQuery defaults to the projected baseline and accepts a single override", () => {
  const q = parseRaceQuery({ o: "5:away", base: "weird" });
  assert.equal(q.base, "projected");
  assert.deepEqual(q.o, { "5": "away" });
  assert.equal(q.team, undefined);
});

test("raceHref omits defaults and sorts overrides by matchup id", () => {
  assert.equal(raceHref({ base: "projected", o: {} }), "/playoff-race");
  assert.equal(raceHref({ team: "3", base: "actual", o: { "20": "home", "9": "tie" } }), "/playoff-race?team=3&base=actual&o=9%3Atie&o=20%3Ahome");
});

test("withOverride sets, replaces and removes one override, keeping the rest", () => {
  const q = { team: "6", base: "projected" as const, o: { "9": "home" } };
  assert.equal(withOverride(q, 10, "away"), "/playoff-race?team=6&o=9%3Ahome&o=10%3Aaway");
  assert.equal(withOverride(q, 9, "tie"), "/playoff-race?team=6&o=9%3Atie");
  assert.equal(withOverride(q, 9, null), "/playoff-race?team=6");
  assert.deepEqual(q.o, { "9": "home" }, "the query is not mutated");
});

test("backendQuery maps the scenario to the API's parameter names", () => {
  assert.equal(backendQuery({ team: "6", base: "projected", o: { "9": "home" } }), "team_id=6&base=projected&override=9%3Ahome");
  assert.equal(backendQuery({ base: "actual", o: {} }), "base=actual");
});

test("cutlineIndex counts inside rows; null without spots or any decided status", () => {
  const rows = [{ status: "inside" as const }, { status: "inside" as const }, { status: "cutline_tie" as const }, { status: "outside" as const }];
  assert.equal(cutlineIndex(rows, 2), 2);
  assert.equal(cutlineIndex(rows, null), null);
  assert.equal(cutlineIndex([{ status: null }, { status: null }], 8), null);
  assert.equal(cutlineIndex([{ status: "outside" as const }], 8), 0);
});

test("withCutline inserts one marker before the row at the index", () => {
  const items = withCutline(["a", "b", "c"], 1);
  assert.deepEqual(items.map((i) => (i.kind === "cutline" ? "|" : i.row)), ["a", "|", "b", "c"]);
  assert.equal(items.filter((i) => i.kind === "cutline").length, 1);
});

test("withCutline: at the top, past the end and null", () => {
  assert.deepEqual(withCutline(["a", "b"], 0).map((i) => i.kind), ["cutline", "row", "row"]);
  assert.deepEqual(withCutline(["a", "b"], 2).map((i) => i.kind), ["row", "row"]);
  assert.deepEqual(withCutline(["a", "b"], null).map((i) => i.kind), ["row", "row"]);
});

test("weeksText handles single, multi-week and missing weeks", () => {
  assert.equal(weeksText([7], 7), "wk 7");
  assert.equal(weeksText([14, 15], 14), "wk 14–15");
  assert.equal(weeksText(null, 3), "period 3");
});
