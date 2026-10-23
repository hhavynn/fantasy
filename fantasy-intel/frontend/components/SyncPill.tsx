import Link from "@/components/LeagueLink";
import { when } from "@/lib/format";
import type { Provider } from "@/lib/labels";
import { type StatusPayload, computeSyncSummary } from "@/lib/sync-status";
import { SyncPopover } from "./SyncPopover";

/** One health dot for every feed; the details (and Data quality) open from it. Rendered per request
 * (the root layout is force-dynamic), so the ages are as of this request. `degraded`: the status read came back
 * `stale` (lib/api.ts), the backend serving the league's previous files. */
export function SyncPill({ status, provider = "espn", degraded = false }: { status: StatusPayload; provider?: Provider; degraded?: boolean }) {
  const s = computeSyncSummary(status, undefined, provider, degraded);
  return (
    <SyncPopover state={s.state} text={s.text}>
      {s.state === "offline" ? (
        <div className="sync-callout bad">
          <b>Cannot reach the local API.</b> Start it from <code>fantasy-intel/backend</code>:
          <code className="block">uv run uvicorn app.main:app --reload</code>
        </div>
      ) : (
        <>
          {s.degraded && (
            <div className="sync-callout warn">
              <b>Showing older data.</b> The newest league files could not be loaded in time, so this page may use the
              previous ones. Reload in a moment for the latest.
            </div>
          )}
          {s.activeFailures.map((f) => (
            <div key={f.source} className="sync-callout warn">
              <b>{f.label} sync {f.status}</b>
              {f.message ? <span> · {f.message}</span> : null}
              {f.finished_at ? <span className="faint"> · {when(f.finished_at)}</span> : null}
            </div>
          ))}
          <dl>
            {s.feeds.map((f) => (
              <div key={f.key}>
                <dt>{f.label}</dt>
                <dd>
                  {when(f.timestamp)}
                  {f.key === "nfl" && s.nflLatestWeek ? <span className="faint"> · thru wk {s.nflLatestWeek}</span> : null}
                </dd>
              </div>
            ))}
          </dl>
        </>
      )}
      <Link href="/data">Data quality report →</Link>
    </SyncPopover>
  );
}
