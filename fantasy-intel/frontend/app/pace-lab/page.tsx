import type { ReactNode } from "react";
import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import {
  EmptyState, Glossary, Method, Notice, PageHeader, Section, Stat, StatStrip, Verdict, VerdictFigure,
} from "@/components/ui";
import { api } from "@/lib/api";
import { int, num, pct, when } from "@/lib/format";
import {
  CONTRACT, SCRIPTS, SCRIPT_LABEL, SCRIPT_RULE, VERDICT_TEXT, backtestSentences, coverageText, missingWhy, modelLabel,
  paceApiPath, paceHref, parsePaceState, resultText, seasonsText, type Backtest, type Estimate, type PaceLabResponse,
  type PaceState, type ScoreBlock,
} from "@/lib/pace-lab";
import { plural } from "@/lib/plural";
import type { SearchParams } from "@/lib/sort";
import { Controls } from "./Controls";
import { Scenario } from "./Scenario";
import styles from "./pace-lab.module.css";

export const metadata = { title: "Pace lab" };

const TITLE = "Pace lab";
const EYEBROW = "Players · Research";

// Play-pace and game-script lab (issue #211): every NFL offense's observed play volume, neutral pass rate and per-script
// tendencies from stored nflverse play-by-play aggregates, the model assumptions (shrunk estimates) kept visibly apart,
// a hypothetical script mix, and the frozen backtest that says whether the assumptions forecast anything. No betting
// line, no projection, no probability.
export default async function PaceLabPage({ searchParams }: PageProps<"/pace-lab">) {
  const sp = (await searchParams) as SearchParams;
  const state = parsePaceState(sp);
  const res = await api<PaceLabResponse>(paceApiPath(state));
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} />
        {res.status === 422 ? <Notice tone="caution" title="This filter cannot be answered">{res.error} <Link href={paceHref(parsePaceState({}))} prefetch={false}>Reset the filters</Link>.</Notice>
          : <BackendProblem error={res.error} kind={res.kind} status={res.status} />}
      </div>
    );
  }
  const d = res.data;
  if (d.contract !== CONTRACT) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} />
        <Notice tone="caution" title="The backend sent an unexpected format">
          This page reads the {CONTRACT} response and the backend answered with {d.contract ? `“${d.contract}”` : "no version"}. Update the backend and the app together.
        </Notice>
      </div>
    );
  }
  if (d.status === "no_data" || !d.query) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} meta="Team play volume, neutral pass rate and game script from nflverse play-by-play" />
        <EmptyState title="No play-by-play aggregates are stored">{d.message ?? "Run the NFL sync."}</EmptyState>
      </div>
    );
  }
  const q = d.query;
  const bt = d.backtest;
  const said = backtestSentences(bt);
  return (
    <div className="stack">
      <PageHeader
        eyebrow={EYEBROW}
        title={TITLE}
        meta={<>{seasonsText(q.seasons)} regular {plural(q.seasons.length, "season")}{q.through_week !== null ? `, through week ${q.through_week}` : ""} ·
          nflverse play-by-play · NFL sync {when(d.source?.nfl_synced_at ?? null)} · research only, not a forecast</>}
      >
        <Controls state={state} d={d} />
      </PageHeader>

      <Notice tone="info" title="Observed and assumed are kept apart">
        <b>Observed</b> columns are what happened in the games counted. <b>Assumption</b> columns are this lab&apos;s model:
        each team&apos;s average pulled toward the league average by a stated number of games or plays. {said.pace} {said.neutral}
      </Notice>
      <Alerts d={d} />

      {d.team ? <TeamView d={d} state={state} bt={bt} said={said} /> : <Overview d={d} state={state} />}

      <BacktestSection bt={bt} />

      <Section title="What this lab cannot measure">
        <ul className="reasons">{d.gaps.map((g, i) => <li key={i}>{g}</li>)}</ul>
      </Section>

      <Glossary terms={["Neutral pass rate", "Game script", "Shrunk estimate", "Plays per drive", "Hypothetical scenario", "—"]} />
      <Method title="How the lab is built">
        <dl className={styles.defs}>
          {Object.entries(d.definitions).map(([k, v]) => (
            <div key={k}><dt>{k.replaceAll("_", " ")}</dt><dd>{v}</dd></div>
          ))}
        </dl>
        <p className={styles.note}>
          Sources: {d.source?.tables.join(", ")} (nflverse play-by-play and schedules, as the NFL sync stores them). No
          betting line, market total or third-party ranking is read. Model {d.version}, {d.model_status.replace("_", " ")}:
          nothing here feeds a projection, lineup, trade or waiver recommendation.
        </p>
      </Method>
    </div>
  );
}

function Alerts({ d }: { d: PaceLabResponse }) {
  const c = d.coverage!;
  const codes = d.source?.franchise_codes ?? [];
  return (
    <>
      {c.missing_pbp > 0 && (
        <Notice tone="caution">
          {c.missing_pbp} final {plural(c.missing_pbp, "team-game")} in this filter {c.missing_pbp === 1 ? "has" : "have"} no stored
          play-by-play aggregate: listed as missing, never as zero plays.
        </Notice>
      )}
      {c.overtime_unknown > 0 && (
        <Notice tone="caution">
          The overtime flag is not stored for {c.overtime_unknown} {plural(c.overtime_unknown, "team-game")}: they count toward per-game volume, and an
          overtime game among them would inflate it.
        </Notice>
      )}
      {c.not_final > 0 && <Notice tone="info">{c.not_final} {plural(c.not_final, "team-game")} without a final score {c.not_final === 1 ? "is" : "are"} not counted.</Notice>}
      {codes.length > 0 && (
        <Notice tone="info">
          Franchise codes merged across seasons: {codes.map(([from, to]) => `${from} → ${to}`).join(", ")}. A relocated team keeps one history.
        </Notice>
      )}
    </>
  );
}

function EstimateCells({ e, fmt }: { e: Estimate; fmt: (v: number | null) => ReactNode }) {
  return (
    <>
      <td className="num">{fmt(e.observed)}<span className={styles.sub}>{e.n.toLocaleString("en-US")} {e.unit}</span></td>
      <td className={`num ${styles.assumed}`}>{fmt(e.estimate)}
        <span className={styles.sub}>
          prior {fmt(e.prior)}, k = {e.prior_strength}{e.data_weight !== null ? ` · ${Math.round(e.data_weight * 100)}% own data` : ""}
        </span>
      </td>
      <td className="num">{e.missing || e.excluded ? <span className={styles.sub}>{[e.missing ? `${e.missing} missing` : null, e.excluded ? `${e.excluded} overtime left out` : null].filter(Boolean).join(" · ")}</span> : <span className={styles.sub}>none</span>}</td>
    </>
  );
}

const f1 = (v: number | null) => num(v, 1, "No game in this filter");
const f2 = (v: number | null) => num(v, 2, "No drive in this filter");
const fp = (v: number | null) => pct(v, 1, "No play in this filter");
/** Plain text for notes (a missing value reads as a dash in words, never 0). */
const fx = (v: number | null, digits: number) => (v === null ? "—" : v.toFixed(digits));

function TeamView({ d, state, bt, said }: { d: PaceLabResponse; state: PaceState; bt: Backtest; said: ReturnType<typeof backtestSentences> }) {
  const t = d.team!;
  const s = t.summary;
  const league = d.league!;
  const ppg = s.plays_per_game;
  return (
    <>
      <Verdict
        tone="info"
        eyebrow={`${t.team}${state.opp ? ` vs ${state.opp}` : ""} · ${seasonsText(d.query!.seasons)}`}
        headline={ppg.observed !== null
          ? <>{t.team} ran {ppg.observed.toFixed(1)} plays per game over {ppg.n} {plural(ppg.n, "game")}; the lab assumes {ppg.estimate?.toFixed(1)}</>
          : <>No counted game for {t.team} in this filter; the lab falls back to the league&apos;s {ppg.prior?.toFixed(1) ?? "—"} plays per game</>}
        figures={<>
          <VerdictFigure label="Observed plays/g" value={f1(ppg.observed)} note={`${ppg.n} ${plural(ppg.n, "game")}`} />
          <VerdictFigure label="Assumed plays/g" value={f1(ppg.estimate)} note={`league ${fx(league.plays_per_game, 1)}`} emphasis />
          <VerdictFigure label="Neutral pass rate" value={fp(s.neutral_pass_rate.observed)} note={`assumed ${s.neutral_pass_rate.estimate !== null ? `${(s.neutral_pass_rate.estimate * 100).toFixed(1)}%` : "—"}`} />
          <VerdictFigure label="Plays per drive" value={f2(s.plays_per_drive.observed)} note={`${s.plays_per_drive.n} drives`} />
        </>}
      >
        {said.pace} Treat differences in pace as a description of past games.
      </Verdict>

      <Section title="Pace and tendencies" subtitle={`${t.team}, ${s.games} counted ${plural(s.games, "game")}${state.window !== "season" ? ` (${t.window_games} in the window)` : ""}`} flush>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Measure</th>
                <th scope="col" className="num">Observed <span className={styles.sub}>sample</span></th>
                <th scope="col" className={`num ${styles.assumed}`}>Assumption <span className={styles.sub}>shrunk toward the league</span></th>
                <th scope="col" className="num">Missing</th>
              </tr>
            </thead>
            <tbody>
              <tr><th scope="row">Plays per game</th><EstimateCells e={s.plays_per_game} fmt={f1} /></tr>
              <tr><th scope="row">Drives per game</th><EstimateCells e={s.drives_per_game} fmt={f1} /></tr>
              <tr><th scope="row">Plays per drive</th><EstimateCells e={s.plays_per_drive} fmt={f2} /></tr>
              <tr><th scope="row">Neutral pass rate</th><EstimateCells e={s.neutral_pass_rate} fmt={fp} /></tr>
              <tr><th scope="row">Pass rate, all plays</th><EstimateCells e={s.pass_rate} fmt={fp} /></tr>
              <tr><th scope="row">Targets per dropback</th><EstimateCells e={s.targets_per_dropback} fmt={fp} /></tr>
              <tr>
                <th scope="row">Pass rate over expected</th>
                <td className="num">{s.proe.observed !== null ? `${s.proe.observed > 0 ? "+" : ""}${s.proe.observed.toFixed(1)} pts` : f1(null)}<span className={styles.sub}>{s.proe.n.toLocaleString("en-US")} plays</span></td>
                <td className="num"><span className={styles.sub} title="nflverse's own model output; shown as observed only">not modelled</span></td>
                <td className="num"><span className={styles.sub}>—</span></td>
              </tr>
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="By game script" subtitle="Games classified by final margin; outcome-based, so a script describes games that ended that way" flush
        footnote="Assumptions here are pulled toward the team's overall assumption shifted by the league's difference for that script (3 games, 120 plays).">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Script</th>
                <th scope="col" className="num">Games</th>
                <th scope="col" className="num">Observed plays/g</th>
                <th scope="col" className={`num ${styles.assumed}`}>Assumed plays/g</th>
                <th scope="col" className="num">Observed pass rate</th>
                <th scope="col" className={`num ${styles.assumed}`}>Assumed pass rate</th>
                <th scope="col" className="num">League plays/g</th>
              </tr>
            </thead>
            <tbody>
              {SCRIPTS.map((k) => {
                const e = t.scripts[k];
                return (
                  <tr key={k}>
                    <th scope="row">{SCRIPT_LABEL[k]}<span className={styles.sub}>{SCRIPT_RULE[k]}</span></th>
                    <td className="num">{e.games}</td>
                    <td className="num">{num(e.plays_per_game.observed, 1, `No ${k} game without overtime in this filter`)}</td>
                    <td className={`num ${styles.assumed}`}>{f1(e.plays_per_game.estimate)}<span className={styles.sub}>{Math.round((e.plays_per_game.data_weight ?? 0) * 100)}% own data</span></td>
                    <td className="num">{pct(e.pass_rate.observed, 1, `No ${k} game in this filter`)}</td>
                    <td className={`num ${styles.assumed}`}>{fp(e.pass_rate.estimate)}<span className={styles.sub}>{Math.round((e.pass_rate.data_weight ?? 0) * 100)}% own data</span></td>
                    <td className="num">{f1(league.scripts[k].plays_per_game)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Section>

      {d.scenario && <Scenario state={state} d={d} bt={bt} sentence={said.scenario} />}

      {d.matchup && (
        <Section title={`Matchup assumption: ${t.team} against ${d.matchup.opponent}`}>
          <StatStrip compact>
            <Stat label={`${t.team} plays/g (assumed)`} value={f1(d.matchup.team_plays_per_game.estimate)} note={`${d.matchup.team_plays_per_game.n} games in the window`} />
            <Stat label={`${d.matchup.opponent} plays allowed/g (assumed)`} value={f1(d.matchup.opponent_plays_allowed.estimate)} note={`observed ${fx(d.matchup.opponent_plays_allowed.observed, 1)} over ${d.matchup.opponent_plays_allowed.n} games`} />
            <Stat label="League plays/g" value={f1(d.matchup.league_plays_per_game)} />
            <Stat label="Additive assumption" value={f1(d.matchup.additive_plays)} note="team + opponent allowed − league" tone="caution" />
          </StatStrip>
          <p className={styles.note}>{said.matchup} The head-to-head table below covers {d.matchup.head_to_head_games} {plural(d.matchup.head_to_head_games, "game")}.</p>
        </Section>
      )}

      <Section title="Team-week log" subtitle="Every game in the filter; a dash is missing, never zero" flush>
        {t.games.length === 0 ? <EmptyState title="No game in this filter">Widen the window or clear the opponent.</EmptyState> : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Week</th><th scope="col">Opponent</th><th scope="col">Result</th><th scope="col">Script</th>
                  <th scope="col" className="num">Plays</th><th scope="col" className="num">Drives</th>
                  <th scope="col" className="num">Plays/drive</th><th scope="col" className="num">Neutral pass</th>
                  <th scope="col" className="num">Pass rate</th><th scope="col" className="num">PROE</th>
                </tr>
              </thead>
              <tbody>
                {t.games.map((g) => (
                  <tr key={`${g.season}-${g.week}`}>
                    <th scope="row">{q2(d) ? `${g.season} ` : ""}W{g.week}</th>
                    <td>{g.home === false ? "@ " : ""}{g.opponent ?? "—"}{g.listed_as && g.listed_as !== t.team ? <span className={styles.sub}>listed as {g.listed_as}</span> : null}</td>
                    <td>{resultText(g) ?? "—"}{g.overtime ? <span className={styles.sub}>overtime: left out of per-game volume</span> : null}</td>
                    <td>{g.script ? SCRIPT_LABEL[g.script] : "—"}</td>
                    <td className="num">{int(g.plays, missingWhy(g, "plays"))}</td>
                    <td className="num">{int(g.drives, missingWhy(g, "drives"))}</td>
                    <td className="num">{num(g.plays_per_drive, 2, missingWhy(g, "drives"))}</td>
                    <td className="num">{pct(g.neutral_pass_rate, 0, missingWhy(g, "neutral"))}{g.neutral_plays !== null ? <span className={styles.sub}>{g.neutral_plays} plays</span> : null}</td>
                    <td className="num">{pct(g.pass_rate, 0, missingWhy(g, "pass"))}</td>
                    <td className="num">{num(g.proe, 1, missingWhy(g, "pass"))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      <p className={styles.note}><Link href={paceHref(state, { team: null, opp: null, mix: null })} prefetch={false}>Back to every offense</Link></p>
    </>
  );
}

const q2 = (d: PaceLabResponse) => (d.query?.seasons.length ?? 1) > 1;

function Overview({ d, state }: { d: PaceLabResponse; state: PaceState }) {
  const rows = d.overview ?? [];
  const league = d.league!;
  const top = rows[0];
  return (
    <>
      <Verdict
        tone="info"
        eyebrow={seasonsText(d.query!.seasons)}
        headline={top ? <>{top.team} has the highest assumed play volume, {top.plays_per_game.estimate?.toFixed(1)} plays per game; the league averages {league.plays_per_game?.toFixed(1) ?? "—"}</> : "No offense in this filter"}
        figures={<>
          <VerdictFigure label="League plays/g" value={f1(league.plays_per_game)} note={`${league.games.toLocaleString("en-US")} team-games`} />
          <VerdictFigure label="League neutral pass rate" value={fp(league.neutral_pass_rate)} note={`${league.neutral_plays.toLocaleString("en-US")} neutral plays`} />
          <VerdictFigure label="Plays/g when trailing" value={f1(league.scripts.trailing.plays_per_game)} note={`${league.scripts.trailing.games} games`} />
          <VerdictFigure label="Plays/g when leading" value={f1(league.scripts.leading.plays_per_game)} note={`${league.scripts.leading.games} games`} />
        </>}
      >
        Pick a team for its weekly log, its per-script tendencies and a hypothetical script mix.
      </Verdict>
      <Section title="Every offense" subtitle="Sorted by assumed plays per game; observed values and samples beside them" flush>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Team</th>
                <th scope="col" className="num">Games</th>
                <th scope="col" className="num">Observed plays/g</th>
                <th scope="col" className={`num ${styles.assumed}`}>Assumed plays/g</th>
                <th scope="col" className="num">Observed neutral pass</th>
                <th scope="col" className={`num ${styles.assumed}`}>Assumed neutral pass</th>
                <th scope="col" className="num">Plays/drive</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.team}>
                  <th scope="row"><Link href={paceHref(state, { team: r.team, opp: null, mix: null })} prefetch={false}>{r.team}</Link></th>
                  <td className="num">{r.games}{r.missing_pbp || r.overtime_games ? <span className={styles.sub}>{[r.missing_pbp ? `${r.missing_pbp} missing` : null, r.overtime_games ? `${r.overtime_games} OT` : null].filter(Boolean).join(" · ")}</span> : null}</td>
                  <td className="num">{f1(r.plays_per_game.observed)}</td>
                  <td className={`num ${styles.assumed}`}>{f1(r.plays_per_game.estimate)}</td>
                  <td className="num">{fp(r.neutral_pass_rate.observed)}<span className={styles.sub}>{r.neutral_pass_rate.n} plays</span></td>
                  <td className={`num ${styles.assumed}`}>{fp(r.neutral_pass_rate.estimate)}</td>
                  <td className="num">{f2(r.plays_per_drive.observed)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}

function ScoreTable({ block, caption, digits }: { block: ScoreBlock; caption: string; digits: number }) {
  const keys = Object.keys(block.models);
  return (
    <div className="table-wrap">
      <table>
        <caption className={styles.caption}>{caption}</caption>
        <thead>
          <tr><th scope="col">Estimate</th><th scope="col" className="num">MAE</th><th scope="col" className="num">RMSE</th><th scope="col" className="num">Bias</th><th scope="col">Verdict</th></tr>
        </thead>
        <tbody>
          {keys.map((k) => {
            const v = block.verdicts[k];
            const c = block.best_baseline ? block.comparisons[k]?.[block.best_baseline] : undefined;
            return (
              <tr key={k}>
                <th scope="row">{modelLabel(k)}</th>
                <td className="num">{block.models[k].mae.toFixed(digits)}</td>
                <td className="num">{block.models[k].rmse.toFixed(digits)}</td>
                <td className="num">{block.models[k].bias.toFixed(digits)}</td>
                <td>{v ? <>{VERDICT_TEXT[v]}{c ? <span className={styles.sub}>MAE difference {c.mean > 0 ? "+" : ""}{c.mean.toFixed(digits)} [{c.low.toFixed(digits)}, {c.high.toFixed(digits)}]</span> : null}</> : k === block.best_baseline ? "Better simple baseline" : "Simple baseline"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function BacktestSection({ bt }: { bt: Backtest }) {
  if (bt.status !== "frozen") {
    return <Section title="Backtest"><Notice tone="info">{bt.message}</Notice></Section>;
  }
  const cond = coverageText(bt.range_coverage.conditional_on_script);
  const pre = coverageText(bt.range_coverage.pregame_mixture);
  return (
    <Section title="Backtest" subtitle={`Frozen walk-forward, ${seasonsText(bt.seasons)} regular seasons: each game predicted from earlier weeks of the same season only`}
      footnote={`Spec ${bt.version} (${bt.spec_sha256.slice(0, 12)}), run ${bt.run_at.slice(0, 10)} on ${bt.data.games.toLocaleString("en-US")} stored team-games. 95% intervals from a bootstrap over weeks. Nothing is promoted to a projection.`}>
      <ScoreTable block={bt.plays} caption={`Next-game plays (${bt.plays.n.toLocaleString("en-US")} team-games, overtime excluded)`} digits={2} />
      <ScoreTable block={bt.neutral_pass_rate} caption={`Next-game neutral pass rate (${bt.neutral_pass_rate.n.toLocaleString("en-US")} team-games, weighted by neutral plays)`} digits={4} />
      <p className={styles.note}>
        Scenario ranges: the range for the script that actually happened contained {cond ?? "—"}; the pregame range from the team&apos;s observed mix
        contained {pre ?? "—"}.
      </p>
    </Section>
  );
}
