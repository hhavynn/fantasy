import assert from "node:assert/strict";
import test from "node:test";
import { feedHealth, latestFailed, lastSuccessfulRun, latestRun, mappingCoverage, validationCounts, type SyncRunRow } from "./data-health.ts";

const run = (id: string, source: string, started: string, finished: string | null, status = "ok"): SyncRunRow => ({
  run_id: id, source, started_at: started, finished_at: finished, status, message: null,
});

const runs = [
  run("a1", "espn", "2026-10-15T06:00:00", "2026-10-15T06:02:00"),
  run("a2", "espn", "2026-10-15T08:00:00", "2026-10-15T08:01:00", "failed"),
  run("n1", "nfl", "2026-10-15T05:00:00", "2026-10-15T05:05:00"),
  run("x1", "analytics", "2026-10-15T09:00:00", null, "running"),
];

test("latestRun picks the newest run per source regardless of status", () => {
  assert.equal(latestRun(runs, "espn")?.run_id, "a2");
  assert.equal(latestRun(runs, "nfl")?.run_id, "n1");
  assert.equal(latestRun(runs, "sleeper"), null);
});

test("lastSuccessfulRun skips failed and unfinished runs", () => {
  assert.equal(lastSuccessfulRun(runs, "espn")?.run_id, "a1");
  assert.equal(lastSuccessfulRun(runs, "analytics"), null);
});

test("latestFailed is true only when the newest run did not succeed", () => {
  assert.equal(latestFailed(runs, "espn"), true);
  assert.equal(latestFailed(runs, "nfl"), false);
  assert.equal(latestFailed(runs, "sleeper"), false);
});

test("mappingCoverage counts everything but unmatched as matched", () => {
  const c = mappingCoverage([
    { match_method: "espn_id:rosters_weekly", n: 789 },
    { match_method: "team_defense", n: 32 },
    { match_method: "unmatched", n: 3 },
  ]);
  assert.deepEqual({ total: c.total, matched: c.matched, unmatched: c.unmatched }, { total: 824, matched: 821, unmatched: 3 });
  assert.ok(Math.abs((c.pct ?? 0) - 821 / 824) < 1e-12);
});

test("mappingCoverage with no rows has no percentage", () => {
  assert.equal(mappingCoverage([]).pct, null);
  assert.equal(mappingCoverage([]).total, 0);
});

test("validationCounts splits errors from warnings", () => {
  assert.deepEqual(validationCounts([{ severity: "error" }, { severity: "warning" }, { severity: "WARN" }]), { errors: 1, warnings: 2 });
  assert.deepEqual(validationCounts([]), { errors: 0, warnings: 0 });
});

test("feedHealth reports hours since the last success and a newer failure", () => {
  const now = Date.parse("2026-10-15T08:02:00Z");
  const e = feedHealth(runs, "espn", now);
  assert.equal(e.finishedAt, "2026-10-15T06:02:00");
  assert.ok(Math.abs((e.hours ?? 0) - 2) < 1e-9);
  assert.equal(e.failed, true);
  const a = feedHealth(runs, "analytics", now);
  assert.equal(a.hours, null);
  assert.equal(a.finishedAt, null);
});
