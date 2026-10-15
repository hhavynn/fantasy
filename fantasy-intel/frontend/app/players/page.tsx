import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, ErrorBox, Pos, Score, Sparkline, TrendArrow } from "@/components/ui";
import { api } from "@/lib/api";
import { int, label, num, pct, signed, when } from "@/lib/format";
import { BASIS_LABEL, FTN_READ1_LABEL, FTN_READ1_NOTE } from "@/lib/types";
import type { ContextEvidence, ContextLabels, ForwardProjection, PlayerCore, RouteData, TrendInfo, UsageWeek } from "@/lib/types";

type Detail = {
  player: PlayerCore & { mapping_notes: string | null; nfl_name: string | null; nfl_team: string | null };
  score: { components: { trends: TrendInfo[]; opportunity_basis: string; recent_weeks: number[]; value_formula: string;
    recent_opportunity_per_game: number | null; recent_ppr_per_game: number | null }; reasons: string[]; as_of_week: number } | null;
  weekly: UsageWeek[];
  league_points: { scoring_period: number; actual_points: number | null; projected_points: number | null }[];
  forward_projection: ForwardProjection | null;
  injuries: { week: number; report_status: string | null; report_primary_injury: string | null; practice_status: string | null }[];
  depth_chart: { dt: string; team: string; pos_abb: string; pos_rank: number } | null;
  waiver: { recommendation: string; waiver_score: number; reasons: string[] } | null;
  unavailable_metrics: Record<string, string>;
  route_data: RouteData | null;
  context: { labels: ContextLabels; evidence: ContextEvidence } | null;
  team_context: { proe: number | null; neutral_pass_rate: number | null; sack_rate: number | null;
    hit_or_sack_rate: number | null; pfr_pressure_rate: number | null; next_opponent: string | null;
    next_implied_total: number | null; games: number; through_week: number } | null;
  opponent_defense: { defteam: string; games: number; pass_epa_allowed: number | null; rush_epa_allowed: number | null;
    rush_success_allowed: number | null; explosive_run_rate_allowed: number | null;
    rb_target_share_allowed: number | null; te_target_share_allowed: number | null;
    ranks: Record<string, number | null>; pass_plays: number; rush_plays: number } | null;
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
                    <td><Badge value={p.market_signal} /></td>
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
              <Badge value={p.market_signal} title="Market signal (needs a meaningful absolute role for BUY)" />
              <span className="faint">role</span><Badge value={p.opportunity_tier} title="Absolute opportunity tier" />
              <span className="faint">trend</span><Badge value={p.role_trend} title="Composite usage trend" />
              <span className="faint">production</span>
              <Badge value={p.production_sustainability ?? p.value_gap_label}
                title="Production vs the opportunity (xFP) baseline. Regression evidence only, not a sell signal: xFP is an average-player model." />
            </div>
            <div className="kv">
              <span className="k">Opportunity score</span><span><Score value={p.opportunity_score} /> <span className="faint">basis: {BASIS_LABEL[p.opportunity_basis ?? "unavailable"] ?? p.opportunity_basis}</span></span>
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
              Opportunity basis: {BASIS_LABEL[d.score.components.opportunity_basis] ?? d.score.components.opportunity_basis}, weeks {d.score.components.recent_weeks.join(", ")}. {d.score.components.value_formula}.
            </p>
          </section>
          <section className="panel">
            <h2>Usage trends (recent vs baseline games)</h2>
            <table>
              <thead><tr><th>Metric</th><th className="num">Baseline</th><th className="num">Recent</th><th className="num">Δ</th><th className="num">Threshold</th><th>Label</th></tr></thead>
              <tbody>
                {d.score.components.trends.map((t) => {
                  const isPct = ["offense_pct", "target_share", "team_rush_share", "backfield_rush_share"].includes(t.metric);
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

      {d.forward_projection && <ForwardPanel f={d.forward_projection} />}
      {["QB", "RB", "WR", "TE"].includes(d.player.position ?? "") && (
        <p className="faint" style={{ margin: 0 }}>
          <Link href={`/schedule-intelligence?player=${d.player.espn_player_id}`}>Schedule intelligence</Link>: remaining opponents
          and what each defense has observed allowing (descriptive; separate from the projection above).
        </p>
      )}
      {d.context && <ContextPanel d={d} />}
      {d.route_data && <RoutePanel r={d.route_data} />}

      <section className="panel">
        <h2>Weekly usage</h2>
        {weeks.length === 0 ? <p className="faint">No NFL games recorded for this player this season.</p> : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Wk</th><th>Opp</th><th className="num">Snaps</th><th className="num">Snap%</th>
                  {(isRB || isQB) && <><th className="num">Car</th><th className="num" title="Rush attempts (incl. scrambles, excl. kneels) / team rush attempts">Team rush sh</th>
                    {isRB && <th className="num" title="Designed rushes / team RB designed rushes">Backfield sh</th>}
                    <th className="num">Rush yd</th><th className="num">YPC</th>
                    <th className="num" title="Designed rushes with EPA > 0">Succ%</th><th className="num" title="PFR yards before contact / att">YBC/a</th>
                    <th className="num" title="PFR yards after contact / att">YAC/a</th><th className="num" title="PFR broken tackles (rush + rec)">BT</th></>}
                  <th className="num">Tgt</th><th className="num">Tgt sh</th><th className="num">Rec</th><th className="num">Rec yd</th>
                  {!isRB && <><th className="num">Air yd</th><th className="num">aDOT</th><th className="num">WOPR</th>
                    <th className="num" title={FTN_READ1_NOTE}>FTN code-1 sh*</th></>}
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
                      <td className="num">{int(w.carries)}</td><td className="num">{pct(w.team_rush_share, 0, "No play-by-play")}</td>
                      {isRB && <td className="num">{pct(w.backfield_rush_share, 0, "No RB carries for team")}</td>}
                      <td className="num">{int(w.rushing_yards)}</td><td className="num">{num(w.yards_per_carry, 1, "No carries")}</td>
                      <td className="num">{pct(w.rushing_success_rate, 0, "No designed rushes")}</td>
                      <td className="num">{num(w.ybc_per_att, 1, "No PFR row")}</td><td className="num">{num(w.yac_per_att, 1, "No PFR row")}</td>
                      <td className="num">{int(w.broken_tackles, "No PFR row")}</td></>}
                    <td className="num">{int(w.targets)}</td><td className="num">{pct(w.target_share)}</td>
                    <td className="num">{int(w.receptions)}</td><td className="num">{int(w.receiving_yards)}</td>
                    {!isRB && <>
                      <td className="num">{int(w.air_yards)}</td><td className="num">{num(w.adot, 1, "No targets")}</td>
                      <td className="num">{num(w.wopr, 2)}</td>
                      <td className="num faint" title={FTN_READ1_NOTE}>{pct(w.ftn_read1_target_share, 0, "Game not charted by FTN")}</td></>}
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
          {d.route_data
            ? <>Route data: external provider <b>{d.route_data.providers.join(", ")}</b> (not nflverse), weeks {d.route_data.weeks.join(", ")}.</>
            : <>Route participation / routes run: <b>unavailable</b> ({d.unavailable_metrics.route_participation}). Snap share is shown instead.</>}
          * FTN code-1 share is experimental: read-code semantics are unresolved upstream (nflverse/nflreadr#325); it is not used in any score.
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


function weeksRange(weeks: number[]): string {
  return weeks.length ? `wk ${weeks.join(", ")}` : "none";
}

function ForwardPanel({ f }: { f: ForwardProjection }) {
  const s = f.summary;
  const span = (avail: number | null, expected: number | null) =>
    expected === null ? "" : `${avail}/${expected} wk`;
  const incomplete = "Not every week in this window has an ESPN projection, so no total is shown (missing weeks are not counted as 0)";
  return (
    <section className="panel">
      <h2>ESPN forward projection <span className="faint" style={{ textTransform: "none" }}>
        · Source: {f.source} · weeks {s.as_of_week}–{s.final_week} · synced {when(f.projection_synced_at)}</span></h2>
      <div className="grid grid-2">
        <div className="kv">
          <span className="k">Rest of season</span>
          <span>{num(s.ros_projected_points, 1, incomplete)} <span className="faint">{span(s.ros_weeks_available, s.ros_weeks_expected)}</span></span>
          <span className="k">Regular season left</span>
          <span>{s.last_regular_season_week === null ? <span className="faint">split unknown</span>
            : <>{num(s.regular_season_projected_points, 1, incomplete)} <span className="faint">
              {s.regular_season_weeks_expected ? `wk ${s.as_of_week}–${s.last_regular_season_week} · ` : ""}
              {span(s.regular_season_weeks_available, s.regular_season_weeks_expected)}</span></>}</span>
          <span className="k">Fantasy playoffs</span>
          <span>{s.last_regular_season_week === null ? <span className="faint">split unknown</span>
            : <>{num(s.playoff_projected_points, 1, incomplete)} <span className="faint">
              wk {Math.max(s.last_regular_season_week + 1, s.as_of_week)}–{s.final_week} ·{" "}
              {span(s.playoff_weeks_available, s.playoff_weeks_expected)}</span></>}</span>
        </div>
        <div className="kv">
          <span className="k">This week</span><span>{num(s.current_week_projection, 1, "No ESPN projection for this week")}</span>
          <span className="k">Coverage</span>
          <span>{pct(s.ros_coverage, 0)}{s.missing_weeks.length > 0 && <span className="faint"> · missing {weeksRange(s.missing_weeks)}</span>}</span>
          <span className="k">Remaining bye</span>
          <span>{s.bye_weeks_remaining === null
            ? <span className="faint" title="NFL schedule or team unknown for at least one remaining week">unknown{s.bye_weeks.length ? ` (${weeksRange(s.bye_weeks)})` : ""}</span>
            : s.bye_weeks_remaining === 0 ? "none" : weeksRange(s.bye_weeks)}</span>
        </div>
      </div>
      <div className="table-wrap" style={{ marginTop: 10 }}>
        <table>
          <thead><tr><th>Week</th><th>Opponent</th><th className="num">ESPN proj</th><th>Bye</th><th></th></tr></thead>
          <tbody>
            {f.weeks.map((w) => (
              <tr key={w.week}>
                <td>{w.week}</td>
                <td>{w.on_bye ? <span className="faint">—</span>
                  : w.opponent ? `${w.is_home ? "vs" : "@"} ${w.opponent}`
                  : <span className="missing" title="NFL schedule unavailable">—</span>}</td>
                <td className="num">{num(w.espn_projected_points, 1, "ESPN returned no projection for this week (unknown, not 0)")}</td>
                <td>{w.on_bye === null ? <span className="missing" title="Schedule unknown">?</span> : w.on_bye ? <Badge value="BYE" tone="warn" /> : ""}</td>
                <td className="faint">{w.phase === "playoff" ? "playoffs" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="faint" style={{ marginTop: 8 }}>
        {f.note} These are ESPN&apos;s weekly projections, shown unmodified; totals require every week in the window
        (a projected 0 counts, a missing week does not). Byes come from the NFL schedule, not from a 0 projection.
      </p>
    </section>
  );
}


function ContextPanel({ d }: { d: Detail }) {
  const ctx = d.context!;
  const L = ctx.labels;
  const e = ctx.evidence;
  const pos = d.player.position;
  const tc = d.team_context;
  const od = d.opponent_defense;
  const tone = (v?: string) =>
    ["STRONG", "HIGH", "GOOD", "FAVORABLE", "ABOVE_AVERAGE", "ABOVE_EXPECTED", "SUPPORTIVE", "UP"].includes(v ?? "") ? "good"
      : ["LIMITED", "LOW", "POOR", "TOUGH", "BELOW_AVERAGE", "BELOW_EXPECTED", "DOWN"].includes(v ?? "") ? "bad" : "";
  return (
    <section className="panel">
      <h2>Player context <span className="faint" style={{ textTransform: "none" }}>· last {e.games_recent} games (wk {e.recent_weeks.join(", ")})</span></h2>
      <div className="grid grid-4">
        <div className="panel tight">
          <h3>Role <Badge value={L.role} tone={tone(L.role)} /> <Badge value={L.role_trend} /></h3>
          <div className="kv">
            <span className="k">Snap share</span><span>{pct(e.snap_pct)}</span>
            <span className="k">Target share</span><span>{pct(e.target_share)}</span>
            {pos === "RB" && <><span className="k">Backfield share</span><span>{pct(e.backfield_rush_share)}</span>
              <span className="k">Team rush share</span><span>{pct(e.team_rush_share)}</span></>}
            {pos !== "RB" && <><span className="k">Air-yard share</span><span>{pct(e.air_yards_share)}</span></>}
          </div>
        </div>
        <div className="panel tight">
          <h3>{pos === "RB" ? "Rushing" : "Receiving"} usage <Badge value={L.scoring_opportunity} tone={tone(L.scoring_opportunity)} title="Scoring opportunity" /></h3>
          <div className="kv">
            {pos === "RB" ? <>
              <span className="k">RZ carries / g</span><span>{num(e.rz_carries_pg)}</span>
              <span className="k">GL carries</span><span>{int(e.gl_carries_recent)}</span>
              <span className="k">Efficiency</span><Badge value={L.efficiency} tone={tone(L.efficiency)} />
              <span className="k">YBC / YAC per att</span><span>{num(e.ybc_per_att)} / {num(e.yac_per_att)}</span>
              <span className="k">Success / explosive</span><span>{pct(e.rushing_success_rate)} / {pct(e.explosive_run_rate)}</span>
              <span className="k">Run blocking</span><Badge value={L.run_blocking} tone={tone(L.run_blocking)} />
              <span className="k">Designed rushes</span><span>{e.designed_rushes_season} <span className="faint">(season sample)</span></span>
            </> : <>
              <span className="k">RZ targets / g</span><span>{num(e.rz_targets_pg)}</span>
              <span className="k">aDOT / WOPR</span><span>{num(e.adot)} / {num(e.wopr, 2)}</span>
              <span className="k">Designed targets</span><span>{int(e.designed_targets, "Not charted")}</span>
              <span className="k">Efficiency</span><Badge value={L.efficiency} tone={tone(L.efficiency)} />
              <span className="k">NGS separation</span><span>{num(e.ngs_avg_separation, 1, "NGS: not qualified / unavailable")}</span>
            </>}
          </div>
        </div>
        <div className="panel tight">
          <h3>Offensive environment <Badge value={L.offensive_environment} tone={tone(L.offensive_environment)} /></h3>
          <div className="kv">
            <span className="k">Next implied total</span><span>{num(tc?.next_implied_total, 1, "No betting line / bye")}</span>
            <span className="k">PROE</span><span>{num(tc?.proe)} <span className="faint">pts</span></span>
            <span className="k">Neutral pass rate</span><span>{pct(tc?.neutral_pass_rate)}</span>
            <span className="k">Sack / hit-or-sack</span><span>{pct(tc?.sack_rate, 1)} / {pct(tc?.hit_or_sack_rate, 1)}</span>
            <span className="k">PFR pressure rate</span><span>{pct(tc?.pfr_pressure_rate, 1, "PFR not published for these weeks")}</span>
          </div>
        </div>
        <div className="panel tight">
          <h3>Matchup <Badge value={L.matchup} tone={tone(L.matchup)} /></h3>
          {od ? (
            <div className="kv">
              <span className="k">Opponent</span><span>{od.defteam} <span className="faint">({od.games} games)</span></span>
              <span className="k">Rush EPA allowed</span><span>{num(od.rush_epa_allowed, 3)} <span className="faint">#{od.ranks.rush_epa_allowed}</span></span>
              <span className="k">Pass EPA allowed</span><span>{num(od.pass_epa_allowed, 3)} <span className="faint">#{od.ranks.pass_epa_allowed}</span></span>
              <span className="k">Rush success allowed</span><span>{pct(od.rush_success_allowed)}</span>
              <span className="k">{pos === "TE" ? "TE" : "RB"} target share allowed</span>
              <span>{pct(pos === "TE" ? od.te_target_share_allowed : od.rb_target_share_allowed)}</span>
            </div>
          ) : <p className="faint">{L.matchup === "BYE" ? "On bye this week." : "No opponent context."}</p>}
          <p className="faint" style={{ marginTop: 6 }}>Rank #1 = most generous defense of 32 (season to date).</p>
        </div>
      </div>
      {pos !== "RB" && e.experimental && (
        <details style={{ marginTop: 8 }}>
          <summary>Advanced / experimental</summary>
          <div className="kv faint">
            <span className="k" title={FTN_READ1_NOTE}>{FTN_READ1_LABEL}</span>
            <span>{pct(e.experimental.ftn_read1_target_share, 0, "Not charted by FTN")}{" "}
              <Badge value={e.experimental.read_semantics_status} tone="warn" title={FTN_READ1_NOTE} /></span>
          </div>
          <p className="faint" style={{ marginTop: 4 }}>{FTN_READ1_NOTE}</p>
        </details>
      )}
      <p className="faint" style={{ marginTop: 8 }}>
        Data confidence: <Badge value={L.data_confidence} tone={tone(L.data_confidence)} /> · {e.rules?.data_confidence}
      </p>
    </section>
  );
}


const ROUTE_ROWS: [keyof RouteData["season"], string, "int" | "pct" | "num"][] = [
  ["routes", "Routes", "int"], ["route_participation", "Route %", "pct"], ["tprr", "TPRR", "num"],
  ["yprr", "YPRR", "num"], ["pass_block_snaps", "Pass blocks", "int"], ["pass_block_rate", "Pass-block rate", "pct"],
  ["slot_rate", "Slot %", "pct"], ["wide_rate", "Wide %", "pct"], ["inline_rate", "Inline %", "pct"],
  ["backfield_rate", "Backfield %", "pct"], ["two_minute_route_share", "Two-minute role", "pct"],
  ["long_down_route_share", "Long-down role", "pct"], ["short_down_route_share", "Short-down role", "pct"],
  ["man_tprr", "TPRR vs man", "num"], ["zone_tprr", "TPRR vs zone", "num"],
];

function RoutePanel({ r }: { r: RouteData }) {
  const rows = ROUTE_ROWS.filter(([k]) => r.season[k] !== undefined && r.season[k] !== null);
  return (
    <section className="panel">
      <h2>Route participation <span className="faint" style={{ textTransform: "none" }}>
        · source {r.providers.join(", ")} ({r.datasets.join(", ")}) · updated {when(r.source_updated_at)} · weeks {r.weeks.join(", ")}</span></h2>
      <div className="kv" style={{ maxWidth: 520 }}>
        {rows.map(([k, label, kind]) => {
          const v = r.season[k] as number;
          const providerDefined = r.provider_defined.includes(k);
          return (
            <span key={k} style={{ display: "contents" }}>
              <span className="k">{label}{providerDefined && <> <Badge value="provider-defined" tone="warn"
                title="Definition belongs to the source provider; not normalized to an app-wide definition" /></>}</span>
              <span>{kind === "pct" ? pct(v) : kind === "int" ? int(v) : num(v, 2)}</span>
            </span>
          );
        })}
      </div>
      <p className="faint" style={{ marginTop: 8 }}>
        {r.note} Season totals for counts, week averages for rates. External route metrics are shown for context only
        and do not affect scores or recommendations yet.
      </p>
    </section>
  );
}
