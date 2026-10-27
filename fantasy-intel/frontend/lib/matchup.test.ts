import assert from "node:assert/strict";
import { test } from "node:test";
import {
  availableViews, benchRows, chooseView, hasDesignation, lineupRows, lineupTotal, headline, leader, margin, matchupHref, ordinal, pairSlots, parseWeekParam, share, statusLabel, sumAll, switcherWeeks,
} from "./matchup.ts";
import type { LineupSlot, Matchup, SetPlayer, TeamSide } from "./matchup-types.ts";

const side = (over: Partial<TeamSide> = {}): TeamSide => ({
  team_id: 1, name: "A", abbrev: "A", record: { wins: 1, losses: 0, ties: 0 }, standing: 1,
  actual_points: null, projected_points: null, known_points: null, projection_complete: false,
  lineup: [], bench: [], actual_lineup: null, actual_bench: null, ...over,
});

test("headline: a complete projection is projected; started matchups lead with actual points", () => {
  assert.deepEqual(headline(side({ projected_points: 110.5, known_points: 110.5, projection_complete: true }), "upcoming"),
    { value: 110.5, basis: "projected", label: "Projected", incomplete: false });
  assert.deepEqual(headline(side({ actual_points: 87.2, projected_points: 110.5 }), "in_progress"),
    { value: 87.2, basis: "actual", label: "Actual so far", incomplete: false });
  assert.equal(headline(side({ actual_points: 91, projected_points: 110 }), "final").label, "Final");
});

test("headline: an upcoming week never shows actual points, even when ESPN stored a zero", () => {
  const h = headline(side({ actual_points: 0, projected_points: 100 }), "upcoming");
  assert.equal(h.basis, "projected");
  assert.equal(h.value, 100);
});

test("headline: an incomplete projection is known-only and flagged; nothing at all is null", () => {
  assert.deepEqual(headline(side({ known_points: 88.5 }), "upcoming"),
    { value: 88.5, basis: "known", label: "Known only", incomplete: true });
  assert.deepEqual(headline(side(), "final"), { value: null, basis: null, label: "No projection", incomplete: false });
});

test("a measured zero is a value, not missing", () => {
  assert.equal(headline(side({ actual_points: 0 }), "final").value, 0);
  assert.equal(headline(side({ projected_points: 0, projection_complete: true }), "upcoming").value, 0);
});

test("margin: same complete basis only, rounded", () => {
  const p = (v: number) => headline(side({ projected_points: v, projection_complete: true }), "upcoming");
  assert.equal(margin(p(114.36), p(100.34)).value, 14.02);
  assert.equal(margin(p(100), p(114.5)).value, -14.5);
  assert.equal(margin(p(100), p(100)).value, 0);
  const known = headline(side({ known_points: 90 }), "upcoming");
  assert.equal(margin(p(100), known).value, null);
  assert.match(margin(p(100), known).reason ?? "", /incomplete/);
  const none = headline(side(), "upcoming");
  assert.equal(margin(p(100), none).value, null);
  const actual = headline(side({ actual_points: 90 }), "final");
  assert.equal(margin(p(100), actual).value, null);
});

test("share: my fraction of the combined total; unavailable for known-only, mixed or non-positive totals", () => {
  const p = (v: number) => headline(side({ projected_points: v, projection_complete: true }), "upcoming");
  assert.equal(share(p(75), p(25)), 75);
  assert.equal(share(p(50), p(50)), 50);
  assert.equal(share(p(0), p(0)), null);
  assert.equal(share(p(0), p(40)), 0);
  assert.equal(share(headline(side({ known_points: 10 }), "upcoming"), p(10)), null);
  assert.equal(share(headline(side({ actual_points: 10 }), "final"), p(10)), null);
});

test("pairSlots: aligns by slot in display order and keeps duplicate slots in sequence", () => {
  const L = [{ slot: "K", n: "k1" }, { slot: "RB", n: "r1" }, { slot: "RB", n: "r2" }, { slot: "QB", n: "q1" }, { slot: "FLEX", n: "f1" }];
  const R = [{ slot: "QB", n: "Q" }, { slot: "RB", n: "R" }, { slot: "D/ST", n: "D" }, { slot: "FLEX", n: "F" }];
  const rows = pairSlots(L, R).map((r) => `${r.slot}:${r.left?.n ?? "-"}|${r.right?.n ?? "-"}`);
  assert.deepEqual(rows, ["QB:q1|Q", "RB:r1|R", "RB:r2|-", "FLEX:f1|F", "D/ST:-|D", "K:k1|-"]);
});

test("pairSlots: unknown slots sort after the known ones; empty inputs give no rows", () => {
  assert.deepEqual(pairSlots([{ slot: "XYZ" }, { slot: "QB" }], []).map((r) => r.slot), ["QB", "XYZ"]);
  assert.deepEqual(pairSlots([], []), []);
});

test("sumAll: any missing value means no total", () => {
  assert.equal(sumAll([10.25, 5.5]), 15.75);
  assert.equal(sumAll([10, null]), null);
  assert.equal(sumAll([]), null);
  assert.equal(sumAll([0, 0]), 0);
});

test("leader: only a real difference leads", () => {
  assert.equal(leader(10, 9), "left");
  assert.equal(leader(9, 10), "right");
  assert.equal(leader(9, 9), null);
  assert.equal(leader(null, 9), null);
  assert.equal(leader(0, null), null);
});

test("hasDesignation: active and normal are not designations", () => {
  for (const s of [null, undefined, "", "ACTIVE", "active", "NORMAL"]) assert.equal(hasDesignation(s), false);
  for (const s of ["QUESTIONABLE", "OUT", "INJURY_RESERVE", "DOUBTFUL"]) assert.equal(hasDesignation(s), true);
});

test("parseWeekParam: absent, valid or invalid", () => {
  assert.deepEqual(parseWeekParam(undefined), { kind: "none" });
  assert.deepEqual(parseWeekParam(""), { kind: "none" });
  assert.deepEqual(parseWeekParam("3"), { kind: "ok", week: 3 });
  assert.deepEqual(parseWeekParam(["7", "9"]), { kind: "ok", week: 7 });
  for (const bad of ["abc", "0", "-1", "3.5", "1e3", " 3", "0003", "9999"]) {
    assert.equal(parseWeekParam(bad).kind, "invalid", bad);
  }
});

test("switcherWeeks includes the viewed week once, sorted", () => {
  assert.deepEqual(switcherWeeks([1, 2, 3], 15), [1, 2, 3, 15]);
  assert.deepEqual(switcherWeeks([3, 1, 2], 2), [1, 2, 3]);
});

test("matchupHref keeps only set parameters", () => {
  assert.equal(matchupHref({}), "/matchup");
  assert.equal(matchupHref({ week: 4 }), "/matchup?week=4");
  assert.equal(matchupHref({ week: 4, team: "3", lineup: "set" }), "/matchup?week=4&team=3&lineup=set");
});

test("ordinal handles the teens and the usual suffixes", () => {
  assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111].map(ordinal),
    ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "101st", "111th"]);
});

// ------------------------------------------------------------------------------ views --

const slot = (over: Partial<LineupSlot> = {}): LineupSlot => ({
  slot: "QB", player_id: 1, name: "Q", position: "QB", pro_team: "KC", points: 20, is_flex: false, injury_status: null, on_bye: false, ...over,
});
const setP = (over: Partial<SetPlayer> = {}): SetPlayer => ({
  slot: "QB", player_id: 1, name: "Q", position: "QB", pro_team: "KC", projected_points: 19, points: 25, injury_status: null, on_bye: false, ...over,
});
const m = (me: Partial<TeamSide>, opponent: Partial<TeamSide> | null = {}, status: Matchup["status"] = "upcoming") =>
  ({ me: side(me), opponent: opponent && side(opponent), status });

test("availableViews and chooseView: projected before play, set after, whichever exists otherwise", () => {
  const both = m({ lineup: [slot()], actual_lineup: [setP()] });
  assert.deepEqual(availableViews(both), ["projected", "set"]);
  assert.equal(chooseView(undefined, both), "projected");
  assert.equal(chooseView("set", both), "set");
  assert.equal(chooseView("nonsense", both), "projected");
  assert.equal(chooseView(undefined, { ...both, status: "final" }), "set");
  assert.equal(chooseView(undefined, { ...both, status: "in_progress" }), "set");
  const past = m({ actual_lineup: [setP()] }, { actual_lineup: [setP()] }, "final");
  assert.deepEqual(availableViews(past), ["set"]);
  assert.equal(chooseView("projected", past), "set");
  const none = m({}, {});
  assert.deepEqual(availableViews(none), []);
  assert.equal(chooseView(undefined, none), null);
  assert.deepEqual(availableViews(m({ lineup: [slot()] }, null)), ["projected"]);
});

test("lineupRows: projected uses ESPN projections; set uses actual points only once started", () => {
  const s = side({ lineup: [slot({ points: 18.5 }), slot({ slot: "K", player_id: null, name: null, points: null })], actual_lineup: [setP()] });
  const proj = lineupRows(s, "projected", "upcoming")!;
  assert.deepEqual(proj.map((r) => [r.slot, r.id, r.value]), [["QB", 1, 18.5], ["K", null, null]]);
  assert.equal(lineupRows(s, "set", "upcoming")![0].value, 19);
  assert.equal(lineupRows(s, "set", "in_progress")![0].value, 25);
  assert.equal(lineupRows(s, "set", "final")![0].value, 25);
  assert.equal(lineupRows(side(), "projected", "upcoming"), null);
  assert.equal(lineupRows(side(), "set", "final"), null);
});

test("benchRows: the projected bench keeps unknown projections null; the set bench keeps IR", () => {
  const s = side({
    lineup: [slot()], bench: [{ player_id: 5, name: "B", position: "WR", pro_team: "DET", points: null, injury_status: null, on_bye: null }],
    actual_lineup: [setP()], actual_bench: [setP({ slot: "IR", player_id: 9 })],
  });
  assert.deepEqual(benchRows(s, "projected", "upcoming")!.map((r) => [r.id, r.value]), [[5, null]]);
  assert.deepEqual(benchRows(s, "set", "upcoming")!.map((r) => r.slot), ["IR"]);
  assert.equal(benchRows(side(), "projected", "upcoming"), null);
});

test("lineupTotal: backend total for projected (known-only when incomplete); a set total needs every number", () => {
  const complete = side({ lineup: [slot()], projected_points: 100, known_points: 100, projection_complete: true });
  assert.deepEqual(lineupTotal(complete, "projected", lineupRows(complete, "projected", "upcoming")), { value: 100, incomplete: false });
  const partial = side({ lineup: [slot()], known_points: 80 });
  assert.deepEqual(lineupTotal(partial, "projected", lineupRows(partial, "projected", "upcoming")), { value: 80, incomplete: true });
  const set = side({ actual_lineup: [setP({ points: 10.5 }), setP({ player_id: 2, points: 4.25 })] });
  assert.deepEqual(lineupTotal(set, "set", lineupRows(set, "set", "final")), { value: 14.75, incomplete: false });
  const unknown = side({ actual_lineup: [setP({ projected_points: null }), setP({ player_id: 2 })] });
  assert.deepEqual(lineupTotal(unknown, "set", lineupRows(unknown, "set", "upcoming")), { value: null, incomplete: true });
  assert.deepEqual(lineupTotal(side(), "set", null), { value: null, incomplete: false });
});

test("matchup status labels do not imply that stored scores are live", () => {
  assert.equal(statusLabel("in_progress"), "In progress at last sync");
  assert.equal(statusLabel("final"), "Final");
  assert.equal(statusLabel("upcoming"), "Not started");
  assert.equal(statusLabel("unknown"), "Status unknown");
  assert.doesNotMatch(statusLabel("in_progress"), /live|real.time|now/i);
});
