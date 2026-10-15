import assert from "node:assert/strict";
import { test } from "node:test";
import { computeSyncSummary, parseTimestamp } from "./sync-status.ts";
import type { SyncInfo } from "./types.ts";

const NOW = Date.parse("2026-10-14T14:00:00Z");
const status = (sync: SyncInfo, nfl_latest_week: number | null = 5) =>
  ({ league: { name: "Test League", current_week: 5 }, nfl_latest_week, sync });
const ok = (source: string, finished_at: string) => ({ source, status: "ok", message: null, finished_at });

test("backend unreachable (null input) is offline", () => {
  const r = computeSyncSummary(null, NOW);
  assert.equal(r.state, "offline");
  assert.equal(r.text, "Backend offline");
  assert.equal(r.activeFailures.length, 0);
});

test("completely unsynced data reports Not synced", () => {
  const r = computeSyncSummary(status({ last_success: { espn: null, nfl: null, analytics: null }, latest_runs: [] }), NOW);
  assert.equal(r.state, "stale");
  assert.equal(r.text, "Not synced");
});

test("recent successful syncs are ok (status 'ok' and 'success') and report the oldest feed's age", () => {
  const r = computeSyncSummary(status({
    last_success: { espn: "2026-10-14T12:00:00Z", nfl: "2026-10-14T11:05:00Z", analytics: "2026-10-14T12:10:00Z" },
    latest_runs: [ok("espn", "2026-10-14T12:00:00Z"), { ...ok("nfl", "2026-10-14T11:05:00Z"), status: "success" },
      ok("analytics", "2026-10-14T12:10:00Z")],
  }), NOW);
  assert.equal(r.state, "ok");
  assert.equal(r.text, "Synced 2h ago");
  assert.equal(r.activeFailures.length, 0);
  assert.equal(r.nflLatestWeek, 5);
});

test("a latest run that failed with no newer success is an active failure", () => {
  const r = computeSyncSummary(status({
    last_success: { espn: "2026-10-14T10:00:00Z", nfl: "2026-10-14T09:00:00Z", analytics: "2026-10-14T10:05:00Z" },
    latest_runs: [{ source: "espn", status: "error", message: "ESPN API 500", finished_at: "2026-10-14T11:00:00Z" },
      { ...ok("nfl", "2026-10-14T09:00:00Z"), status: "success" }],
  }), NOW);
  assert.equal(r.state, "stale");
  assert.equal(r.text, "ESPN league sync error");
  assert.deepEqual(r.activeFailures.map((f) => [f.source, f.message]), [["espn", "ESPN API 500"]]);
});

test("an old failure cleared by a later success is not a warning", () => {
  const r = computeSyncSummary(status({
    last_success: { espn: "2026-10-14T12:00:00Z", nfl: "2026-10-14T11:00:00Z", analytics: "2026-10-14T12:05:00Z" },
    latest_runs: [{ source: "espn", status: "error", message: "ESPN API 500", finished_at: "2026-10-14T10:00:00Z" },
      ok("espn", "2026-10-14T12:00:00Z")],
  }), NOW);
  assert.equal(r.state, "ok");
  assert.equal(r.activeFailures.length, 0);
});

test("a failure older than the feed's last success (naive UTC timestamps) is cleared", () => {
  const r = computeSyncSummary(status({
    last_success: { espn: "2026-10-14 12:00:00", nfl: "2026-10-14 12:00:00", analytics: "2026-10-14 12:00:00" },
    latest_runs: [{ source: "nfl", status: "error", message: "timeout", finished_at: "2026-10-14 11:00:00" }],
  }), NOW);
  assert.equal(r.state, "ok");
});

test("a feed that never synced or an old feed is stale", () => {
  const partial = computeSyncSummary(status({
    last_success: { espn: "2026-10-14T12:00:00Z" }, latest_runs: [],
  } as unknown as SyncInfo), NOW);
  assert.equal(partial.state, "stale");
  assert.equal(partial.text, "NFL stats never synced");
  assert.equal(partial.feeds.find((f) => f.key === "analytics")?.timestamp, null);

  const old = computeSyncSummary(status({
    last_success: { espn: "2026-10-14T12:00:00Z", nfl: "2026-10-11T12:00:00Z", analytics: "2026-10-14T12:00:00Z" },
    latest_runs: [],
  }), NOW);
  assert.equal(old.state, "stale");
  assert.equal(old.text, "NFL stats 3d ago");
});

test("timestamps: naive values are UTC, offsets are honoured, garbage is null", () => {
  assert.equal(parseTimestamp("2026-10-14 12:00:00"), Date.parse("2026-10-14T12:00:00Z"));
  assert.equal(parseTimestamp("2026-10-14T05:00:00-07:00"), Date.parse("2026-10-14T12:00:00Z"));
  assert.equal(parseTimestamp("not a date"), null);
  assert.equal(parseTimestamp(null), null);
});
