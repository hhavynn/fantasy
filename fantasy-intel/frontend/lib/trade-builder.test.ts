// Run: npm test (node --test with type stripping; no extra dependencies).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  canEvaluate, changeTeam, clearPicks, filterRoster, groupByPosition, initialSelection, positionsIn, problems,
  rosterPreview, selectionKey, setStartWeek, shapeLabel, sideLimit, toQuery, togglePlayer, type BuilderTeam,
} from "./trade-builder.ts";

const P = (id: number, position = "WR", on_ir = false) => ({ espn_player_id: id, position, on_ir });
const TEAMS: BuilderTeam[] = [
  { team_id: 1, roster: [P(101, "QB"), P(102, "RB"), P(103, "WR"), P(104, "TE"), P(105, "RB", true)] },
  { team_id: 2, roster: [P(201, "QB"), P(202, "RB"), P(203, "WR"), P(204, "WR")] },
  { team_id: 3, roster: [P(301, "RB"), P(302, "WR")] },
];

test("deep link loads its selection and drops players not on the chosen rosters", () => {
  const s = initialSelection(TEAMS, { ta: 1, tb: 2, a: [102, 103, 999], b: [203, 101], sw: 9 });
  assert.deepEqual(s, { ta: 1, tb: 2, a: [102, 103], b: [203], sw: 9 });
  assert.equal(shapeLabel(s), "2-for-1");
});

test("defaults: my team as A, the first other team as B, nothing selected", () => {
  assert.deepEqual(initialSelection(TEAMS, {}, 2), { ta: 2, tb: 1, a: [], b: [], sw: null });
  assert.deepEqual(initialSelection(TEAMS, { ta: 42 }, null), { ta: 1, tb: 2, a: [], b: [], sw: null });
});

test("changing Team A swaps its roster and clears its old picks; Team B's picks persist", () => {
  let s = initialSelection(TEAMS, { ta: 1, tb: 2, a: [102, 103], b: [203, 204] });
  s = changeTeam(s, TEAMS, "a", 3);
  assert.equal(s.ta, 3);
  assert.deepEqual(s.a, []);
  assert.deepEqual(s.b, [203, 204]);
});

test("changing Team B is symmetric", () => {
  let s = initialSelection(TEAMS, { ta: 1, tb: 2, a: [102], b: [203] });
  s = changeTeam(s, TEAMS, "b", 3);
  assert.deepEqual([s.tb, s.a, s.b], [3, [102], []]);
});

test("the same team on both sides is a clear validation problem", () => {
  let s = initialSelection(TEAMS, { ta: 1, tb: 2, a: [102], b: [203] });
  s = changeTeam(s, TEAMS, "b", 1);
  assert.ok(problems(s, 6).some((p) => p.code === "same_team"));
  assert.equal(canEvaluate(s, 6), false);
});

test("shape updates as players are toggled, and the per-side limit is enforced", () => {
  let s = initialSelection(TEAMS, { ta: 1, tb: 2 });
  for (const id of [101, 102, 103, 104]) s = togglePlayer(s, "a", id);
  s = togglePlayer(s, "b", 202);
  s = togglePlayer(s, "b", 203);
  assert.equal(shapeLabel(s), "4-for-2");
  assert.equal(canEvaluate(s, 6), true);
  assert.equal(canEvaluate(s, 3), false);
  s = togglePlayer(s, "a", 104);
  assert.equal(shapeLabel(s), "3-for-2");
});

test("the selection key changes on every edit, so an old result is never shown as current", () => {
  const s = initialSelection(TEAMS, { ta: 1, tb: 2, a: [102], b: [203] });
  const k = selectionKey(s);
  assert.notEqual(selectionKey(togglePlayer(s, "a", 103)), k);
  assert.notEqual(selectionKey(setStartWeek(s, 10)), k);
  assert.notEqual(selectionKey(changeTeam(s, TEAMS, "b", 3)), k);
  assert.equal(selectionKey({ ...s, a: [...s.a] }), k);
  assert.equal(selectionKey(togglePlayer(togglePlayer(s, "a", 103), "a", 103)), k);
});

test("query string round-trips the selection (deep links)", () => {
  const s = initialSelection(TEAMS, { ta: 1, tb: 2, a: [102, 103], b: [203], sw: 11 });
  assert.equal(toQuery(s), "ta=1&tb=2&a=102&a=103&b=203&sw=11");
});

test("roster preview flags capacity and position limits before drops", () => {
  const own = TEAMS[1].roster;
  const gets = [P(102, "RB"), P(103, "WR"), P(104, "TE")];
  const pv = rosterPreview(own, [203], gets, 4, { WR: 2 });
  assert.equal(pv.activeAfter, 6);
  assert.equal(pv.overCapacityBy, 2);
  assert.deepEqual(pv.positionsOver, []);         // WR 203 out, WR 103 in: still 2 WR
  const pv2 = rosterPreview(own, [202], [P(105, "WR"), P(106, "WR")], 10, { WR: 2 });
  assert.deepEqual(pv2.positionsOver, [{ position: "WR", count: 4, limit: 2 }]);
});

test("a full side refuses another player but still lets one be removed", () => {
  let s = initialSelection(TEAMS, { ta: 1, tb: 2, a: [101, 102] });
  const full = togglePlayer(s, "a", 103, 2);
  assert.equal(full, s);                              // unchanged, same object
  assert.deepEqual(full.a, [101, 102]);
  s = togglePlayer(s, "a", 102, 2);                   // removing works at the limit
  assert.deepEqual(s.a, [101]);
  s = togglePlayer(s, "a", 103, 2);                   // and frees a slot
  assert.deepEqual(s.a, [101, 103]);
  assert.deepEqual(togglePlayer(initialSelection(TEAMS, { ta: 1, tb: 2 }), "a", 101).a, [101]);   // no max: unchanged behaviour
});

test("clearPicks empties both sides and keeps teams and start week", () => {
  const s = initialSelection(TEAMS, { ta: 1, tb: 2, a: [102], b: [203], sw: 9 });
  assert.deepEqual(clearPicks(s), { ta: 1, tb: 2, a: [], b: [], sw: 9 });
});

test("sideLimit reports remaining room and the limit state", () => {
  assert.deepEqual(sideLimit(2, 6), { count: 2, max: 6, remaining: 4, atLimit: false, over: false });
  assert.deepEqual(sideLimit(6, 6), { count: 6, max: 6, remaining: 0, atLimit: true, over: false });
  assert.deepEqual(sideLimit(7, 6), { count: 7, max: 6, remaining: 0, atLimit: true, over: true });
});

test("roster groups follow the picker position order and keep player order inside a group", () => {
  const ps = [P(1, "WR"), P(2, "K"), P(3, "QB"), P(4, "WR"), P(5, "D/ST"), P(6, "RB"), { ...P(7), position: null }, P(8, "TE"), P(9, "LB")];
  const groups = groupByPosition(ps);
  assert.deepEqual(groups.map((g) => g.position), ["QB", "RB", "WR", "TE", "K", "D/ST", "LB", "?"]);
  assert.deepEqual(groups.find((g) => g.position === "WR")!.players.map((p) => p.espn_player_id), [1, 4]);
  assert.deepEqual(positionsIn(ps), ["QB", "RB", "WR", "TE", "K", "D/ST", "LB", "?"]);
  assert.deepEqual(groupByPosition([]), []);
});

test("roster filter matches name or NFL team and a position, ignoring case and padding", () => {
  const ps = [
    { name: "Jalen Hurts", position: "QB", pro_team: "PHI" },
    { name: "A.J. Brown", position: "WR", pro_team: "PHI" },
    { name: "Bijan Robinson", position: "RB", pro_team: "ATL" },
    { name: null, position: null, pro_team: null },
  ];
  assert.equal(filterRoster(ps, "", "").length, 4);
  assert.deepEqual(filterRoster(ps, "  HURTS ", "").map((p) => p.name), ["Jalen Hurts"]);
  assert.deepEqual(filterRoster(ps, "phi", "").map((p) => p.name), ["Jalen Hurts", "A.J. Brown"]);
  assert.deepEqual(filterRoster(ps, "phi", "WR").map((p) => p.name), ["A.J. Brown"]);
  assert.deepEqual(filterRoster(ps, "", "?").length, 1);
  assert.deepEqual(filterRoster(ps, "zzz", ""), []);
});
