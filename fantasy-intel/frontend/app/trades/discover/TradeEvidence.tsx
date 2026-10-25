"use client";

import { TeamLink } from "@/components/TeamLink";
import { useRef, useState } from "react";
import { PlayerLink } from "@/components/PlayerLink";
import { TradeVsFreeAgency } from "@/components/TradeVsFreeAgency";
import { Badge, Notice, Pos } from "@/components/ui";
import { useLeagueId } from "@/components/useAppPathname";
import { num } from "@/lib/format";
import { leagueRouteHref } from "@/lib/league-scope";
import { projectionName, projectionWord } from "@/lib/labels";
import type { DEvidence } from "@/lib/trade-types";
import { AssetEvidence, DepthEvidence, PlausibilityChecks, RatingsEvidence, WeeklyEvidence } from "./DualValueEvidence";
import { VerdictBadge } from "./DualValueParts";
import styles from "./discover.module.css";

type Load = { status: "idle" | "loading" | "error" } | { status: "ready"; e: DEvidence };

/**
 * A trade's "Why this trade, concerns and evidence" panel. The evidence is fetched when the panel opens (or when the
 * pointer or focus reaches its summary, so it is usually there by the time it opens) instead of being rendered into
 * the page for every row. `src` is the row's `/api/trade-evidence` URL; on a league page it is fetched under
 * `/l/<lg>/` (leagueRouteHref) so the evidence is that league's, never the classic site's. The numbers are the same
 * computation as the table row's. `projLabel` names the page's projections ("ESPN", or "Ours" for a league whose provider has none) for an
 * evidence payload that does not say.
 */
export function TradeEvidence({ src, analyzer, projLabel = "ESPN" }: { src: string; analyzer: string; projLabel?: string }) {
  const [load, setLoad] = useState<Load>({ status: "idle" });
  const started = useRef(false);
  const league = useLeagueId();
  const start = () => {
    if (started.current) return;
    started.current = true;
    setLoad({ status: "loading" });
    fetch(leagueRouteHref(src, league))
      .then((res) => (res.ok ? (res.json() as Promise<DEvidence>) : Promise.reject(new Error(String(res.status)))))
      .then((e) => setLoad({ status: "ready", e }))
      .catch(() => {
        started.current = false;            // reopening the panel retries
        setLoad({ status: "error" });
      });
  };
  return (
    <details className={styles.detail} onToggle={(ev) => { if (ev.currentTarget.open) start(); }}>
      <summary onPointerEnter={start} onFocus={start}>Why this trade, concerns and evidence</summary>
      <div className={styles.detailBody} aria-busy={load.status === "loading"}>
        {load.status === "ready" ? <Evidence e={load.e} analyzer={analyzer} projLabel={projLabel} />
          : load.status === "error" ? (
            <p className={styles.small}>
              The evidence for this trade did not load. Close and reopen this panel to retry, or <TeamLink href={analyzer}>open the trade in the analyzer</TeamLink>.
            </p>
          ) : <p className={`${styles.small} faint`} role="status">Loading evidence…</p>}
      </div>
    </details>
  );
}

function Evidence({ e, analyzer, projLabel }: { e: DEvidence; analyzer: string; projLabel: string }) {
  const concerns = e.rationale.concerns;
  const dv = e.dual_value ?? null;      // absent on an older backend: the classic evidence below still renders
  const proj = dv?.projection_source ? projectionName(dv.projection_source) : projLabel;
  const names = e.names ?? null;
  return (
    <>
      <div className={styles.tags}>
        {dv && <VerdictBadge verdict={dv.plausibility.verdict} text={dv.plausibility.verdict_text} />}
        <Badge value={e.search_method === "exact" ? "exact search" : "heuristic search"} tone={e.search_method === "exact" ? "info" : ""}
          title={e.search_method === "exact" ? "From the proven exact search" : "From the bounded beam search (the row itself is exact)"} />
        {dv?.rank?.front !== null && dv?.rank?.front !== undefined && (
          <Badge value={`Pareto front ${dv.rank.front + 1}`} tone="" title={`${dv.rank.policy}. Front 1 is not dominated by another trade on smaller-side gain, asset ratio or certainty; fronts are an ordering, not a score.`} />
        )}
      </div>
      {dv && <p className={styles.small}>{dv.plausibility.verdict_text}</p>}
      {dv && <PlausibilityChecks dv={dv} />}
      {concerns.length > 0 && (
        <Notice tone="caution" title="Concerns">
          <ul className={styles.list}>{concerns.map((c, i) => <li key={i}>{c}</li>)}</ul>
        </Notice>
      )}
      {e.rationale.points.length > 0 && <ul className={styles.list}>{e.rationale.points.map((p, i) => <li key={i}>{p}</li>)}</ul>}
      <TradeVsFreeAgency t={e.trade_vs_free_agency} only="team_a" projLabel={proj} />
      {dv ? <AssetEvidence dv={dv} /> : <AssetTable e={e} proj={proj} />}
      {dv && <DepthEvidence dv={dv} />}
      {dv && <RatingsEvidence ratings={e.ratings} sends={e.sends} receives={e.receives} names={names ?? namesFromAssets(dv)} />}
      {dv && <WeeklyEvidence dv={dv} names={names} />}
      <div className={styles.cols}>
        {[{ s: e.mine_detail, t: e.mine_detail.team_name ?? "Analyzed team" }, { s: e.theirs_detail, t: e.theirs_detail.team_name ?? "Partner" }].map(({ s, t }) => (
          <div key={s.team_id}>
            <h4>{t}</h4>
            <ul className={styles.list}>{s.fit_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
          </div>
        ))}
      </div>
      {e.injury_scenarios.length > 0 && (
        <p className={styles.small}>Injury return scenarios (earliest return, delayed, none) are in the analyzer: <TeamLink href={analyzer}>open</TeamLink>.</p>
      )}
      {e.manager_context && (
        <p className={styles.mgrNote}>{e.opponent_name}: {e.manager_context.trade_history.completed_trades} completed trades
          ({e.manager_context.trade_history.confidence.toLowerCase()} confidence). {e.manager_context.note}</p>
      )}
      {e.need_notes.map((n, i) => <p key={i} className={styles.mgrNote}>{n}</p>)}
    </>
  );
}

/** Names of the traded players from the asset block, for an evidence payload that has no `names` map. */
function namesFromAssets(dv: NonNullable<DEvidence["dual_value"]>): Record<string, { name: string | null; position: string | null }> {
  return Object.fromEntries([...dv.asset.you_send, ...dv.asset.you_receive].map((p) => [String(p.espn_player_id), { name: p.name, position: p.position }]));
}

/** The classic asset table (no dual-value block in the payload). */
function AssetTable({ e, proj }: { e: DEvidence; proj: string }) {
  const rows = [...e.asset_balance.you_send.map((p) => ({ p, who: "gives" })), ...e.asset_balance.they_send.map((p) => ({ p, who: "receives" }))];
  return (
    <div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Player</th><th>Side</th>
              <th className="num" title={`${projectionWord(proj)}points above the best available free agent at his position, over his NFL-eligible weeks`}>Pts above repl.</th>
              <th className="num">Repl. / wk</th><th>Position rank</th><th>Notes</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ p, who }) => (
              <tr key={p.espn_player_id}>
                <td><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /></td>
                <td className="faint">{who}</td>
                <td className="num">{p.par === null ? <span title="Missing projections">incomplete ({p.known_par.toFixed(0)} known)</span> : p.par.toFixed(1)}</td>
                <td className="num faint">{num(p.replacement_per_week, 1)}</td>
                <td>{p.position_rank ? <>{p.position}{p.position_rank} of {p.position_rank_of} {p.starter_caliber && <Badge value="starter-caliber" tone="info" title={`Within the league's ${p.league_starters_at_position} starters at ${p.position}`} />}</> : "—"}</td>
                <td className="faint wrap">{[p.replacement_source.join("; "), p.nfl_ineligible_weeks.length ? `NFL-ineligible weeks ${p.nfl_ineligible_weeks.join(", ")} not counted` : "",
                  p.eligibility_unknown ? "NFL eligibility unknown" : "", p.return_unconfirmed ? "return not confirmed" : ""].filter(Boolean).join(" · ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {e.asset_balance.caveats.length > 0 && <ul className={styles.list}>{e.asset_balance.caveats.map((c, i) => <li key={i} className="faint">{c}</li>)}</ul>}
    </div>
  );
}
