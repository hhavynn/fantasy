import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { ChooseTeam, needsTeam } from "@/components/TeamPerspective";
import { PlayerLink } from "@/components/PlayerLink";
import { PlayerPhoto } from "@/components/PlayerPhoto";
import {
  Badge, Glossary, Method, Notice, PageHeader, Pos, Section, SortableTh, Stat, StatStrip,
} from "@/components/ui";
import { api, type ApiResult } from "@/lib/api";
import type { ByePlanner } from "@/lib/bye-planner";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import { cx } from "@/lib/cx";
import { when } from "@/lib/format";
import {
  LABEL_GLYPH, buildColumns, buildTimeline, countNextByLabel, nextWeekOf, phaseGroups, primaryMetric, type TimelineCell,
} from "@/lib/schedule-timeline";
import { parseSort, sortRows, type SearchParams, type SortState } from "@/lib/sort";
import type { ScheduleIntelligence, SiLabel, SiMetric, SiPlayer, SiWeek } from "@/lib/types";
import styles from "./schedule.module.css";
import { TeamLink } from "@/components/TeamLink";
import { ByePlannerSection } from "./ByePlanner";
import { StreamingPlanSection } from "./StreamingPlan";
import { streamingPath, type KdstStreaming } from "@/lib/kdst-streaming";

export const metadata = { title: "Schedule" };

// Schedule intelligence: each remaining week's opponent and what that defense has allowed in games
// completed before the week. Descriptive only: no projection, no points adjustment, no composite score.

const TONE: Record<SiLabel, string> = {
  permissive: "good", middle: "", tougher: "bad", insufficient: "warn", unavailable: "warn", bye: "info",
};
const SHORT: Record<SiLabel, string> = {
  permissive: "permissive", middle: "middle", tougher: "tougher", insufficient: "insufficient", unavailable: "n/a", bye: "bye",
};
const LABELS: SiLabel[] = ["permissive", "middle", "tougher", "insufficient", "unavailable", "bye"];

const NEXT_SORTS = ["roster", "player", "week", "opp", "rank", "value", "plays", "games"] as const;
const NEXT_DEFAULT: SortState = { key: "roster", dir: "asc" };

const one = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

function fmt(m: SiMetric): string | null {
  if (m.value === null) return null;
  return m.metric.endsWith("_per_play") ? `${m.value >= 0 ? "+" : ""}${m.value.toFixed(3)}` : `${(m.value * 100).toFixed(1)}%`;
}

function numerator(m: SiMetric): string {
  if (m.numerator === null) return "—";
  return m.metric.endsWith("_per_play") ? m.numerator.toFixed(2) : String(m.numerator);
}

function Counts({ c }: { c: Record<SiLabel, number> }) {
  const parts = LABELS.filter((k) => c[k] > 0).map((k) => `${c[k]} ${SHORT[k]}`);
  return <span className="faint">{parts.length ? parts.join(" · ") : "—"}</span>;
}

// Warnings that mean no defensive evidence can be shown at all.
const UNKNOWN_INPUT = new Set(["schedule_refresh_unknown", "defense_refresh_unknown"]);

export default async function ScheduleIntelligencePage({ searchParams }: PageProps<"/schedule-intelligence">) {
  const sp = await searchParams;
  const q = new URLSearchParams();
  const team = one(sp.team), position = one(sp.position) ?? "all", phase = one(sp.phase) ?? "all";
  const selected = one(sp.player);
  if (team) q.set("team_id", team);
  q.set("position", position);
  q.set("phase", phase);
  // The bye planner covers every starting slot of the same team; it is fetched beside the opponent view.
  const planned = new URLSearchParams();
  if (team) planned.set("team_id", team);
  const plannerPath = `/api/schedule-intelligence/bye-planner${planned.toString() ? `?${planned}` : ""}`;
  const [res, planner, streaming, league] = await Promise.all([
    api<ScheduleIntelligence>(`/api/schedule-intelligence?${q.toString()}`),
    api<ByePlanner>(plannerPath),
    api<KdstStreaming>(streamingPath(team, sp)),
    currentLeague(),
  ]);
  let detail: SiPlayer | undefined;
  if (res.ok && selected) {
    detail = res.data.players.find((p) => String(p.espn_player_id) === selected);
    if (!detail) {
      const single = await api<ScheduleIntelligence>(`/api/schedule-intelligence?player_id=${selected}&phase=${phase}`);
      if (single.ok) detail = single.data.players[0];
    }
  }
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Schedule intelligence" />
        {needsTeam(res) ? <ChooseTeam path="/schedule-intelligence" sp={sp} what="its players' upcoming schedule" />
          : <BackendProblem error={res.error} kind={res.kind} status={res.status} />}
      </div>
    );
  }
  return <Body d={res.data} detail={detail} position={position} phase={phase} sp={sp} planner={planner} streaming={streaming} league={league?.id ?? null} />;
}

function Body({ d, detail, position, phase, sp, planner, streaming, league }: {
  d: ScheduleIntelligence; detail?: SiPlayer; position: string; phase: string; sp: SearchParams;
  planner: ApiResult<ByePlanner>; streaming: ApiResult<KdstStreaming>; league: string | null;
}) {
  const w = d.window;
  const href = (pid?: number) => {
    const p = new URLSearchParams();
    if (d.query.team_id !== null) p.set("team", String(d.query.team_id));
    p.set("position", position);
    p.set("phase", phase);
    if (pid !== undefined) p.set("player", String(pid));
    return `/schedule-intelligence?${p.toString()}#player-detail`;
  };
  const range = `weeks ${w.weeks[0] ?? "—"}–${w.weeks[w.weeks.length - 1] ?? "—"}`;
  const regular = w.regular_season_through !== null
    ? ` · regular season through week ${w.regular_season_through} · fantasy playoffs ${w.playoff_weeks.length ? `weeks ${w.playoff_weeks.join(", ")}` : "unknown"} (from league settings)`
    : "";
  const next = countNextByLabel(d.players);

  return (
    <div className="stack">
      <PageHeader
        eyebrow="Players · Research"
        title="Schedule intelligence"
        actions={<TeamLink href="/schedule-intelligence/sos" prefetch={false}>Strength-of-schedule grid</TeamLink>}
        meta={<>
          {d.team?.name ?? "Roster"} · {range}{regular} · first actionable week {w.first_actionable_week ?? "none"}.
          {" "}Upcoming opponents and what each defense has <b>observed</b> allowing; descriptive, not a projection.
        </>}
      />

      <Alerts d={d} />

      <Section>
        <form action={withLeague("/schedule-intelligence", league)} className={styles.controls}>
          <label>Team <select name="team" defaultValue={d.query.team_id ?? ""}>
            {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (default)" : ""}</option>)}
          </select></label>
          <label>Position <select name="position" defaultValue={position}>
            {["all", "QB", "RB", "WR", "TE"].map((p) => <option key={p} value={p}>{p === "all" ? "QB/RB/WR/TE" : p}</option>)}
          </select></label>
          <label>Weeks <select name="phase" defaultValue={phase}>
            <option value="all">All remaining</option><option value="regular">Regular season</option><option value="playoffs">Fantasy playoffs</option>
          </select></label>
          <label>Player <select name="player" defaultValue={detail ? String(detail.espn_player_id) : ""}>
            <option value="">—</option>
            {d.players.map((p) => <option key={p.espn_player_id} value={p.espn_player_id}>{p.name} ({p.position})</option>)}
          </select></label>
          <button type="submit">Show</button>
          <TeamLink href="/schedule-intelligence" className={styles.reset}>Reset</TeamLink>
        </form>
      </Section>

      <ByePlannerSection res={planner} league={league} />
      <StreamingPlanSection res={streaming} league={league}
        carry={{ team: d.query.team_id !== null ? String(d.query.team_id) : undefined, position, phase }} />

      {d.players.length === 0 ? (
        <Notice tone="caution">No {position === "all" ? "QB/RB/WR/TE" : position} players on this roster.</Notice>
      ) : (
        <>
          <StatStrip>
            <Stat label="Players" value={d.players.length} note={`${d.team?.name ?? "roster"}, ${position === "all" ? "QB/RB/WR/TE" : position}`} />
            <Stat label="Next week: more permissive" value={next.permissive} note="opponent defense observed allowing more" tone={next.permissive > 0 ? "positive" : undefined} />
            <Stat label="Next week: tougher" value={next.tougher} note="opponent defense observed allowing less" tone={next.tougher > 0 ? "negative" : undefined} />
            <Stat label="Next week: no evidence" value={next.insufficient + next.unavailable} note={`${next.bye} on a bye · ${next.middle} in the middle`} />
          </StatStrip>

          <Timeline d={d} href={href} phase={phase} />
          <NextMatchups d={d} href={href} sp={sp} />
        </>
      )}

      {detail && <Detail p={detail} />}

      <Glossary terms={["EPA", "Target share", "Bye", "Bye cluster", "Slot coverage", "Streaming baseline", "—"]} />
      <Method title="What this is (and is not)">
        <ul className="reasons">{d.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        <p>{d.label_rule}</p>
        <details>
          <summary>How games are presumed complete (and what that cannot rule out)</summary>
          <p>{d.schedule.completion_rule}</p>
          <ul className="reasons">{d.schedule.completion_limits.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </details>
        <details>
          <summary>Metric definitions</summary>
          <ul className="reasons">
            {Object.entries(d.metrics).map(([k, m]) => (
              <li key={k}><b>{m.label}</b> ({m.positions.join(", ")}): {m.definition} <span className="faint">[{m.numerator} / {m.denominator}]</span></li>
            ))}
          </ul>
        </details>
      </Method>
    </div>
  );
}

function Alerts({ d }: { d: ScheduleIntelligence }) {
  const w = d.window;
  const f = d.freshness;
  const latest = f.latest_nfl_sync;
  const skipped = latest ? [!latest.refreshed_nfl_schedules && "schedule", !latest.refreshed_nfl_defense_week && "defensive aggregates"].filter(Boolean) : [];
  return (
    <>
      {!w.remaining_weeks && <Notice tone="caution">No remaining fantasy scoring periods: every week through the final period has kicked off. Past weeks are shown for information only.</Notice>}
      {w.uncertain && w.warning && <Notice tone="caution">{w.warning}</Notice>}
      {!d.schedule.available && <Notice tone="caution">No NFL schedule is loaded, so no opponents can be assigned. Run the NFL sync.</Notice>}
      {d.schedule.available && d.schedule.warnings.map((x) => (
        UNKNOWN_INPUT.has(x.code) ? <Notice key={x.code} tone="caution">{x.message}</Notice> : <Notice key={x.code} tone="info">{x.message}</Notice>))}
      <p className={styles.inline}>
        NFL schedule refreshed {f.nfl_schedules ? when(f.nfl_schedules.refreshed_at) : <b>never (unknown)</b>}
        {" "}· defensive aggregates refreshed {f.nfl_defense_week ? when(f.nfl_defense_week.refreshed_at) : <b>never (unknown)</b>}
        {f.defense_weeks_loaded.length > 0 && <> (weeks {Math.min(...f.defense_weeks_loaded)}–{Math.max(...f.defense_weeks_loaded)} loaded)</>}
        {latest && skipped.length > 0 && <> · latest {f.season} NFL sync {when(latest.refreshed_at)} did not refresh the {skipped.join(" or ")}</>}
        {d.schedule.completion_reference_time && <> · games presumed complete if kicked off at least 8 h before {when(d.schedule.completion_reference_time)}</>}
      </p>
    </>
  );
}

function CellView({ c }: { c: TimelineCell }) {
  const glyph = LABEL_GLYPH[c.label];
  const cls = cx(styles.cell, c.kind === "bye" ? styles.bye : c.kind === "none" ? styles.none : styles[c.label]);
  if (c.kind === "bye") return <span className={cls} title={c.description}><b>BYE</b></span>;
  if (c.kind === "none") return <span className={cls} title={c.description}><b>—</b></span>;
  if (c.kind === "unknown") return <span className={cls} title={c.description}><b>?</b><span>no opponent</span></span>;
  return (
    <span className={cls} title={c.description}>
      <b>{c.where} {c.opponent}</b>
      <span>{c.rank ? `${glyph} #${c.rank}` : `${glyph} ${SHORT[c.label]}`}</span>
    </span>
  );
}

function Timeline({ d, href, phase }: { d: ScheduleIntelligence; href: (pid?: number) => string; phase: string }) {
  const w = d.window;
  const cols = buildColumns(w.weeks, w.playoff_weeks, w.matchup_periods, w.regular_season_through).filter((c) => phase === "all" || (phase === "regular" ? c.phase === "regular" : c.phase === "playoffs"));
  const weeks = cols.map((c) => c.week);
  const groups = phaseGroups(cols);
  const firstActionable = w.first_actionable_week;
  return (
    <Section
      title={`${d.team?.name ?? "Roster"}: opponents by week`}
      actions={
        <ul className={styles.legend} aria-label="Legend">
          {(["permissive", "middle", "tougher", "insufficient"] as SiLabel[]).map((k) => (
            <li key={k}><Badge value={`${LABEL_GLYPH[k]} ${d.labels[k]}`} tone={TONE[k]} /></li>
          ))}
        </ul>
      }
      flush
      footnote={<>Each cell is the opponent (vs = home, @ = away) and its rank on the position&apos;s primary metric (#1 = most permissive defense observed so far). Hover a cell for the evidence. Weeks before week {firstActionable ?? "—"} are not actionable.</>}
    >
      <div className="table-wrap">
        <table className={styles.timeline}>
          <thead>
            <tr className={styles.phaseRow}>
              <th className={styles.corner} rowSpan={2} scope="col">Player</th>
              {groups.map((g, i) => (
                <th key={`${g.phase}-${i}`} colSpan={g.count} scope="colgroup" className={g.phase === "playoffs" ? styles.playoffHead : undefined} title={g.phase === null ? "The league settings do not establish whether these weeks are regular season or playoffs" : undefined}>
                  {g.label}
                </th>
              ))}
            </tr>
            <tr>
              {cols.map((c) => (
                <th key={c.week} scope="col" className={cx(styles.wk, c.startsPlayoffs && styles.playoffEdge)} title={c.matchupPeriod ? `Matchup period ${c.matchupPeriod}` : undefined}>
                  Wk {c.week}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.players.map((p) => {
              const cells = buildTimeline(weeks, p);
              return (
                <tr key={p.espn_player_id}>
                  <th scope="row" className={styles.who}>
                    <Link href={href(p.espn_player_id)}><PlayerPhoto id={p.espn_player_id} name={p.name} inline />{p.name ?? `Player #${p.espn_player_id}`}</Link>{" "}
                    <Pos value={p.position} /> <span className={styles.team}>{p.pro_team ?? "no team"}</span>
                    {p.on_ir && <> <Badge value="IR" tone="bad" /></>}
                  </th>
                  {cells.map((c, i) => (
                    <td key={c.week} className={cx(cols[i].startsPlayoffs && styles.playoffEdge, !c.actionable && c.kind !== "none" && styles.past)}>
                      <CellView c={c} />
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

type NextRow = { p: SiPlayer; order: number; next: SiWeek | undefined; pm: SiMetric | undefined };

function NextMatchups({ d, href, sp }: { d: ScheduleIntelligence; href: (pid?: number) => string; sp: SearchParams }) {
  const sort = parseSort(sp, NEXT_SORTS, NEXT_DEFAULT);
  const base: NextRow[] = d.players.map((p, order) => {
    const next = nextWeekOf(p);
    return { p, order, next, pm: next ? primaryMetric(next) : undefined };
  });
  const rows = sortRows(base, (r) => {
    switch (sort.key) {
      case "player": return r.p.name;
      case "week": return r.next?.week;
      case "opp": return r.next?.opponent;
      case "rank": return r.pm?.rank;
      case "value": return r.pm?.value;
      case "plays": return r.pm?.denominator;
      case "games": return r.next?.evidence?.games;
      default: return r.order;
    }
  }, sort.dir);
  return (
    <Section
      title="Next actionable matchup: defense evidence"
      flush
      footnote="Rank 1 is the most permissive defense on the player's primary metric among defenses with at least 3 presumed-complete games before that week. Sample is the plays and games behind the number; an opponent with too little evidence says why instead of showing a rank."
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <SortableTh sortKey="player" label="Player" current={sort} searchParams={sp} />
              <SortableTh sortKey="week" label="Wk" align="right" defaultDir="asc" current={sort} searchParams={sp} />
              <SortableTh sortKey="opp" label="Opponent" current={sort} searchParams={sp} />
              <th>Label</th>
              <SortableTh sortKey="rank" label="Rank" align="right" defaultDir="asc" current={sort} searchParams={sp} title="Rank of the opponent defense on the primary metric (1 = most permissive)" />
              <SortableTh sortKey="value" label="Allowed" align="right" current={sort} searchParams={sp} title="Primary metric value: EPA per play, or a rate" />
              <SortableTh sortKey="plays" label="Plays" align="right" current={sort} searchParams={sp} title="Plays behind the metric (denominator)" />
              <SortableTh sortKey="games" label="Games" align="right" current={sort} searchParams={sp} title="Presumed-complete games sampled" />
              <th>Evidence</th>
              <th>Gaps</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ p, next, pm }) => {
              const gaps = p.summary.all.counts.insufficient + p.summary.all.counts.unavailable;
              const reason = !next ? "No remaining weeks" : next.status === "bye" ? "Bye week" : next.evidence ? (pm && fmt(pm) === null ? (pm.missing_reason ?? "Metric unavailable") : null) : (next.reason ?? "Unavailable");
              return (
                <tr key={p.espn_player_id}>
                  <td>
                    <PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /> <span className={styles.team}>{p.pro_team ?? "no team"}</span>
                    {p.on_ir && <> <Badge value="IR" tone="bad" /></>}
                    {p.notes.length > 0 && <span className="faint" title={p.notes.join("\n")}> ⓘ</span>}
                  </td>
                  <td className="num">{next?.week ?? <span className="missing" title="No remaining week">—</span>}</td>
                  <td>{next?.status === "scheduled" ? `${next.home ? "vs" : "@"} ${next.opponent}` : next?.status === "bye" ? "BYE" : <span className="missing" title={next?.reason ?? "No opponent"}>—</span>}</td>
                  <td>{next && next.status !== "bye" ? <Badge value={`${LABEL_GLYPH[next.label]} ${next.label_text}`.trim()} tone={TONE[next.label]} /> : <span className="faint">—</span>}</td>
                  <td className="num">{pm?.rank ? <>{pm.rank}<span className="faint"> / {pm.ranked_defenses}</span></> : <span className="missing" title={pm?.missing_reason ?? reason ?? "No rank"}>—</span>}</td>
                  <td className="num">{pm && fmt(pm) !== null ? <b>{fmt(pm)}</b> : <span className="missing" title={pm?.missing_reason ?? reason ?? "No value"}>—</span>}</td>
                  <td className="num">{pm?.denominator ?? <span className="missing" title="No sample">—</span>}</td>
                  <td className="num">{next?.evidence ? next.evidence.games : <span className="missing" title="No sample">—</span>}</td>
                  <td className={styles.evidence}>
                    {reason ? <span className="faint">{reason}</span> : <>{pm?.label}</>}
                    {next?.evidence && next.evidence.excluded_weeks.length > 0 && (
                      <span className={styles.note}>Excluded (not presumed complete): wk {next.evidence.excluded_weeks.join(", ")}</span>
                    )}
                  </td>
                  <td>
                    {gaps > 0 ? <span className={styles.gap} title="Remaining weeks with insufficient or unavailable evidence">{gaps} wk without evidence</span> : <span className="faint">All weeks</span>}
                    {" "}<Link href={href(p.espn_player_id)}>Details</Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Detail({ p }: { p: SiPlayer }) {
  return (
    <Section id="player-detail" title={`${p.name} (${p.position}, ${p.pro_team ?? "no team"}): week by week`} flush>
      <div className={styles.detailTop}>
        {p.notes.map((n, i) => <Notice key={i} tone="caution">{n}</Notice>)}
        <div className={styles.counts}>
          {(["next", "regular", "playoffs", "all"] as const).map((k) => (
            <span key={k} className={styles.countsItem}>
              <b>{k === "all" ? "All remaining" : k === "next" ? "Next" : k === "regular" ? "Regular season" : "Playoffs"}:</b> <Counts c={p.summary[k].counts} />
            </span>
          ))}
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Wk</th><th>Phase</th><th>Opponent</th><th>Observed defense</th>
              <th>Metrics (value · numerator / denominator · rank)</th><th>Sample</th>
            </tr>
          </thead>
          <tbody>
            {p.weeks.map((w) => (
              <tr key={w.week} className={w.actionable ? undefined : styles.dim}>
                <td>{w.week}{!w.actionable && <span className={styles.note}>{w.window_note}</span>}</td>
                <td className="faint">{w.phase ?? "phase unknown"}{w.matchup_period ? ` · m${w.matchup_period}` : ""}</td>
                <td>{w.status === "scheduled" ? `${w.home ? "vs" : "@"} ${w.opponent}` : w.status === "bye" ? "BYE" : <span className="faint">{w.reason}</span>}</td>
                <td className={styles.evidence}>
                  <Badge value={`${LABEL_GLYPH[w.label]} ${w.label_text}`.trim()} tone={TONE[w.label]} />{" "}
                  <span className="faint">{w.evidence?.sentence ?? w.reason}</span>
                </td>
                <td className="wrap">{w.evidence ? (
                  <ul className={styles.metrics}>
                    {w.evidence.metrics.map((m) => (
                      <li key={m.metric}>
                        {m.label}: <b>{fmt(m) ?? "—"}</b>
                        {m.value !== null && <span className="faint"> · {numerator(m)} / {m.denominator}{m.rank ? ` · rank ${m.rank}/${m.ranked_defenses}` : ""}</span>}
                        {m.position_coverage !== undefined && m.position_coverage !== null && <span className="faint"> · positions identified for {(m.position_coverage * 100).toFixed(0)}% of targets</span>}
                        {m.missing_reason && <span className="faint"> · {m.missing_reason}</span>}
                      </li>
                    ))}
                  </ul>
                ) : <span className="faint">—</span>}</td>
                <td className="faint">{w.evidence ? <>{w.evidence.games} games{w.evidence.through_week !== null ? `, through wk ${w.evidence.through_week}` : ""}
                  {w.evidence.excluded_weeks.length > 0 && (
                    <span className={styles.note} title={Object.entries(w.evidence.excluded_reasons).map(([wk, why]) => `wk ${wk}: ${why.reason}`).join("\n")}>
                      Excluded (not presumed complete): wk {w.evidence.excluded_weeks.join(", ")}
                    </span>)}</> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
