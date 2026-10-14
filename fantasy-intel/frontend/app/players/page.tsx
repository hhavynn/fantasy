import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, ErrorBox, Pos, Score, Sparkline, TrendArrow } from "@/components/ui";
import { api } from "@/lib/api";
import { int, label, num, pct, signed } from "@/lib/format";
import type { PlayerCore, TrendInfo, UsageWeek } from "@/lib/types";

type Detail = {
  player: PlayerCore & { mapping_notes: string | null; nfl_name: string | null; nfl_team: string | null };
  score: { components: { trends: TrendInfo[]; opportunity_basis: string; recent_weeks: number[]; value_formula: string;
    recent_opportunity_per_game: number | null; recent_ppr_per_game: number | null }; reasons: string[]; as_of_week: number } | null;
  weekly: UsageWeek[];
  league_points: { scoring_period: number; actual_points: number | null; projected_points: number | null }[];
  injuries: { week: number; report_status: string | null; report_primary_injury: string | null; practice_status: string | null }[];
  depth_chart: { dt: string; team: string; pos_abb: string; pos_rank: number } | null;
  waiver: { recommendation: string; waiver_score: number; reasons: string[] } | null;
  unavailable_metrics: Record<string, string>;
};

export default async function PlayersPage({ searchParams }: PageProps<"/players">) {
  const sp = await searchParams;
  const q = typeof sp.q === "string" ? sp.q : "";
  const id = typeof sp.id === "string" ? sp.id : null;
  const [list, detail] = await Promise.all([
    id && !q ? Promise.resolve(null) : api<(PlayerCore & { fantasy_team: string | null })[]>(`/api/players?q=${encodeURIComponent(q)}&limit=40`),
    id ? api<Detail>(`/api/players/${id}`) : Promise.resolve(null),
  ]);

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Players</h1>
        <span className="sub">Usage beneath the box score: snaps, shares, expected points, red-zone work.</span>
      </div>
      <form className="row-flex" action="/players">
        <input type="search" name="q" defaultValue={q} placeholder="Search a player (e.g. Lloyd)" autoFocus={!id} />
        <button type="submit">Search</button>
        {id && <Link href="/players">clear</Link>}
      </form>

      {list?.error && <ErrorBox error={list.error} />}
      {list?.data && (
        <section className="panel">
          <h2>{q ? `Results for “${q}”` : "Most-owned players"}</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Player</th><th>NFL</th><th>Fantasy team</th><th className="num">% own</th><th className="num">G</th>
                <th className="num">PPR/g</th><th className="num">xFP/g</th><th>Trend</th><th>Class</th><th className="num">Opp</th></tr></thead>
              <tbody>
                {list.data.map((p) => (
                  <tr key={p.espn_player_id}>
                    <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                    <td>{p.pro_team ?? "FA"}</td>
                    <td>{p.fantasy_team ?? <span className="faint">{label(p.availability_status) || "—"}</span>}</td>
                    <td className="num">{num(p.percent_owned, 1)}</td>
                    <td className="num">{int(p.games, "No games / no NFL id")}</td>
                    <td className="num">{num(p.fp_per_game)}</td>
                    <td className="num">{num(p.xfp_per_game)}</td>
                    <td><TrendArrow label={p.trend_label} /></td>
                    <td><Badge value={p.classification} /></td>
                    <td className="num">{num(p.opportunity_score)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {detail?.error && <ErrorBox error={detail.error} />}
      {detail?.data && <PlayerDetail d={detail.data} />}
    </div>
  );
}

function PlayerDetail({ d }: { d: Detail }) {
  const p = d.player;
  const isRB = p.position === "RB";
  const isQB = p.position === "QB";
  const weeks = d.weekly;
  const latestInj = d.injuries[0];
  return (
    <div className="stack">
      <section className="panel">
        <div className="page-head" style={{ marginBottom: 6 }}>
          <h1>{p.full_name}</h1>
          <Pos value={p.position} />
          <span className="sub">{p.pro_team ?? "Free agent (NFL)"}</span>
          <Badge value={p.fantasy_team ?? label(p.availability_status)} tone={p.fantasy_team ? "info" : "good"} />
          {p.injury_status && !["ACTIVE", "NORMAL"].includes(p.injury_status) && <Badge value={p.injury_status} />}
        </div>
        <div className="row-flex faint">
          <span>{num(p.percent_owned, 1)}% owned ESPN-wide ({signed(p.percent_change)} this week)</span>
          <span>· season {num(p.season_points)} league pts ({num(p.season_avg_points)}/g)</span>
          {d.depth_chart && <span>· depth chart: {d.depth_chart.pos_abb}{d.depth_chart.pos_rank} ({d.depth_chart.team})</span>}
          {latestInj?.report_status && <span>· wk {latestInj.week} report: <Badge value={latestInj.report_status} /> {latestInj.report_primary_injury}</span>}
          <span>· ID map: <span className="mono">{p.match_method ?? "none"}</span>{p.match_confidence === "medium" && " (name match, verify)"}</span>
        </div>
        {p.match_method === "unmatched" && (
          <div className="callout" style={{ marginTop: 10 }}>No nflverse ID match for this ESPN player, so there&apos;s no usage data. {p.mapping_notes}</div>
        )}
      </section>

      {d.score && (
        <div className="grid grid-2">
          <section className="panel">
            <h2>Underlying value vs box score</h2>
            <div className="row-flex" style={{ marginBottom: 10 }}>
              <Badge value={p.classification} />
              <Badge value={p.trend_label} title="Composite usage trend" />
              <Badge value={p.value_gap_label} />
            </div>
            <div className="kv">
              <span className="k">Opportunity score</span><Score value={p.opportunity_score} />
              <span className="k">Production score</span><Score value={p.production_score} />
              <span className="k">Usage trend score</span><span>{signed(p.usage_trend_score, 2)} <span className="faint">(−1…+1)</span></span>
              <span className="k">PPR / game</span><span>{num(p.fp_per_game)}</span>
              <span className="k">Expected PPR / game</span><span>{num(p.xfp_per_game)} <span className="faint">(nflverse xFP)</span></span>
              <span className="k">Actual − expected</span><span>{signed(p.fp_minus_xfp)}</span>
              <span className="k">Value score</span><Score value={p.value_score} />
            </div>
            <ul className="reasons">
              {d.score.reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
            <p className="faint" style={{ marginTop: 8 }}>
              Opportunity basis: {d.score.components.opportunity_basis}, weeks {d.score.components.recent_weeks.join(", ")}. {d.score.components.value_formula}.
            </p>
          </section>
          <section className="panel">
            <h2>Usage trends (recent vs baseline games)</h2>
            <table>
              <thead><tr><th>Metric</th><th className="num">Baseline</th><th className="num">Recent</th><th className="num">Δ</th><th className="num">Threshold</th><th>Label</th></tr></thead>
              <tbody>
                {d.score.components.trends.map((t) => {
                  const isPct = ["offense_pct", "target_share", "rush_share"].includes(t.metric);
                  const f = (v: number | null) => (isPct ? pct(v) : num(v));
                  return (
                    <tr key={t.metric}>
                      <td>{label(t.metric)}</td>
                      <td className="num">{f(t.baseline_avg)} <span className="faint">{t.baseline_weeks.length ? `wk ${t.baseline_weeks.join(",")}` : ""}</span></td>
                      <td className="num">{f(t.recent_avg)} <span className="faint">{t.recent_weeks.length ? `wk ${t.recent_weeks.join(",")}` : ""}</span></td>
                      <td className="num">{t.delta === null ? "—" : isPct ? signed(t.delta * 100, 0) : signed(t.delta)}</td>
                      <td className="num faint">{isPct ? `${(t.threshold * 100).toFixed(0)} pts` : t.threshold}</td>
                      <td><Badge value={t.label} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="row-flex" style={{ marginTop: 10, gap: 16 }}>
              <span>Snap % <Sparkline points={weeks.map((w) => w.offense_pct)} max={1} /></span>
              <span>{isRB ? "Touches" : "Targets"} <Sparkline points={weeks.map((w) => (isRB ? w.opportunities : w.targets))} /></span>
              <span>xFP <Sparkline points={weeks.map((w) => w.xfp)} /></span>
            </div>
          </section>
        </div>
      )}

      <section className="panel">
        <h2>Weekly usage</h2>
        {weeks.length === 0 ? <p className="faint">No NFL games recorded for this player this season.</p> : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Wk</th><th>Opp</th><th className="num">Snaps</th><th className="num">Snap%</th>
                  {(isRB || isQB) && <><th className="num">Car</th><th className="num">Rush sh</th><th className="num">Rush yd</th><th className="num">YPC</th></>}
                  <th className="num">Tgt</th><th className="num">Tgt sh</th><th className="num">Rec</th><th className="num">Rec yd</th>
                  {!isRB && <><th className="num">Air yd</th><th className="num">aDOT</th><th className="num">WOPR</th></>}
                  <th className="num">RZ tgt</th><th className="num">RZ car</th><th className="num">GL car</th><th className="num">TD</th>
                  <th className="num">PPR</th><th className="num">League pts</th><th className="num">xFP</th><th>Source</th>
                </tr>
              </thead>
              <tbody>
                {weeks.map((w) => (
                  <tr key={w.week}>
                    <td>{w.week}</td><td>{w.opponent}</td>
                    <td className="num">{int(w.offense_snaps, "No snap record")}</td>
                    <td className="num">{pct(w.offense_pct, 0, "No snap record")}</td>
                    {(isRB || isQB) && <>
                      <td className="num">{int(w.carries)}</td><td className="num">{pct(w.rush_share)}</td>
                      <td className="num">{int(w.rushing_yards)}</td><td className="num">{num(w.yards_per_carry, 1, "No carries")}</td></>}
                    <td className="num">{int(w.targets)}</td><td className="num">{pct(w.target_share)}</td>
                    <td className="num">{int(w.receptions)}</td><td className="num">{int(w.receiving_yards)}</td>
                    {!isRB && <>
                      <td className="num">{int(w.air_yards)}</td><td className="num">{num(w.adot, 1, "No targets")}</td>
                      <td className="num">{num(w.wopr, 2)}</td></>}
                    <td className="num">{int(w.rz_targets, "No play-by-play")}</td><td className="num">{int(w.rz_carries, "No play-by-play")}</td>
                    <td className="num">{int(w.gl_carries, "No play-by-play")}</td>
                    <td className="num">{int((w.receiving_tds ?? 0) + (w.rushing_tds ?? 0))}</td>
                    <td className="num">{num(w.fantasy_points_ppr)}</td>
                    <td className="num">{num(w.league_points, 1, "No ESPN weekly points")}</td>
                    <td className="num">{num(w.xfp, 1, "Not in nflverse ff_opportunity")}</td>
                    <td className="faint">{w.stats_source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="faint" style={{ marginTop: 8 }}>
          Route participation / routes run: <b>unavailable</b> ({d.unavailable_metrics.route_participation}). Snap share is shown instead.
          “—” = data missing (hover for reason); 0 = measured zero.
        </p>
      </section>

      {d.waiver && (
        <section className="panel">
          <h2>Waiver view</h2>
          <div className="row-flex"><Badge value={d.waiver.recommendation} /> <span>score {d.waiver.waiver_score.toFixed(2)}</span> <Link href="/waivers">all waivers →</Link></div>
          <ul className="reasons">{d.waiver.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </section>
      )}
    </div>
  );
}
