import assert from "node:assert/strict";
import { test } from "node:test";
import {
  barWidth, biggestMovers, cardHref, componentValueText, movementText, ordinal, pctText, recordText, splitRanked,
  weightText, type PrTeam,
} from "./power-rankings.ts";

function t(id: number, rank: number | null, movement: number | null, previous: number | null = null): PrTeam {
  const c = { value: null, score: null, rank: null, previous_value: null, previous_score: null };
  return {
    team_id: id, name: `Team ${id}`, abbrev: null, is_mine: false, rank, previous_rank: previous, movement,
    power_score: null, previous_power_score: null, record: { wins: 1, losses: 1, ties: 0 }, points_for: null,
    components: { strength: c, depth: c, replacement: c, all_play: c }, all_play: { wins: 0, losses: 0, ties: 0, pct: null },
    depth: null, replacement: null, positions: null, unranked_reason: null,
  };
}

test("ordinal suffixes, including the teens", () => {
  assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 112].map(ordinal), ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "112th"]);
});

test("records and percentages: missing is a dash, ties only when present", () => {
  assert.equal(recordText({ wins: 4, losses: 1, ties: 0 }), "4-1");
  assert.equal(recordText({ wins: 3, losses: 1, ties: 1 }), "3-1-1");
  assert.equal(recordText({ wins: null, losses: 1 }), "—");
  assert.equal(pctText(0.6182), "62%");
  assert.equal(pctText(null), "—");
  assert.equal(weightText(0.45), "45%");
});

test("movement text names the direction and last week's rank", () => {
  assert.equal(movementText({ movement: 2, previous_rank: 5 }), "Up 2 from 5th");
  assert.equal(movementText({ movement: -1, previous_rank: 3 }), "Down 1 from 3rd");
  assert.equal(movementText({ movement: 0, previous_rank: 1 }), "No change from 1st");
  assert.equal(movementText({ movement: null, previous_rank: null }), null);
});

test("biggest movers: largest rise and fall, ties go to the better current rank", () => {
  const m = biggestMovers([t(1, 1, 0, 1), t(2, 2, 2, 4), t(3, 3, 2, 5), t(4, 4, -3, 1), t(5, 5, -1, 4), t(6, null, null)]);
  assert.equal(m.rise?.team_id, 2);
  assert.equal(m.fall?.team_id, 4);
  assert.equal(m.steady, false);
  assert.equal(biggestMovers([t(1, 1, 0, 1), t(2, 2, 0, 2)]).steady, true);
  const none = biggestMovers([t(1, 1, null), t(2, 2, null)]);
  assert.equal(none.rise, null);
  assert.equal(none.steady, false);
});

test("component values: all-play as a percentage, points to one decimal, missing as a dash", () => {
  assert.equal(componentValueText("all_play", 0.5), "50%");
  assert.equal(componentValueText("strength", 134.704), "134.7");
  assert.equal(componentValueText("depth", null), "—");
});

test("bar widths clamp to 0-100 and missing has no bar", () => {
  assert.equal(barWidth(140), 100);
  assert.equal(barWidth(-5), 0);
  assert.equal(barWidth(42.5), 42.5);
  assert.equal(barWidth(null), null);
});

test("unranked teams are split out, order kept", () => {
  const { ranked, unranked } = splitRanked([t(1, 1, null), t(2, null, null), t(3, 2, null)]);
  assert.deepEqual(ranked.map((x) => x.team_id), [1, 3]);
  assert.deepEqual(unranked.map((x) => x.team_id), [2]);
  assert.equal(cardHref(7), "/power-rankings/7");
});
