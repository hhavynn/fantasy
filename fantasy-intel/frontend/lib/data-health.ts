// Pure helpers for the Data quality health strip. They only count and pick rows the backend
// already returned (sync runs, mapping rows, validation issues).

export type SyncRunRow = { run_id: string; source: string; started_at: string; finished_at: string | null; status: string; message: string | null };

/** Backend timestamps without an offset are UTC. Unparseable values are null. */
function parseTimestamp(v: string | null | undefined): number | null {
  if (!v) return null;
  const t = new Date(/([zZ]|[+-]\d{2}:?\d{2})$/.test(v) ? v : `${v}Z`).getTime();
  return Number.isNaN(t) ? null : t;
}

const isOk = (status: string) => ["ok", "success"].includes(status.toLowerCase());

/** The most recent run for `source` (by finish time, else start time), whatever its status. */
export function latestRun(runs: readonly SyncRunRow[], source: string): SyncRunRow | null {
  let best: SyncRunRow | null = null;
  let bestT = -Infinity;
  for (const r of runs) {
    if (r.source !== source) continue;
    const t = parseTimestamp(r.finished_at) ?? parseTimestamp(r.started_at) ?? -Infinity;
    if (best === null || t >= bestT) { best = r; bestT = t; }
  }
  return best;
}

/** The most recent successful, finished run for `source`; null if none. */
export function lastSuccessfulRun(runs: readonly SyncRunRow[], source: string): SyncRunRow | null {
  return latestRun(runs.filter((r) => isOk(r.status) && r.finished_at), source);
}

/** True when the newest run for `source` did not succeed (a failure no later success has cleared). */
export function latestFailed(runs: readonly SyncRunRow[], source: string): boolean {
  const r = latestRun(runs, source);
  return r !== null && !isOk(r.status);
}

export type MappingCoverage = { total: number; matched: number; unmatched: number; pct: number | null };

/** Players with any mapping against all players the crosswalk covers. `pct` is null when there are none. */
export function mappingCoverage(summary: readonly { match_method: string; n: number }[]): MappingCoverage {
  const total = summary.reduce((s, r) => s + r.n, 0);
  const unmatched = summary.filter((r) => r.match_method === "unmatched").reduce((s, r) => s + r.n, 0);
  const matched = total - unmatched;
  return { total, matched, unmatched, pct: total === 0 ? null : matched / total };
}

/** Validation issues by severity; anything not "error" counts as a warning. */
export function validationCounts(issues: readonly { severity: string }[]): { errors: number; warnings: number } {
  const errors = issues.filter((i) => i.severity.toLowerCase() === "error").length;
  return { errors, warnings: issues.length - errors };
}

export type FeedHealth = { finishedAt: string | null; hours: number | null; failed: boolean };

/** When `source` last succeeded (and how many hours ago), and whether its newest run failed. */
export function feedHealth(runs: readonly SyncRunRow[], source: string, now: number = Date.now()): FeedHealth {
  const ok = lastSuccessfulRun(runs, source);
  const t = parseTimestamp(ok?.finished_at);
  return { finishedAt: ok?.finished_at ?? null, hours: t === null ? null : Math.max(0, (now - t) / 36e5), failed: latestFailed(runs, source) };
}
