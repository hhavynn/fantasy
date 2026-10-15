import Link from "next/link";
import { when } from "@/lib/format";
import type { SyncInfo } from "@/lib/types";

const FEEDS: { key: string; label: string }[] = [
  { key: "espn", label: "ESPN league" },
  { key: "nfl", label: "NFL stats" },
  { key: "analytics", label: "Analytics" },
];
const STALE_HOURS = 36;

function parse(v: string): Date {
  return new Date(v.endsWith("Z") || v.includes("+") ? v : `${v}Z`);
}

function ago(hours: number): string {
  if (hours < 1) return "just now";
  if (hours < 48) return `${Math.floor(hours)}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** One health dot for every feed; the details (and Data quality) open from it. */
export function SyncPill({ sync, nflLatestWeek }: { sync: SyncInfo | null; nflLatestWeek: number | null }) {
  if (!sync) {
    return <Link className="sync-pill" data-state="offline" href="/data">Backend offline</Link>;
  }
  // Server component rendered per request (the root layout is force-dynamic), so reading the clock here is intended.
  // eslint-disable-next-line react-hooks/purity
  const now = Date.now();
  const ages = FEEDS.map((f) => {
    const v = sync.last_success[f.key];
    return { ...f, hours: v ? (now - parse(v).getTime()) / 36e5 : null };
  });
  const missing = ages.find((a) => a.hours === null);
  const oldest = ages.reduce((m, a) => (a.hours !== null && a.hours > (m?.hours ?? -1) ? a : m), null as (typeof ages)[number] | null);
  const failed = sync.latest_runs.find((r) => !["ok", "success"].includes(r.status.toLowerCase()));
  const stale = Boolean(missing || failed || (oldest && oldest.hours! > STALE_HOURS));
  const text = missing
    ? `${missing.label} never synced`
    : failed
      ? `${failed.source} sync ${failed.status.toLowerCase()}`
      : stale
        ? `${oldest!.label} ${ago(oldest!.hours!)}`
        : `Synced ${ago(oldest?.hours ?? 0)}`;

  return (
    <details className="sync">
      <summary className="sync-pill" data-state={stale ? "stale" : "ok"}>{text}</summary>
      <div className="sync-pop">
        <dl>
          {FEEDS.map((f) => (
            <div key={f.key}>
              <dt>{f.label}</dt>
              <dd>
                {when(sync.last_success[f.key])}
                {f.key === "nfl" && nflLatestWeek ? <span className="faint"> · thru wk {nflLatestWeek}</span> : null}
              </dd>
            </div>
          ))}
        </dl>
        <Link href="/data">Data quality report →</Link>
      </div>
    </details>
  );
}
