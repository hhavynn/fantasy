import assert from "node:assert/strict";
import test from "node:test";
import type { PrRootingGame } from "./types.ts";
import {
  backendQuery, cutlineIndex, matchupAnalysisHref, parseRaceQuery, raceHref, rootingReason, rootPlace, unresolvedText, weeksText, whatifChanged,
  withCutline, withOverride,
} from "./playoff-view.ts";

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

// ------------------------------------------------------------------ what-if (issue #192) --

test("whatifChanged compares status, rank and every record field", () => {
  const rec = { wins: 3, losses: 2, ties: 0, unresolved: 1 };
  const same = { rank: 2, status: "inside" as const, record: rec };
  assert.equal(whatifChanged({ baseline: same, whatif: { ...same, record: { ...rec } } }), false);
  assert.equal(whatifChanged({ baseline: same, whatif: { ...same, status: "cutline_tie" } }), true);
  assert.equal(whatifChanged({ baseline: same, whatif: { ...same, rank: 3 } }), true);
  assert.equal(whatifChanged({ baseline: same, whatif: { ...same, record: { ...rec, unresolved: 0 } } }), true);
  assert.equal(whatifChanged({ baseline: same, whatif: { ...same, record: { ...rec, wins: 4, losses: 1 } } }), true);
  assert.equal(whatifChanged({ baseline: { ...same, record: null }, whatif: { ...same, record: null } }), false);
});

test("unresolvedText names each reason with its count, largest first", () => {
  assert.equal(unresolvedText({ in_progress: 1, projection_incomplete: 2 }), "2 projection incomplete, 1 under way");
  assert.equal(unresolvedText({ set_unresolved: 1, brand_new: 1, equal_projection: 0 }), "1 brand new, 1 set unresolved by you");
  assert.equal(unresolvedText({}), "");
  assert.equal(unresolvedText({ no_period: 1 }), "1 no matchup period (cannot be placed or set)");
});

test("matchupAnalysisHref opens the game's first week from the spotlight side when it plays, else home", () => {
  const m = { weeks: [7, 8], home_team_id: 3, away_team_id: 5 };
  assert.equal(matchupAnalysisHref(m, 5), "/matchup?week=7&team=5");
  assert.equal(matchupAnalysisHref(m, 9), "/matchup?week=7&team=3");
  assert.equal(matchupAnalysisHref(m, null), "/matchup?week=7&team=3");
  assert.equal(matchupAnalysisHref({ ...m, weeks: null }, 3), "/matchup?team=3");
});

// -------------------------------------------------------------- rooting guide (issue #217) --

function game(over: Partial<PrRootingGame> = {}): PrRootingGame {
  const place = (rank: number, status: "inside" | "outside" | "cutline_tie" | null, tied = false) =>
    ({ scenario: { rank, tied, status, record: { wins: 5, losses: 4, ties: 0 }, places: [rank, rank] as [number, number] }, range: { teams_able_to_match_worst: 5, teams_certain_ahead_of_best: 0, record_order: "open" as const } });
  return {
    matchup_id: 9, matchup_period: 10, weeks: [10], home_team_id: 3, away_team_id: 4, status: "upcoming", close: false,
    scenario_result: "home", if_home: place(5, "outside"), if_away: place(4, "inside"), prefer: "away", basis: ["scenario"],
    conflict: false, swing: { status_changes: true, rank_difference: 1, range_changes: false }, ...over,
  };
}
const nm = (id: number) => `Team ${id}`;

test("rootingReason states the backend's comparison for the preferred side, scenario first", () => {
  assert.equal(rootingReason(game(), nm),
    "A Team 4 win leaves you #4, inside the line; a Team 3 win, #5, outside the line, every other result as in this scenario.");
  const tied = game({ if_away: { ...game().if_away, scenario: { rank: 4, tied: true, status: "cutline_tie", record: null, places: [4, 6] } } });
  assert.match(rootingReason(tied, nm), /#4–6 \(tied\), tied at the line/);
});

test("rootingReason words the possible range as record order only, never a clinch", () => {
  const g = game({
    prefer: "home", basis: ["possible_range"],
    if_home: { ...game().if_home, range: { teams_able_to_match_worst: 2, teams_certain_ahead_of_best: 0, record_order: "above_line" } },
  });
  const text = rootingReason(g, nm);
  assert.match(text, /after a Team 3 win, fewer teams than there are spots can match your record/);
  assert.match(text, /not a clinch or elimination/);
  assert.doesNotMatch(text, /clinched|eliminated|probab|%/i);
});

test("rootingReason for no preference and for a conflict names both results", () => {
  const same = game({ prefer: null, basis: [], if_away: game().if_home });
  assert.equal(rootingReason(same, nm), "Neither result is better for you: Team 3 win, #5, outside the line; Team 4 win, #5, outside the line.");
  const below = { teams_able_to_match_worst: 7, teams_certain_ahead_of_best: 2, record_order: "below_line" as const };
  const mixed = rootingReason(game({ prefer: null, basis: [], conflict: true, if_home: { ...game().if_home, range: below } }), nm);
  assert.match(mixed, /^Mixed: /);
  assert.match(mixed, /Team 3 win: #5, outside the line; Team 4 win: #4, inside the line\./);
  assert.match(mixed, /after a Team 3 win, at least as many teams as there are spots finish ahead.*after a Team 4 win, these bounds do not settle/);
  const blank = { scenario: { rank: null, tied: false, status: null, record: null, places: null }, range: null };
  assert.equal(rootingReason(game({ prefer: null, basis: [], if_home: blank, if_away: blank }), nm),
    "Neither result is better for you: Team 3 win, unranked; Team 4 win, unranked.");
});

test("rootPlace shows a shared rank as the span of places the tie covers", () => {
  assert.equal(rootPlace({ rank: 3, tied: true, places: [3, 7] }), "#3–7 (tied)");
  assert.equal(rootPlace({ rank: 4, tied: false, places: [4, 4] }), "#4");
  assert.equal(rootPlace({ rank: 2, tied: true, places: null }), "#2 (tied)");
  assert.equal(rootPlace({ rank: null, tied: false, places: null }), "unranked");
});
