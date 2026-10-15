import assert from "node:assert/strict";
import { test } from "node:test";
import { computeSyncSummary } from "./sync-status.ts";
import type { SyncInfo } from "./types.ts";

test("backend unreachable (null input) reports offline state with bad tone", () => {
  const result = computeSyncSummary(null);
  assert.equal(result.state, "offline");
  assert.equal(result.label, "Backend offline");
  assert.equal(result.tone, "bad");
  assert.equal(result.activeFailures.length, 0);
  assert.equal(result.details.espn.timestamp, null);
});

test("completely unsynced data (all last_success null, no runs) reports not_synced", () => {
  const data = {
    league: { name: "Test League", current_week: 1 },
    nfl_latest_week: null,
    sync: {
      last_success: { espn: null, nfl: null, analytics: null },
      latest_runs: [],
    },
  };
  const result = computeSyncSummary(data);
  assert.equal(result.state, "not_synced");
  assert.equal(result.label, "Not synced");
  assert.equal(result.tone, "warn");
  assert.equal(result.activeFailures.length, 0);
});

test("healthy sync runs report normal status with neutral tone (supports status 'ok' and 'success')", () => {
  const data = {
    league: { name: "Test League", current_week: 5 },
    nfl_latest_week: 5,
    sync: {
      last_success: {
        espn: "2026-10-14T12:00:00Z",
        nfl: "2026-10-14T12:05:00Z",
        analytics: "2026-10-14T12:10:00Z",
      },
      latest_runs: [
        { source: "espn", status: "ok", message: null, finished_at: "2026-10-14T12:00:00Z" },
        { source: "nfl", status: "success", message: null, finished_at: "2026-10-14T12:05:00Z" },
        { source: "analytics", status: "ok", message: null, finished_at: "2026-10-14T12:10:00Z" },
      ],
    },
  };
  const result = computeSyncSummary(data);
  assert.equal(result.state, "healthy");
  assert.equal(result.label, "Sync status");
  assert.equal(result.tone, "neutral");
  assert.equal(result.activeFailures.length, 0);
  assert.equal(result.details.nfl.statsWeek, 5);
});

test("active failure in latest run without subsequent success reports sync warning", () => {
  const data = {
    league: { name: "Test League", current_week: 5 },
    nfl_latest_week: 4,
    sync: {
      last_success: {
        espn: "2026-10-14T10:00:00Z",
        nfl: "2026-10-14T09:00:00Z",
        analytics: "2026-10-14T10:05:00Z",
      },
      latest_runs: [
        { source: "espn", status: "error", message: "ESPN API 500", finished_at: "2026-10-14T11:00:00Z" },
        { source: "nfl", status: "success", message: null, finished_at: "2026-10-14T09:00:00Z" },
      ],
    },
  };
  const result = computeSyncSummary(data);
  assert.equal(result.state, "warning");
  assert.equal(result.label, "Sync warning");
  assert.equal(result.tone, "warn");
  assert.equal(result.activeFailures.length, 1);
  assert.equal(result.activeFailures[0].source, "espn");
  assert.equal(result.activeFailures[0].message, "ESPN API 500");
});

test("old failure cleared by later successful sync does not trigger warning", () => {
  const data = {
    league: { name: "Test League", current_week: 5 },
    nfl_latest_week: 5,
    sync: {
      last_success: {
        espn: "2026-10-14T12:00:00Z",
        nfl: "2026-10-14T11:00:00Z",
        analytics: "2026-10-14T12:05:00Z",
      },
      latest_runs: [
        { source: "espn", status: "error", message: "ESPN API 500", finished_at: "2026-10-14T10:00:00Z" },
        { source: "espn", status: "success", message: null, finished_at: "2026-10-14T12:00:00Z" },
      ],
    },
  };
  const result = computeSyncSummary(data);
  assert.equal(result.state, "healthy");
  assert.equal(result.label, "Sync status");
  assert.equal(result.tone, "neutral");
  assert.equal(result.activeFailures.length, 0);
});

test("partially populated status response is handled gracefully without crashing", () => {
  const data = {
    league: null,
    nfl_latest_week: null,
    sync: {
      last_success: { espn: "2026-10-14T12:00:00Z" },
      latest_runs: [],
    } as unknown as SyncInfo,
  };
  const result = computeSyncSummary(data);
  assert.equal(result.state, "healthy");
  assert.equal(result.label, "Sync status");
  assert.equal(result.details.espn.timestamp, "2026-10-14T12:00:00Z");
  assert.equal(result.details.nfl.timestamp, null);
  assert.equal(result.details.analytics.timestamp, null);
});
