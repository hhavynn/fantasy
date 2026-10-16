import type { ReactNode } from "react";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Delta, Pos } from "@/components/ui";
import {
  checkStatusInfo, favoursText, parText, playerLabel, ratingFields, ratioText, roleChangeText, weeklyChangeRow, weeksText,
} from "@/lib/trade-dual-value";
import type { DName, DRatings, DVBestAsset, DVDepth, DVLineupSide, DVPlayerAsset, DualValue } from "@/lib/trade-types";
import styles from "./discover.module.css";

// The dual-value evidence blocks of a trade's expandable panel (docs/trade_discovery_dual_value.md): the full plausibility
// checks, the asset table, the depth cost, the 0-100 ratings and the weekly lineup changes for both teams.
// Missing data is a dash with a reason, never 0.

type Names = Record<string, DName | undefined> | null | undefined;

const SIDE: Record<string, string> = { You: "Team gives → partner receives", They: "Partner gives → team receives" };

export function PlausibilityChecks({ dv }: { dv: DualValue }) {
  const { checks, criteria } = dv.plausibility;
  return (
    <section className={styles.block} aria-label="Plausibility checks">
      <h4>Plausibility checks</h4>
      <ul className={styles.checkList}>
        {checks.map((c) => {
          const s = checkStatusInfo(c.status);
          return (
            <li key={c.criterion} title={criteria?.[c.criterion]}>
              <span className={`${styles.chip} ${s.tone ? styles[`chip_${s.tone}`] : ""}`}>
                <span aria-hidden="true" className={styles.chipGlyph}>{s.glyph}</span>{s.label}
              </span>
              <span className={styles.checkText}>
                <b>{c.criterion.replaceAll("_", " ").replace(/^\w/, (x) => x.toUpperCase())}.</b> {c.text ?? criteria?.[c.criterion]}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// ----------------------------------------------------------------------------------------- assets --

function BestAsset({ label, a }: { label: string; a: DVBestAsset | null }) {
  return (
    <p className={styles.small}>
      <b>{label}:</b>{" "}
      {a ? <>{a.name ?? `#${a.espn_player_id}`} <Pos value={a.position} /> · {parText(a.par)} above replacement
        {a.position_rank ? <> · {a.position}{a.position_rank}{a.starter_caliber ? ", starter-caliber" : ""}</> : null}</>
        : <span className="missing" title="Not shown: a player's value is unknown (missing projections), so no best asset is named">—</span>}
    </p>
  );
}

function AssetRow({ p }: { p: DVPlayerAsset }) {
  const r = roleChangeText(p);
  const gaps = [
    p.missing_weeks?.length ? `no ESPN projection in ${weeksText(p.missing_weeks)}` : "",
    p.nfl_ineligible_weeks?.length ? `NFL-ineligible ${weeksText(p.nfl_ineligible_weeks)}, not counted` : "",
  ].filter(Boolean);
  return (
    <tr>
      <td data-label="Player"><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /></td>
      <td className="faint" data-label="Moves">{SIDE[p.from_side] ?? `${p.from_side} → ${p.to_side}`}</td>
      <td className="num" data-label="Points above replacement">
        {p.par === null
          ? <span title={`Missing ESPN projections: ${parText(p.known_par)} known`}>incomplete ({parText(p.known_par)} known)</span>
          : parText(p.par)}
      </td>
      <td data-label="Position rank">
        {p.position_rank
          ? <>{p.position}{p.position_rank} of {p.position_rank_of}{" "}
            {p.starter_caliber && <Badge value="starter-caliber" tone="info" title={`Within the league's ${p.league_starters_at_position} starters at ${p.position}`} />}</>
          : <span className="missing" title="No positional rank (value unknown)">—</span>}
      </td>
      <td data-label="Role (before → after)" title="Role for the sending team before the trade, and for the receiving team after it">
        {r.roles}<span className={styles.subline}>{r.starts}</span>
      </td>
      <td className="faint" data-label="Notes">
        {p.bench_asset && (
          <span title="Positive points above replacement, but he starts fewer than half the weeks for the receiver">
            Bench asset: keeps his asset value as depth, injury cover and trade currency; never counted as starting points.{" "}
          </span>
        )}
        {gaps.join(" · ")}
      </td>
    </tr>
  );
}

export function AssetEvidence({ dv }: { dv: DualValue }) {
  const a = dv.asset;
  const fav = favoursText(a.favours);
  const rows = [...a.you_send, ...a.you_receive];
  return (
    <section className={styles.block} aria-label="Asset value">
      <h4>Asset value</h4>
      <p className={styles.small}>
        {a.text}: gives {parText(a.you_send_par)} · gets {parText(a.they_send_par)} points above replacement
        (the smaller package is {ratioText(a.ratio)} of the larger){fav ? <> · <b>{fav}</b></> : null}.
        {" "}{a.note}
      </p>
      <table className={styles.evTable}>
        <thead>
          <tr>
            <th>Player</th><th>Moves</th>
            <th className="num" title="ESPN points above the best available free agent at his position, over his NFL-eligible weeks">Pts above repl.</th>
            <th>Position rank</th><th>Role (before → after)</th><th>Notes</th>
          </tr>
        </thead>
        <tbody>{rows.map((p) => <AssetRow key={`${p.from_side}-${p.espn_player_id}`} p={p} />)}</tbody>
      </table>
      <div className={styles.pair}>
        <BestAsset label="Best asset you surrender" a={a.best_asset_surrendered.you} />
        <BestAsset label="Best asset they surrender" a={a.best_asset_surrendered.them} />
      </div>
      {a.caveats.length > 0 && <ul className={styles.list}>{a.caveats.map((c, i) => <li key={i} className="faint">{c}</li>)}</ul>}
    </section>
  );
}

// ------------------------------------------------------------------------------------------ depth --

function dropsText(d: DVDepth) {
  if (d.forced_drops.length === 0) return <span className="faint">none</span>;
  return (
    <>
      {d.forced_drops.map((x, i) => (
        <span key={x.espn_player_id} className={styles.nowrap}>{i > 0 && "; "}{x.name ?? `#${x.espn_player_id}`} <Pos value={x.position} /> ·{" "}
          {x.par === null ? <span className="missing" title="Asset value unknown">value unknown</span> : <>{parText(x.par)} above replacement</>}
          {x.starter_caliber ? ", starter-caliber" : ""}</span>
      ))}
    </>
  );
}

function net(n: number) {
  return n === 0 ? "0" : `${n > 0 ? "+" : "−"}${Math.abs(n)}`;
}

export function DepthEvidence({ dv }: { dv: DualValue }) {
  const { you, them } = dv.asset.depth;
  const sides: [string, DVDepth][] = [["You", you], ["They", them]];
  const lines: { label: string; title?: string; cell: (d: DVDepth) => ReactNode }[] = [
    { label: "Net players", cell: (d) => `${net(d.net_players)} · ${d.kind}` },
    { label: "Forced drops (asset value)", cell: dropsText },
    { label: "Non-starter value sent", title: "Points above replacement in the players he sends who were not regular starters for him",
      cell: (d) => d.outgoing_non_starter_par === null ? <span className="missing" title="A sent player's value is unknown">—</span> : `${parText(d.outgoing_non_starter_par)} above replacement` },
    { label: "Empty starting slots", cell: (d) => d.empty_slots_after.length === 0 ? <span className="faint">none</span>
      : d.empty_slots_after.map((h) => `wk ${h.week} ${h.slot}`).join(", ") },
    { label: "Open roster spots after", cell: (d) => d.open_roster_spots_after === null ? <span className="missing" title="Not reported">—</span> : String(d.open_roster_spots_after) },
  ];
  return (
    <section className={styles.block} aria-label="Depth cost">
      <h4>Depth</h4>
      <table className={styles.evTable}>
        <thead><tr><th>Measure</th>{sides.map(([k]) => <th key={k}>{k}</th>)}</tr></thead>
        <tbody>
          {lines.map((l) => (
            <tr key={l.label}>
              <td data-label="Measure" title={l.title}>{l.label}</td>
              {sides.map(([k, d]) => <td key={k} data-label={k} className={styles.wrapText}>{l.cell(d)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// --------------------------------------------------------------------------------------- ratings --

export function RatingsEvidence({ ratings, sends, receives, names }: {
  ratings: DRatings | null | undefined; sends: number[]; receives: number[]; names: Names;
}) {
  if (!ratings) return null;
  const note = "Supporting context only: position-relative 0–100 ratings of recent opportunity and production. Not a price: never summed, and not comparable across positions.";
  if (!ratings.available) {
    return (
      <section className={styles.block} aria-label="Ratings">
        <h4>0–100 ratings</h4>
        <p className={`${styles.small} faint`}>Not available{ratings.reason ? `: ${ratings.reason}` : ""}. {note}</p>
      </section>
    );
  }
  const rows = [...sends.map((id) => ({ id, side: "gives" })), ...receives.map((id) => ({ id, side: "receives" }))];
  return (
    <section className={styles.block} aria-label="Ratings">
      <h4>0–100 ratings</h4>
      <p className={`${styles.small} faint`}>{note}{ratings.reason ? ` (${ratings.reason})` : ""}</p>
      <table className={styles.evTable}>
        <thead>
          <tr><th>Player</th><th>Moves</th>{ratingFields(null).map((f) => <th key={f.key} className="num">{f.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map(({ id, side }) => {
            const r = ratings.players[String(id)];
            const pos = r?.position ?? names?.[String(id)]?.position ?? null;
            return (
              <tr key={`${side}-${id}`}>
                <td data-label="Player"><PlayerLink id={id} name={names?.[String(id)]?.name ?? null} /> <Pos value={pos} /></td>
                <td className="faint" data-label="Moves">{side}</td>
                {ratingFields(r).map((f) => (
                  <td key={f.key} className="num" data-label={f.label}>
                    {f.text === "—" ? <span className="missing" title="No stored 0–100 rating for this player (never shown as 0)">—</span> : f.text}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
      {ratings.comparable_pairs.length > 0 ? (
        <p className={styles.small}>
          Same-position pairs: {ratings.comparable_pairs.map((p) => `${playerLabel(p.you_send, names)} ↔ ${playerLabel(p.you_receive, names)} (${p.position ?? "same pool"})`).join("; ")}.
        </p>
      ) : <p className={`${styles.small} faint`}>No pair of traded players shares a position and rating pool, so none is compared.</p>}
    </section>
  );
}

// ---------------------------------------------------------------------------------- weekly changes --

function PlayerChanges({ label, items }: { label: string; items: { espn_player_id: number; name: string | null; position: string | null; weeks: number[]; incoming?: boolean }[] }) {
  if (items.length === 0) return null;
  return (
    <p className={styles.small}>
      <b>{label}:</b>{" "}
      {items.map((x, i) => (
        <span key={`${x.espn_player_id}-${i}`}>{i > 0 && "; "}{x.name ?? `#${x.espn_player_id}`} <Pos value={x.position} /> {weeksText(x.weeks)}</span>
      ))}
    </p>
  );
}

function SideWeekly({ side, names, title }: { side: DVLineupSide; names: Names; title: string }) {
  const weeks = side.weekly.map((w) => weeklyChangeRow(w, names));
  return (
    <div>
      <h4>{title}</h4>
      <ol className={styles.weeks}>
        {weeks.map((w) => (
          <li key={w.week}>
            <span className={styles.weekHead}>
              <b>Week {w.week}</b> <span className="faint">{w.phase}</span>{" "}
              {w.delta === null
                ? <span className="missing" title="Missing ESPN projections: no complete total for this week (never treated as 0)">—</span>
                : <Delta value={w.delta} />}
            </span>
            {w.changed ? (
              <span className={styles.weekMoves}>
                {w.entered.length > 0 && <span><span className="up" aria-hidden="true">▲ </span><b>In:</b> {w.entered.join(", ")}</span>}
                {w.exited.length > 0 && <span><span className="down" aria-hidden="true">▼ </span><b>Out:</b> {w.exited.join(", ")}</span>}
              </span>
            ) : <span className="faint">No change to the starting lineup</span>}
          </li>
        ))}
      </ol>
      <PlayerChanges label="Starts more" items={side.promoted} />
      <PlayerChanges label="Starts less" items={side.displaced} />
      {side.forced_drops.length > 0 && (
        <p className={styles.small}><b>Forced drops:</b> {side.forced_drops.map((d) => d.name ?? `#${d.espn_player_id}`).join(", ")}
          {side.forced_drop_lineup_cost !== null && <> · lineup cost {parText(side.forced_drop_lineup_cost)}</>}</p>
      )}
      {side.missing.weeks_incomplete > 0 && (
        <p className={`${styles.small} faint`}>
          {side.missing.weeks_incomplete} {side.missing.weeks_incomplete === 1 ? "week has" : "weeks have"} missing ESPN projections ({weeksText(side.missing.weeks)}); those weeks are not counted as 0.
        </p>
      )}
    </div>
  );
}

export function WeeklyEvidence({ dv, names }: { dv: DualValue; names: Names }) {
  const { you, them, basis } = dv.lineup;
  return (
    <section className={styles.block} aria-label="Weekly lineup changes">
      <h4>Weekly lineup changes</h4>
      <p className={`${styles.small} faint`}>{basis}</p>
      <div className={styles.cols}>
        <SideWeekly side={you} names={names} title={`${you.team_name ?? "Your team"} (you)`} />
        <SideWeekly side={them} names={names} title={them.team_name ?? "Their team"} />
      </div>
    </section>
  );
}
