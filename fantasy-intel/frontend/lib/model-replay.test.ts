import assert from "node:assert/strict";
import test from "node:test";
import { replaySummary, replayView, totalDiff, weeksWith, type ReplayComparison, type ReplayResponse } from "./model-replay.ts";

const cell = (diff: number, n = 10) => ({ diff, direction: Math.abs(diff) < 0.05 ? "same" : diff < 0 ? "better" : "worse", n_new: n, n_old: n } as const);
const comparison = (diffs: Record<number, number>): ReplayComparison => ({
  weeks: Object.entries(diffs).map(([w, d]) => ({
    week: Number(w), groups: { ALL: cell(d), QB: cell(d), RB: null, WR: cell(d, 30), TE: null },
  })),
  tally: { ALL: { better: 0, worse: 0, same: 0 }, QB: { better: 0, worse: 0, same: 0 }, RB: { better: 0, worse: 0, same: 0 },
    WR: { better: 0, worse: 0, same: 0 }, TE: { better: 0, worse: 0, same: 0 } },
});

test("summary names the weeks that got closer, further off and stayed the same", () => {
  const c = comparison({ 1: -0.2, 2: 0.1, 3: -0.3, 4: 0.2, 5: 0.01 });
  assert.deepEqual(weeksWith(c, "ALL", "better"), [1, 3]);
  assert.equal(replaySummary(c), "Closer in weeks 1 and 3, further off in weeks 2 and 4, about the same in week 5.");
  assert.equal(replaySummary(comparison({})), "No week to compare yet.");
});

test("season difference is weighted by player-weeks and skips groups with nothing to compare", () => {
  const c = comparison({ 1: -0.2, 2: 0.1 });
  assert.ok(Math.abs((totalDiff(c, "ALL") ?? NaN) - -0.05) < 1e-9);
  assert.equal(totalDiff(c, "RB"), null);
});

test("deltas are against the previous model once one exists, else the prior version", () => {
  const snap = { model: { version: "ifp-2.2.0", fingerprint: "abcdef0123" }, prior_version: { version: "ifp-2.1.0" },
    generated_at: "2026-10-19T00:00:00+00:00" } as unknown as ReplayResponse["latest"];
  const base = { available: true, latest: snap, previous: null, vs_previous: null, vs_prior: comparison({ 1: -0.1 }) } as unknown as ReplayResponse;
  assert.equal(replayView(base)?.mode, "prior_version");
  assert.equal(replayView(base)?.label, "ifp-2.1.0");
  const changed = { ...base, previous: snap, vs_previous: comparison({ 1: 0.2 }) } as ReplayResponse;
  assert.equal(replayView(changed)?.mode, "previous_snapshot");
  assert.match(replayView(changed)?.label ?? "", /ifp-2\.2\.0 · abcdef0, replayed 2026-10-19/);
  assert.equal(replayView({ ...base, latest: null }), null);
});
