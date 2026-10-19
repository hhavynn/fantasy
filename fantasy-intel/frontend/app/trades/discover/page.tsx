import Link from "next/link";
import { Suspense } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { PlayerLink } from "@/components/PlayerLink";
import {
  Badge, Delta, EmptyState, Glossary, Method, Notice, PageHeader, Pos, Section, Skeleton, SkeletonStatStrip, SkeletonTable, SortableTh, Stat, StatStrip, Tabs, VisuallyHidden,
} from "@/components/ui";
import { api, type ApiResult } from "@/lib/api";
import { signed } from "@/lib/format";
import { nextDir, sortHref, sortRows } from "@/lib/sort";
import { discoverQuery, evidenceSrc, resolveSort, sortValue } from "@/lib/trade-discovery";
import {
  beforeAfterText, favoursText, excludedText, lensTabText, lensTabs, modeUsesPhase, modeUsesStrictness, sectionsAreVerdicts,
} from "@/lib/trade-dual-value";
import { signedText } from "@/lib/trade-format";
import type { DSummaryRow, DVSideSummary, DiscoveryContext, DiscoverySummary, NeedRow } from "@/lib/trade-types";
import type { TradeDeltas } from "@/lib/types";
import { CheckChips, MainConcern, VerdictBadge } from "./DualValueParts";
import { TradeEvidence } from "./TradeEvidence";
import styles from "./discover.module.css";
import { TeamLink } from "@/components/TeamLink";

export const metadata = { title: "Discover trades" };

// Trade Discovery 2.0. Every number is the exact weekly optimal-lineup simulation on ESPN's projections
// (the manual analyzer's computation). A lens (mode) decides which exact trades are shown; each trade is judged
// on two things kept apart, asset quality (points above replacement) and lineup benefit (starters only), plus
// plausibility checks and a verdict that describes how they fit (docs/trade_discovery_dual_value.md).
// No trade grade, no acceptance probability.
//
// The page streams: the header and filters come from the light `/discover/context` call, the search result
// (`/discover/summary`) streams in below them, and each row's evidence loads when its panel opens (TradeEvidence).

const PHASE_LABEL: Record<string, string> = { ros: "Rest of season", regular: "Regular season", playoff: "Fantasy playoffs" };
const ASSET_TONE: Record<string, string> = {
  balanced: "good", replacement_level: "", they_give_more: "warn", you_give_more: "warn",
  significant_they_give_more: "bad", significant_you_give_more: "bad", they_surrender_multiple_strong: "bad",
  you_surrender_multiple_strong: "bad", insufficient_data: "warn",
};
const TIER_TONE: Record<string, string> = { practical: "good", concerns: "warn", speculative: "warn" };
const TIER_SHORT: Record<string, string> = { practical: "Practical", concerns: "Concerns", speculative: "Speculative" };
/** Sort links shown instead of the column headers on phones, where each trade is a card. */
const PHONE_SORTS: [string, string][] = [
  ["verdict", "Verdict"], ["my_gain", "Team Δ"], ["their_gain", "Partner Δ"], ["min_side", "Smaller side"], ["asset", "Asset balance"],
  ["partner", "Partner"], ["shape", "Shape"],
];
const why = "Incomplete: a rostered player with an NFL team has no ESPN projection in some week (never treated as 0)";

type Params = Record<string, string | string[] | undefined>;

const label = (k: string) => k.toLowerCase().replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());

export default async function DiscoverPage({ searchParams }: PageProps<"/trades/discover">) {
  const sp = await searchParams;
  const q = discoverQuery(sp).toString();
  // Start the search now; the header only waits for the light context call, the results stream in below it.
  const summary = api<DiscoverySummary>(`/api/trades/discover/summary?${q}`);
  const res = await api<DiscoveryContext>(`/api/trades/discover/context?${q}`);
  return (
    <div className="stack">
      {!res.ok ? (
        <>
          <PageHeader title="Trade discovery" actions={<TeamLink href="/trades">Trade analyzer</TeamLink>} />
          <BackendProblem error={res.error} kind={res.kind} status={res.status} />
        </>
      ) : <Body c={res.data} summary={summary} q={q} sp={sp} />}
    </div>
  );
}

/** Link to this page with the current query overridden by `over` (undefined removes a key). Sorting is dropped. */
function hrefFor(d: Pick<DiscoveryContext, "query">, over: Record<string, string | undefined>) {
  const base: Record<string, string | undefined> = {
    team: String(d.query.team_id), mode: d.query.mode, phase: d.query.phase, strictness: d.query.strictness,
    opponent: d.query.opponent === null ? undefined : String(d.query.opponent),
    kdst: d.query.include_kdst ? "1" : undefined, exact: d.query.heuristic ? undefined : "1",
    limit: String(d.query.limit), ...over,
  };
  const p = new URLSearchParams();
  Object.entries(base).forEach(([k, v]) => { if (v) p.set(k, v); });
  d.query.shapes.forEach((s) => p.append("shape", s));
  return `/trades/discover?${p.toString()}`;
}

function analyzerHref(r: DSummaryRow) {
  const q = new URLSearchParams({ ta: String(r.anchor_team_id), tb: String(r.opponent_team_id) });
  r.sends.forEach((id) => q.append("a", String(id)));
  r.receives.forEach((id) => q.append("b", String(id)));
  return `/trades?${q.toString()}`;
}

function Body({ c: d, summary, q, sp }: { c: DiscoveryContext; summary: Promise<ApiResult<DiscoverySummary>>; q: string; sp: Params }) {
  const ev = d.evaluation;
  const modeHref = (m: string) => hrefFor(d, { mode: m });
  const tabs = lensTabs(d.query.mode);
  return (
    <>
      <PageHeader
        eyebrow="Trades"
        title="Trade discovery"
        meta={<>Projected points: <b>ESPN</b> · Trade impact: <b>exact lineup simulation</b> for both teams · weeks {ev.start_week}–{ev.final_week}
          {ev.last_regular_season_week !== null && <> (regular season through {ev.last_regular_season_week})</>}</>}
        actions={<TeamLink href="/trades">Trade analyzer</TeamLink>}
      >
        <Tabs label="Discovery lens" current={modeHref(d.query.mode)}
          items={tabs.map((t) => ({ href: modeHref(t.mode), label: lensTabText(t) }))} />
      </PageHeader>

      <p className={styles.modeNote}>{d.mode_text}</p>
      {ev.uncertain && ev.warning && <Notice tone="caution">{ev.warning}</Notice>}

      <Filters d={d} />

      <Suspense key={q} fallback={<ResultsSkeleton />}>
        <Discovered summary={summary} q={q} sp={sp} />
      </Suspense>
    </>
  );
}

/** Stands in for the coverage strip and the results table while the search runs (as in loading.tsx). */
function ResultsSkeleton() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Searching for trades…</VisuallyHidden>
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={8} cols={7} />
      </Section>
    </div>
  );
}

/** Everything that needs the search: coverage, the trades, the per-manager view and the method notes. */
async function Discovered({ summary, q, sp }: { summary: Promise<ApiResult<DiscoverySummary>>; q: string; sp: Params }) {
  const res = await summary;
  if (!res.ok) return <BackendProblem error={res.error} kind={res.kind} status={res.status} />;
  const d = res.data;
  const ev = d.evaluation;
  const rows = Object.entries(d.sections).flatMap(([tier, items]) => items.map((r) => ({ tier, r })));
  const total = rows.length;
  const excluded = (d.excluded.lopsided ?? 0) + (d.excluded.one_sided ?? 0);
  return (
    <>
      {d.search.consistency_failures > 0 && (
        <Notice tone="negative" title={`${d.search.consistency_failures} of ${d.search.consistency_checked} rows disagree with the trade analyzer`}>
          Re-run the analytics refresh; the search coverage below has the details.
        </Notice>
      )}
      {d.lens_note && <Notice tone="info">{d.lens_note}</Notice>}

      <Results d={d} rows={rows} total={total} excluded={excluded} q={q} sp={sp} />

      <Managers d={d} />

      <Method title="About this search: pool, coverage and checks">
        <Coverage d={d} />
      </Method>

      <Glossary terms={["Lineup points", "ROS", "Projected points", "—"]} />
      <Method title="Method, assumptions and limitations">
        <p>{ev.start_week_reason}</p>
        <p>{d.note}</p>
        <p>
          {d.asset_note} Thresholds: balanced when the smaller package is at least {Math.round(d.asset_thresholds.balanced_ratio * 100)}% of the
          larger; significant imbalance below {Math.round(d.asset_thresholds.significant_ratio * 100)}%; replacement-level when both packages are
          under {d.asset_thresholds.replacement_per_week} point per week above replacement.
        </p>
        <p>{d.kdst_policy}</p>
        <p>{d.availability_note}</p>
        <MethodDualValue d={d} />
        <ul className={styles.list}>{d.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        <ul className={styles.list}>{d.limitations.map((a, i) => <li key={i} className="faint">{a}</li>)}</ul>
      </Method>
    </>
  );
}

// ---------------------------------------------------------------------------------- filters --

function Filters({ d }: { d: DiscoveryContext }) {
  const qy = d.query;
  const allMode = qy.mode === "all";
  return (
    <details className="filter-box">
    <summary>
      <span className="filter-box-title">Filters</span>
      <span className="filter-box-summary">
        {qy.opponent === null ? "All managers" : d.teams.find((t) => t.team_id === qy.opponent)?.name ?? "One manager"} · {qy.shapes.join(", ") || "No shapes"}
      </span>
    </summary>
    <form action="/trades/discover" className={`panel ${styles.filters}`} aria-label="Discovery filters">
      <input type="hidden" name="mode" value={qy.mode} />
      <div className={styles.row}>
        <label className={styles.field}>Partner
          <select name="opponent" defaultValue={qy.opponent ?? ""}>
            <option value="">All managers</option>
            {d.teams.filter((t) => t.team_id !== qy.team_id).map((t) => <option key={t.team_id} value={t.team_id}>{t.name}</option>)}
          </select>
        </label>
        <fieldset className={styles.group}>
          <legend>Trade shape (gives – gets)</legend>
          <div className={styles.shapes}>
            {d.exact_shapes.map((x) => (
              <label key={x} className={styles.shape} title="Exact search: proven top list">
                <input type="checkbox" name="shape" value={x} defaultChecked={qy.shapes.includes(x)} /><span>{x}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <label className={styles.field}>Category
          <select name="category" defaultValue={qy.category ?? ""} disabled={!allMode}
            title={allMode ? undefined : "Categories filter the All candidates view"}>
            <option value="">All</option>
            {Object.keys(d.categories).map((k) => <option key={k} value={k} title={d.categories[k]}>{label(k)}</option>)}
          </select>
          {!allMode && <span className={styles.fieldHint}>All candidates view only</span>}
        </label>
        <div className={styles.actions}>
          <button type="submit">Apply filters</button>
          <TeamLink href={`/trades/discover?mode=${qy.mode}`}>Reset</TeamLink>
        </div>
      </div>

      <details className={styles.more}>
        <summary>More options</summary>
        <div className={styles.moreBody}>
          <div className={styles.row}>
            <label className={styles.field}>Team
              <select name="team" defaultValue={qy.team_id}>
                {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (default)" : ""}</option>)}
              </select>
            </label>
            {modeUsesPhase(qy.mode) && (
              <label className={styles.field}>Phase
                <select name="phase" defaultValue={qy.phase}>
                  {d.phases.map((p) => <option key={p} value={p}>{PHASE_LABEL[p] ?? p}</option>)}
                </select>
              </label>
            )}
            {modeUsesStrictness(qy.mode) && (
              <label className={styles.field} title="How strictly asset imbalance against the other manager filters practical results">Asset-balance filter
                <select name="strictness" defaultValue={qy.strictness}>
                  {Object.entries(d.strictness_options).map(([k, v]) => <option key={k} value={k} title={v}>{k}</option>)}
                </select>
              </label>
            )}
            <label className={styles.field}>Show
              <select name="limit" defaultValue={qy.limit}>
                {[5, 10, 20, 30].map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
          </div>
          <div className={styles.row}>
            <span className="faint">Positions: QB · RB · WR · TE</span>
            <label className={styles.check} title="Kickers and team defenses are excluded from automatic discovery by default">
              <input type="checkbox" name="kdst" value="1" defaultChecked={qy.include_kdst} /> Include K and D/ST
            </label>
            <label className={styles.check} title="Only the exact shapes (proven top lists)">
              <input type="checkbox" name="exact" value="1" defaultChecked={!qy.heuristic} /> Exact search only
            </label>
          </div>
          <fieldset className={styles.group}>
            <legend>Larger shapes (heuristic beam search)</legend>
            <div className={styles.shapes}>
              {d.heuristic_shapes.map((x) => (
                <label key={x} className={styles.shape} title="Heuristic beam search: exact rows, not a proven top list">
                  <input type="checkbox" name="shape" value={x} defaultChecked={qy.shapes.includes(x)} /><span>{x}*</span>
                </label>
              ))}
            </div>
          </fieldset>
          <p className={styles.footnote}>
            * Larger shapes come from a bounded beam search: every listed trade is evaluated exactly, but the list is not a proven top list.
            Shapes are from {d.anchor.name}&apos;s side: 3-for-1 = {d.anchor.name} sends three, receives one.
          </p>
        </div>
      </details>
    </form>
    </details>
  );
}

function Coverage({ d }: { d: DiscoverySummary }) {
  const ex = d.coverage.exact, he = d.coverage.heuristic;
  return (
    <StatStrip>
      <Stat label={<span title={d.pool_rule}>Pool for this view</span>} value={d.pool_size.toLocaleString()} note={d.pool_rule} />
      <Stat
        label={<span title={ex?.not_evaluated_reason ?? "Every exact candidate matching the filters was evaluated or proven unable to enter"}>Exact search ({d.coverage.exact_shapes.join(", ") || "none"})</span>}
        value={ex ? (ex.exhaustive ? "Proven" : "Cap reached") : "—"}
        note={ex ? `${ex.exhaustive ? "proven top list · " : "evaluation cap reached · "}${ex.candidates_considered.toLocaleString()} candidates, ${ex.fully_evaluated.toLocaleString()} evaluated exactly` : undefined}
      />
      <Stat
        label={<span title={he?.note}>Larger shapes ({d.coverage.heuristic_shapes.join(", ") || "off"})</span>}
        value={he ? "Heuristic" : "Off"}
        note={he ? `not exhaustive · ${he.packages_evaluated.toLocaleString()} packages evaluated exactly (beam ${he.beam.width}, ${he.beam.seeds} seeds/manager)` : "not searched"}
      />
      <Stat
        label="Checked against the analyzer"
        value={d.search.consistency_failures === 0 ? `${d.search.consistency_checked}/${d.search.consistency_checked}` : `${d.search.consistency_failures} off`}
        note={`${d.search.consistency_failures === 0 ? `all ${d.search.consistency_checked} match` : `${d.search.consistency_failures} mismatch`} · positions: ${(d.coverage.positions ?? ["all"]).join(", ")}`}
        tone={d.search.consistency_failures === 0 ? undefined : "negative"}
      />
    </StatStrip>
  );
}

// ----------------------------------------------------------------------------------- results --

function Players({ ids, d }: { ids: number[]; d: DiscoverySummary }) {
  return (
    <ul className={styles.players}>
      {ids.map((id) => {
        const n = d.names[id];
        return (
          <li key={id}>
            <PlayerLink id={id} name={n?.name ?? `#${id}`} /> <Pos value={n?.position} />
            {n?.on_ir && <Badge value="fantasy IR" tone="bad" />}
            {n?.nfl_ineligible_weeks?.length ? <Badge value={`NFL-ineligible wk ${n.nfl_ineligible_weeks[0]}–${n.nfl_ineligible_weeks[n.nfl_ineligible_weeks.length - 1]}`} tone="bad" /> : null}
          </li>
        );
      })}
    </ul>
  );
}

/** The same players seen from the opponent's side: plain names under the list, so the row says it once without repeating the badges. */
function SeenFromThem({ label, ids, d }: { label: string; ids: number[]; d: DiscoverySummary }) {
  return <span className={styles.subline}><b>{label}</b> {ids.map((id) => d.names[id]?.name ?? `#${id}`).join(", ")}</span>;
}

function DeltaCell({ v, side }: { v: TradeDeltas; side?: DVSideSummary }) {
  return (
    <>
      {v.ros_delta === null
        ? <span title={why}>incomplete ({signedText(v.known_ros_delta)} known)</span>
        : <Delta value={v.ros_delta} />}
      <span className={styles.subline}>
        reg {v.regular_season_delta === null ? <span className="missing" title={why}>—</span> : signed(v.regular_season_delta)}
        {" · "}playoffs {v.playoff_delta === null ? <span className="missing" title={why}>—</span> : signed(v.playoff_delta)}
      </span>
      {side && (
        <span className={styles.subline} title="Projected rest-of-season starting points before → after the trade (starters only)">
          starters {beforeAfterText(side.before.ros, side.after.ros)}
        </span>
      )}
    </>
  );
}

function Results({ d, rows, total, excluded, q, sp }: {
  d: DiscoverySummary; rows: { tier: string; r: DSummaryRow }[]; total: number; excluded: number; q: string; sp: Params;
}) {
  const href = hrefFor;
  const sort = resolveSort(sp, d.query.mode);
  const indexed = rows.map((x, i) => ({ ...x, rank: i }));
  const sorted = sortRows(indexed, (x) => sortValue(x.r, sort.key, x.rank), sort.dir);
  const verdictGroups = sectionsAreVerdicts(Object.keys(d.sections), d.verdicts);
  // The verdict groups are the backend's own section order; any other sort mixes them, so each row then carries its verdict.
  const grouped = verdictGroups && sort.key === "rank" && sort.dir === "asc";
  const counts = new Map<string, number>(Object.entries(d.sections).map(([k, items]) => [k, items.length]));
  const th = (key: string, text: string, opts: { align?: "left" | "right"; title?: string; defaultDir?: "asc" | "desc" } = {}) => (
    <SortableTh sortKey={key} label={text} current={sort} searchParams={sp} align={opts.align} title={opts.title} defaultDir={opts.defaultDir} />
  );
  const sortNote = d.query.mode === "all"
    ? "Sorting by a gain column in All candidates mode chooses which trades are returned first, then orders them."
    : "Sorting reorders the trades shown; it does not search for others.";
  const left = excludedText(d.excluded);

  return (
    <Section
      title={`Trades for ${d.anchor.name}`}
      actions={<span className="faint">{total} {total === 1 ? "trade" : "trades"}</span>}
      flush={total > 0}
      footnote={total > 0 ? <>
        {sortNote}
        {verdictGroups && !grouped && <> The verdict groups are shown in the default order; here every trade carries its own verdict.</>}
        {d.query.mode === "practical" && excluded > 0 && <>
          {" "}Left out of practical results: {d.excluded.lopsided} with a lopsided asset package against the other manager and {d.excluded.one_sided} where
          one lineup does not improve. They remain in <Link href={href(d, { mode: "upside" })}>Maximum upside</Link> and{" "}
          <Link href={href(d, { mode: "all" })}>All candidates</Link>, and any trade can be opened in the analyzer.
        </>}
        {d.query.mode !== "practical" && left && <>
          {" "}Left out of this view: {left}. Any trade can still be opened in the analyzer.
        </>}
      </> : undefined}
    >
      {total === 0 ? (
        <EmptyState title="No trade matches these settings"
          action={d.query.mode === "practical" && excluded > 0 ? <Link href={href(d, { mode: "upside" })}>Try Maximum upside</Link> : undefined}>
          No trade matches these settings{d.query.opponent !== null ? " with this manager" : ""}.
          {d.query.mode === "practical" && excluded > 0 && <>
            {" "}{d.excluded.lopsided} lopsided and {d.excluded.one_sided} one-sided candidates were left out.</>}
          {d.query.mode !== "practical" && left && <>{" "}Left out of this view: {left}.</>}
          {" "}Nothing is manufactured to fill the list.
        </EmptyState>
      ) : (
        <div className="table-wrap flat">
          <nav className={styles.phoneSort} aria-label="Sort trades">
            <span>Sort by</span>
            {PHONE_SORTS.map(([key, text]) => {
              const active = sort.key === key;
              const dir = nextDir(sort, key, key === "shape" || key === "partner" || key === "verdict" ? "asc" : "desc");
              return (
                <Link key={key} href={sortHref(sp, key, dir)} scroll={false} prefetch={false} aria-current={active ? "true" : undefined}>
                  {text}{active ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
                </Link>
              );
            })}
          </nav>
          <table className={styles.table}>
            <thead>
              <tr>
                {th("shape", "Shape")}
                {th("partner", "Partner")}
                <th scope="col" title="What the team sends, and what the partner gets">Gives</th>
                <th scope="col" title="What the team receives, and what the partner gives">Gets</th>
                {th("asset", "Asset balance", { title: "Smaller package as a share of the larger, in points above replacement; who the imbalance favours" })}
                {th("my_gain", "Team Δ", { align: "right", title: "Change in the analyzed team's projected starting points, rest of season" })}
                {th("their_gain", "Partner Δ", { align: "right", title: "Change in the trade partner's projected starting points, rest of season" })}
                {th("min_side", "Smaller side", { align: "right", title: "The smaller of the two rest-of-season deltas" })}
                <th scope="col" title="The most important change to each starting lineup">Lineup change</th>
                {th("verdict", "Verdict & checks", { title: "Sorted in the backend's verdict order, best first", defaultDir: "asc" })}
              </tr>
            </thead>
            {sorted.map(({ tier, r }, i) => (
              <TradeGroup key={r.key} header={grouped && (i === 0 || sorted[i - 1].tier !== tier) ? { tier, count: counts.get(tier) ?? 0 } : null}
                tier={tier} r={r} d={d} q={q} verdictGroups={verdictGroups} />
            ))}
          </table>
        </div>
      )}
    </Section>
  );
}

/** An optional verdict-group header (the backend's section and its description) followed by one trade's rows. */
function TradeGroup({ header, tier, r, d, q, verdictGroups }: {
  header: { tier: string; count: number } | null; tier: string; r: DSummaryRow; d: DiscoverySummary; q: string; verdictGroups: boolean;
}) {
  const text = header ? d.verdicts?.[header.tier] ?? d.tiers?.[header.tier] : undefined;
  return (
    <>
      {header && (
        <tbody className={styles.groupBody}>
          <tr className={styles.groupRow}>
            <th scope="colgroup" colSpan={10}>
              <span className={styles.groupTitle}>
                <VerdictBadge verdict={header.tier} text={text} />
                <span className="faint">{header.count} {header.count === 1 ? "trade" : "trades"}</span>
              </span>
              {text && <span className={styles.groupText}>{text}</span>}
            </th>
          </tr>
        </tbody>
      )}
      <TradeRows tier={tier} r={r} d={d} q={q} verdictGroups={verdictGroups} />
    </>
  );
}

function TradeRows({ tier, r, d, q, verdictGroups }: { tier: string; r: DSummaryRow; d: DiscoverySummary; q: string; verdictGroups: boolean }) {
  const b = r.asset_balance;
  const concerns = r.rationale.concerns;
  const mc = r.manager_context;
  const dv = r.dual_value_summary ?? null;      // absent on an older backend or a legacy row: the classic cells still render
  const fav = favoursText(dv?.asset.favours);
  const nm = (ids: number[]) => ids.map((id) => d.names[id]?.name ?? `#${id}`).join(" + ");
  return (
    <tbody>
      <tr className={styles.mainRow}>
        <td data-label="Shape">{r.shape}</td>
        <td className={styles.partner} data-label="Partner">
          <Link href={`/managers/${r.opponent_team_id}`}>{r.opponent_name}</Link>
          {mc && <span className={styles.subline}>{mc.trade_history.completed_trades} completed trades · {mc.trade_history.confidence.toLowerCase()} confidence</span>}
          <span className={styles.subline}><TeamLink href={analyzerHref(r)} aria-label={`Analyze ${nm(r.sends)} for ${nm(r.receives)} in the trade analyzer`}>Analyze →</TeamLink></span>
        </td>
        <td className={styles.wrapCell} data-label="Gives">
          <Players ids={r.sends} d={d} />
          <SeenFromThem label="Partner gets:" ids={r.sends} d={d} />
        </td>
        <td className={styles.wrapCell} data-label="Gets">
          <Players ids={r.receives} d={d} />
          <SeenFromThem label="Partner gives:" ids={r.receives} d={d} />
        </td>
        <td className={`${styles.assetCell} ${styles.wide}`} data-label="Asset balance">
          <span title={b.note}><Badge value={b.text} tone={ASSET_TONE[b.label]} /></span>
          {b.you_send_par !== null && b.they_send_par !== null
            ? <span className={styles.subline}>gives {b.you_send_par.toFixed(0)} · gets {b.they_send_par.toFixed(0)} pts above replacement</span>
            : <span className={styles.subline}><span className="missing" title="A player's points above replacement are unknown (missing projections), so no total is shown">—</span> points above replacement incomplete</span>}
          {fav && <span className={styles.subline}><b>{fav}</b></span>}
        </td>
        <td className="num" data-label="Team Δ"><DeltaCell v={r.mine} side={dv?.lineup.you} /></td>
        <td className="num" data-label="Partner Δ"><DeltaCell v={r.theirs} side={dv?.lineup.them} /></td>
        <td className="num" data-label="Smaller side">{r.min_side_ros_delta === null ? <span className="missing" title={why}>—</span> : <Delta value={r.min_side_ros_delta} />}</td>
        <td className={`${styles.changeCell} ${styles.wide}`} data-label="Lineup change">
          {dv?.you_change
            ? <>
              {dv.you_change}
              {dv.them_change && <span className={styles.subline}><b>Partner:</b> {dv.them_change}</span>}
            </>
            : <span className="missing" title="No lineup-change summary for this trade (older backend or classic view)">—</span>}
        </td>
        <td className={`${styles.typeCell} ${styles.wide}`} data-label="Verdict & checks">
          <div className={styles.tags}>
            {dv && <VerdictBadge verdict={dv.verdict} text={dv.verdict_text} />}
            {!verdictGroups && TIER_SHORT[tier] && <Badge value={TIER_SHORT[tier]} tone={TIER_TONE[tier] ?? ""} title={d.tiers[tier]} />}
            {!verdictGroups && concerns.length > 0 && <Badge value={`${concerns.length} concern${concerns.length === 1 ? "" : "s"}`} tone="warn" />}
            {r.categories.map((c) => <Badge key={c} value={label(c)} tone="" title={d.categories[c]} />)}
            {!r.consistent_with_manual_evaluator && <Badge value="analyzer mismatch" tone="bad" />}
          </div>
          {dv && <MainConcern text={dv.main_concern} checks={dv.checks} />}
          {dv && <CheckChips checks={dv.checks} criteria={d.criteria} />}
          <span className={styles.subline}>{r.rationale.trade_type.text}</span>
        </td>
      </tr>
      <tr className={styles.detailRow}>
        <td colSpan={10} className={styles.detailCell}>
          <TradeEvidence src={evidenceSrc(q, r.key)} analyzer={analyzerHref(r)} />
        </td>
      </tr>
    </tbody>
  );
}

/** The dual-value method notes: the lens's pool, the ranking policy, the projection source and what is not estimated. */
function MethodDualValue({ d }: { d: DiscoverySummary }) {
  const pool = (d.lens?.pool_rule ?? d.pool_rule).replace(/\.$/, "");
  return (
    <>
      <p>
        <b>{d.lens?.label ?? "This view"}: candidate pool.</b> The pool is drawn from the exact search ({pool}); trades are then assessed and ordered.
        The new ordering is never fed back into the search.
      </p>
      {d.ranking_policy && <p><b>Ranking.</b> {d.ranking_policy}</p>}
      {d.projection_source && <p><b>Projection source.</b> {d.projection_source.note}</p>}
      <p>
        <b>No acceptance estimate.</b> No acceptance probability, grade or chance is estimated. Verdicts and checks describe what the projections and asset
        values show for each manager; nothing here has been calibrated against real accepted or rejected offers.
      </p>
      {d.criteria && (
        <ul className={styles.list}>{Object.entries(d.criteria).map(([k, t]) => <li key={k}><b>{k.replaceAll("_", " ")}:</b> {t}</li>)}</ul>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------------- managers --

function needText(needs: Record<string, NeedRow>) {
  const short = Object.entries(needs).filter(([, n]) => n.short).map(([p, n]) => `${p} (${n.league_rank}/${n.teams})`);
  const unknown = Object.entries(needs).filter(([, n]) => n.incomplete).map(([p]) => p);
  return (short.length ? short.join(", ") : "none below the league median")
    + (unknown.length ? ` · unknown (missing projections): ${unknown.join(", ")}` : "");
}

function Managers({ d }: { d: DiscoverySummary }) {
  const nm = (ids: number[]) => ids.map((id) => d.names[id]?.name ?? `#${id}`).join(" + ");
  return (
    <Section title="By manager" flush
      footnote={<>{d.anchor.name}&apos;s weaker starting positions (below the league median of projected points per slot): <b>{needText(d.my_needs)}</b>. A position need is a roster fact, not a willingness to trade.</>}>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Manager</th><th>Their weaker positions</th>
              <th>Best {d.query.mode === "practical" ? "practical " : ""}trade in this view</th>
              <th className="num" title="Rest-of-season change: analyzed team / partner">Team / partner</th><th><VisuallyHidden>Filter to this manager</VisuallyHidden></th>
            </tr>
          </thead>
          <tbody>
            {d.opponents.map((o) => (
              <tr key={o.opponent_team_id}>
                <td><Link href={`/managers/${o.opponent_team_id}`}>{o.team_name}</Link></td>
                <td className="faint wrap">{needText(o.needs)}</td>
                <td className="wrap">
                  {o.best ? <>{nm(o.best.sends)} → {nm(o.best.receives)} <span className="faint">{o.best.shape}</span></> : <span className="faint">none in this view</span>}
                </td>
                <td className="num">{o.best ? <>{signed(o.best.mine.ros_delta)} / {signed(o.best.theirs.ros_delta)}</> : null}</td>
                <td><Link href={hrefFor(d, { opponent: String(o.opponent_team_id) })}>Filter</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
