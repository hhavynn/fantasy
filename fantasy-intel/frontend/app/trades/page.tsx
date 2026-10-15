import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { TradeVsFreeAgency } from "@/components/TradeVsFreeAgency";
import { Badge, Callout, ErrorBox, Pos, TrendArrow } from "@/components/ui";
import { api } from "@/lib/api";
import { label, num, signed } from "@/lib/format";
import { canEvaluate, initialSelection, selectionKey } from "@/lib/trade-builder";
import type { InjuryScenarios, TradeOptionsV3, TradeResultV3, TradeSideV3 } from "@/lib/trade-types";
import type { IrActivation, LineupWeek } from "@/lib/types";
import { TradeBuilder } from "./TradeBuilder";

export const metadata = { title: "Trade analyzer" };

// Manual Trade Analyzer. Projected points: ESPN (unmodified). Trade impact: our exact weekly
// optimal-lineup simulation for both teams. Any player counts per side up to the configured limit.
// No trade grade, no acceptance probability. Team and player selection is client-side (TradeBuilder);
// the evaluation below is rendered on the server for the URL selection only.

type TradeResult = TradeResultV3;
type TradeSide = TradeSideV3;

const RESULT_TEXT: Record<string, string> = {
  MUTUAL_PROJECTED_GAIN: "Both lineups project to gain",
  BOTH_LOSE: "Both lineups project to lose",
  NO_PROJECTED_CHANGE: "Neither lineup changes",
  INCOMPLETE_DATA: "Incomplete ESPN projections: no canonical total",
};

function ids(v: string | string[] | undefined): number[] {
  const arr = v === undefined ? [] : Array.isArray(v) ? v : [v];
  return arr.map(Number).filter((n) => Number.isFinite(n));
}

function one(v: string | string[] | undefined): number | null {
  const x = Array.isArray(v) ? v[0] : v;
  return x === undefined || x === "" || !Number.isFinite(Number(x)) ? null : Number(x);
}

function resultText(code: string, a: string, b: string) {
  if (RESULT_TEXT[code]) return RESULT_TEXT[code];
  const m = code.match(/^TEAM_A_(\w+)_TEAM_B_(\w+)$/);
  return m ? `${a}: ${m[1].toLowerCase()} · ${b}: ${m[2].toLowerCase()}` : code;
}

function tone(code: string) {
  return code === "MUTUAL_PROJECTED_GAIN" ? "good" : code === "BOTH_LOSE" ? "bad" : code === "INCOMPLETE_DATA" ? "warn" : "info";
}

const NARROW = { gridTemplateColumns: "repeat(auto-fit, minmax(min(420px, 100%), 1fr))" };
const why = "Incomplete: a rostered player with an NFL team has no ESPN projection that week";
const IR_HOLD_NOTE = "Current IR status is held constant for this hypothetical evaluation. This does not predict when a "
  + "player will return. Activation upside is shown separately and may require a roster move.";

export default async function TradesPage({ searchParams }: PageProps<"/trades">) {
  const sp = await searchParams;
  const opts = await api<TradeOptionsV3>("/api/trades/options");
  if (!opts.ok) return <ErrorBox error={opts.error} />;
  const { teams, evaluation } = opts.data;
  const firstWeek = evaluation.start_week;
  if (firstWeek === null || evaluation.remaining_weeks === false) {
    return (
      <div className="stack">
        <div className="page-head"><h1>Trade analyzer</h1></div>
        <Callout>{evaluation.warning ?? "No remaining fantasy scoring periods."}</Callout>
        <p className="faint">{evaluation.start_week_reason.reason}</p>
      </div>
    );
  }
  const maxPerSide = opts.data.max_players_per_side ?? 6;
  const mine = teams.find((t) => t.is_mine)?.team_id ?? null;
  // The URL is the evaluated selection (deep links from Discovery / My Team Value, or Evaluate).
  const initial = initialSelection(teams, { ta: one(sp.ta), tb: one(sp.tb), a: ids(sp.a), b: ids(sp.b), sw: one(sp.sw) }, mine);
  const ready = canEvaluate(initial, maxPerSide);
  const evaluated = ready
    ? await api<TradeResult>("/api/trades/evaluate", { team_a_id: initial.ta, team_b_id: initial.tb, team_a_sends: initial.a,
      team_b_sends: initial.b, start_week: initial.sw })
    : null;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Trade analyzer</h1>
        <span className="sub">
          Projected points source: <b>ESPN</b> · Trade impact: <b>our exact lineup simulation</b> · weeks {firstWeek}–{evaluation.final_week} ·{" "}
          <Link href="/trades/discover">discover trades</Link>
        </span>
      </div>
      <p className="faint">
        Pick any number of players from each roster (up to {maxPerSide} per side). Every remaining week, both teams&apos; optimal lineups are
        rebuilt before and after the trade with the league&apos;s real slots and FLEX rules; roster capacity and ESPN position limits are
        enforced with exact forced drops. Only starters count. Evaluation starts at week {firstWeek}: {evaluation.start_week_reason.reason}
      </p>
      {evaluation.uncertain && evaluation.warning && <Callout>{evaluation.warning}</Callout>}
      <TradeBuilder teams={teams} initial={initial} evaluatedKey={evaluated ? selectionKey(initial) : null} maxPerSide={maxPerSide}
        capacity={opts.data.active_capacity} limits={opts.data.position_limits} firstWeek={firstWeek} finalWeek={evaluation.final_week}>
        {evaluated && !evaluated.ok && <ErrorBox error={evaluated.error} />}
        {evaluated?.ok && <Result r={evaluated.data} />}
      </TradeBuilder>
    </div>
  );
}

function names(r: TradeResult, list: number[]) {
  return list.map((id) => r.names[id]?.name ?? `#${id}`).join(", ");
}

function Result({ r }: { r: TradeResult }) {
  const A = r.team_a, B = r.team_b;
  const an = A.team_name ?? "Team A", bn = B.team_name ?? "Team B";
  return (
    <>
      <section className="panel">
        <h2>Trade · {r.trade.shape} <span className="faint" style={{ fontSize: 13 }}>({r.trade.team_a_sends_count} out, {r.trade.team_b_sends_count} in for {an})</span></h2>
        <div className="grid" style={NARROW}>
          <div><b>{an}</b> sends {names(r, A.sends)}</div>
          <div><b>{bn}</b> sends {names(r, B.sends)}</div>
        </div>
        <div className="row-flex" style={{ marginTop: 10 }}>
          <Badge value={resultText(r.result.label, an, bn)} tone={tone(r.result.label)} />
          <span className="faint">regular season: {resultText(r.result.regular_season_label, an, bn)} · playoffs: {resultText(r.result.playoff_label, an, bn)}</span>
        </div>
        <p className="faint" style={{ marginBottom: 0 }}>
          Combined lineup delta {signed(r.result.combined_ros_delta)} · smaller-side delta {signed(r.result.min_side_ros_delta)}. {r.result.note}
        </p>
        {r.evaluation.start_week_overridden && <p className="faint">Start week overridden: weeks {r.evaluation.weeks.join(", ")}. {r.evaluation.start_week_reason.reason}</p>}
        {r.evaluation.uncertain && r.evaluation.warning && <Callout>{r.evaluation.warning}</Callout>}
        {r.ir_warning && <Callout>{r.ir_warning} {r.ir_assumption ?? IR_HOLD_NOTE}</Callout>}
        {r.injury_warnings.map((w) => (
          <div key={w.espn_player_id} className="callout"><b>{w.name}</b>: {w.warnings.join(" ")}</div>
        ))}
      </section>

      <section className="panel">
        <h2>Projected starting-lineup impact</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Team</th><th className="num">Rest of season</th><th className="num">Regular season</th><th className="num">Playoffs</th>
              <th className="num">Avg / week</th><th className="num">Weeks ▲ / ▼ / =</th><th className="num">ROS before → after</th></tr></thead>
            <tbody>
              {[A, B].map((s) => (
                <tr key={s.team_id}>
                  <td><Link href={`/managers/${s.team_id}`}>{s.team_name}</Link></td>
                  <td className="num"><b>{s.deltas.ros_delta === null ? <span title={why}>incomplete ({signed(s.deltas.known_ros_delta)} known)</span> : signed(s.deltas.ros_delta)}</b></td>
                  <td className="num">{s.deltas.regular_season_delta === null ? <span className="missing" title={why}>—</span> : signed(s.deltas.regular_season_delta)}</td>
                  <td className="num">{s.deltas.playoff_delta === null ? <span className="missing" title={why}>—</span> : signed(s.deltas.playoff_delta)}</td>
                  <td className="num">{signed(s.deltas.average_weekly_delta, 2)}</td>
                  <td className="num">{s.deltas.weeks_improved} / {s.deltas.weeks_worsened} / {s.deltas.weeks_unchanged}</td>
                  <td className="num">{num(s.before.ros_projected_starting_points, 1, why)} → {num(s.after.ros_projected_starting_points, 1, why)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {[A, B].filter((s) => s.incomplete.missing_weeks.length > 0).map((s) => (
          <Callout key={s.team_id}>
            <b>{s.team_name}</b>: no ESPN projection for {names(r, s.incomplete.missing_players)} in week(s) {s.incomplete.missing_weeks.join(", ")}.
            Those weeks have no canonical total; missing is never treated as 0.
          </Callout>
        ))}
      </section>

      <TradeVsFreeAgency t={r.trade_vs_free_agency} />

      <RosterAndStarts r={r} />

      <InjuryScenarioPanel r={r} />

      <section className="panel">
        <h2>Week by week</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Wk</th><th>Phase</th>
              <th className="num">{an} before</th><th className="num">after</th><th className="num">Δ</th><th>In / out</th>
              <th className="num">{bn} before</th><th className="num">after</th><th className="num">Δ</th><th>In / out</th></tr></thead>
            <tbody>
              {A.weekly.map((wa, i) => {
                const wb = B.weekly[i];
                return (
                  <tr key={wa.week}>
                    <td>{wa.week}</td><td className="faint">{wa.phase ?? "?"}</td>
                    {[wa, wb].map((w, j) => (
                      <WeekCells key={j} w={w} r={r} />
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <div className="grid" style={NARROW}>
        {[A, B].map((s) => <SidePanel key={s.team_id} s={s} />)}
      </div>

      <section className="panel">
        <h2>Traded players: role, sustainability and market context</h2>
        <p className="faint">Context only. HOLD is not untouchable, SELL is not must-trade, BUY is not accept-any-price. The trade math above is lineup impact.</p>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Player</th><th>To</th><th className="num">ESPN ROS</th><th className="num">ESPN playoffs</th>
              <th className="num" title="Optimal lineup with him minus without him, on his current team">Lineup value now</th>
              <th>Opportunity</th><th>Role trend</th><th>Production vs xFP</th><th>Market</th><th>Injury</th><th>Context</th></tr></thead>
            <tbody>
              {r.players.map((p) => {
                const i = p.intelligence;
                const mv = p.marginal_value_on_current_team;
                return (
                  <tr key={p.espn_player_id}>
                    <td><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /> <span className="faint">{p.pro_team ?? "FA"}</span></td>
                    <td>{p.to_team_id === A.team_id ? an : bn}</td>
                    <td className="num">{num(i?.ros_projected_points, 1, "Incomplete ESPN coverage")}</td>
                    <td className="num">{num(i?.playoff_projected_points, 1, "Incomplete ESPN coverage")}</td>
                    <td className="num" title={`starts ${mv.weeks_started.length} of ${mv.weeks_expected} weeks`}>
                      {num(mv.roster_marginal_lineup_points, 1, why)} <span className="faint">({mv.weeks_started.length}/{mv.weeks_expected} starts)</span>
                      {mv.ir_activation_sensitivity && <div className="faint">IR · if activated {signed(mv.ir_activation_sensitivity.lineup_points_upside)}</div>}</td>
                    <td><Badge value={i?.opportunity_tier} /></td>
                    <td><TrendArrow label={i?.role_trend} /> <span className="faint">{label(i?.role_trend)}</span></td>
                    <td><Badge value={i?.production_sustainability} /></td>
                    <td><Badge value={i?.market_signal} /></td>
                    <td className="wrap">{p.on_ir && <Badge value="fantasy IR slot" tone="bad" />} <Badge value={p.injury_status && !["ACTIVE", "NORMAL"].includes(p.injury_status) ? p.injury_status : null} title="ESPN designation" />
                      {p.availability && p.availability.nfl_designation_label && <div className="faint">NFL: {p.availability.nfl_designation_label}{p.availability.earliest_eligible_week ? ` · eligible wk ${p.availability.earliest_eligible_week}` : ""}{p.availability.return_confirmed ? " · returned" : ""}</div>}</td>
                    <td className="faint wrap">{i?.context_labels ? [i.context_labels.role, i.context_labels.efficiency, i.context_labels.data_confidence].filter(Boolean).map((x) => label(x)).join(" · ") : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <IrSensitivity r={r} />

      <section className="panel">
        <h2>Hypothetical FA replacement (not in the trade delta)</h2>
        <p className="faint">If the player simply disappeared and his team added the best currently available player who can fill one of his slots. Waiver availability is not guaranteed.</p>
        <div className="table-wrap"><table>
          <thead><tr><th>Outgoing</th><th>Best available replacement</th><th className="num">Lineup pts lost</th><th className="num">Recovered</th><th className="num">Residual loss</th></tr></thead>
          <tbody>
            {r.players.map((p) => {
              const c = p.replacement_context;
              return (
                <tr key={p.espn_player_id}>
                  <td>{p.name} <Pos value={p.position} /></td>
                  <td>{c.replacement ? <><PlayerLink id={c.replacement.espn_player_id} name={c.replacement.name} /> <Pos value={c.replacement.position} /></> : <span className="faint">{p.on_ir ? "current IR: nothing to replace" : "none projected"}</span>}</td>
                  <td className="num">{num(c.lineup_points_lost_without_player, 1, why)}</td>
                  <td className="num">{num(c.lineup_points_recovered, 1, why)}</td>
                  <td className="num">{num(c.residual_loss_vs_original, 1, why)}</td>
                </tr>
              );
            })}
          </tbody>
        </table></div>
      </section>

      <div className="grid" style={NARROW}>
        <section className="panel">
          <h2>Manager context (secondary)</h2>
          {[A, B].map((s) => {
            const m = r.manager_context[s.team_id];
            return (
              <div key={s.team_id} style={{ marginBottom: 8 }}>
                <b>{s.team_name}</b>{" "}
                <span className="faint">top needs: {m?.needs.slice(0, 3).map((n) => `${n.position} ${n.need_score.toFixed(2)}`).join(", ") || "—"}</span>
                {m?.classifications.trade_activity && <div className="faint" style={{ marginTop: 4 }}>
                  Trade activity: <Badge value={m.classifications.trade_activity.label} /></div>}
                <details>
                  <summary className="faint">All manager classifications</summary>
                  <div className="row-flex" style={{ marginTop: 4 }}>
                    {Object.entries(m?.classifications ?? {}).map(([dim, c]) => <span key={dim} className="faint">{label(dim)}: <Badge value={c.label} /></span>)}
                  </div>
                </details>
              </div>
            );
          })}
          <p className="faint">No acceptance probability is computed.</p>
        </section>
        <details className="panel method">
          <summary>Assumptions &amp; limitations</summary>
          <ul className="reasons">{r.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
          <ul className="reasons">{r.limitations.map((a, i) => <li key={i} className="faint">{a}</li>)}</ul>
        </details>
      </div>
    </>
  );
}

function RosterAndStarts({ r }: { r: TradeResult }) {
  const n = r.evaluation.weeks.length;
  return (
    <section className="panel">
      <h2>Roster limits and starting opportunities</h2>
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(420px, 100%), 1fr))" }}>
        {[r.team_a, r.team_b].map((s) => {
          const iss = s.roster.issues_before_drops;
          const gained = s.incoming_usage.filter((u) => u.weeks_started.length > 0);
          return (
            <div key={s.team_id}>
              <h3 style={{ margin: "4px 0" }}>{s.team_name}</h3>
              <ul className="reasons">
                <li>Active roster after the trade, before drops: {iss.active_before_drops} of {iss.active_capacity}
                  {iss.over_capacity_by > 0 ? <> · <b>over capacity by {iss.over_capacity_by}</b></> : " · within capacity"}</li>
                {iss.position_limits.map((x) => <li key={x.position}><b>{x.position}</b>: {x.count_before_drops} rostered, ESPN limit {x.limit} (over by {x.over_by})</li>)}
                {s.roster.forced_drops.length > 0
                  ? <li>Forced drops: {s.roster.forced_drops.map((d) => `${d.name ?? d.espn_player_id} (${d.position}${d.is_incoming ? ", incoming" : ""})`).join(", ")}
                    {" "}<span className="faint">lineup cost {s.roster.forced_drop_lineup_cost === null ? `${s.roster.forced_drop_known_lineup_cost.toFixed(1)} known only` : s.roster.forced_drop_lineup_cost.toFixed(1)}
                    {s.roster.forced_drop_search && <> · {s.roster.forced_drop_search.method}, {s.roster.forced_drop_search.candidate_sets.toLocaleString()} drop sets, {s.roster.forced_drop_search.lineup_solves} lineup solves</>}</span></li>
                  : <li className="faint">No forced drop.</li>}
                {gained.map((u) => <li key={u.espn_player_id}><span className="up">Becomes a starter</span>: {u.name} ({u.position}) starts {u.weeks_started.length} of {n} weeks</li>)}
                {s.incoming_usage.filter((u) => u.weeks_started.length === 0).map((u) => <li key={u.espn_player_id}>{u.name} ({u.position}) <b>would not start</b> in any remaining week</li>)}
                {s.outgoing_usage.filter((u) => u.weeks_started.length > 0).map((u) => <li key={u.espn_player_id}><span className="down">Starter leaves</span>: {u.name} ({u.position}) started {u.weeks_started.length} of {n} weeks</li>)}
                {s.displaced.map((d) => <li key={d.espn_player_id}>{d.name} ({d.position}){d.starts_lost.length > 0 && <> <span className="down">loses {d.starts_lost.length} start(s)</span></>}{d.starts_gained.length > 0 && <> <span className="up">gains {d.starts_gained.length} start(s)</span></>}</li>)}
              </ul>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function scen(v: InjuryScenarios["scenarios"][number]["team_a"]) {
  if (!v) return <span className="missing">—</span>;
  return v.ros_delta === null ? <span title={why}>{signed(v.known_ros_delta)} known</span> : signed(v.ros_delta);
}

function InjuryScenarioPanel({ r }: { r: TradeResult }) {
  if (r.injury_scenarios.length === 0) return null;
  const an = r.team_a.team_name ?? "Team A", bn = r.team_b.team_name ?? "Team B";
  return (
    <section className="panel">
      <h2>Injured players: return scenarios (not in the canonical delta unless marked)</h2>
      <p className="faint">{r.availability_note}</p>
      {r.injury_scenarios.map((p) => (
        <div key={p.espn_player_id} style={{ marginBottom: 10 }}>
          <b>{p.name}</b> <Pos value={p.position} /> <Badge value={p.status.replaceAll("_", " ")} tone="warn" />
          <div className="faint">{p.eligibility_basis ? `${p.eligibility_basis}.` : "No NFL eligibility date is known."} {p.return_confirmed ? "Return confirmed." : "Actual return not confirmed."}</div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Scenario</th><th className="num">{an} ROS</th><th className="num">reg / playoffs</th><th className="num">{bn} ROS</th><th className="num">reg / playoffs</th></tr></thead>
              <tbody>
                {p.scenarios.map((x) => (
                  <tr key={x.scenario}>
                    <td className="wrap">{x.description}{x.canonical && <> <Badge value="canonical" tone="info" title="The scenario the deltas above use" /></>}{x.error && <span className="faint"> ({x.error})</span>}</td>
                    <td className="num"><b>{scen(x.team_a)}</b></td>
                    <td className="num faint">{signed(x.team_a?.regular_season_delta)} / {signed(x.team_a?.playoff_delta)}</td>
                    <td className="num"><b>{scen(x.team_b)}</b></td>
                    <td className="num faint">{signed(x.team_b?.regular_season_delta)} / {signed(x.team_b?.playoff_delta)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="faint" style={{ margin: 0 }}>{p.note}</p>
        </div>
      ))}
    </section>
  );
}

function upside(s: IrActivation) {
  return s.lineup_points_upside === null
    ? <span title={why}>incomplete ({signed(s.known_lineup_points_upside)} known)</span>
    : signed(s.lineup_points_upside);
}

function IrSensitivity({ r }: { r: TradeResult }) {
  const rows = [r.team_a, r.team_b].flatMap((s) => s.ir_activation_sensitivity.players.map((p) => ({ s, p })));
  if (rows.length === 0) return null;
  return (
    <section className="panel">
      <h2>IR activation sensitivity (not in the trade delta)</h2>
      <p className="faint">
        Players currently in an IR slot start no canonical lineup. This shows what each would add to his team&apos;s optimal
        lineups if activated for free. It is deliberately optimistic: any roster move an activation needs is not chosen.
      </p>
      <p className="faint">{r.ir_assumption ?? IR_HOLD_NOTE}</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Team</th><th>Player</th><th>ESPN projected weeks</th><th>Would start</th>
            <th className="num">Upside before trade</th><th className="num">Upside after trade</th><th>Open active spot</th></tr></thead>
          <tbody>
            {rows.map(({ s, p }) => {
              const b = p.before_trade;
              return (
                <tr key={`${s.team_id}-${p.espn_player_id}`}>
                  <td>{s.team_name}</td>
                  <td><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /> <Badge value="IR" tone="bad" />
                    {p.traded_away && <span className="faint"> traded away</span>}</td>
                  <td className="faint wrap" title={Object.entries(b.weekly_projection).map(([w, v]) => `wk ${w}: ${v === null ? "no projection" : v.toFixed(1)}`).join("\n")}>
                    {b.projected_weeks.length ? b.projected_weeks.join(", ") : "none"}
                    {b.unprojected_weeks.length > 0 && ` · no projection: ${b.unprojected_weeks.join(", ")}`}</td>
                  <td className="faint wrap">{b.weeks_would_start.length ? b.weeks_would_start.join(", ") : "none"}</td>
                  <td className="num">{upside(b)}</td>
                  <td className="num">{p.after_trade ? upside(p.after_trade) : <span className="faint">—</span>}</td>
                  <td className="wrap">{b.open_active_roster_spot ? "yes" : <span className="faint">no. {b.roster_move_note}</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function WeekCells({ w, r }: { w: TradeSide["weekly"][number]; r: TradeResult }) {
  return (
    <>
      <td className="num">{num(w.before, 1, why)}</td>
      <td className="num">{num(w.after, 1, why)}</td>
      <td className="num">{w.delta === null ? <span className="missing" title={why}>?</span> : signed(w.delta)}</td>
      <td className="faint wrap">{w.entered.length || w.exited.length
        ? <>{w.entered.length > 0 && <span className="up">+{names(r, w.entered)}</span>} {w.exited.length > 0 && <span className="down">−{names(r, w.exited)}</span>}</>
        : "no starter change"}</td>
    </>
  );
}

function LineupTable({ before, after }: { before: LineupWeek; after: LineupWeek }) {
  return (
    <table>
      <thead><tr><th>Slot</th><th>Before</th><th className="num">Pts</th><th>After</th><th className="num">Pts</th></tr></thead>
      <tbody>
        {before.assignment.map((b, i) => {
          const a = after.assignment[i];
          const changed = b.player_id !== a?.player_id;
          return (
            <tr key={i}>
              <td><Pos value={b.slot} /></td>
              <td className={changed ? "down" : ""}>{b.name ?? <span className="faint">empty</span>}</td>
              <td className="num">{num(b.points)}</td>
              <td className={changed ? "up" : ""}>{a?.name ?? <span className="faint">empty</span>}</td>
              <td className="num">{num(a?.points)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function SidePanel({ s }: { s: TradeSide }) {
  const ro = s.roster;
  return (
    <section className="panel">
      <h2>{s.team_name}: starter changes</h2>
      <ul className="reasons">{s.fit_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
      <div className="kv" style={{ marginTop: 8 }}>
        <span className="k">Active roster</span>
        <span>{ro.active_before} → {ro.active_after} of {ro.active_capacity}</span>
        <span className="k">Mandatory drop</span>
        <span>{ro.forced_drops.length === 0 ? <span className="faint">none</span> : ro.forced_drops.map((d) => (
          <span key={d.espn_player_id}><PlayerLink id={d.espn_player_id} name={d.name} /> <Pos value={d.position} />{" "}
            <span className="faint">lineup cost {ro.forced_drop_lineup_cost === null ? `${ro.forced_drop_known_lineup_cost.toFixed(1)} (known only)` : ro.forced_drop_lineup_cost.toFixed(1)}</span></span>
        ))}</span>
        <span className="k">Open roster spots</span>
        <span>{ro.open_active_roster_spots}{ro.open_active_roster_spots_before !== ro.open_active_roster_spots ? ` (was ${ro.open_active_roster_spots_before})` : ""}</span>
        {s.hypothetical_fa_fill?.player && <>
          <span className="k">Best hypothetical FA fill</span>
          <span><PlayerLink id={s.hypothetical_fa_fill.player.espn_player_id} name={s.hypothetical_fa_fill.player.name} /> <Pos value={s.hypothetical_fa_fill.player.position} />{" "}
            <span className="faint">{signed(s.hypothetical_fa_fill.lineup_points_added ?? null)} lineup pts if added (not guaranteed; not in the delta)</span></span>
        </>}
      </div>
      {ro.redundant_incoming_asset && (
        <Callout><b>Redundant incoming asset:</b> cutting {ro.redundant_incoming_asset.names.join(", ")} right away would cost{" "}
          {ro.redundant_incoming_asset.lineup_points_saved_by_cutting_it.toFixed(1)} fewer lineup points than the drop shown above.</Callout>
      )}
      {ro.forced_drops.length > 0 && <p className="faint">Drop chosen as the legal removal that keeps the most projected starting points. ESPN undroppable-list restrictions are not modeled.</p>}
      <details style={{ marginTop: 8 }}>
        <summary>Weekly lineups before / after</summary>
        {s.lineups.before.map((b, i) => (
          <div key={b.week} style={{ marginTop: 8 }}>
            <div className="faint">Week {b.week} · {num(b.projected_lineup_points, 1, why)} → {num(s.lineups.after[i].projected_lineup_points, 1, why)}</div>
            <LineupTable before={b} after={s.lineups.after[i]} />
          </div>
        ))}
      </details>
    </section>
  );
}
