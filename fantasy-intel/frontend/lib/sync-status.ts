import type { SyncInfo } from "./types.ts";
import { normalizeProvider, providerName, type Provider } from "./labels.ts";

export type StatusPayload = {
  sync: SyncInfo;
  nfl_latest_week: number | null;
  league: { name: string; current_week: number } | null;
} | null;

export const FEEDS = [
  { key: "espn", label: "ESPN league" },
  { key: "nfl", label: "NFL stats" },
  { key: "analytics", label: "Analytics" },
] as const;
/** The feeds of a league from `provider`: its league sync is recorded under the provider's own source key. */
export function feedsFor(provider: Provider | string | null | undefined = "espn"): readonly { key: string; label: string }[] {
  const p = normalizeProvider(provider);
  if (p === "espn") return FEEDS;
  return [{ key: p, label: `${providerName(p)} league` }, ...FEEDS.slice(1)];
}
export const STALE_HOURS = 36;

/** The pill's dot: ok (green), stale (amber: old, failed or missing data), offline (red). */
export type SyncState = "ok" | "stale" | "offline";
export type FeedStatus = { key: string; label: string; timestamp: string | null; hours: number | null };
export type SyncFailure = { source: string; label: string; status: string; message: string | null; finished_at: string | null };
export type SyncSummary = {
  state: SyncState;
  text: string;
  feeds: FeedStatus[];
  activeFailures: SyncFailure[];
  nflLatestWeek: number | null;
  /** The status read itself was served from the league's previous files (`ApiResult.stale`): the data may be older. */
  degraded: boolean;
  /** The league has no published data yet (the API's 409 "first sync in progress"): the backend is up, not offline. */
  firstSync?: true;
};

/** The pill's text for a league with no published data yet. The API's 409 comes whether its first sync is queued, running
 *  or has failed, so the pill says only what is true in every case; the progress steps say which. */
export const FIRST_SYNC_TEXT = "No league data yet";

/** The pill's text when the backend served older files and nothing else is wrong. */
export const DEGRADED_TEXT = "Showing older data";

const OK = new Set(["ok", "success"]);

/** Backend timestamps without an offset are UTC. Unparseable values are null, never "now". */
export function parseTimestamp(v: string | null | undefined): number | null {
  if (!v) return null;
  const t = new Date(/([zZ]|[+-]\d{2}:?\d{2})$/.test(v) ? v : `${v}Z`).getTime();
  return Number.isNaN(t) ? null : t;
}

export function ago(hours: number): string {
  if (hours < 1) return "just now";
  if (hours < 48) return `${Math.floor(hours)}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

const feedLabel = (source: string) => [...FEEDS, ...feedsFor("sleeper")].find((f) => f.key === source)?.label ?? source;

/**
 * One conservative health summary for the /api/status payload.
 * - null (backend unreachable) is offline, unless the read answered "first sync in progress" (`firstSync`): a league
 *   that has no data yet, with the backend up.
 * - A failure counts only when it is its source's latest run and no newer success cleared it.
 * - Any feed never synced, or the oldest feed older than STALE_HOURS, is stale.
 * - `degraded` (the backend answered from the league's previous files) is stale too; a more specific problem keeps
 *   its own text, and the popover says why either way.
 */
export function computeSyncSummary(data: StatusPayload, now: number = Date.now(), provider: Provider = "espn",
                                   degraded = false, firstSync = false): SyncSummary {
  if (!data && firstSync) {
    return { state: "stale", text: FIRST_SYNC_TEXT, feeds: [], activeFailures: [], nflLatestWeek: null, degraded: false,
      firstSync: true };
  }
  if (!data) {
    return { state: "offline", text: "Backend offline", feeds: [], activeFailures: [], nflLatestWeek: null, degraded: false };
  }
  const lastSuccess = data.sync?.last_success ?? {};
  const runs = data.sync?.latest_runs ?? [];

  const feeds: FeedStatus[] = feedsFor(provider).map((f) => {
    const timestamp = lastSuccess[f.key] ?? null;
    const t = parseTimestamp(timestamp);
    return { key: f.key, label: f.label, timestamp, hours: t === null ? null : Math.max(0, (now - t) / 36e5) };
  });

  const latest = new Map<string, (typeof runs)[number]>();
  for (const run of runs) {
    if (!run?.source || (run.status ?? "").toLowerCase() === "interrupted") continue;   // neither running nor failed
    const prev = latest.get(run.source);
    if (!prev || (parseTimestamp(run.finished_at) ?? 0) >= (parseTimestamp(prev.finished_at) ?? 0)) latest.set(run.source, run);
  }
  const activeFailures: SyncFailure[] = [];
  for (const [source, run] of latest) {
    if (OK.has((run.status ?? "").toLowerCase())) continue;
    const success = parseTimestamp(lastSuccess[source]);
    const failed = parseTimestamp(run.finished_at);
    if (success !== null && failed !== null && success > failed) continue;     // cleared by a newer success
    activeFailures.push({ source, label: feedLabel(source), status: (run.status ?? "failed").toLowerCase(),
                          message: run.message ?? null, finished_at: run.finished_at ?? null });
  }

  const base = { feeds, activeFailures, nflLatestWeek: data.nfl_latest_week ?? null, degraded };
  const missing = feeds.find((f) => f.hours === null);
  if (feeds.every((f) => f.hours === null) && runs.length === 0) return { ...base, state: "stale", text: "Not synced" };
  if (activeFailures.length) {
    const f = activeFailures[0];
    return { ...base, state: "stale", text: `${f.label} sync ${f.status}` };
  }
  if (missing) return { ...base, state: "stale", text: `${missing.label} never synced` };
  const oldest = feeds.reduce((m, f) => (f.hours! > m.hours! ? f : m));
  if (oldest.hours! > STALE_HOURS) return { ...base, state: "stale", text: `${oldest.label} ${ago(oldest.hours!)}` };
  if (degraded) return { ...base, state: "stale", text: DEGRADED_TEXT };
  return { ...base, state: "ok", text: `Synced ${ago(oldest.hours!)}` };
}
