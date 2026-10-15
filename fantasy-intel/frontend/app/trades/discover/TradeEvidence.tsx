"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { TradeVsFreeAgency } from "@/components/TradeVsFreeAgency";
import { Badge, Notice, Pos } from "@/components/ui";
import { num } from "@/lib/format";
import type { DEvidence } from "@/lib/trade-types";
import styles from "./discover.module.css";

type Load = { status: "idle" | "loading" | "error" } | { status: "ready"; e: DEvidence };

/**
 * A trade's "Why this trade, concerns and evidence" panel. The evidence is fetched when the panel opens (or when the
 * pointer or focus reaches its summary, so it is usually there by the time it opens) instead of being rendered into
 * the page for every row. `src` is the row's `/api/trade-evidence` URL; the numbers are the same computation as the
 * table row's.
 */
export function TradeEvidence({ src, analyzer }: { src: string; analyzer: string }) {
  const [load, setLoad] = useState<Load>({ status: "idle" });
  const started = useRef(false);
  const start = () => {
    if (started.current) return;
    started.current = true;
    setLoad({ status: "loading" });
    fetch(src)
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
        {load.status === "ready" ? <Evidence e={load.e} analyzer={analyzer} />
          : load.status === "error" ? (
            <p className={styles.small}>
              The evidence for this trade did not load. Close and reopen this panel to retry, or <Link href={analyzer}>open the trade in the analyzer</Link>.
            </p>
          ) : <p className={`${styles.small} faint`} role="status">Loading evidence…</p>}
      </div>
    </details>
  );
}

function Evidence({ e, analyzer }: { e: DEvidence; analyzer: string }) {
  const concerns = e.rationale.concerns;
  return (
    <>
      <div className={styles.tags}>
        <Badge value={e.search_method === "exact" ? "exact search" : "heuristic search"} tone={e.search_method === "exact" ? "info" : ""}
          title={e.search_method === "exact" ? "From the proven exact search" : "From the bounded beam search (the row itself is exact)"} />
      </div>
      {concerns.length > 0 && (
        <Notice tone="caution" title="Concerns">
          <ul className={styles.list}>{concerns.map((c, i) => <li key={i}>{c}</li>)}</ul>
        </Notice>
      )}
      {e.rationale.points.length > 0 && <ul className={styles.list}>{e.rationale.points.map((p, i) => <li key={i}>{p}</li>)}</ul>}
      <TradeVsFreeAgency t={e.trade_vs_free_agency} only="team_a" />
      <AssetTable e={e} />
      <div className={styles.cols}>
        {[{ s: e.mine_detail, t: `${e.mine_detail.team_name} (you)` }, { s: e.theirs_detail, t: e.theirs_detail.team_name ?? "Them" }].map(({ s, t }) => (
          <div key={s.team_id}>
            <h4>{t}</h4>
            <ul className={styles.list}>{s.fit_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
          </div>
        ))}
      </div>
      {e.injury_scenarios.length > 0 && (
        <p className={styles.small}>Injury return scenarios (earliest return, delayed, none) are in the analyzer: <Link href={analyzer}>open</Link>.</p>
      )}
      {e.manager_context && (
        <p className={styles.mgrNote}>{e.opponent_name}: {e.manager_context.trade_history.completed_trades} completed trades
          ({e.manager_context.trade_history.confidence.toLowerCase()} confidence). {e.manager_context.note}</p>
      )}
      {e.need_notes.map((n, i) => <p key={i} className={styles.mgrNote}>{n}</p>)}
    </>
  );
}

function AssetTable({ e }: { e: DEvidence }) {
  const rows = [...e.asset_balance.you_send.map((p) => ({ p, who: "you send" })), ...e.asset_balance.they_send.map((p) => ({ p, who: "they send" }))];
  return (
    <div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Player</th><th>Side</th>
              <th className="num" title="ESPN points above the best available free agent at his position, over his NFL-eligible weeks">Pts above repl.</th>
              <th className="num">Repl. / wk</th><th>Position rank</th><th>Notes</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ p, who }) => (
              <tr key={p.espn_player_id}>
                <td>{p.name} <Pos value={p.position} /></td>
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
