import assert from "node:assert/strict";
import test from "node:test";
import {
  slotStatus, summaryText, waiversPath, weekHeadline, weekLinks, weekReasons, type PlannerPlayer, type PlannerWeek,
} from "./bye-planner.ts";

const LG = "lg_abcdefghijklmnop";

const week = (over: Partial<PlannerWeek> = {}): PlannerWeek => ({
  week: 10, phase: "regular", matchup_period: 10, status: "covered", status_text: "Covered", conflict: false,
  required: 9, covered: 9, unknown: 0, uncovered: 0, unresolved: 0, slots: [], short_slots: [], unconfirmed_slots: [],
  byes: [], bye_cluster: false, starters_on_bye: [], out: [], uncertain: [], bench_alternatives: [], ...over,
});

const player = (id: number, over: Partial<PlannerPlayer> = {}): PlannerPlayer => ({
  espn_player_id: id, name: `P${id}`, position: "QB", pro_team: "BUF", injury_status: null, slot: "starter", counts: true,
  eligible_slots: ["QB"], bye_week: 10, availability: null, weeks: {}, ...over,
});

test("slot status: uncovered beats unknown; unknown is never covered", () => {
  assert.equal(slotStatus({ uncovered: 1, unknown: 1 }), "short");
  assert.equal(slotStatus({ uncovered: 0, unknown: 1 }), "unconfirmed");
  assert.equal(slotStatus({ uncovered: 0, unknown: 0 }), "covered");
});

test("week links keep the exact league, team and week", () => {
  const links = weekLinks(week({ short_slots: ["QB", "K"] }), 3, LG);
  assert.equal(links.startSit, `/l/${LG}/start-sit?week=10&team=3`);
  assert.equal(links.waivers, `/l/${LG}/waivers?pos=QB&week=10&team=3`);
  const unscoped = weekLinks(week({ short_slots: [], unconfirmed_slots: ["D/ST"] }), 4, null);
  assert.equal(unscoped.startSit, "/start-sit?week=10&team=4");
  assert.equal(unscoped.waivers, "/waivers?view=STREAMING&pos=D%2FST&week=10&team=4");
});

test("waivers path: a flex slot or no slot gets no position filter", () => {
  assert.equal(waiversPath({ short_slots: ["FLEX"], unconfirmed_slots: [] }), "/waivers");
  assert.equal(waiversPath({ short_slots: [], unconfirmed_slots: [] }), "/waivers");
});

test("headlines restate the backend status and never call an unknown week safe", () => {
  assert.equal(weekHeadline(week({ status: "short", uncovered: 2, short_slots: ["QB", "TE"], byes: [1, 2, 3] })),
    "Week 10: 2 starting slots cannot be filled from the roster (QB, TE). 3 players on bye.");
  const unk = weekHeadline(week({ status: "unconfirmed", unknown: 1 }));
  assert.match(unk, /not confirmed\. 1 slot depends on players whose availability is unknown\./);
  assert.doesNotMatch(unk, /covered/);
  assert.equal(weekHeadline(week({ byes: [1] })), "Week 10: every starting slot is covered. 1 player on bye.");
});

test("reasons separate byes, verified absences, unknowns and bench alternatives", () => {
  const players = [
    player(1), player(2, { position: "TE", slot: "bench" }),
    player(3, { position: "RB", pro_team: "KC", weeks: { "10": { state: "out", code: "nfl_ineligible", source: "availability", detail: "Reserve/Injured since week 7", opponent: null, home: null, kickoff: null } } }),
    player(4, { position: "WR", pro_team: null }),
    player(5, { position: "WR", pro_team: "SEA", slot: "bench" }),
  ];
  const lines = weekReasons(week({ byes: [1, 2], bye_cluster: true, starters_on_bye: [1], out: [3],
    uncertain: [{ espn_player_id: 4, code: "no_team" }], bench_alternatives: [{ espn_player_id: 5, slots: ["WR", "FLEX"] }] }),
  players, { no_team: "no current NFL team" });
  assert.deepEqual(lines.map((l) => l.kind), ["bye", "out", "unknown", "alternatives"]);
  assert.equal(lines[0].label, "Bye cluster");
  assert.equal(lines[0].text, "P1 (QB, BUF), current starter and P2 (TE, BUF)");
  assert.equal(lines[1].text, "P3 (RB, KC): Reserve/Injured since week 7");
  assert.equal(lines[2].text, "P4 (WR): no current NFL team");
  assert.equal(lines[3].text, "P5 (WR, FLEX)");
});

test("summary counts the backend's statuses", () => {
  assert.equal(summaryText({ weeks: 3, covered: [9], short: [10], unconfirmed: [11], bye_cluster_weeks: [10],
    conflict_weeks: [10, 11], first_conflict_week: 10, most_byes: null }), "3 weeks: 1 covered, 1 short, 1 not confirmed");
});
