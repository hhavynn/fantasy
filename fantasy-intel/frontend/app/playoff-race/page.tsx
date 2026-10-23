import Link from "@/components/LeagueLink";
import type { CSSProperties } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { BarChart } from "@/components/charts";
import { Badge, EmptyState, Method, Notice, PageHeader, Section, Stat, StatStrip, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { cx } from "@/lib/cx";
import { projectionName, providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import {
  backendQuery, cutlineIndex, matchupAnalysisHref, parseRaceQuery, raceHref, rootingReason, unresolvedText, weeksText,
  whatifChanged, withCutline, withOverride, type RaceQuery,
} from "@/lib/playoff-view";
import type {
  PlayoffRace, PrBound, PrLeverage, PrMatchup, PrOutcome, PrPlace, PrPlaceRow, PrRecord, PrRooting, PrRootingGame,
  PrRootingSide, PrStatus, PrTeam, PrWhatIf,
} from "@/lib/types";
import { CopyScenarioLink } from "./CopyScenarioLink";
import styles from "./playoff.module.css";
import { plural } from "@/lib/plural";

export const metadata = { title: "Playoff race" };

// Playoff Race Lab: actual standings and all-play, projected remaining matchups (ESPN weekly projections
// through the canonical lineup optimizer), one deterministic projected scenario and URL-backed overrides.
// Nothing here is a probability; no clinch or elimination is claimed. Every standing is labelled actual or
// hypothetical (issue #192), and a what-if (at least one set result) shows its inputs, assumptions and method.

type Q = RaceQuery;
/** Who the league data comes from (ESPN or Sleeper) and whose projections the scenarios use (the league default). */
type L = { prov: string; proj: string };
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
  const [res, profile, league] = await Promise.all([api<PlayoffRace>(`/api/playoff-race?${backendQuery(q)}`), getLeagueProfile(), currentLeague()]);
  const labels: L = { prov: providerName(profile.provider), proj: projectionName(profile.defaultSource) };
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Playoff race lab" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
        {Object.keys(q.o).length > 0 && <p><Link href={raceHref({ ...q, o: {} })}>Clear all overrides</Link></p>}
      </div>
    );
  }
  // The no-JS spotlight form stays in the page's league (`/l/<lg>/playoff-race`), whatever the league cookie says.
  return <Body d={res.data} q={q} L={labels} action={withLeague("/playoff-race", league?.id)}
    share={withLeague(raceHref(q), league?.id)} />;
}

/** The marker on every hypothetical standing, record or result (issue #192); actual ones say "actual". */
function Hyp() {
  return <Badge value="Hypothetical" tone="info" title="Not an actual result: a scenario built from projections and the results you set" />;
}

function Body({ d, q, L, action, share }: { d: PlayoffRace; q: Q; L: L; action: string; share: string }) {
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
          {d.league.name} {d.league.season} · {L.prov} week {d.league.current_week}
          {d.league.reg_season_matchups !== null && <> · regular season: {d.league.reg_season_matchups} matchup periods
            {d.league.last_regular_season_week !== null && <> (through week {d.league.last_regular_season_week})</>}</>}
          {" "}· playoff spots {d.league.playoff_teams ?? <b>unknown</b>} (league settings)
          {" "}· first projectable week {d.window.first_actionable_week ?? "none"}
          {focus && <> · spotlight <b>{focus.name}</b>{focus.is_mine ? " (default team)" : ""}</>}
          . Deterministic scenarios from {L.proj} projections, not probabilities.
        </>}
        actions={
          <form action={action} className={styles.spotlightForm}>
            {q.base !== "projected" && <input type="hidden" name="base" value={q.base} />}
            {Object.entries(q.o).map(([mid, out]) => <input key={mid} type="hidden" name="o" value={`${mid}:${out}`} />)}
            <label>Spotlight <select name="team" defaultValue={d.focus_team_id ?? ""}>
              {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (default)" : ""}</option>)}
            </select></label>
            <button type="submit">Show</button>
          </form>
        }
      />
      {d.warnings.map((w, i) => <Notice key={i} tone="caution">{w}</Notice>)}

      {focus && <FocusStrip d={d} t={focus} q={q} L={L} />}
      <ScenarioBar d={d} q={q} name={name} />
      {d.whatif && <WhatIf d={d} w={d.whatif} q={q} name={name} share={share} L={L} />}
      {focus && <Spotlight d={d} t={focus} q={q} name={name} />}
      {focus && d.scenario.rooting && <Rooting d={d} g={d.scenario.rooting} t={focus} q={q} name={name} />}

      <div className="grid grid-2">
        <Standings d={d} name={name} L={L} />
        <Ladder d={d} name={name} />
      </div>

      <Race d={d} q={q} name={name} />
      {focus && <Margins t={focus} L={L} />}
      <Matchups d={d} q={q} open={open} L={L} />
      <Power d={d} L={L} />
      {d.matchups.playoff_known.length > 0 && <KnownPlayoffs d={d} L={L} />}
      <MethodNotes d={d} L={L} />
    </div>
  );
}

// ------------------------------------------------------------------- headline --

function PlaceNote({ p }: { p: PrPlace }) {
  return <>{p.rank !== null ? `#${p.rank} ` : ""}<StatusBadge s={p.status} /></>;
}

function FocusStrip({ d, t, q, L }: { d: PlayoffRace; t: PrTeam; q: Q; L: L }) {
  const f = d.scenario.focus;
  if (!f) return null;
  return (
    <StatStrip>
      <Stat label={`Actual now (${L.prov})`} value={rec(t.record)} note={<>seed {t.espn_seed ?? "—"} · <StatusBadge s={t.current.status} title="Decided games only" /></>} />
      <Stat label={q.base === "projected" ? "Hypothetical: this scenario" : "Hypothetical: actual + set results"} value={rec(f.scenario.record)} note={<PlaceNote p={f.scenario} />} />
      <Stat label={`Hypothetical: win all ${f.open_games} open`} value={rec(f.win_out.record)} note={<PlaceNote p={f.win_out} />} />
      <Stat label={`Hypothetical: lose all ${f.open_games} open`} value={rec(f.lose_out.record)} note={<PlaceNote p={f.lose_out} />} />
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
    <Section title={<>Scenario <Hyp /></>} subtitle="Hypothetical results on top of actual records: the baseline below plus any result you set.">
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

// ------------------------------------------------------------------- what-if (issue #192) --

function resultText(outcome: PrOutcome | "none" | null, home: string, away: string): string {
  return outcome === "home" ? `${home} wins` : outcome === "away" ? `${away} wins` : outcome === "tie" ? "Tie" : "Unresolved";
}

function PlaceCell({ p }: { p: PrPlaceRow }) {
  return <>{rec(p.record)}{p.record?.unresolved ? <span className="faint" title="Games unresolved in this scenario"> +{p.record.unresolved}?</span> : null}
    {" "}<span className="faint">#{p.rank ?? "—"}{p.tied ? "=" : ""}</span> <StatusBadge s={p.status} /></>;
}

/** What the record alone says, as counts; never a clinch or elimination claim (the gate keeps those blocked). */
function BoundText({ b, spots }: { b: PrBound; spots: number | null }) {
  if (b.record_order === null) return <span className="missing" title={spots === null ? "Playoff spots unknown" : "Record bounds abstain (see the note above)"}>—</span>;
  const n = b.open_games;
  if (b.record_order === "above_line") {
    const k = b.teams_able_to_match_worst ?? 0;
    return <>Even losing all {n} open {plural(n, "game")}, {k} other {plural(k, "team")} can match or pass its record ({spots} spots)</>;
  }
  if (b.record_order === "below_line") {
    const k = b.teams_certain_ahead_of_best ?? 0;
    return <>Even winning all {n} open {plural(n, "game")}, {k} {plural(k, "team")} finish ahead in every outcome ({spots} spots)</>;
  }
  return <span className="faint">Depends on remaining results</span>;
}

function WhatIf({ d, w, q, name, share, L }: { d: PlayoffRace; w: PrWhatIf; q: Q; name: Name; share: string; L: L }) {
  const changed = w.changes.filter(whatifChanged);
  const statusMoves = w.changes.filter((c) => c.status_changed).length;
  const sets = w.inputs.set_results;
  const s = w.sampling;
  const bounds = w.conclusions.record_bounds;
  const spots = d.league.playoff_teams;
  return (
    <Section
      title={<>What-if: your results vs the baseline <Hyp /></>}
      subtitle={`Method: ${s.method}, ${s.scenarios} exact ${plural(s.scenarios, "scenario")}, ${s.random_samples} random samples, no seed, no probability.`}
      actions={<CopyScenarioLink href={share} />}
      footnote={<>Baseline: {w.inputs.base === "projected" ? `the higher ${L.proj} projection wins each eligible upcoming game` : "actual results only"}, without your results. Both columns are hypothetical; actual records are in the standings table below.</>}
    >
      <div className={styles.whatif}>
        <p className={styles.raceLine}>
          Your {sets.length} set {plural(sets.length, "result")} {changed.length === 0
            ? <>change no team&apos;s rank, record or cutline status against the baseline.</>
            : <>change the standing of {changed.length} {plural(changed.length, "team")} against the baseline{statusMoves > 0 ? <>; {statusMoves} {plural(statusMoves, "team")} {statusMoves === 1 ? "changes" : "change"} cutline status</> : null}.</>}
        </p>
        {w.completeness.status === "partial" && (
          <Notice tone="caution" title={`Partial: ${w.completeness.unresolved_games} remaining ${plural(w.completeness.unresolved_games, "game")} unresolved`}>
            {unresolvedText(w.completeness.by_reason)}. Unresolved games count for nobody: they are left out of the records, never scored as a loss or as 0 points.
          </Notice>
        )}
        <div>
          <h3 className={styles.subhead}>Results you set</h3>
          <ul className={styles.setList}>
            {sets.map((r) => {
              const home = name(r.home_team_id), away = name(r.away_team_id);
              return (
                <li key={r.matchup_id}>
                  <b>{weeksText(r.weeks, r.matchup_period)}: {resultText(r.outcome, home, away)}</b>
                  <span className="faint"> · {home} vs {away} · baseline: {r.baseline_result === null ? "unresolved" : r.baseline_result === "tie" ? "tie" : resultText(r.baseline_result, home, away)}{r.baseline_result && w.inputs.base === "projected" ? " (projection)" : ""}{r.changes_baseline ? "" : " · same as the baseline"}</span>
                  {" "}<span className={styles.setLinks}>
                    <Link href={matchupAnalysisHref(r, d.focus_team_id)}>Matchup analysis</Link>
                    <Link href={withOverride(q, r.matchup_id, null)} aria-label={`Remove the result set for ${weeksText(r.weeks, r.matchup_period)}, ${home} vs ${away}`}>Remove</Link>
                  </span>
                </li>
              );
            })}
          </ul>
          <p className={styles.scenarioNote}><Link href={raceHref(q, { o: {} })}>Reset to the baseline</Link> · the copied link reopens this exact what-if.</p>
        </div>
        <div className={styles.flushTable}>
          <h3 className={cx(styles.subhead, styles.padSide)}>Standings: baseline vs your what-if</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Team</th><th>Baseline (hypothetical)</th><th>Your what-if (hypothetical)</th><th>Change</th></tr></thead>
              <tbody>
                {w.changes.map((c) => {
                  const mv = c.rank_change;
                  return (
                    <tr key={c.team_id} className={c.team_id === d.focus_team_id ? "mine" : ""}>
                      <td>{name(c.team_id)}</td>
                      <td className="num"><PlaceCell p={c.baseline} /></td>
                      <td className="num"><PlaceCell p={c.whatif} /></td>
                      <td>{!whatifChanged(c) ? <span className="faint">No change</span> : <>
                        {mv ? (mv > 0 ? <span className="up">▲ {mv}</span> : <span className="down">▼ {-mv}</span>) : null}
                        {c.status_changed ? <> <Badge value="Status changed" tone="warn" /></> : null}
                        {!mv && !c.status_changed ? <span className="faint">Record only</span> : null}
                      </>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <h3 className={styles.subhead}>Clinch and elimination: not claimed</h3>
          <ul className="reasons">
            {w.conclusions.blocked_by.map((b) => <li key={b.rule}>{b.detail}</li>)}
          </ul>
          {bounds.abstain.length > 0 && <Notice tone="caution" title="Record bounds withheld">{bounds.abstain.join(" ")}</Notice>}
        </div>
        <div className={styles.flushTable}>
          <h3 className={cx(styles.subhead, styles.padSide)}>Record bounds with your results fixed (hypothetical)</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Team</th><th className="num">Open games</th><th>Wins all open</th><th>Loses all open</th><th>What the record alone says</th></tr></thead>
              <tbody>
                {bounds.teams.map((b) => (
                  <tr key={b.team_id} className={b.team_id === d.focus_team_id ? "mine" : ""}>
                    <td>{name(b.team_id)}</td>
                    <td className="num">{b.open_games}</td>
                    <td className="num">{rec(b.best)}</td>
                    <td className="num">{rec(b.worst)}</td>
                    <td className={styles.decide}><BoundText b={b} spots={spots} /> <Badge value="Not claimed" title="No clinch or elimination is claimed while a rule it needs is unmapped" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className={cx(styles.scenarioNote, styles.padSide)}>{bounds.rule}</p>
        </div>
        <Method title="What-if assumptions and method">
          <ul className="reasons">
            <li>{w.method}</li>
            {w.assumptions.map((a, i) => <li key={i}>{a}</li>)}
          </ul>
        </Method>
      </div>
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
    <Section title={<>What decides {t.name}&apos;s cutline <Hyp /></>}>
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
          <thead><tr><th>Week</th><th>Opponent</th><th>In this hypothetical scenario</th><th>If win</th><th>If loss</th><th>Try it</th></tr></thead>
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

// ------------------------------------------------------------- rooting guide (issue #217) --

function RootPlace({ s }: { s: PrRootingSide }) {
  const p = s.scenario;
  return <>#{p.rank ?? "—"}{p.tied ? "=" : ""} <StatusBadge s={p.status} /></>;
}

function Rooting({ d, g, t, q, name }: { d: PlayoffRace; g: PrRooting; t: PrTeam; q: Q; name: Name }) {
  const when = g.period !== null ? weeksText(g.weeks, g.period) : null;
  const preferred = g.games.filter((x) => x.prefer !== null);
  const mixed = g.games.filter((x) => x.conflict);
  const own = g.own_game !== null ? d.matchups.regular.find((m) => m.matchup_id === g.own_game) : undefined;
  return (
    <Section
      title={<>Rooting guide{when ? `: ${when}` : ""} <Hyp /></>}
      subtitle={`Other games ${t.name} does not play, one at a time against this scenario. Deterministic, no probability.`}
      flush
      footnote={<>Each row sets one game&apos;s result and keeps every other result as in this scenario. {g.later_open_games > 0
        ? `${g.later_open_games} later open ${plural(g.later_open_games, "game")} also remain and can change any of this. ` : ""}
        Ties are not compared. Nothing here is a clinch or elimination: {g.conclusions.status === "blocked"
        ? "the league's tiebreak and seeding rules are not mapped." : "no claim is made."}</>}
    >
      {g.status === "no_games_remaining" && <p className={styles.padded}>No open regular-season games remain: there is nothing left to root for.</p>}
      {g.status === "no_other_games" && <p className={styles.padded}>Every open game {when ? `in ${when} ` : ""}involves {t.name}; see its own games above.</p>}
      {g.status === "ok" && (
        <>
          <p className={styles.padded}>
            {preferred.length > 0
              ? <>{preferred.length} of {g.games.length + g.no_effect.length} other {plural(g.games.length + g.no_effect.length, "game")} {plural(preferred.length, "has", "have")} a better side for {t.name}.</>
              : <>No other game {when ? `in ${when} ` : ""}has a better side for {t.name} on its own.</>}
            {mixed.length > 0 && <> {mixed.length} {plural(mixed.length, "game is", "games are")} mixed.</>}
            {own && <> {t.name}&apos;s own game, against {name(own.home_team_id === t.team_id ? own.away_team_id : own.home_team_id)}, is not part of this guide.</>}
          </p>
          {g.range_abstain.length > 0 && (
            <div className={styles.padded}>
              <Notice tone="info">Possible range not used: {g.range_abstain.join(" ")} Only the scenario place is compared.</Notice>
            </div>
          )}
          {g.games.length > 0 && <RootingTable rows={g.games} q={q} name={name} />}
          {g.no_effect.length > 0 && (
            <p className={styles.padded}>No effect on {t.name} either way: {g.no_effect.map((x) =>
              `${name(x.home_team_id)} vs ${name(x.away_team_id)}`).join(", ")}.</p>
          )}
        </>
      )}
    </Section>
  );
}

function RootingTable({ rows, q, name }: { rows: PrRootingGame[]; q: Q; name: Name }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Game</th><th>Root for</th><th>If home wins</th><th>If away wins</th><th>Why</th><th>Try it</th></tr></thead>
        <tbody>
          {rows.map((x) => (
            <tr key={x.matchup_id}>
              <td>{name(x.home_team_id)} vs {name(x.away_team_id)}
                <span className={styles.sub}>{x.status === "in_progress" ? "under way" : weeksText(x.weeks, x.matchup_period)}
                  {x.scenario_result ? ` · scenario: ${x.scenario_result === "tie" ? "tie" : name(x.scenario_result === "home" ? x.home_team_id : x.away_team_id)}` : " · unresolved"}</span></td>
              <td>{x.prefer ? <b>{name(x.prefer === "home" ? x.home_team_id : x.away_team_id)}</b> : <Badge value="Mixed" tone="warn" title="The scenario and the possible range point to different sides" />}</td>
              <td><RootPlace s={x.if_home} /></td>
              <td><RootPlace s={x.if_away} /></td>
              <td className={styles.remaining}>{rootingReason(x, (id) => name(id))}</td>
              <td><span className={styles.overrideLinks}>
                <Link href={withOverride(q, x.matchup_id, "home")}>Home wins</Link>
                <Link href={withOverride(q, x.matchup_id, "away")}>Away wins</Link>
              </span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ----------------------------------------------------------------------- standings --

function Standings({ d, name, L }: { d: PlayoffRace; name: Name; L: L }) {
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
      title={<>Actual vs hypothetical standings <Hyp /></>}
      subtitle={`"Actual now" is ${L.prov}'s record; "Hypothetical" adds ${s.base === "projected" ? "projected winners and " : ""}any result you set.`}
      flush
      footnote={<>&ldquo;=&rdquo; marks a shared rank (equal winning percentage; no tiebreak applied). &ldquo;+n?&rdquo; counts games still unresolved in this scenario. Bubble teams have a remaining game whose result changes their cutline status. {after === null
        ? (spots === null ? "Playoff spots are unknown, so no playoff line is drawn." : "No team has a decided game in this scenario yet, so no playoff line position is drawn.")
        : <>The playoff line is after {spots} spots{tie ? `; a tied group straddles it, so it is drawn above them and they share ${tie.spots_left} ${plural(tie.spots_left, "spot")}` : ""}.</>}</>}
    >
      <div className="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Team</th><th>Actual now</th><th title={`${L.prov}'s own seed`}>{L.prov} seed</th><th>Hypothetical</th><th>Move</th><th>Hypothetical status</th></tr></thead>
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
      title={<>Hypothetical record: win share (wins ÷ games) <Hyp /></>}
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
        ariaLabel="Hypothetical scenario win share (wins divided by games) by team with the playoff line"
      />
    </Section>
  );
}

// ------------------------------------------------------------- spotlight margins chart --

function Margins({ t, L }: { t: PrTeam; L: L }) {
  const rows = t.remaining.filter((r) => r.status !== "bye");
  if (rows.length === 0) return null;
  return (
    <Section
      title={`${t.name}: projected margin by remaining matchup`}
      footnote={`Margins compare ${L.proj}-projected optimal lineups; they are not win probabilities. A dash means no projected margin (game under way, a projection is incomplete, or its weeks are unknown).`}
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
      title={<>Bubble teams · {cut.playoff_spots} playoff spots <Hyp /></>}
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
            <thead><tr><th>Team</th><th>Hypothetical</th><th>Hypothetical status</th><th>Games that decide it</th><th /></tr></thead>
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

function SideCell({ m, side, L }: { m: PrMatchup; side: "home" | "away"; L: L }) {
  const s = side === "home" ? m.home_projection : m.away_projection;
  if (m.status === "in_progress") {
    const v = side === "home" ? m.home_points : m.away_points;
    return <span className="faint" title={`${L.prov} points as of the last sync; not final`}>{pts(v)} live</span>;
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

function Matchups({ d, q, open, L }: { d: PlayoffRace; q: Q; open: PrMatchup[]; L: L }) {
  const periods = [...new Set(open.map((m) => m.matchup_period))].sort((a, b) => a - b);
  const maxAbs = Math.max(1, ...open.map((m) => Math.abs(m.projected_margin ?? 0)));
  const focus = d.focus_team_id;
  const highIds = new Set((focus !== null ? d.scenario.leverage[String(focus)] ?? [] : []).filter((g) => g.high_leverage).map((g) => g.matchup_id));
  return (
    <Section
      title="Remaining regular-season matchups"
      flush={open.length > 0}
      footnote={open.length > 0 ? <>Projected totals are {L.proj} projections through the lineup optimizer. The margin is home minus away, drawn toward the projected leader. &ldquo;Close&rdquo; means within 5% of the mean projected total. Highlighted rows involve the spotlight team{highIds.size > 0 ? "; High leverage marks games that change its cutline status" : ""}. Overrides are in the URL.</> : undefined}
    >
      {open.length === 0 ? <EmptyState title="No remaining regular-season matchups">The {L.prov} schedule has no open regular-season games.</EmptyState> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Home</th><th className="num">Proj.</th><th>Margin</th><th className="num">Proj.</th><th>Away</th><th>Hypothetical result</th><th>Set a result</th></tr></thead>
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
                        <td className="num"><SideCell m={m} side="home" L={L} /></td>
                        <td>
                          <MarginBar m={m} maxAbs={maxAbs} />
                          {m.close && <> <Badge value="Close" tone="warn" title="Projected margin within 5% of the mean projected total" /></>}
                        </td>
                        <td className="num"><SideCell m={m} side="away" L={L} /></td>
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

function Power({ d, L }: { d: PlayoffRace; L: L }) {
  const rows = [...d.teams].sort((a, b) => (a.current.rank ?? 99) - (b.current.rank ?? 99) || (a.espn_seed ?? 99) - (b.espn_seed ?? 99) || a.team_id - b.team_id);
  return (
    <Section
      title="League power view: actual vs forward"
      flush
      footnote={`Ordered by actual standing. No composite power score: actual results (left) and ${L.proj}-based forward projections (right) are kept separate. Badges: W, L or T is the hypothetical result in this scenario (? = unresolved), followed by the projected margin from this team's side.`}
    >
      <div className="table-wrap">
        <table>
          <thead><tr><th>Team</th><th>Actual record</th><th className="num">PF</th><th className="num">PA</th><th>All-play</th>
            <th className="num">Proj. reg-season pts</th><th>Remaining (hypothetical result · projected margin)</th><th>Coverage</th></tr></thead>
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

function KnownPlayoffs({ d, L }: { d: PlayoffRace; L: L }) {
  return (
    <Section title={`Playoff matchups ${L.prov} already lists`} flush footnote={`Shown as ${L.prov} reports them; future playoff opponents are not modeled.`}>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Period</th><th>Tier</th><th>Home</th><th>Away</th><th>{L.prov} winner</th></tr></thead>
          <tbody>{d.matchups.playoff_known.map((m) => (
            <tr key={m.matchup_id}><td>{m.weeks?.join("–") ?? m.matchup_period}</td><td>{m.playoff_tier}</td><td>{m.home_name ?? "TBD"}</td><td>{m.away_name ?? "—"}</td><td>{m.espn_winner}</td></tr>
          ))}</tbody>
        </table>
      </div>
    </Section>
  );
}

function MethodNotes({ d, L }: { d: PlayoffRace; L: L }) {
  const cov = d.schedule_coverage;
  return (
    <Method title="How this works (and what it cannot say)">
      <ul className="reasons">
        <li>{d.rules.ordering}</li>
        <li>{d.rules.projection}</li>
        <li>{d.rules.close}</li>
        <li>{d.rules.clinch}</li>
        {d.notes.map((n, i) => <li key={i}>{n}</li>)}
        <li>{L.prov} seeding/tie settings: {Object.keys(d.rules.espn_rules_reported).length
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
