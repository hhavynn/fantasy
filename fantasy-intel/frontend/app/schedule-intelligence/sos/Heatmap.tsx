import Link from "@/components/LeagueLink";
import { PlayerLink } from "@/components/PlayerLink";
import { TeamLink } from "@/components/TeamLink";
import { Badge, Pos, Section, SortableTh, VisuallyHidden } from "@/components/ui";
import { cx } from "@/lib/cx";
import {
  GLYPH, describeCell, fmtDelta, fmtValue, isGraded, rowLinks, rowName, sampleText, sortValue, sosHref, where,
  type SosCell, type SosHeatmap, type SosLabel, type SosRow, type SosState,
} from "@/lib/sos-heatmap";
import { sortRows, type SearchParams, type SortState } from "@/lib/sort";
import styles from "./sos.module.css";

const LEGEND: SosLabel[] = ["permissive", "middle", "tougher", "insufficient"];

function Cell({ c, d, edge }: { c: SosCell; d: SosHeatmap; edge: boolean }) {
  const text = describeCell(c, d);
  const cls = cx(styles.cell, edge && styles.playoffEdge,
    c.status === "bye" ? styles.bye : c.status === "scheduled" ? styles[c.label] : styles.none);
  let body;
  if (c.status === "bye") {
    body = <span className={styles.opp}>BYE</span>;
  } else if (c.status !== "scheduled") {
    body = <><span className={styles.opp}>—</span><span className={styles.sub}>no opponent</span></>;
  } else if (isGraded(c)) {
    body = (
      <>
        <span className={styles.opp}>{where(c)} {c.opponent}</span>
        <span className={styles.grade}>{GLYPH[c.label]} #{c.rank}</span>
        <span className={styles.sub}>{fmtDelta(c.delta, d.metric.kind) ?? "—"}</span>
      </>
    );
  } else {
    body = (
      <>
        <span className={styles.opp}>{where(c)} {c.opponent}</span>
        <span className={styles.grade}>? not graded</span>
        <span className={styles.sub}>{sampleText(c, d.min_games)}</span>
      </>
    );
  }
  return (
    <td className={cls} title={text}>
      <span aria-hidden="true">{body}</span>
      <VisuallyHidden>{text}</VisuallyHidden>
    </td>
  );
}

function Who({ r, d, state, sp }: { r: SosRow; d: SosHeatmap; state: SosState; sp: SearchParams }) {
  const p = r.player;
  if (!p) {
    return (
      <th scope="row" className={styles.who}>
        <Link href={sosHref(state, { nfl: r.nfl_team }, sp)} prefetch={false} title={`Only ${r.nfl_team}`}><b>{r.nfl_team}</b></Link>
        <span className={styles.whoMeta}>{d.position} offense</span>
      </th>
    );
  }
  return (
    <th scope="row" className={styles.who} title={[rowName(r), ...r.notes].join("\n")}>
      <PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} />
      <span className={styles.whoMeta}>{p.pro_team ?? "no team"}</span>
      {p.on_ir && <> <Badge value="IR" tone="bad" /></>}
      {(r.team_uncertain || !p.pro_team) && <> <Badge value={p.pro_team ? "Team?" : "No team"} tone="warn" title={r.notes.join(" ")} /></>}
    </th>
  );
}

function Links({ r, d, myTeamId }: { r: SosRow; d: SosHeatmap; myTeamId: number | null }) {
  const l = rowLinks(r, d.position, myTeamId);
  const name = rowName(r);
  return (
    <td className={styles.links}>
      {l.profile && <Link href={l.profile} prefetch={false}>Profile<VisuallyHidden> of {name}</VisuallyHidden></Link>}
      {l.waivers && <TeamLink href={l.waivers.href} prefetch={false}>{l.waivers.label}<VisuallyHidden>{r.player ? ` ${name}` : ""}</VisuallyHidden></TeamLink>}
      {l.trade && <TeamLink href={l.trade.href} prefetch={false}>{l.trade.label}<VisuallyHidden>{r.player ? ` ${name}` : ""}</VisuallyHidden></TeamLink>}
    </td>
  );
}

export function Heatmap({ d, state, sp, sort, myTeamId }: {
  d: SosHeatmap; state: SosState; sp: SearchParams; sort: SortState; myTeamId: number | null;
}) {
  const weeks = d.horizon.weeks;
  const rows = sortRows(d.rows, (r) => sortValue(r, sort.key), sort.dir);
  const firstPlayoff = d.horizon.regular_season_through !== null ? d.horizon.regular_season_through + 1 : null;
  const mixed = !d.horizon.known;
  const baseline = new Map(d.baselines.map((b) => [b.week, b]));
  const kind = d.metric.kind;
  const source = d.source.name
    ? <>Source: {d.source.name}{d.source.through_week !== null ? `, games through week ${d.source.through_week}` : ""}.</>
    : <>Source: unavailable ({d.source.reason ?? "no defensive evidence"}).</>;
  return (
    <Section
      title={`${d.position} strength of schedule: ${d.horizon.label.toLowerCase()}`}
      subtitle={<>Opponent defenses ranked on {d.metric.label} · comparable only within {d.position}</>}
      flush
      actions={
        <ul className={styles.legend} aria-label="Legend">
          {LEGEND.map((k) => (
            <li key={k}><span className={cx(styles.swatch, styles[k])} aria-hidden="true">{GLYPH[k]}</span>{d.labels[k]}</li>
          ))}
        </ul>
      }
      footnote={<>
        Each cell: the opponent (vs = home, @ = away), its rank among {d.position} defenses (#1 = most permissive so far)
        and its difference from the neutral baseline in {kind === "rate" ? "percentage points" : d.metric.unit}. A cell
        that is not graded shows the games sampled against the {d.min_games} needed. {source}{" "}
        {d.coverage.graded} of {d.coverage.scheduled} matchups in view are graded.
      </>}
    >
      <div className="table-wrap">
        <table className={styles.grid}>
          <caption><VisuallyHidden>
            {d.position} strength of schedule by week, {d.horizon.label.toLowerCase()}; rows are {state.player !== null ? "one player" : d.query.view === "teams" ? "NFL offenses" : "players"}.
          </VisuallyHidden></caption>
          <thead>
            <tr>
              <SortableTh sortKey="name" label={d.rows[0]?.player ? "Player" : "NFL team"} current={sort} searchParams={sp} className={styles.who} />
              {weeks.map((w) => (
                <th key={w} scope="col" className={cx(styles.wk, w === firstPlayoff && styles.playoffEdge)}>
                  Wk {w}
                  {mixed && <small>phase unknown</small>}
                  {!mixed && w === firstPlayoff && <small>playoffs</small>}
                </th>
              ))}
              <SortableTh sortKey="permissive" label="▲ wks" align="right" current={sort} searchParams={sp} title="Weeks against an observed more permissive defense" />
              <SortableTh sortKey="tougher" label="▼ wks" align="right" defaultDir="desc" current={sort} searchParams={sp} title="Weeks against an observed tougher defense" />
              <SortableTh sortKey="avg" label="Avg rank" align="right" defaultDir="asc" current={sort} searchParams={sp} title="Average opponent rank over graded weeks only (1 = most permissive)" />
              <th scope="col" className={styles.links}>Decide</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.player ? `p${r.player.espn_player_id}` : `t${r.nfl_team}`}>
                <Who r={r} d={d} state={state} sp={sp} />
                {r.cells.map((c) => <Cell key={c.week} c={c} d={d} edge={c.week === firstPlayoff} />)}
                <td className={styles.num}>{r.summary.counts.permissive}</td>
                <td className={styles.num}>{r.summary.counts.tougher}</td>
                <td className={styles.num}>
                  {r.summary.average_rank !== null
                    ? <>{r.summary.average_rank.toFixed(1)} <small>({r.summary.graded_weeks} of {r.summary.weeks} wk)</small></>
                    : <span className="missing" title="No graded week in this horizon: the average is missing, not 0">—</span>}
                </td>
                <Links r={r} d={d} myTeamId={myTeamId} />
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className={styles.baselineRow}>
              <th scope="row" className={styles.who}>Neutral baseline</th>
              {weeks.map((w) => {
                const b = baseline.get(w);
                const v = fmtValue(b?.value, kind);
                return (
                  <td key={w} className={cx(w === firstPlayoff && styles.playoffEdge)}
                    title={b?.value !== null && b ? `Pooled over ${b.ranked_defenses} rankable defenses` : (b?.reason ?? "No baseline")}>
                    {v ?? <span className="missing">—</span>}
                  </td>
                );
              })}
              <td colSpan={4} />
            </tr>
          </tfoot>
        </table>
      </div>
    </Section>
  );
}
