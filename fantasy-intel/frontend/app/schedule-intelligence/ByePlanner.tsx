import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { Badge, Notice, ProvenanceBadge, Section, VisuallyHidden } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import {
  STATUS_GLYPH, STATUS_WORD, slotStatus, summaryText, weekHeadline, weekLinks, weekReasons,
  type ByePlanner as Planner, type ByeWeekStatus, type PlannerSlot, type PlannerWeek,
} from "@/lib/bye-planner";
import { cx } from "@/lib/cx";
import { plural } from "@/lib/plural";
import { signalsOf } from "@/lib/signal-provenance";
import styles from "./bye-planner.module.css";

// Roster-wide bye and lineup-coverage planner (issue #177): a week-by-week matrix on wide screens, a grid of week tiles
// on phones, and one detail per conflict week with links to Start/Sit and Waivers in the same league, team and week.
// Every status is the backend's; nothing here decides whether a week is covered.

const TONE: Record<ByeWeekStatus, string> = { covered: "good", short: "bad", unconfirmed: "warn" };

function StatusBadge({ status }: { status: ByeWeekStatus }) {
  return <Badge value={`${STATUS_GLYPH[status]} ${STATUS_WORD[status]}`} tone={TONE[status]} />;
}

function slotSentence(s: PlannerSlot, week: number): string {
  const parts = [`${s.covered} of ${s.required} covered`];
  if (s.unknown) parts.push(`${s.unknown} depends on unknown availability`);
  if (s.uncovered) parts.push(`${s.uncovered} cannot be filled`);
  const pool = [`${s.available} eligible ${plural(s.available, "player")} can play`];
  if (s.bye) pool.push(`${s.bye} on bye`);
  if (s.out) pool.push(`${s.out} out`);
  if (s.uncertain) pool.push(`${s.uncertain} unknown`);
  return `${s.slot}, week ${week}: ${parts.join(", ")}; ${pool.join(", ")}.`;
}

export function ByePlannerSection({ res, league }: { res: ApiResult<Planner>; league: string | null }) {
  if (!res.ok) {
    return (
      <Section id="bye-planner" title="Lineup coverage by week">
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </Section>
    );
  }
  const d = res.data;
  const team = d.team.team_id;
  const byWeek = new Map(d.weeks.map((w) => [w.week, w]));
  const conflicts = d.weeks.filter((w) => w.conflict);
  const slotLabels = d.slots.starting.map((s) => s.slot);
  const first = d.summary.first_conflict_week !== null ? byWeek.get(d.summary.first_conflict_week) : undefined;
  return (
    <Section
      id="bye-planner"
      title="Lineup coverage by week"
      subtitle={<>{d.team.name} · every starting slot, weeks {d.window.weeks[0] ?? "—"}–{d.window.weeks[d.window.weeks.length - 1] ?? "—"} (through the fantasy playoffs). The position and weeks filters above do not apply.</>}
      actions={<ProvenanceBadge signals={signalsOf(d.provenance)} />}
      footnote={<>Covered: a rostered player who can legally start the slot has a game and no open designation. Short: no rostered player can fill it, whatever the unknowns turn out to be. Not confirmed: the slot depends on a player whose week is unknown. Can play counts eligible rostered players with a game (a FLEX counts every eligible player, including those a fixed slot uses). IR and taxi players never count.</>}
    >
      {!d.window.remaining_weeks && <Notice tone="caution">No remaining fantasy weeks to plan: every week through the final scoring period has kicked off.</Notice>}
      {d.window.uncertain && d.window.warning && <Notice tone="caution">{d.window.warning}</Notice>}
      {!d.schedule.available && <Notice tone="caution">No NFL schedule is loaded, so no bye can be confirmed and no week can be called covered. Run the NFL sync.</Notice>}
      {d.schedule.available && d.schedule.weeks_missing.length > 0 && (
        <Notice tone="caution">No NFL schedule is loaded for {plural(d.schedule.weeks_missing.length, "week")} {d.schedule.weeks_missing.join(", ")}: those weeks are not confirmed.</Notice>
      )}
      {d.schedule.incomplete_teams.length > 0 && (
        <Notice tone="caution">The NFL schedule is incomplete for {d.schedule.incomplete_teams.join(", ")}, so a missing game there is not treated as a bye.</Notice>
      )}
      {d.slots.unresolved.length > 0 && (
        <Notice tone="caution">Not evaluated: {d.slots.unresolved.map((u) => `${u.count} ${u.slot}`).join(", ")}. No lineup engine fills these slots, so no week is called covered.</Notice>
      )}

      {d.weeks.length > 0 && (
        <>
          <p className={styles.lead} data-testid="bye-planner-summary">
            {first ? <b>{weekHeadline(first)}</b> : <b>No conflicts: every planned week is covered with no bye cluster.</b>}
            {" "}<span className="faint">{summaryText(d.summary)}{d.summary.bye_cluster_weeks.length > 0 && ` · bye clusters in ${plural(d.summary.bye_cluster_weeks.length, "week")} ${d.summary.bye_cluster_weeks.join(", ")}`}.</span>
          </p>
          <Matrix d={d} slotLabels={slotLabels} league={league} team={team} />
          <WeekGrid d={d} league={league} team={team} />
          {conflicts.length > 0 && <Conflicts d={d} weeks={conflicts} league={league} team={team} />}
        </>
      )}
    </Section>
  );
}

function Matrix({ d, slotLabels, league, team }: { d: Planner; slotLabels: string[]; league: string | null; team: number }) {
  const counts = new Map(d.slots.starting.map((s) => [s.slot, s.count]));
  return (
    <div className={cx("table-wrap", styles.matrixWrap)} data-testid="bye-matrix">
      <table className={styles.matrix}>
        <caption><VisuallyHidden>Starting-slot coverage for {d.team.name}, by week. Week links open the week&apos;s conflict details.</VisuallyHidden></caption>
        <thead>
          <tr>
            <th scope="col" className={styles.corner}>Slot</th>
            {d.weeks.map((w) => (
              <th key={w.week} scope="col" className={cx(styles.wk, w.phase === "playoffs" && styles.playoff)}>
                {w.conflict
                  ? <a href={`#bye-week-${w.week}`}>Wk {w.week}<VisuallyHidden>: details</VisuallyHidden></a>
                  : <>Wk {w.week}</>}
                {w.phase === "playoffs" && <small>playoffs</small>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr className={styles.statusRow}>
            <th scope="row">Week</th>
            {d.weeks.map((w) => <td key={w.week}><span className={cx(styles.status, styles[w.status])}>{STATUS_GLYPH[w.status]}<VisuallyHidden> {STATUS_WORD[w.status]}</VisuallyHidden></span></td>)}
          </tr>
          {slotLabels.map((label) => (
            <tr key={label}>
              <th scope="row">{label}{(counts.get(label) ?? 1) > 1 && <span className="faint"> ×{counts.get(label)}</span>}</th>
              {d.weeks.map((w) => {
                const s = w.slots.find((x) => x.slot === label);
                if (!s) return <td key={w.week}><span className="missing" title="Slot not planned">—</span></td>;
                const st = slotStatus(s);
                return (
                  <td key={w.week} className={cx(styles.cell, styles[st])}>
                    <span aria-hidden="true"><b>{s.covered}/{s.required} {STATUS_GLYPH[st]}</b><small>{s.available} can play</small></span>
                    <VisuallyHidden>{slotSentence(s, w.week)}</VisuallyHidden>
                  </td>
                );
              })}
            </tr>
          ))}
          <tr className={styles.factRow}>
            <th scope="row">On bye</th>
            {d.weeks.map((w) => <td key={w.week}>{w.byes.length ? <b className={w.bye_cluster ? styles.cluster : undefined}>{w.byes.length}{w.bye_cluster && <small> cluster</small>}</b> : <span className="faint">0</span>}</td>)}
          </tr>
          <tr className={styles.factRow}>
            <th scope="row">Out</th>
            {d.weeks.map((w) => <td key={w.week}>{w.out.length ? <b>{w.out.length}</b> : <span className="faint">0</span>}</td>)}
          </tr>
          <tr className={styles.factRow}>
            <th scope="row">Unknown</th>
            {d.weeks.map((w) => <td key={w.week}>{w.uncertain.length ? <b className={styles.unknownCount}>{w.uncertain.length}</b> : <span className="faint">0</span>}</td>)}
          </tr>
          <tr className={styles.actRow}>
            <th scope="row">Act</th>
            {d.weeks.map((w) => {
              if (!w.conflict) return <td key={w.week}><span className="faint" title="No conflict this week">—</span></td>;
              const links = weekLinks(w, team, league);
              return (
                <td key={w.week}>
                  <Link href={links.startSit}>Start/Sit<VisuallyHidden> for week {w.week}</VisuallyHidden></Link>
                  <Link href={links.waivers}>Waivers<VisuallyHidden> for week {w.week}</VisuallyHidden></Link>
                </td>
              );
            })}
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function WeekGrid({ d, league, team }: { d: Planner; league: string | null; team: number }) {
  return (
    <ol className={styles.grid} aria-label={`Weeks for ${d.team.name}`} data-testid="bye-grid">
      {d.weeks.map((w) => {
        const links = weekLinks(w, team, league);
        return (
          <li key={w.week} className={cx(styles.tile, styles[`tile_${w.status}`])}>
            <p className={styles.tileHead}>
              <b>Wk {w.week}</b>{w.phase === "playoffs" && <small> playoffs</small>}
              <span className={cx(styles.status, styles[w.status])}>{STATUS_GLYPH[w.status]} {STATUS_WORD[w.status]}</span>
            </p>
            <p className={styles.tileFacts}>
              {w.uncovered > 0 && <span>{w.uncovered} short: {w.short_slots.join(", ")}</span>}
              {w.unknown > 0 && <span>{w.unknown} unknown: {w.unconfirmed_slots.join(", ")}</span>}
              <span>{w.byes.length} on bye{w.bye_cluster ? " (cluster)" : ""}</span>
            </p>
            {w.conflict && (
              <p className={styles.tileLinks}>
                <a href={`#bye-week-${w.week}`}>Details<VisuallyHidden> for week {w.week}</VisuallyHidden></a>
                <Link href={links.startSit}>Start/Sit<VisuallyHidden> for week {w.week}</VisuallyHidden></Link>
                <Link href={links.waivers}>Waivers<VisuallyHidden> for week {w.week}</VisuallyHidden></Link>
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Conflicts({ d, weeks, league, team }: { d: Planner; weeks: PlannerWeek[]; league: string | null; team: number }) {
  return (
    <div className={styles.conflicts}>
      <h3 className={styles.conflictsTitle}>Conflict weeks</h3>
      <ul className={styles.conflictList}>
        {weeks.map((w) => {
          const links = weekLinks(w, team, league);
          return (
            <li key={w.week} id={`bye-week-${w.week}`} tabIndex={-1} className={styles.conflict} data-testid={`bye-week-${w.week}`}>
              <p className={styles.conflictHead}><StatusBadge status={w.status} /> <b>{weekHeadline(w)}</b></p>
              <dl className={styles.reasons}>
                {weekReasons(w, d.players, d.rules.unknown_reasons).map((r) => (
                  <div key={r.kind} className={styles[`reason_${r.kind}`]}><dt>{r.label}</dt><dd>{r.text}</dd></div>
                ))}
              </dl>
              <p className={styles.conflictLinks}>
                <Link href={links.startSit}>Open Start/Sit for week {w.week}</Link>
                <Link href={links.waivers}>Find waiver options for week {w.week}</Link>
              </p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
