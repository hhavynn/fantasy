import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Callout, ErrorBox, Pos } from "@/components/ui";
import { api } from "@/lib/api";
import { signed } from "@/lib/format";
import type { Discovery, DiscoveryDeltas, DiscoveryResult, DiscoveryRow, DiscoverySide } from "@/lib/types";

// Trade opportunity discovery. Every number is the exact weekly optimal-lineup simulation on ESPN's
// projections (the manual evaluator's computation). No trade grade, no acceptance probability.

const SORT_LABEL: Record<string, string> = {
  my_gain: "My projected gain", their_gain: "Their projected gain", combined: "Combined gain",
  min_side: "Smaller-side gain", playoff_gain: "My playoff gain",
};
const CATEGORY_LABEL: Record<string, string> = {
  MUTUAL_PROJECTED_IMPROVEMENT: "Mutual improvement",
  IMPROVES_MINE_COSTS_THEIRS: "Helps me, costs them",
  IMPROVES_MINE_THEIRS_UNCHANGED: "Helps me, theirs unchanged",
  CONSOLIDATION: "Consolidation",
  PLAYOFF_FOCUSED: "Playoff-focused",
  REGULAR_SEASON_FOCUSED: "Regular-season-focused",
};
const CATEGORY_TONE: Record<string, string> = {
  MUTUAL_PROJECTED_IMPROVEMENT: "good", IMPROVES_MINE_COSTS_THEIRS: "warn", IMPROVES_MINE_THEIRS_UNCHANGED: "info",
  CONSOLIDATION: "info", PLAYOFF_FOCUSED: "info", REGULAR_SEASON_FOCUSED: "info",
};
const why = "Incomplete: a rostered player with an NFL team has no ESPN projection in some week (never treated as 0)";

function one(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

function many(v: string | string[] | undefined): string[] {
  return v === undefined ? [] : Array.isArray(v) ? v : [v];
}

type Names = Discovery["names"];

function Players({ ids, names }: { ids: number[]; names: Names }) {
  return (
    <>
      {ids.map((id, i) => (
        <span key={id}>
          {i > 0 && ", "}
          <PlayerLink id={id} name={names[id]?.name ?? `#${id}`} /> <Pos value={names[id]?.position} />
          {names[id]?.on_ir && <> <Badge value="IR" tone="bad" /></>}
        </span>
      ))}
    </>
  );
}

function Delta({ d, field }: { d: DiscoveryDeltas; field: "ros_delta" | "regular_season_delta" | "playoff_delta" }) {
  const v = d[field];
  if (v === null) {
    return field === "ros_delta"
      ? <span title={why}>incomplete ({signed(d.known_ros_delta)} known)</span>
      : <span className="missing" title={why}>—</span>;
  }
  return signed(v);
}

function evaluatorHref(r: DiscoveryRow) {
  const q = new URLSearchParams({ ta: String(r.anchor_team_id), tb: String(r.opponent_team_id) });
  r.sends.forEach((id) => q.append("a", String(id)));
  r.receives.forEach((id) => q.append("b", String(id)));
  return `/trades?${q.toString()}`;
}

export default async function DiscoverPage({ searchParams }: PageProps<"/trades/discover">) {
  const sp = await searchParams;
  const shapes = many(sp.shape);
  const q = new URLSearchParams();
  const team = one(sp.team);
  if (team) q.set("team_id", team);
  if (shapes.length) q.set("shapes", shapes.join(","));
  const sort = one(sp.sort) ?? "my_gain";
  q.set("sort", sort);
  const category = one(sp.category);
  if (category) q.set("category", category);
  const opponent = one(sp.opponent);
  if (opponent) q.set("opponent", opponent);
  const equivalents = one(sp.equivalents) === "1";
  if (equivalents) q.set("include_equivalents", "true");
  const res = await api<Discovery>(`/api/trades/discover?${q.toString()}`);

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Trade discovery</h1>
        <span className="sub">
          Projected points: <b>ESPN</b> · Trade impact: <b>exact lineup simulation</b> for both teams ·{" "}
          <Link href="/trades">manual evaluator</Link>
        </span>
      </div>
      {!res.ok ? <ErrorBox error={res.error} /> : <Body d={res.data} sort={sort} category={category} opponent={opponent}
        equivalents={equivalents} />}
    </div>
  );
}

function Body({ d, sort, category, opponent, equivalents }: {
  d: Discovery; sort: string; category?: string; opponent?: string; equivalents: boolean;
}) {
  const s = d.search;
  const ev = d.evaluation;
  const link = (over: Record<string, string | undefined>) => {
    const p = new URLSearchParams();
    const base: Record<string, string | undefined> = { team: String(d.query.team_id), sort, category, opponent,
      equivalents: equivalents ? "1" : undefined, ...over };
    Object.entries(base).forEach(([k, v]) => { if (v) p.set(k, v); });
    d.query.shapes.forEach((x) => p.append("shape", x));
    return `/trades/discover?${p.toString()}`;
  };
  return (
    <>
      <p className="faint" style={{ margin: 0 }}>
        Weeks {ev.start_week}–{ev.final_week}
        {ev.last_regular_season_week !== null && <> (regular season through {ev.last_regular_season_week})</>}. {ev.start_week_reason}
      </p>
      {ev.uncertain && ev.warning && <Callout>{ev.warning}</Callout>}

      <form action="/trades/discover" className="panel stack">
        <div className="row-flex">
          <label>Team <select name="team" defaultValue={d.query.team_id}>
            {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
          </select></label>
          <label>Sort <select name="sort" defaultValue={sort}>
            {d.sorts.map((k) => <option key={k} value={k}>{SORT_LABEL[k] ?? k}</option>)}
          </select></label>
          <label>Category <select name="category" defaultValue={category ?? ""}>
            <option value="">All</option>
            {Object.keys(d.categories).map((k) => <option key={k} value={k}>{CATEGORY_LABEL[k] ?? k}</option>)}
          </select></label>
          <label>Opponent <select name="opponent" defaultValue={opponent ?? ""}>
            <option value="">All managers</option>
            {d.teams.filter((t) => t.team_id !== d.query.team_id).map((t) => <option key={t.team_id} value={t.team_id}>{t.name}</option>)}
          </select></label>
          {d.shapes.map((x) => (
            <label key={x}><input type="checkbox" name="shape" value={x} defaultChecked={d.query.shapes.includes(x)} /> {x}</label>
          ))}
          <label title="Two-player trades whose 1-for-1 sub-trade has identical deltas for both teams">
            <input type="checkbox" name="equivalents" value="1" defaultChecked={equivalents} /> show simpler-trade equivalents</label>
          <button type="submit">Search</button>
          <Link href="/trades/discover">reset</Link>
        </div>
        <p className="faint" style={{ margin: 0 }}>
          Shapes are from {d.anchor.name}&apos;s side: 2-for-1 = you send two, receive one. Orderings describe projected lineup
          results only; nothing here estimates whether a manager would accept.
        </p>
      </form>

      <div className="filters">
        <Link className={`chip ${!category ? "active" : ""}`} href={link({ category: undefined })}>All</Link>
        {Object.keys(d.categories).map((k) => (
          <Link key={k} className={`chip ${category === k ? "active" : ""}`} href={link({ category: k })} title={d.categories[k]}>
            {CATEGORY_LABEL[k] ?? k}</Link>
        ))}
      </div>

      <SearchStats s={s} limit={d.query.limit} />

      <Opponents d={d} link={link} />

      {d.results.length === 0 ? (
        <Callout>
          No candidate trade {category ? <>in <b>{CATEGORY_LABEL[category] ?? category}</b> </> : ""}
          {opponent ? "with this manager " : ""}projects a positive rest-of-season or playoff gain for {d.anchor.name}.
          Nothing is manufactured to fill the list. {s.excluded_missing_data > 0 && <>{s.excluded_missing_data} candidate(s) could not be judged because ESPN
          projections are missing.</>}
        </Callout>
      ) : (
        <Results d={d} />
      )}

      <section className="panel">
        <h2>Assumptions &amp; limitations</h2>
        <p className="faint">{d.note}</p>
        <ul className="reasons">{d.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        <ul className="reasons">{d.limitations.map((a, i) => <li key={i} className="faint">{a}</li>)}</ul>
      </section>
    </>
  );
}

function SearchStats({ s, limit }: { s: Discovery["search"]; limit: number }) {
  const n = (x: number) => x.toLocaleString();
  return (
    <div className="grid grid-4">
      <div className="stat"><div className="label">Candidates considered</div><div className="value">{n(s.candidates_considered)}</div></div>
      <div className="stat" title="Upper bound proves the trade cannot raise your rest-of-season or playoff starting total">
        <div className="label">Proven unable to help you</div><div className="value">{n(s.screened_out_by_upper_bound)}</div></div>
      <div className="stat"><div className="label">Fully evaluated</div><div className="value">{n(s.fully_evaluated)}</div></div>
      <div className="stat" title={s.exhaustive ? "Every unevaluated candidate's upper bound is below the shown results" : s.not_evaluated_reason ?? ""}>
        <div className="label">Coverage</div>
        <div className="value" style={{ fontSize: 15 }}>{s.exhaustive ? `exact top ${limit}` : "bounded (cap reached)"}</div></div>
      <p className="faint" style={{ gridColumn: "1 / -1", margin: 0 }}>
        {s.excluded_invalid_roster > 0 && <>{s.excluded_invalid_roster} excluded: no legal roster. </>}
        {s.excluded_missing_data > 0 && <>{s.excluded_missing_data} excluded: missing projections. </>}
        {s.simpler_equivalents_hidden > 0 && <>{s.simpler_equivalents_hidden} simpler-trade equivalents hidden. </>}
        {s.not_evaluated > 0 && <>{n(s.not_evaluated)} not evaluated: {s.not_evaluated_reason}. </>}
        {!s.bounds_valid && <>A negative projection exists, so no candidate was screened out by bounds. </>}
        {s.consistency_failures > 0
          ? <b>{s.consistency_failures} result(s) differed from the manual evaluator.</b>
          : <>All {s.consistency_checked} shown results re-checked with the manual evaluator.</>}
        {" "}Search {(s.seconds.stage_a + s.seconds.stage_b).toFixed(2)}s.
      </p>
    </div>
  );
}

function Results({ d }: { d: Discovery }) {
  return (
    <section className="panel">
      <h2>Candidate trades</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Trade</th>
              <th className="num" title="Your rest-of-season starting-lineup delta">You ROS</th><th className="num">You REG</th><th className="num">You PO</th>
              <th className="num" title="Their rest-of-season starting-lineup delta">Them ROS</th><th className="num">Them REG</th><th className="num">Them PO</th>
              <th className="num">Combined</th><th>Categories</th><th /></tr>
          </thead>
          {d.results.map((r) => <ResultRows key={r.key} r={r} d={d} />)}
        </table>
      </div>
    </section>
  );
}

function ResultRows({ r, d }: { r: DiscoveryResult; d: Discovery }) {
  return (
    <tbody>
      <tr>
        <td className="wrap">
          <span className="faint">{r.shape} with </span><Link href={`/managers/${r.opponent_team_id}`}>{r.opponent_name}</Link>
          <div>Send <Players ids={r.sends} names={d.names} /></div>
          <div>Get <Players ids={r.receives} names={d.names} /></div>
        </td>
        <td className="num"><b><Delta d={r.mine} field="ros_delta" /></b></td>
        <td className="num"><Delta d={r.mine} field="regular_season_delta" /></td>
        <td className="num"><Delta d={r.mine} field="playoff_delta" /></td>
        <td className="num"><b><Delta d={r.theirs} field="ros_delta" /></b></td>
        <td className="num"><Delta d={r.theirs} field="regular_season_delta" /></td>
        <td className="num"><Delta d={r.theirs} field="playoff_delta" /></td>
        <td className="num">{signed(r.combined_ros_delta)}</td>
        <td>{r.categories.map((c) => <span key={c}><Badge value={CATEGORY_LABEL[c] ?? c} tone={CATEGORY_TONE[c]} title={d.categories[c]} /> </span>)}
          {(r.forced_drops.mine.length > 0 || r.forced_drops.theirs.length > 0) && <Badge value="forced drop" tone="warn" />}
          {r.players.some((p) => p.on_ir) && <> <Badge value="IR player traded" tone="bad" title={r.ir_assumption} /></>}
          {!r.consistent_with_manual_evaluator && <> <Badge value="evaluator mismatch" tone="bad" /></>}</td>
        <td><Link href={evaluatorHref(r)}>evaluate</Link></td>
      </tr>
      <tr>
        <td colSpan={10} className="wrap" style={{ paddingTop: 0 }}>
          {/* width 0 / min-width 100%: the explanation wraps to the table's width instead of widening it */}
          <details style={{ width: 0, minWidth: "100%" }}>
            <summary>Why: lineup changes, roster effects, manager context</summary>
            <div className="grid grid-2">
              <SideExplain s={r.mine_detail} title={`${r.mine_detail.team_name} (you)`} />
              <SideExplain s={r.theirs_detail} title={r.theirs_detail.team_name ?? "Them"} />
            </div>
            {r.ir_warning && <Callout>{r.ir_warning} {r.ir_assumption}</Callout>}
            <ManagerPanel r={r} />
          </details>
        </td>
      </tr>
    </tbody>
  );
}

function SideExplain({ s, title }: { s: DiscoverySide; title: string }) {
  return (
    <div>
      <h3 style={{ margin: "4px 0" }}>{title}</h3>
      <div className="faint">
        Avg / week {signed(s.deltas.average_weekly_delta, 2)} · weeks ▲ {s.deltas.weeks_improved} ▼ {s.deltas.weeks_worsened} = {s.deltas.weeks_unchanged}
      </div>
      <ul className="reasons">{s.fit_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
      <div className="kv" style={{ marginTop: 6 }}>
        <span className="k">Open roster spots</span>
        <span>{s.open_active_roster_spots}{s.open_active_roster_spots !== s.open_active_roster_spots_before ? ` (was ${s.open_active_roster_spots_before})` : ""}</span>
        {s.forced_drops.length > 0 && <>
          <span className="k">Forced drop</span>
          <span>{s.forced_drops.map((x) => x.name ?? `#${x.espn_player_id}`).join(", ")}{" "}
            <span className="faint">lineup cost {s.forced_drop_lineup_cost === null ? "incomplete" : s.forced_drop_lineup_cost.toFixed(1)} (included in the delta)</span></span>
        </>}
        {s.hypothetical_fa_fill?.player && <>
          <span className="k">Hypothetical FA fill</span>
          <span>{s.hypothetical_fa_fill.player.name} <span className="faint">{signed(s.hypothetical_fa_fill.lineup_points_added ?? null)} if added (not guaranteed; not in the delta)</span></span>
        </>}
        {s.incomplete.missing_weeks.length > 0 && <>
          <span className="k">Missing projections</span>
          <span className="faint">weeks {s.incomplete.missing_weeks.join(", ")} (no canonical total)</span>
        </>}
      </div>
      {s.redundant_incoming_asset && <p className="faint">Redundant incoming asset: {s.redundant_incoming_asset.names.join(", ")}.</p>}
      {s.ir_activation_sensitivity.players.map((p) => (
        <p key={p.espn_player_id} className="faint">
          IR: {p.name} · activation upside {signed(p.before_trade.lineup_points_upside)} (separate; not in the delta)
        </p>
      ))}
    </div>
  );
}

function ManagerPanel({ r }: { r: DiscoveryResult }) {
  const m = r.manager_context;
  if (!m) return <p className="faint">No stored manager-scouting evidence for {r.opponent_name}.</p>;
  const needs = m.needs.filter((n) => (n.starters_required ?? 0) > 0).slice(0, 4);
  return (
    <div style={{ marginTop: 8 }}>
      <h3 style={{ margin: "4px 0" }}>{r.opponent_name}: manager context (secondary)</h3>
      {r.need_notes.length > 0 && <ul className="reasons">{r.need_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
      <div className="kv">
        <span className="k">Current needs</span>
        <span>{needs.length ? needs.map((n) => `${n.position} ${n.need_score === null ? "—" : n.need_score.toFixed(2)}`).join(" · ") : <span className="faint">none stored</span>}</span>
        <span className="k">Roster (wk {m.roster_scoring_period})</span>
        <span>{m.roster_construction.filter((p) => p.position !== "IR").map((p) => `${p.position} ${p.count}`).join(" · ")}</span>
        <span className="k">Completed trades</span>
        <span>{m.trade_history.completed_trades} <Badge value={m.trade_history.confidence} title={m.trade_history.rule} /> <span className="faint">trade preference: {m.trade_history.behavior.toLowerCase()}</span></span>
      </div>
      {m.headlines.length > 0 && (
        <ul className="reasons">{m.headlines.map((h, i) => (
          <li key={i} title={h.evidence.join(" ")}>{h.text} <span className="faint">({h.category}; {h.confidence.toLowerCase()})</span></li>
        ))}</ul>
      )}
      <p className="faint" style={{ marginBottom: 0 }}>{m.note}</p>
    </div>
  );
}

function Opponents({ d, link }: { d: Discovery; link: (o: Record<string, string | undefined>) => string }) {
  return (
    <section className="panel">
      <h2>By manager</h2>
      <p className="faint">Best exact candidate with each manager. &quot;Best for you&quot; = largest rest-of-season gain for {d.anchor.name};
        &quot;best mutual&quot; = largest smaller-side gain where both lineups improve.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Manager</th><th>Best for you</th><th className="num">You / them</th><th>Best mutual</th><th className="num">You / them</th><th /></tr></thead>
          <tbody>
            {d.opponents.map((o) => (
              <tr key={o.opponent_team_id}>
                <td><Link href={`/managers/${o.opponent_team_id}`}>{o.team_name}</Link></td>
                <td className="wrap">{o.best_my_gain ? <TradeText r={o.best_my_gain} d={d} /> : <span className="faint">none</span>}</td>
                <td className="num">{o.best_my_gain ? <>{signed(o.best_my_gain.mine.ros_delta)} / {signed(o.best_my_gain.theirs.ros_delta)}</> : null}</td>
                <td className="wrap">{o.best_mutual ? <TradeText r={o.best_mutual} d={d} /> : <span className="faint">none</span>}</td>
                <td className="num">{o.best_mutual ? <>{signed(o.best_mutual.mine.ros_delta)} / {signed(o.best_mutual.theirs.ros_delta)}</> : null}</td>
                <td><Link href={link({ opponent: String(o.opponent_team_id) })}>filter</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function TradeText({ r, d }: { r: DiscoveryRow; d: Discovery }) {
  const nm = (ids: number[]) => ids.map((id) => d.names[id]?.name ?? `#${id}`).join(" + ");
  return <><Link href={evaluatorHref(r)}>{nm(r.sends)} → {nm(r.receives)}</Link> <span className="faint">{r.shape}</span></>;
}
