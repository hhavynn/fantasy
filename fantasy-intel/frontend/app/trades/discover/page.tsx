import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { PlayerLink } from "@/components/PlayerLink";
import { TradeVsFreeAgency } from "@/components/TradeVsFreeAgency";
import {
  Badge, Delta, EmptyState, Method, Notice, PageHeader, Pos, Section, SortableTh, Stat, StatStrip, Tabs, VisuallyHidden,
} from "@/components/ui";
import { api } from "@/lib/api";
import { num, signed } from "@/lib/format";
import { nextDir, sortHref, sortRows } from "@/lib/sort";
import { isApiSort, resolveSort, sortValue } from "@/lib/trade-discovery";
import { signedText } from "@/lib/trade-format";
import type { DResult, DiscoveryV3, NeedRow } from "@/lib/trade-types";
import type { TradeDeltas } from "@/lib/types";
import styles from "./discover.module.css";

export const metadata = { title: "Discover trades" };

// Trade Discovery. Every number is the exact weekly optimal-lineup simulation on ESPN's projections
// (the manual analyzer's computation). Modes decide which exact trades are shown and how they are
// explained; asset balance is a separate diagnostic. No trade grade, no acceptance probability.

const MODE_LABEL: Record<string, string> = {
  practical: "Practical trades", mutual: "Mutual improvement", upside: "Maximum upside", all: "All candidates",
};
const MODE_HINT: Record<string, string> = { practical: " (default)", upside: " · exploratory", all: " · advanced" };
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
  ["my_gain", "Your Δ"], ["their_gain", "Their Δ"], ["min_side", "Smaller side"], ["asset", "Asset balance"], ["partner", "Partner"], ["shape", "Shape"],
];
const why = "Incomplete: a rostered player with an NFL team has no ESPN projection in some week (never treated as 0)";

type Params = Record<string, string | string[] | undefined>;

function one(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

function many(v: string | string[] | undefined): string[] {
  return v === undefined ? [] : Array.isArray(v) ? v : [v];
}

const label = (k: string) => k.toLowerCase().replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());

export default async function DiscoverPage({ searchParams }: PageProps<"/trades/discover">) {
  const sp = await searchParams;
  const q = new URLSearchParams();
  const pass = (k: string, apiKey = k) => { const v = one(sp[k]); if (v) q.set(apiKey, v); };
  pass("team", "team_id");
  pass("mode");
  pass("phase");
  pass("strictness");
  pass("opponent");
  pass("category");
  pass("limit");
  // The API only takes its own sort keys, and only "All candidates" uses them (to choose which rows survive the limit).
  const sort = one(sp.sort);
  if (isApiSort(sort)) q.set("sort", sort);
  if (one(sp.kdst) === "1") q.set("include_kdst", "true");
  if (one(sp.exact) === "1") q.set("heuristic", "false");
  const shapes = many(sp.shape);
  if (shapes.length) q.set("shapes", shapes.join(","));
  const res = await api<DiscoveryV3>(`/api/trades/discover?${q.toString()}`);
  return (
    <div className="stack">
      {!res.ok ? (
        <>
          <PageHeader title="Trade discovery" actions={<Link href="/trades">Trade analyzer</Link>} />
          <BackendProblem error={res.error} kind={res.kind} status={res.status} />
        </>
      ) : <Body d={res.data} sp={sp} />}
    </div>
  );
}

/** Link to this page with the current query overridden by `over` (undefined removes a key). Sorting is dropped. */
function hrefFor(d: DiscoveryV3, over: Record<string, string | undefined>) {
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

function analyzerHref(r: DResult) {
  const q = new URLSearchParams({ ta: String(r.anchor_team_id), tb: String(r.opponent_team_id) });
  r.sends.forEach((id) => q.append("a", String(id)));
  r.receives.forEach((id) => q.append("b", String(id)));
  return `/trades?${q.toString()}`;
}

function Body({ d, sp }: { d: DiscoveryV3; sp: Params }) {
  const ev = d.evaluation;
  const rows = Object.entries(d.sections).flatMap(([tier, items]) => items.map((r) => ({ tier, r })));
  const total = rows.length;
  const modeHref = (m: string) => hrefFor(d, { mode: m });
  const excluded = d.excluded.lopsided + d.excluded.one_sided;
  return (
    <>
      <PageHeader
        title="Trade discovery"
        meta={<>Projected points: <b>ESPN</b> · Trade impact: <b>exact lineup simulation</b> for both teams · weeks {ev.start_week}–{ev.final_week}
          {ev.last_regular_season_week !== null && <> (regular season through {ev.last_regular_season_week})</>}</>}
        actions={<Link href="/trades">Trade analyzer</Link>}
      >
        <Tabs label="Discovery mode" current={modeHref(d.query.mode)}
          items={Object.keys(d.modes).map((m) => ({ href: modeHref(m), label: `${MODE_LABEL[m] ?? m}${MODE_HINT[m] ?? ""}` }))} />
      </PageHeader>

      <p className={styles.modeNote}>{d.mode_text}</p>
      {ev.uncertain && ev.warning && <Notice tone="caution">{ev.warning}</Notice>}

      <Filters d={d} />
      <Coverage d={d} />

      <Results d={d} rows={rows} total={total} excluded={excluded} sp={sp} />

      <Managers d={d} />

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
        <ul className={styles.list}>{d.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        <ul className={styles.list}>{d.limitations.map((a, i) => <li key={i} className="faint">{a}</li>)}</ul>
      </Method>
    </>
  );
}

// ---------------------------------------------------------------------------------- filters --

function Filters({ d }: { d: DiscoveryV3 }) {
  const qy = d.query;
  const allMode = qy.mode === "all";
  return (
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
          <legend>Trade shape (you give – you get)</legend>
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
          <Link href={`/trades/discover?mode=${qy.mode}`}>Reset</Link>
        </div>
      </div>

      <details className={styles.more}>
        <summary>More options</summary>
        <div className={styles.moreBody}>
          <div className={styles.row}>
            <label className={styles.field}>Team
              <select name="team" defaultValue={qy.team_id}>
                {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
              </select>
            </label>
            {qy.mode !== "upside" && qy.mode !== "all" && (
              <label className={styles.field}>Phase
                <select name="phase" defaultValue={qy.phase}>
                  {d.phases.map((p) => <option key={p} value={p}>{PHASE_LABEL[p] ?? p}</option>)}
                </select>
              </label>
            )}
            {qy.mode === "practical" && (
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
            Shapes are from {d.anchor.name}&apos;s side: 3-for-1 = you send three, receive one.
          </p>
        </div>
      </details>
    </form>
  );
}

function Coverage({ d }: { d: DiscoveryV3 }) {
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

function Players({ ids, d }: { ids: number[]; d: DiscoveryV3 }) {
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

function DeltaCell({ v }: { v: TradeDeltas }) {
  return (
    <>
      {v.ros_delta === null
        ? <span title={why}>incomplete ({signedText(v.known_ros_delta)} known)</span>
        : <Delta value={v.ros_delta} />}
      <span className={`${styles.subline} ${styles.nowrap}`}>
        reg {v.regular_season_delta === null ? <span className="missing" title={why}>—</span> : signed(v.regular_season_delta)}
        {" · "}playoffs {v.playoff_delta === null ? <span className="missing" title={why}>—</span> : signed(v.playoff_delta)}
      </span>
    </>
  );
}

function Results({ d, rows, total, excluded, sp }: {
  d: DiscoveryV3; rows: { tier: string; r: DResult }[]; total: number; excluded: number; sp: Params;
}) {
  const href = hrefFor;
  const sort = resolveSort(sp, d.query.mode);
  const indexed = rows.map((x, i) => ({ ...x, rank: i }));
  const sorted = sortRows(indexed, (x) => sortValue(x.r, sort.key, x.rank), sort.dir);
  const th = (key: string, text: string, opts: { align?: "left" | "right"; title?: string; defaultDir?: "asc" | "desc" } = {}) => (
    <SortableTh sortKey={key} label={text} current={sort} searchParams={sp} align={opts.align} title={opts.title} defaultDir={opts.defaultDir} />
  );
  const sortNote = d.query.mode === "all"
    ? "Sorting by a gain column in All candidates mode chooses which trades are returned first, then orders them."
    : "Sorting reorders the trades shown; it does not search for others.";

  return (
    <Section
      title={`Trades for ${d.anchor.name}`}
      actions={<span className="faint">{total} {total === 1 ? "trade" : "trades"}</span>}
      flush={total > 0}
      footnote={total > 0 ? <>
        {sortNote}
        {d.query.mode === "practical" && excluded > 0 && <>
          {" "}Left out of practical results: {d.excluded.lopsided} with a lopsided asset package against the other manager and {d.excluded.one_sided} where
          one lineup does not improve. They remain in <Link href={href(d, { mode: "upside" })}>Maximum upside</Link> and{" "}
          <Link href={href(d, { mode: "all" })}>All candidates</Link>, and any trade can be opened in the analyzer.
        </>}
      </> : undefined}
    >
      {total === 0 ? (
        <EmptyState title="No trade matches these settings"
          action={d.query.mode === "practical" && excluded > 0 ? <Link href={href(d, { mode: "upside" })}>Try Maximum upside</Link> : undefined}>
          No trade matches these settings{d.query.opponent !== null ? " with this manager" : ""}.
          {d.query.mode === "practical" && excluded > 0 && <>
            {" "}{d.excluded.lopsided} lopsided and {d.excluded.one_sided} one-sided candidates were left out.</>}
          {" "}Nothing is manufactured to fill the list.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <nav className={styles.phoneSort} aria-label="Sort trades">
            <span>Sort by</span>
            {PHONE_SORTS.map(([key, text]) => {
              const active = sort.key === key;
              const dir = nextDir(sort, key, key === "shape" || key === "partner" ? "asc" : "desc");
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
                <th scope="col">You give</th>
                <th scope="col">You get</th>
                {th("my_gain", "Your Δ", { align: "right", title: "Change in your projected starting points, rest of season" })}
                {th("their_gain", "Their Δ", { align: "right", title: "Change in their projected starting points, rest of season" })}
                {th("min_side", "Smaller side", { align: "right", title: "The smaller of the two rest-of-season deltas" })}
                {th("asset", "Asset balance", { title: "Smaller package as a share of the larger, in points above replacement" })}
                <th scope="col">Type</th>
                <th scope="col"><VisuallyHidden>Open trade in analyzer</VisuallyHidden></th>
              </tr>
            </thead>
            {sorted.map(({ tier, r }) => <TradeRows key={r.key} tier={tier} r={r} d={d} />)}
          </table>
        </div>
      )}
    </Section>
  );
}

function TradeRows({ tier, r, d }: { tier: string; r: DResult; d: DiscoveryV3 }) {
  const b = r.asset_balance;
  const concerns = r.rationale.concerns;
  const mc = r.manager_context;
  return (
    <tbody>
      <tr className={styles.mainRow}>
        <td data-label="Shape">{r.shape}</td>
        <td className={styles.partner} data-label="Partner">
          <Link href={`/managers/${r.opponent_team_id}`}>{r.opponent_name}</Link>
          {mc && <span className={styles.subline}>{mc.trade_history.completed_trades} completed trades · {mc.trade_history.confidence.toLowerCase()} confidence</span>}
        </td>
        <td className={styles.wrapCell} data-label="You give"><Players ids={r.sends} d={d} /></td>
        <td className={styles.wrapCell} data-label="You get"><Players ids={r.receives} d={d} /></td>
        <td className="num" data-label="Your Δ"><DeltaCell v={r.mine} /></td>
        <td className="num" data-label="Their Δ"><DeltaCell v={r.theirs} /></td>
        <td className="num" data-label="Smaller side">{r.min_side_ros_delta === null ? <span className="missing" title={why}>—</span> : <Delta value={r.min_side_ros_delta} />}</td>
        <td className={styles.assetCell} data-label="Asset balance">
          <span title={b.note}><Badge value={b.text} tone={ASSET_TONE[b.label]} /></span>
          {b.you_send_par !== null && <span className={styles.subline}>you give {b.you_send_par.toFixed(0)} · they give {b.they_send_par?.toFixed(0)} pts above replacement</span>}
        </td>
        <td className={styles.typeCell} data-label="Type">
          <div className={styles.tags}>
            {TIER_SHORT[tier] && <Badge value={TIER_SHORT[tier]} tone={TIER_TONE[tier] ?? ""} title={d.tiers[tier]} />}
            {concerns.length > 0 && <Badge value={`${concerns.length} concern${concerns.length === 1 ? "" : "s"}`} tone="warn" />}
            {r.categories.map((c) => <Badge key={c} value={label(c)} tone="" title={d.categories[c]} />)}
            {!r.consistent_with_manual_evaluator && <Badge value="analyzer mismatch" tone="bad" />}
          </div>
          <span className={styles.subline}>{r.rationale.trade_type.text}</span>
        </td>
        <td className={styles.action}><Link href={analyzerHref(r)}>Analyze →</Link></td>
      </tr>
      <tr className={styles.detailRow}>
        <td colSpan={10} className={styles.detailCell}>
          <details className={styles.detail}>
            <summary>Why this trade, concerns and evidence</summary>
            <div className={styles.detailBody}>
              <div className={styles.tags}>
                <Badge value={r.search_method === "exact" ? "exact search" : "heuristic search"} tone={r.search_method === "exact" ? "info" : ""}
                  title={r.search_method === "exact" ? "From the proven exact search" : "From the bounded beam search (the row itself is exact)"} />
              </div>
              {concerns.length > 0 && (
                <Notice tone="caution" title="Concerns">
                  <ul className={styles.list}>{concerns.map((c, i) => <li key={i}>{c}</li>)}</ul>
                </Notice>
              )}
              {r.rationale.points.length > 0 && <ul className={styles.list}>{r.rationale.points.map((p, i) => <li key={i}>{p}</li>)}</ul>}
              <TradeVsFreeAgency t={r.trade_vs_free_agency} only="team_a" />
              <AssetTable r={r} />
              <div className={styles.cols}>
                {[{ s: r.mine_detail, t: `${r.mine_detail.team_name} (you)` }, { s: r.theirs_detail, t: r.theirs_detail.team_name ?? "Them" }].map(({ s, t }) => (
                  <div key={s.team_id}>
                    <h4>{t}</h4>
                    <ul className={styles.list}>{s.fit_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
                  </div>
                ))}
              </div>
              {r.injury_scenarios.length > 0 && (
                <p className={styles.small}>Injury return scenarios (earliest return, delayed, none) are in the analyzer: <Link href={analyzerHref(r)}>open</Link>.</p>
              )}
              {r.manager_context && (
                <p className={styles.mgrNote}>{r.opponent_name}: {r.manager_context.trade_history.completed_trades} completed trades
                  ({r.manager_context.trade_history.confidence.toLowerCase()} confidence). {r.manager_context.note}</p>
              )}
              {r.need_notes.map((n, i) => <p key={i} className={styles.mgrNote}>{n}</p>)}
            </div>
          </details>
        </td>
      </tr>
    </tbody>
  );
}

function AssetTable({ r }: { r: DResult }) {
  const rows = [...r.asset_balance.you_send.map((p) => ({ p, who: "you send" })), ...r.asset_balance.they_send.map((p) => ({ p, who: "they send" }))];
  return (
    <div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Player</th><th>Side</th>
              <th className="num" title="ESPN points above the best available free agent at his position, over his NFL-eligible weeks">Pts above repl.</th>
              <th className="num">Repl. / wk</th><th>Position rank</th><th>Notes</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ p, who }) => (
              <tr key={p.espn_player_id}>
                <td>{p.name} <Pos value={p.position} /></td>
                <td className="faint">{who}</td>
                <td className="num">{p.par === null ? <span title="Missing projections">incomplete ({p.known_par.toFixed(0)} known)</span> : p.par.toFixed(1)}</td>
                <td className="num faint">{num(p.replacement_per_week, 1)}</td>
                <td>{p.position_rank ? <>{p.position}{p.position_rank} of {p.position_rank_of} {p.starter_caliber && <Badge value="starter-caliber" tone="info" title={`Within the league's ${p.league_starters_at_position} starters at ${p.position}`} />}</> : "—"}</td>
                <td className="faint wrap">{[p.replacement_source.join("; "), p.nfl_ineligible_weeks.length ? `NFL-ineligible weeks ${p.nfl_ineligible_weeks.join(", ")} not counted` : "",
                  p.eligibility_unknown ? "NFL eligibility unknown" : "", p.return_unconfirmed ? "return not confirmed" : ""].filter(Boolean).join(" · ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {r.asset_balance.caveats.length > 0 && <ul className={styles.list}>{r.asset_balance.caveats.map((c, i) => <li key={i} className="faint">{c}</li>)}</ul>}
    </div>
  );
}

// ---------------------------------------------------------------------------------- managers --

function needText(needs: Record<string, NeedRow>) {
  const short = Object.entries(needs).filter(([, n]) => n.short).map(([p, n]) => `${p} (${n.league_rank}/${n.teams})`);
  const unknown = Object.entries(needs).filter(([, n]) => n.incomplete).map(([p]) => p);
  return (short.length ? short.join(", ") : "none below the league median")
    + (unknown.length ? ` · unknown (missing projections): ${unknown.join(", ")}` : "");
}

function Managers({ d }: { d: DiscoveryV3 }) {
  const nm = (ids: number[]) => ids.map((id) => d.names[id]?.name ?? `#${id}`).join(" + ");
  return (
    <Section title="By manager" flush
      footnote={<>Your weaker starting positions (below the league median of projected points per slot): <b>{needText(d.my_needs)}</b>. A position need is a roster fact, not a willingness to trade.</>}>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Manager</th><th>Their weaker positions</th>
              <th>Best {d.query.mode === "practical" ? "practical " : ""}trade in this view</th>
              <th className="num">You / them</th><th><VisuallyHidden>Filter to this manager</VisuallyHidden></th>
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
