import { Badge, Delta, Method, Notice, Pos, Section, Stat, StatStrip } from "@/components/ui";
import { cx } from "@/lib/cx";
import { num, signed } from "@/lib/format";
import { projectionWord } from "@/lib/labels";
import { signedText } from "@/lib/trade-format";
import type { TradeVsFreeAgency as Tvf, TvfAcquisition, TvfDeltas, TvfSide } from "@/lib/trade-types";
import styles from "./TradeVsFreeAgency.module.css";

// Trade vs. free agency (V3.1, analytics/trade_alternatives.py). What the trade itself adds, whether one available
// player gives a comparable improvement, and what the trade still adds once that move is made. Hypothetical: the
// canonical trade delta never includes any of it.

/** `proj` names the projections the evaluation ran on: "ESPN", or "Ours" for a league whose provider has none. */
const whyFor = (proj: string) => `Incomplete ${projectionWord(proj)}projections (or a drop whose value is unknown): no canonical total`;
const TYPE: Record<string, { text: string; tone: string }> = {
  free_agent: { text: "free agent", tone: "info" },
  waiver_claim: { text: "waiver claim · not guaranteed", tone: "warn" },
  unknown: { text: "availability type unknown", tone: "warn" },
};

type DeltaKey = "ros_delta" | "regular_season_delta" | "playoff_delta";

/** A delta as coloured text (tables and sentences). */
function D({ v, k = "ros_delta", why }: { v: TvfDeltas; k?: DeltaKey; why: string }) {
  const x = v[k];
  if (x === null) {
    return k === "ros_delta" && v.known_ros_delta !== null
      ? <span title={why}>incomplete ({signedText(v.known_ros_delta)} known)</span>
      : <span className="missing" title={why}>—</span>;
  }
  return <>{signed(x)}</>;
}

/** A delta as a headline number (stat strip). */
function BigDelta({ v, why }: { v: TvfDeltas; why: string }) {
  return v.ros_delta === null ? <D v={v} why={why} /> : <Delta value={v.ros_delta} />;
}

function Who({ a }: { a: TvfAcquisition }) {
  if (!a.player) {
    return <span className="faint">{a.reason === "no_legal_acquisition" ? "no legal acquisition" : "none improves the lineup"}</span>;
  }
  const t = TYPE[a.acquisition_type ?? "unknown"] ?? TYPE.unknown;
  return (
    <>
      {a.player.name ?? `#${a.player.espn_player_id}`} <Pos value={a.player.position} /> <Badge value={t.text} tone={t.tone} title={a.acquisition_note} />
      {a.drops.length > 0 && <span className={styles.sub}>drop {a.drops.map((d) => d.name ?? d.espn_player_id).join(", ")}
        {a.drop_cost_unknown ? " (value unknown)" : a.drop_lineup_cost !== null ? ` (lineup cost ${a.drop_lineup_cost.toFixed(1)})` : ""}</span>}
      {a.drops.length === 0 && <span className={styles.sub}>open roster spot, no drop</span>}
    </>
  );
}

function SideSummary({ s, why }: { s: TvfSide; why: string }) {
  const c = s.comparison;
  const share = c.share_available_without_trade;
  const isInteracting =
    c.interaction_type === "positive_synergy" ||
    c.interaction_type === "negative_interaction" ||
    (share !== null && (share < 0 || share > 1));
  const interactionText =
    c.interaction_note ?? "These roster moves interact, so a simple percentage of replaceable gain would be misleading.";

  return (
    <div className={styles.side}>
      <h3 className={styles.sideTitle}>{s.label}</h3>
      <StatStrip>
        <Stat
          label={<span title="L(trade roster) − L(current roster): the canonical delta">What the trade adds</span>}
          value={<BigDelta why={why} v={c.canonical_gain} />}
          note={<>reg <D why={why} v={c.canonical_gain} k="regular_season_delta" /> · playoffs <D why={why} v={c.canonical_gain} k="playoff_delta" /></>}
        />
        <Stat
          label={<span title="L(current roster + best available free agent − required drop) − L(current roster)">Best free-agent move instead</span>}
          value={<BigDelta why={why} v={c.free_agent_alternative_gain} />}
          note={<Who a={s.free_agent_alternative} />}
        />
        <Stat
          label={<span title="L(trade + the same free agent − drops) − L(current roster + that free agent − drop)">Trade beyond that move</span>}
          value={<BigDelta why={why} v={c.trade_advantage} />}
          note={isInteracting
            ? <span title={interactionText}>Moves interact; simple % misleading</span>
            : share !== null ? `${Math.round(share * 100)}% of the trade's gain is available without trading` : "share not defined"}
        />
      </StatStrip>
      {isInteracting && (
        <p className={styles.interact}>
          {interactionText} Free agency adds {signed(c.free_agent_alternative_gain.ros_delta)}, and the trade adds{" "}
          {signed(c.trade_advantage.ros_delta)} beyond it.
        </p>
      )}
      {s.statements.length > 0 && <ul className={styles.statements}>{s.statements.map((x, i) => <li key={i}>{x}</li>)}</ul>}
    </div>
  );
}

const ROWS: { key: keyof TvfSide["comparison"]; text: string }[] = [
  { key: "canonical_gain", text: "Trade (canonical delta)" },
  { key: "free_agent_alternative_gain", text: "No trade + best free agent" },
  { key: "trade_advantage", text: "Trade beyond that free-agent move" },
  { key: "available_without_trade", text: "Part of the trade's gain available without trading" },
  { key: "trade_instead_of_free_agent", text: "Trade instead of the free-agent move (diagnostic)" },
  { key: "trade_plus_best_free_agent_gain", text: "Trade + its own best free agent (diagnostic)" },
  { key: "trade_advantage_if_both_add", text: "Both worlds take their own best free agent (diagnostic)" },
];

function Details({ t, sides, why }: { t: Extract<Tvf, { available: true }>; sides: TvfSide[]; why: string }) {
  return (
    <Method title="Formulas, alternatives by position, streaming frontier and assumptions" className={styles.details}>
      <div className={cx("table-wrap", styles.formulaTable)}>
        <table>
          <thead>
            <tr>
              <th>Projected starting points</th>
              {sides.map((s) => <th key={s.team_id} className="num">{s.label} ROS · reg · playoffs</th>)}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => (
              <tr key={row.key}>
                <td className="wrap" title={t.formulas[row.key]}>{row.text}</td>
                {sides.map((s) => {
                  const v = s.comparison[row.key] as TvfDeltas;
                  return <td key={s.team_id} className="num"><b><D why={why} v={v} /></b> <span className="faint">· <D why={why} v={v} k="regular_season_delta" /> · <D why={why} v={v} k="playoff_delta" /></span></td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className={styles.list}>
        {Object.entries(t.formulas).map(([k, f]) => <li key={k} className={styles.faintItem}><code>{k}</code> = {f}</li>)}
        <li className={styles.faintItem}>{t.majority_rule}</li>
      </ul>
      {sides.map((s) => (
        <div key={s.team_id} className={styles.block}>
          <b>{s.label}</b>
          <ul className={styles.list}>
            {s.position_alternatives.map((p) => (
              <li key={p.position}>Best available {p.position}: <Who a={p} /> <span className="faint">→ whole lineup <D why={why} v={p.gain} />{p.slot_delta !== null ? `, ${p.position} slot ${p.slot_delta > 0 ? "+" : ""}${p.slot_delta.toFixed(1)}` : ""}</span></li>
            ))}
            {s.waiver_claim_alternative && <li>Waiver claim: <Who a={s.waiver_claim_alternative} /> <span className="faint">→ <D why={why} v={s.waiver_claim_alternative.gain} /> if the claim succeeds</span></li>}
            {s.unknown_availability_alternative && <li>Unconfirmed availability: <Who a={s.unknown_availability_alternative} /> <span className="faint">→ <D why={why} v={s.unknown_availability_alternative.gain} /> if addable</span></li>}
            <li>After the trade, its own best free agent: <Who a={s.with_trade_alternative} /> <span className="faint">→ <D why={why} v={s.with_trade_alternative.gain} /></span></li>
            {s.trade_with_free_agent && (
              <li className={styles.faintItem}>
                {s.trade_with_free_agent.available === false ? (
                  <span>Trade + {s.free_agent_alternative.player?.name ?? "free agent"} cannot be legally combined: {s.trade_with_free_agent.reason?.replace(/_/g, " ")}.</span>
                ) : s.trade_with_free_agent.drops.length > 0 ? (
                  <span>Trade + {s.free_agent_alternative.player?.name} would drop {s.trade_with_free_agent.drops.map((d) => d.name ?? d.espn_player_id).join(", ")}.</span>
                ) : (
                  <span>Trade + {s.free_agent_alternative.player?.name} needs no additional drop.</span>
                )}
              </li>
            )}
            <li className={styles.faintItem}>Search: {s.free_agent_alternative.search.candidates} available players scored, {s.free_agent_alternative.search.solved} solved exactly ({s.free_agent_alternative.search.method}); {s.free_agent_alternative.search.excluded_unprojected} unprojected not scored.</li>
          </ul>
          {s.streaming_frontier.map((f) => (
            <div key={f.position} className="table-wrap">
              <p className={styles.frontierNote}><b>{f.position} streaming frontier</b>: {f.no_trade.label}</p>
              <table>
                <thead>
                  <tr>
                    <th>Wk</th>
                    <th>Best available {f.position}</th>
                    <th className="num">Pts</th>
                    <th className="num">{f.no_trade.slot_count && f.no_trade.slot_count > 1 ? `Starters (${f.no_trade.slot_count})` : "Your starter now"}</th>
                    <th className="num">Gain (no trade)</th>
                    <th className="num">Gain (after trade)</th>
                  </tr>
                </thead>
                <tbody>
                  {f.no_trade.weeks.map((w, i) => (
                    <tr key={w.week}>
                      <td>{w.week}</td>
                      <td>{w.best_available.length > 0 ? w.best_available.map((b) => b.name ?? `#${b.espn_player_id}`).join(", ") : <span className="faint">none projected</span>}</td>
                      <td className="num">{w.best_available.length > 1 ? `${w.best_available.map((b) => num(b.points)).join(" + ")} (${num(w.best_available.reduce((sum, b) => sum + b.points, 0))})` : num(w.best_available[0]?.points)}</td>
                      <td className="num">
                        {w.starter_slots && w.starter_slots.length > 1 ? (
                          <span>
                            {num(w.starter_points)} <span className={styles.starters}>({w.starter_slots.map((x) => (x !== null ? x.toFixed(1) : "—")).join(", ")})</span>
                          </span>
                        ) : (
                          num(w.starter_points)
                        )}
                      </td>
                      <td className="num">{signed(w.gain)}</td>
                      <td className="num">{signed(f.after_trade.weeks[i]?.gain)}</td>
                    </tr>
                  ))}
                  <tr className={styles.totalRow}>
                    <td colSpan={4}>Total (illustration only, never in a metric)</td>
                    <td className="num"><b>{signed(f.no_trade.total_gain)}</b></td>
                    <td className="num"><b>{signed(f.after_trade.total_gain)}</b></td>
                  </tr>
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ))}
      <ul className={styles.list}>{t.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
      <ul className={cx(styles.list, styles.faintItem)}>{t.limitations.map((a, i) => <li key={i}>{a}</li>)}</ul>
    </Method>
  );
}

export function TradeVsFreeAgency({ t, only, projLabel = "ESPN" }: { t?: Tvf; only?: "team_a"; projLabel?: string }) {
  if (!t) return null;
  const why = whyFor(projLabel);
  if (!t.available) return <p className="faint">Trade vs. free agency unavailable: {t.reason}</p>;
  const sides = only ? [t[only]] : [t.team_a, t.team_b];
  const body = (
    <>
      <p className={styles.note}>{t.note}{!t.consistent_with_canonical && <> <Badge value="canonical mismatch" tone="bad" /></>}</p>
      {t.contested && <Notice tone="caution" className={styles.alert}>{t.contested.note}</Notice>}
      <div className={styles.sides}>
        {sides.map((s) => <SideSummary key={s.team_id} s={s} why={why} />)}
      </div>
      <Details t={t} sides={only ? [t.team_a, t.team_b] : sides} why={why} />
    </>
  );
  if (only) {
    return (
      <div className={styles.inline}>
        <p className={styles.inlineTitle}>Trade vs. free agency</p>
        {body}
      </div>
    );
  }
  return <Section title="Trade vs. free agency">{body}</Section>;
}
