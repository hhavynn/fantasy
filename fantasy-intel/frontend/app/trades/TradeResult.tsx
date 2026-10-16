import Link from "next/link";
import { BarChart, type BarDatum } from "@/components/charts";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { PlayerLink } from "@/components/PlayerLink";
import { TradeVsFreeAgency } from "@/components/TradeVsFreeAgency";
import { Badge, Delta, Method, Notice, Pos, Section, Stat, StatStrip, TrendArrow } from "@/components/ui";
import { label, num, signed } from "@/lib/format";
import { resultText, resultTone, signedText } from "@/lib/trade-format";
import type { InjuryScenarios, TradeResultV3, TradeSideV3 } from "@/lib/trade-types";
import type { IrActivation, LineupWeek } from "@/lib/types";
import styles from "./result.module.css";
import { plural } from "@/lib/plural";

// Server-rendered evaluation of the URL selection. Order: verdict (lineup-point deltas for both teams),
// mandatory-drop and roster notices, weekly delta charts, starter changes, hypothetical FA replacement,
// trade vs. free agency, player context, then the secondary evidence and the method.

type TradeResult = TradeResultV3;
type TradeSide = TradeSideV3;

const why = "Incomplete: a rostered player with an NFL team has no ESPN projection that week";
const IR_HOLD_NOTE = "Current IR status is held constant for this hypothetical evaluation. This does not predict when a "
  + "player will return. Activation upside is shown separately and may require a roster move.";

function Players({ r, ids }: { r: TradeResult; ids: number[] }) {
  if (ids.length === 0) return <span className="faint">nobody</span>;
  return (
    <ul className={styles.exchangeList}>
      {ids.map((id) => {
        const n = r.names[id];
        return (
          <li key={id}>
            <PlayerIdentity id={id} name={n?.name ?? null} position={n?.position} team={n?.pro_team ?? null} />
          </li>
        );
      })}
    </ul>
  );
}

const nameOf = (r: TradeResult, id: number) => r.names[id]?.name ?? `#${id}`;
const names = (r: TradeResult, list: number[]) => list.map((id) => nameOf(r, id)).join(", ");

export function TradeResultView({ r }: { r: TradeResult }) {
  return (
    <div className="stack">
      <Verdict r={r} />
      <RosterNotices r={r} />
      <WeeklyDelta r={r} />
      <WeekTable r={r} />
      <StarterChanges r={r} />
      <Replacement r={r} />
      <TradeVsFreeAgency t={r.trade_vs_free_agency} />
      <PlayerContext r={r} />
      <InjuryScenarioPanel r={r} />
      <IrSensitivity r={r} />
      <ManagerContext r={r} />
      <Method title="Assumptions and limitations">
        <ul className={styles.reasons}>{r.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        <ul className={`${styles.reasons} ${styles.reasonsFaint}`}>{r.limitations.map((a, i) => <li key={i}>{a}</li>)}</ul>
      </Method>
    </div>
  );
}

// ---------------------------------------------------------------------------------- verdict --

function TeamStrip({ r, s, side }: { r: TradeResult; s: TradeSide; side: "a" | "b" }) {
  const d = s.deltas;
  const lastReg = r.evaluation.last_regular_season_week;
  const final = r.evaluation.weeks[r.evaluation.weeks.length - 1];
  const first = r.evaluation.weeks[0];
  const incomplete = <span title={why}>incomplete · {signedText(d.known_ros_delta)} known</span>;
  return (
    <div className={`${styles.strip} ${side === "a" ? styles.sideA : styles.sideB}`}>
      <h3 className={styles.stripHead}>
        <Link href={`/managers/${s.team_id}`}>{s.team_name}</Link>
        <span>receives {s.receives.length === 0 ? "nobody" : names(r, s.receives)}</span>
      </h3>
      <StatStrip>
        <Stat label="Rest of season" value={<Delta value={d.ros_delta} />}
          note={d.ros_delta === null ? incomplete : <>{num(s.before.ros_projected_starting_points, 1, why)} → {num(s.after.ros_projected_starting_points, 1, why)} pts</>} />
        <Stat label="Regular season" value={<Delta value={d.regular_season_delta} />}
          note={lastReg !== null ? `weeks ${first}–${lastReg}` : undefined} />
        <Stat label="Playoffs" value={<Delta value={d.playoff_delta} />}
          note={lastReg !== null && final > lastReg ? `weeks ${Math.max(first, lastReg + 1)}–${final}` : undefined} />
        <Stat label="Average per week" value={<Delta value={d.average_weekly_delta} digits={2} />}
          note={`${d.weeks_improved} up · ${d.weeks_worsened} down · ${d.weeks_unchanged} flat`} />
      </StatStrip>
    </div>
  );
}

function Verdict({ r }: { r: TradeResult }) {
  const A = r.team_a, B = r.team_b;
  const an = A.team_name ?? "Team A", bn = B.team_name ?? "Team B";
  return (
    <Section
      title={`Trade · ${r.trade.shape}`}
      actions={<Badge value={resultText(r.result.label, an, bn)} tone={resultTone(r.result.label)} />}
      footnote={<>
        Lineup-point deltas are the change in each team&apos;s projected starting points versus no trade (ESPN projections, exact optimal-lineup
        simulation). Combined {signedText(r.result.combined_ros_delta)} · smaller side {signedText(r.result.min_side_ros_delta)}. {r.result.note}
      </>}
    >
      <div className={styles.exchange}>
        <div className={`${styles.exchangeSide} ${styles.sideA}`}>
          <span className={styles.eyebrow}>{an} sends</span>
          <Players r={r} ids={A.sends} />
        </div>
        <span className={styles.exchangeArrow} aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h11l-3-3" /><path d="M15 12H4l3 3" /></svg>
        </span>
        <div className={`${styles.exchangeSide} ${styles.sideB}`}>
          <span className={styles.eyebrow}>{bn} sends</span>
          <Players r={r} ids={B.sends} />
        </div>
      </div>
      <p className={`${styles.labels} ${styles.muted} ${styles.small}`}>
        <span>Regular season: {resultText(r.result.regular_season_label, an, bn)}</span>
        <span>Playoffs: {resultText(r.result.playoff_label, an, bn)}</span>
      </p>
      <div className={styles.strips}>
        <TeamStrip r={r} s={A} side="a" />
        <TeamStrip r={r} s={B} side="b" />
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------------- notices --

function RosterNotices({ r }: { r: TradeResult }) {
  const sides = [r.team_a, r.team_b];
  return (
    <>
      {sides.map((s) => {
        const ro = s.roster;
        const cost = ro.forced_drop_lineup_cost === null
          ? `${ro.forced_drop_known_lineup_cost.toFixed(1)} known only` : ro.forced_drop_lineup_cost.toFixed(1);
        if (ro.forced_drops.length > 0) {
          return (
            <Notice key={`drop-${s.team_id}`} tone="caution" title={`${s.team_name}: mandatory drop`}>
              <p>
                {ro.forced_drops.map((d, i) => (
                  <span key={d.espn_player_id}>{i > 0 && ", "}<PlayerLink id={d.espn_player_id} name={d.name} /> <Pos value={d.position} />{d.is_incoming ? " (incoming)" : ""}</span>
                ))}
                {" "}must be dropped to fit the roster. Lineup cost {cost}.
              </p>
              <p className="faint">Drop chosen as the legal removal that keeps the most projected starting points. ESPN undroppable-list restrictions are not modeled.</p>
            </Notice>
          );
        }
        if (ro.open_active_roster_spots > 0) {
          const before = ro.open_active_roster_spots_before;
          return (
            <Notice key={`spot-${s.team_id}`} tone="info" title={`${s.team_name}: ${ro.open_active_roster_spots} open roster spot${ro.open_active_roster_spots === 1 ? "" : "s"}`}>
              No drop is needed. Active roster {ro.active_before} → {ro.active_after} of {ro.active_capacity}
              {before !== ro.open_active_roster_spots ? ` (${before} open before the trade)` : ""}.
            </Notice>
          );
        }
        return null;
      })}
      {r.evaluation.uncertain && r.evaluation.warning && <Notice tone="caution">{r.evaluation.warning}</Notice>}
      {r.evaluation.start_week_overridden && (
        <Notice tone="info" title={`Start week overridden: weeks ${r.evaluation.weeks[0]}–${r.evaluation.weeks[r.evaluation.weeks.length - 1]}`}>
          {r.evaluation.start_week_reason.reason}
        </Notice>
      )}
      {r.ir_warning && <Notice tone="caution" title="Injured reserve">{r.ir_warning} {r.ir_assumption ?? IR_HOLD_NOTE}</Notice>}
      {r.injury_warnings.map((w) => (
        <Notice key={w.espn_player_id} tone="caution" title={w.name ?? `#${w.espn_player_id}`}>{w.warnings.join(" ")}</Notice>
      ))}
      {sides.filter((s) => s.incomplete.missing_weeks.length > 0).map((s) => (
        <Notice key={`inc-${s.team_id}`} tone="caution" title={`${s.team_name}: incomplete projections`}>
          No ESPN projection for {names(r, s.incomplete.missing_players)} in {plural(s.incomplete.missing_weeks.length, "week")} {s.incomplete.missing_weeks.join(", ")}.
          Those weeks have no canonical total; missing is never treated as 0.
        </Notice>
      ))}
    </>
  );
}

// ------------------------------------------------------------------------------------ weekly --

function weeklyData(s: TradeSide): BarDatum[] {
  return s.weekly.map((w) => ({
    label: String(w.week),
    value: w.delta,
    tone: w.delta === null ? undefined : w.delta > 0 ? "positive" : w.delta < 0 ? "negative" : "muted",
  }));
}

function WeeklyDelta({ r }: { r: TradeResult }) {
  return (
    <Section
      title="Weekly lineup change"
      footnote="Bars are the change in projected starting points that week versus no trade. A dash is a week without a complete ESPN projection: missing, not zero."
    >
      <div className={styles.charts}>
        {[r.team_a, r.team_b].map((s) => (
          <div key={s.team_id} className={styles.chart}>
            <h3 className={styles.chartTitle}>{s.team_name}</h3>
            <p className={styles.chartSub}>Change in projected starting points by week</p>
            <BarChart data={weeklyData(s)} height={200} ariaLabel={`${s.team_name} weekly lineup change`} />
          </div>
        ))}
      </div>
    </Section>
  );
}

function WeekCells({ w, r }: { w: TradeSide["weekly"][number]; r: TradeResult }) {
  return (
    <>
      <td className="num">{num(w.before, 1, why)}</td>
      <td className="num">{num(w.after, 1, why)}</td>
      <td className="num"><Delta value={w.delta} /></td>
      <td className="faint wrap">{w.entered.length || w.exited.length
        ? <>{w.entered.length > 0 && <span className="up">+{names(r, w.entered)}</span>} {w.exited.length > 0 && <span className="down">−{names(r, w.exited)}</span>}</>
        : "no starter change"}</td>
    </>
  );
}

function WeekTable({ r }: { r: TradeResult }) {
  const A = r.team_a, B = r.team_b;
  const an = A.team_name ?? "Team A", bn = B.team_name ?? "Team B";
  return (
    <Section title="Starter changes by week" flush>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Wk</th><th>Phase</th>
              <th className="num">{an} before</th><th className="num">after</th><th className="num">Δ</th><th>In / out</th>
              <th className="num">{bn} before</th><th className="num">after</th><th className="num">Δ</th><th>In / out</th>
            </tr>
          </thead>
          <tbody>
            {A.weekly.map((wa, i) => {
              const wb = B.weekly[i];
              return (
                <tr key={wa.week}>
                  <td>{wa.week}</td><td className="faint">{wa.phase ?? "?"}</td>
                  {[wa, wb].map((w, j) => <WeekCells key={j} w={w} r={r} />)}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ------------------------------------------------------------------------- starter changes --

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

function TeamImpact({ s }: { s: TradeSide }) {
  const ro = s.roster;
  const iss = ro.issues_before_drops;
  const cost = ro.forced_drop_lineup_cost === null ? `${ro.forced_drop_known_lineup_cost.toFixed(1)} known only` : ro.forced_drop_lineup_cost.toFixed(1);
  return (
    <div className={styles.col}>
      <h3 className={styles.colTitle}>{s.team_name}</h3>
      {/* fit_notes already state, per player, how many weeks he starts (or never starts) and who is displaced. */}
      <ul className={styles.list}>
        {s.fit_notes.map((note, i) => <li key={i}>{note}</li>)}
      </ul>

      <dl className={styles.facts}>
        <dt>Active roster</dt>
        <dd>
          {ro.active_before} → {ro.active_after} of {ro.active_capacity}
          <span className={styles.sub}>
            Before drops: {iss.active_before_drops} of {iss.active_capacity}{iss.over_capacity_by > 0 ? <> · <b>over capacity by {iss.over_capacity_by}</b></> : " · within capacity"}
          </span>
        </dd>
        {iss.position_limits.length > 0 && <>
          <dt>Position limits</dt>
          <dd>{iss.position_limits.map((x) => <span key={x.position}><b>{x.position}</b>: {x.count_before_drops} rostered, ESPN limit {x.limit} (over by {x.over_by})</span>)}</dd>
        </>}
        <dt>Mandatory drop</dt>
        <dd>
          {ro.forced_drops.length === 0 ? <span className="faint">none</span> : <>
            {ro.forced_drops.map((d) => (
              <span key={d.espn_player_id}><PlayerLink id={d.espn_player_id} name={d.name} /> <Pos value={d.position} />{d.is_incoming ? " (incoming)" : ""}</span>
            ))}
            <span className={styles.sub}>
              Lineup cost {cost}
              {ro.forced_drop_search && <> · {ro.forced_drop_search.method}, {ro.forced_drop_search.candidate_sets.toLocaleString()} drop sets, {ro.forced_drop_search.lineup_solves} lineup solves</>}
            </span>
          </>}
        </dd>
        <dt>Open roster spots</dt>
        <dd>{ro.open_active_roster_spots}{ro.open_active_roster_spots_before !== ro.open_active_roster_spots ? ` (was ${ro.open_active_roster_spots_before})` : ""}</dd>
        {s.hypothetical_fa_fill?.player && <>
          <dt>Best hypothetical FA fill</dt>
          <dd>
            <PlayerLink id={s.hypothetical_fa_fill.player.espn_player_id} name={s.hypothetical_fa_fill.player.name} /> <Pos value={s.hypothetical_fa_fill.player.position} />
            <span className={styles.sub}>{signedText(s.hypothetical_fa_fill.lineup_points_added ?? null)} lineup pts if added (not guaranteed; not in the delta)</span>
          </dd>
        </>}
      </dl>

      {ro.redundant_incoming_asset && (
        <Notice tone="info" title="Redundant incoming asset" className={styles.details}>
          Cutting {ro.redundant_incoming_asset.names.join(", ")} right away would cost {ro.redundant_incoming_asset.lineup_points_saved_by_cutting_it.toFixed(1)} fewer
          lineup points than the drop shown above.
        </Notice>
      )}
      {ro.forced_drops.length > 0 && (
        <p className={`faint ${styles.small} ${styles.details}`}>Drop chosen as the legal removal that keeps the most projected starting points. ESPN undroppable-list restrictions are not modeled.</p>
      )}

      <Method title="Weekly lineups before and after" className={styles.details}>
        {s.lineups.before.map((b, i) => (
          <div key={b.week} className={styles.lineupWeek}>
            <div className="faint">Week {b.week} · {num(b.projected_lineup_points, 1, why)} → {num(s.lineups.after[i].projected_lineup_points, 1, why)}</div>
            <LineupTable before={b} after={s.lineups.after[i]} />
          </div>
        ))}
      </Method>
    </div>
  );
}

function StarterChanges({ r }: { r: TradeResult }) {
  return (
    <Section title="Roster limits and starting opportunities">
      <div className={styles.cols}>
        <TeamImpact s={r.team_a} />
        <TeamImpact s={r.team_b} />
      </div>
    </Section>
  );
}

// --------------------------------------------------------------------- hypothetical replacement --

function Replacement({ r }: { r: TradeResult }) {
  return (
    <Section title="Hypothetical FA replacement (not in the trade delta)" flush>
      <p className={styles.intro}>
        If the player simply disappeared and his team added the best currently available player who can fill one of his slots. Waiver availability is not guaranteed.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Outgoing</th><th>Best available replacement</th><th className="num">Lineup pts lost</th><th className="num">Recovered</th><th className="num">Residual loss</th></tr>
          </thead>
          <tbody>
            {r.players.map((p) => {
              const c = p.replacement_context;
              return (
                <tr key={p.espn_player_id}>
                  <td><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /></td>
                  <td>
                    {c.replacement
                      ? <><PlayerLink id={c.replacement.espn_player_id} name={c.replacement.name} /> <Pos value={c.replacement.position} /></>
                      : <span className="faint">{p.on_ir ? "current IR: nothing to replace" : "none projected"}</span>}
                  </td>
                  <td className="num">{num(c.lineup_points_lost_without_player, 1, why)}</td>
                  <td className="num">{num(c.lineup_points_recovered, 1, why)}</td>
                  <td className="num">{num(c.residual_loss_vs_original, 1, why)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------- player context --

function PlayerContext({ r }: { r: TradeResult }) {
  const A = r.team_a, B = r.team_b;
  const an = A.team_name ?? "Team A", bn = B.team_name ?? "Team B";
  return (
    <Section title="Traded players: role, sustainability and market context" flush>
      <p className={styles.intro}>
        Context only. HOLD is not untouchable, SELL is not must-trade, BUY is not accept-any-price. The trade math above is lineup impact.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Player</th><th>To</th><th className="num">ESPN ROS</th><th className="num">ESPN playoffs</th>
              <th className="num" title="Optimal lineup with him minus without him, on his current team">Lineup value now</th>
              <th>Opportunity</th><th>Role trend</th><th>Production vs xFP</th><th>Market</th><th>Injury</th><th>Context</th>
            </tr>
          </thead>
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
                    {mv.ir_activation_sensitivity && <div className="faint">IR · if activated {signed(mv.ir_activation_sensitivity.lineup_points_upside)}</div>}
                  </td>
                  <td><Badge value={i?.opportunity_tier} /></td>
                  <td><TrendArrow label={i?.role_trend} /> <span className="faint">{label(i?.role_trend)}</span></td>
                  <td><Badge value={i?.production_sustainability} /></td>
                  <td><Badge value={i?.market_signal} /></td>
                  <td className="wrap">
                    {p.on_ir && <Badge value="fantasy IR slot" tone="bad" />}{" "}
                    <Badge value={p.injury_status && !["ACTIVE", "NORMAL"].includes(p.injury_status) ? p.injury_status : null} title="ESPN designation" />
                    {p.availability && p.availability.nfl_designation_label && (
                      <div className="faint">
                        NFL: {p.availability.nfl_designation_label}
                        {p.availability.earliest_eligible_week ? ` · eligible wk ${p.availability.earliest_eligible_week}` : ""}
                        {p.availability.return_confirmed ? " · returned" : ""}
                      </div>
                    )}
                  </td>
                  <td className="faint wrap">
                    {i?.context_labels
                      ? [i.context_labels.role, i.context_labels.efficiency, i.context_labels.data_confidence].filter(Boolean).map((x) => label(x)).join(" · ")
                      : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------- injury scenarios --

function scen(v: InjuryScenarios["scenarios"][number]["team_a"]) {
  if (!v) return <span className="missing">—</span>;
  return v.ros_delta === null ? <span title={why}>{signedText(v.known_ros_delta)} known</span> : signed(v.ros_delta);
}

function InjuryScenarioPanel({ r }: { r: TradeResult }) {
  if (r.injury_scenarios.length === 0) return null;
  const an = r.team_a.team_name ?? "Team A", bn = r.team_b.team_name ?? "Team B";
  return (
    <Section title="Injured players: return scenarios (not in the canonical delta unless marked)">
      <p className={`faint ${styles.small}`}>{r.availability_note}</p>
      {r.injury_scenarios.map((p) => (
        <div key={p.espn_player_id} className={styles.scenario}>
          <h3><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /> <Badge value={p.status.replaceAll("_", " ")} tone="warn" /></h3>
          <div className="faint">
            {p.eligibility_basis ? `${p.eligibility_basis}.` : "No NFL eligibility date is known."} {p.return_confirmed ? "Return confirmed." : "Actual return not confirmed."}
          </div>
          <div className={`table-wrap ${styles.tableWrap}`}>
            <table>
              <thead>
                <tr><th>Scenario</th><th className="num">{an} ROS</th><th className="num">reg / playoffs</th><th className="num">{bn} ROS</th><th className="num">reg / playoffs</th></tr>
              </thead>
              <tbody>
                {p.scenarios.map((x) => (
                  <tr key={x.scenario}>
                    <td className="wrap">
                      {x.description}
                      {x.canonical && <> <Badge value="canonical" tone="info" title="The scenario the deltas above use" /></>}
                      {x.error && <span className="faint"> ({x.error})</span>}
                    </td>
                    <td className="num"><b>{scen(x.team_a)}</b></td>
                    <td className="num faint">{signedText(x.team_a?.regular_season_delta)} / {signedText(x.team_a?.playoff_delta)}</td>
                    <td className="num"><b>{scen(x.team_b)}</b></td>
                    <td className="num faint">{signedText(x.team_b?.regular_season_delta)} / {signedText(x.team_b?.playoff_delta)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className={`faint ${styles.small} ${styles.noMargin}`}>{p.note}</p>
        </div>
      ))}
    </Section>
  );
}

// ------------------------------------------------------------------------------ IR sensitivity --

function upside(s: IrActivation) {
  return s.lineup_points_upside === null
    ? <span title={why}>incomplete ({signedText(s.known_lineup_points_upside)} known)</span>
    : signed(s.lineup_points_upside);
}

function IrSensitivity({ r }: { r: TradeResult }) {
  const rows = [r.team_a, r.team_b].flatMap((s) => s.ir_activation_sensitivity.players.map((p) => ({ s, p })));
  if (rows.length === 0) return null;
  return (
    <Section title="IR activation sensitivity (not in the trade delta)" flush>
      <div className={styles.intro}>
        <p className={styles.noMargin}>
          Players currently in an IR slot start no canonical lineup. This shows what each would add to his team&apos;s optimal
          lineups if activated for free. It is deliberately optimistic: any roster move an activation needs is not chosen.
        </p>
        <p className={styles.noMargin}>{r.ir_assumption ?? IR_HOLD_NOTE}</p>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Team</th><th>Player</th><th>ESPN projected weeks</th><th>Would start</th>
              <th className="num">Upside before trade</th><th className="num">Upside after trade</th><th>Open active spot</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ s, p }) => {
              const b = p.before_trade;
              return (
                <tr key={`${s.team_id}-${p.espn_player_id}`}>
                  <td>{s.team_name}</td>
                  <td>
                    <PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /> <Badge value="IR" tone="bad" />
                    {p.traded_away && <span className="faint"> traded away</span>}
                  </td>
                  <td className="faint wrap" title={Object.entries(b.weekly_projection).map(([w, v]) => `wk ${w}: ${v === null ? "no projection" : v.toFixed(1)}`).join("\n")}>
                    {b.projected_weeks.length ? b.projected_weeks.join(", ") : "none"}
                    {b.unprojected_weeks.length > 0 && ` · no projection: ${b.unprojected_weeks.join(", ")}`}
                  </td>
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
    </Section>
  );
}

// ---------------------------------------------------------------------------- manager context --

function ManagerContext({ r }: { r: TradeResult }) {
  return (
    <Section title="Manager context (secondary)" footnote="No acceptance probability is computed.">
      {[r.team_a, r.team_b].map((s) => {
        const m = r.manager_context[s.team_id];
        return (
          <div key={s.team_id} className={styles.manager}>
            <b>{s.team_name}</b>{" "}
            <span className="faint">top needs: {m?.needs.slice(0, 3).map((n) => `${n.position} ${n.need_score.toFixed(2)}`).join(", ") || "—"}</span>
            {m?.classifications.trade_activity && (
              <div className={`faint ${styles.small}`}>Trade activity: <Badge value={m.classifications.trade_activity.label} /></div>
            )}
            <Method title="All manager classifications" className={styles.details}>
              <div className={styles.classes}>
                {Object.entries(m?.classifications ?? {}).map(([dim, c]) => <span key={dim} className="faint">{label(dim)}: <Badge value={c.label} /></span>)}
              </div>
            </Method>
          </div>
        );
      })}
    </Section>
  );
}
