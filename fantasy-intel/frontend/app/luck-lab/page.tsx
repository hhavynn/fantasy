import Link from "next/link";
import { Badge, Callout, ErrorBox } from "@/components/ui";
import { api } from "@/lib/api";
import type { LlBrief, LlCounterfactual, LlLeader, LlRecord, LlResult, LlScheduleRow, LlTimeline, LuckLab } from "@/lib/luck-types";

// Luck vs. Skill Lab: expected wins from all-play, schedule luck (actual minus expected), schedule
// difficulty, notable results and counterfactual opponent schedules, over completed, fully observed
// regular-season periods only. Historical and explanatory: no probabilities, no projections.

const AHEAD = "#4a96ec";   // positive luck / my team / my score (validated on the dark panel surface)
const BEHIND = "#d9762f";  // negative luck
const NEUTRAL = "#6e7681"; // other teams / opponents

const FLAG_TEXT: Record<string, string> = {
  top_band_loss: "top-band scorer lost", above_median_loss: "above-median loss", lost_to_top_scorer: "lost to the week's top scorer",
  bottom_band_win: "bottom-band scorer won", below_median_win: "below-median win",
};
// Narrow-safe grids (columns never wider than the screen).
const GRID4 = { gridTemplateColumns: "repeat(auto-fit, minmax(min(160px, 100%), 1fr))" };
const RESULT_TONE: Record<LlResult, string> = { W: "good", L: "bad", T: "warn" };

function one(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

function rec(r: LlRecord | { wins: number | null; losses: number | null; ties: number | null } | undefined | null): string {
  if (!r || r.wins === null || r.losses === null) return "—";
  return `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}`;
}

function signed(v: number | null | undefined, d = 2): string {
  return v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)}`;
}

function wk(weeks: number[] | null, period: number): string {
  return weeks && weeks.length ? (weeks.length > 1 ? `wk ${weeks[0]}–${weeks[weeks.length - 1]}` : `wk ${weeks[0]}`) : `period ${period}`;
}

export default async function LuckLabPage({ searchParams }: PageProps<"/luck-lab">) {
  const sp = await searchParams;
  const team = one(sp.team);
  // A schedule equal to the focus team is meaningless (the API rejects it): drop it rather than show an error.
  // When the focus team is the default (yours), the page cannot know it up front, so retry without the schedule.
  let vs = one(sp.vs);
  if (vs && team && vs === team) vs = undefined;
  const q = new URLSearchParams();
  if (team) q.set("team_id", team);
  if (vs) q.set("schedule_of", vs);
  let res = await api<LuckLab>(`/api/luck-lab?${q.toString()}`);
  let cleared = one(sp.vs) !== undefined && one(sp.vs) !== "" && vs === undefined;
  if (!res.ok && vs) {
    q.delete("schedule_of");
    const retry = await api<LuckLab>(`/api/luck-lab?${q.toString()}`);
    if (retry.ok && retry.data.focus_team_id === Number(vs)) {
      res = retry;
      cleared = true;
    }
  }
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Luck vs. skill lab</h1>
        <span className="sub">Scoring strength, head-to-head results and schedule advantage · historical, explanatory, not a prediction</span>
      </div>
      {cleared && res.ok && <Callout>The schedule comparison was cleared: a team cannot be compared against its own schedule. Pick another manager&apos;s schedule.</Callout>}
      {!res.ok ? <ErrorBox error={res.error} /> : <Body d={res.data} />}
    </div>
  );
}

function Body({ d }: { d: LuckLab }) {
  const name = (id: number | null | undefined) => d.teams.find((t) => t.team_id === id)?.name ?? `Team ${id}`;
  const cov = d.coverage;
  const pending = cov.excluded_periods.filter((e) => e.kind === "not_completed").map((e) => e.matchup_period);
  const gaps = cov.excluded_periods.filter((e) => e.kind !== "not_completed");
  return (
    <>
      <form action="/luck-lab" className="row-flex">
        <label>Team <select name="team" defaultValue={d.focus_team_id}>
          {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
        </select></label>
        <label>With the schedule of <select name="vs" defaultValue={d.schedule_of ?? ""}>
          <option value="">—</option>
          {d.teams.filter((t) => t.team_id !== d.focus_team_id).map((t) => <option key={t.team_id} value={t.team_id}>{t.name}</option>)}
        </select></label>
        <button type="submit">Show</button>
        <span className="faint" style={{ marginLeft: "auto" }}><Link href="/playoff-race">Playoff race</Link> · <Link href="/">Standings</Link></span>
      </form>
      <p className="faint" style={{ margin: 0 }}>
        <b>{cov.eligible_periods.length}</b> completed, fully observed regular-season period(s) of {cov.teams} teams
        {pending.length > 0 && <> · not completed yet: period{pending.length > 1 ? "s" : ""} {pending.join(", ")}</>}
      </p>
      {gaps.length > 0 && <Callout>Excluded for incomplete data (every team, so pools stay complete): {gaps.map((e) => `period ${e.matchup_period}: ${e.reasons.join("; ")} (${e.teams_with_final_scores}/${e.teams} teams with final scores)`).join(" · ")}</Callout>}
      {cov.eligible_periods.length === 0 ? <Callout>No completed, fully observed regular-season period yet: expected wins and schedule luck need every team&apos;s final score in the same period. {cov.rule}</Callout> : <>
        <Spotlight d={d} name={name} />
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(420px, 100%), 1fr))" }}>
          <Leaderboard d={d} />
          <Scatter d={d} />
        </div>
        <Timeline d={d} name={name} />
        <Explorer d={d} name={name} />
        <Matrix d={d} />
      </>}
      <section className="panel">
        <h2>How the numbers work</h2>
        <ul className="reasons">
          <li>{d.rules.expected}</li><li>{d.rules.luck}</li><li>{d.rules.eligible}</li><li>{d.rules.counterfactual}</li>
          <li>{d.rules.bands}</li><li>{d.rules.result_source}</li>
          {d.notes.map((n, i) => <li key={i}>{n}</li>)}
          {cov.data_notes.duplicate_ids_merged.length > 0 && <li>Identical duplicate matchup ids counted once: {cov.data_notes.duplicate_ids_merged.join(", ")}.</li>}
          {cov.data_notes.conflicting_ids.length > 0 && <li>Conflicting duplicate matchup ids (their periods excluded): {cov.data_notes.conflicting_ids.join(", ")}.</li>}
          {cov.data_notes.no_period.length > 0 && <li>Matchups without a matchup period are ignored: {cov.data_notes.no_period.length}.</li>}
          {cov.data_notes.playoff_rows > 0 && <li>{cov.data_notes.playoff_rows} playoff matchup(s) are never mixed with the regular season.</li>}
        </ul>
      </section>
    </>
  );
}

// ---------------------------------------------------------------------- spotlight --

function Result({ r }: { r: LlResult }) {
  return <Badge value={r} tone={RESULT_TONE[r]} />;
}

function Notable({ b, kind, name }: { b: LlBrief | undefined; kind: string; name: (id: number) => string }) {
  if (!b) return <span className="faint">none in eligible periods</span>;
  return (
    <span>{wk(b.weeks, b.matchup_period)}: <b>{b.score.toFixed(1)}</b> (#{b.rank} of {b.teams}) {kind} {b.opponent_score.toFixed(1)} vs {name(b.opponent_id)} (#{b.opponent_rank})
      <span className="faint"> · {b.flags.map((f) => FLAG_TEXT[f] ?? f).join(", ")}</span></span>
  );
}

function Spotlight({ d, name }: { d: LuckLab; name: (id: number) => string }) {
  const s = d.spotlight;
  if (!s.periods) return <Callout>{s.name} has no eligible periods.</Callout>;
  const luck = s.schedule_luck ?? 0;
  return (
    <section className="panel" style={{ borderColor: AHEAD }}>
      <h2>{s.name}{s.is_mine ? " (your team)" : ""}</h2>
      <div className="grid" style={GRID4}>
        <div className="stat"><div className="label">Record</div>
          <div className="value">{rec(s.actual)}</div>
          <span className="faint">ESPN {rec(s.espn_record)} · {s.periods} eligible period(s)</span></div>
        <div className="stat"><div className="label">Expected record (all-play)</div>
          <div className="value">{s.expected_wins?.toFixed(2)}–{s.expected_losses?.toFixed(2)}</div>
          <span className="faint">all-play {rec(s.all_play)} ({((s.all_play?.pct ?? 0) * 100).toFixed(1)}%) · scoring rank #{s.scoring_rank} of {s.teams_ranked}</span></div>
        <div className="stat"><div className="label">Schedule luck</div>
          <div className="value" style={{ color: luck >= 0 ? AHEAD : BEHIND }}>{signed(luck)} wins</div>
          <span className="faint">#{s.luck_rank} of {s.teams_ranked} (1 = most actual wins above expected)</span></div>
        <div className="stat"><div className="label">Opponents faced</div>
          <div className="value">{s.difficulty?.opponent_points_mean.toFixed(1)} pts</div>
          <span className="faint">average opponent score · #{s.difficulty_rank} toughest of {s.teams_ranked}</span></div>
      </div>
      <ul className="reasons" style={{ marginTop: 10 }}>
        <li>Biggest brutal loss: <Notable b={s.notable?.brutal_losses[0]} kind="lost to" name={name} /></li>
        <li>Biggest fortunate win: <Notable b={s.notable?.fortunate_wins[0]} kind="beat" name={name} /></li>
        <li>Scoring: mean {s.scoring?.mean.toFixed(1)} · median {s.scoring?.median.toFixed(1)} · range {s.scoring?.min.toFixed(1)}–{s.scoring?.max.toFixed(1)}
          {s.scoring?.iqr !== null && <> · middle half {s.scoring?.q1?.toFixed(1)}–{s.scoring?.q3?.toFixed(1)} (IQR {s.scoring?.iqr?.toFixed(1)})</>}
          {" "}· top half {((s.scoring?.top_half_pct ?? 0) * 100).toFixed(0)}% of periods · {s.scoring?.first_place} first-place and {s.scoring?.last_place} last-place weeks</li>
        {s.difficulty && <li>Schedule: opponents averaged a {((s.difficulty.opponent_share_mean) * 100).toFixed(0)}% all-play share in the weeks you met them
          {s.difficulty.opponent_season_share_mean !== null && <> versus {((s.difficulty.opponent_season_share_mean) * 100).toFixed(0)}% across their whole season (how strong those managers were overall)</>}
          {" "}· {s.difficulty.opponents_top_band} opponent week(s) in the top band, {s.difficulty.opponents_bottom_band} in the bottom band</li>}
      </ul>
    </section>
  );
}

// -------------------------------------------------------------------- leaderboard --

function LuckBar({ v, max }: { v: number; max: number }) {
  const w = Math.max(1, (Math.abs(v) / max) * 50);
  return (
    <svg width={104} height={12} role="img" aria-label={`Schedule luck ${signed(v)}`}>
      <line x1={52} x2={52} y1={0} y2={12} stroke="var(--border)" />
      <rect x={v >= 0 ? 52 : 52 - w} y={2} width={w} height={8} rx={3} fill={v >= 0 ? AHEAD : BEHIND} />
    </svg>
  );
}

function Leaderboard({ d }: { d: LuckLab }) {
  const max = Math.max(0.5, ...d.leaderboard.map((x) => Math.abs(x.schedule_luck)));
  return (
    <section className="panel">
      <h2>League luck leaderboard</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Team</th><th className="num">Actual</th><th className="num">Expected</th><th>Schedule luck</th>
            <th>All-play</th><th className="num">Opp. avg</th><th className="num">Periods</th></tr></thead>
          <tbody>
            {d.leaderboard.map((x: LlLeader) => (
              <tr key={x.team_id} className={x.is_mine ? "mine" : ""}>
                <td>{x.luck_rank}</td>
                <td><Link href={`/luck-lab?team=${x.team_id}`}>{x.name}</Link> <span className="faint">ESPN {rec(x.espn_record)}</span></td>
                <td className="num">{x.actual.win_equivalents.toFixed(1)} <span className="faint">({rec(x.actual)})</span></td>
                <td className="num">{x.expected_wins.toFixed(2)}</td>
                <td><LuckBar v={x.schedule_luck} max={max} /> <b style={{ color: x.schedule_luck >= 0 ? AHEAD : BEHIND }}>{signed(x.schedule_luck)}</b></td>
                <td>{rec(x.all_play)} <span className="faint">{((x.all_play.pct ?? 0) * 100).toFixed(0)}%</span></td>
                <td className="num">{x.opponent_points_mean.toFixed(1)} <span className="faint">#{x.difficulty_rank}</span></td>
                <td className="num">{x.periods}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="faint" style={{ marginTop: 6 }}>Actual = win equivalents (tie = ½). Expected = all-play win equivalents. Luck = actual − expected over the same periods. Opp. avg = average opponent score faced (#1 = toughest).</p>
    </section>
  );
}

function Scatter({ d }: { d: LuckLab }) {
  const pts = d.leaderboard;
  const n = d.coverage.eligible_periods.length;
  const W = 300, H = 260, pad = 34;
  const x = (v: number) => pad + (v / n) * (W - pad - 10);
  const y = (v: number) => H - pad - (v / n) * (H - pad - 10);
  return (
    <section className="panel">
      <h2>Actual vs expected wins</h2>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ maxWidth: 460 }} role="img" aria-label="Actual win equivalents against expected wins per team; above the diagonal = more actual wins than expected">
        <line x1={x(0)} y1={y(0)} x2={x(n)} y2={y(n)} stroke="var(--border)" strokeDasharray="4 3" />
        <line x1={x(0)} y1={y(0)} x2={x(n)} y2={y(0)} stroke="var(--border)" />
        <line x1={x(0)} y1={y(0)} x2={x(0)} y2={y(n)} stroke="var(--border)" />
        <text x={x(n)} y={y(0) + 14} textAnchor="end" fontSize={10} fill="var(--muted)">expected wins →</text>
        <text x={x(0) + 4} y={y(n) + 2} fontSize={10} fill="var(--muted)">↑ actual wins</text>
        <text x={x(n * 0.12)} y={y(n * 0.9)} fontSize={10} fill="var(--faint)">more wins than scoring implies</text>
        <text x={x(n * 0.55)} y={y(n * 0.12)} fontSize={10} fill="var(--faint)">fewer</text>
        {pts.map((p) => (
          <g key={p.team_id}>
            <title>{`${p.name}: actual ${p.actual.win_equivalents.toFixed(1)}, expected ${p.expected_wins.toFixed(2)}, luck ${signed(p.schedule_luck)}`}</title>
            <circle cx={x(p.expected_wins)} cy={y(p.actual.win_equivalents)} r={p.is_mine ? 6 : 4.5}
                    fill={p.is_mine ? AHEAD : NEUTRAL} stroke="var(--panel)" strokeWidth={2} />
            {(p.is_mine || Math.abs(p.schedule_luck) >= 1) && (
              <text x={x(p.expected_wins) + (p.is_mine ? 0 : 8)} y={y(p.actual.win_equivalents) + (p.is_mine ? -10 : 4)}
                    textAnchor={p.is_mine ? "middle" : "start"} fontSize={10} fontWeight={p.is_mine ? 600 : 400} fill="var(--text)">{p.name.slice(0, 14)}</text>
            )}
          </g>
        ))}
      </svg>
      <p className="faint" style={{ marginTop: 6 }}>Diagonal = actual equals expected. Labelled: your team and teams at least one win off the diagonal. Hover any point for its numbers.</p>
    </section>
  );
}

// -------------------------------------------------------------------- timeline --

function Timeline({ d, name }: { d: LuckLab; name: (id: number) => string }) {
  const t = d.timeline;
  if (t.length === 0) return null;
  const maxPts = Math.max(1, ...t.map((x) => Math.max(x.score, x.opponent_score)));
  const maxLuck = Math.max(1, ...t.map((x) => Math.max(Math.abs(x.cumulative_luck), Math.abs(x.luck))));
  const colW = 56, H = 140, base = 120;
  const lmid = 60, lscale = 48 / maxLuck;
  const width = t.length * colW + 10;
  return (
    <section className="panel">
      <h2>{d.spotlight.name}: week-by-week luck</h2>
      <div className="table-wrap">
        <svg viewBox={`0 0 ${width} ${H + 26}`} width="100%" style={{ minWidth: Math.min(width, 520), maxWidth: width * 1.5 }} role="img" aria-label="Team score and opponent score per period with the result">
          <line x1={0} x2={width} y1={base} y2={base} stroke="var(--border)" />
          {t.map((x, i) => {
            const cx = 5 + i * colW;
            const hs = Math.max(2, (x.score / maxPts) * 100), ho = Math.max(2, (x.opponent_score / maxPts) * 100);
            return (
              <g key={x.matchup_period}>
                <title>{`${wk(x.weeks, x.matchup_period)}: ${x.score.toFixed(1)} vs ${x.opponent_score.toFixed(1)} (${x.opponent_name}) · ${x.result} · rank #${x.rank} of ${x.teams} · expected ${x.expected_wins.toFixed(2)} · luck ${signed(x.luck)}`}</title>
                <rect x={cx + 8} width={18} y={base - hs} height={hs} rx={4} fill={AHEAD} />
                <rect x={cx + 28} width={18} y={base - ho} height={ho} rx={4} fill={NEUTRAL} />
                <text x={cx + colW / 2} y={base + 13} textAnchor="middle" fontSize={10} fill="var(--muted)">{x.weeks?.join("–") ?? x.matchup_period}</text>
                <text x={cx + colW / 2} y={base + 25} textAnchor="middle" fontSize={11} fontWeight={600}
                      fill={x.result === "W" ? "var(--good)" : x.result === "L" ? "var(--bad)" : "var(--warn)"}>{x.result}</text>
              </g>
            );
          })}
        </svg>
      </div>
      <p className="faint"><span style={{ color: AHEAD }}>■</span> your score · <span style={{ color: NEUTRAL }}>■</span> opponent&apos;s score · letter = actual result (also coloured).</p>
      <div className="table-wrap">
        <svg viewBox={`0 0 ${width} 130`} width="100%" style={{ minWidth: Math.min(width, 520), maxWidth: width * 1.5 }} role="img" aria-label="Luck per period (bars) and cumulative schedule luck (line), in wins">
          <line x1={0} x2={width} y1={lmid} y2={lmid} stroke="var(--border)" />
          {t.map((x, i) => {
            const cx = 5 + i * colW + colW / 2;
            const h = Math.max(1, Math.abs(x.luck) * lscale);
            return (
              <g key={x.matchup_period}>
                <title>{`${wk(x.weeks, x.matchup_period)}: luck ${signed(x.luck)} (actual ${x.result === "W" ? 1 : x.result === "T" ? 0.5 : 0} − expected ${x.expected_wins.toFixed(2)}) · cumulative ${signed(x.cumulative_luck)}`}</title>
                <rect x={cx - 9} width={18} y={x.luck >= 0 ? lmid - h : lmid} height={h} rx={3} fill={x.luck >= 0 ? AHEAD : BEHIND} opacity={0.75} />
              </g>
            );
          })}
          <polyline fill="none" stroke="var(--text)" strokeWidth={2}
                    points={t.map((x, i) => `${5 + i * colW + colW / 2},${lmid - x.cumulative_luck * lscale}`).join(" ")} />
          {t.map((x, i) => <circle key={x.matchup_period} cx={5 + i * colW + colW / 2} cy={lmid - x.cumulative_luck * lscale} r={4} fill="var(--text)" stroke="var(--panel)" strokeWidth={2} />)}
          <text x={width - 4} y={12} textAnchor="end" fontSize={10} fill="var(--muted)">cumulative {signed(t[t.length - 1].cumulative_luck)}</text>
        </svg>
      </div>
      <p className="faint">Bars: luck that period (actual result − expected wins; <span style={{ color: AHEAD }}>■</span> positive, <span style={{ color: BEHIND }}>■</span> negative). Line: cumulative schedule luck. Both in wins, on one scale.</p>
      <h3 style={{ marginTop: 8 }}>Weeks that moved the gap most</h3>
      <ul className="reasons">{d.biggest_contributors.map((x: LlTimeline) => (
        <li key={x.matchup_period}>{wk(x.weeks, x.matchup_period)}: <Result r={x.result} /> {x.score.toFixed(1)}–{x.opponent_score.toFixed(1)} vs {name(x.opponent_id)} · #{x.rank} of {x.teams} · expected {x.expected_wins.toFixed(2)} → luck <b>{signed(x.luck)}</b></li>
      ))}</ul>
    </section>
  );
}

// -------------------------------------------------------------------- explorer --

function CfTable({ c, name }: { c: LlCounterfactual; name: (id: number) => string }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Period</th><th className="num">Your score</th><th>Actual</th><th>Their opponent that week</th><th className="num">Margin</th><th>Hypothetical</th></tr></thead>
        <tbody>{c.rows.map((x) => (
          <tr key={x.matchup_period} style={x.changed ? { background: "var(--info-bg)" } : undefined}>
            <td>{wk(x.weeks, x.matchup_period)}</td>
            <td className="num">{x.score.toFixed(1)}</td>
            <td><Result r={x.actual_result} /> <span className="faint">{x.actual_opponent_score.toFixed(1)} vs {name(x.actual_opponent_id)}</span></td>
            <td>{name(x.substituted_opponent_id)} <span className="faint">{x.substituted_opponent_score.toFixed(1)}</span></td>
            <td className="num">{signed(x.margin, 1)}</td>
            <td><Result r={x.counterfactual_result} />{x.changed && <b> changed</b>}</td>
          </tr>))}</tbody>
      </table>
    </div>
  );
}

function Explorer({ d, name }: { d: LuckLab; name: (id: number) => string }) {
  const c = d.counterfactual;
  const rows = d.all_schedules;
  const max = Math.max(0.5, ...rows.map((r) => Math.abs(r.win_equivalent_difference)));
  return (
    <section className="panel">
      <h2>Counterfactual schedule explorer</h2>
      <p className="faint">{d.rules.counterfactual}</p>
      {c ? <>
        <div className="grid" style={GRID4}>
          <div className="stat"><div className="label">Periods compared</div><div className="value">{c.periods_compared.length}</div>
            <span className="faint">{c.excluded.length ? `${c.excluded.length} excluded: ${c.excluded.map((e) => e.reason).join("; ")}` : "none excluded"}</span></div>
          <div className="stat"><div className="label">Actual (same periods)</div><div className="value">{rec(c.actual)}</div></div>
          <div className="stat"><div className="label">With {name(c.schedule_of)}&apos;s opponents</div><div className="value">{rec(c.counterfactual)}</div></div>
          <div className="stat"><div className="label">Difference</div>
            <div className="value" style={{ color: c.win_equivalent_difference >= 0 ? AHEAD : BEHIND }}>{signed(c.win_equivalent_difference, 1)} wins</div>
            <span className="faint">{c.flips.loss_to_win.length} L→W · {c.flips.win_to_loss.length} W→L{c.flips.other_changes.length ? ` · ${c.flips.other_changes.length} via ties` : ""}</span></div>
        </div>
        <CfTable c={c} name={name} />
      </> : <p className="faint">Pick a manager above (&ldquo;with the schedule of&rdquo;) to see {d.spotlight.name}&apos;s scores against that manager&apos;s opponents, week by week.</p>}
      <h3 style={{ marginTop: 10 }}>{d.spotlight.name} under every other manager&apos;s schedule</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Schedule of</th><th className="num">Periods</th><th className="num">Actual</th><th className="num">Hypothetical</th><th>Difference</th><th className="num">Win % actual → hypothetical</th><th /></tr></thead>
          <tbody>{rows.map((r: LlScheduleRow) => (
            <tr key={r.schedule_of}>
              <td>{r.name}</td>
              <td className="num" title={r.excluded.map((e) => `period ${e.matchup_period}: ${e.reason}`).join("\n")}>{r.periods_compared.length}{r.excluded.length ? <span className="faint"> (−{r.excluded.length})</span> : null}</td>
              <td className="num">{rec(r.actual)}</td>
              <td className="num">{rec(r.counterfactual)}</td>
              <td><LuckBar v={r.win_equivalent_difference} max={max} /> {signed(r.win_equivalent_difference, 1)}</td>
              <td className="num">{r.actual_pct !== null ? `${(r.actual_pct * 100).toFixed(0)}% → ${((r.counterfactual_pct ?? 0) * 100).toFixed(0)}%` : "—"}</td>
              <td><Link href={`/luck-lab?team=${d.focus_team_id}&vs=${r.schedule_of}`}>weeks</Link></td>
            </tr>))}</tbody>
        </table>
      </div>
      <p className="faint">Each row compares actual and hypothetical results on its own periods: the weeks that manager played {d.spotlight.name} are excluded (−n), so rows can cover different periods and are listed by team, not ranked. The win % columns compare like with like within a row.</p>
    </section>
  );
}

// ---------------------------------------------------------------------- matrix --

function Matrix({ d }: { d: LuckLab }) {
  const m = d.matrix;
  return (
    <section className="panel">
      <h2>Weekly results matrix</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Team</th>{m.periods.map((p) => <th key={p.matchup_period} className="num">{p.weeks?.join("–") ?? p.matchup_period}</th>)}</tr></thead>
          <tbody>{m.teams.map((t) => (
            <tr key={t.team_id} className={t.team_id === d.focus_team_id ? "mine" : ""}>
              <td>{t.name}</td>
              {t.cells.map((c) => (
                <td key={c.matchup_period} className="num" title={`Period ${c.matchup_period}: ${c.score.toFixed(1)} pts · #${c.rank} · ${c.result} · expected ${c.expected_wins.toFixed(2)}${c.flags.length ? " · " + c.flags.map((f) => FLAG_TEXT[f] ?? f).join(", ") : ""}`}
                    style={{ background: `rgba(74, 150, 236, ${(0.08 + 0.5 * c.expected_wins).toFixed(2)})` }}>
                  #{c.rank} <b style={{ color: c.result === "W" ? "var(--good)" : c.result === "L" ? "var(--bad)" : "var(--warn)" }}>{c.result}</b>
                  {c.flags.length > 0 && <span title={c.flags.map((f) => FLAG_TEXT[f] ?? f).join(", ")}> ⚑</span>}
                </td>
              ))}
            </tr>))}</tbody>
        </table>
      </div>
      <p className="faint">Each cell: weekly scoring rank (#1 = highest) and the actual result; deeper blue = more of the league outscored that week (all-play share); ⚑ marks a brutal loss or fortunate win. Hover for points and expected wins.</p>
    </section>
  );
}
