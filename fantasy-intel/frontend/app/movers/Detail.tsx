import { LineChart } from "@/components/charts";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Delta, Notice, Section } from "@/components/ui";
import { Missing } from "@/lib/format";
import {
  GROUP_TITLE, PCT_MISSING, SOURCE_NAME, beforeAfterText, groupSignals, informationWindowText, pctText, rankText, rankTitle,
  requestedMissingText, signalSample, signalValue, signedPts, snapshotName, weekNote, weekSeries, windowText,
  type EvidenceSignal, type Mover, type MoversResponse,
} from "@/lib/projection-movers";
import styles from "./movers.module.css";
import { WhyItChanged } from "./WhyItChanged";

const oneDecimal = (v: number) => String(Math.round(v * 10) / 10);

function SignalItem({ s }: { s: EvidenceSignal }) {
  const before = signalValue(s.before, s.unit);
  const after = signalValue(s.after, s.unit);
  const sample = signalSample(s);
  return (
    <li className={styles.sig}>
      <p className={styles.sigHead}>
        <strong>{s.label}</strong>
        <span className={styles.sigNums}>
          {before ?? <Missing why="No earlier value was recorded" />} → {after ?? <Missing why="No later value was recorded" />}
        </span>
      </p>
      <p className={styles.sigMeta}>{[sample, s.source].filter(Boolean).join(" · ")}</p>
      {s.detail && <p className={styles.sigDetail}>{s.detail}</p>}
    </li>
  );
}

function SignalGroup({ title, signals }: { title: string; signals: EvidenceSignal[] }) {
  if (signals.length === 0) return null;
  return (
    <div className={styles.sigGroup}>
      <h4 className={styles.groupTitle}>{title}</h4>
      <ul className={styles.sigList}>{signals.map((s) => <SignalItem key={`${s.kind}:${s.key}`} s={s} />)}</ul>
    </div>
  );
}

/** Ours: the exact split of the change into availability and per-game parts, when no model signal carries it. */
function Components({ m }: { m: Mover }) {
  if (!m.components) return null;
  return (
    <div className={styles.sigGroup}>
      <h4 className={styles.groupTitle}>{GROUP_TITLE.model}</h4>
      <ul className={styles.sigList}>
        <li className={styles.sig}>
          <p className={styles.sigHead}><strong>Availability</strong><span className={styles.sigNums}>{signedPts(m.components.availability)}</span></p>
          <p className={styles.sigDetail}>How much of the change comes from the model&apos;s chance that the player appears.</p>
        </li>
        <li className={styles.sig}>
          <p className={styles.sigHead}><strong>Per-game outlook</strong><span className={styles.sigNums}>{signedPts(m.components.per_game)}</span></p>
          <p className={styles.sigDetail}>How much comes from the points expected in the games played. The two parts add up to the change.</p>
        </li>
      </ul>
    </div>
  );
}

function EvidencePanel({ m }: { m: Mover }) {
  const e = m.evidence;
  const groups = groupSignals(e);
  const window = informationWindowText(e);
  return (
    <div className={styles.evidence}>
      <h3 className={styles.panelTitle}>Recorded alongside the change</h3>
      {!e ? (
        <p className={styles.muted}>Evidence is read only for the biggest risers and fallers, so none is shown for {m.name}.</p>
      ) : (
        <>
          <p className={e.headline ? styles.headline : styles.muted}>
            {e.headline ?? "No recorded change in usage, availability or role passed the thresholds."}
          </p>
          <SignalGroup title={GROUP_TITLE.recorded} signals={groups.recorded} />
          {groups.model.length > 0 ? <SignalGroup title={GROUP_TITLE.model} signals={groups.model} /> : <Components m={m} />}
          {e.missing.length > 0 && (
            <div className={styles.sigGroup}>
              <h4 className={styles.groupTitle}>Not found or not available</h4>
              <ul className={styles.missingList}>{e.missing.map((t, i) => <li key={i}>{t}</li>)}</ul>
            </div>
          )}
          {window && <p className={styles.sigMeta}>Read from games and records in this period: {window}</p>}
          <p className={styles.note}>{e.note}</p>
        </>
      )}
      {!e && groups.model.length === 0 && <Components m={m} />}
    </div>
  );
}

function WeekTable({ m, before, after }: { m: Mover; before: string; after: string }) {
  const weeks = [...m.weeks].sort((a, b) => a.week - b.week);
  const notes = weeks.map(weekNote);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Week</th>
            <th scope="col" className="num" title={`Forecast ${before}`}>Earlier</th>
            <th scope="col" className="num" title={`Forecast ${after}`}>Latest</th>
            <th scope="col" className="num">Change</th>
          </tr>
        </thead>
        <tbody>
          {weeks.map((w, i) => (
            <tr key={w.week}>
              <td>
                {w.week}
                {notes[i] && <span className={styles.weekNote}>{notes[i]}</span>}
              </td>
              <td className="num">{w.before === null ? <Missing why="No projection in the earlier forecast" /> : oneDecimal(w.before)}</td>
              <td className="num">{w.after === null ? <Missing why="No projection in the latest forecast" /> : oneDecimal(w.after)}</td>
              <td className="num"><Delta value={w.delta} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The selected player: totals, the per-week before and after, and the evidence recorded alongside the change. */
export function Detail({ d, m, requestedMissing }: { d: MoversResponse; m: Mover; requestedMissing: boolean }) {
  const before = d.baseline ? snapshotName(d.baseline) : "earlier";
  const after = d.current ? snapshotName(d.current) : "latest";
  const series = weekSeries(m);
  const pct = pctText(m.pct);
  const rank = rankText(m);
  const span = windowText(d.window.weeks);
  return (
    <Section id="detail" title="Player detail" subtitle={`${SOURCE_NAME[d.source]} · ${span ?? "no weeks"}`}>
      {requestedMissing && (
        <Notice tone="info" title="That player is not in this comparison">
          {requestedMissingText(d) ?? "The player may be filtered out by position, or may not be comparable for these weeks."} Another mover from this comparison is shown instead.
        </Notice>
      )}
      <div className={styles.detailHead}>
        <PlayerIdentity id={m.espn_player_id} name={m.name} position={m.position} team={m.nfl_team} size="lg" />
        <p className={styles.detailFacts}>
          <span>{beforeAfterText(m)} pts</span>
          <span className={styles.big}><Delta value={m.delta} /></span>
          <span>{pct ?? <Missing why={PCT_MISSING} />}</span>
          <span title={rankTitle(m)}>{rank ?? <Missing why={rankTitle(m)} />}</span>
        </p>
      </div>
      <div className={styles.detailGrid}>
        <div className={styles.detailChart}>
          <LineChart
            series={[
              { id: "before", label: `Earlier forecast (${before})`, color: "var(--chart-6)", points: series.before },
              { id: "after", label: `Latest forecast (${after})`, color: "var(--chart-1)", points: series.after },
            ]}
            xLabels={series.labels}
            yFormat={oneDecimal}
            height={220}
            width={420}
            markers
            title="Projected points by week"
            caption={`${SOURCE_NAME[d.source]}. A bye week is a gap, not a projected zero.`}
            ariaLabel={`${m.name}: projected points by week, earlier and latest forecast`}
          />
          <WeekTable m={m} before={before} after={after} />
        </div>
        <EvidencePanel m={m} />
      </div>
      <WhyItChanged m={m} />
    </Section>
  );
}
