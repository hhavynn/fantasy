import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Method, Notice, Pos, Score, Section } from "@/components/ui";
import { date, int, num, pct, when } from "@/lib/format";
import { formatMetric } from "@/lib/managers";
import type { ScoutingReport as Report, ScoutingSignal } from "@/lib/manager-scouting";
import styles from "./scouting.module.css";

function Confidence({ value }: { value: string }) {
  return <span className={styles.confidence}>{value === "INSUFFICIENT" ? "Insufficient history" : value === "CURRENT_STATE" ? "Current state" : `${value.toLowerCase()} confidence`}</span>;
}

function SignalEvidence({ signal: s }: { signal: ScoutingSignal }) {
  return <details className={styles.rule}>
    <summary>n={s.sample_size} · <Confidence value={s.confidence} /></summary>
    <p>Minimum sample: {s.min_sample}. {s.rule}</p>
    {s.evidence_status !== "CONSISTENT" && s.stored_classification && <p>Stored label: {s.stored_classification.label}; confidence: {s.stored_classification.confidence}; sample: {s.stored_classification.sample_size}; computed {when(s.stored_classification.computed_at)}. It is withheld from the active report because its evidence is unavailable or inconsistent.</p>}
    <p>Updated {when(s.computed_at)}</p>
  </details>;
}

function Headline({ item: h }: { item: Report["headlines"][number] }) {
  return <li>
    <details className={styles.headline}>
      <summary><span>{h.text}</span><span className={styles.headlineMeta}>{h.category === "current" ? "Current state" : h.category === "trade_history" ? "Trade history" : "Historical tendency"} · <Confidence value={h.confidence} /></span></summary>
      <ul>{h.evidence.map((e) => <li key={e}>{e}</li>)}</ul>
    </details>
  </li>;
}

/** Overview tab: identity counters and the supported headline observations, each with its evidence. */
export function ScoutingSummary({ report: d }: { report: Report }) {
  const t = d.identity;
  const history = d.trade_history;
  return (
    <Section title="Scouting summary" footnote={<>Historical metrics updated {when(d.evidence.metrics_computed_at)}. Open an observation for its evidence; every one is limited to what the stored history supports.</>}>
      <dl className={styles.facts}>
        <div><dt>Acquisitions / drops</dt><dd>{int(t.acquisitions)} / {int(t.drops)} <span>ESPN counters</span></dd></div>
        <div><dt>Observed adds / drops</dt><dd>{int(t.observed_adds)} / {int(t.observed_drops)} <span>metric history</span></dd></div>
        <div><dt>Completed trades</dt><dd>{history.completed_trades} <span>deduplicated</span></dd></div>
        <div><dt>{t.is_faab ? "FAAB remaining" : "Waiver position"}</dt><dd>{t.is_faab ? <>{num(t.faab_remaining, 0)} / {num(t.faab_budget, 0)} <span>spent {num(t.faab_spent, 0)}</span></> : int(t.waiver_rank)}</dd></div>
      </dl>
      {(!d.evidence.metrics_available || !d.evidence.classifications_available) && (
        <Notice tone="caution">Historical metrics or classifications are unavailable. Sync and run analytics to populate tendencies; missing history remains insufficient.</Notice>
      )}
      {d.headlines.length === 0
        ? <p className={styles.note}>No observation is supported by enough history yet.</p>
        : <ul className={styles.headlines}>{d.headlines.slice(0, 4).map((h) => <Headline key={h.text} item={h} />)}</ul>}
      {d.headlines.length > 4 && <details className={styles.more}><summary>{d.headlines.length - 4} more supported observations</summary>
        <ul className={styles.headlines}>{d.headlines.slice(4).map((h) => <Headline key={h.text} item={h} />)}</ul>
      </details>}
    </Section>
  );
}

/** Scouting tab: the evidence tables behind the summary. */
export function ScoutingEvidence({ report: d }: { report: Report }) {
  const t = d.identity;
  const history = d.trade_history;
  const c = d.current_roster;
  return (
    <>
      <Section title="Current roster construction" flush footnote={<>Week {c.scoring_period} snapshot · {when(c.synced_at)} · {c.league_team_samples} teams observed. Counts describe current state. {c.note}{(c.unknown_position_count ?? 0) > 0 ? ` ${c.unknown_position_count} players have unknown positions.` : ""}</>}>
        {!c.available && <div className={styles.padded}><Notice tone="caution">No current-week roster snapshot. Counts are unknown; earlier rosters are available in the weekly history on the overview.</Notice></div>}
        <div className={styles.table} tabIndex={0} role="region" aria-label="Current roster counts">
          <table><thead><tr><th scope="col">Position</th>{c.positions.map((p) => <th scope="col" key={p.position} className="num">{p.position}</th>)}</tr></thead>
            <tbody>
              <tr><th scope="row">Rostered</th>{c.positions.map((p) => <td key={p.position} className="num">{int(p.count)}</td>)}</tr>
              <tr><th scope="row">League average</th>{c.positions.map((p) => <td key={p.position} className="num">{num(p.league_average, 1)}</td>)}</tr>
              <tr><th scope="row">Compared with league</th>{c.positions.map((p) => <td key={p.position} className="num">{{ ABOVE: "Above", BELOW: "Below", AT: "At average", UNKNOWN: "Unknown" }[p.league_comparison]}</td>)}</tr>
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Position tendencies" flush footnote={<>Historical acquisitions and weekly roster averages, with current roster and need shown separately. A high add share does not imply a current shortage. {d.evidence.position_ratio_rule} League shares are weighted by observed add counts.</>}>
        <div className={styles.table} tabIndex={0} role="region" aria-label="Historical position tendencies">
          <table><thead><tr><th scope="col">Position</th><th scope="col">Acquisition tendency</th><th scope="col" className="num">Adds / known</th><th scope="col" className="num">Share / league</th><th scope="col" className="num">Ratio</th><th scope="col" className="num">Avg roster / weeks</th><th scope="col" className="num">Current roster</th><th scope="col" className="num">Current need</th><th scope="col">Evidence / confidence</th></tr></thead>
            <tbody>{d.position_affinity.map((p) => <tr key={p.position}>
              <th scope="row"><Pos value={p.position} /></th><td><Badge value={p.classification} /></td>
              <td className="num">{int(p.add_count)} / {int(p.denominator)}</td><td className="num">{pct(p.add_share)} / {pct(p.league_add_share)}</td>
              <td className="num">{p.relative_ratio === null ? <span title={p.ratio_rule}>Insufficient benchmark</span> : `${p.relative_ratio.toFixed(1)}×`}</td>
              <td className="num">{num(p.average_rostered)} / {p.roster_history_sample}</td><td className="num">{int(p.current_rostered)}</td>
              <td className="num">{pct(p.current_need_score)}</td><td><SignalEvidence signal={p} /></td>
            </tr>)}</tbody>
          </table>
        </div>
      </Section>

      <Section title="Waiver behavior" flush footnote={<>Patience measures completed holds only. Quick drops use adds old enough to judge. Rookie and injury measures depend on available player/context coverage.{t.is_faab ? " FAAB spending is the share of the season budget spent." : " This league uses waiver priority; no FAAB spending classification applies."}</>}>
        <div className={styles.table} tabIndex={0} role="region" aria-label="Waiver behavior evidence">
          <table><thead><tr><th scope="col">Measure</th><th scope="col">Classification</th><th scope="col" className="num">Raw value</th><th scope="col" className="num">Numerator / denominator</th><th scope="col" className="num">League mean</th><th scope="col">Sample / confidence / rule</th></tr></thead>
            <tbody>{d.waiver_style.map((s) => <tr key={s.dimension}>
              <th scope="row">{s.title}</th><td><Badge value={s.classification} /></td><td className="num">{formatMetric(s.metric, s.value)}</td>
              <td className="num">{num(s.numerator)} / {num(s.denominator)}</td><td className="num">{formatMetric(s.metric, s.league_mean)}</td><td><SignalEvidence signal={s} /></td>
            </tr>)}</tbody>
          </table>
        </div>
      </Section>

      <Section title="Streaming behavior" flush footnote={d.evidence.streaming_definition}>
        <div className={styles.table} tabIndex={0} role="region" aria-label="Starter rotation evidence">
          <table><thead><tr><th scope="col">Position</th><th scope="col">Classification</th><th scope="col" className="num">Distinct starters</th><th scope="col" className="num">Changed / transitions</th><th scope="col" className="num">Turnover</th><th scope="col" className="num">Pickups</th><th scope="col">Evidence / confidence</th></tr></thead>
            <tbody>{d.streaming_style.map((s) => <tr key={s.position}>
              <th scope="row"><Pos value={s.position} /></th><td><Badge value={s.classification} /></td><td className="num">{int(s.distinct_starters)}</td>
              <td className="num">{int(s.turnover_changes)} / {int(s.turnover_transitions)}</td><td className="num">{pct(s.starter_turnover)}</td><td className="num">{int(s.pickups)}</td><td><SignalEvidence signal={s} /></td>
            </tr>)}</tbody>
          </table>
        </div>
      </Section>

      <Section title="Current positional needs" flush={d.current_needs.length > 0} footnote="Stored need scores, highest first. Starter quality uses the existing value score scale. No need formula is recalculated here.">
        {d.current_needs.length === 0 ? <Notice tone="info">No stored positional needs. Run analytics after syncing the current roster.</Notice> :
          <div className={styles.table} tabIndex={0} role="region" aria-label="Current positional need evidence"><table>
            <thead><tr><th scope="col">Position</th><th scope="col">Need / 100</th><th scope="col" className="num">Rostered</th><th scope="col" className="num">Healthy</th><th scope="col" className="num">Effective available</th><th scope="col" className="num">Starter quality / league</th><th scope="col">Source / rule</th></tr></thead>
            <tbody>{d.current_needs.map((n) => <tr key={n.position}>
              <th scope="row"><Pos value={n.position} /></th><td><Score value={n.need_score === null ? null : n.need_score * 100} max={100} /></td>
              <td className="num">{int(n.rostered)}</td><td className="num">{int(n.healthy)}</td><td className="num">{num(n.effective_available, 2)}</td>
              <td className="num">{num(n.starter_value, 2)} / {num(n.league_starter_average, 2)}</td>
              <td><details className={styles.rule}><summary>Stored analytics · {when(n.computed_at)}</summary><p>{JSON.stringify(n.details)}</p></details></td>
            </tr>)}</tbody>
          </table></div>}
      </Section>

      <Section title="Trade history" flush={history.recent_trades.length > 0} footnote={<>{history.confidence_rule} {history.note}{history.completed_trades > 10 ? " Showing the 10 most recent completed trades; totals include all observed trades." : ""}</>}>
        <div className={history.recent_trades.length > 0 ? styles.padded : undefined}>
          <p className={styles.tradeLine}><b>{history.completed_trades} completed</b> · {history.players_sent} players sent · {history.players_received} received · {history.uneven_player_count_trades} with unequal player counts</p>
          <p className={styles.tradeLine}>Trade behavior: <b>unknown</b> · <Confidence value={history.trade_behavior_confidence} /></p>
          {history.recent_trades.length === 0 && <p className={styles.note}>No completed trades observed. There is insufficient evidence to infer a trade preference.</p>}
        </div>
        {history.recent_trades.length > 0 && (
          <div className={styles.table} tabIndex={0} role="region" aria-label="Recent completed trades"><table>
            <thead><tr><th scope="col">Date</th><th scope="col">Players sent</th><th scope="col">Players received</th><th scope="col">Source</th></tr></thead>
            <tbody>{history.recent_trades.map((tr) => <tr key={tr.trade_id}>
              <td>{date(tr.date)}</td>
              <td className={styles.playerList}>{tr.players_sent.map((p) => <span key={p.espn_player_id}><PlayerLink id={p.espn_player_id} name={p.name ?? `Player ${p.espn_player_id}`} /> → team {p.to_team_id ?? "unknown"}</span>)}</td>
              <td className={styles.playerList}>{tr.players_received.map((p) => <span key={p.espn_player_id}><PlayerLink id={p.espn_player_id} name={p.name ?? `Player ${p.espn_player_id}`} /> ← team {p.from_team_id ?? "unknown"}</span>)}</td>
              <td className={styles.source}>{tr.sources.join(" + ")}<details><summary>Source ID</summary>{tr.trade_id}</details></td>
            </tr>)}</tbody>
          </table></div>
        )}
      </Section>

      <Method title="Evidence and confidence guide">
        <p>HIGH / MEDIUM / LOW describe a measured tendency. Confidence describes sample sufficiency. INSUFFICIENT means history cannot support a classification; it does not mean LOW.</p>
        <p>Historical labels retain the stored engine rules. Trade-history confidence uses completed trade count, separately from the legacy trade-activity classification in the raw data on the overview.</p>
        <ul>{d.evidence.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      </Method>
    </>
  );
}
