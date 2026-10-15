import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, ErrorBox, Pos } from "@/components/ui";
import { api } from "@/lib/api";
import { label, num, when } from "@/lib/format";
import type { Coverage, Decision, WaiverRow } from "./types";
import styles from "./waivers.module.css";

export const metadata = { title: "Waivers" };

const POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K", "D/ST"];
const VIEWS: Record<string, string> = {
  BEST: "Best moves for my team", FREE_AGENTS: "Free-agent adds", CLAIMS: "Waiver claims",
  STREAMING: "Streaming options", WATCHLIST: "Stashes / watchlist", ALL: "All evaluated",
};
const ACTIONS: Record<string, string> = {
  ADD_NOW: "Add now", WAIVER_CLAIM: "Worth a waiver claim", PRESERVE_PRIORITY: "Preserve priority",
  MONITOR: "Monitor for more evidence", NOT_AN_UPGRADE: "Not an upgrade", NO_LEGAL_MOVE: "No legal move identified",
  VERIFY_LEGALITY: "Verify roster restrictions", VERIFY_COST: "Verify acquisition rules", SEASON_COMPLETE: "No actionable week remaining",
};

function href(pos: string, view: string, offset = 0, rec = "ALL") {
  const qs = new URLSearchParams({ view });
  if (pos !== "ALL") qs.set("pos", pos);
  if (offset) qs.set("offset", String(offset));
  if (rec !== "ALL") qs.set("rec", rec);
  return `/waivers?${qs}`;
}

function Evidence({ row, decision: d }: { row: WaiverRow; decision: Decision }) {
  const stream = d.streaming;
  return <details className={styles.evidence}>
    <summary>Evidence, alternatives &amp; trade-offs</summary>
    <div className={styles.evidenceBody}>
      <p><b>Player quality.</b> {row.components.player.basis_description ?? row.components.player.basis}; opportunity {num(row.opportunity_score)}/10, historical value {num(row.components.player.value)}/10. These are heuristics, not claim-success probabilities.</p>
      <ul>{row.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      <p><b>Roster benefit.</b> {d.utility.summary}</p>
      <p>Future lineup gain: {num(d.utility.forward.total_gain)} ESPN points across {d.utility.forward.weeks.length} comparable weeks.
        {d.utility.forward.missing_weeks.length > 0 && ` ${d.utility.forward.missing_weeks.length} weeks lack complete comparisons.`}</p>
      {d.roster_move.drop && <ul>{d.roster_move.drop.sacrifice.map((s) => <li key={s}>{s}</li>)}</ul>}
      {d.roster_move.alternatives.length > 1 && <p>Other defensible drops: {d.roster_move.alternatives.slice(1, 4).map((drop, i) => <span key={drop.espn_player_id}>{i > 0 && ", "}<PlayerLink id={drop.espn_player_id} name={drop.name} /></span>)}.</p>}
      {d.roster_move.excluded.length > 0 && <details><summary>Why other drops were excluded</summary><ul>{d.roster_move.excluded.map((drop) => <li key={drop.espn_player_id}><PlayerLink id={drop.espn_player_id} name={null} />: {drop.reason}</li>)}</ul></details>}
      {stream && <>
        <p><b>{label(stream.category)}.</b> {stream.summary}</p>
        <p>Best other available projection: {num(stream.alternative_points)}. Opponent implied total: {num(stream.opponent_implied_total)}. Team implied total: {num(stream.team_implied_total)}. Lines provide context; they are not converted into invented fantasy points.</p>
        {stream.opponent_offense && <p>Opponent offense: sack rate {stream.opponent_offense.sack_rate === null ? "unknown" : `${num(stream.opponent_offense.sack_rate * 100, 1)}%`} across {stream.opponent_offense.games} stored games.</p>}
        {stream.defense_performance && <p>Defense context across {stream.defense_performance.games} games: pass EPA allowed/play {num(stream.defense_performance.pass_epa_allowed, 2)}, rush EPA allowed/play {num(stream.defense_performance.rush_epa_allowed, 2)}. Context only; not a fantasy-points adjustment.</p>}
        <ul>{stream.weeks.map((w) => <li key={w.week}>Week {w.week}: {w.bye ? "bye" : w.opponent ? `vs ${w.opponent}` : "opponent unknown"}; projected {num(w.points)}, your lineup {num(w.incumbent_points)}.</li>)}</ul>
        <p>Projection snapshot: {when(stream.projection_synced_at)}.</p>
      </>}
      <p><b>Competition: {row.league_interest.toLowerCase()} interest.</b> Historical tendencies and needs; no prediction of bids or claim success.</p>
      <ul>{row.competitors.map((c) => <li key={c.team_id}><Link href={`/managers/${c.team_id}`}>{c.name}</Link>{c.reasons.length ? `: ${c.reasons.join("; ")}` : ": limited behavioral evidence"}</li>)}</ul>
      <p><b>Known capacity:</b> {d.roster_move.active_count}/{d.roster_move.capacity ?? "unknown"} active players. IR is excluded from active capacity. Position limits: {Object.entries(d.roster_move.position_limits).map(([p, n]) => `${p} ${n}`).join(", ") || "none supplied"}.</p>
      {d.risks.length > 0 && <><b>Evidence limits</b><ul>{d.risks.map((r) => <li key={r}>{r}</li>)}</ul></>}
      <p>Each pickup is a separate scenario against your current roster. Refresh after making a move; these are not a combined claim queue.</p>
    </div>
  </details>;
}

export default async function WaiversPage({ searchParams }: PageProps<"/waivers">) {
  const sp = await searchParams;
  const pos = typeof sp.pos === "string" && POSITIONS.includes(sp.pos) ? sp.pos : "ALL";
  const view = typeof sp.view === "string" && sp.view in VIEWS ? sp.view : "BEST";
  const rec = typeof sp.rec === "string" && ["CLAIM", "WAIT", "IGNORE"].includes(sp.rec) ? sp.rec : "ALL";
  const offset = typeof sp.offset === "string" ? Math.max(0, Number.parseInt(sp.offset, 10) || 0) : 0;
  const qs = new URLSearchParams({ view, limit: "30", offset: String(offset) });
  if (pos !== "ALL") qs.set("position", pos);
  if (rec !== "ALL") qs.set("recommendation", rec);
  const res = await api<{ players: WaiverRow[]; computed_at: string | null; coverage: Coverage | null; total: number }>(`/api/waivers?${qs}`);
  if (!res.ok) return <ErrorBox error={res.error} />;
  const { players, computed_at, coverage, total } = res.data;

  return <div className={`stack ${styles.page}`}>
    <div className="page-head"><h1>Waiver intelligence</h1><span className="sub">Moves for your roster · computed {when(computed_at)}</span></div>
    <p>Find an upgrade, understand the roster trade-off, and decide whether to spend waiver resources.</p>
    <nav className="filters" aria-label="Waiver decision views">{Object.entries(VIEWS).map(([key, name]) => <Link key={key} className={`chip ${view === key ? "active" : ""}`} aria-current={view === key ? "page" : undefined} href={href(pos, key)}>{name}</Link>)}</nav>
    <nav className="filters" aria-label="Player positions">{POSITIONS.map((p) => <Link key={p} className={`chip ${pos === p ? "active" : ""}`} aria-current={pos === p ? "page" : undefined} href={href(p, view)}>{p}</Link>)}</nav>
    <div className={styles.context}>
      {coverage ? <p>{coverage.evaluated} of {coverage.eligible} eligible players in the stored pool evaluated · {coverage.excluded_by_limit} excluded by the per-position evaluation limit. <span>{coverage.limitation}</span></p> : <p>V3 coverage is unavailable. Run the analytics refresh to generate decision diagnostics.</p>}
      <p>Historical value and xFP are PPR-based heuristics. Forward ESPN projections use your league scoring. Advice reflects stored snapshots; verify transaction locks and current availability in ESPN.</p>
    </div>
    <section className={styles.list} aria-label={VIEWS[view]}>
      <h2 className={styles.sectionTitle}>{VIEWS[view]} <span>{total} {total === 1 ? "player" : "players"}</span></h2>
      {players.map((w) => {
        const d = w.components.decision;
        return <article key={w.espn_player_id} className={styles.move}>
          <div className={styles.identity}>
            <h3><PlayerLink id={w.espn_player_id} name={w.full_name} /> <Pos value={w.position} /></h3>
            <p className={styles.meta}>{w.pro_team ?? "Team unknown"} · {label(w.availability_status)} · {num(w.percent_owned, 1)}% owned</p>
            {w.injury_status && !["ACTIVE", "NORMAL"].includes(w.injury_status) && <Badge value={w.injury_status} />}
          </div>
          {d ? <div>
            <div className={styles.action}><strong>{ACTIONS[d.action] ?? label(d.action)}</strong><Badge value={d.confidence === "SUPPORTED" ? "Evidence supported" : "Limited evidence"} tone={d.confidence === "SUPPORTED" ? "info" : "warn"} /></div>
            <p>{d.streaming?.summary ?? d.utility.summary}</p>
            {d.streaming && <p>{label(d.streaming.category)}</p>}
            {d.utility.stash && <p>Potential bench stash · future role remains uncertain.</p>}
            <p>{d.roster_move.summary}</p>
            {d.roster_move.drop && <p>Give up <PlayerLink id={d.roster_move.drop.espn_player_id} name={d.roster_move.drop.name} /> <Pos value={d.roster_move.drop.position} /></p>}
            {d.roster_move.status === "UNKNOWN" && <p>Missing drop information does not establish an open spot.</p>}
            <p className={styles.cost}>{d.resources.summary}</p>
            {d.resources.alternative && <p>Compare <PlayerLink id={d.resources.alternative.espn_player_id} name={d.resources.alternative.name} /> before using priority.</p>}
            {d.risks.length > 0 && <p className={styles.risk}>Evidence limits: {d.risks[0]}{d.risks.length > 1 && ` (${d.risks.length - 1} more below)`}</p>}
            <Evidence row={w} decision={d} />
          </div> : <div><p>Refresh analytics to generate roster legality and acquisition advice.</p><p>Existing recommendation: {w.recommendation}. An absent drop does not establish an open roster spot.</p></div>}
        </article>;
      })}
      {players.length === 0 && <div className={styles.empty}><h3>{view === "BEST" ? "No supported actionable pickups in this view" : "No players match this view"}</h3><p>Check the watchlist for uncertain moves, or inspect all evaluated players and their constraints. Refresh ESPN and NFL data if snapshots are missing.</p><Link href={href(pos, view === "WATCHLIST" ? "ALL" : "WATCHLIST")}>Browse {view === "WATCHLIST" ? "all evaluated players" : "stashes and watchlist"} →</Link></div>}
    </section>
    <nav className="row-flex" aria-label="Waiver pages">
      {offset > 0 && <Link className="chip" href={href(pos, view, Math.max(0, offset - 30), rec)}>Previous</Link>}
      <span className="sub">{total ? `${offset + 1}–${Math.min(offset + players.length, total)} of ${total}` : "0 results"}</span>
      {offset + players.length < total && <Link className="chip" href={href(pos, view, offset + 30, rec)}>Next</Link>}
    </nav>
  </div>;
}
