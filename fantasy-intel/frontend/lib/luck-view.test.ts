import assert from "node:assert/strict";
import test from "node:test";
import { heatOf, luckBars, periodLabel, scatterPoints, shortPeriod, signedText, timelineSeries } from "./luck-view.ts";
import type { LlLeader, LlTimeline } from "./luck-types.ts";

const leader = (over: Partial<LlLeader> & { team_id: number; name: string }): LlLeader => ({
  is_mine: false, luck_rank: 1, schedule_luck: 0, actual: { wins: 2, losses: 1, ties: 0, win_equivalents: 2 },
  expected_wins: 2, expected_wins_rank: 1, all_play: { wins: 1, losses: 1, ties: 0, pct: 0.5 },
  espn_record: { wins: 2, losses: 1, ties: 0 }, scoring_mean: 100, scoring_rank: 1, opponent_points_mean: 100, difficulty_rank: 1, periods: 3,
  ...over,
});

test("periodLabel handles one week, a span and no weeks", () => {
  assert.equal(periodLabel([6], 6), "wk 6");
  assert.equal(periodLabel([14, 15], 14), "wk 14–15");
  assert.equal(periodLabel(null, 3), "period 3");
  assert.equal(periodLabel([], 3), "period 3");
});

test("shortPeriod mirrors periodLabel for axes", () => {
  assert.equal(shortPeriod([6], 6), "6");
  assert.equal(shortPeriod([14, 15], 14), "14–15");
  assert.equal(shortPeriod(undefined, 3), "P3");
});

test("signedText uses a real minus and never signs a rounded zero", () => {
  assert.equal(signedText(0.8), "+0.80");
  assert.equal(signedText(-1.234, 1), "−1.2");
  assert.equal(signedText(0.001, 2), "0.00");
  assert.equal(signedText(-0.001, 2), "0.00");
  assert.equal(signedText(null), "—");
});

test("luckBars sorts by luck descending and marks your team", () => {
  const bars = luckBars([
    leader({ team_id: 1, name: "A", schedule_luck: -1 }),
    leader({ team_id: 2, name: "B", schedule_luck: 2, is_mine: true }),
    leader({ team_id: 3, name: "C", schedule_luck: 0.5 }),
  ]);
  assert.deepEqual(bars.map((b) => b.label), ["B (you)", "C", "A"]);
  assert.deepEqual(bars.map((b) => b.value), [2, 0.5, -1]);
});

test("scatterPoints labels your team and big movers only", () => {
  const pts = scatterPoints([
    leader({ team_id: 1, name: "Mine", is_mine: true, schedule_luck: 0.1 }),
    leader({ team_id: 2, name: "Lucky Team With A Long Name", schedule_luck: 1.5 }),
    leader({ team_id: 3, name: "Quiet", schedule_luck: -0.3 }),
  ]);
  assert.equal(pts[0].label, "Mine");
  assert.equal(pts[0].highlight, true);
  assert.equal(pts[1].label, "Lucky Team With A Long Name");
  assert.equal(pts[2].label, undefined);
  assert.equal(pts[2].x, 2);
});

test("timelineSeries keeps backend order", () => {
  const row = (period: number, luck: number, cum: number) => ({
    matchup_period: period, weeks: [period], score: 100 + period, opponent_score: 90 + period, luck, cumulative_luck: cum,
  }) as unknown as LlTimeline;
  const s = timelineSeries([row(1, 0.5, 0.5), row(2, -0.5, 0)]);
  assert.deepEqual(s.labels, ["1", "2"]);
  assert.deepEqual(s.cumulative, [0.5, 0]);
  assert.deepEqual(s.score, [101, 102]);
});

test("heatOf stays within 0..1", () => {
  assert.equal(heatOf(0), 0.08);
  assert.equal(heatOf(1), 0.58);
  assert.equal(heatOf(5), 1);
  assert.equal(heatOf(-3), 0);
});
