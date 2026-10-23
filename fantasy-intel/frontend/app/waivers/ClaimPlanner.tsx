"use client";

import Link from "@/components/LeagueLink";
import { useEffect, useId, useState, useSyncExternalStore, type DragEvent } from "react";
import { Delta, Notice, ProvenanceBadge } from "@/components/ui";
import {
  MAX_CLAIMS, VALIDATION_LABEL, claimPlanKey, claimPlanSrc, moveClaim, parseBid, planFreshness, planText,
  readClaimPlan, reconcile, rulesText, shownValidation, updateClaim, writeClaimPlan,
  type Claim, type ClaimPlanResponse, type PlanClaim, type Validation,
} from "@/lib/claim-plan";
import { Missing } from "@/lib/format";
import { withLeague } from "@/lib/league-scope";
import { plural } from "@/lib/plural";
import { signalsOf, type ProvenanceStatus } from "@/lib/signal-provenance";
import { shortlistKey } from "@/lib/waiver-shortlist";
import { DEFAULT_SHARE } from "@/lib/faab";
import { FaabClaimGuidance, FaabSummary, useFaabGuidance } from "./FaabGuidance";
import { SavePlayer, useShortlist } from "./Shortlist";
import styles from "./claim-plan.module.css";

export type ShortlistPlayer = { id: number; name: string; position: string; team: string | null };

type PlanSnapshot = { claims: Claim[]; ready: boolean; available: boolean };

/** One store per mounted league and team (the parent keys the planner by both); nothing is shared across leagues. */
function createPlanStore(leagueId: string | null, team: number | null) {
  const initial: PlanSnapshot = { claims: [], ready: false, available: true };
  let snapshot = initial;
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((l) => l());
  const key = claimPlanKey(leagueId, team);
  const load = () => {
    try {
      snapshot = { ...readClaimPlan(window.localStorage, leagueId, team), ready: true };
    } catch {
      snapshot = { claims: [], ready: true, available: false };
    }
    if (!key) snapshot = { ...snapshot, available: true };
    emit();
  };
  return {
    getSnapshot: () => snapshot,
    getServerSnapshot: () => initial,
    subscribe(listener: () => void) {
      listeners.add(listener);
      if (!snapshot.ready) load();
      const changed = (event: StorageEvent) => {
        if (event.key === key || event.key === null) load();
      };
      window.addEventListener("storage", changed);
      return () => { listeners.delete(listener); window.removeEventListener("storage", changed); };
    },
    save(claims: Claim[]) {
      if (!snapshot.ready || !key) return;
      let ok = false;
      try { ok = writeClaimPlan(window.localStorage, leagueId, team, claims); } catch { /* the visit keeps the plan */ }
      snapshot = { claims, ready: true, available: snapshot.available && ok };
      emit();
    },
  };
}

type Checked = { src: string; data: ClaimPlanResponse | null; error: string | null; freshness: ProvenanceStatus; today: string };

const TONE: Record<Validation, "positive" | "caution" | "negative"> = { legal: "positive", unverified: "caution", invalid: "negative" };

/**
 * The Waivers shortlist as a ranked claim plan (issue #180). Order, drop and bid are saved on this device under the
 * canonical league and the team; the backend checks the plan with the add/drop simulator. Nothing is submitted.
 */
export function WaiverShortlist({ players, team, teamName, provider, leagueName }: {
  players: ShortlistPlayer[];
  /** The team the page is for (resolved by the backend), or null when no team is known. */
  team: number | null;
  teamName: string | null;
  provider: string;
  leagueName: string | null;
}) {
  const value = useShortlist();
  if (!value) return null;
  const scoped = !!shortlistKey(value.leagueId);
  return (
    <section className={styles.shortlist} aria-label="Waiver shortlist">
      <div className={styles.head}>
        <div>
          <p className={styles.eyebrow}>Shortlist</p>
          <h2 className={styles.title}>Claim plan <span className={styles.count}>{value.ids.length}</span></h2>
        </div>
      </div>
      <p className={styles.sub}>
        Saved on this device for this league{team !== null ? " and team" : ""}. Not synced between devices. Not submitted:
        nothing here is sent to {provider}.
      </p>
      {!scoped
        ? <p>Open a league to save waiver candidates.</p>
        : !value.ready ? <p role="status">Loading saved players…</p>
          : <Planner key={`${value.leagueId}:${team ?? "none"}`} players={players} team={team} teamName={teamName}
            provider={provider} leagueName={leagueName} />}
    </section>
  );
}

function Planner({ players, team, teamName, provider, leagueName }: {
  players: ShortlistPlayer[]; team: number | null; teamName: string | null; provider: string; leagueName: string | null;
}) {
  const shortlist = useShortlist()!;
  const leagueId = shortlist.leagueId!;
  const [store] = useState(() => createPlanStore(leagueId, team));
  const plan = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot);
  const claims = reconcile(plan.claims, shortlist.ids);
  const canPlan = team !== null && plan.ready;
  const src = canPlan && claims.length > 0 ? claimPlanSrc(leagueId, team, claims) : null;
  const [checked, setChecked] = useState<Checked | null>(null);
  const [copied, setCopied] = useState<"" | "copied" | "failed">("");
  const [badBids, setBadBids] = useState<ReadonlySet<number>>(new Set());
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const textId = useId();

  useEffect(() => {
    if (!src) return;
    const ctrl = new AbortController();
    // Bids are typed: wait for a pause before checking the plan again.
    const timer = window.setTimeout(() => {
      fetch(src, { signal: ctrl.signal, cache: "no-store" })
        .then(async (r) => {
          const body = await r.json().catch(() => null);
          const now = Date.now();
          const today = new Date(now).toISOString().slice(0, 10);
          if (!r.ok || !body || body.contract !== "waiver-claim-plan/v1" || body.scope !== leagueId) {
            setChecked({ src, data: null, error: (body && typeof body.error === "string" ? body.error : null) ?? `The check failed (${r.status}).`, freshness: "unknown", today });
          } else {
            setChecked({ src, data: body as ClaimPlanResponse, error: null, freshness: planFreshness(body as ClaimPlanResponse, now), today });
          }
        })
        .catch((err: unknown) => {
          if ((err as { name?: string })?.name === "AbortError") return;
          setChecked({ src, data: null, error: "The plan could not be checked: the server did not answer.", freshness: "unknown", today: new Date().toISOString().slice(0, 10) });
        });
    }, 250);
    return () => { window.clearTimeout(timer); ctrl.abort(); };
  }, [src, leagueId]);

  const save = (next: Claim[]) => store.save(next);
  // The last answer still supplies the rules and the drop picker while a new check runs; claim checks come only from
  // the answer for exactly this plan (claims depend on each other, so an answer for another order or drop is stale).
  const last = checked?.data ?? null;
  const res = checked?.src === src ? last : null;
  const pending = src !== null && checked?.src !== src;
  const byAdd = new Map((res?.claims ?? []).map((c) => [c.request.add, c]));
  const lastByAdd = new Map((last?.claims ?? []).map((c) => [c.request.add, c]));
  const loaded = new Map(players.map((p) => [p.id, p]));
  const names = new Map<number, string>([
    ...players.map((p) => [p.id, p.name] as [number, string]),
    ...(last?.roster.drop_options ?? []).flatMap((o) => (o.name ? [[o.espn_player_id, o.name] as [number, string]] : [])),
    ...(last?.claims ?? []).flatMap((c) => (c.add?.name ? [[c.add.espn_player_id, c.add.name] as [number, string]] : [])),
  ]);
  const faab = last?.rules.acquisition === "FAAB";
  // FAAB guidance (#179): asked for unless the synced rules say claims go by waiver priority; the answer gates itself.
  const [share, setShare] = useState(DEFAULT_SHARE);
  const guidance = useFaabGuidance(leagueId, team, claims, share, canPlan && last?.rules.acquisition !== "PRIORITY");
  const byRank = new Map((guidance.data?.claims ?? []).map((g) => [g.rank, g]));
  const freshness = checked?.freshness ?? "unknown";
  const shown = claims.slice(0, MAX_CLAIMS).map((c) => {
    const r = byAdd.get(c.add);
    return r && r.request.drop === c.drop && r.request.bid === c.bid ? shownValidation(r, freshness).validation : null;
  });
  const counts = { legal: 0, unverified: 0, invalid: 0 };
  shown.forEach((v) => { if (v) counts[v] += 1; });
  const text = planText({ claims, res, freshness, provider, leagueName, teamName, names,
    today: checked?.today ?? "" });

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied("copied");
    } catch {
      setCopied("failed");
    }
  };
  const drop = (to: number) => (e: DragEvent) => {
    e.preventDefault();
    if (dragFrom !== null && dragFrom !== to) save(moveClaim(claims, dragFrom, to));
    setDragFrom(null);
  };

  return (
    <>
      {(!shortlist.available || !plan.available) && <p role="status">Browser storage is unavailable. Changes last for this visit only.</p>}
      {team === null && claims.length > 0 && (
        <p className={styles.sub}>Pick a team at the top of the page to order claims, choose drops and check them.</p>
      )}
      {claims.length === 0
        ? <>
          <p>No saved players yet. Use Save beside a waiver player to plan claims here.</p>
          {canPlan && <FaabSummary res={guidance.data} error={guidance.error} pending={guidance.pending}
            freshness={guidance.freshness} share={share} onShare={setShare} provider={provider} />}
        </>
        : <>
          {canPlan && (
            <div className={styles.status} aria-live="polite" aria-busy={pending || undefined}>
              {checked?.error && !pending
                ? <Notice tone="caution" title="This plan has not been checked">{checked.error} Every claim stays unconfirmed.</Notice>
                : res
                  ? <p className={styles.summary}>
                    <b>{counts.legal} of {Math.min(claims.length, MAX_CLAIMS)} {plural(Math.min(claims.length, MAX_CLAIMS), "claim")} {counts.legal === 1 ? "passes its" : "pass their"} checks</b>
                    {counts.unverified > 0 && <> · {counts.unverified} {counts.unverified === 1 ? "needs" : "need"} review</>}
                    {counts.invalid > 0 && <> · {counts.invalid} not valid</>}
                  </p>
                  : <p className={styles.summary} role="status">Checking the plan…</p>}
              {last && (
                <div className={styles.rules}>
                  <span>{rulesText(last.rules, provider)}</span>
                  {res && res.summary.total_bid !== null && <span>Bids total ${res.summary.total_bid}</span>}
                  {last.provenance && <ProvenanceBadge signals={signalsOf(last.provenance)} />}
                </div>
              )}
              {last && last.rules.unknown.length > 0 && (
                <details className={styles.unknown}>
                  <summary>{last.rules.unknown.length} waiver {plural(last.rules.unknown.length, "rule")} not in the synced settings</summary>
                  {last.rules.unknown.map((u) => <p key={u.rule}>{u.reason}</p>)}
                </details>
              )}
            </div>
          )}
          {canPlan && <FaabSummary res={guidance.data} error={guidance.error} pending={guidance.pending}
            freshness={guidance.freshness} share={share} onShare={setShare} provider={provider} />}
          <ol className={styles.rows} aria-label="Claim order">
            {claims.map((c, i) => {
              const r = byAdd.get(c.add);
              const fresh = r && r.request.drop === c.drop && r.request.bid === c.bid ? r : null;
              const player = loaded.get(c.add);
              const known = lastByAdd.get(c.add) ?? null;
              const name = known?.add?.name ?? player?.name ?? `Player #${c.add}`;
              return (
                <li key={c.add} className={styles.row} data-dragging={dragFrom === i || undefined}
                  onDragOver={(e) => { if (dragFrom !== null) e.preventDefault(); }} onDrop={drop(i)}>
                  <div className={styles.rowHead}>
                    {canPlan && (
                      <span className={styles.handle} draggable aria-hidden="true" title="Drag to reorder"
                        onDragStart={(e) => { setDragFrom(i); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(c.add)); }}
                        onDragEnd={() => setDragFrom(null)}>⋮⋮</span>
                    )}
                    <span className={styles.rank}>{i + 1}</span>
                    <div className={styles.who}>
                      <Link href={withLeague(`/players?id=${c.add}`, leagueId)}>{name}</Link>
                      <span className={styles.meta}>{whoLine(known, player)}</span>
                    </div>
                    {canPlan && i < MAX_CLAIMS && <Check claim={fresh} freshness={freshness} pending={pending} error={!!checked?.error} />}
                  </div>
                  {canPlan && (
                    <div className={styles.controls}>
                      <DropPicker claim={c} res={last} names={names}
                        onChange={(d) => save(updateClaim(claims, c.add, { drop: d }))} />
                      {((faab && known?.claim_kind !== "free_agent") || c.bid !== null) && (
                        <div className={styles.field}>
                          <label htmlFor={`bid-${c.add}`}>Bid ($)</label>
                          <input id={`bid-${c.add}`} inputMode="numeric" autoComplete="off" defaultValue={c.bid ?? ""}
                            aria-invalid={badBids.has(c.add) || undefined} aria-describedby={badBids.has(c.add) ? `bid-${c.add}-err` : undefined}
                            onChange={(e) => {
                              const b = parseBid(e.currentTarget.value);
                              setBadBids((prev) => { const n = new Set(prev); if (b.ok) n.delete(c.add); else n.add(c.add); return n; });
                              if (b.ok) save(updateClaim(claims, c.add, { bid: b.bid }));
                            }} />
                          {badBids.has(c.add) && <span id={`bid-${c.add}-err`} className={styles.error}>Whole dollars only</span>}
                        </div>
                      )}
                      <div className={styles.order}>
                        <button type="button" className={styles.move} disabled={i === 0}
                          aria-label={`Move ${name} up`} onClick={() => save(moveClaim(claims, i, i - 1))}>Up</button>
                        <button type="button" className={styles.move} disabled={i === claims.length - 1}
                          aria-label={`Move ${name} down`} onClick={() => save(moveClaim(claims, i, i + 1))}>Down</button>
                        <SavePlayer id={c.add} name={name} />
                      </div>
                    </div>
                  )}
                  {!canPlan && <div className={styles.order}><SavePlayer id={c.add} name={name} /></div>}
                  {canPlan && i < MAX_CLAIMS && byRank.get(i + 1)?.add.espn_player_id === c.add
                    && byRank.get(i + 1)?.drop === c.drop && guidance.data && (
                    <FaabClaimGuidance g={byRank.get(i + 1)!} bid={c.bid} minObserved={guidance.data.inputs.min_observed} />
                  )}
                  {canPlan && i >= MAX_CLAIMS && <p className={styles.reason}>Not checked: only the first {MAX_CLAIMS} claims are checked.</p>}
                  {canPlan && fresh && <Details claim={fresh} freshness={freshness} faab={faab}
                    onUseDrop={(d) => save(updateClaim(claims, c.add, { drop: d }))} />}
                </li>
              );
            })}
          </ol>
          {canPlan && (
            <div className={styles.export}>
              <button type="button" className={styles.copy} onClick={copy}>Copy plan as text</button>
              <span role="status" className={styles.muted}>
                {copied === "copied" ? "Copied. The plan has not been submitted." : copied === "failed" ? "Copy did not work: select the text below." : ""}
              </span>
              <details className={styles.text}>
                <summary>Plain-text plan</summary>
                <label htmlFor={textId} className={styles.muted}>Not submitted. Paste it into your notes; enter the claims in {provider} yourself.</label>
                <textarea id={textId} readOnly value={text} rows={Math.min(14, claims.length + 5)} />
              </details>
            </div>
          )}
        </>}
    </>
  );
}

function whoLine(r: PlanClaim | null, player: ShortlistPlayer | undefined): string {
  const p = r?.add;
  const pos = p?.position ?? player?.position ?? null;
  const team = p?.pro_team ?? player?.team ?? null;
  const kind = r?.claim_kind === "waiver_claim" ? "On waivers" : r?.claim_kind === "free_agent" ? "Free agent" : null;
  if (!pos && !team) return "Current data not loaded. This player may be outside the current filters or evaluated pool.";
  return [pos, team, kind].filter(Boolean).join(" · ");
}

function Check({ claim, freshness, pending, error }: { claim: PlanClaim | null; freshness: ProvenanceStatus; pending: boolean; error: boolean }) {
  if (!claim) {
    return <span className={styles.check} data-tone="caution">{pending ? "Checking" : error ? "Not checked" : "Checking"}</span>;
  }
  const v = shownValidation(claim, freshness).validation;
  return <span className={styles.check} data-tone={TONE[v]}><span aria-hidden="true">{v === "legal" ? "✓" : v === "invalid" ? "✕" : "!"}</span> {VALIDATION_LABEL[v]}</span>;
}

function DropPicker({ claim, res, names, onChange }: {
  claim: Claim; res: ClaimPlanResponse | null; names: ReadonlyMap<number, string>; onChange: (drop: number | null) => void;
}) {
  const options = res?.roster.drop_options ?? [];
  const known = claim.drop === null || options.some((o) => o.espn_player_id === claim.drop);
  return (
    <div className={styles.field}>
      <label htmlFor={`drop-${claim.add}`}>Drop</label>
      <select id={`drop-${claim.add}`} value={claim.drop === null ? "" : String(claim.drop)}
        onChange={(e) => onChange(e.currentTarget.value === "" ? null : Number(e.currentTarget.value))}>
        <option value="">No drop</option>
        {!known && claim.drop !== null && <option value={String(claim.drop)}>{names.get(claim.drop) ?? `Player #${claim.drop}`}{res ? " (not on this roster)" : ""}</option>}
        {options.map((o) => (
          <option key={o.espn_player_id} value={String(o.espn_player_id)}>
            {o.name ?? `Player #${o.espn_player_id}`} ({[o.position, o.pro_team ?? "FA"].filter(Boolean).join(", ")}){o.on_ir ? " · IR" : o.on_taxi ? " · taxi" : ""}
          </option>
        ))}
      </select>
    </div>
  );
}

function Details({ claim, freshness, faab, onUseDrop }: {
  claim: PlanClaim; freshness: ProvenanceStatus; faab: boolean; onUseDrop: (drop: number) => void;
}) {
  const shown = shownValidation(claim, freshness);
  const reasons = claim.reasons.filter((r) => r.effect !== "info");
  const info = claim.reasons.filter((r) => r.effect === "info");
  const s = claim.suggested_drop;
  const ros = claim.impact?.rest_of_season ?? null;
  const rosValue = ros ? (ros.ros_delta ?? ros.known_ros_delta) : null;
  return (
    <div className={styles.details}>
      {shown.reason && <p className={styles.reason} data-tone="caution">{shown.reason}</p>}
      {reasons.map((r) => <p key={`${r.code}-${r.player_id}`} className={styles.reason} data-tone={TONE[r.effect as Validation]}>{r.message}</p>)}
      {s?.available && claim.request.drop !== s.espn_player_id && (
        <p className={styles.reason}>
          <button type="button" className={styles.link} onClick={() => onUseDrop(s.espn_player_id)}>
            Use the suggested drop: {s.name ?? `Player #${s.espn_player_id}`}
          </button>{" "}
          (the legal drop that keeps the most projected starting points; costs {s.lineup_cost !== null ? s.lineup_cost.toFixed(1) : `${s.known_lineup_cost.toFixed(1)} known-only`}).
        </p>
      )}
      {claim.impact && (
        <p className={styles.impact}>
          Rest of season: {rosValue === null ? <Missing why="No projected change" /> : <Delta value={rosValue} suffix=" pts" />}
          {ros && ros.ros_delta === null && <span className={styles.muted}> known-only</span>}
          {claim.impact.first_week && <> · week {claim.impact.first_week.week}: <Delta value={claim.impact.first_week.delta ?? claim.impact.first_week.known_delta} suffix=" pts" /></>}
        </p>
      )}
      {info.length > 0 && (
        <details className={styles.more}>
          <summary>{info.length} more {plural(info.length, "note")}</summary>
          {info.map((r) => <p key={`${r.code}-${r.player_id}`}>{r.message}</p>)}
        </details>
      )}
      {!faab && claim.request.bid !== null && <p className={styles.muted}>Clear the bid field to remove the bid.</p>}
    </div>
  );
}
