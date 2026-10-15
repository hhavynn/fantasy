import { Badge, Pos } from "@/components/ui";
import { num, signed } from "@/lib/format";
import type { TradeVsFreeAgency as Tvf, TvfAcquisition, TvfDeltas, TvfSide } from "@/lib/trade-types";

// Trade vs. free agency (V3.1, analytics/trade_alternatives.py). What the trade itself adds, whether one available
// player gives a comparable improvement, and what the trade still adds once that move is made. Hypothetical: the
// canonical trade delta never includes any of it.

const why = "Incomplete ESPN projections (or a drop whose value is unknown): no canonical total";
const TYPE: Record<string, { text: string; tone: string }> = {
  free_agent: { text: "free agent", tone: "info" },
  waiver_claim: { text: "waiver claim · not guaranteed", tone: "warn" },
  unknown: { text: "availability type unknown", tone: "warn" },
};

function D({ v, k = "ros_delta" }: { v: TvfDeltas; k?: "ros_delta" | "regular_season_delta" | "playoff_delta" }) {
  const x = v[k];
  if (x === null) {
    return k === "ros_delta" && v.known_ros_delta !== null
      ? <span title={why}>incomplete ({signed(v.known_ros_delta)} known)</span>
      : <span className="missing" title={why}>—</span>;
  }
  return <>{signed(x)}</>;
}

function Who({ a }: { a: TvfAcquisition }) {
  if (!a.player) {
    return <span className="faint">{a.reason === "no_legal_acquisition" ? "no legal acquisition" : "none improves the lineup"}</span>;
  }
  const t = TYPE[a.acquisition_type ?? "unknown"] ?? TYPE.unknown;
  return (
    <>
      {a.player.name ?? `#${a.player.espn_player_id}`} <Pos value={a.player.position} /> <Badge value={t.text} tone={t.tone} title={a.acquisition_note} />
      {a.drops.length > 0 && <div className="faint" style={{ fontSize: 11 }}>drop {a.drops.map((d) => d.name ?? d.espn_player_id).join(", ")}
        {a.drop_cost_unknown ? " (value unknown)" : a.drop_lineup_cost !== null ? ` (lineup cost ${a.drop_lineup_cost.toFixed(1)})` : ""}</div>}
      {a.drops.length === 0 && <div className="faint" style={{ fontSize: 11 }}>open roster spot, no drop</div>}
    </>
  );
}

function SideSummary({ s }: { s: TvfSide }) {
  const c = s.comparison;
  const share = c.share_available_without_trade;
  const isInteracting =
    c.interaction_type === "positive_synergy" ||
    c.interaction_type === "negative_interaction" ||
    (share !== null && (share < 0 || share > 1));
  const interactionText =
    c.interaction_note ?? "These roster moves interact, so a simple percentage of replaceable gain would be misleading.";

  return (
    <div>
      <h3 style={{ margin: "4px 0" }}>{s.label === "You" ? "You" : s.label}</h3>
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))" }}>
        <div className="stat" title="L(trade roster) − L(current roster): the canonical delta">
          <div className="label">What the trade adds</div><div className="value"><D v={c.canonical_gain} /></div>
          <div className="faint" style={{ fontSize: 11 }}>reg <D v={c.canonical_gain} k="regular_season_delta" /> · playoffs <D v={c.canonical_gain} k="playoff_delta" /></div></div>
        <div className="stat" title="L(current roster + best available free agent − required drop) − L(current roster)">
          <div className="label">Best free-agent move instead</div><div className="value"><D v={c.free_agent_alternative_gain} /></div>
          <div style={{ fontSize: 12 }}><Who a={s.free_agent_alternative} /></div></div>
        <div className="stat" title="L(trade + the same free agent − drops) − L(current roster + that free agent − drop)">
          <div className="label">Trade beyond that move</div><div className="value"><D v={c.trade_advantage} /></div>
          <div className="faint" style={{ fontSize: 11 }}>
            {isInteracting ? (
              <span title={interactionText}>Moves interact; simple % misleading</span>
            ) : share !== null ? (
              `${Math.round(share * 100)}% of the trade's gain is available without trading`
            ) : (
              "share not defined"
            )}
          </div>
        </div>
      </div>
      {isInteracting && (
        <p className="faint" style={{ margin: "4px 0", fontSize: 11 }}>
          {interactionText} Free agency adds {signed(c.free_agent_alternative_gain.ros_delta)}, and the trade adds{" "}
          {signed(c.trade_advantage.ros_delta)} beyond it.
        </p>
      )}
      {s.statements.length > 0 && <ul className="reasons" style={{ marginTop: 6 }}>{s.statements.map((x, i) => <li key={i}>{x}</li>)}</ul>}
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

function Details({ t, sides }: { t: Extract<Tvf, { available: true }>; sides: TvfSide[] }) {
  return (
    <details style={{ marginTop: 6 }}>
      <summary>Formulas, alternatives by position, streaming frontier and assumptions</summary>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Projected starting points</th>{sides.map((s) => <th key={s.team_id} className="num">{s.label} ROS · reg · playoffs</th>)}</tr></thead>
          <tbody>
            {ROWS.map((row) => (
              <tr key={row.key}>
                <td className="wrap" title={t.formulas[row.key]}>{row.text}</td>
                {sides.map((s) => {
                  const v = s.comparison[row.key] as TvfDeltas;
                  return <td key={s.team_id} className="num"><b><D v={v} /></b> <span className="faint">· <D v={v} k="regular_season_delta" /> · <D v={v} k="playoff_delta" /></span></td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="reasons">
        {Object.entries(t.formulas).map(([k, f]) => <li key={k} className="faint"><code>{k}</code> = {f}</li>)}
        <li className="faint">{t.majority_rule}</li>
      </ul>
      {sides.map((s) => (
        <div key={s.team_id} style={{ marginTop: 6 }}>
          <b>{s.label}</b>
          <ul className="reasons">
            {s.position_alternatives.map((p) => (
              <li key={p.position}>Best available {p.position}: <Who a={p} /> <span className="faint">→ whole lineup <D v={p.gain} />{p.slot_delta !== null ? `, ${p.position} slot ${p.slot_delta > 0 ? "+" : ""}${p.slot_delta.toFixed(1)}` : ""}</span></li>
            ))}
            {s.waiver_claim_alternative && <li>Waiver claim: <Who a={s.waiver_claim_alternative} /> <span className="faint">→ <D v={s.waiver_claim_alternative.gain} /> if the claim succeeds</span></li>}
            {s.unknown_availability_alternative && <li>Unconfirmed availability: <Who a={s.unknown_availability_alternative} /> <span className="faint">→ <D v={s.unknown_availability_alternative.gain} /> if addable</span></li>}
            <li>After the trade, its own best free agent: <Who a={s.with_trade_alternative} /> <span className="faint">→ <D v={s.with_trade_alternative.gain} /></span></li>
            {s.trade_with_free_agent && (
              <li className="faint">
                {s.trade_with_free_agent.available === false ? (
                  <span>Trade + {s.free_agent_alternative.player?.name ?? "free agent"} cannot be legally combined: {s.trade_with_free_agent.reason?.replace(/_/g, " ")}.</span>
                ) : s.trade_with_free_agent.drops.length > 0 ? (
                  <span>Trade + {s.free_agent_alternative.player?.name} would drop {s.trade_with_free_agent.drops.map((d) => d.name ?? d.espn_player_id).join(", ")}.</span>
                ) : (
                  <span>Trade + {s.free_agent_alternative.player?.name} needs no additional drop.</span>
                )}
              </li>
            )}
            <li className="faint">Search: {s.free_agent_alternative.search.candidates} available players scored, {s.free_agent_alternative.search.solved} solved exactly ({s.free_agent_alternative.search.method}); {s.free_agent_alternative.search.excluded_unprojected} unprojected not scored.</li>
          </ul>
          {s.streaming_frontier.map((f) => (
            <div key={f.position} className="table-wrap">
              <p className="faint" style={{ margin: "4px 0" }}><b>{f.position} streaming frontier</b>: {f.no_trade.label}</p>
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
                            {num(w.starter_points)} <span className="faint" style={{ fontSize: 10 }}>({w.starter_slots.map((s) => (s !== null ? s.toFixed(1) : "—")).join(", ")})</span>
                          </span>
                        ) : (
                          num(w.starter_points)
                        )}
                      </td>
                      <td className="num">{signed(w.gain)}</td>
                      <td className="num">{signed(f.after_trade.weeks[i]?.gain)}</td>
                    </tr>
                  ))}
                  <tr><td colSpan={4} className="faint">Total (illustration only, never in a metric)</td><td className="num"><b>{signed(f.no_trade.total_gain)}</b></td><td className="num"><b>{signed(f.after_trade.total_gain)}</b></td></tr>
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ))}
      <ul className="reasons">{t.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
      <ul className="reasons">{t.limitations.map((a, i) => <li key={i} className="faint">{a}</li>)}</ul>
    </details>
  );
}

export function TradeVsFreeAgency({ t, only }: { t?: Tvf; only?: "team_a" }) {
  if (!t) return null;
  if (!t.available) return <p className="faint">Trade vs. free agency unavailable: {t.reason}</p>;
  const sides = only ? [t[only]] : [t.team_a, t.team_b];
  return (
    <section className="panel" style={only ? { background: "transparent", padding: 0, border: 0, marginTop: 8 } : undefined}>
      {only ? <b>Trade vs. free agency</b> : <h2>Trade vs. free agency</h2>}
      <p className="faint" style={{ margin: "2px 0 6px" }}>{t.note}{!t.consistent_with_canonical && <> <Badge value="canonical mismatch" tone="bad" /></>}</p>
      {t.contested && <div className="callout">{t.contested.note}</div>}
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(420px, 100%), 1fr))" }}>
        {sides.map((s) => <SideSummary key={s.team_id} s={s} />)}
      </div>
      <Details t={t} sides={only ? [t.team_a, t.team_b] : sides} />
    </section>
  );
}
