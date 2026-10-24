import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { ScatterChart, type ScatterPoint } from "@/components/charts";
import { EmptyState, Method, Notice, PageHeader, Pos, Section, Stat, StatStrip, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import {
  OUTCOME_LABEL, formatDifference, formatSplit, parseSplitsState, reasonList, seasonOptions, splitsApiQuery, splitsHref,
  splitsUnavailable, visibleWeeks, windowSentence, type SplitsState,
} from "@/lib/game-splits";
import type {
  GameSplitsResponse, SplitPlayersResponse, SplitsEnvironment, SplitsNeedsTeammate, SplitsPlayer, SplitsResult, SplitsWeek,
} from "@/lib/game-splits-types";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import { plural } from "@/lib/plural";
import styles from "./splits.module.css";

export const metadata = { title: "Game splits" };

type Params = Record<string, string | string[] | undefined>;

/** One player's games with a teammate on the field against games without him. Every input is in the URL. */
export default async function GameSplitsPage({ searchParams }: { searchParams: Promise<Params> }) {
  const state = parseSplitsState(await searchParams);
  const season = state.seasonTo ?? state.seasonFrom;
  const [players, res, options] = await Promise.all([
    api<SplitPlayersResponse>(`/api/research/game-splits/players${season ? `?season=${season}` : ""}`),
    state.player ? api<GameSplitsResponse>(`/api/research/game-splits?${splitsApiQuery(state)}`) : null,
    // The teammate list comes from the same window without a teammate (its own cache entry).
    state.player && state.teammate
      ? api<GameSplitsResponse>(`/api/research/game-splits?${splitsApiQuery({ ...state, teammate: null })}`)
      : null,
  ]);
  const result = res?.ok ? res.data : null;
  const pick = (options?.ok ? options.data : result) as GameSplitsResponse | null;
  const teammates = pick?.status === "needs_teammate" ? pick.teammate_options : [];
  const ready = result && (result.status === "ok" || result.status === "low_n") ? result : null;
  // The picker answers 200 with status "no_data" when the NFL tables are missing: show the setup message, not a form.
  const noData = players.ok && players.data.status === "no_data";

  return (
    <div className="stack">
      <PageHeader
        eyebrow="Players · Research lab"
        title="Game splits"
        meta={ready
          ? <>{ready.player.name} with and without {ready.teammate.name} · {windowSentence(ready)} · nflverse data, full PPR</>
          : "A player's games with a teammate on the field against games without him"}
        actions={<Link href="/research">Lab overview</Link>}
      />

      <p className={styles.lede}>
        Split a receiver&apos;s or running back&apos;s past games by whether a teammate played. Each game counts only when the
        teammate&apos;s status is on record; an unknown status is excluded and listed, never treated as an absence. The split
        describes what happened. It is not a forecast, and the groups differ in more than this one teammate.
      </p>

      {!players.ok && <BackendProblem error={players.error} kind={players.kind} status={players.status} />}
      {players.ok && players.data.status === "no_data" && (
        <Section>
          <EmptyState title="No NFL data for splits">{players.data.message ?? "Game splits are not available yet: the NFL data they need has not been loaded."}</EmptyState>
        </Section>
      )}
      {players.ok && !noData && <SplitsForm state={state} players={players.data} teammates={teammates} />}

      {res && !res.ok && (splitsUnavailable(res.status, res.error)
        ? <Section><EmptyState title="No NFL data for splits">Game splits are not available yet: no NFL usage data has been loaded.</EmptyState></Section>
        : res.status === 404 || res.status === 422
          ? <Notice tone="caution" title={res.status === 404 ? "Not found" : "Check the window"}>{res.error}</Notice>
          : <BackendProblem error={res.error} kind={res.kind} status={res.status} />)}

      {!state.player && players.ok && !noData && (
        <Section>
          <EmptyState title="Choose a player">
            Pick a wide receiver, running back or tight end, then a teammate who played with him in the window.
          </EmptyState>
        </Section>
      )}
      {result?.status === "no_data" && !noData && <Section><EmptyState title="No NFL data for splits">{result.message}</EmptyState></Section>}
      {result?.status === "needs_teammate" && <ChooseTeammate r={result} state={state} />}
      {ready && <SplitResult r={ready} state={state} />}
      <SplitsMethod />
    </div>
  );
}

async function SplitsForm({ state, players, teammates }: {
  state: SplitsState;
  players: SplitPlayersResponse;
  teammates: SplitsNeedsTeammate["teammate_options"];
}) {
  const action = withLeague("/research/splits", (await currentLeague())?.id);
  const seasons = seasonOptions(players.seasons, players.season);
  const byPos = ["WR", "RB", "TE"].map((p) => [p, players.players.filter((x) => x.position === p)] as const);
  const weeks = Array.from({ length: 18 }, (_, i) => i + 1);
  const known = players.players.some((p) => p.gsis_id === state.player);
  return (
    <form className={styles.form} action={action} method="get" aria-label="Game split inputs">
      {/* The player the teammate list below was built for: a different submitted player drops the teammate. */}
      {state.player && <input type="hidden" name="for_player" value={state.player} />}
      <label className={`${styles.field} ${styles.wide}`}>
        <span>Player</span>
        <select name="player" defaultValue={state.player ?? ""} required>
          <option value="" disabled>Choose a WR, RB or TE</option>
          {state.player && !known && <option value={state.player}>{state.player}</option>}
          {byPos.map(([pos, list]) => list.length > 0 && (
            <optgroup key={pos} label={pos}>
              {list.map((p) => <option key={p.gsis_id} value={p.gsis_id}>{p.name} · {p.team ?? "—"}</option>)}
            </optgroup>
          ))}
        </select>
      </label>
      <label className={`${styles.field} ${styles.wide}`}>
        <span>Teammate</span>
        <select name="teammate" defaultValue={state.teammate ?? ""} disabled={!state.player}>
          <option value="">{state.player ? "Choose a teammate" : "Choose a player first"}</option>
          {state.teammate && !teammates.some((t) => t.gsis_id === state.teammate) && (
            <option value={state.teammate}>{state.teammate}</option>
          )}
          {teammates.map((t) => (
            <option key={t.gsis_id} value={t.gsis_id}>{t.name} · {t.position} · {t.teams.join(", ")}</option>
          ))}
        </select>
      </label>
      <fieldset className={styles.window}>
        <legend>Window (inclusive)</legend>
        <label className={styles.field}>
          <span>From season</span>
          <select name="season_from" defaultValue={state.seasonFrom ?? ""}>
            <option value="">Default</option>
            {seasons.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className={styles.field}>
          <span>From week</span>
          <select name="week_from" defaultValue={state.weekFrom ?? ""}>
            <option value="">1</option>
            {weeks.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
        <label className={styles.field}>
          <span>To season</span>
          <select name="season_to" defaultValue={state.seasonTo ?? ""}>
            <option value="">Same</option>
            {seasons.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className={styles.field}>
          <span>To week</span>
          <select name="week_to" defaultValue={state.weekTo ?? ""}>
            <option value="">18</option>
            {weeks.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
        <label className={styles.field}>
          <span>Games before</span>
          <input type="date" name="as_of" defaultValue={state.asOf ?? ""} />
        </label>
      </fieldset>
      <div className={styles.actions}>
        <button type="submit">Compare</button>
        <Link href="/research/splits">Reset</Link>
      </div>
    </form>
  );
}

function ProfileLinks({ p }: { p: SplitsPlayer }) {
  const slug = p.position === "WR" || p.position === "RB" || p.position === "TE" ? p.position.toLowerCase() : null;
  return (
    <span className={styles.links}>
      {p.espn_player_id !== null
        ? <Link href={`/players?id=${p.espn_player_id}`}>Player profile</Link>
        : <span className="faint" title={p.mapping_status === "ambiguous" ? "Two provider ids match this player, so no profile is linked" : "No verified provider id for this player"}>No profile link</span>}
      {slug && <Link href={`/research/${slug}?sel=${encodeURIComponent(p.gsis_id)}`}>Research evidence</Link>}
    </span>
  );
}

function ChooseTeammate({ r, state }: { r: SplitsNeedsTeammate; state: SplitsState }) {
  return (
    <Section title={<>Teammates of {r.player.name}</>} subtitle="Players who appeared for one of his teams in a counted game of the window">
      {r.teammate_options.length === 0 ? (
        <p className={styles.note}>No teammate appeared with him in a final game of this window.</p>
      ) : (
        <ul className={styles.options}>
          {r.teammate_options.map((t) => (
            <li key={t.gsis_id}>
              <Link href={splitsHref(state, { teammate: t.gsis_id })}><Pos value={t.position} /> {t.name}</Link>
              <span className="faint"> {t.teams.join(", ")} · {t.games_together_listed} {plural(t.games_together_listed, "game")} together</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function V({ v, why = "Not available for these games" }: { v: string | null; why?: string }) {
  return v === null ? <span className="missing" title={why}>—</span> : <>{v}</>;
}

function SplitResult({ r, state }: { r: SplitsResult; state: SplitsState }) {
  const { counts } = r;
  const mate = r.teammate.name;
  return (
    <>
      <Section title={<>{r.player.name} <Pos value={r.player.position ?? "WR"} /> with and without {mate} <Pos value={r.teammate.position ?? "WR"} /></>}
        footnote={<>Window: {windowSentence(r)}. Reproduce it with the week list below; cache key <code className={styles.key}>{r.cache_key}</code>.</>}>
        <StatStrip>
          <Stat label={`With ${mate}`} value={counts.with} note={plural(counts.with, "game")} />
          <Stat label={`Without ${mate}`} value={counts.without} note={plural(counts.without, "game")} />
          <Stat label="Excluded" value={counts.excluded} note="status unknown" tone={counts.excluded ? "caution" : undefined} />
          <Stat label="Not a sample" value={counts.not_counted} note="byes, missed games, other weeks" />
        </StatStrip>
        <div className={styles.people}>
          <span>{r.player.name}: <ProfileLinks p={r.player} /></span>
          <span>{mate}: <ProfileLinks p={r.teammate} /></span>
        </div>
      </Section>

      {r.abstention && (
        <Notice tone="caution" title="Too few games to compare">
          {r.abstention.message} The groups are still listed so the counts can be checked.
        </Notice>
      )}
      <Notice tone="info" title="Not a causal estimate">
        <ul className={styles.list}>
          {r.confounding.map((c) => <li key={c}>{c}</li>)}
          {r.cautions.map((c) => <li key={c}>{c}</li>)}
        </ul>
      </Notice>

      <MetricsTable r={r} />
      <EnvironmentTable r={r} />
      <Distribution r={r} />
      <WeekTable r={r} state={state} />
    </>
  );
}

function MetricsTable({ r }: { r: SplitsResult }) {
  const w = r.groups.with.metrics;
  const o = r.groups.without.metrics;
  const withheld = `Withheld: each group needs ${r.rules.min_group_games} games`;
  const val = (m: SplitsResult["groups"]["with"]["metrics"], key: string) => {
    const v = m[key];
    return typeof v === "number" ? v : null;
  };
  return (
    <Section title="Usage and production" subtitle="Per-game averages and shares over each group's games" flush>
      <div className="table-wrap">
        <table aria-label={`Usage and production with and without ${r.teammate.name}`}>
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col" className="num">With ({r.groups.with.games})</th>
              <th scope="col" className="num">Without ({r.groups.without.games})</th>
              <th scope="col" className="num">Without − with</th>
            </tr>
          </thead>
          <tbody>
            {r.metrics.map((m) => {
              const partial = [w.coverage?.[m.key], o.coverage?.[m.key]].some((x) => x !== undefined);
              return (
                <tr key={m.key}>
                  <th scope="row">{m.label}{partial && <span className="missing" title="Some games have no value for this metric; the average uses the games that do"> partial</span>}</th>
                  <td className="num"><V v={formatSplit(val(w, m.key), m.format)} /></td>
                  <td className="num"><V v={formatSplit(val(o, m.key), m.format)} /></td>
                  <td className="num"><V v={r.differences ? formatDifference(r.differences[m.key], m.format) : null} why={r.differences ? undefined : withheld} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function qbText(e: SplitsEnvironment): string | null {
  const qbs = e.primary_qbs ?? [];
  return qbs.length ? qbs.map((q) => `${q.name} (${q.games})`).join(", ") : null;
}

function EnvironmentTable({ r }: { r: SplitsResult }) {
  const a = r.groups.with.environment;
  const b = r.groups.without.environment;
  const rows: [string, string | null, string | null][] = [
    ["Team points/G", formatSplit(a.team_points_pg, "num"), formatSplit(b.team_points_pg, "num")],
    ["Neutral pass rate", formatSplit(a.neutral_pass_rate, "pct"), formatSplit(b.neutral_pass_rate, "pct")],
    ["Offensive plays/G", formatSplit(a.offense_plays_pg, "num"), formatSplit(b.offense_plays_pg, "num")],
    ["Home games", a.games ? String(a.home_games ?? 0) : null, b.games ? String(b.home_games ?? 0) : null],
    ["Starting QB (games)", qbText(a), qbText(b)],
    ["Opponents", (a.opponents ?? []).map((x) => x.team).join(", ") || null, (b.opponents ?? []).map((x) => x.team).join(", ") || null],
  ];
  return (
    <Section title="Environment" subtitle="What else differed between the groups" flush
      footnote="Starting QB is the team's quarterback with the most offensive snaps in the game. A dash means the source has no value for these games.">
      <div className="table-wrap">
        <table aria-label={`Team environment with and without ${r.teammate.name}`}>
          <thead>
            <tr><th scope="col">Context</th><th scope="col">With</th><th scope="col">Without</th></tr>
          </thead>
          <tbody>
            {rows.map(([label, x, y]) => (
              <tr key={label}><th scope="row">{label}</th><td className={styles.wrap}><V v={x} /></td><td className={styles.wrap}><V v={y} /></td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Distribution({ r }: { r: SplitsResult }) {
  const games = r.weeks.filter((w) => (w.outcome === "with" || w.outcome === "without") && w.subject?.fantasy_points_ppr != null);
  const points: ScatterPoint[] = games.map((w, i) => ({
    id: `${w.season}-${w.week}`,
    x: i + 1,
    y: w.subject?.fantasy_points_ppr ?? 0,
    color: w.outcome === "with" ? "var(--chart-1)" : "var(--chart-2)",
    title: `${w.season} week ${w.week} (${OUTCOME_LABEL[w.outcome].toLowerCase()}): ${w.subject?.fantasy_points_ppr?.toFixed(1)} PPR, xFP ${w.subject?.xfp?.toFixed(1) ?? "—"}`,
  }));
  return (
    <Section title="PPR by game" subtitle="Each counted game in date order">
      <p className={styles.legend}>
        <span className={styles.swatchWith} aria-hidden="true" /> With {r.teammate.name}
        <span className={styles.swatchWithout} aria-hidden="true" /> Without
      </p>
      <ScatterChart points={points} xLabel="Game (date order)" yLabel="PPR points" xFormat={(v) => String(Math.round(v))}
        ariaLabel={`PPR points per game with (blue) and without (orange) ${r.teammate.name}`} height={260}
        caption={`${games.length} ${plural(games.length, "counted game")}; excluded and uncounted weeks are not plotted.`} />
    </Section>
  );
}

function teammateEvidence(w: SplitsWeek): string {
  const t = w.teammate;
  if (!t) return "";
  const roster = t.roster.map((x) => `${x.status} ${x.team}`).join(", ");
  const snap = t.offense_pct !== null ? `${Math.round(t.offense_pct * 100)}% snaps` : "";
  return [roster && `Roster: ${roster}`, snap].filter(Boolean).join(" · ");
}

function WeekTable({ r, state }: { r: SplitsResult; state: SplitsState }) {
  const rows = visibleWeeks(r.weeks, state.show);
  const tabs = [
    { href: splitsHref(state, { show: "all" }), label: "All weeks", count: r.weeks.length },
    { href: splitsHref(state, { show: "samples" }), label: "With and without", count: r.counts.with + r.counts.without },
    { href: splitsHref(state, { show: "excluded" }), label: "Excluded", count: r.counts.excluded },
  ];
  const excluded = reasonList(r.counts.excluded_by_reason, r.rules.reasons);
  const notCounted = reasonList(r.counts.not_counted_by_reason, r.rules.reasons);
  return (
    <Section title="Week by week" id="weeks" subtitle="The source of every count above" flush
      footnote={<>
        {excluded.length > 0 && <>Excluded: {excluded.map(([l, n]) => `${l} (${n})`).join("; ")}. </>}
        {notCounted.length > 0 && <>Not a sample: {notCounted.map(([l, n]) => `${l} (${n})`).join("; ")}.</>}
      </>}>
      <Tabs label="Weeks shown" items={tabs} current={splitsHref(state)} className={styles.tabs} />
      {rows.length === 0 ? <p className={styles.note}>No weeks in this view.</p> : (
        <div className="table-wrap">
          <table aria-label={`Weekly source rows for the split`}>
            <thead>
              <tr>
                <th scope="col">Week</th><th scope="col">Game</th><th scope="col">Group</th><th scope="col">Why</th>
                <th scope="col">Teammate evidence</th><th scope="col" className="num">Snap %</th>
                <th scope="col" className="num">Tgt</th><th scope="col" className="num">Routes</th>
                <th scope="col" className="num">xFP</th><th scope="col" className="num">PPR</th><th scope="col">QB</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((w) => (
                <tr key={`${w.season}-${w.week}`} data-outcome={w.outcome}>
                  <th scope="row">{w.season} wk {w.week}</th>
                  <td className={styles.nowrap}>{w.team ? <>{w.team}{w.opponent ? <> {w.home ? "vs" : "at"} {w.opponent}</> : null}</> : <V v={null} why="No team or game on record for this week" />}{w.gameday && <span className="faint"> {w.gameday}</span>}</td>
                  <td><span className={styles[`outcome_${w.outcome}`]}>{OUTCOME_LABEL[w.outcome]}</span></td>
                  <td className={styles.wrap}>{w.outcome === "with" ? "Played" : w.reason_label}</td>
                  <td className={styles.wrap}>{teammateEvidence(w) || <span className="faint">—</span>}</td>
                  <td className="num"><V v={formatSplit(w.subject?.offense_pct, "pct")} /></td>
                  <td className="num"><V v={w.subject?.targets == null ? null : String(w.subject.targets)} /></td>
                  <td className="num"><V v={w.subject?.routes == null ? null : String(w.subject.routes)} why="No route data for this game" /></td>
                  <td className="num"><V v={formatSplit(w.subject?.xfp, "num")} /></td>
                  <td className="num"><V v={formatSplit(w.subject?.fantasy_points_ppr, "num")} /></td>
                  <td className={styles.nowrap}><V v={w.primary_qb_name} why="No quarterback snap record" /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function SplitsMethod() {
  return (
    <Method title="Definitions and method">
      <p><b>Counted game</b>: a final regular-season game in the window that the player played (an offensive snap, target or carry) with nflverse xFP and PPR. With a &quot;games before&quot; date, a week whose first game is on or after it is not read at all.</p>
      <p><b>With</b>: the teammate appeared for the player&apos;s team (a box-score row or offensive snaps) with a snap record, and it was not a partial game: below half his median snap share for that team over the counted games, once that median rests on three games.</p>
      <p><b>Without</b>: confirmed off the field for the team: game-day inactive, a reserve list, the practice squad, suspended or another non-active status; on another team that week; or a snap record with no offensive snap. Games count only for teams the two shared in the window.</p>
      <p><b>Excluded</b>: any unknown status (listed on two teams in one week, active with no record, an appearance without a snap record, a partial game, no roster record, no roster data). Excluded games are never counted as without.</p>
      <p><b>Differences</b> are without minus with, shown only when each group has three games. Shares are aggregate (targets ÷ team targets over the group&apos;s games); the difference of a share is in percentage points. No significance test, probability or forecast is computed.</p>
      <p><b>Data</b>: nflverse player usage, weekly roster statuses and schedules already stored by the NFL sync (full PPR, xFP from ffopportunity). Routes appear only where an authorized external import provides them. Your league&apos;s scoring is not applied.</p>
    </Method>
  );
}
