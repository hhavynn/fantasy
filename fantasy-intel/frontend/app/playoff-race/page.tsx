import Link from "next/link";
import { Fragment } from "react";
import { Badge, Callout, ErrorBox } from "@/components/ui";
import { api } from "@/lib/api";
import type { PlayoffRace, PrLeverage, PrMatchup, PrOutcome, PrPlace, PrRecord, PrStatus, PrTeam } from "@/lib/types";

export const metadata = { title: "Playoff race" };

// Playoff Race Lab: actual standings and all-play, projected remaining matchups (ESPN weekly projections
// through the canonical lineup optimizer), one deterministic projected scenario and URL-backed overrides.
// Nothing here is a probability; no clinch or elimination is claimed.

const AHEAD = "#4a96ec";     // projected ahead (diverging pole, validated on the dark panel surface)
const BEHIND = "#d9762f";    // projected behind
const NEUTRAL = "#6e7681";   // other teams in the ladder (emphasis: your team in AHEAD)

type Q = { team?: string; base: string; o: Record<string, string> };

function many(v: string | string[] | undefined): string[] {
  return v === undefined ? [] : Array.isArray(v) ? v : [v];
}

function href(q: Q, change: Partial<Q> = {}): string {
  const n = { ...q, ...change };
  const p = new URLSearchParams();
  if (n.team) p.set("team", n.team);
  if (n.base !== "projected") p.set("base", n.base);
  Object.entries(n.o).sort(([a], [b]) => Number(a) - Number(b)).forEach(([mid, out]) => p.append("o", `${mid}:${out}`));
  const s = p.toString();
  return `/playoff-race${s ? `?${s}` : ""}`;
}

function withOverride(q: Q, mid: number, outcome: string | null): string {
  const o = { ...q.o };
  if (outcome === null) delete o[String(mid)];
  else o[String(mid)] = outcome;
  return href(q, { o });
}

function rec(r: PrRecord | null | undefined): string {
  if (!r) return "—";
  return `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}`;
}

function pts(v: number | null | undefined, d = 1): string {
  return v === null || v === undefined ? "—" : v.toFixed(d);
}

function signed(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}`;
}

// Row index BEFORE which the dashed cutline is drawn: the first row past the last spot, or the first
// row of a tied group that straddles it (that group shares the remaining spots; no tiebreak picks
// among them). 0 = above the first row (e.g. every team tied at 0-0); not drawn when it is past the end.
// The position comes from the statuses, never from row order alone: no line when no team has a decided
// game yet (nothing establishes a cutline position).
function cutAfter(d: PlayoffRace): number | null {
  if (d.league.playoff_teams === null) return null;
  const rows = d.scenario.projected_standings;
  if (rows.every((x) => x.status === null)) return null;
  return rows.filter((x) => x.status === "inside").length;      // inside teams come first in the order
}

const STATUS_TEXT: Record<string, string> = { inside: "inside cutline", outside: "outside", cutline_tie: "tied at cutline" };
const STATUS_TONE: Record<string, string> = { inside: "good", outside: "bad", cutline_tie: "warn" };

function StatusBadge({ s, title }: { s: PrStatus | undefined; title?: string }) {
  if (!s) return <span className="missing" title={title ?? "No cutline: playoff spots unknown or record unknown"}>—</span>;
  return <Badge value={STATUS_TEXT[s]} tone={STATUS_TONE[s]} title={title} />;
}

export default async function PlayoffRacePage({ searchParams }: PageProps<"/playoff-race">) {
  const sp = await searchParams;
  const team = many(sp.team)[0];
  const base = many(sp.base)[0] === "actual" ? "actual" : "projected";
  const o: Record<string, string> = {};
  for (const v of many(sp.o)) {
    const [mid, out] = v.split(":");
    if (mid && out) o[mid] = out;
  }
  const q: Q = { team, base, o };
  const p = new URLSearchParams();
  if (team) p.set("team_id", team);
  p.set("base", base);
  Object.entries(o).forEach(([mid, out]) => p.append("override", `${mid}:${out}`));
  const res = await api<PlayoffRace>(`/api/playoff-race?${p.toString()}`);
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Playoff Race Lab</h1>
        <span className="sub">Actual standings, projected remaining matchups and what-if scenarios · deterministic, not probabilities</span>
      </div>
      {!res.ok ? <><ErrorBox error={res.error} />{Object.keys(o).length > 0 && <p><Link href={href({ ...q, o: {} })}>Clear all overrides</Link></p>}</>
        : <Body d={res.data} q={q} />}
    </div>
  );
}

function Body({ d, q }: { d: PlayoffRace; q: Q }) {
  const teams = new Map(d.teams.map((t) => [t.team_id, t]));
  const focus = d.focus_team_id !== null ? teams.get(d.focus_team_id) : undefined;
  const name = (id: number | null | undefined) => (id === null || id === undefined ? "?" : teams.get(id)?.name ?? `Team ${id}`);
  const open = d.matchups.regular.filter((m) => m.status !== "final" && m.status !== "bye");
  return (
    <>
      <p className="faint" style={{ margin: 0 }}>
        {d.league.name} {d.league.season} · ESPN week {d.league.current_week}
        {d.league.reg_season_matchups !== null && <> · regular season: {d.league.reg_season_matchups} matchup periods
          {d.league.last_regular_season_week !== null && <> (through week {d.league.last_regular_season_week})</>}</>}
        {" "}· playoff spots {d.league.playoff_teams ?? <b>unknown</b>} (league settings)
        {" "}· first projectable week {d.window.first_actionable_week ?? "none"}
      </p>
      {d.warnings.map((w, i) => <Callout key={i}>{w}</Callout>)}

      <ScenarioBar d={d} q={q} name={name} />
      {focus && <Spotlight d={d} t={focus} q={q} name={name} />}

      <div className="grid grid-2">
        <Standings d={d} name={name} />
        <Ladder d={d} name={name} />
      </div>

      {focus && <Margins t={focus} />}
      <Race d={d} q={q} name={name} />
      <Matchups d={d} q={q} open={open} />
      <Power d={d} />
      {d.matchups.playoff_known.length > 0 && <KnownPlayoffs d={d} />}
      <Method d={d} />
    </>
  );
}

// ------------------------------------------------------------------- scenario controls --

function ScenarioBar({ d, q, name }: { d: PlayoffRace; q: Q; name: (id: number | null) => string }) {
  const s = d.scenario;
  const byId = new Map(d.matchups.regular.map((m) => [m.matchup_id, m]));
  return (
    <section className="panel">
      <h2>Scenario</h2>
      <div className="row-flex">
        <span>Baseline:</span>
        <Link href={href(q, { base: "projected" })}><Badge value="higher projection wins" tone={q.base === "projected" ? "info" : ""} /></Link>
        <Link href={href(q, { base: "actual" })}><Badge value="actual results only" tone={q.base === "actual" ? "info" : ""} /></Link>
        <span className="faint">· {s.decided_open_games} remaining game(s) decided in this scenario, {s.unresolved_open_games} unresolved</span>
        <form action="/playoff-race" className="row-flex" style={{ marginLeft: "auto" }}>
          {q.base !== "projected" && <input type="hidden" name="base" value={q.base} />}
          {Object.entries(q.o).map(([mid, out]) => <input key={mid} type="hidden" name="o" value={`${mid}:${out}`} />)}
          <label>Spotlight <select name="team" defaultValue={d.focus_team_id ?? ""}>
            {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
          </select></label>
          <button type="submit">Show</button>
        </form>
      </div>
      {s.overrides.length > 0 ? (
        <div className="row-flex" style={{ marginTop: 8 }}>
          <span>Your overrides:</span>
          {s.overrides.map((ov) => {
            const m = byId.get(ov.matchup_id);
            const who = ov.outcome === "home" ? name(m?.home_team_id ?? null) : ov.outcome === "away" ? name(m?.away_team_id ?? null) : null;
            return (
              <span key={ov.matchup_id} className="badge info">
                wk {m?.weeks?.join("–") ?? m?.matchup_period}: {who ? `${who} wins` : ov.outcome === "tie" ? "tie" : "unresolved"}
                {" "}<Link href={withOverride(q, ov.matchup_id, null)} title="Remove this override">✕</Link>
              </span>
            );
          })}
          <Link href={href(q, { o: {} })}>clear all</Link>
        </div>
      ) : <p className="faint" style={{ margin: "8px 0 0" }}>No overrides. Use the buttons in “Remaining matchups” (or a spotlight game) to set a winner; each choice is in the URL, so the back button undoes it.</p>}
    </section>
  );
}

// ----------------------------------------------------------------------- spotlight --

function Place({ label, p, hint }: { label: string; p: PrPlace; hint?: string }) {
  return (
    <div className="stat" title={hint}>
      <div className="label">{label}</div>
      <div className="value">{rec(p.record)}{p.rank !== null && <span className="faint" style={{ fontSize: 13 }}> · #{p.rank}</span>}</div>
      <StatusBadge s={p.status} />
    </div>
  );
}

function Spotlight({ d, t, q, name }: { d: PlayoffRace; t: PrTeam; q: Q; name: (id: number | null) => string }) {
  const f = d.scenario.focus;
  const lev = d.scenario.leverage[String(t.team_id)] ?? [];
  const high = lev.filter((g) => g.high_leverage);
  const fewest = f?.fewest_wins_to_get_inside;
  return (
    <section className="panel" style={{ borderColor: AHEAD }}>
      <h2>{t.name}{t.is_mine ? " (your team)" : ""}</h2>
      {f && (
        <div className="grid grid-4">
          <div className="stat">
            <div className="label">Now (ESPN)</div>
            <div className="value">{rec(t.record)}<span className="faint" style={{ fontSize: 13 }}> · seed {t.espn_seed ?? "—"}</span></div>
            <StatusBadge s={t.current.status} title="Decided games only" />
          </div>
          <Place label={q.base === "projected" ? "This scenario" : "Actual + overrides"} p={f.scenario} />
          <Place label={`Win all ${f.open_games} open`} p={f.win_out} hint="Every remaining game of this team won; all other results as in the scenario" />
          <Place label={`Lose all ${f.open_games} open`} p={f.lose_out} hint="Every remaining game of this team lost; all other results as in the scenario" />
        </div>
      )}
      {f && (
        <ul className="reasons" style={{ marginTop: 10 }}>
          {fewest && fewest.wins_needed !== null && (
            <li>Fewest wins among the {fewest.of_games} open game(s) that put {t.name} inside the cutline, every other result as in this
              scenario: <b>{fewest.wins_needed}</b>{fewest.games.length > 0 && <> (for example weeks {fewest.games.map((id) => {
                const g = lev.find((x) => x.matchup_id === id);
                return g ? `${g.weeks?.join("–") ?? g.matchup_period} vs ${name(g.opponent_id)}` : id;
              }).join(", ")})</>}.</li>)}
          {fewest && fewest.wins_needed === null && fewest.note && <li>{fewest.note}: the fewest-wins search was skipped.</li>}
          {!fewest && f.win_out.status !== "inside" && d.league.playoff_teams && f.open_games > 0 && (
            <li>Winning every open game does not put {t.name} inside the cutline under the other results of this scenario; see results elsewhere that help.</li>)}
          {f.open_games === 0 && <li>No open regular-season games remain for {t.name}.</li>}
        </ul>
      )}
      {high.length > 0 && <>
        <h3 style={{ marginTop: 10 }}>High-leverage games (the result changes {t.name}&apos;s cutline status)</h3>
        <LeverageTable rows={high} q={q} name={name} d={d} tid={t.team_id} />
      </>}
      {f && f.helpful_other_results.length > 0 && <>
        <h3 style={{ marginTop: 10 }}>Results elsewhere that help {t.name}</h3>
        <ul className="reasons">
          {f.helpful_other_results.map((h) => {
            const m = d.matchups.regular.find((x) => x.matchup_id === h.matchup_id);
            const winner = h.result === "home" ? m?.home_team_id : m?.away_team_id;
            return (
              <li key={`${h.matchup_id}-${h.result}`}>
                Week {m?.weeks?.join("–") ?? h.matchup_period}: {name(winner ?? null)} beats {name(h.loser_id)} → {t.name} #{h.rank} <StatusBadge s={h.status} />
                {" "}<Link href={withOverride(q, h.matchup_id, h.result)}>apply</Link>
              </li>
            );
          })}
        </ul>
      </>}
    </section>
  );
}

function LeverageTable({ rows, q, name, d, tid }: { rows: PrLeverage[]; q: Q; name: (id: number | null) => string; d: PlayoffRace; tid: number }) {
  const byId = new Map(d.matchups.regular.map((m) => [m.matchup_id, m]));
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Week</th><th>Opponent</th><th>In this scenario</th><th>If win</th><th>If loss</th><th>Try it</th></tr></thead>
        <tbody>
          {rows.map((g) => {
            const m = byId.get(g.matchup_id);
            const side = m?.home_team_id === tid ? "home" : "away";
            const other = side === "home" ? "away" : "home";
            return (
              <tr key={g.matchup_id}>
                <td>{g.weeks?.join("–") ?? g.matchup_period}</td>
                <td>{name(g.opponent_id)}{g.close && <> <Badge value="close" tone="warn" /></>}</td>
                <td>{g.scenario_result ?? <span className="faint">unresolved ({g.status.replace("_", " ")})</span>}</td>
                <td>#{g.if_win.rank} <StatusBadge s={g.if_win.status} /></td>
                <td>#{g.if_loss.rank} <StatusBadge s={g.if_loss.status} /></td>
                <td><Link href={withOverride(q, g.matchup_id, side)}>win</Link> · <Link href={withOverride(q, g.matchup_id, other)}>loss</Link></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ----------------------------------------------------------------------- standings --

function Standings({ d, name }: { d: PlayoffRace; name: (id: number | null) => string }) {
  const s = d.scenario;
  const spots = d.league.playoff_teams;
  const after = cutAfter(d);
  const tie = s.cutline?.cutline_tie;
  const cur = new Map(s.current_standings.map((x) => [x.team_id, x]));
  const teams = new Map(d.teams.map((t) => [t.team_id, t]));
  return (
    <section className="panel">
      <h2>Current vs {s.base === "projected" ? "projected" : "scenario"} standings</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Team</th><th>Now</th><th>ESPN seed</th><th>Scenario</th><th>Move</th><th>Status</th></tr></thead>
          <tbody>
            {s.projected_standings.map((x, i) => {
              const c = cur.get(x.team_id);
              const move = c?.rank && x.rank ? c.rank - x.rank : null;
              const t = teams.get(x.team_id);
              return (
                <Fragment key={x.team_id}>
                  {after !== null && i === after && (
                    <tr><td colSpan={7} style={{ borderBottom: `2px dashed ${AHEAD}`, padding: 0 }}
                            title={tie ? `Cutline: ${tie.spots_left} spot(s) shared by the tied teams below` : `Playoff cutline: ${spots} spots`} /></tr>
                  )}
                  <tr className={t?.is_mine ? "mine" : ""}>
                    <td>{x.rank ?? "—"}{x.tied ? "=" : ""}</td>
                    <td>{name(x.team_id)}</td>
                    <td className="num">{rec(c?.record)}{c?.rank ? <span className="faint"> #{c.rank}{c.tied ? "=" : ""}</span> : null}</td>
                    <td className="num">{t?.espn_seed ?? "—"}</td>
                    <td className="num"><b>{rec(x.record)}</b>{x.record?.unresolved ? <span className="faint" title="Games unresolved in this scenario"> +{x.record.unresolved}?</span> : null}</td>
                    <td className="num">{move === null || move === 0 ? <span className="faint">·</span> : move > 0 ? <span style={{ color: AHEAD }}>▲{move}</span> : <span style={{ color: BEHIND }}>▼{-move}</span>}</td>
                    <td><StatusBadge s={x.status} /></td>
                  </tr>
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="faint" style={{ marginTop: 6 }}>“=” marks a shared rank (equal winning percentage; no tiebreak applied). “+n?” counts games still unresolved in this scenario. {after === null
        ? (spots === null ? " Playoff spots are unknown, so no cutline is drawn." : " No team has a decided game in this scenario yet, so no cutline position is drawn.")
        : <> The dashed line is the cutline after {spots} spots{tie ? `; a tied group straddles it, so it is drawn above them and they share ${tie.spots_left} spot(s)` : ""}.</>}</p>
    </section>
  );
}

function Ladder({ d, name }: { d: PlayoffRace; name: (id: number | null) => string }) {
  const rows = d.scenario.projected_standings;
  const after = cutAfter(d);
  const tie = d.scenario.cutline?.cutline_tie;
  const mine = new Set(d.teams.filter((t) => t.team_id === d.focus_team_id).map((t) => t.team_id));
  const rowH = 22, labelW = 150, barW = 260, top = 16;
  const h = top + rows.length * rowH + 8;
  return (
    <section className="panel">
      <h2>Cutline ladder · scenario winning %</h2>
      <svg viewBox={`0 0 ${labelW + barW + 110} ${h}`} width="100%" role="img" aria-label="Scenario winning percentage by team with the playoff cutline">
        {[0, 0.5, 1].map((v) => (
          <line key={v} x1={labelW + v * barW} x2={labelW + v * barW} y1={top - 4} y2={h - 6} stroke="var(--border)" strokeWidth={1} />
        ))}
        {rows.map((x, i) => {
          const y = top + i * rowH;
          const w = Math.max(2, (x.pct ?? 0) * barW);
          return (
            <g key={x.team_id}>
              <title>{`${name(x.team_id)}: ${rec(x.record)} (${x.pct === null ? "no record" : (x.pct * 100).toFixed(1) + "%"})${x.status ? ` · ${STATUS_TEXT[x.status]}` : ""}`}</title>
              <text x={labelW - 8} y={y + 14} textAnchor="end" fontSize={11} fill={mine.has(x.team_id) ? "var(--text)" : "var(--muted)"}>
                {(name(x.team_id) ?? "").slice(0, 20)}
              </text>
              {x.pct !== null && <rect x={labelW} y={y + 4} width={w} height={rowH - 8} rx={4} fill={mine.has(x.team_id) ? AHEAD : NEUTRAL} />}
              <text x={labelW + barW + 6} y={y + 14} fontSize={11} fill="var(--muted)">{rec(x.record)}</text>
              {after !== null && i === after && (
                <g>
                  <line x1={labelW - 4} x2={labelW + barW + 106} y1={y} y2={y} stroke={AHEAD} strokeWidth={2} strokeDasharray="4 3" />
                  <text x={labelW + barW + 106} y={y - 4} fontSize={10} textAnchor="end" fill="var(--text)">{tie ? `cutline · ${tie.spots_left} shared` : "cutline"}</text>
                </g>
              )}
            </g>
          );
        })}
      </svg>
      <p className="faint" style={{ marginTop: 6 }}>Bars: winning % in this scenario (decided games only), {d.focus_team_id !== null ? `${name(d.focus_team_id)} highlighted` : "no spotlight team"}. Gridlines at 0, 50 and 100%.</p>
    </section>
  );
}

// ------------------------------------------------------------- spotlight margins chart --

function Margins({ t }: { t: PrTeam }) {
  const rows = t.remaining.filter((r) => r.status !== "bye");
  if (rows.length === 0) return null;
  const maxAbs = Math.max(5, ...rows.map((r) => Math.abs(r.projected_margin ?? 0)));
  const colW = 64, h = 150, mid = 70, scale = 55 / maxAbs;
  return (
    <section className="panel">
      <h2>{t.name}: projected margin by remaining matchup</h2>
      <svg viewBox={`0 0 ${rows.length * colW + 20} ${h}`} width="100%" style={{ maxWidth: rows.length * colW + 20 }} role="img" aria-label="Projected scoring margin per remaining matchup">
        <line x1={0} x2={rows.length * colW + 20} y1={mid} y2={mid} stroke="var(--border)" strokeWidth={1} />
        {rows.map((r, i) => {
          const x = 10 + i * colW;
          const m = r.projected_margin;
          const bh = m === null ? 0 : Math.max(2, Math.abs(m) * scale);
          return (
            <g key={r.matchup_id}>
              <title>{`Week ${r.weeks?.join("–") ?? r.matchup_period} ${r.home ? "vs" : "@"} ${r.opponent_name}: ${m === null ? "no projected margin (" + r.status.replace("_", " ") + ")" : signed(m) + " projected"}${r.close ? " · close" : ""}`}</title>
              {m !== null ? (
                <rect x={x + 14} width={colW - 28} y={m >= 0 ? mid - bh : mid} height={bh} rx={4} fill={m >= 0 ? AHEAD : BEHIND} />
              ) : (
                <text x={x + colW / 2} y={mid + 4} textAnchor="middle" fontSize={11} fill="var(--faint)">gap</text>
              )}
              {m !== null && <text x={x + colW / 2} y={m >= 0 ? mid - bh - 4 : mid + bh + 12} textAnchor="middle" fontSize={10} fill="var(--muted)">{signed(m)}</text>}
              <text x={x + colW / 2} y={h - 18} textAnchor="middle" fontSize={10} fill="var(--muted)">wk {r.weeks?.join("–") ?? r.matchup_period}</text>
              <text x={x + colW / 2} y={h - 5} textAnchor="middle" fontSize={10} fill="var(--faint)">{(r.home ? "vs " : "@ ") + (r.opponent_name ?? "?").slice(0, 9)}</text>
            </g>
          );
        })}
      </svg>
      <p className="faint" style={{ marginTop: 6 }}>
        <span style={{ color: AHEAD }}>■</span> projected ahead · <span style={{ color: BEHIND }}>■</span> projected behind · “gap” = no projected margin (game under way,
        a projection is incomplete, or its weeks are unknown). Margins compare ESPN-projected optimal lineups; they are not win probabilities.
      </p>
    </section>
  );
}

// -------------------------------------------------------------------- race & leverage --

function group(rows: { team_id: number; record: PrRecord | null }[], name: (id: number | null) => string): string {
  return rows.length ? rows.map((x) => `${name(x.team_id)} ${rec(x.record)}`).join(" = ") : "—";
}

function Race({ d, q, name }: { d: PlayoffRace; q: Q; name: (id: number | null) => string }) {
  const cut = d.scenario.cutline;
  if (!cut) return <Callout>Playoff spots are not reported in league settings, so no cutline or bubble can be shown.</Callout>;
  return (
    <section className="panel">
      <h2>Playoff race · {cut.playoff_spots} spots</h2>
      <p>
        {cut.cutline_tie ? <>
          Tied at the cutline: <b>{cut.cutline_tie.team_ids.map((id) => name(id)).join(", ")}</b> share {cut.cutline_tie.spots_left} remaining
          spot(s) <Badge value="unresolved: no tiebreak applied" tone="warn" />
        </> : <>
          Last in: <b>{group(cut.last_inside, name)}</b> · first out: <b>{group(cut.first_outside, name)}</b>
        </>}
      </p>
      <p className="faint">Bubble teams: those with at least one remaining game whose result changes their cutline status (all else as in this scenario), plus any team tied at the cutline.</p>
      {cut.bubble.length === 0 ? <p className="faint">No team&apos;s status depends on a single result in this scenario.</p> : (
        <div className="grid grid-3">
          {cut.bubble.map((tid) => {
            const rows = (d.scenario.leverage[String(tid)] ?? []).filter((g) => g.high_leverage);
            const proj = d.scenario.projected_standings.find((x) => x.team_id === tid);
            return (
              <div key={tid} className="panel tight">
                <h3>{name(tid)} <span className="faint">{rec(proj?.record)} #{proj?.rank}</span> <StatusBadge s={proj?.status} /></h3>
                <ul className="reasons">
                  {rows.map((g) => (
                    <li key={g.matchup_id}>wk {g.weeks?.join("–") ?? g.matchup_period} vs {name(g.opponent_id)}: win → <StatusBadge s={g.if_win.status} />, loss → <StatusBadge s={g.if_loss.status} />
                      {" "}<span className="faint">({g.scenario_result ?? "unresolved"})</span></li>
                  ))}
                  {rows.length === 0 && <li>Tied at the cutline; no single own result decides it.</li>}
                </ul>
                {tid !== d.focus_team_id && <Link href={href(q, { team: String(tid) })}>spotlight this team</Link>}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ remaining matchups --

function SideCell({ m, side }: { m: PrMatchup; side: "home" | "away" }) {
  const s = side === "home" ? m.home_projection : m.away_projection;
  if (m.status === "in_progress") {
    const v = side === "home" ? m.home_points : m.away_points;
    return <span className="faint" title="ESPN points as of the last sync; not final">{pts(v)} live</span>;
  }
  if (!s) return <span className="faint">—</span>;
  if (s.projected_points !== null) return <b className="num">{pts(s.projected_points)}</b>;
  return <span className="faint" title={`${s.gap ?? "incomplete"}${s.missing_players.length ? ": " + s.missing_players.join(", ") : ""}`}>known {pts(s.known_points)} · incomplete</span>;
}

// Bars share one scale: the largest projected margin among the remaining matchups fills half the width.
function MarginBar({ m, maxAbs }: { m: PrMatchup; maxAbs: number }) {
  if (m.projected_margin === null || m.projected_margin === undefined) return <span className="faint">—</span>;
  const w = Math.max(2, (Math.abs(m.projected_margin) / maxAbs) * 50);
  const home = m.projected_margin >= 0;
  return (
    <svg width={104} height={12} role="img" aria-label={`Projected margin ${signed(m.projected_margin)}`}>
      <title>{`Projected margin ${signed(m.projected_margin)} (home minus away)`}</title>
      <line x1={52} x2={52} y1={0} y2={12} stroke="var(--border)" />
      <rect x={home ? 52 - w : 52} y={2} width={w} height={8} rx={3} fill={home ? AHEAD : BEHIND} />
    </svg>
  );
}

function OverrideLinks({ m, q }: { m: PrMatchup; q: Q }) {
  const cur = q.o[String(m.matchup_id)];
  const opt = (out: PrOutcome | "none", label: string) => (
    cur === out ? <b key={out}>{label}</b> : <Link key={out} href={withOverride(q, m.matchup_id, out)}>{label}</Link>
  );
  return (
    <span className="row-flex" style={{ gap: 6 }}>
      {opt("home", "home wins")}{opt("away", "away wins")}{opt("tie", "tie")}{opt("none", "unresolved")}
      {cur !== undefined && <Link href={withOverride(q, m.matchup_id, null)} title="Back to the baseline">reset</Link>}
    </span>
  );
}

function Matchups({ d, q, open }: { d: PlayoffRace; q: Q; open: PrMatchup[] }) {
  const periods = [...new Set(open.map((m) => m.matchup_period))].sort((a, b) => a - b);
  const maxAbs = Math.max(1, ...open.map((m) => Math.abs(m.projected_margin ?? 0)));
  return (
    <section className="panel">
      <h2>Remaining regular-season matchups</h2>
      {open.length === 0 ? <p className="faint">No remaining regular-season matchups in the ESPN schedule.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Week</th><th>Home</th><th className="num">Proj.</th><th>Margin</th><th className="num">Proj.</th><th>Away</th><th>Scenario</th><th>Override</th></tr></thead>
            <tbody>
              {periods.flatMap((p) => open.filter((m) => m.matchup_period === p).map((m) => {
                const res = m.scenario_result;
                const mine = d.teams.find((t) => t.team_id === d.focus_team_id);
                const involves = mine && (m.home_team_id === mine.team_id || m.away_team_id === mine.team_id);
                return (
                  <tr key={m.matchup_id} className={involves ? "mine" : ""}>
                    <td>{m.weeks?.join("–") ?? `period ${m.matchup_period}`}{m.weeks && m.weeks.length > 1 && <div className="faint" style={{ fontSize: 11 }}>{m.weeks.length}-week matchup</div>}</td>
                    <td>{m.home_name}</td>
                    <td className="num"><SideCell m={m} side="home" /></td>
                    <td><MarginBar m={m} maxAbs={maxAbs} /> <span className="faint">{signed(m.projected_margin)}</span>{m.close && <> <Badge value="close" tone="warn" title="Projected margin within 5% of the mean projected total" /></>}</td>
                    <td className="num"><SideCell m={m} side="away" /></td>
                    <td>{m.away_name}</td>
                    <td className="wrap" style={{ minWidth: 160 }}>
                      {res ? <b>{res === "tie" ? "tie" : `${res === "home" ? m.home_name : m.away_name} wins`}</b> : <span className="faint">unresolved</span>}
                      {m.override ? <Badge value="override" tone="info" /> : res ? <span className="faint"> (projection)</span> : null}
                      <div className="faint" style={{ fontSize: 11 }}>{m.status.replace("_", " ")}{m.status !== "upcoming" || !res ? ` · ${m.detail ?? ""}` : ""}</div>
                    </td>
                    <td><OverrideLinks m={m} q={q} /></td>
                  </tr>
                );
              }))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------- power rankings --

function Power({ d }: { d: PlayoffRace }) {
  const rows = [...d.teams].sort((a, b) => (a.current.rank ?? 99) - (b.current.rank ?? 99) || (a.espn_seed ?? 99) - (b.espn_seed ?? 99) || a.team_id - b.team_id);
  return (
    <section className="panel">
      <h2>League power view · actual vs forward</h2>
      <p className="faint">Ordered by actual standing. No composite power score: actual results (left) and ESPN-based forward projections (right) are kept separate.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Team</th><th>Record</th><th className="num">PF</th><th className="num">PA</th><th>All-play</th>
            <th className="num">Proj. reg-season pts</th><th>Remaining (scenario result · projected margin)</th><th>Coverage</th></tr></thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.team_id} className={t.is_mine ? "mine" : ""}>
                <td>{t.name}{t.is_mine && <> <Badge value="You" tone="info" /></>}</td>
                <td>{rec(t.record)}</td>
                <td className="num">{pts(t.points_for)}</td>
                <td className="num">{pts(t.points_against)}</td>
                <td title={`All-play over final periods ${t.all_play.periods.join(", ") || "none"}`}>
                  {t.all_play.periods.length ? <>{t.all_play.wins}-{t.all_play.losses}{t.all_play.ties ? `-${t.all_play.ties}` : ""} <span className="faint">({((t.all_play.pct ?? 0) * 100).toFixed(0)}%)</span></> : <span className="faint">no final periods</span>}
                </td>
                <td className="num">{t.regular_season_projected_points !== null ? pts(t.regular_season_projected_points)
                  : t.projection_gap ? <span className="faint" title={t.projection_gap}>—</span>
                  : <span className="faint" title={`Incomplete weeks: ${(t.projection_weeks_incomplete ?? []).join(", ")}`}>known {pts(t.regular_season_known_points)}</span>}</td>
                <td className="wrap">{t.remaining.length === 0 ? <span className="faint">none</span> : t.remaining.map((r) => (
                  <span key={r.matchup_id} className="badge" style={{ marginRight: 4, marginBottom: 2, color: r.scenario_result === "win" ? AHEAD : r.scenario_result === "loss" ? BEHIND : undefined }}
                        title={`Week ${r.weeks?.join("–") ?? r.matchup_period} ${r.home ? "vs" : "@"} ${r.opponent_name}: ${r.scenario_result ?? "unresolved"} (${r.status.replace("_", " ")})`}>
                    {r.weeks?.join("–") ?? r.matchup_period} {r.home ? "vs" : "@"} {r.opponent_name?.slice(0, 10) ?? "?"} {r.scenario_result ? r.scenario_result[0].toUpperCase() : "?"}{r.projected_margin !== null ? ` ${signed(r.projected_margin)}` : ""}
                  </span>
                ))}</td>
                <td className="faint">{t.projection_gap ?? ((t.projection_weeks_incomplete ?? []).length ? `incomplete wk ${(t.projection_weeks_incomplete ?? []).join(", ")}` : "complete")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="faint">Badges: W/L/T is the result in this scenario (? = unresolved), followed by the projected margin from this team&apos;s side.</p>
    </section>
  );
}

function KnownPlayoffs({ d }: { d: PlayoffRace }) {
  return (
    <section className="panel">
      <h2>Playoff matchups ESPN already lists</h2>
      <p className="faint">Shown as ESPN reports them; future playoff opponents are not modeled.</p>
      <table>
        <thead><tr><th>Period</th><th>Tier</th><th>Home</th><th>Away</th><th>ESPN winner</th></tr></thead>
        <tbody>{d.matchups.playoff_known.map((m) => (
          <tr key={m.matchup_id}><td>{m.weeks?.join("–") ?? m.matchup_period}</td><td>{m.playoff_tier}</td><td>{m.home_name ?? "TBD"}</td><td>{m.away_name ?? "—"}</td><td>{m.espn_winner}</td></tr>
        ))}</tbody>
      </table>
    </section>
  );
}

function Method({ d }: { d: PlayoffRace }) {
  const cov = d.schedule_coverage;
  return (
    <details className="panel method">
      <summary>How this works (and what it cannot say)</summary>
      <ul className="reasons">
        <li>{d.rules.ordering}</li>
        <li>{d.rules.projection}</li>
        <li>{d.rules.close}</li>
        <li>{d.rules.clinch}</li>
        {d.notes.map((n, i) => <li key={i}>{n}</li>)}
        <li>ESPN seeding/tie settings: {Object.keys(d.rules.espn_rules_reported).length
          ? Object.entries(d.rules.espn_rules_reported).map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join("; ") + ". "
          : ""}{d.rules.espn_rules_note}</li>
        <li>Schedule coverage: {cov.checked ? (cov.complete ? `every team has a matchup in all ${cov.regular_season_periods} regular-season periods`
          : `missing regular-season matchups for ${cov.missing.length} team(s)`) : cov.reason}{cov.unmapped_periods && cov.unmapped_periods.length > 0 ? `; periods without known weeks: ${cov.unmapped_periods.join(", ")}` : ""}.</li>
        {d.record_check.map((n, i) => <li key={`rc${i}`}>{n}</li>)}
        {d.window.reason && <li>Projection window: {d.window.reason}</li>}
      </ul>
    </details>
  );
}
