import type { SyncInfo } from "./types.ts";

export type SyncHealthState = "healthy" | "warning" | "offline" | "not_synced";

export type SyncSourceDetail = {
  timestamp: string | null;
  formatted: string;
};

export type SyncFailure = {
  source: string;
  message: string | null;
  finished_at: string | null;
};

export type SyncSummary = {
  state: SyncHealthState;
  label: string;
  tone: "neutral" | "warn" | "bad";
  details: {
    espn: SyncSourceDetail;
    nfl: SyncSourceDetail & { statsWeek: number | null };
    analytics: SyncSourceDetail;
  };
  league: { name: string; current_week: number } | null;
  activeFailures: SyncFailure[];
};

export function formatSyncTime(v: string | null | undefined): string {
  if (!v) return "Never synced";
  try {
    const d = new Date(v.endsWith("Z") || v.includes("+") ? v : `${v}Z`);
    if (Number.isNaN(d.getTime())) return "Invalid date";
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return "Invalid date";
  }
}

/**
 * Computes a conservative sync status summary from the /api/status payload.
 * - Backend offline: null input returns 'offline' with 'bad' tone.
 * - Completely unsynced: returns 'not_synced' with 'warn' tone.
 * - Active failures: latest run for a source failed and has no newer success timestamp -> 'warning' with 'warn' tone.
 * - Resolved failures: a newer success clears older failures for that source.
 * - Healthy: normal 'healthy' state with 'neutral' tone.
 */
export function computeSyncSummary(
  data: {
    sync: SyncInfo;
    nfl_latest_week: number | null;
    league: { name: string; current_week: number } | null;
  } | null
): SyncSummary {
  if (!data) {
    return {
      state: "offline",
      label: "Backend offline",
      tone: "bad",
      details: {
        espn: { timestamp: null, formatted: "Backend unreachable" },
        nfl: { timestamp: null, formatted: "Backend unreachable", statsWeek: null },
        analytics: { timestamp: null, formatted: "Backend unreachable" },
      },
      league: null,
      activeFailures: [],
    };
  }

  const sync = data.sync;
  const lastSuccess = sync?.last_success ?? {};
  const latestRuns = sync?.latest_runs ?? [];

  const espnTimestamp = lastSuccess.espn ?? null;
  const nflTimestamp = lastSuccess.nfl ?? null;
  const analyticsTimestamp = lastSuccess.analytics ?? null;

  const hasAnySuccess = Boolean(espnTimestamp || nflTimestamp || analyticsTimestamp);

  // Group latest runs by source and find the most recent one for each
  const runsBySource = new Map<string, (typeof latestRuns)[number]>();
  for (const run of latestRuns) {
    if (!run?.source) continue;
    const existing = runsBySource.get(run.source);
    if (!existing) {
      runsBySource.set(run.source, run);
    } else {
      const existingTime = existing.finished_at ? new Date(existing.finished_at).getTime() : 0;
      const runTime = run.finished_at ? new Date(run.finished_at).getTime() : 0;
      if (runTime >= existingTime) {
        runsBySource.set(run.source, run);
      }
    }
  }

  const activeFailures: SyncFailure[] = [];
  for (const [source, run] of runsBySource.entries()) {
    const isError = run.status && run.status.toLowerCase() !== "success";
    if (isError) {
      const successTs = lastSuccess[source];
      if (successTs && run.finished_at) {
        const successTime = new Date(successTs).getTime();
        const failureTime = new Date(run.finished_at).getTime();
        if (successTime > failureTime) {
          // An earlier failure was cleared by a later successful run
          continue;
        }
      }
      activeFailures.push({
        source,
        message: run.message ?? null,
        finished_at: run.finished_at ?? null,
      });
    }
  }

  let state: SyncHealthState = "healthy";
  let label = "Sync status";
  let tone: "neutral" | "warn" | "bad" = "neutral";

  if (activeFailures.length > 0) {
    state = "warning";
    label = "Sync warning";
    tone = "warn";
  } else if (!hasAnySuccess && latestRuns.length === 0) {
    state = "not_synced";
    label = "Not synced";
    tone = "warn";
  }

  return {
    state,
    label,
    tone,
    details: {
      espn: { timestamp: espnTimestamp, formatted: formatSyncTime(espnTimestamp) },
      nfl: {
        timestamp: nflTimestamp,
        formatted: formatSyncTime(nflTimestamp),
        statsWeek: data.nfl_latest_week ?? null,
      },
      analytics: { timestamp: analyticsTimestamp, formatted: formatSyncTime(analyticsTimestamp) },
    },
    league: data.league ?? null,
    activeFailures,
  };
}
