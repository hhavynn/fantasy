import Link from "next/link";
import type { CSSProperties } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { BarChart } from "@/components/charts";
import { Badge, EmptyState, Method, Notice, PageHeader, Section, Stat, StatStrip, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { cx } from "@/lib/cx";
import {
  backendQuery, cutlineIndex, parseRaceQuery, raceHref, weeksText, withCutline, withOverride, type RaceQuery,
} from "@/lib/playoff-view";
import type { PlayoffRace, PrLeverage, PrMatchup, PrOutcome, PrPlace, PrRecord, PrStatus, PrTeam } from "@/lib/types";
import styles from "./playoff.module.css";
import { plural } from "@/lib/plural";

export const metadata = { title: "Playoff race" };

// Playoff Race Lab: actual standings and all-play, projected remaining matchups (ESPN weekly projections
// through the canonical lineup optimizer), one deterministic projected scenario and URL-backed overrides.
// Nothing here is a probability; no clinch or elimination is claimed.

type Q = RaceQuery;
type Name = (id: number | null | undefined) => string;

function rec(r: PrRecord | null | undefined): string {
  if (!r) return "—";
  return `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}`;
}

function pts(v: number | null | undefined, d = 1): string {
  return v === null || v === undefined ? "—" : v.toFixed(d);
}

function signed(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v > 0 ? `+${v.toFixed(1)}` : v < 0 ? `−${Math.abs(v).toFixed(1)}` : "0.0";
}

const STATUS_TEXT: Record<string, string> = { inside: "Inside the line", outside: "Outside", cutline_tie: "Tied at the line" };
const STATUS_TONE: Record<string, string> = { inside: "good", outside: "bad", cutline_tie: "warn" };

function StatusBadge({ s, title }: { s: PrStatus | undefined; title?: string }) {
  if (!s) return <span className="missing" title={title ?? "No cutline: playoff spots unknown or record unknown"}>—</span>;
  return <Badge value={STATUS_TEXT[s]} tone={STATUS_TONE[s]} title={title} />;
}

export default async function PlayoffRacePage({ searchParams }: PageProps<"/playoff-race">) {
  const sp = await searchParams;
  const q = parseRaceQuery(sp);
  const res = await api<PlayoffRace>(`/api/playoff-race?${backendQuery(q)}`);
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Playoff race lab" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
        {Object.keys(q.o).length > 0 && <p><Link href={raceHref({ ...q, o: {} })}>Clear all overrides</Link></p>}
      </div>
    );
  }
  return <Body d={res.data} q={q} />;
}

function Body({ d, q }: { d: PlayoffRace; q: Q }) {
  const teams = new Map(d.teams.map((t) => [t.team_id, t]));
  const focus = d.focus_team_id !== null ? teams.get(d.focus_team_id) : undefined;
  const name: Name = (id) => (id === null || id === undefined ? "?" : teams.get(id)?.name ?? `Team ${id}`);
  const open = d.matchups.regular.filter((m) => m.status !== "final" && m.status !== "bye");
  return (
    <div className="stack">
      <PageHeader
        eyebrow="League"
        title="Playoff race lab"
        meta={<>
          {d.league.name} {d.league.season} · ESPN week {d.league.current_week}
          {d.league.reg_season_matchups !== null && <> · regular season: {d.league.reg_season_matchups} matchup periods
            {d.league.last_regular_season_week !== null && <> (through week {d.league.last_regular_season_week})</>}</>}
          {" "}· playoff spots {d.league.playoff_teams ?? <b>unknown</b>} (league settings)
          {" "}· first projectable week {d.window.first_actionable_week ?? "none"}
          {focus && <> · spotlight <b>{focus.name}</b>{focus.is_mine ? " (you)" : ""}</>}
          . Deterministic scenarios from ESPN projections, not probabilities.
        </>}
        actions={
          <form action="/playoff-race" className={styles.spotlightForm}>
            {q.base !== "projected" && <input type="hidden" name="base" value={q.base} />}
            {Object.entries(q.o).map(([mid, out]) => <input key={mid} type="hidden" name="o" value={`${mid}:${out}`} />)}
            <label>Spotlight <select name="team" defaultValue={d.focus_team_id ?? ""}>
              {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
            </select></label>
            <button type="submit">Show</button>
          </form>
        }
      />
      {d.warnings.map((w, i) => <Notice key={i} tone="caution">{w}</Notice>)}

      {focus && <FocusStrip d={d} t={focus} q={q} />}
      <ScenarioBar d={d} q={q} name={name} />
      {focus && <Spotlight d={d} t={focus} q={q} name={name} />}

      <div className="grid grid-2">
        <Standings d={d} name={name} />
        <Ladder d={d} name={name} />
      </div>

      <Race d={d} q={q} name={name} />
      {focus && <Margins t={focus} />}
      <Matchups d={d} q={q} open={open} />
      <Power d={d} />
      {d.matchups.playoff_known.length > 0 && <KnownPlayoffs d={d} />}
      <MethodNotes d={d} />
    </div>
  );
}

// ------------------------------------------------------------------- headline --

function PlaceNote({ p }: { p: PrPlace }) {
  return <>{p.rank !== null ? `#${p.rank} ` : ""}<StatusBadge s={p.status} /></>;
}

function FocusStrip({ d, t, q }: { d: PlayoffRace; t: PrTeam; q: Q }) {
  const f = d.scenario.focus;
  if (!f) return null;
  return (
    <StatStrip>
      <Stat label="Now (ESPN)" value={rec(t.record)} note={<>seed {t.espn_seed ?? "—"} · <StatusBadge s={t.current.status} title="Decided games only" /></>} />
      <Stat label={q.base === "projected" ? "This scenario" : "Actual + overrides"} value={rec(f.scenario.record)} note={<PlaceNote p={f.scenario} />} />
      <Stat label={`Win all ${f.open_games} open`} value={rec(f.win_out.record)} note={<PlaceNote p={f.win_out} />} />
      <Stat label={`Lose all ${f.open_games} open`} value={rec(f.lose_out.record)} note={<PlaceNote p={f.lose_out} />} />
    </StatStrip>
  );
}

// ------------------------------------------------------------- scenario controls --

function ScenarioBar({ d, q, name }: { d: PlayoffRace; q: Q; name: Name }) {
  const s = d.scenario;
  const byId = new Map(d.matchups.regular.map((m) => [m.matchup_id, m]));
  const baseItems = [
    { href: raceHref(q, { base: "projected" }), label: "Higher projection wins" },
    { href: raceHref(q, { base: "actual" }), label: "Actual results only" },
  ];
  return (
    <Section title="Scenario">
      <div className={styles.scenario}>
        <Tabs label="Scenario baseline" items={baseItems} current={raceHref(q)} />
        <p className={styles.scenarioNote}>{s.decided_open_games} remaining {plural(s.decided_open_games, "game")} decided in this scenario, {s.unresolved_open_games} unresolved.</p>
      </div>
      {s.overrides.length > 0 ? (
        <div className={styles.overrides}>
          <span>Your overrides:</span>
          {s.overrides.map((ov) => {
            const m = byId.get(ov.matchup_id);
            const who = ov.outcome === "home" ? name(m?.home_team_id ?? null) : ov.outcome === "away" ? name(m?.away_team_id ?? null) : null;
            return (
              <span key={ov.matchup_id} className={styles.chip}>
                {m ? weeksText(m.weeks, m.matchup_period) : `matchup ${ov.matchup_id}`}: {who ? `${who} wins` : ov.outcome === "tie" ? "tie" : "unresolved"}
                {" "}<Link href={withOverride(q, ov.matchup_id, null)} title="Remove this override" aria-label="Remove this override">✕</Link>
              </span>
            );
          })}
          <Link href={raceHref(q, { o: {} })}>Clear all</Link>
        </div>
      ) : (
        <p className={styles.scenarioNote}>No overrides. Use the links in &ldquo;Remaining matchups&rdquo; (or a spotlight game) to set a winner; each choice is in the URL, so the back button undoes it.</p>
      )}
    </Section>
  );
}

// ----------------------------------------------------------------------- spotlight --

function Spotlight({ d, t, q, name }: { d: PlayoffRace; t: PrTeam; q: Q; name: Name }) {
  const f = d.scenario.focus;
  const lev = d.scenario.leverage[String(t.team_id)] ?? [];
  const high = lev.filter((g) => g.high_leverage);
  const fewest = f?.fewest_wins_to_get_inside;
  const hasText = f && (
    (fewest && (fewest.wins_needed !== null || fewest.note)) || (!fewest && f.win_out.status !== "inside" && d.league.playoff_teams && f.open_games > 0) || f.open_games === 0
  );
  if (!f || (!hasText && high.length === 0 && f.helpful_other_results.length === 0)) return null;
  return (
    <Section title={`What decides ${t.name}'s cutline`}>
      <div className={styles.spotlight}>
        {hasText && (
          <ul className="reasons">
            {fewest && fewest.wins_needed !== null && (
              <li>Fewest wins among the {fewest.of_games} open {plural(fewest.of_games, "game")} that put {t.name} inside the cutline, every other result as in this
                scenario: <b>{fewest.wins_needed}</b>{fewest.games.length > 0 && <> (for example {fewest.games.map((id) => {
                  const g = lev.find((x) => x.matchup_id === id);
                  return g ? `${weeksText(g.weeks, g.matchup_period)} vs ${name(g.opponent_id)}` : id;
                }).join(", ")})</>}.</li>)}
            {fewest && fewest.wins_needed === null && fewest.note && <li>{fewest.note}: the fewest-wins search was skipped.</li>}
            {!fewest && f.win_out.status !== "inside" && d.league.playoff_teams && f.open_games > 0 && (
              <li>Winning every open game does not put {t.name} inside the cutline under the other results of this scenario; see results elsewhere that help.</li>)}
            {f.open_games === 0 && <li>No open regular-season games remain for {t.name}.</li>}
          </ul>
        )}
        {high.length > 0 && (
          <div>
            <h3 className={styles.subhead}>High-leverage games: the result changes {t.name}&apos;s cutline status</h3>
            <LeverageTable rows={high} q={q} name={name} d={d} tid={t.team_id} />
          </div>
        )}
        {f.helpful_other_results.length > 0 && (
          <div>
            <h3 className={styles.subhead}>Results elsewhere that help {t.name}</h3>
            <ul className="reasons">
              {f.helpful_other_results.map((h) => {
                const m = d.matchups.regular.find((x) => x.matchup_id === h.matchup_id);
                const winner = h.result === "home" ? m?.home_team_id : m?.away_team_id;
                return (
                  <li key={`${h.matchup_id}-${h.result}`}>
                    {m ? weeksText(m.weeks, h.matchup_period) : `Period ${h.matchup_period}`}: {name(winner ?? null)} beats {name(h.loser_id)} → {t.name} #{h.rank} <StatusBadge s={h.status} />
                    {" "}<Link href={withOverride(q, h.matchup_id, h.result)}>Apply</Link>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>
    </Section>
  );
}

function LeverageTable({ rows, q, name, d, tid }: { rows: PrLeverage[]; q: Q; name: Name; d: PlayoffRace; tid: number }) {
  const byId = new Map(d.matchups.regular.map((m) => [m.matchup_id, m]));
  return (
    <div className={styles.flushTable}>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Week</th><th>Opponent</th><th>In this scenario</th><th>If win</th><th>If loss</th><th>Try it</th></tr></thead>
          <tbody>
            {rows.map((g) => {
              const m = byId.get(g.matchup_id);
              const side = m?.home_team_id === tid ? "home" : "away";
              const other = side === "home" ? "away" : "home";
              return (
                <tr key={g.matchup_id} className={styles.leverage}>
                  <td>{g.weeks?.join("–") ?? g.matchup_period}</td>
                  <td>{name(g.opponent_id)}{g.close && <> <Badge value="close" tone="warn" /></>}</td>
                  <td>{g.scenario_result ?? <span className="faint">unresolved ({g.status.replace("_", " ")})</span>}</td>
                  <td>#{g.if_win.rank} <StatusBadge s={g.if_win.status} /></td>
                  <td>#{g.if_loss.rank} <StatusBadge s={g.if_loss.status} /></td>
                  <td><Link href={withOverride(q, g.matchup_id, side)}>Win</Link> · <Link href={withOverride(q, g.matchup_id, other)}>Loss</Link></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------- standings --

function Standings({ d, name }: { d: PlayoffRace; name: Name }) {
  const s = d.scenario;
  const spots = d.league.playoff_teams;
  const after = cutlineIndex(s.projected_standings, spots);
  const tie = s.cutline?.cutline_tie;
  const bubble = new Set(s.cutline?.bubble ?? []);
  const cur = new Map(s.current_standings.map((x) => [x.team_id, x]));
  const teams = new Map(d.teams.map((t) => [t.team_id, t]));
  const items = withCutline(s.projected_standings, after);
  return (
    <Section
      title={`Current vs ${s.base === "projected" ? "projected" : "scenario"} standings`}
      flush
      footnote={<>&ldquo;=&rdquo; marks a shared rank (equal winning percentage; no tiebreak applied). &ldquo;+n?&rdquo; counts games still unresolved in this scenario. Bubble teams have a remaining game whose result changes their cutline status. {after === null
        ? (spots === null ? "Playoff spots are unknown, so no playoff line is drawn." : "No team has a decided game in this scenario yet, so no playoff line position is drawn.")
        : <>The playoff line is after {spots} spots{tie ? `; a tied group straddles it, so it is drawn above them and they share ${tie.spots_left} ${plural(tie.spots_left, "spot")}` : ""}.</>}</>}
    >
      <div className="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Team</th><th>Now</th><th title="ESPN's own seed">ESPN seed</th><th>Scenario</th><th>Move</th><th>Status</th></tr></thead>
          <tbody>
            {items.map((it) => {
              if (it.kind === "cutline") {
                return (
                  <tr key="cutline" className={styles.cutline}>
                    <td colSpan={7} title={tie ? `${tie.spots_left} ${plural(tie.spots_left, "spot")} shared by the tied teams below` : `${spots} playoff spots`}>
                      Playoff line · {spots} spots{tie ? ` · ${tie.spots_left} shared by the tied teams below` : ""}
                    </td>
                  </tr>
                );
              }
              const x = it.row;
              const c = cur.get(x.team_id);
              const move = c?.rank && x.rank ? c.rank - x.rank : null;
              const t = teams.get(x.team_id);
              return (
                <tr key={x.team_id} className={t?.is_mine ? "mine" : ""}>
                  <td className="num">{x.rank ?? "—"}{x.tied ? "=" : ""}</td>
                  <td>{name(x.team_id)}{bubble.has(x.team_id) && <> <Badge value="Bubble" tone="warn" title="A remaining game changes this team's cutline status" /></>}</td>
                  <td className="num">{rec(c?.record)}{c?.rank ? <span className="faint"> #{c.rank}{c.tied ? "=" : ""}</span> : null}</td>
                  <td className="num">{t?.espn_seed ?? "—"}</td>
                  <td className="num"><b>{rec(x.record)}</b>{x.record?.unresolved ? <span className="faint" title="Games unresolved in this scenario"> +{x.record.unresolved}?</span> : null}</td>
                  <td className="num">{move === null || move === 0 ? <span className="faint">·</span> : move > 0 ? <span className="up">▲ {move}</span> : <span className="down">▼ {-move}</span>}</td>
                  <td><StatusBadge s={x.status} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Ladder({ d, name }: { d: PlayoffRace; name: Name }) {
  const rows = d.scenario.projected_standings;
  const after = cutlineIndex(rows, d.league.playoff_teams);
  const tie = d.scenario.cutline?.cutline_tie;
  return (
    <Section
      title="Scenario record: win share (wins ÷ games)"
      footnote={<>Bars: each team&apos;s win share in the deterministic scenario, W ÷ (W + L) over decided games. It is the scenario&apos;s record, not a probability or a chance of making the playoffs{d.focus_team_id !== null ? `; ${name(d.focus_team_id)} is highlighted` : ""}. Teams are in standings order; the dashed line is the playoff line.</>}
    >
      <BarChart
        orientation="horizontal"
        data={rows.map((x) => ({
          label: name(x.team_id),
          value: x.pct,
          tone: x.team_id === d.focus_team_id ? "default" : "muted",
        }))}
        valueFormat={(v) => `${Math.round(v * 100)}%`}
        divider={after !== null ? { after, label: tie ? `Playoff line · ${tie.spots_left} shared` : "Playoff line" } : undefined}
        ariaLabel="Scenario win share (wins divided by games) by team with the playoff line"
      />
    </Section>
  );
}

// ------------------------------------------------------------- spotlight margins chart --

function Margins({ t }: { t: PrTeam }) {
  const rows = t.remaining.filter((r) => r.status !== "bye");
  if (rows.length === 0) return null;
  return (
    <Section
      title={`${t.name}: projected margin by remaining matchup`}
      footnote="Margins compare ESPN-projected optimal lineups; they are not win probabilities. A dash means no projected margin (game under way, a projection is incomplete, or its weeks are unknown)."
    >
      <BarChart
        orientation="horizontal"
        data={rows.map((r) => ({
          label: `${weeksText(r.weeks, r.matchup_period)} ${r.home ? "vs" : "@"} ${r.opponent_name ?? "?"}${r.close ? " (close)" : ""}`,
          value: r.projected_margin,
        }))}
        valueFormat={signed}
        ariaLabel="Projected scoring margin per remaining matchup, from this team's side"
      />
    </Section>
  );
}

// -------------------------------------------------------------------- race & leverage --

function group(rows: { team_id: number; record: PrRecord | null }[], name: Name): string {
  return rows.length ? rows.map((x) => `${name(x.team_id)} ${rec(x.record)}`).join(" = ") : "—";
}

function Race({ d, q, name }: { d: PlayoffRace; q: Q; name: Name }) {
  const cut = d.scenario.cutline;
  if (!cut) return <Notice tone="caution">Playoff spots are not reported in league settings, so no cutline or bubble can be shown.</Notice>;
  return (
    <Section
      title={`Bubble teams · ${cut.playoff_spots} playoff spots`}
      flush={cut.bubble.length > 0}
      footnote="Bubble teams: those with at least one remaining game whose result changes their cutline status (all else as in this scenario), plus any team tied at the cutline."
    >
      <p className={cx(styles.raceLine, cut.bubble.length > 0 && styles.padded)}>
        {cut.cutline_tie ? <>
          Tied at the cutline: <b>{cut.cutline_tie.team_ids.map((id) => name(id)).join(", ")}</b> share {cut.cutline_tie.spots_left} remaining
          {plural(cut.cutline_tie.spots_left, "spot")} <Badge value="Unresolved: no tiebreak applied" tone="warn" />
        </> : <>
          Last in: <b>{group(cut.last_inside, name)}</b> · first out: <b>{group(cut.first_outside, name)}</b>
        </>}
      </p>
      {cut.bubble.length === 0 ? <EmptyState title="No team's status depends on a single result in this scenario" /> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Team</th><th>Scenario</th><th>Status</th><th>Games that decide it</th><th /></tr></thead>
            <tbody>
              {cut.bubble.map((tid) => {
                const rows = (d.scenario.leverage[String(tid)] ?? []).filter((g) => g.high_leverage);
                const proj = d.scenario.projected_standings.find((x) => x.team_id === tid);
                return (
                  <tr key={tid} className={tid === d.focus_team_id ? "mine" : ""}>
                    <td>{name(tid)}</td>
                    <td className="num">{rec(proj?.record)} <span className="faint">#{proj?.rank}</span></td>
                    <td><StatusBadge s={proj?.status} /></td>
                    <td className={styles.decide}>
                      {rows.length === 0 ? <span className="faint">Tied at the cutline; no single own result decides it.</span> : (
                        <ul className={styles.decideList}>
                          {rows.map((g) => (
                            <li key={g.matchup_id}>
                              {weeksText(g.weeks, g.matchup_period)} vs {name(g.opponent_id)}: win → <StatusBadge s={g.if_win.status} />, loss → <StatusBadge s={g.if_loss.status} />
                              {" "}<span className="faint">({g.scenario_result ?? "unresolved"})</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </td>
                    <td>{tid !== d.focus_team_id && <Link href={raceHref(q, { team: String(tid) })}>Spotlight</Link>}</td>
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

// ------------------------------------------------------------------ remaining matchups --

function SideCell({ m, side }: { m: PrMatchup; side: "home" | "away" }) {
  const s = side === "home" ? m.home_projection : m.away_projection;
  if (m.status === "in_progress") {
    const v = side === "home" ? m.home_points : m.away_points;
    return <span className="faint" title="ESPN points as of the last sync; not final">{pts(v)} live</span>;
  }
  if (!s) return <span className="missing" title="No projection">—</span>;
  if (s.projected_points !== null) return <b>{pts(s.projected_points)}</b>;
  return <span className="faint" title={`${s.gap ?? "incomplete"}${s.missing_players.length ? `: ${s.missing_players.join(", ")}` : ""}`}>known {pts(s.known_points)} · incomplete</span>;
}

/** The bar grows toward the projected leader's column; all bars share one scale (the biggest margin fills half). */
function MarginBar({ m, maxAbs }: { m: PrMatchup; maxAbs: number }) {
  const v = m.projected_margin;
  if (v === null || v === undefined) return <span className="missing" title="No projected margin">—</span>;
  const w = Math.max(2, (Math.abs(v) / maxAbs) * 50);
  const home = v >= 0;
  return (
    <span className={styles.marginCell} title={`Projected margin ${signed(v)} (home minus away)`}>
      <span className={styles.track} aria-hidden="true">
        <span className={cx(styles.fill, home ? styles.fillHome : styles.fillAway)} style={{ "--w": `${w.toFixed(1)}%` } as CSSProperties} />
      </span>
      <span>{v === 0 ? "Even" : `${home ? "Home" : "Away"} ${signed(Math.abs(v))}`}</span>
    </span>
  );
}

function OverrideLinks({ m, q }: { m: PrMatchup; q: Q }) {
  const cur = q.o[String(m.matchup_id)];
  const opt = (out: PrOutcome | "none", label: string) => (
    cur === out ? <b key={out}>{label}</b> : <Link key={out} href={withOverride(q, m.matchup_id, out)}>{label}</Link>
  );
  return (
    <span className={styles.overrideLinks}>
      {opt("home", "Home wins")}{opt("away", "Away wins")}{opt("tie", "Tie")}{opt("none", "Unresolved")}
      {cur !== undefined && <Link href={withOverride(q, m.matchup_id, null)} title="Back to the baseline">Reset</Link>}
    </span>
  );
}

function Matchups({ d, q, open }: { d: PlayoffRace; q: Q; open: PrMatchup[] }) {
  const periods = [...new Set(open.map((m) => m.matchup_period))].sort((a, b) => a - b);
  const maxAbs = Math.max(1, ...open.map((m) => Math.abs(m.projected_margin ?? 0)));
  const focus = d.focus_team_id;
  const highIds = new Set((focus !== null ? d.scenario.leverage[String(focus)] ?? [] : []).filter((g) => g.high_leverage).map((g) => g.matchup_id));
  return (
    <Section
      title="Remaining regular-season matchups"
      flush={open.length > 0}
      footnote={open.length > 0 ? <>Projected totals are ESPN projections through the lineup optimizer. The margin is home minus away, drawn toward the projected leader. &ldquo;Close&rdquo; means within 5% of the mean projected total. Highlighted rows involve the spotlight team{highIds.size > 0 ? "; High leverage marks games that change its cutline status" : ""}. Overrides are in the URL.</> : undefined}
    >
      {open.length === 0 ? <EmptyState title="No remaining regular-season matchups">The ESPN schedule has no open regular-season games.</EmptyState> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Home</th><th className="num">Proj.</th><th>Margin</th><th className="num">Proj.</th><th>Away</th><th>Scenario</th><th>Override</th></tr></thead>
            {periods.map((p) => {
              const ms = open.filter((m) => m.matchup_period === p);
              const sample = ms[0];
              return (
                <tbody key={p}>
                  <tr className="group"><td colSpan={7}>{weeksText(sample?.weeks, p)}{sample?.weeks && sample.weeks.length > 1 ? ` · ${sample.weeks.length}-week matchup` : ""}</td></tr>
                  {ms.map((m) => {
                    const res = m.scenario_result;
                    const involves = focus !== null && (m.home_team_id === focus || m.away_team_id === focus);
                    return (
                      <tr key={m.matchup_id} className={involves ? "mine" : ""}>
                        <td>{m.home_name}</td>
                        <td className="num"><SideCell m={m} side="home" /></td>
                        <td>
                          <MarginBar m={m} maxAbs={maxAbs} />
                          {m.close && <> <Badge value="Close" tone="warn" title="Projected margin within 5% of the mean projected total" /></>}
                        </td>
                        <td className="num"><SideCell m={m} side="away" /></td>
                        <td>{m.away_name}</td>
                        <td className={styles.scenarioCell}>
                          {res ? <b>{res === "tie" ? "Tie" : `${res === "home" ? m.home_name : m.away_name} wins`}</b> : <span className="faint">Unresolved</span>}
                          {m.override ? <> <Badge value="Override" tone="info" /></> : res ? <span className="faint"> (projection)</span> : null}
                          {highIds.has(m.matchup_id) && <> <Badge value="High leverage" tone="warn" title="This result changes the spotlight team's cutline status" /></>}
                          <span className={styles.sub}>{m.status.replace("_", " ")}{m.status !== "upcoming" || !res ? ` · ${m.detail ?? ""}` : ""}</span>
                        </td>
                        <td><OverrideLinks m={m} q={q} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              );
            })}
          </table>
        </div>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------- power rankings --

function Power({ d }: { d: PlayoffRace }) {
  const rows = [...d.teams].sort((a, b) => (a.current.rank ?? 99) - (b.current.rank ?? 99) || (a.espn_seed ?? 99) - (b.espn_seed ?? 99) || a.team_id - b.team_id);
  return (
    <Section
      title="League power view: actual vs forward"
      flush
      footnote="Ordered by actual standing. No composite power score: actual results (left) and ESPN-based forward projections (right) are kept separate. Badges: W, L or T is the result in this scenario (? = unresolved), followed by the projected margin from this team's side."
    >
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
                  : t.projection_gap ? <span className="missing" title={t.projection_gap}>—</span>
                  : <span className="faint" title={`Incomplete weeks: ${(t.projection_weeks_incomplete ?? []).join(", ")}`}>known {pts(t.regular_season_known_points)}</span>}</td>
                <td className={styles.remaining}>{t.remaining.length === 0 ? <span className="faint">none</span> : (
                  <span className={styles.badges}>
                    {t.remaining.map((r) => (
                      <Badge
                        key={r.matchup_id}
                        tone={r.scenario_result === "win" ? "good" : r.scenario_result === "loss" ? "bad" : r.scenario_result === "tie" ? "warn" : ""}
                        title={`${weeksText(r.weeks, r.matchup_period)} ${r.home ? "vs" : "@"} ${r.opponent_name}: ${r.scenario_result ?? "unresolved"} (${r.status.replace("_", " ")})`}
                        value={`${r.weeks?.join("–") ?? r.matchup_period} ${r.home ? "vs" : "@"} ${r.opponent_name?.slice(0, 10) ?? "?"} ${r.scenario_result ? r.scenario_result[0].toUpperCase() : "?"}${r.projected_margin !== null ? ` ${signed(r.projected_margin)}` : ""}`}
                      />
                    ))}
                  </span>
                )}</td>
                <td className="faint">{t.projection_gap ?? ((t.projection_weeks_incomplete ?? []).length ? `incomplete wk ${(t.projection_weeks_incomplete ?? []).join(", ")}` : "complete")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function KnownPlayoffs({ d }: { d: PlayoffRace }) {
  return (
    <Section title="Playoff matchups ESPN already lists" flush footnote="Shown as ESPN reports them; future playoff opponents are not modeled.">
      <div className="table-wrap">
        <table>
          <thead><tr><th>Period</th><th>Tier</th><th>Home</th><th>Away</th><th>ESPN winner</th></tr></thead>
          <tbody>{d.matchups.playoff_known.map((m) => (
            <tr key={m.matchup_id}><td>{m.weeks?.join("–") ?? m.matchup_period}</td><td>{m.playoff_tier}</td><td>{m.home_name ?? "TBD"}</td><td>{m.away_name ?? "—"}</td><td>{m.espn_winner}</td></tr>
          ))}</tbody>
        </table>
      </div>
    </Section>
  );
}

function MethodNotes({ d }: { d: PlayoffRace }) {
  const cov = d.schedule_coverage;
  return (
    <Method title="How this works (and what it cannot say)">
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
          : `missing regular-season matchups for ${cov.missing.length} ${plural(cov.missing.length, "team")}`) : cov.reason}{cov.unmapped_periods && cov.unmapped_periods.length > 0 ? `; periods without known weeks: ${cov.unmapped_periods.join(", ")}` : ""}.</li>
        {d.record_check.map((n, i) => <li key={`rc${i}`}>{n}</li>)}
        {d.window.reason && <li>Projection window: {d.window.reason}</li>}
      </ul>
    </Method>
  );
}
