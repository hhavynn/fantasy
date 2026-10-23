import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Missing } from "@/lib/format";
import { plural } from "@/lib/plural";
import {
  contributionText, deltaSentence, rangeText, sideRankText,
  type CompareResponse, type CompareRow, type HistoryBlock, type ModelDrivers,
} from "@/lib/projection-compare";
import { signalValue, type EvidenceUnit } from "@/lib/projection-movers";
import { formatPoints } from "@/lib/rankings";
import styles from "./compare.module.css";

function Drivers({ m }: { m: ModelDrivers }) {
  if (!m.available) return <p className={styles.muted}>{m.reason}.</p>;
  const run = m.run;
  return (
    <>
      {m.inputs.length > 0 ? (
        <ul className={styles.factList}>
          {m.inputs.map((e, i) => (
            <li key={`${e.feature ?? "input"}-${i}`}>
              <span>{e.text ?? e.feature ?? "Model input"}</span>
              <span className={styles.factNum} title="This input's contribution to the projection, in points">
                {contributionText(e.contribution) ?? <Missing why="The run records no contribution for this input" />}
              </span>
            </li>
          ))}
        </ul>
      ) : <p className={styles.muted}>The stored run records no input contributions for this player.</p>}
      {(m.flags.length > 0 || m.missing_inputs.length > 0) && (
        <p className={styles.small}>
          {m.flags.length > 0 && <>Data flags: {m.flags.join("; ")}. </>}
          {m.missing_inputs.length > 0 && <>Missing inputs: {m.missing_inputs.join("; ")}.</>}
        </p>
      )}
      <p className={styles.provenance}>
        Week {m.week} · {run.model_version} run {run.run_id}
        {run.data_through && <> · data through {run.data_through.season} week {run.data_through.week}</>}
        {m.games_used !== null && <> · {m.games_used} {plural(m.games_used, "game")} used</>}
        {m.p_play !== null && <> · chance of appearing {Math.round(m.p_play * 100)}% (model)</>}
      </p>
      <p className={styles.small}>{m.note}</p>
    </>
  );
}

function History({ h }: { h: HistoryBlock }) {
  if (!h.available) return <p className={styles.muted}><b>{h.label}:</b> {h.reason ?? "No forecast history"}</p>;
  const p = h.player;
  const span = `${h.baseline} → ${h.current}`;
  if (!p || !p.listed) return <p className={styles.muted}><b>{h.label}</b> ({span}): {p && !p.listed ? p.text : "Not listed"}.</p>;
  return (
    <div className={styles.history}>
      <p>
        <b>{h.label}</b> ({span}): {formatPoints(p.before)} → {formatPoints(p.after)} points
        {p.rank_before !== null && p.rank_after !== null && <>, rank {p.rank_before} → {p.rank_after}</>}.
      </p>
      {p.evidence.headline && <p className={styles.small}>Recorded alongside: {p.evidence.headline}.</p>}
      {p.evidence.signals.length > 0 && (
        <ul className={styles.factList}>
          {p.evidence.signals.map((s, i) => (
            <li key={`${s.label}-${i}`}>
              <span>{s.label}{s.source ? <span className={styles.sigSource}> · {s.source}</span> : null}</span>
              <span className={styles.factNum}>
                {signalValue(s.before as number | string | null, s.unit as EvidenceUnit) ?? "—"} → {signalValue(s.after as number | string | null, s.unit as EvidenceUnit) ?? "—"}
              </span>
            </li>
          ))}
        </ul>
      )}
      {p.evidence.note && <p className={styles.small}>{p.evidence.note}</p>}
    </div>
  );
}

function Item({ row, data }: { row: CompareRow; data: CompareResponse }) {
  const name = data.provider.name;
  const ex = row.explanation;
  const pr = sideRankText(row.sources.provider, data.scope);
  const or = sideRankText(row.sources.ours, data.scope);
  const range = row.range && row.range.available ? row.range : null;
  return (
    <li className={styles.item}>
      <details>
        <summary className={styles.itemSummary}>
          <PlayerIdentity id={row.espn_player_id} name={row.full_name} position={row.position} team={row.pro_team} link={false} />
          <span className={styles.itemGap}>
            <span>{name} {pr} · Ours {or}</span>
            <span className={styles.small}>{deltaSentence(row.delta, name, data.scope)}</span>
          </span>
        </summary>
        {ex && (
          <div className={styles.itemBody}>
            <div>
              <h3 className={styles.panelTitle}>Model drivers (Ours)</h3>
              <Drivers m={ex.model} />
            </div>
            <div>
              <h3 className={styles.panelTitle}>Recent forecast history</h3>
              <History h={ex.history.ours} />
              <History h={ex.history.provider} />
            </div>
            <div>
              <h3 className={styles.panelTitle}>Range</h3>
              {range ? (
                <p>
                  {rangeText(range)} points if he plays in week {range.week}, around the week&apos;s Ours projection of {formatPoints(range.centre)} (median {formatPoints(range.median)}).{" "}
                  <span className={styles.small}>{range.horizon}. {range.basis}. Coverage: {range.coverage.text}.</span>
                </p>
              ) : <p className={styles.muted}>{(row.range && !row.range.available && row.range.reason) || "No range"}.</p>}
            </div>
          </div>
        )}
      </details>
    </li>
  );
}

/** The large gaps on this page, each with the model's own stored drivers, the recorded forecast history and the range. */
export function Disagreements({ data }: { data: CompareResponse }) {
  const rows = data.players.filter((r) => r.delta.large && r.explanation);
  if (rows.length === 0) return null;
  return <ul className={styles.items}>{rows.map((r) => <Item key={r.espn_player_id} row={r} data={data} />)}</ul>;
}
