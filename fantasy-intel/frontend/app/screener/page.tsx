import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { Glossary, Method, Notice, PageHeader, Section, Stat, StatStrip } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { plural } from "@/lib/plural";
import {
  CONTRACT, apiQuery, filterText, isPreset, parseState, presetHref,
  type MetricsResponse, type ScreenResponse, type ScreenerState, type SearchParams,
} from "@/lib/screener";
import { Builder } from "./Builder";
import { Results } from "./Results";
import styles from "./screener.module.css";

export const metadata = { title: "Player screener" };

const TITLE = "Player screener";
const EYEBROW = "Players · Research";

function summaryText(s: ScreenerState, d: MetricsResponse): string {
  const by = new Map(d.metrics.map((m) => [m.key, m]));
  const filters = s.rows.map((r) => filterText(r, by.get(r.metric)));
  const pos = s.pos.length ? s.pos.join(", ") : "WR, RB, TE";
  return `${pos} · ${filters.length ? filters.join("; ") : "no filters"}`;
}

// Advanced player screener (issue #226): typed multi-metric screens over the whole NFL WR / RB / TE pool from stored
// nflverse usage (and route data only where an authorized import supplied it), with league ownership from this
// league's own player pool. Every number is the backend's (GET /api/screener, contract screener/v1); missing stays
// missing, and the page never runs a screen it could not parse.
export default async function ScreenerPage({ searchParams }: PageProps<"/screener">) {
  const sp = (await searchParams) as SearchParams;
  const state = parseState(sp);
  const meta = await api<MetricsResponse>("/api/screener/metrics");
  if (!meta.ok) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} />
        <BackendProblem error={meta.error} kind={meta.kind} status={meta.status} />
      </div>
    );
  }
  if (meta.data.contract !== CONTRACT) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} />
        <Notice tone="caution" title="The backend sent an unexpected format">
          This page reads the {CONTRACT} response and the backend answered with {meta.data.contract ? `“${meta.data.contract}”` : "no version"}. Update the backend and the app together.
        </Notice>
      </div>
    );
  }
  const m = meta.data;
  const res = state.errors.length ? null : await api<ScreenResponse>(`/api/screener?${apiQuery(state)}`);
  const d = res?.ok ? res.data : null;
  const data = d?.data ?? null;
  const usedKeys = new Set([...state.rows.map((r) => r.metric), d?.query.sort ?? ""]);
  const used = m.metrics.filter((x) => usedKeys.has(x.key));

  return (
    <div className="stack">
      <PageHeader
        eyebrow={EYEBROW}
        title={TITLE}
        meta={data
          ? <>{d!.season} season · nflverse data through week {data.latest_week ?? "—"} (xFP through week {data.xfp_through_week ?? "—"}) · full PPR · NFL sync {when(data.nfl_synced_at)}</>
          : "Combine opportunity, role and production filters across every WR, RB and TE."}
      />

      <p className={styles.lede}>
        Every WR, RB and TE in the NFL, not only rostered players. Pick a ready-made screen or build one: each filter compares one
        metric over an exact week range, with the games behind every number. Exploratory lists, not recommendations.
      </p>

      <Section title="Ready-made screens" subtitle="Each opens as an editable screen.">
        <ul className={styles.presets}>
          {m.presets.map((p) => {
            const active = isPreset(state, p.query);
            return (
              <li key={p.key} className={styles.preset}>
                <Link href={presetHref(p.query)} prefetch={false} aria-current={active ? "page" : undefined}
                  className={active ? styles.presetActive : undefined}>{p.title}</Link>
                <span className={styles.presetSummary}>{p.summary}</span>
              </li>
            );
          })}
        </ul>
      </Section>

      <Builder state={state} metrics={m.metrics} limits={m.limits} sp={sp} summary={summaryText(state, m)} />

      {state.errors.length > 0 && (
        <Notice tone="caution" title="This screen cannot run yet">
          <ul className={styles.errors}>{state.errors.map((e) => <li key={e}>{e}</li>)}</ul>
        </Notice>
      )}
      {res && !res.ok && (res.status === 422
        ? <Notice tone="caution" title="This screen cannot run">{res.error}</Notice>
        : <BackendProblem error={res.error} kind={res.kind} status={res.status} />)}

      {d && d.status === "no_data" && <Notice tone="info" title="No NFL usage data yet">{d.message}</Notice>}

      {d && d.status !== "no_data" && d.counts && (
        <>
          <StatStrip compact>
            <Stat label="Matched" value={`${d.counts.matched - d.counts.ownership_excluded}`}
              note={`of ${d.counts.pool} ${plural(d.counts.pool, "player")} with a game in the window`} />
            <Stat label="Undecided" value={String(d.counts.incomplete)} note="missing data, listed below" />
            <Stat label="Window" value={data?.window ? (data.window.from === data.window.to ? `Week ${data.window.from}` : `Weeks ${data.window.from}–${data.window.to}`) : "—"}
              note={d.query.last ? `each player's last ${d.query.last} ${plural(d.query.last, "game")} there` : "every game played"} />
            <Stat label="Ownership unknown" value={String(d.counts.ownership_unknown)}
              note={d.ownership?.available ? "never counted as available" : "no league player pool"} />
          </StatStrip>
          {d.expression && <p className={styles.expression}><span className={styles.label}>Screen</span> {d.expression}</p>}
          {data?.route_data?.note && used.some((x) => x.inputs.includes("routes") || x.inputs.includes("route_participation")) && (
            <Notice tone="info" title="No route data for these weeks">{data.route_data.note}</Notice>
          )}
          <Results d={d} state={state} sp={sp} />

          {d.incomplete.length > 0 && (
            <Section title="Could not be decided" subtitle="A filter's metric (or the route count) is missing for these players, so they are neither matched nor ruled out."
              footnote={d.incomplete_truncated ? `Showing ${d.incomplete.length} of ${d.counts.incomplete}.` : undefined}>
              <ul className={styles.undecided}>
                {d.incomplete.map((r) => (
                  <li key={r.gsis_id}>
                    <b>{r.name}</b> <span className="faint">{r.position} · {r.team ?? "—"} · {r.games} {plural(r.games, "game")}</span>
                    {" "}missing {r.missing.map((k) => m.metrics.find((x) => x.key === k)?.short ?? k).join(", ")}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title="Metrics in this screen" subtitle="Unit, denominator, source and the last week with data in the window." flush>
            <div className="table-wrap">
              <table className={styles.metricTable}>
                <thead>
                  <tr><th scope="col">Metric</th><th scope="col">Unit</th><th scope="col">Denominator</th><th scope="col">Source</th><th scope="col" className="num">Data through</th></tr>
                </thead>
                <tbody>
                  {(used.length ? used : m.metrics.filter((x) => x.key === "xfp_pg")).map((x) => {
                    const cut = data?.cutoffs?.[x.key];
                    return (
                      <tr key={x.key}>
                        <td><b>{x.label}</b> <span className="faint">({x.short}; {x.positions.join(", ")})</span></td>
                        <td>{x.unit_label}</td>
                        <td>{x.denominator}</td>
                        <td>{x.source}</td>
                        <td className="num">{cut ? `Week ${cut}` : <span className="missing" title="No game in the window has this metric's inputs">None</span>}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Section>
        </>
      )}

      <Glossary terms={["xFP/g", "PPR/g", "Target share", "Snap share", "Backfield share", "—"]} />
      <Method title="How the screener works">
        <p>{m.limits.precedence} {m.limits.missing} {m.limits.rounding}</p>
        <p>
          A game counts toward a metric only when every input of that metric is known for that game. Shares are the player&apos;s
          total over the team&apos;s total in the same games, not an average of weekly shares; snap share and route participation are the
          mean of the weekly values. A week without a game (bye, inactive) is not a game.
        </p>
        <p>
          Route participation and routes come only from an authorized external utilization import and are never estimated from snaps.
          Percentiles rank a player within his own position among players in the same window who meet the minimum games, and are left out
          when fewer than {m.limits.percentile_min_pool} players have the metric.
        </p>
        <p>
          League ownership comes from this league&apos;s own player pool through a verified one-to-one player link
          {d?.ownership?.rosters_synced_at ? <> (rosters synced {when(d.ownership.rosters_synced_at)})</> : null}. A player without one is
          &ldquo;Roster status unknown&rdquo; and is never treated as available. At most {m.limits.max_filters} filters,{" "}
          {m.limits.max_limit} rows per screen.
        </p>
        <ul className={styles.metricDefs}>
          {m.metrics.map((x) => <li key={x.key}><b>{x.label}</b> ({x.unit_label}): {x.definition}</li>)}
        </ul>
      </Method>
    </div>
  );
}
