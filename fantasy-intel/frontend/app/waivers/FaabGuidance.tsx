"use client";

import { useEffect, useId, useState } from "react";
import { Notice } from "@/components/ui";
import { planFreshness, type Claim } from "@/lib/claim-plan";
import {
  AUDIT_LABEL, URGENCY_LABEL, bidVsRange, budgetText, faabSrc, money, observedText, rangeText,
  type ClaimGuidance, type FaabResponse,
} from "@/lib/faab";
import { plural } from "@/lib/plural";
import type { ProvenanceStatus } from "@/lib/signal-provenance";
import styles from "./claim-plan.module.css";

type Loaded = { src: string; data: FaabResponse | null; error: string | null; freshness: ProvenanceStatus };

/**
 * FAAB guidance for the claim plan (issue #179), read through /l/<lg>/api/waiver-faab. The answer is shown only for
 * exactly the current claims and share; it never depends on a typed bid, so bids do not refetch it.
 */
export function useFaabGuidance(leagueId: string, team: number | null, claims: readonly Claim[], share: number, enabled: boolean) {
  const src = enabled && team !== null ? faabSrc(leagueId, team, claims, share) : null;
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  useEffect(() => {
    if (!src) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      fetch(src, { signal: ctrl.signal, cache: "no-store" })
        .then(async (r) => {
          const body = await r.json().catch(() => null);
          if (!r.ok || !body || body.contract !== "waiver-faab/v1" || body.scope !== leagueId) {
            setLoaded({ src, data: null, error: (body && typeof body.error === "string" ? body.error : null) ?? `FAAB guidance failed (${r.status}).`, freshness: "unknown" });
          } else {
            setLoaded({ src, data: body as FaabResponse, error: null, freshness: planFreshness(body as FaabResponse) });
          }
        })
        .catch((err: unknown) => {
          if ((err as { name?: string })?.name === "AbortError") return;
          setLoaded({ src, data: null, error: "FAAB guidance could not be loaded: the server did not answer.", freshness: "unknown" });
        });
    }, 250);
    return () => { window.clearTimeout(timer); ctrl.abort(); };
  }, [src, leagueId]);
  const current = loaded && loaded.src === src ? loaded : null;
  return { data: current?.data ?? null, error: current?.error ?? null, freshness: current?.freshness ?? "unknown" as ProvenanceStatus, pending: src !== null && !current };
}

/** The plan-level FAAB block: capability, budget, the share input, the combined check and observed league bids. */
export function FaabSummary({ res, error, pending, freshness, share, onShare, provider }: {
  res: FaabResponse | null; error: string | null; pending: boolean; freshness: ProvenanceStatus;
  share: number; onShare: (share: number) => void; provider: string;
}) {
  const shareId = useId();
  if (error) return <Notice tone="caution" title="FAAB guidance is unavailable">{error} No bid range is shown.</Notice>;
  if (!res) return pending ? <p className={styles.muted} role="status">Loading FAAB guidance…</p> : null;
  const cap = res.capability;
  if (cap.guidance === "not_applicable") return null;
  return (
    <section className={styles.faab} aria-label="FAAB bid guidance">
      <p className={styles.faabHead}><b>FAAB bid guidance</b> <span className={styles.muted}>heuristic ranges, not predictions</span></p>
      {cap.guidance === "unavailable"
        ? <Notice tone="info" title="No bid ranges for this league">{cap.reasons[0]} Nothing is guessed in its place.</Notice>
        : <>
          <p className={styles.sub}>{budgetText(res.budget)}</p>
          {freshness !== "current" && (
            <p className={styles.reason} data-tone="caution">
              {freshness === "stale" ? "The league data is stale" : "The age of the league data is unknown"}: your remaining FAAB may have changed since the last sync, so check it in {provider} before bidding.
            </p>
          )}
          <div className={`${styles.field} ${styles.share}`}>
            <label htmlFor={shareId}>Your assumption: most of your FAAB one claim may take</label>
            <select id={shareId} value={String(share)} onChange={(e) => onShare(Number(e.currentTarget.value))}>
              {res.inputs.share_choices.map((s) => <option key={s} value={String(s)}>{s}%{s === res.inputs.default_share_pct ? " (default)" : ""}</option>)}
            </select>
          </div>
          {res.combined && res.combined.ranged_claims > 0 && <Combined c={res.combined} />}
        </>}
      <Observed res={res} />
      <details className={styles.unknown}>
        <summary>What {provider} supplies for FAAB ({cap.fields.filter((f) => f.status === "supplied").length} of {cap.fields.length} synced)</summary>
        <div className="table-wrap flat">
          <table className={styles.audit} aria-label={`FAAB data in the last ${provider} sync`}>
            <thead><tr><th scope="col">Field</th><th scope="col">Status</th><th scope="col">Detail</th></tr></thead>
            <tbody>
              {cap.fields.map((f) => (
                <tr key={f.field}>
                  <th scope="row">{f.label}</th>
                  <td>{AUDIT_LABEL[f.status]}</td>
                  <td>{[f.display, f.source ? `from ${f.source}` : null, f.reason].filter(Boolean).join(" · ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <details className={styles.unknown}>
        <summary>How the ranges are built</summary>
        {res.assumptions.map((a) => <p key={a}>{a}</p>)}
        {res.limitations.map((a) => <p key={a}>{a}</p>)}
      </details>
    </section>
  );
}

function Combined({ c }: { c: NonNullable<FaabResponse["combined"]> }) {
  if (c.max_spend_if_won === null) return null;
  const list = (r: number[]) => (r.length < 3 ? r.join(" and ") : `${r.slice(0, -1).join(", ")} and ${r[r.length - 1]}`);
  return (
    <div className={styles.details}>
      <p className={styles.reason} data-tone={c.fits === false ? "caution" : undefined}>
        If every claim with a range won at the top of it: {money(c.max_spend_if_won)}
        {c.remaining !== null && <> of your {money(c.remaining)}</>}
        {c.fits === false && ". That is more than you have, so each claim also shows a top that fits if all of them win"}
        {c.fits === true && ", which fits"}.
        {!c.complete && <> {plural(c.unranged_ranks.length, "Claim")} {list(c.unranged_ranks)} {c.unranged_ranks.length === 1 ? "has" : "have"} no range and {c.unranged_ranks.length === 1 ? "is" : "are"} not counted.</>}
      </p>
      {c.exclusive_groups.map((g) => (
        <p key={`${g.kind}-${g.ranks.join("-")}`} className={styles.reason}>
          {g.kind === "shared_drop"
            ? <>Claims {list(g.ranks)} drop the same player, so at most one of them can go through</>
            : <>Claims {list(g.ranks)} each need an open roster spot and you have {g.capacity}, so at most {g.capacity} of them can go through</>}
          : only {g.capacity === 1 ? "the largest top is" : `the ${g.capacity} largest tops are`} counted.
        </p>
      ))}
    </div>
  );
}

function Observed({ res }: { res: FaabResponse }) {
  // The list of past claims (player names) is rendered only while open: it is detail, not part of the plan's text.
  const [open, setOpen] = useState(false);
  const cap = res.capability;
  if (cap.observed_bids === "not_applicable") return null;
  const o = res.observed;
  if (cap.observed_bids !== "available" || !o || !o.overall) {
    const why = cap.reasons.find((r) => /bid history|bid amounts/.test(r))
      ?? "No league bid history is in the synced data.";
    return <p className={styles.reason}>Observed league bids: none to show. {why}</p>;
  }
  return (
    <details className={styles.unknown} open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        Observed: {o.overall.count} winning {plural(o.overall.count, "bid")} in this league this season, median {money(o.overall.median)}, highest {money(o.overall.max)}
      </summary>
      {open && <>
      <p>Executed waiver claims only, as {o.without_bid > 0 ? `stored (${o.without_bid} without a stored amount are left out)` : "stored"}. Losing and pending bids are private to each manager and are not shown.</p>
      <ul className={styles.recent}>
        {o.recent.map((r, i) => (
          <li key={`${r.espn_player_id ?? "x"}-${r.week ?? "x"}-${i}`}>
            {r.week !== null ? `Week ${r.week}: ` : ""}{r.name ?? "Unknown player"}{r.position ? ` (${r.position})` : ""} for {money(r.bid)}
          </li>
        ))}
      </ul>
      </>}
    </details>
  );
}

/** One claim's guidance, beside its bid field. Heuristic ranges and observed bids are labelled apart. */
export function FaabClaimGuidance({ g, bid, minObserved }: { g: ClaimGuidance; bid: number | null; minObserved: number }) {
  if (g.status !== "range" || !g.range) {
    if (!g.reason || g.status === "no_bid_needed") return null;
    return <p className={styles.reason}>FAAB: {g.reason}</p>;
  }
  const vs = bidVsRange(bid, g.range);
  const scaled = g.if_all_win && g.if_all_win.high < g.range.high ? g.if_all_win : null;
  const obs = observedText(g.observed, minObserved);
  return (
    <div className={styles.details}>
      <p className={styles.reason} data-tone="info">
        <b>Heuristic range {rangeText(g.range)}</b> · {g.urgency ? URGENCY_LABEL[g.urgency] : null}
        {scaled && <> · up to {money(scaled.high)} if every claim wins</>}
        {vs && vs !== "within" && <> · your bid is {vs} it</>}
      </p>
      <p className={styles.muted}>
        {g.basis.gain_per_week !== null && <>{g.basis.gain_per_week >= 0 ? "+" : ""}{g.basis.gain_per_week.toFixed(1)} projected starting points a week over {g.basis.weeks} {plural(g.basis.weeks, "week")}; </>}
        cap {g.range.cap_share_pct}% of your remaining FAAB ({money(g.range.cap)}), {g.urgency} band {g.range.band_pct[0]}–{g.range.band_pct[1]}% of it.
      </p>
      {obs && <p className={styles.muted}>{obs}</p>}
      {g.notes.map((n) => <p key={n.code} className={styles.muted}>{n.message}</p>)}
    </div>
  );
}
