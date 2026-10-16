import { Badge, Method, Notice, Pos, Section } from "@/components/ui";
import { cx } from "@/lib/cx";
import { Missing, num, when } from "@/lib/format";
import { STATUS_LABEL, clearsBar, coversLaterGames, matchedProgress, signedPct, type PromotionTracker } from "@/lib/promotion";
import styles from "./data.module.css";

const SYNC_CMD = "uv run python ../scripts/sync_espn.py";
const BASELINE: Record<string, string> = { recent_avg: "recent average", season_avg: "season average" };

function SnapshotNotice({ t }: { t: PromotionTracker }) {
  const s = t.pregame_snapshot;
  if (!s.first_kickoff) {
    return <Notice tone="info" title={`Week ${s.week} schedule not synced`}>The first kickoff is unknown, so the pregame snapshot can&apos;t be checked.</Notice>;
  }
  if (s.captured) {
    return (
      <Notice tone="positive" title={`Week ${s.week} pregame ESPN snapshot captured`}>
        Latest before the first kickoff ({when(s.first_kickoff)}): {when(s.latest_pregame_snapshot_at)} · {s.players} players.
        Each player counts the last snapshot taken before his own kickoff, so syncing again before Sunday keeps the comparison current.
      </Notice>
    );
  }
  const late = s.latest_snapshot_at;
  return (
    <Notice tone="caution" title={`No ESPN snapshot from before week ${s.week}'s first kickoff`}>
      First kickoff {when(s.first_kickoff)}. From <code>backend/</code>, run <code>{SYNC_CMD}</code> before each week&apos;s first kickoff.
      {late && (coversLaterGames(late, s.last_kickoff)
        ? <> The latest snapshot ({when(late)}) came after the first kickoff: it doesn&apos;t count for games that had already started, but it still counts for players whose games kick off after it.</>
        : <> The latest snapshot ({when(late)}) came after every game had kicked off, so it doesn&apos;t count for this week.</>)}
    </Notice>
  );
}

export function PromotionSection({ t }: { t: PromotionTracker }) {
  const c = t.criteria;
  const bar = c.min_relative_mae_improvement_vs_best_trailing_baseline;
  const barText = `${(bar * 100).toFixed(0)}%`;
  return (
    <Section
      title="Independent projections: path to promotion"
      flush
      footnote="Read only. Nothing here promotes a model or changes lineups, start/sit, trades or waivers."
    >
      <div className={styles.block}>
        <p className={styles.meta}>
          Model <b>{t.model_version}</b> is production by owner override, not by these criteria: its validation so far is
          historical research only and it is not shown to be better than ESPN. A position can be called Start/Sit-ready only
          after it passes the frozen criteria (held-out improvement of at least {barText} over the better trailing baseline
          with a 95% CI below 0, then at least {c.espn_min_matched_per_position} matched pregame ESPN observations with MAE
          no worse than ESPN).
        </p>
        <div className={styles.padded}><SnapshotNotice t={t} /></div>
        {t.run_missing_reason && <div className={styles.padded}><Notice tone="caution" title="No stored held-out status">{t.run_missing_reason}</Notice></div>}
        {!t.espn.available && <div className={styles.padded}><Notice tone="caution" title="ESPN comparison unavailable">{t.espn.unavailable_reason}</Notice></div>}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Pos</th><th>Status</th>
                <th className="num" title={`Held-out MAE improvement over the better trailing-average baseline; the bar is ${barText}`}>vs baseline</th>
                <th>Held-out MAE</th>
                <th title={`Player-weeks with a pregame ESPN projection, a completed game and an independent projection; ${c.espn_min_matched_per_position} required`}>Matched pregame ESPN</th>
                <th className="num" title="Mean absolute error on matched player-weeks, league points">MAE ours / ESPN</th>
                <th>Start/Sit</th>
              </tr>
            </thead>
            <tbody>
              {t.positions.map(({ position, model: m, espn: e }) => {
                const st = m.status ? STATUS_LABEL[m.status] ?? { text: m.status, tone: "" } : null;
                const clears = clearsBar(m.relative_mae_improvement, bar);
                const share = matchedProgress(e.n, e.required);
                return (
                  <tr key={position}>
                    <td><Pos value={position} /></td>
                    <td>{st ? <Badge value={st.text} tone={st.tone} title={m.reasons.join("; ") || undefined} /> : <Missing why="No stored held-out evaluation" />}</td>
                    <td className="num">
                      {m.relative_mae_improvement === null ? <Missing why="No stored held-out evaluation" /> : (
                        <span className={cx(clears ? styles.clears : styles.short)} title={clears ? "Clears the bar" : "Below the bar"}>
                          {signedPct(m.relative_mae_improvement)}
                        </span>
                      )}
                      <span className="faint"> / {barText}</span>
                    </td>
                    <td className={styles.nowrap}>
                      {m.mae_primary === null ? <Missing why="No stored held-out evaluation" /> : (
                        <>{num(m.mae_primary, 2)} <span className="faint">vs {num(m.mae_best_baseline, 2)} {BASELINE[m.best_trailing_baseline ?? ""] ?? m.best_trailing_baseline} · n {m.heldout_n}</span></>
                      )}
                    </td>
                    <td>
                      <div className={styles.progress}>
                        <span className={styles.count}>{e.n} / {e.required}</span>
                        <span className={cx("meter", styles.meter)} role="img" aria-label={`${e.n} of ${e.required} matched observations`}>
                          <div style={{ width: `${share * 100}%` }} />
                        </span>
                      </div>
                      <span className={cx("faint", styles.small)}>{e.pregame_completed} with pregame ESPN and a completed game</span>
                    </td>
                    <td className="num">
                      {e.n === 0 ? <Missing why="No matched observations yet" /> : <>{num(e.mae_independent, 2)} / {num(e.mae_espn, 2)}</>}
                    </td>
                    <td>
                      {m.start_sit_ready === null ? <Missing why="No stored held-out evaluation" /> : (
                        <Badge value={m.start_sit_ready ? "Ready" : "Not ready"} tone={m.start_sit_ready ? "good" : ""} title={m.reasons.join("; ") || undefined} />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <Method title="Criteria, evidence by week and how to keep it accumulating">
          <ul className={styles.list}>
            <li>Held-out: at least {c.heldout_min_observations_per_position} observations per position; improvement over the better trailing baseline of at least {barText}{c.require_bootstrap_ci_upper_below_zero ? ", with the week-clustered bootstrap 95% CI of the MAE difference entirely below 0" : ""}.</li>
            <li>Frozen rule: {c.espn_rule.replace("espn_min_matched", String(c.espn_min_matched_per_position))}</li>
            {t.notes.map((n) => <li key={n}>{n}</li>)}
            <li>
              Weekly: from <code>backend/</code> run <code>{SYNC_CMD}</code> before Thursday&apos;s first kickoff (again before Sunday is fine), and
              <code>uv run python ../scripts/backtest_independent_projections.py project --week N</code> so the week has a stored independent projection.
              A run counts only if its model artifact was fitted before that week&apos;s first kickoff, because <code>fit</code> trains on every completed week.
              Once a position has {c.espn_min_matched_per_position} matched observations, rerun <code>evaluate</code>; promotion still needs your approval.
            </li>
          </ul>
          {t.espn.excluded_runs.length > 0 && (
            <p className={styles.methodMeta}>
              Not counted as evidence: {t.espn.excluded_runs.map((r) => `week ${r.week} (${r.reason})`).join("; ")}.
            </p>
          )}
          {t.run && <p className={styles.methodMeta}>Status from run <span className="mono">{t.run.run_id}</span> (week {t.run.week}, {when(t.run.created_at)}).</p>}
          {t.espn.by_week.length === 0 ? <p className={styles.methodMeta}>No completed week has a pregame ESPN snapshot yet.</p> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Week</th><th className="num">Pregame ESPN + completed game</th><th className="num">Matched</th></tr></thead>
                <tbody>
                  {t.espn.by_week.map((w) => (
                    <tr key={w.week}><td>{w.week}</td><td className="num">{w.pregame_completed}</td><td className="num">{w.matched}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Method>
      </div>
    </Section>
  );
}
