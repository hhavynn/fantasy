import type { ReactNode } from "react";
import { OriginTag } from "@/components/ProjectionNotice";
import { Badge, TrendArrow, VisuallyHidden } from "@/components/ui";
import { cx } from "@/lib/cx";
import {
  type Cell, type CompareCard, type CompareResponse, type DisplayRow, type Edge,
  byeText, edgeMark, edgeText, nextOpponentText, playerName, projectionRows, upcomingText, usageRows, allRows, rowNote,
} from "@/lib/compare";
import { label } from "@/lib/format";
import { EMPTY_RANGES, RANGE_TITLE, rangeFor, rangeLabel, rangeText, type RangeMap } from "@/lib/projection-ranges";
import styles from "./compare.module.css";

// Side-by-side tables: Measure | Player A | edge | Player B. The edge column is an arrow toward the side with the
// edge (or "=" / "–"), always paired with hidden text, so colour is never the only signal.

function Names({ r }: { r: CompareResponse }) {
  const [a, b] = r.players;
  return (
    <>
      <th scope="col" className={cx(styles.sideA, styles.thSide)}><span className={styles.sideDot} aria-hidden="true" />{playerName(a)}</th>
      <th scope="col" className={styles.thEdge}><VisuallyHidden>Edge</VisuallyHidden></th>
      <th scope="col" className={cx(styles.sideB, styles.thSide)}><span className={styles.sideDot} aria-hidden="true" />{playerName(b)}</th>
    </>
  );
}

export function EdgeMark({ edge, nameA, nameB }: { edge: Edge; nameA: string; nameB: string }) {
  return (
    <span className={cx(styles.edge, styles[`edge_${edge === "n/a" ? "na" : edge}`])}>
      <span aria-hidden="true">{edgeMark(edge)}</span>
      <VisuallyHidden>{edgeText(edge, nameA, nameB)}</VisuallyHidden>
    </span>
  );
}

/** A value cell: the number, or a dash with the reason it is absent in plain text (not only a tooltip). */
export function ValueCell({ cell }: { cell: Cell }) {
  if (cell.missing) {
    return (
      <>
        <span className="missing" title={cell.why ?? "No data available"}>—</span>
        {cell.tag === "espn_fallback" ? <> <OriginTag origin="espn" ours title="No independent estimate: ESPN's projection stands in elsewhere" /></> : null}
        {cell.why ? <span className={styles.why}>{cell.why}</span> : null}
      </>
    );
  }
  return (
    <>
      <span className={styles.val}>{cell.text}</span>
      {cell.tag === "zero" ? <> <Badge value="Model: 0" tone="" title={cell.why ?? undefined} /></> : null}
    </>
  );
}

function RowView({ row, r }: { row: DisplayRow; r: CompareResponse }) {
  const [a, b] = r.players;
  const note = rowNote(row.note);
  return (
    <tr>
      <th scope="row" className={styles.measure}>
        <span className={styles.measureName}>
          {row.label}
        </span>
        <span className={styles.unit}>{row.unit}</span>
        {note ? <span className={styles.note}>{note}</span> : null}
      </th>
      <td className={styles.cell}><ValueCell cell={row.a} /></td>
      <td className={styles.edgeCell}><EdgeMark edge={row.edge} nameA={playerName(a)} nameB={playerName(b)} /></td>
      <td className={styles.cell}><ValueCell cell={row.b} /></td>
    </tr>
  );
}

function PlainRow({ name, hint, a, b }: { name: string; hint?: ReactNode; a: ReactNode; b: ReactNode }) {
  return (
    <tr>
      <th scope="row" className={styles.measure}>
        <span className={styles.measureName}>{name}</span>
        {hint ? <span className={styles.unit}>{hint}</span> : null}
      </th>
      <td className={styles.cell}>{a}</td>
      <td className={styles.edgeCell} aria-hidden="true" />
      <td className={styles.cell}>{b}</td>
    </tr>
  );
}

const missing = (why: string) => (
  <>
    <span className="missing" title={why}>—</span>
    <span className={styles.why}>{why}</span>
  </>
);

const orMissing = (text: string | null, why: string): ReactNode => (text === null ? missing(why) : <span className={styles.val}>{text}</span>);

function EdgeLegend() {
  return (
    <p className={styles.legend}>
      <span aria-hidden="true">◀ ▶</span> points at the player with an edge, shown only when the gap passes a materiality threshold;{" "}
      <span aria-hidden="true">=</span> even; <span aria-hidden="true">–</span> not comparable. Higher is better in every row, games remaining included.
    </p>
  );
}

/** Position, schedule and the projection rows. */
export function ProjectionTable({ r, ranges = EMPTY_RANGES, espnProjections = true }: { r: CompareResponse; ranges?: RangeMap; espnProjections?: boolean }) {
  const [a, b] = r.players;
  const rows = projectionRows(r, espnProjections);
  const noWindow = "The league's projection window is unknown.";
  return (
    <>
      <div className="table-wrap">
        <table className={styles.compareTable}>
          <thead><tr><th scope="col">Measure</th><Names r={r} /></tr></thead>
          <tbody>
            <PlainRow name="Position" a={<PosCell card={a} />} b={<PosCell card={b} />} />
            <PlainRow name="Next opponent" hint={`Week ${r.as_of_week}`}
              a={orMissing(nextOpponentText(a), noWindow)} b={orMissing(nextOpponentText(b), noWindow)} />
            {rows.map((row) => <RowView key={row.key} row={row} r={r} />)}
            {ranges.size > 0 && (
              <PlainRow name="Range" hint={`Week ${r.as_of_week} · floor–ceiling if he plays`}
                a={<RangeCell card={a} ranges={ranges} />} b={<RangeCell card={b} ranges={ranges} />} />
            )}
            <PlainRow name="Bye week(s)" hint="This season's remaining schedule"
              a={orMissing(byeText(a), a.projection ? "No bye listed in the remaining weeks." : noWindow)}
              b={orMissing(byeText(b), b.projection ? "No bye listed in the remaining weeks." : noWindow)} />
            <PlainRow name="Next opponents" hint="Week, then opponent"
              a={orMissing(upcomingText(a), noWindow)} b={orMissing(upcomingText(b), noWindow)} />
          </tbody>
        </table>
      </div>
      <EdgeLegend />
    </>
  );
}

/** Ours floor–ceiling for the week (RB, WR, TE), or why there is none. */
function RangeCell({ card, ranges }: { card: CompareCard; ranges: RangeMap }) {
  const range = rangeFor(ranges, card.player.espn_player_id);
  if (!range) {
    const pos = card.player.position;
    return missing(pos === "RB" || pos === "WR" || pos === "TE" ? "No Ours projection this week" : `Ranges cover RB, WR and TE`);
  }
  return (
    <span className={styles.val} title={RANGE_TITLE}>
      <span aria-hidden="true">{rangeText(range)}</span>
      <VisuallyHidden>{rangeLabel(range)}</VisuallyHidden>
    </span>
  );
}

function PosCell({ card }: { card: CompareCard }) {
  const p = card.player;
  return <span className={styles.val}>{p.position ?? "?"} · {p.pro_team ?? "No NFL team"}</span>;
}

/** Usage metrics, trend and ratings, then the per-player context that has no edge. */
export function UsageTable({ r }: { r: CompareResponse }) {
  const [a, b] = r.players;
  const rows = usageRows(r);
  const stab = (c: CompareCard) => {
    const s = c.usage.stability;
    if (s.cv === null || s.cv === undefined) return missing(s.games < 4 ? `Needs 4 or more games; has ${s.games}.` : "No stability measure.");
    return <><span className={styles.val}>{s.cv.toFixed(2)}</span> <span className="faint">({s.metric === "opportunities" ? "opportunities" : "xFP"}, {s.games} games)</span></>;
  };
  const trend = (c: CompareCard) => {
    const t = c.usage.trend.label;
    return t ? <span className={styles.val}><TrendArrow label={t} /> {label(t)}</span> : missing("Not enough games for a trend.");
  };
  const games = (c: CompareCard) =>
    c.usage.games > 0
      ? <span className={styles.val}>{c.usage.games}</span>
      : missing("No completed games with a usage record.");
  return (
    <>
      <div className="table-wrap">
        <table className={styles.compareTable}>
          <thead><tr><th scope="col">Measure</th><Names r={r} /></tr></thead>
          <tbody>
            <PlainRow name="Games with usage" hint={`Completed weeks through ${r.completed_through_week}`} a={games(a)} b={games(b)} />
            {rows.map((row) => <RowView key={row.key} row={row} r={r} />)}
            <PlainRow name="Usage trend label" hint="Recent games vs the baseline games before them" a={trend(a)} b={trend(b)} />
            <PlainRow name="Opportunity variability" hint="Coefficient of variation of weekly xFP; lower is steadier" a={stab(a)} b={stab(b)} />
          </tbody>
        </table>
      </div>
      <EdgeLegend />
    </>
  );
}

/** Compact table of every statistical row: value, marker and accessible edge text. */
export function AdvantagesTable({ r, espnProjections = true }: { r: CompareResponse; espnProjections?: boolean }) {
  const [a, b] = r.players;
  const na = playerName(a);
  const nb = playerName(b);
  return (
    <>
      <div className="table-wrap">
        <table className={cx(styles.compareTable, styles.compact)}>
          <thead><tr><th scope="col">Measure</th><Names r={r} /></tr></thead>
          <tbody>
            {allRows(r, espnProjections).map((row) => (
              <tr key={row.key}>
                <th scope="row" className={styles.measure}><span className={styles.measureName}>{row.label}</span></th>
                <td className={styles.cell}>{row.a.missing ? <span className="missing" title={row.a.why ?? undefined}>—</span> : <span className={styles.val}>{row.a.text}</span>}</td>
                <td className={styles.edgeCell}><EdgeMark edge={row.edge} nameA={na} nameB={nb} /></td>
                <td className={styles.cell}>{row.b.missing ? <span className="missing" title={row.b.why ?? undefined}>—</span> : <span className={styles.val}>{row.b.text}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <EdgeLegend />
    </>
  );
}
