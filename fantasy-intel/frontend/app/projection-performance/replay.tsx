import { Delta, EmptyState, Pos, Section } from "@/components/ui";
import { cx } from "@/lib/cx";
import { num, when } from "@/lib/format";
import {
  REPLAY_GROUPS, diffFor, replaySummary, replayView, seasonLabel, type ReplayGroup, type ReplayResponse,
} from "@/lib/model-replay";
import styles from "./performance.module.css";

/** A MAE change, lower is better; a change under the "about the same" threshold is muted, never colored. */
function DiffText({ diff, same }: { diff: number | null; same: number }) {
  if (diff !== null && Math.abs(diff) < same) {
    return <span className="faint">{diff > 0 ? "+" : diff < 0 ? "−" : ""}{Math.abs(diff).toFixed(2)}</span>;
  }
  return <Delta value={diff} digits={2} invert />;
}

const head = (g: ReplayGroup) => (g === "ALL" ? "All" : <Pos value={g} />);

/** Today's model replayed on every finished week: does each model change help or hurt, week by week? */
export function WeeklyReplay({ d }: { d: ReplayResponse }) {
  const s = d.latest;
  const v = replayView(d);
  if (!d.available || !s || !v) {
    return (
      <Section title="Weekly replay" subtitle="Today's model re-run on every finished week">
        <EmptyState title="No replay yet">{d.unavailable_reason ?? "The next scheduled refresh builds it."}</EmptyState>
      </Section>
    );
  }
  const c = v.comparison;
  return (
    <Section title="Weekly replay"
      subtitle={`${s.model.version} re-run on every finished ${s.season} week using only data from before that week · rebuilt ${when(s.generated_at)}`}
      flush footnote={s.notes.join(" ")}>
      <p className={styles.plain}>
        <span className={styles.strong}>Against {v.label}: </span>{replaySummary(c)}
        {v.mode === "prior_version" && " The next model change will be compared week by week against this replay."}
        <span className={cx("faint", styles.small)}>
          Cells: mean absolute error in league points (lower is closer) and the change against {v.label};
          a change under {d.same_within} points counts as about the same.
        </span>
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Week</th>
              {REPLAY_GROUPS.map((g) => <th key={g} className="num" colSpan={2}>{head(g)}</th>)}
            </tr>
          </thead>
          <tbody>
            {s.weeks.map((w) => (
              <tr key={w.week}>
                <td>{w.week}{c.inputs_changed_weeks.includes(w.week) && (
                  <span className="faint" title="This week's inputs changed between the two replays (data corrections): its change mixes model and data"> †</span>)}</td>
                {REPLAY_GROUPS.map((g) => {
                  const cell = w.groups[g];
                  const dd = diffFor(c, w.week, g);
                  return [
                    <td key={`${g}m`} className="num" title={`${cell.n} player-weeks`}>{num(cell.mae.model, 2, "No player-weeks")}</td>,
                    <td key={`${g}d`} className="num" title={dd ? `${dd.direction} · n ${dd.n_new} vs ${dd.n_old}` : "Nothing to compare"}>
                      <DiffText diff={dd?.diff ?? null} same={d.same_within} />
                    </td>,
                  ];
                })}
              </tr>
            ))}
            <tr>
              <td className={styles.strong}>{seasonLabel(c, s.weeks.map((w) => w.week))}</td>
              {REPLAY_GROUPS.map((g) => {
                const sc = c.season.groups[g];
                return [
                  <td key={`${g}m`} className={cx("num", styles.strong)} title={`${sc.n_new} player-weeks`}>{num(sc.mae_new, 2, "No player-weeks")}</td>,
                  <td key={`${g}d`} className="num" title={`${num(sc.mae_old, 2)} before, same weeks`}><DiffText diff={sc.diff} same={d.same_within} /></td>,
                ];
              })}
            </tr>
          </tbody>
        </table>
      </div>
      {d.history.length > 1 && (
        <>
          <h3 className={styles.subhead}>Replay history (newest first)</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Replayed</th><th>Model</th><th>Weeks</th><th className="num">MAE, all</th><th className="num">Player-weeks</th></tr>
              </thead>
              <tbody>
                {d.history.map((h) => (
                  <tr key={`${h.generated_at}-${h.model.fingerprint}`}>
                    <td>{when(h.generated_at)}</td>
                    <td>{h.model.version} <span className="faint">{h.model.fingerprint?.slice(0, 7)}</span></td>
                    <td>{h.weeks.length ? `${h.weeks[0]}–${h.weeks[h.weeks.length - 1]}` : "—"}</td>
                    <td className="num">{num(h.mae, 2)}</td>
                    <td className="num">{h.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Section>
  );
}
