"use client";

// Interactive trade builder. Team and player selection is local state: switching teams swaps the
// already-loaded roster instantly with no request. Only "Evaluate" navigates (to the same URL-backed
// deep link the server renders), so an evaluation runs only when asked for, and the server-rendered
// result (children) is shown only while it matches the current selection.

import { useRouter } from "next/navigation";
import { type ReactNode, useState, useTransition } from "react";
import { Badge, Pos } from "@/components/ui";
import {
  canEvaluate, changeTeam, problems, rosterPreview, type Selection, selectionKey, setStartWeek, shapeLabel, toQuery,
  togglePlayer,
} from "@/lib/trade-builder";
import type { OptionPlayer, OptionTeam } from "@/lib/trade-types";

const NARROW_GRID = { gridTemplateColumns: "repeat(auto-fit, minmax(min(420px, 100%), 1fr))" };
const GHOST = { background: "transparent", color: "var(--accent)", border: "1px solid var(--border)", fontWeight: 400 };

function availabilityBadge(p: OptionPlayer) {
  const a = p.availability;
  if (a && ["nfl_ineligible", "no_eligible_week_remaining"].includes(a.status)) {
    const until = a.earliest_eligible_week ? `NFL IR · eligible wk ${a.earliest_eligible_week}` : "NFL IR · no week left";
    return <Badge value={until} tone="bad" title={a.warnings.join(" ")} />;
  }
  if (a && ["eligible_return_unconfirmed", "reserve_eligibility_unknown"].includes(a.status)) {
    return <Badge value={a.status === "reserve_eligibility_unknown" ? "IR · return unknown" : "IR · return unconfirmed"}
      tone="warn" title={a.warnings.join(" ")} />;
  }
  if (p.injury_status && !["ACTIVE", "NORMAL", "INJURY_RESERVE"].includes(p.injury_status)) return <Badge value={p.injury_status} />;
  return null;
}

export function TradeBuilder({ teams, initial, evaluatedKey, maxPerSide, capacity, limits, firstWeek, finalWeek, children }: {
  teams: OptionTeam[]; initial: Selection; evaluatedKey: string | null; maxPerSide: number; capacity: number;
  limits: Record<string, number>; firstWeek: number; finalWeek: number; children?: ReactNode;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [sel, setSel] = useState<Selection>(initial);
  const [showStale, setShowStale] = useState(false);
  // A new URL selection (e.g. a deep link followed while on this page) replaces local state.
  const initKey = selectionKey(initial);
  const [seenInit, setSeenInit] = useState(initKey);
  if (initKey !== seenInit) {
    setSeenInit(initKey);
    setSel(initial);
  }
  const key = selectionKey(sel);
  const current = evaluatedKey !== null && evaluatedKey === key;
  const issues = problems(sel, maxPerSide);
  const teamA = teams.find((t) => t.team_id === sel.ta);
  const teamB = teams.find((t) => t.team_id === sel.tb);
  const pick = (team: OptionTeam | undefined, ids: number[]) =>
    ids.map((id) => team?.roster.find((p) => p.espn_player_id === id)).filter((p): p is OptionPlayer => !!p);
  const aOut = pick(teamA, sel.a), bOut = pick(teamB, sel.b);
  const previews = teamA && teamB && sel.ta !== sel.tb ? [
    { team: teamA, pv: rosterPreview(teamA.roster, sel.a, bOut, capacity, limits) },
    { team: teamB, pv: rosterPreview(teamB.roster, sel.b, aOut, capacity, limits) },
  ] : [];

  const evaluate = () => {
    if (!canEvaluate(sel, maxPerSide)) return;
    setShowStale(false);
    startTransition(() => router.push(`/trades?${toQuery(sel)}`, { scroll: false }));
  };

  return (
    <>
      <section className="panel stack">
        <div className="row-flex">
          <label>Team A <select value={sel.ta ?? ""} onChange={(e) => setSel(changeTeam(sel, teams, "a", Number(e.target.value)))}>
            {teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
          </select></label>
          <label>Team B <select value={sel.tb ?? ""} onChange={(e) => setSel(changeTeam(sel, teams, "b", Number(e.target.value)))}>
            {teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
          </select></label>
          <label>Start week <select value={sel.sw ?? ""} onChange={(e) => setSel(setStartWeek(sel, e.target.value === "" ? null : Number(e.target.value)))}>
            <option value="">auto ({firstWeek})</option>
            {/* Weeks that have already kicked off are not offered: the API rejects them. */}
            {Array.from({ length: finalWeek - firstWeek + 1 }, (_, i) => firstWeek + i).map((w) => <option key={w} value={w}>{w}</option>)}
          </select></label>
          <button type="button" onClick={evaluate} disabled={pending || issues.length > 0}
            style={pending || issues.length > 0 ? { opacity: 0.5, cursor: "not-allowed" } : undefined}>
            {pending ? "Evaluating…" : "Evaluate"}</button>
          <button type="button" style={GHOST} onClick={() => setSel({ ...sel, a: [], b: [] })}>clear picks</button>
        </div>
        <div className="row-flex" aria-live="polite">
          <span><b>Trade shape {shapeLabel(sel)}</b></span>
          <span className="faint">{teamA?.name ?? "Team A"} sends {sel.a.length} · {teamB?.name ?? "Team B"} sends {sel.b.length}
            {" "}· up to {maxPerSide} per side</span>
          {current && !pending && <Badge value="result below is for this selection" tone="good" />}
          {!current && evaluatedKey !== null && !pending && <Badge value="selection changed: not evaluated" tone="warn" />}
        </div>
        {issues.filter((p) => p.code !== "empty_side" || sel.a.length + sel.b.length > 0).map((p) => (
          <div key={p.code} className="callout">{p.text}</div>
        ))}
        {previews.map(({ team, pv }) => (pv.overCapacityBy > 0 || pv.positionsOver.length > 0) && (
          <div key={team.team_id} className="callout">
            <b>{team.name}</b>:{" "}
            {pv.overCapacityBy > 0 && <>{pv.activeAfter} active players for {pv.capacity} active spots, so at least {pv.overCapacityBy} forced
              drop{pv.overCapacityBy > 1 ? "s" : ""}. </>}
            {pv.positionsOver.map((x) => <span key={x.position}>{x.count} {x.position} over ESPN&apos;s limit of {x.limit}. </span>)}
            The evaluator chooses the drops exactly (the legal removal that keeps the most projected starting points).
          </div>
        ))}
        <div className="grid" style={NARROW_GRID}>
          {[{ team: teamA, side: "a" as const, picked: sel.a }, { team: teamB, side: "b" as const, picked: sel.b }].map(({ team, side, picked }) => team && (
            <div key={side}>
              <h2>{team.name} sends <span className="faint" style={{ fontSize: 13 }}>({picked.length} selected)</span></h2>
              <div className="table-wrap">
                <table>
                  <thead><tr><th /><th>Player</th><th>NFL</th><th className="num" title="ESPN rest-of-season projection (sum of weekly projections)">ESPN ROS</th></tr></thead>
                  <tbody>
                    {team.roster.map((p) => {
                      const on = picked.includes(p.espn_player_id);
                      return (
                        <tr key={p.espn_player_id} className={on ? "selected" : undefined} style={on ? { background: "var(--info-bg)" } : undefined}>
                          <td><input type="checkbox" aria-label={`${team.name} sends ${p.name ?? p.espn_player_id}`} checked={on}
                            onChange={() => setSel(togglePlayer(sel, side, p.espn_player_id))} /></td>
                          <td>{p.name ?? `#${p.espn_player_id}`} <Pos value={p.position} />
                            {p.on_ir && <> <Badge value="fantasy IR slot" tone="bad" title="In an ESPN IR slot (inactive in the canonical lineup)" /></>}
                            {" "}{availabilityBadge(p)}</td>
                          <td className="faint">{p.pro_team ?? "FA"}</td>
                          <td className="num">{p.ros_projected_points === null ? <span className="missing" title="Incomplete ESPN coverage">—</span> : p.ros_projected_points.toFixed(1)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      </section>

      {pending && <div className="callout">Evaluating {shapeLabel(sel)}: rebuilding both teams&apos; optimal lineups for every remaining week…</div>}
      {current && !pending && children}
      {!current && evaluatedKey !== null && !pending && (
        <div className="callout">
          The selection changed since the last evaluation, so that result is hidden. Press <b>Evaluate</b> to analyze the
          current {shapeLabel(sel)}.{" "}
          <button type="button" style={GHOST} onClick={() => setShowStale(!showStale)}>
            {showStale ? "hide" : "show"} the previous result (a different trade)</button>
        </div>
      )}
      {!current && showStale && !pending && <div style={{ opacity: 0.55 }} aria-label="previous result for a different selection">{children}</div>}
    </>
  );
}
