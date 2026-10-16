import { LineChart } from "@/components/charts";
import { OriginTag } from "@/components/ProjectionNotice";
import { Badge, Notice, Section, Stat, StatStrip } from "@/components/ui";
import { oursBlock, oursByWeek, oursCell, oursPoints, oursTotalNote } from "@/lib/forward-view";
import { num, pct, when } from "@/lib/format";
import { valueDomain } from "@/lib/player-usage";
import type { ForwardProjection } from "@/lib/types";
import styles from "./players.module.css";

const weeksRange = (weeks: number[]) => (weeks.length ? `wk ${weeks.join(", ")}` : "none");
const INCOMPLETE = "Not every week in this window has an ESPN projection, so no total is shown (missing weeks are not counted as 0)";

const OURS_INCOMPLETE = "Not every remaining week has a Fantasy Intel projection (ESPN's number is shown in those weeks), so no total is shown";

/**
 * ESPN's own weekly projections for every remaining fantasy week, shown unmodified: season-to-go
 * totals, a week-by-week table with the opponent, and the same numbers as a line (byes are gaps).
 * When the visitor's switch is on "Ours" the response also carries Fantasy Intel's series for the remaining
 * regular season: shown next to ESPN's, with ESPN-fallback weeks marked and the experimental note.
 */
export function ForwardView({ f }: { f: ForwardProjection }) {
  const s = f.summary;
  const span = (available: number | null, expected: number | null) => (expected === null ? "" : `${available}/${expected} wk`);
  const splitKnown = s.last_regular_season_week !== null;
  const thisWeekOnBye = f.weeks.find((w) => w.week === s.as_of_week)?.on_bye === true;
  const labels = f.weeks.map((w) => String(w.week));
  const points = f.weeks.map((w) => (w.on_bye ? null : w.espn_projected_points));
  const ours = oursBlock(f);
  const oursOn = ours?.available === true;
  const oursWeeks = ours && oursOn ? oursByWeek(ours) : null;
  const oursLine = ours && oursOn ? oursPoints(f.weeks, ours) : null;
  const fallback = ours?.fallback_weeks ?? [];

  return (
    <Section
      title="Forward view"
      actions={<span className="faint">{oursOn ? `${f.source} + Ours` : f.source} · weeks {s.as_of_week}–{s.final_week} · synced {when(f.projection_synced_at)}</span>}
      flush
      footnote={<>
        {f.note} These are ESPN&apos;s weekly projections, shown unmodified; totals require every week in the window
        (a projected 0 counts, a missing week does not). Byes come from the NFL schedule, not from a 0 projection.
        {oursOn && ours && <> {ours.label}. Ours covers the regular season only; weeks marked <OriginTag origin="espn" ours /> have no
          Fantasy Intel projection yet and show ESPN&apos;s number, which also keeps the Ours total empty.</>}
      </>}
    >
      {ours && !oursOn && (
        <Notice tone="caution" title="Showing ESPN projections">
          {ours.reason ?? "Fantasy Intel has no projections for the remaining weeks yet."}
        </Notice>
      )}
      <div className={styles.stripBlock}>
        <StatStrip>
          <Stat
            label="This week"
            value={thisWeekOnBye ? "Bye" : num(s.current_week_projection, 1, "No ESPN projection for this week")}
            note={thisWeekOnBye ? `week ${s.as_of_week} · no game` : `week ${s.as_of_week}`}
          />
          <Stat label="Rest of season" value={num(s.ros_projected_points, 1, INCOMPLETE)}
            note={<>{span(s.ros_weeks_available, s.ros_weeks_expected)} · {pct(s.ros_coverage, 0)} covered</>} />
          {ours && oursOn && (
            <Stat label="Ours: rest of season"
              value={num(ours.ros_projected_points, 1, OURS_INCOMPLETE)}
              note={oursTotalNote(ours)} />
          )}
          <Stat label="Regular season left"
            value={splitKnown ? num(s.regular_season_projected_points, 1, INCOMPLETE) : <span className="missing" title="Fantasy playoff split unknown">—</span>}
            note={splitKnown ? `${s.regular_season_weeks_expected ? `wk ${s.as_of_week}–${s.last_regular_season_week} · ` : ""}${span(s.regular_season_weeks_available, s.regular_season_weeks_expected)}` : "split unknown"} />
          <Stat label="Fantasy playoffs"
            value={splitKnown ? num(s.playoff_projected_points, 1, INCOMPLETE) : <span className="missing" title="Fantasy playoff split unknown">—</span>}
            note={splitKnown ? `wk ${Math.max((s.last_regular_season_week ?? 0) + 1, s.as_of_week)}–${s.final_week} · ${span(s.playoff_weeks_available, s.playoff_weeks_expected)}` : "split unknown"} />
          <Stat label="Remaining bye"
            value={s.bye_weeks_remaining === null ? <span className="missing" title="NFL schedule or team unknown for at least one remaining week">?</span> : s.bye_weeks_remaining === 0 ? "None" : weeksRange(s.bye_weeks)}
            note={s.missing_weeks.length > 0 ? `no projection ${weeksRange(s.missing_weeks)}` : undefined} />
        </StatStrip>
      </div>
      <div className={styles.forward}>
        <div className={`table-wrap ${styles.forwardTable}`}>
          <table>
            <thead>
              <tr><th>Week</th><th>Opponent</th>{oursOn && <th className="num" title={ours?.label}>Ours</th>}<th className="num">ESPN proj</th><th>Bye</th><th>Phase</th></tr>
            </thead>
            <tbody>
              {f.weeks.map((w) => (
                <tr key={w.week}>
                  <td>{w.week}</td>
                  <td>
                    {w.on_bye ? <span className="faint">—</span>
                      : w.opponent ? `${w.is_home ? "vs" : "@"} ${w.opponent}`
                        : <span className="missing" title="NFL schedule unavailable">—</span>}
                  </td>
                  {oursOn && <OursTd cell={oursCell(oursWeeks?.get(w.week))} />}
                  <td className="num">{num(w.espn_projected_points, 1, "ESPN returned no projection for this week (unknown, not 0)")}</td>
                  <td>{w.on_bye === null ? <span className="missing" title="Schedule unknown">?</span> : w.on_bye ? <Badge value="BYE" tone="warn" /> : null}</td>
                  <td className={styles.phase}>{w.phase === "playoff" ? "Playoffs" : w.phase === "regular" ? "Regular" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className={styles.forwardChart}>
          <h3 className={styles.chartTitle}>{oursLine ? "Projection by week" : "ESPN projection by week"}</h3>
          <LineChart
            series={[
              ...(oursLine ? [{ id: "ours", label: "Ours", points: oursLine }] : []),
              { id: "espn", label: "ESPN projection", points },
            ]}
            xLabels={labels}
            yFormat={(v) => String(Math.round(v * 10) / 10)}
            yDomain={valueDomain(oursLine ? [...points, ...oursLine] : points, "pts")}
            height={200}
            width={420}
            markers
            ariaLabel={oursLine ? "Ours and ESPN projection by remaining week" : "ESPN projection by remaining week"}
          />
          <p className={styles.chartNote}>
            Bye weeks are gaps: a bye is not a projected zero.
            {oursLine && <> {fallback.length > 0 ? `Ours is also a gap in wk ${fallback.join(", ")} (ESPN fallback). ` : " "}Experimental beyond next week.</>}
          </p>
        </div>
      </div>
    </Section>
  );
}

/** The Ours cell: our number, a 0 for a bye or unavailable, or ESPN's number with an ESPN tag. */
function OursTd({ cell }: { cell: ReturnType<typeof oursCell> }) {
  if (cell.kind === "none" || cell.kind === "bye") return <td className="num"><span className="faint" title={cell.title}>—</span></td>;
  return (
    <td className="num" title={cell.title}>
      {cell.kind === "espn" && <OriginTag origin="espn" ours />}{" "}
      {num(cell.value, 1, cell.title)}
      {cell.experimental && <sup className="faint" aria-label="experimental"> exp</sup>}
    </td>
  );
}
