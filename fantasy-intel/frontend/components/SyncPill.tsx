import Link from "next/link";
import { when } from "@/lib/format";
import { type StatusPayload, computeSyncSummary } from "@/lib/sync-status";
import { SyncPopover } from "./SyncPopover";

/** One health dot for every feed; the details (and Data quality) open from it. Rendered per request
 * (the root layout is force-dynamic), so the ages are as of this request. */
export function SyncPill({ status }: { status: StatusPayload }) {
  const s = computeSyncSummary(status);
  return (
    <SyncPopover state={s.state} text={s.text}>
      {s.state === "offline" ? (
        <div className="sync-callout bad">
          <b>Cannot reach the local API.</b> Start it from <code>fantasy-intel/backend</code>:
          <code className="block">uv run uvicorn app.main:app --reload</code>
        </div>
      ) : (
        <>
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
