import Link from "@/components/LeagueLink";
import { Section } from "@/components/ui";
import { cx } from "@/lib/cx";
import { Missing } from "@/lib/format";
import {
  barWidth, leadsText, rankText, recordText, scoutingHref, signedText, sortForPosition, targetHref, tradesWithHref,
  unknownLabel, valueHref, viewHref, visibleGroups, type LaCell, type LaCompare, type LaSide, type LaSideCell, type LaView,
  type LeagueAnalyzer,
} from "@/lib/league-analyzer";
import { plural } from "@/lib/plural";
import styles from "./analyzer.module.css";

/** Horizon, position and K/D/ST treatment as link chips (shareable URLs, no JavaScript needed). */
export function Controls({ d, team, view }: { d: LeagueAnalyzer; team: number | null; view: LaView }) {
  const chip = (href: string, label: string, on: boolean) => (
    <Link key={label} className={cx("chip", on && "active")} aria-current={on ? "page" : undefined} href={href}
      scroll={false} prefetch={false}>{label}</Link>
  );
  const filtered = view.horizon !== "ros" || view.pos !== null || !view.kdst;
  return (
    <div className={styles.controls}>
      <nav className="filters" aria-label="Horizon">
        <span className={styles.chipLabel}>Horizon</span>
        {d.horizon.options.map((o) => chip(viewHref(team, view, { horizon: o.key }), o.label, d.horizon.key === o.key))}
      </nav>
      <nav className="filters" aria-label="Position">
        <span className={styles.chipLabel}>Position</span>
        {chip(viewHref(team, view, { pos: null }), "All", view.pos === null)}
        {d.groups.map((g) => chip(viewHref(team, view, { pos: g.slot }), g.slot, view.pos === g.slot))}
      </nav>
      <nav className="filters" aria-label="K and D/ST">
        <span className={styles.chipLabel}>K and D/ST</span>
        {chip(viewHref(team, view, { kdst: true }), "Streamed", d.kdst_streaming)}
        {chip(viewHref(team, view, { kdst: false }), "Held", !d.kdst_streaming)}
      </nav>
      {filtered && <Link className="chip reset" href={viewHref(team, view, { horizon: "ros", pos: null, kdst: true })} prefetch={false}>Reset</Link>}
    </div>
  );
}

function ScoreBar({ score, label }: { score: number | null; label: string }) {
  const w = barWidth(score);
  if (w === null) return null;
  return (
    <span className={styles.track} role="img" aria-label={`${label}: ${w.toFixed(0)} out of 100`}>
      <span className={styles.fill} style={{ width: `${w}%` }} />
    </span>
  );
}

/** One matrix cell: points above replacement per slot, the 0-100 score as a bar, the rank. Unknown says so in words. */
function Cell({ c, slot }: { c: LaCell; slot: string }) {
  if (c.status !== "ranked") {
    return <span className={styles.unknown} title={c.reason ?? undefined}>{unknownLabel(c)}</span>;
  }
  const above = signedText(c.above_replacement);
  return (
    <span className={styles.cell} title={`${slot}: ${rankText(c)} · score ${c.score?.toFixed(0) ?? "—"} of 100 · ${c.points?.toFixed(1) ?? "—"} projected points per week`}>
      <span className={styles.cellValue}>{above ?? <Missing why="Replacement level unknown: no free agent projection in some week" />}</span>
      <ScoreBar score={c.score} label={`${slot} score`} />
      <span className={styles.cellRank}>{rankText(c)}</span>
    </span>
  );
}

/** The league matrix: one row per team, one column per slot group (or the detail of one group under a filter). */
export function Matrix({ d, team, view }: { d: LeagueAnalyzer; team: number | null; view: LaView }) {
  const groups = visibleGroups(d.groups, view.pos);
  const one = groups.length === 1 && view.pos !== null ? groups[0] : null;
  const rows = sortForPosition(d.teams, one?.slot ?? null);
  const focus = d.focus_team_id;
  return (
    <div className="table-wrap">
      <table className={styles.table}>
        <caption className={styles.srOnly}>Position strength by team: points above replacement per starting slot, rank and league score</caption>
        <thead>
          <tr>
            <th scope="col" className={styles.teamCol}>Team</th>
            <th scope="col" className="num" title="Power rankings composite, rest of season">Power</th>
            {one ? (
              <>
                <th scope="col" className="num" title={d.units.above_replacement}>{one.slot} over repl.</th>
                <th scope="col" className="num" title={d.units.points}>Pts/wk</th>
                <th scope="col" className="num" title="Projected points per starting slot per week">Per slot</th>
                <th scope="col" className="num" title="Per slot, minus the league median">Vs median</th>
              </>
            ) : groups.map((g) => (
              <th key={g.slot} scope="col" className="num" title={`${g.slot}: ${g.count} starting ${plural(g.count, "slot")}; replacement ${g.replacement_per_slot?.toFixed(1) ?? "unknown"} per slot`}>
                <Link href={viewHref(team, view, { pos: g.slot })} prefetch={false}>{g.slot}</Link>
              </th>
            ))}
            <th scope="col">Open</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => {
            const c = one ? t.positions[one.slot] : null;
            return (
              <tr key={t.team_id} className={cx(t.team_id === focus && "mine")}>
                <td className={styles.teamCol}>
                  <Link href={viewHref(t.team_id, view, { vs: null })} className={styles.teamName} title={t.name} prefetch={false}>{t.name}</Link>
                  <span className={styles.sub}>{recordText(t.record)}{t.is_mine ? " · your team" : ""}{t.team_id === focus && !t.is_mine ? " · selected" : ""}</span>
                </td>
                <td className="num">{t.power.rank ? rankLabel(t.power.rank) : <Missing why={t.power.unranked_reason ?? "Unranked"} />}</td>
                {one && c ? (
                  <>
                    <td className="num"><Cell c={c} slot={one.slot} /></td>
                    <td className="num">{c.points !== null ? c.points.toFixed(1) : <Missing why={c.reason ?? "Unknown"} />}</td>
                    <td className="num">{c.per_slot !== null ? c.per_slot.toFixed(1) : <Missing why={c.reason ?? "Unknown"} />}</td>
                    <td className="num">{signedText(c.vs_median) ?? <Missing why={c.reason ?? "Unknown"} />}</td>
                  </>
                ) : groups.map((g) => (
                  <td key={g.slot} className="num"><Cell c={t.positions[g.slot]} slot={g.slot} /></td>
                ))}
                <td>
                  <span className={styles.links}>
                    {focus !== null && t.team_id !== focus && <Link href={viewHref(focus, view, { vs: t.team_id })} prefetch={false}>Compare</Link>}
                    <Link href={scoutingHref(t.team_id)} prefetch={false}>Scouting</Link>
                    {focus !== null && t.team_id !== focus && <Link href={tradesWithHref(focus, t.team_id)} prefetch={false}>Trades</Link>}
                    <Link href={valueHref(t.team_id)} prefetch={false}>Value</Link>
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const rankLabel = (n: number) => <strong className={styles.rank}>{n}</strong>;

/** The compare form: a plain GET form posting to this league's page, so it works without JavaScript. */
export function ComparePicker({ d, view, action }: { d: LeagueAnalyzer; view: LaView; action: string }) {
  const focus = d.focus_team_id;
  if (focus === null) {
    return <p className="faint">Pick a team at the top of the page first; then choose who to compare it with here.</p>;
  }
  return (
    <form className={styles.picker} action={action} method="get" aria-label="Compare with">
      <input type="hidden" name="team" value={focus} />
      {view.horizon !== "ros" && <input type="hidden" name="horizon" value={view.horizon} />}
      {view.pos && <input type="hidden" name="pos" value={view.pos} />}
      {!view.kdst && <input type="hidden" name="kdst" value="hold" />}
      <label className={styles.field}>
        <span className={styles.chipLabel}>Compare {d.teams.find((t) => t.team_id === focus)?.name ?? "this team"} with</span>
        <select name="vs" defaultValue="">
          <option value="" disabled>Choose a team</option>
          {d.teams.filter((t) => t.team_id !== focus).map((t) => (
            <option key={t.team_id} value={t.team_id}>{t.name}</option>
          ))}
        </select>
      </label>
      <button type="submit">Compare</button>
    </form>
  );
}

function SideCell({ c, slot }: { c: LaSideCell; slot: string }) {
  return (
    <div className={styles.side}>
      <Cell c={c} slot={slot} />
      {c.starters.length > 0 && (
        <span className={styles.starters}>
          {c.starters.slice(0, 3).map((s) => `${s.name ?? "Unnamed"} ${s.average_points?.toFixed(1) ?? "—"} (${s.weeks} wk)`).join(" · ")}
        </span>
      )}
      <span className={styles.sub}>
        {c.bench ? (
          c.bench.has_backup
            ? <>Bench best {c.bench.best_bench?.toFixed(1) ?? "—"} · {signedText(c.bench.edge) ?? "—"} over the wire</>
            : <>No bench backup · wire {c.bench.best_free_agent?.toFixed(1) ?? "—"}</>
        ) : null}
        {c.bench ? " · " : ""}{c.depth} active {plural(c.depth, "player")}
      </span>
    </div>
  );
}

function Leader({ row, c }: { row: LaCompare["positions"][number]; c: LaCompare }) {
  if (row.leader === null) return <span className={styles.unknown} title="At least one side is unknown here">Not compared</span>;
  if (row.leader === "even") return <>Even</>;
  const name = row.leader === "a" ? c.a.name : c.b.name;
  return <span title={`${name} by ${Math.abs(row.difference_per_slot ?? 0).toFixed(1)} per slot`}>{name} {signedText(Math.abs(row.difference_per_slot ?? 0))}</span>;
}

function Byes({ s, horizon }: { s: LaSide; horizon: string }) {
  if (!s.byes.available) return <p className="faint">{s.byes.reason}</p>;
  if (s.byes.weeks.length === 0) return <p className={styles.muted}>No bye conflict {horizon}: every starting slot is covered and no week has two or more players on bye.</p>;
  return (
    <ul className={styles.byeList}>
      {s.byes.weeks.map((w) => (
        <li key={w.week}>
          <strong>Week {w.week}</strong> · {w.status_text}
          {w.short_slots.length ? <> · short at {w.short_slots.join(", ")}</> : null}
          {w.unconfirmed_slots.length ? <> · not confirmed at {w.unconfirmed_slots.join(", ")}</> : null}
          {w.byes.length ? <> · on bye: {w.byes.join(", ")}</> : null}
        </li>
      ))}
    </ul>
  );
}

function Needs({ c }: { c: LaCompare }) {
  if (!c.a.needs.available && !c.b.needs.available) return <p className="faint">No stored roster composition or need scores for these teams.</p>;
  const positions = [...new Set([...c.a.needs.positions, ...c.b.needs.positions].map((p) => p.position))];
  const by = (s: LaSide, pos: string) => s.needs.positions.find((p) => p.position === pos);
  const text = (s: LaSide, pos: string) => {
    const n = by(s, pos);
    if (!n) return <Missing why="No stored snapshot" />;
    return <>{n.rostered ?? "—"} <span className={styles.sub}>league {n.league_average?.toFixed(1) ?? "—"}{n.need_score !== null ? ` · need ${n.need_score.toFixed(2)}` : ""}</span></>;
  };
  return (
    <div className="table-wrap">
      <table className={styles.table}>
        <caption className={styles.srOnly}>Rostered players and stored need scores by position</caption>
        <thead><tr><th scope="col">Pos</th><th scope="col" className={cx("num", styles.sideA)}>{c.a.name}</th><th scope="col" className={cx("num", styles.sideB)}>{c.b.name}</th></tr></thead>
        <tbody>
          {positions.map((p) => <tr key={p}><td>{p}</td><td className="num">{text(c.a, p)}</td><td className="num">{text(c.b, p)}</td></tr>)}
        </tbody>
      </table>
    </div>
  );
}

/** Compare-two-teams mode: starters and bench by position, bye conflicts, positional needs and coverage caveats. */
export function CompareView({ d, c, team, view, span }: { d: LeagueAnalyzer; c: LaCompare; team: number | null; view: LaView; span: string }) {
  const horizon = d.horizon.key === "week" ? `in ${span}` : `over ${span}`;
  return (
    <>
      <Section
        title={`${c.a.name} vs ${c.b.name}`}
        subtitle={<>{d.horizon.label} · {span} · {leadsText(c, plural)} (K and D/ST not counted)</>}
        actions={<Link href={viewHref(team, view, { vs: null })} prefetch={false}>Close comparison</Link>}
        flush
        footnote={c.rule}
      >
        {c.caveats.length > 0 && (
          <ul className={cx(styles.caveatList, styles.inset)}>{c.caveats.map((x) => <li key={`${x.code}-${x.text}`}>{x.text}</li>)}</ul>
        )}
        <div className="table-wrap">
          <table className={styles.table}>
            <caption className={styles.srOnly}>Starters and bench by position for both teams</caption>
            <thead>
              <tr>
                <th scope="col">Pos</th>
                <th scope="col" className={styles.sideA}>{c.a.name}</th>
                <th scope="col" className={styles.sideB}>{c.b.name}</th>
                <th scope="col">Edge per slot</th>
              </tr>
            </thead>
            <tbody>
              {c.positions.map((row) => (
                <tr key={row.slot}>
                  <td><strong>{row.slot}</strong>{row.count > 1 ? <span className={styles.sub}>{row.count} slots</span> : null}</td>
                  <td><SideCell c={row.a} slot={row.slot} /></td>
                  <td><SideCell c={row.b} slot={row.slot} /></td>
                  <td><Leader row={row} c={c} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
      <div className={styles.pair}>
        {[c.a, c.b].map((s, i) => (
          <Section key={s.team_id} title={`${s.name}: bye conflicts`} className={i === 0 ? styles.cardA : styles.cardB}
            subtitle={c.shared_conflict_weeks.length ? `Both teams have a conflict in week ${c.shared_conflict_weeks.join(", ")}` : `NFL schedule ${horizon}`}>
            <Byes s={s} horizon={horizon} />
            <p className={styles.links}>
              <Link href={scoutingHref(s.team_id)} prefetch={false}>Manager scouting</Link>
              <Link href={valueHref(s.team_id)} prefetch={false}>Team value</Link>
              <Link href={`/schedule-intelligence?team=${s.team_id}#bye-planner`} prefetch={false}>Bye planner</Link>
            </p>
          </Section>
        ))}
      </div>
      <Section title="Positional needs" subtitle="Rostered players against the league average and the stored need score (current state, not a forecast)" flush
        footnote={<>From <Link href={scoutingHref(c.a.team_id)}>Manager scouting</Link>. Find a trade: {" "}
          <Link href={tradesWithHref(c.a.team_id, c.b.team_id)} prefetch={false}>trades between these teams</Link>
          {(() => {
            const weak = d.teams.find((t) => t.team_id === c.a.team_id)?.weakest[0];
            const href = weak ? targetHref(c.a.team_id, weak) : null;
            return href ? <> · <Link href={href} prefetch={false}>targets at {weak}</Link></> : null;
          })()}.</>}
      >
        <Needs c={c} />
      </Section>
    </>
  );
}

