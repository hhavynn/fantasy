import Link from "next/link";
import { BarChart, LineChart } from "@/components/charts";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Delta, EmptyState, Method, Pos, Section, SortableTh, VisuallyHidden } from "@/components/ui";
import { Missing, num, when } from "@/lib/format";
import { sortRows, type SortState } from "@/lib/sort";
import {
  PHASE_TEXT, knownMarginalFor, marginalFor, marginalSortValue, recordText, topContributors, withParams, type Params,
} from "@/lib/team-value-helpers";
import type { TeamValue, TvMatchupEffect, TvPhase, TvPlayer } from "@/lib/types";
import styles from "./value.module.css";
import { plural } from "@/lib/plural";
import { TeamLink } from "@/components/TeamLink";

const HEALTHY = ["ACTIVE", "NORMAL"];

const REASON_TEXT: Record<string, string> = {
  flex_competition: "lost FLEX/slot competition", outscored: "outscored at his position", bye: "NFL bye",
  zero_projection: "projected 0", no_projection: "no projection", ir: "IR (inactive)", taxi: "taxi squad (inactive)",
  streamable: "a waiver streamer projects more",
};

/** An exact signed change, or the partial known total labelled as incomplete (never passed off as exact). */
function Exact({ value, known }: { value: number | null | undefined; known: number | null | undefined }) {
  if (value !== null && value !== undefined) return <Delta value={value} />;
  return (
    <span title="Some lineup-relevant players lack projections: partial total over projected weeks only">
      <span className={styles.muted}>known </span><Delta value={known} /> <span className={styles.muted}>(incomplete)</span>
    </span>
  );
}

/** A non-negative lineup total, or the partial known value labelled "known … (incomplete)". */
function Points({ value, known, why = "Incomplete weeks in this window" }: { value: number | null | undefined; known: number | null | undefined; why?: string }) {
  if (value !== null && value !== undefined) return <>{num(value, 1)}</>;
  return <span title={why} className={styles.muted}>known {num(known, 1)} (incomplete)</span>;
}

// ---------------------------------------------------------------- summary --

export function Summary({ d, phase }: { d: TeamValue; phase: TvPhase }) {
  const pos = d.positions!;
  const up = d.upgrades!;
  const race = d.playoff_race!;
  const best = up.add_drop[0];
  const closest = [...race.remaining]
    .filter((g) => g.projected_margin !== null)
    .sort((a, b) => Math.abs(a.projected_margin!) - Math.abs(b.projected_margin!))[0];
  const status = race.standing_projected.status;
  return (
    <Section title={`${d.team.name}${d.team.is_mine ? " (default team)" : ""} · ${PHASE_TEXT[phase]}`}>
      <ul className={styles.takeaways}>
        {pos.strongest.length > 0 && (
          <li>Strongest starting slots vs the league: <b>{pos.strongest.join(", ")}</b>. Weakest: <b>{pos.weakest.join(", ")}</b> (average projected points per comparable week).</li>
        )}
        {best && best.known_ros_delta > 0 && (
          <li>
            Best add/drop found: add <b>{best.add.name}</b> ({best.add.position}), drop <b>{best.drop.name}</b>:{" "}
            <Exact value={best.ros_delta} known={best.known_ros_delta} /> projected starting points over the rest of the season
            (regular {num(best.regular_season_delta, 1)} · playoffs {num(best.playoff_delta, 1)}). <TeamLink href="/waivers">Waivers</TeamLink>
          </li>
        )}
        {closest && (
          <li>
            Closest remaining matchup: week {closest.weeks?.join("–")} {closest.home ? "vs" : "@"} {closest.opponent_name}, projected margin{" "}
            <Delta value={closest.projected_margin} />{closest.close ? " (close)" : ""}.
          </li>
        )}
        <li>
          Playoff race: now {recordText(race.standing_now.record)}{race.standing_now.rank ? ` (#${race.standing_now.rank})` : ""}, projected{" "}
          {recordText(race.standing_projected.record)}{race.standing_projected.rank ? ` (#${race.standing_projected.rank})` : ""}
          {" "}({status?.replace("_", " ") ?? "no cutline"}). <Link href={`/playoff-race?team=${d.team.team_id}`}>Playoff race lab</Link>
        </li>
      </ul>
    </Section>
  );
}

// ------------------------------------------------------------ weekly chart --

export function WeeklyChart({ d }: { d: TeamValue }) {
  const weeks = d.overview!.weekly;
  const lastReg = d.window.last_regular_season_week;
  const incomplete = weeks.filter((w) => !w.projection_complete);
  const playoffWeeks = lastReg !== null ? weeks.filter((w) => w.week > lastReg).map((w) => w.week) : [];
  return (
    <Section title="Projected starting points by week">
      <LineChart
        xLabels={weeks.map((w) => String(w.week))}
        series={[
          { id: "lineup", label: "Your optimal lineup", color: "var(--chart-1)", points: weeks.map((w) => (w.projection_complete ? w.projected_lineup_points : null)) },
          { id: "median", label: "League median", color: "var(--chart-6)", points: weeks.map((w) => w.league_median) },
        ]}
        markers
        width={520}
        height={240}
        ariaLabel="Projected optimal starting lineup points per week with the league median"
      />
      <p className={styles.caption}>
        Median shown only for weeks where every team is complete.
        {playoffWeeks.length > 0 && <> Weeks {playoffWeeks[0]}–{playoffWeeks[playoffWeeks.length - 1]} are the fantasy playoffs.</>}
        {incomplete.length > 0 && <> Not plotted (incomplete projections): {incomplete.map((w) => `week ${w.week} (known ${w.known_lineup_points.toFixed(1)})`).join(", ")}.</>}
      </p>
    </Section>
  );
}

// ------------------------------------------------------- top contributors --

export function TopContributors({ d, phase }: { d: TeamValue; phase: TvPhase }) {
  const top = topContributors(d.players!, phase, 10);
  return (
    <Section title={`Top contributors · ${PHASE_TEXT[phase]}`} footnote="Lineup marginal: starting-lineup points with the player minus without him. Not additive.">
      {top.length === 0 ? (
        <EmptyState title="No lineup contributors">No rostered player has a positive lineup marginal in this window.</EmptyState>
      ) : (
        <BarChart
          orientation="horizontal"
          data={top.map((t) => ({ label: t.exact ? t.name : `${t.name} (partial)`, value: t.value, tone: t.exact ? undefined : "caution" }))}
          valueFormat={(v) => v.toFixed(1)}
          width={520}
          ariaLabel={`Top lineup contributors, ${PHASE_TEXT[phase]}`}
        />
      )}
    </Section>
  );
}

// ---------------------------------------------------------- slot strength --

export function SlotStrength({ d, phase }: { d: TeamValue; phase: TvPhase }) {
  const slots = d.positions!.slots;
  const max = Math.max(1, ...slots.map((s) => Math.max(s.strength[phase].league_best ?? 0, s.strength[phase].average ?? 0)));
  return (
    <Section
      title={`Slot strength and depth · ${PHASE_TEXT[phase]}`}
      footnote="Bar = your average projected points from the slot per comparable week; tick = the league's best. Removal scenarios re-optimize your lineup without the player: observed depth today, not an injury prediction."
      flush
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Slot</th>
              <th>Average per week vs league best</th>
              <th>Rank</th>
              <th>If the key starter were gone</th>
            </tr>
          </thead>
          <tbody>
            {slots.map((s) => {
              const st = s.strength[phase];
              const loss = s.removal_scenarios?.length
                ? [...s.removal_scenarios].sort((a, b) => {
                    const va = a.lineup_points_lost_by_phase[phase] ?? a.known_lineup_points_lost_by_phase[phase] ?? -Infinity;
                    const vb = b.lineup_points_lost_by_phase[phase] ?? b.known_lineup_points_lost_by_phase[phase] ?? -Infinity;
                    if (vb !== va) return vb - va;
                    return a.espn_player_id - b.espn_player_id;
                  })[0]
                : s.largest_single_loss;
              return (
                <tr key={s.slot}>
                  <td><span className={styles.slotName}>{s.slot}</span>{s.count > 1 ? <span className="faint"> ×{s.count}</span> : null}</td>
                  <td>
                    <span className={styles.meter} role="img" aria-label={`${s.slot} average ${st.average?.toFixed(1) ?? "n/a"} versus league best ${st.league_best?.toFixed(1) ?? "n/a"}`}>
                      <span className={styles.fill} style={{ width: `${Math.max(2, ((st.average ?? 0) / max) * 100)}%` }} />
                      {st.league_best !== null && <span className={styles.tick} style={{ left: `calc(${(st.league_best / max) * 100}% - 1px)` }} />}
                    </span>
                    <span className={styles.muted}>{num(st.average, 1)} / {num(st.league_best, 1)}</span>
                  </td>
                  <td>
                    {st.rank !== null
                      ? <Badge value={`#${st.rank} of ${st.teams_ranked}`} tone={st.rank <= 3 ? "good" : st.rank > st.teams_ranked - 3 ? "bad" : ""} />
                      : <Missing why="No comparable weeks" />}
                  </td>
                  <td className={styles.depth}>
                    {loss ? (
                      <>
                        <PlayerLink id={loss.espn_player_id} name={loss.name} />: lineup loses{" "}
                        <b><Points value={loss.lineup_points_lost_by_phase[phase]} known={loss.known_lineup_points_lost_by_phase[phase]} /></b>
                        {phase === "all" && loss.recovered_by_roster !== null && (
                          <span className="faint"> (roster recovers {num(loss.recovered_by_roster, 1)} of his {num(loss.own_started_points, 1)})</span>
                        )}
                        {loss.fill_ins.length > 0 && <small>covered by {loss.fill_ins.map((f) => `${f.name} (${f.weeks.length} wk)`).join(", ")}</small>}
                      </>
                    ) : <span className="faint">No starter</span>}
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

// -------------------------------------------------------- marginal values --

const MARGINAL_SORTS = ["name", "proj", "marginal", "regular", "playoff", "starts"] as const;
export const MARGINAL_SORT_KEYS: readonly string[] = MARGINAL_SORTS;

function StartStrip({ p, weeks }: { p: TvPlayer; weeks: number[] }) {
  const started = new Set(p.weeks_started);
  const bench = new Map(p.bench.map((b) => [b.week, b]));
  const weekly = new Map(p.weekly.map((x) => [x.week, x]));
  const startedCount = weeks.filter((w) => started.has(w)).length;
  return (
    <svg className={styles.strip} width={weeks.length * 9} height={12} role="img" aria-label={`Started ${startedCount} of ${weeks.length} weeks`}>
      {weeks.map((w, i) => {
        const b = bench.get(w);
        const cls = started.has(w) ? styles.started : b?.code === "ir" ? styles.irWeek : b?.code === "no_projection" ? styles.noProj : styles.benched;
        const m = weekly.get(w);
        const marginal = m ? (m.marginal !== null ? m.marginal.toFixed(1) : `known ${m.known_marginal.toFixed(1)} (incomplete week)`) : "—";
        return (
          <rect key={w} className={cls} x={i * 9} y={1} width={7} height={10} rx={2}>
            <title>{`Week ${w}: ${started.has(w) ? "starts" : b ? b.text : "bench"} · lineup marginal ${marginal}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

/** Words that depend on the league: `proj` the projections ("ESPN" or "Ours"), `pv` the provider ("ESPN" or "Sleeper"). */
export type TvWords = { proj: string; pv: string };
const ESPN_WORDS: TvWords = { proj: "ESPN", pv: "ESPN" };

export function MarginalTable({ d, phase, sort, sp, words = ESPN_WORDS }: { d: TeamValue; phase: TvPhase; sort: SortState; sp: Params; words?: TvWords }) {
  const phaseWeeks = d.overview?.phases?.[phase]?.weeks ?? d.window.weeks;
  const phaseWeekSet = new Set(phaseWeeks);
  const countIn = (weeks: number[]) => weeks.filter((w) => phaseWeekSet.has(w)).length;
  // Ties keep the engine's own order, so a re-sort is stable.
  const base = [...d.players!].sort((a, b) => a.espn_player_id - b.espn_player_id);
  const rows = sortRows(base, (p) => {
    switch (sort.key) {
      case "name": return p.name ?? "";
      case "proj": return p.espn_projected[phase].points ?? p.espn_projected[phase].known_points;
      case "regular": return p.marginal.regular ?? p.marginal.known_regular;
      case "playoff": return p.marginal.playoff ?? p.marginal.known_playoff;
      case "starts": return countIn(p.weeks_started);
      default: return marginalSortValue(p, phase);
    }
  }, sort.dir);
  return (
    <Section
      title={`Player marginal value · ${PHASE_TEXT[phase]}`}
      footnote="Marginal = your optimal starting-lineup points with the player minus without him. It is contextual and not additive: never add these up or treat them as trade value. A partial value is labelled “known … (incomplete)”."
      flush
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <SortableTh sortKey="name" label="Player" current={sort} searchParams={sp} defaultDir="asc" />
              <SortableTh sortKey="proj" label={`${words.proj} proj`} align="right" current={sort} searchParams={sp} title={`${words.proj} projected points in this window`} />
              <SortableTh sortKey="marginal" label="Lineup marginal" align="right" current={sort} searchParams={sp} title="Lineup points added in the selected window" />
              <SortableTh sortKey="regular" label="Regular" align="right" current={sort} searchParams={sp} title="Lineup marginal, regular season" />
              <SortableTh sortKey="playoff" label="Playoff" align="right" current={sort} searchParams={sp} title="Lineup marginal, fantasy playoffs" />
              <SortableTh sortKey="starts" label="Starts / bench" align="right" current={sort} searchParams={sp} title="Weeks started and benched in this window" />
              <th title="Hover a week for its lineup marginal points">By week</th>
              <th>Context</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const raw = p.espn_projected[phase];
              const exactIR = p.ir_activation_sensitivity;
              return (
                <tr key={p.espn_player_id}>
                  <td><span className={styles.person}><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /> <span className="faint">{p.pro_team ?? "FA"}</span></span></td>
                  <td className="num">
                    {raw.points !== null ? num(raw.points, 1) : <span className="faint" title={`No ${words.proj} projection for ${plural(raw.missing_weeks.length, "week")} ${raw.missing_weeks.join(", ")}`}>known {num(raw.known_points, 1)}</span>}
                  </td>
                  <td className="num"><b><Points value={marginalFor(p, phase)} known={knownMarginalFor(p, phase)} /></b></td>
                  <td className="num faint">{num(p.marginal.regular, 1)}</td>
                  <td className="num faint">{num(p.marginal.playoff, 1)}</td>
                  <td className="num">{countIn(p.weeks_started)} / {countIn(p.weeks_benched)}</td>
                  <td><StartStrip p={p} weeks={phaseWeeks} /></td>
                  <td className={styles.context}>
                    <span className={styles.tags}>
                      {p.on_ir && <Badge value="IR" tone="bad" title={`Inactive in the canonical lineup until ${words.pv} shows him off IR`} />}
                      {p.on_taxi && <Badge value="Taxi" title="Sleeper taxi squad: rostered, but never in the canonical lineup" />}
                      {p.injury_status && !HEALTHY.includes(p.injury_status) && !p.on_ir && <Badge value={p.injury_status} />}
                      {exactIR && (
                        <span className="faint">
                          Activation upside {fmtSigned(exactIR.lineup_points_upside ?? exactIR.known_lineup_points_upside)} (hypothetical free activation; not in totals)
                        </span>
                      )}
                      {!p.on_ir && p.unused_projected_points > 0 && <span className="faint">{p.unused_projected_points.toFixed(1)} projected pts unused on bench</span>}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className={styles.legendRow}>
        <span className={`${styles.faintText} ${styles.legend}`}>
          <span><span className={`${styles.swatch} ${styles.swatchStarted}`} aria-hidden="true" />Starts</span>
          <span><span className={`${styles.swatch} ${styles.swatchBench}`} aria-hidden="true" />Bench</span>
          <span><span className={`${styles.swatch} ${styles.swatchIr}`} aria-hidden="true" />IR</span>
          <span><span className={`${styles.swatch} ${styles.swatchNone}`} aria-hidden="true" />No projection</span>
        </span>
      </div>
    </Section>
  );
}

function fmtSigned(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}`;
}

// ------------------------------------------------------------------ bench --

export function BenchSection({ d, words = ESPN_WORDS }: { d: TeamValue; words?: TvWords }) {
  const b = d.bench!;
  if (b.players.length === 0) return null;
  const cand = new Set(b.consolidation_candidates);
  const cover = new Map(d.positions!.bench_coverage.map((c) => [c.espn_player_id, c]));
  return (
    <Section
      title="Bench projection your lineup does not use"
      footnote={`${b.note} Unused projection is per player (the weeks he sits); it is never summed into a team number.`}
      flush
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Player</th>
              <th className="num" title={`${words.proj} projected points in weeks he is benched`}>Unused proj</th>
              <th className="num" title="Lineup points he adds">Lineup marginal</th>
              <th>Why he sits</th>
              <th>Covers if a starter is gone</th>
              <th><VisuallyHidden>Links</VisuallyHidden></th>
            </tr>
          </thead>
          <tbody>
            {b.players.map((p) => {
              const c = cover.get(p.espn_player_id);
              return (
                <tr key={p.espn_player_id}>
                  <td>
                    <span className={styles.person}>
                      <PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} />
                      {cand.has(p.espn_player_id) && <Badge value="Consolidation candidate" tone="warn" title="Large unused projection: may be worth more to another roster. Not a sell recommendation." />}
                    </span>
                  </td>
                  <td className="num">{num(p.unused_projected_points, 1)}</td>
                  <td className="num"><Points value={p.marginal} known={p.known_marginal} /></td>
                  <td className="wrap">{Object.entries(p.bench_reason_counts).map(([k, n]) => `${REASON_TEXT[k] ?? k} (${n} wk)`).join(" · ")}</td>
                  <td className="wrap faint">{c ? c.covers.map((x) => `${x.for} at ${x.slot} (${x.weeks.length} wk)`).join("; ") : "—"}</td>
                  <td><TeamLink href="/trades/discover">Trade ideas</TeamLink></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// --------------------------------------------------------------- upgrades --

export function SlotGaps({ d }: { d: TeamValue }) {
  const up = d.upgrades!;
  return (
    <Section
      title="Weakest slot-weeks vs the best free agent"
      footnote="A single-slot, single-week gap, not a lineup total. The exact lineup effect of an addition is under waiver moves."
      flush={up.slot_gaps.length > 0}
    >
      {up.slot_gaps.length === 0 ? (
        <EmptyState title="No slot-week upgrades">No free agent out-projects a starter in any slot-week.</EmptyState>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Week</th><th>Slot</th><th>Your starter</th><th>Best available</th><th className="num">Gap</th></tr>
            </thead>
            <tbody>
              {up.slot_gaps.map((g) => (
                <tr key={`${g.week}-${g.slot}`}>
                  <td>{g.week}{g.phase === "playoff" ? <span className="faint"> playoff</span> : null}</td>
                  <td>{g.slot}</td>
                  <td>{g.current_name ?? <span className="faint">Empty</span>} <span className="faint">{num(g.current_points, 1)}</span></td>
                  <td>{g.best_available.name} <span className="faint">{num(g.best_available.points, 1)}</span></td>
                  <td className="num"><Delta value={g.gap} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

export function WaiverMoves({ d, words = ESPN_WORDS }: { d: TeamValue; words?: TvWords }) {
  const up = d.upgrades!;
  const fa = up.free_addition;
  return (
    <Section
      title="Waiver moves: exact lineup change"
      footnote={up.add_drop.some((x) => x.ros_delta === null)
        ? "Rows marked incomplete are ordered by partial totals over projected weeks; they are not a complete projected comparison."
        : undefined}
      flush
    >
      <div className={styles.lead}>
        {fa.player ? (
          <p>
            Best free addition: <b>{fa.player.name}</b> ({fa.player.position}) <Exact value={fa.lineup_points_added} known={fa.known_lineup_points_added} />
            <span className="faint"> (regular {num(fa.regular_season_points_added, 1)} · playoffs {num(fa.playoff_points_added, 1)})</span>{" "}
            <Badge value={fa.realistic ? "Open roster spot" : "Optimistic: needs a drop"} tone={fa.realistic ? "good" : "warn"} title={fa.label} />
          </p>
        ) : <p className="faint">No fully projected free agent improves the lineup.</p>}
        {fa.excluded_position_limit > 0 && (
          <p className={styles.faintText}>{fa.excluded_position_limit} available players skipped: adding them would exceed {words.pv === "ESPN" ? "an" : "a"} {words.pv} position limit.</p>
        )}
      </div>
      {up.add_drop.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Add</th><th>Drop</th><th className="num">Net</th><th className="num">Regular / playoff</th></tr>
            </thead>
            <tbody>
              {up.add_drop.map((x) => (
                <tr key={`${x.add.espn_player_id}-${x.drop.espn_player_id}`}>
                  <td><span className={styles.person}>{x.add.name} <Pos value={x.add.position} /></span></td>
                  <td><PlayerLink id={x.drop.espn_player_id} name={x.drop.name} /></td>
                  <td className="num"><b><Exact value={x.ros_delta} known={x.known_ros_delta} /></b></td>
                  <td className="num faint">{fmtSigned(x.regular_season_delta)} / {fmtSigned(x.playoff_delta)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {up.waiver_checks.length > 0 && (
        <div className={styles.lead}>
          <p><b>Waiver page suggestions, re-checked</b></p>
          <ul className={styles.takeaways}>
            {up.waiver_checks.map((w) => (
              <li key={w.add_id}>
                {w.recommendation} {w.add_name ?? w.add_id}{w.drop_name ? ` / drop ${w.drop_name}` : ""}:{" "}
                {w.known_ros_delta !== undefined ? <b><Exact value={w.ros_delta} known={w.known_ros_delta} /></b> : null}{" "}
                <span className="faint">({w.status}; waiver score {num(w.waiver_score, 2)})</span>
              </li>
            ))}
          </ul>
          <p className={styles.faintText}>{up.waiver_rule}</p>
        </div>
      )}
    </Section>
  );
}

export function TradePreview({ d, trades, sp }: { d: TeamValue; trades: boolean; sp: Params }) {
  const up = d.upgrades!;
  const toggle = (
    <Link href={withParams("/my-team/value", sp, { trades: trades ? "off" : null })} scroll={false}>
      {trades ? "Hide preview" : "Show preview"}
    </Link>
  );
  return (
    <Section
      title="Trade preview (1-for-1)"
      actions={toggle}
      footnote={<>{up.trade_preview_rule} <TeamLink href="/trades/discover">Open trade discovery</TeamLink> for 2-for-1 and 1-for-2 trades and every sort.</>}
      flush={trades && up.trade_preview.length > 0}
    >
      {!trades ? (
        <p className={styles.faintText}>Trade preview is off.</p>
      ) : up.trade_preview.length === 0 ? (
        <EmptyState title="No 1-for-1 trade improves your lineup">{up.trade_note ?? "Trade discovery searches larger trades."}</EmptyState>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>With</th><th>You send</th><th>You get</th>
                <th className="num" title="Your lineup change over the rest of the season">Your gain</th>
                <th className="num" title="Their lineup change over the rest of the season">Their change</th>
                <th><VisuallyHidden>Evaluate</VisuallyHidden></th>
              </tr>
            </thead>
            <tbody>
              {up.trade_preview.map((t) => {
                const qs = new URLSearchParams({ ta: String(t.evaluator_link.ta), tb: String(t.evaluator_link.tb) });
                t.evaluator_link.a.forEach((x) => qs.append("a", String(x)));
                t.evaluator_link.b.forEach((x) => qs.append("b", String(x)));
                return (
                  <tr key={`${t.opponent_team_id}-${t.sends.join()}-${t.receives.join()}`}>
                    <td><span className={styles.tags}>{t.opponent_name}{t.list === "mutual" && <Badge value="Both gain" tone="good" />}</span></td>
                    <td>{t.send_names.join(", ")}</td>
                    <td>{t.receive_names.join(", ")}</td>
                    <td className="num">
                      <b><Delta value={t.mine.ros_delta} /></b>{" "}
                      <span className="faint">({fmtSigned(t.mine.regular_season_delta)} / {fmtSigned(t.mine.playoff_delta)})</span>
                    </td>
                    <td className="num"><Delta value={t.theirs.ros_delta} /></td>
                    <td><TeamLink href={`/trades?${qs.toString()}`}>Evaluate</TeamLink></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

// ---------------------------------------------------------- playoff race --

function Effects({ title, rows }: { title: string; rows: TvMatchupEffect[] }) {
  if (rows.length === 0) return null;
  return (
    <div>
      <p className={styles.strong}>{title}</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Week</th><th>Opponent</th><th className="num">Margin now</th><th className="num">After</th><th>Projected result</th></tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.matchup_id}>
                <td>{e.weeks.join("–")}</td>
                <td>{e.opponent_name}</td>
                <td className="num"><Delta value={e.margin_before} /></td>
                <td className="num"><Delta value={e.margin_after} /></td>
                <td>
                  {e.changes_projected_result
                    ? <Badge value={`${e.projected_result_before} → ${e.projected_result_after}`} tone={e.flips ? "info" : ""} />
                    : <span className="faint">{e.projected_result_after ?? "—"}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function RaceContext({ d }: { d: TeamValue }) {
  const pr = d.playoff_race!;
  const ad = pr.upgrade_effects.add_drop;
  const tr = pr.upgrade_effects.trade;
  const status = pr.standing_projected.status;
  const partner = tr ? d.upgrades!.trade_preview.find((t) => t.opponent_team_id === tr.opponent_team_id)?.opponent_name ?? "partner" : "";
  const hasEffects = (ad && ad.matchups.length > 0) || (tr && tr.matchups.length > 0);
  return (
    <Section
      title="Playoff race context"
      actions={<Link href={`/playoff-race?team=${d.team.team_id}`}>Open the playoff race lab</Link>}
      footnote={`${pr.rule} Each remaining matchup re-projects both sides' optimal lineups with the changed rosters.`}
    >
      <p>
        Now {recordText(pr.standing_now.record)}{pr.standing_now.rank ? ` (#${pr.standing_now.rank})` : ""} · projected {recordText(pr.standing_projected.record)}{" "}
        {status && <Badge value={status.replace("_", " ")} tone={status === "inside" ? "good" : status === "outside" ? "bad" : "warn"} />}
      </p>
      {pr.close_games.length > 0 && (
        <p className={styles.faintText}>
          Close remaining games: {pr.close_games.map((g) => `week ${g.weeks?.join("–")} ${g.home ? "vs" : "@"} ${g.opponent_name} (${g.projected_margin === null ? "—" : `${g.projected_margin > 0 ? "+" : g.projected_margin < 0 ? "−" : ""}${Math.abs(g.projected_margin).toFixed(1)}`})`).join(", ")}
        </p>
      )}
      {hasEffects && (
        <Method title="Matchup effects of the top add/drop and trade">
          <div className={styles.pair}>
            {ad && <Effects title={`If you add ${ad.add.name} and drop ${ad.drop.name}`} rows={ad.matchups} />}
            {tr && <Effects title={`If the top trade preview happened (with ${partner})`} rows={tr.matchups} />}
          </div>
        </Method>
      )}
    </Section>
  );
}

// ----------------------------------------------------------------- method --

export function ValueMethod({ d, words = ESPN_WORDS }: { d: TeamValue; words?: TvWords }) {
  const ov = d.overview!;
  return (
    <Method title="How to read this">
      <ul className="reasons">
        {d.notes.map((n, i) => <li key={i}>{n}</li>)}
        <li>{ov.league_ranks.rule}</li>
        {ov.league_ranks.excluded_weeks.length > 0 && (
          <li>Weeks left out of league ranks: {ov.league_ranks.excluded_weeks.map((w) => `week ${w.week} (${w.incomplete_teams.join(", ")} incomplete)`).join("; ")}.</li>
        )}
        {ov.league_ranks.teams_unranked.length > 0 && <li>Teams without a roster snapshot are not ranked: {ov.league_ranks.teams_unranked.join(", ")}.</li>}
        {d.window.reason && <li>Projection window: {d.window.reason}</li>}
        <li>Data: {words.pv} league synced {when(d.freshness.league_synced_at)} · {words.proj === "ESPN"
          ? <>ESPN projections synced {when(d.freshness.projections_synced_at)}</>
          : <>Fantasy Intel projections (Ours) as of {when(d.freshness.projections_synced_at)}</>}.</li>
      </ul>
    </Method>
  );
}
