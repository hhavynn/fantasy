import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Callout, ErrorBox, Pos } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import type { PrRecord, TeamValue, TvMatchupEffect, TvPhase, TvPlayer } from "@/lib/types";

// My Team Value: roster strength vs the league, who really moves the starting lineup, unused bench
// projection, upgrades and the playoff-race context. Every number comes from the canonical engine
// (ESPN weekly projections + the exact weekly lineup optimizer); nothing here is a probability.

const AHEAD = "#4a96ec";    // started / projected ahead (validated on the dark panel surface)
const BEHIND = "#d9762f";   // projected behind
const NEUTRAL = "#6e7681";  // bench / league reference

const PHASE_TEXT: Record<TvPhase, string> = { all: "Rest of season", regular: "Regular season", playoff: "Fantasy playoffs" };
const REASON_TEXT: Record<string, string> = {
  flex_competition: "lost FLEX/slot competition", outscored: "outscored at his position", bye: "NFL bye",
  zero_projection: "projected 0", no_projection: "no projection", ir: "IR (inactive)",
};

function one(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

function pts(v: number | null | undefined, d = 1): string {
  return v === null || v === undefined ? "—" : v.toFixed(d);
}

function signed(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}`;
}

function rec(r: PrRecord | null | undefined): string {
  return r ? `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}` : "—";
}

function marginal(p: TvPlayer, phase: TvPhase): number | null {
  return phase === "all" ? p.marginal.all : phase === "regular" ? p.marginal.regular : p.marginal.playoff;
}

export default async function TeamValuePage({ searchParams }: PageProps<"/my-team/value">) {
  const sp = await searchParams;
  const team = one(sp.team);
  const ph = one(sp.phase);
  const phase: TvPhase = ph === "regular" || ph === "playoff" ? ph : "all";
  const trades = one(sp.trades) !== "off";
  const q = new URLSearchParams();
  if (team) q.set("team_id", team);
  if (!trades) q.set("trades", "false");
  const res = await api<TeamValue>(`/api/my-team/value?${q.toString()}`);
  return (
    <div className="stack">
      <div className="page-head">
        <h1>My team value</h1>
        <span className="sub">Who actually moves your starting lineup, where projection sits unused, and where an upgrade helps most</span>
      </div>
      {!res.ok ? <ErrorBox error={res.error} /> : <Body d={res.data} phase={phase} trades={trades} team={team} />}
    </div>
  );
}

function href(team: string | undefined, phase: TvPhase, trades: boolean, change: { team?: string; phase?: TvPhase; trades?: boolean }) {
  const p = new URLSearchParams();
  const t = change.team ?? team;
  if (t) p.set("team", t);
  const ph = change.phase ?? phase;
  if (ph !== "all") p.set("phase", ph);
  if (!(change.trades ?? trades)) p.set("trades", "off");
  const s = p.toString();
  return `/my-team/value${s ? `?${s}` : ""}`;
}

function Body({ d, phase, trades, team }: { d: TeamValue; phase: TvPhase; trades: boolean; team?: string }) {
  return (
    <>
      <div className="row-flex">
        <form action="/my-team/value" className="row-flex">
          {phase !== "all" && <input type="hidden" name="phase" value={phase} />}
          {!trades && <input type="hidden" name="trades" value="off" />}
          <label>Team <select name="team" defaultValue={d.team.team_id}>
            {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
          </select></label>
          <button type="submit">Show</button>
        </form>
        <span className="faint">Window:</span>
        {(["all", "regular", "playoff"] as TvPhase[]).map((p) => (
          <Link key={p} href={href(team, phase, trades, { phase: p })}><Badge value={PHASE_TEXT[p]} tone={phase === p ? "info" : ""} /></Link>
        ))}
        <span className="faint" style={{ marginLeft: "auto" }}>
          <Link href="/trades/discover">Trade discovery</Link> · <Link href="/waivers">Waivers</Link> · <Link href="/playoff-race">Playoff race</Link> · <Link href="/trades">Trade evaluator</Link>
        </span>
      </div>
      {d.window.uncertain && d.window.warning && <Callout>{d.window.warning}</Callout>}
      {d.empty ? <Callout>{d.message}</Callout> : <Dashboard d={d} phase={phase} trades={trades} team={team} />}
    </>
  );
}

function Dashboard({ d, phase, trades, team }: { d: TeamValue; phase: TvPhase; trades: boolean; team?: string }) {
  const ov = d.overview!;
  return (
    <>
      <Summary d={d} phase={phase} />
      <div className="grid grid-2">
        <WeeklyChart d={d} />
        <Positions d={d} phase={phase} />
      </div>
      <Marginals d={d} phase={phase} />
      <BenchSection d={d} />
      <Upgrades d={d} trades={trades} team={team} phase={phase} />
      <Race d={d} />
      <section className="panel">
        <h2>How to read this</h2>
        <ul className="reasons">
          {d.notes.map((n, i) => <li key={i}>{n}</li>)}
          <li>{ov.league_ranks.rule}</li>
          {ov.league_ranks.excluded_weeks.length > 0 && <li>Weeks left out of league ranks: {ov.league_ranks.excluded_weeks.map((w) => `wk ${w.week} (${w.incomplete_teams.join(", ")} incomplete)`).join("; ")}.</li>}
          {ov.league_ranks.teams_unranked.length > 0 && <li>Teams without a roster snapshot are not ranked: {ov.league_ranks.teams_unranked.join(", ")}.</li>}
          {d.window.reason && <li>Projection window: {d.window.reason}</li>}
          <li>Data: ESPN league synced {when(d.freshness.league_synced_at)} · ESPN projections synced {when(d.freshness.projections_synced_at)}.</li>
        </ul>
      </section>
    </>
  );
}

// --------------------------------------------------------------------- summary --

function Summary({ d, phase }: { d: TeamValue; phase: TvPhase }) {
  const ov = d.overview!, split = ov.phases[phase], ranks = ov.league_ranks.phases[phase];
  const pos = d.positions!, up = d.upgrades!, race = d.playoff_race!;
  const best = up.add_drop[0];
  const closest = [...race.remaining].filter((g) => g.projected_margin !== null).sort((a, b) => Math.abs(a.projected_margin!) - Math.abs(b.projected_margin!))[0];
  return (
    <section className="panel" style={{ borderColor: AHEAD }}>
      <h2>{d.team.name}{d.team.is_mine ? " (your team)" : ""} · {PHASE_TEXT[phase]}</h2>
      <div className="grid grid-4">
        <div className="stat"><div className="label">Record (ESPN)</div>
          <div className="value">{rec(d.standing.record)}<span className="faint" style={{ fontSize: 13 }}> · seed {d.standing.espn_seed ?? "—"}</span></div>
          <span className="faint">PF {pts(d.standing.points_for)} · PA {pts(d.standing.points_against)}</span></div>
        <div className="stat"><div className="label">Projected starting points</div>
          <div className="value">{split.projected_points !== null ? pts(split.projected_points) : <span title="Some weeks are incomplete">known {pts(split.known_points)}</span>}</div>
          <span className="faint">{split.weeks.length} weeks · {split.average_weekly !== null ? `${pts(split.average_weekly)} per week` : `incomplete wk ${split.incomplete_weeks.join(", ")}`}</span></div>
        <div className="stat"><div className="label">League rank (comparable weeks)</div>
          <div className="value">{split.league_rank ? `#${split.league_rank.rank}` : "—"}<span className="faint" style={{ fontSize: 13 }}> of {ranks.teams.length}</span></div>
          <span className="faint">{ranks.weeks.length ? `${ranks.weeks.length} of ${ranks.phase_weeks.length} weeks comparable` : "no comparable weeks"}</span></div>
        <div className="stat"><div className="label">Coverage</div>
          <div className="value" style={{ fontSize: 16 }}>{ov.summary.ros_weeks_complete}/{ov.summary.ros_weeks_expected} weeks complete</div>
          <span className="faint">{ov.missing_players.length ? `missing: ${ov.missing_players.join(", ")}` : "every lineup-relevant player projected"}</span></div>
      </div>
      <ul className="reasons" style={{ marginTop: 10 }}>
        {pos.strongest.length > 0 && <li>Strongest starting slots vs the league: <b>{pos.strongest.join(", ")}</b>; weakest: <b>{pos.weakest.join(", ")}</b> (average projected points per comparable week).</li>}
        {best && best.known_ros_delta > 0 && <li>Best add/drop found: add <b>{best.add.name}</b> ({best.add.position}), drop <b>{best.drop.name}</b>: {signed(best.ros_delta ?? best.known_ros_delta)} projected starting points over the rest of the season (reg {signed(best.regular_season_delta)} · playoffs {signed(best.playoff_delta)}).</li>}
        {closest && <li>Closest remaining matchup: week {closest.weeks?.join("–")} {closest.home ? "vs" : "@"} {closest.opponent_name}, projected margin {signed(closest.projected_margin)}{closest.close ? " (close)" : ""}.</li>}
        <li>Playoff race: now {rec(race.standing_now.record)}{race.standing_now.rank ? ` #${race.standing_now.rank}` : ""}, projected {rec(race.standing_projected.record)}{race.standing_projected.rank ? ` #${race.standing_projected.rank}` : ""} ({race.standing_projected.status?.replace("_", " ") ?? "no cutline"}) · <Link href={`/playoff-race?team=${d.team.team_id}`}>open the Playoff Race Lab</Link></li>
      </ul>
    </section>
  );
}

// --------------------------------------------------------------- weekly chart --

function WeeklyChart({ d }: { d: TeamValue }) {
  const weeks = d.overview!.weekly;
  const max = Math.max(1, ...weeks.map((w) => Math.max(w.projected_lineup_points ?? w.known_lineup_points, w.league_median ?? 0)));
  const colW = 34, h = 170, base = 140, scale = 120 / max;
  const lastReg = d.window.last_regular_season_week;
  return (
    <section className="panel">
      <h2>Projected starting points by week</h2>
      <svg viewBox={`0 0 ${weeks.length * colW + 10} ${h}`} width="100%" role="img" aria-label="Projected optimal starting lineup points per week with the league median">
        <line x1={0} x2={weeks.length * colW + 10} y1={base} y2={base} stroke="var(--border)" />
        {weeks.map((w, i) => {
          const x = 5 + i * colW;
          const v = w.projected_lineup_points ?? w.known_lineup_points;
          const bh = Math.max(2, v * scale);
          const playoff = lastReg !== null && w.week > lastReg;
          return (
            <g key={w.week}>
              <title>{`Week ${w.week}${playoff ? " (fantasy playoffs)" : ""}: ${w.projection_complete ? pts(w.projected_lineup_points) : `known ${pts(w.known_lineup_points)} (incomplete)`}${w.league_median !== null ? ` · league median ${pts(w.league_median)}` : ""}`}</title>
              <rect x={x + 6} width={colW - 12} y={base - bh} height={bh} rx={4}
                    fill={w.projection_complete ? AHEAD : "none"} stroke={w.projection_complete ? "none" : AHEAD} strokeDasharray="3 2" />
              {w.league_median !== null && <line x1={x + 3} x2={x + colW - 3} y1={base - w.league_median * scale} y2={base - w.league_median * scale} stroke={NEUTRAL} strokeWidth={2} />}
              <text x={x + colW / 2} y={base + 14} textAnchor="middle" fontSize={10} fill={playoff ? "var(--text)" : "var(--muted)"}>{w.week}</text>
              {playoff && <text x={x + colW / 2} y={base + 26} textAnchor="middle" fontSize={9} fill="var(--faint)">PO</text>}
            </g>
          );
        })}
      </svg>
      <p className="faint" style={{ marginTop: 6 }}><span style={{ color: AHEAD }}>■</span> your optimal starting lineup · dashed outline = incomplete week (known-only) · <span style={{ color: NEUTRAL }}>▬</span> league median (only weeks every team is complete) · PO = fantasy playoffs.</p>
    </section>
  );
}

// -------------------------------------------------------------------- positions --

function Positions({ d, phase }: { d: TeamValue; phase: TvPhase }) {
  const slots = d.positions!.slots;
  const max = Math.max(1, ...slots.map((s) => s.strength[phase].league_best ?? 0));
  return (
    <section className="panel">
      <h2>Starting slots vs the league · {PHASE_TEXT[phase]}</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Slot</th><th>Avg / week vs league best</th><th>Rank</th><th>If the key starter were gone</th></tr></thead>
          <tbody>
            {slots.map((s) => {
              const st = s.strength[phase];
              const loss = s.largest_single_loss;
              return (
                <tr key={s.slot}>
                  <td><b>{s.slot}</b>{s.count > 1 ? <span className="faint"> ×{s.count}</span> : null}</td>
                  <td>
                    <svg width={140} height={14} role="img" aria-label={`${s.slot} average ${pts(st.average)} vs league best ${pts(st.league_best)}`}>
                      <title>{`${s.slot}: ${pts(st.average)} per comparable week; league best ${pts(st.league_best)}`}</title>
                      <rect x={0} y={3} width={Math.max(2, ((st.average ?? 0) / max) * 130)} height={8} rx={3} fill={AHEAD} />
                      {st.league_best !== null && <line x1={(st.league_best / max) * 130} x2={(st.league_best / max) * 130} y1={0} y2={14} stroke="var(--text)" strokeWidth={2} />}
                    </svg>
                    <span className="faint"> {pts(st.average)} / {pts(st.league_best)}</span>
                  </td>
                  <td>{st.rank !== null ? <Badge value={`#${st.rank} of ${st.teams_ranked}`} tone={st.rank <= 3 ? "good" : st.rank > st.teams_ranked - 3 ? "bad" : ""} /> : <span className="faint">—</span>}</td>
                  <td className="wrap" style={{ minWidth: 220 }}>{loss ? <>
                    <PlayerLink id={loss.espn_player_id} name={loss.name} />: lineup loses <b>{loss.lineup_points_lost_by_phase[phase] !== null ? pts(loss.lineup_points_lost_by_phase[phase]) : <span title="Incomplete weeks in this window">known {pts(loss.known_lineup_points_lost)} (all weeks)</span>}</b>
                    {phase === "all" && loss.recovered_by_roster !== null && <span className="faint"> (roster recovers {pts(loss.recovered_by_roster)} of his {pts(loss.own_started_points)})</span>}
                    {loss.fill_ins.length > 0 && <div className="faint" style={{ fontSize: 11 }}>covered by {loss.fill_ins.map((f) => `${f.name} (${f.weeks.length} wk)`).join(", ")}</div>}
                  </> : <span className="faint">no starter</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="faint" style={{ marginTop: 6 }}>Bar = your average projected points from that slot per comparable week; tick = the league&apos;s best. Removal scenarios re-optimize your lineup without the player (observed depth today, not an injury prediction).</p>
    </section>
  );
}

// -------------------------------------------------------------------- marginals --

function Strip({ p, weeks }: { p: TvPlayer; weeks: number[] }) {
  const started = new Set(p.weeks_started);
  const bench = new Map(p.bench.map((b) => [b.week, b]));
  const weekly = new Map(p.weekly.map((x) => [x.week, x]));
  return (
    <svg width={weeks.length * 9} height={12} role="img" aria-label={`Started ${p.weeks_started.length} of ${weeks.length} weeks`}>
      {weeks.map((w, i) => {
        const b = bench.get(w);
        const fill = started.has(w) ? AHEAD : b?.code === "ir" ? "var(--warn)" : b?.code === "no_projection" ? "none" : NEUTRAL;
        return (
          <rect key={w} x={i * 9} y={1} width={7} height={10} rx={2} fill={fill} stroke={fill === "none" ? NEUTRAL : "none"} opacity={started.has(w) ? 1 : 0.55}>
            <title>{`Week ${w}: ${started.has(w) ? "starts" : b ? b.text : "bench"} · lineup marginal ${(() => {
              const m = weekly.get(w);
              return m ? (m.marginal !== null ? m.marginal.toFixed(1) : `known ${m.known_marginal.toFixed(1)} (incomplete week)`) : "—";
            })()}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

function Marginals({ d, phase }: { d: TeamValue; phase: TvPhase }) {
  const rows = [...d.players!].sort((a, b) => (marginal(b, phase) ?? b.marginal.known_all ?? 0) - (marginal(a, phase) ?? a.marginal.known_all ?? 0) || a.espn_player_id - b.espn_player_id);
  const weeks = d.window.weeks;
  return (
    <section className="panel">
      <h2>Who actually moves your lineup · {PHASE_TEXT[phase]}</h2>
      <p className="faint">Marginal = your optimal starting-lineup points with the player minus without him. Contextual and not additive: never add these up or treat them as trade value.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Player</th><th className="num">ESPN projected</th><th className="num">Lineup marginal</th><th className="num">Reg / playoff</th>
            <th title="Hover a week for its lineup marginal points">Starts (blue) by week</th><th className="num">Starts / bench</th><th>Context</th></tr></thead>
          <tbody>
            {rows.map((p) => {
              const raw = p.espn_projected[phase];
              const m = marginal(p, phase);
              return (
                <tr key={p.espn_player_id}>
                  <td><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /> <span className="faint">{p.pro_team ?? "FA"}</span></td>
                  <td className="num">{raw.points !== null ? pts(raw.points) : <span className="faint" title={`No ESPN projection for week(s) ${raw.missing_weeks.join(", ")}`}>known {pts(raw.known_points)}</span>}</td>
                  <td className="num"><b>{m !== null ? pts(m) : <span title="Incomplete weeks">known {pts(p.marginal.known_all)}</span>}</b></td>
                  <td className="num faint">{pts(p.marginal.regular)} / {pts(p.marginal.playoff)}</td>
                  <td><Strip p={p} weeks={weeks} /></td>
                  <td className="num">{p.weeks_started.length} / {p.weeks_benched.length}</td>
                  <td className="wrap" style={{ minWidth: 200 }}>
                    {p.on_ir && <Badge value="IR" tone="bad" title="Inactive in the canonical lineup until ESPN shows him off IR" />}
                    {p.injury_status && !["ACTIVE", "NORMAL"].includes(p.injury_status) && !p.on_ir && <Badge value={p.injury_status} />}
                    {p.ir_activation_sensitivity && <span className="faint"> activation upside {signed(p.ir_activation_sensitivity.lineup_points_upside ?? p.ir_activation_sensitivity.known_lineup_points_upside)} (hypothetical free activation; not in totals)</span>}
                    {!p.on_ir && p.unused_projected_points > 0 && <span className="faint"> {pts(p.unused_projected_points)} projected pts on your bench</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------------ bench --

function BenchSection({ d }: { d: TeamValue }) {
  const b = d.bench!;
  const cand = new Set(b.consolidation_candidates);
  const cover = new Map(d.positions!.bench_coverage.map((c) => [c.espn_player_id, c]));
  if (b.players.length === 0) return null;
  return (
    <section className="panel">
      <h2>Bench projection your lineup does not use</h2>
      <p className="faint">{b.note} Unused projection is per player (the weeks he sits); it is never summed into a team number.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Player</th><th className="num">Unused projected pts</th><th className="num">Lineup marginal</th><th>Why he sits</th><th>Covers if a starter is gone</th><th /></tr></thead>
          <tbody>
            {b.players.map((p) => {
              const c = cover.get(p.espn_player_id);
              return (
                <tr key={p.espn_player_id}>
                  <td><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} />{cand.has(p.espn_player_id) && <> <Badge value="consolidation candidate" tone="warn" title="Large unused projection: may be worth more to another roster. Not a sell recommendation." /></>}</td>
                  <td className="num">{pts(p.unused_projected_points)}</td>
                  <td className="num">{pts(p.marginal ?? p.known_marginal)}</td>
                  <td className="wrap">{Object.entries(p.bench_reason_counts).map(([k, n]) => `${REASON_TEXT[k] ?? k} (${n} wk)`).join(" · ")}</td>
                  <td className="wrap faint">{c ? c.covers.map((x) => `${x.for} at ${x.slot} (${x.weeks.length} wk)`).join("; ") : "—"}</td>
                  <td><Link href={`/trades/discover`}>trade ideas</Link></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// --------------------------------------------------------------------- upgrades --

function Upgrades({ d, trades, team, phase }: { d: TeamValue; trades: boolean; team?: string; phase: TvPhase }) {
  const up = d.upgrades!;
  const fa = up.free_addition;
  return (
    <section className="panel">
      <h2>Where an upgrade helps most</h2>
      <div className="grid grid-2">
        <div>
          <h3>Weakest slot-weeks vs the best available free agent</h3>
          {up.slot_gaps.length === 0 ? <p className="faint">No free agent out-projects a starter in any slot-week.</p> : (
            <table>
              <thead><tr><th>Wk</th><th>Slot</th><th>Your starter</th><th>Best available</th><th className="num">Gap</th></tr></thead>
              <tbody>{up.slot_gaps.map((g) => (
                <tr key={`${g.week}-${g.slot}`}>
                  <td>{g.week}{g.phase === "playoff" ? <span className="faint"> PO</span> : null}</td><td>{g.slot}</td>
                  <td>{g.current_name ?? <span className="faint">empty</span>} <span className="faint">{pts(g.current_points)}</span></td>
                  <td>{g.best_available.name} <span className="faint">{pts(g.best_available.points)}</span></td>
                  <td className="num">+{pts(g.gap)}</td>
                </tr>))}</tbody>
            </table>
          )}
          <p className="faint">Single-slot, single-week gap (not a lineup total); the exact lineup effect of an addition is to the right.</p>
        </div>
        <div>
          <h3>Waiver moves (exact lineup change)</h3>
          {fa.player ? <p>
            Best free addition: <b>{fa.player.name}</b> ({fa.player.position}) {signed(fa.lineup_points_added ?? fa.known_lineup_points_added)}
            <span className="faint"> (reg {signed(fa.regular_season_points_added)} · playoffs {signed(fa.playoff_points_added)})</span>{" "}
            <Badge value={fa.realistic ? "open roster spot" : "optimistic: needs a drop"} tone={fa.realistic ? "good" : "warn"} title={fa.label} />
          </p> : <p className="faint">No fully projected free agent improves the lineup.</p>}
          {up.add_drop.length > 0 && <table>
            <thead><tr><th>Add</th><th>Drop</th><th className="num">Net</th><th className="num">Reg / PO</th></tr></thead>
            <tbody>{up.add_drop.map((x) => (
              <tr key={`${x.add.espn_player_id}-${x.drop.espn_player_id}`}>
                <td>{x.add.name} <Pos value={x.add.position} /></td><td><PlayerLink id={x.drop.espn_player_id} name={x.drop.name} /></td>
                <td className="num"><b>{signed(x.ros_delta ?? x.known_ros_delta)}</b></td>
                <td className="num faint">{signed(x.regular_season_delta)} / {signed(x.playoff_delta)}</td>
              </tr>))}</tbody>
          </table>}
          {up.waiver_checks.length > 0 && <>
            <h3 style={{ marginTop: 8 }}>Waiver page suggestions, re-checked</h3>
            <ul className="reasons">{up.waiver_checks.map((w) => (
              <li key={w.add_id}>{w.recommendation} {w.add_name ?? w.add_id}{w.drop_name ? ` / drop ${w.drop_name}` : ""}: {w.ros_delta !== undefined ? <b>{signed(w.ros_delta)}</b> : null} <span className="faint">({w.status}; waiver score {pts(w.waiver_score, 2)})</span></li>))}</ul>
            <p className="faint">{up.waiver_rule}</p>
          </>}
        </div>
      </div>
      <h3 style={{ marginTop: 10 }}>Trade preview (1-for-1)</h3>
      {!trades ? <p className="faint">Trade preview off. <Link href={href(team, phase, trades, { trades: true })}>Turn it on</Link>.</p>
        : up.trade_preview.length === 0 ? <p className="faint">{up.trade_note ?? "No 1-for-1 trade improves your lineup."} <Link href="/trades/discover">Full trade discovery</Link></p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>With</th><th>You send</th><th>You get</th><th className="num">Your gain</th><th className="num">Their change</th><th /></tr></thead>
              <tbody>{up.trade_preview.map((t) => {
                const qs = new URLSearchParams({ ta: String(t.evaluator_link.ta), tb: String(t.evaluator_link.tb) });
                t.evaluator_link.a.forEach((x) => qs.append("a", String(x)));
                t.evaluator_link.b.forEach((x) => qs.append("b", String(x)));
                return (
                  <tr key={`${t.opponent_team_id}-${t.sends.join()}-${t.receives.join()}`}>
                    <td>{t.opponent_name}{t.list === "mutual" && <> <Badge value="both gain" tone="good" /></>}</td>
                    <td>{t.send_names.join(", ")}</td><td>{t.receive_names.join(", ")}</td>
                    <td className="num"><b>{signed(t.mine.ros_delta)}</b> <span className="faint">({signed(t.mine.regular_season_delta)} / {signed(t.mine.playoff_delta)})</span></td>
                    <td className="num">{signed(t.theirs.ros_delta)}</td>
                    <td><Link href={`/trades?${qs.toString()}`}>evaluate</Link></td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        )}
      <p className="faint">{up.trade_preview_rule} <Link href="/trades/discover">Open trade discovery</Link> for 2-for-1 and 1-for-2 trades and every sort.</p>
    </section>
  );
}

// ------------------------------------------------------------------ playoff race --

function Effects({ title, rows }: { title: string; rows: TvMatchupEffect[] }) {
  if (rows.length === 0) return null;
  return (
    <div>
      <h3>{title}</h3>
      <table>
        <thead><tr><th>Wk</th><th>Opponent</th><th className="num">Margin now</th><th className="num">After</th><th /></tr></thead>
        <tbody>{rows.map((e) => (
          <tr key={e.matchup_id}>
            <td>{e.weeks.join("–")}</td><td>{e.opponent_name}</td>
            <td className="num" style={{ color: (e.margin_before ?? 0) >= 0 ? AHEAD : BEHIND }}>{signed(e.margin_before)}</td>
            <td className="num" style={{ color: (e.margin_after ?? 0) >= 0 ? AHEAD : BEHIND }}>{signed(e.margin_after)}</td>
            <td>{e.flips ? <Badge value={`projected ${e.projected_result_before} → ${e.projected_result_after}`} tone="info" /> : e.changes_projected_result ? <Badge value={`${e.projected_result_before} → ${e.projected_result_after}`} /> : null}</td>
          </tr>))}</tbody>
      </table>
    </div>
  );
}

function Race({ d }: { d: TeamValue }) {
  const pr = d.playoff_race!;
  const ad = pr.upgrade_effects.add_drop, tr = pr.upgrade_effects.trade;
  return (
    <section className="panel">
      <h2>Playoff race context</h2>
      <p>
        Now {rec(pr.standing_now.record)} · projected {rec(pr.standing_projected.record)} {pr.standing_projected.status && <Badge value={pr.standing_projected.status.replace("_", " ")} tone={pr.standing_projected.status === "inside" ? "good" : pr.standing_projected.status === "outside" ? "bad" : "warn"} />}
        {pr.close_games.length > 0 && <> · close remaining games: {pr.close_games.map((g) => `wk ${g.weeks?.join("–")} ${g.home ? "vs" : "@"} ${g.opponent_name} (${signed(g.projected_margin)})`).join(", ")}</>}
        {" "}· <Link href={`/playoff-race?team=${d.team.team_id}`}>Playoff Race Lab</Link>
      </p>
      <div className="grid grid-2">
        {ad && <Effects title={`If you add ${ad.add.name} and drop ${ad.drop.name}`} rows={ad.matchups} />}
        {tr && <Effects title={`If the top trade preview happened (with ${d.upgrades!.trade_preview.find((t) => t.opponent_team_id === tr.opponent_team_id)?.opponent_name ?? "partner"})`} rows={tr.matchups} />}
      </div>
      <p className="faint">{pr.rule} Each remaining matchup re-projects both sides&apos; optimal lineups with the changed roster(s).</p>
    </section>
  );
}
