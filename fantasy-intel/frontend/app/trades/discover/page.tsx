import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Callout, ErrorBox, Pos } from "@/components/ui";
import { api } from "@/lib/api";
import { signed } from "@/lib/format";
import type { DResult, DiscoveryV3, NeedRow } from "@/lib/trade-types";
import type { TradeDeltas } from "@/lib/types";

// Trade Discovery. Every number is the exact weekly optimal-lineup simulation on ESPN's projections
// (the manual analyzer's computation). Modes decide which exact trades are shown and how they are
// explained; asset balance is a separate diagnostic. No trade grade, no acceptance probability.

const MODE_LABEL: Record<string, string> = {
  practical: "Practical trades", mutual: "Mutual improvement", upside: "Maximum upside", all: "All candidates",
};
const PHASE_LABEL: Record<string, string> = { ros: "Rest of season", regular: "Regular season", playoff: "Fantasy playoffs" };
const SORT_LABEL: Record<string, string> = {
  my_gain: "My projected gain", their_gain: "Their projected gain", combined: "Combined gain", min_side: "Smaller-side gain",
  playoff_gain: "My playoff gain", min_side_regular: "Smaller-side regular-season gain", min_side_playoff: "Smaller-side playoff gain",
};
const ASSET_TONE: Record<string, string> = {
  balanced: "good", replacement_level: "", they_give_more: "warn", you_give_more: "warn",
  significant_they_give_more: "bad", significant_you_give_more: "bad", they_surrender_multiple_strong: "bad",
  you_surrender_multiple_strong: "bad", insufficient_data: "warn",
};
const TIER_TONE: Record<string, string> = { practical: "good", concerns: "warn", speculative: "warn" };
const why = "Incomplete: a rostered player with an NFL team has no ESPN projection in some week (never treated as 0)";
const GRID2 = { gridTemplateColumns: "repeat(auto-fit, minmax(min(380px, 100%), 1fr))" };

function one(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

function many(v: string | string[] | undefined): string[] {
  return v === undefined ? [] : Array.isArray(v) ? v : [v];
}

export default async function DiscoverPage({ searchParams }: PageProps<"/trades/discover">) {
  const sp = await searchParams;
  const q = new URLSearchParams();
  const pass = (k: string, apiKey = k) => { const v = one(sp[k]); if (v) q.set(apiKey, v); };
  pass("team", "team_id");
  pass("mode");
  pass("phase");
  pass("strictness");
  pass("opponent");
  pass("sort");
  pass("category");
  pass("limit");
  if (one(sp.kdst) === "1") q.set("include_kdst", "true");
  if (one(sp.exact) === "1") q.set("heuristic", "false");
  const shapes = many(sp.shape);
  if (shapes.length) q.set("shapes", shapes.join(","));
  const res = await api<DiscoveryV3>(`/api/trades/discover?${q.toString()}`);
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Trade discovery</h1>
        <span className="sub">
          Projected points: <b>ESPN</b> · Trade impact: <b>exact lineup simulation</b> for both teams · <Link href="/trades">trade analyzer</Link>
        </span>
      </div>
      {!res.ok ? <ErrorBox error={res.error} /> : <Body d={res.data} />}
    </div>
  );
}

function hrefFor(d: DiscoveryV3, over: Record<string, string | undefined>) {
  const base: Record<string, string | undefined> = {
    team: String(d.query.team_id), mode: d.query.mode, phase: d.query.phase, strictness: d.query.strictness,
    opponent: d.query.opponent === null ? undefined : String(d.query.opponent),
    kdst: d.query.include_kdst ? "1" : undefined, exact: d.query.heuristic ? undefined : "1", ...over,
  };
  const p = new URLSearchParams();
  Object.entries(base).forEach(([k, v]) => { if (v) p.set(k, v); });
  return `/trades/discover?${p.toString()}`;
}

function analyzerHref(r: DResult) {
  const q = new URLSearchParams({ ta: String(r.anchor_team_id), tb: String(r.opponent_team_id) });
  r.sends.forEach((id) => q.append("a", String(id)));
  r.receives.forEach((id) => q.append("b", String(id)));
  return `/trades?${q.toString()}`;
}

function Body({ d }: { d: DiscoveryV3 }) {
  const ev = d.evaluation;
  const sections = Object.entries(d.sections);
  const total = sections.reduce((n, [, v]) => n + v.length, 0);
  return (
    <>
      <p className="faint" style={{ margin: 0 }}>
        Weeks {ev.start_week}–{ev.final_week}{ev.last_regular_season_week !== null && <> (regular season through {ev.last_regular_season_week})</>}.
        {" "}{ev.start_week_reason}
      </p>
      {ev.uncertain && ev.warning && <Callout>{ev.warning}</Callout>}

      <nav className="filters" aria-label="Discovery mode">
        {Object.keys(d.modes).map((m) => (
          <Link key={m} className={`chip ${d.query.mode === m ? "active" : ""}`} href={hrefFor(d, { mode: m })} title={d.modes[m]}
            aria-current={d.query.mode === m ? "page" : undefined}>
            {MODE_LABEL[m] ?? m}{m === "practical" ? " (default)" : m === "upside" ? " · exploratory" : m === "all" ? " · advanced" : ""}
          </Link>
        ))}
      </nav>
      <p className="faint" style={{ margin: 0 }}>{d.mode_text}</p>

      <Controls d={d} />
      <Coverage d={d} />

      {total === 0 && (
        <Callout>
          No trade matches these settings{d.query.opponent !== null ? " with this manager" : ""}.
          {d.query.mode === "practical" && (d.excluded.lopsided + d.excluded.one_sided) > 0 && <>
            {" "}{d.excluded.lopsided} lopsided and {d.excluded.one_sided} one-sided candidates were left out; see{" "}
            <Link href={hrefFor(d, { mode: "upside" })}>Maximum upside</Link>.</>}
          {" "}Nothing is manufactured to fill the list.
        </Callout>
      )}
      {sections.map(([tier, rows]) => rows.length > 0 && (
        <section key={tier} className="panel">
          <h2>{d.tiers[tier] ?? tier} <span className="faint" style={{ fontSize: 13 }}>({rows.length})</span></h2>
          <div className="stack">{rows.map((r) => <TradeCard key={r.key} r={r} d={d} />)}</div>
        </section>
      ))}
      {d.query.mode === "practical" && (d.excluded.lopsided + d.excluded.one_sided) > 0 && total > 0 && (
        <p className="faint">
          Left out of practical results: {d.excluded.lopsided} with a lopsided asset package against the other manager and{" "}
          {d.excluded.one_sided} where one lineup does not improve. They remain in{" "}
          <Link href={hrefFor(d, { mode: "upside" })}>Maximum upside</Link> and <Link href={hrefFor(d, { mode: "all" })}>All candidates</Link>,
          and any trade can be opened in the analyzer.
        </p>
      )}

      <Managers d={d} />

      <details className="panel method">
        <summary>Method, assumptions and limitations</summary>
        <p className="faint">{d.note}</p>
        <p className="faint">{d.asset_note} Thresholds: balanced when the smaller package is at least{" "}
          {Math.round(d.asset_thresholds.balanced_ratio * 100)}% of the larger; significant imbalance below{" "}
          {Math.round(d.asset_thresholds.significant_ratio * 100)}%; replacement-level when both packages are under{" "}
          {d.asset_thresholds.replacement_per_week} point per week above replacement.</p>
        <p className="faint">{d.kdst_policy}</p>
        <p className="faint">{d.availability_note}</p>
        <ul className="reasons">{d.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        <ul className="reasons">{d.limitations.map((a, i) => <li key={i} className="faint">{a}</li>)}</ul>
      </details>
    </>
  );
}

function Controls({ d }: { d: DiscoveryV3 }) {
  const qy = d.query;
  return (
    <form action="/trades/discover" className="panel stack">
      <input type="hidden" name="mode" value={qy.mode} />
      <div className="row-flex">
        <label>Team <select name="team" defaultValue={qy.team_id}>
          {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
        </select></label>
        <label>Opponent <select name="opponent" defaultValue={qy.opponent ?? ""}>
          <option value="">All managers</option>
          {d.teams.filter((t) => t.team_id !== qy.team_id).map((t) => <option key={t.team_id} value={t.team_id}>{t.name}</option>)}
        </select></label>
        {qy.mode !== "upside" && qy.mode !== "all" && <label>Phase <select name="phase" defaultValue={qy.phase}>
          {d.phases.map((p) => <option key={p} value={p}>{PHASE_LABEL[p] ?? p}</option>)}
        </select></label>}
        {qy.mode === "practical" && <label title="How strictly asset imbalance against the other manager filters practical results">
          Asset-balance filter <select name="strictness" defaultValue={qy.strictness}>
            {Object.entries(d.strictness_options).map(([k, v]) => <option key={k} value={k} title={v}>{k}</option>)}
          </select></label>}
        {qy.mode === "all" && <>
          <label>Sort <select name="sort" defaultValue={qy.sort}>
            {d.sorts.map((k) => <option key={k} value={k}>{SORT_LABEL[k] ?? k}</option>)}
          </select></label>
          <label>Category <select name="category" defaultValue={qy.category ?? ""}>
            <option value="">All</option>
            {Object.keys(d.categories).map((k) => <option key={k} value={k}>{k.toLowerCase().replaceAll("_", " ")}</option>)}
          </select></label>
        </>}
        <label>Show <select name="limit" defaultValue={qy.limit}>
          {[5, 10, 20, 30].map((n) => <option key={n} value={n}>{n}</option>)}
        </select></label>
      </div>
      <div className="row-flex">
        <span className="faint">Positions:</span>
        <span>QB · RB · WR · TE</span>
        <label title="Kickers and team defenses are excluded from automatic discovery by default">
          <input type="checkbox" name="kdst" value="1" defaultChecked={qy.include_kdst} /> include K and D/ST</label>
        <span className="faint">Shapes (your side first):</span>
        {d.exact_shapes.map((x) => (
          <label key={x} title="Exact search: proven top list"><input type="checkbox" name="shape" value={x} defaultChecked={qy.shapes.includes(x)} /> {x}</label>
        ))}
        {d.heuristic_shapes.map((x) => (
          <label key={x} title="Heuristic beam search: exact rows, not a proven top list">
            <input type="checkbox" name="shape" value={x} defaultChecked={qy.shapes.includes(x)} /> {x}*</label>
        ))}
        <label title="Only the exact shapes (proven top lists)"><input type="checkbox" name="exact" value="1" defaultChecked={!qy.heuristic} /> exact search only</label>
        <button type="submit">Search</button>
        <Link href={`/trades/discover?mode=${qy.mode}`}>reset</Link>
      </div>
      <p className="faint" style={{ margin: 0 }}>* larger shapes come from a bounded beam search: every listed trade is evaluated exactly, but the list is not a
        proven top list. Shapes are from {d.anchor.name}&apos;s side: 3-for-1 = you send three, receive one.</p>
    </form>
  );
}

function Coverage({ d }: { d: DiscoveryV3 }) {
  const ex = d.coverage.exact, he = d.coverage.heuristic;
  return (
    <div className="grid grid-4">
      <div className="stat" title={d.pool_rule}><div className="label">Pool for this view</div>
        <div className="value">{d.pool_size.toLocaleString()}</div><div className="faint" style={{ fontSize: 11 }}>{d.pool_rule}</div></div>
      <div className="stat" title={ex?.not_evaluated_reason ?? "Every exact candidate matching the filters was evaluated or proven unable to enter"}>
        <div className="label">Exact search ({d.coverage.exact_shapes.join(", ") || "none"})</div>
        <div className="value" style={{ fontSize: 15 }}>{ex ? (ex.exhaustive ? "proven top list" : "evaluation cap reached") : "—"}</div>
        <div className="faint" style={{ fontSize: 11 }}>{ex ? `${ex.candidates_considered.toLocaleString()} candidates, ${ex.fully_evaluated.toLocaleString()} evaluated exactly` : ""}</div></div>
      <div className="stat" title={he?.note}>
        <div className="label">Larger shapes ({d.coverage.heuristic_shapes.join(", ") || "off"})</div>
        <div className="value" style={{ fontSize: 15 }}>{he ? "heuristic, not exhaustive" : "not searched"}</div>
        <div className="faint" style={{ fontSize: 11 }}>{he ? `${he.packages_evaluated.toLocaleString()} packages evaluated exactly (beam ${he.beam.width}, ${he.beam.seeds} seeds/manager)` : ""}</div></div>
      <div className="stat"><div className="label">Checked against the analyzer</div>
        <div className="value" style={{ fontSize: 15 }}>{d.search.consistency_failures === 0 ? `all ${d.search.consistency_checked} match` : `${d.search.consistency_failures} mismatch`}</div>
        <div className="faint" style={{ fontSize: 11 }}>positions: {(d.coverage.positions ?? ["all"]).join(", ")}</div></div>
    </div>
  );
}

function Players({ ids, d }: { ids: number[]; d: DiscoveryV3 }) {
  return (
    <>
      {ids.map((id, i) => {
        const n = d.names[id];
        return (
          <span key={id}>
            {i > 0 && ", "}
            <PlayerLink id={id} name={n?.name ?? `#${id}`} /> <Pos value={n?.position} />
            {n?.on_ir && <> <Badge value="fantasy IR" tone="bad" /></>}
            {n?.nfl_ineligible_weeks?.length ? <> <Badge value={`NFL-ineligible wk ${n.nfl_ineligible_weeks[0]}–${n.nfl_ineligible_weeks[n.nfl_ineligible_weeks.length - 1]}`} tone="bad" /></> : null}
          </span>
        );
      })}
    </>
  );
}

function D({ v, k }: { v: TradeDeltas; k: "ros_delta" | "regular_season_delta" | "playoff_delta" }) {
  const x = v[k];
  if (x === null) return k === "ros_delta" ? <span title={why}>incomplete ({signed(v.known_ros_delta)} known)</span> : <span className="missing" title={why}>—</span>;
  return <>{signed(x)}</>;
}

function TradeCard({ r, d }: { r: DResult; d: DiscoveryV3 }) {
  const b = r.asset_balance;
  const concerns = r.rationale.concerns;
  return (
    <article className="panel" style={{ background: "var(--panel-2)" }}>
      <div className="row-flex" style={{ justifyContent: "space-between" }}>
        <div className="row-flex">
          <b>{r.shape}</b> with <Link href={`/managers/${r.opponent_team_id}`}>{r.opponent_name}</Link>
          <Badge value={r.search_method === "exact" ? "exact search" : "heuristic search"} tone={r.search_method === "exact" ? "info" : ""}
            title={r.search_method === "exact" ? "From the proven exact search" : "From the bounded beam search (the row itself is exact)"} />
          {r.tier && TIER_TONE[r.tier] && <Badge value={r.tier} tone={TIER_TONE[r.tier]} />}
          {!r.consistent_with_manual_evaluator && <Badge value="analyzer mismatch" tone="bad" />}
        </div>
        <Link href={analyzerHref(r)}>open in analyzer →</Link>
      </div>
      <div style={{ marginTop: 6 }}>You send <Players ids={r.sends} d={d} /></div>
      <div>You get <Players ids={r.receives} d={d} /></div>
      {concerns.length > 0 && (
        <div className="callout" style={{ marginTop: 8 }}>
          <b>Concerns:</b>
          <ul className="reasons" style={{ margin: "4px 0 0" }}>{concerns.map((c, i) => <li key={i}>{c}</li>)}</ul>
        </div>
      )}
      <div className="grid grid-4" style={{ marginTop: 8 }}>
        <div className="stat"><div className="label">Your lineup (ROS)</div><div className="value"><D v={r.mine} k="ros_delta" /></div>
          <div className="faint" style={{ fontSize: 11 }}>reg <D v={r.mine} k="regular_season_delta" /> · playoffs <D v={r.mine} k="playoff_delta" /></div></div>
        <div className="stat"><div className="label">Their lineup (ROS)</div><div className="value"><D v={r.theirs} k="ros_delta" /></div>
          <div className="faint" style={{ fontSize: 11 }}>reg <D v={r.theirs} k="regular_season_delta" /> · playoffs <D v={r.theirs} k="playoff_delta" /></div></div>
        <div className="stat" title={b.note}><div className="label">Asset balance</div>
          <div style={{ marginTop: 4 }}><Badge value={b.text} tone={ASSET_TONE[b.label]} /></div>
          {b.you_send_par !== null && <div className="faint" style={{ fontSize: 11 }}>you give {b.you_send_par.toFixed(0)} · they give {b.they_send_par?.toFixed(0)} pts above replacement</div>}</div>
        <div className="stat"><div className="label">Trade type</div><div style={{ marginTop: 4 }}>{r.rationale.trade_type.text}</div></div>
      </div>
      {r.rationale.points.length > 0 && <ul className="reasons" style={{ marginTop: 8 }}>{r.rationale.points.map((p, i) => <li key={i}>{p}</li>)}</ul>}
      <details style={{ marginTop: 6 }}>
        <summary>Evidence: asset profiles, lineup changes, manager context</summary>
        <AssetTable r={r} />
        <div className="grid" style={GRID2}>
          {[{ s: r.mine_detail, t: `${r.mine_detail.team_name} (you)` }, { s: r.theirs_detail, t: r.theirs_detail.team_name ?? "Them" }].map(({ s, t }) => (
            <div key={s.team_id}>
              <h3 style={{ margin: "6px 0 2px" }}>{t}</h3>
              <ul className="reasons">{s.fit_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
            </div>
          ))}
        </div>
        {r.injury_scenarios.length > 0 && <p className="faint">Injury return scenarios (earliest return, delayed, none) are in the analyzer: <Link href={analyzerHref(r)}>open</Link>.</p>}
        {r.manager_context && <p className="faint">{r.opponent_name}: {r.manager_context.trade_history.completed_trades} completed trades
          ({r.manager_context.trade_history.confidence.toLowerCase()} confidence). {r.manager_context.note}</p>}
        {r.need_notes.map((n, i) => <p key={i} className="faint">{n}</p>)}
      </details>
    </article>
  );
}

function AssetTable({ r }: { r: DResult }) {
  const rows = [...r.asset_balance.you_send.map((p) => ({ p, who: "you send" })), ...r.asset_balance.they_send.map((p) => ({ p, who: "they send" }))];
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Player</th><th>Side</th><th className="num" title="ESPN points above the best available free agent at his position, over his NFL-eligible weeks">Pts above repl.</th>
          <th className="num">Repl. / wk</th><th>Position rank</th><th>Notes</th></tr></thead>
        <tbody>
          {rows.map(({ p, who }) => (
            <tr key={p.espn_player_id}>
              <td>{p.name} <Pos value={p.position} /></td><td className="faint">{who}</td>
              <td className="num">{p.par === null ? <span title="Missing projections">incomplete ({p.known_par.toFixed(0)} known)</span> : p.par.toFixed(1)}</td>
              <td className="num faint">{p.replacement_per_week?.toFixed(1) ?? "—"}</td>
              <td>{p.position_rank ? <>{p.position}{p.position_rank} of {p.position_rank_of} {p.starter_caliber && <Badge value="starter-caliber" tone="info" title={`Within the league's ${p.league_starters_at_position} starters at ${p.position}`} />}</> : "—"}</td>
              <td className="faint wrap">{[p.replacement_source.join("; "), p.nfl_ineligible_weeks.length ? `NFL-ineligible weeks ${p.nfl_ineligible_weeks.join(", ")} not counted` : "",
                p.eligibility_unknown ? "NFL eligibility unknown" : "", p.return_unconfirmed ? "return not confirmed" : ""].filter(Boolean).join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {r.asset_balance.caveats.length > 0 && <ul className="reasons">{r.asset_balance.caveats.map((c, i) => <li key={i} className="faint">{c}</li>)}</ul>}
    </div>
  );
}

function needText(needs: Record<string, NeedRow>) {
  const short = Object.entries(needs).filter(([, n]) => n.short).map(([p, n]) => `${p} (${n.league_rank}/${n.teams})`);
  const unknown = Object.entries(needs).filter(([, n]) => n.incomplete).map(([p]) => p);
  return (short.length ? short.join(", ") : "none below the league median")
    + (unknown.length ? ` · unknown (missing projections): ${unknown.join(", ")}` : "");
}

function Managers({ d }: { d: DiscoveryV3 }) {
  const nm = (ids: number[]) => ids.map((id) => d.names[id]?.name ?? `#${id}`).join(" + ");
  return (
    <section className="panel">
      <h2>By manager</h2>
      <p className="faint">Your weaker starting positions (below the league median of projected points per slot): <b>{needText(d.my_needs)}</b>.
        A position need is a roster fact, not a willingness to trade.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Manager</th><th>Their weaker positions</th><th>Best {d.query.mode === "practical" ? "practical " : ""}trade in this view</th><th className="num">You / them</th><th /></tr></thead>
          <tbody>
            {d.opponents.map((o) => (
              <tr key={o.opponent_team_id}>
                <td><Link href={`/managers/${o.opponent_team_id}`}>{o.team_name}</Link></td>
                <td className="faint wrap">{needText(o.needs)}</td>
                <td className="wrap">{o.best ? <>{nm(o.best.sends)} → {nm(o.best.receives)} <span className="faint">{o.best.shape}</span></> : <span className="faint">none in this view</span>}</td>
                <td className="num">{o.best ? <>{signed(o.best.mine.ros_delta)} / {signed(o.best.theirs.ros_delta)}</> : null}</td>
                <td><Link href={hrefFor(d, { opponent: String(o.opponent_team_id) })}>filter</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
