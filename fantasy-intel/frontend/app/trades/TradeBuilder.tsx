"use client";

// Interactive trade builder. Team and player selection is local state: switching teams swaps the
// already-loaded roster instantly with no request. Only "Evaluate" navigates (to the same URL-backed
// deep link the server renders), so an evaluation runs only when asked for, and the server-rendered
// result (children) is shown only while it matches the current selection.

import { useRouter } from "next/navigation";
import { type ReactNode, useEffect, useRef, useState, useTransition } from "react";
import { Badge, EmptyState, Notice, Section, Skeleton, SkeletonStatStrip } from "@/components/ui";
import {
  canEvaluate, changeTeam, clearPicks, problems, rosterPreview, type Selection, selectionKey, setStartWeek, shapeLabel, toQuery,
  togglePlayer,
} from "@/lib/trade-builder";
import type { OptionPlayer, OptionTeam } from "@/lib/trade-types";
import { TeamLink } from "@/components/TeamLink";
import { useTeam } from "@/components/useTeam";
import { withTeam } from "@/lib/team-perspective";
import styles from "./builder.module.css";
import { RosterPicker } from "./RosterPicker";

export function TradeBuilder({ teams, initial, evaluatedKey, maxPerSide, capacity, limits, firstWeek, finalWeek, children }: {
  teams: OptionTeam[]; initial: Selection; evaluatedKey: string | null; maxPerSide: number; capacity: number;
  limits: Record<string, number>; firstWeek: number; finalWeek: number; children?: ReactNode;
}) {
  const router = useRouter();
  // The analyzed team (`?team=`) is the page's perspective, separate from the two sides: evaluating keeps it.
  const team = useTeam();
  const [pending, startTransition] = useTransition();
  const [sel, setSel] = useState<Selection>(initial);
  const [showStale, setShowStale] = useState(false);
  // Phones: which roster is on screen. Wider screens show both side by side.
  const [side, setSide] = useState<"a" | "b">("a");
  const results = useRef<HTMLDivElement>(null);
  const scrollToResult = useRef(false);
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
  const shape = shapeLabel(sel);

  const evaluate = () => {
    if (!canEvaluate(sel, maxPerSide)) return;
    setShowStale(false);
    scrollToResult.current = true;
    startTransition(() => router.push(withTeam(`/trades?${toQuery(sel)}`, team), { scroll: false }));
  };

  // Arriving on a link that already carries an evaluation (from Discovery, My Team Value or a shared URL): show the verdict,
  // unless the browser restored a scroll position (reload).
  const arrivedEvaluated = useRef(evaluatedKey !== null && evaluatedKey === initKey);
  useEffect(() => {
    if (arrivedEvaluated.current && window.scrollY < 8) results.current?.scrollIntoView({ block: "start" });
  }, []);

  // Once the requested evaluation has rendered, bring it into view (matters on phones, where it is below the builder).
  useEffect(() => {
    if (!scrollToResult.current || !current || pending) return;
    scrollToResult.current = false;
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    results.current?.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
  }, [current, pending]);

  // Named sides, never "you": anyone in the league may be analyzing either team.
  const giveLabel = `${teamA?.name ?? "Team A"} gives`;
  const getLabel = `${teamB?.name ?? "Team B"} gives`;
  const ready = issues.length === 0;

  return (
    <div className={`stack ${styles.builder}`}>
      <Section title="Build a trade" subtitle="Pick who each team sends, then evaluate">
        <div className={styles.sideSwitch} role="group" aria-label="Roster shown">
          <button type="button" className={styles.sideTab} data-side="a" aria-pressed={side === "a"} onClick={() => setSide("a")}>
            <span className={styles.sideTabRole}>Team A sends</span>
            <span className={styles.sideTabName}>{teamA?.name ?? "Team A"}</span>
            <span className={styles.sideTabCount}>{sel.a.length}</span>
          </button>
          <button type="button" className={styles.sideTab} data-side="b" aria-pressed={side === "b"} onClick={() => setSide("b")}>
            <span className={styles.sideTabRole}>Team B sends</span>
            <span className={styles.sideTabName}>{teamB?.name ?? "Team B"}</span>
            <span className={styles.sideTabCount}>{sel.b.length}</span>
          </button>
        </div>
        <div className={styles.sides}>
          {teamA && (
            <RosterPicker role="Team A sends" className={styles.sideA} active={side === "a"} team={teamA} teams={teams} picked={sel.a} maxPerSide={maxPerSide}
              onTeam={(id) => setSel(changeTeam(sel, teams, "a", id))}
              onToggle={(id) => setSel(togglePlayer(sel, "a", id, maxPerSide))} />
          )}
          <span className={styles.swap} aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h11l-3-3" /><path d="M15 12H4l3 3" /></svg>
          </span>
          {teamB && (
            <RosterPicker role="Team B sends" className={styles.sideB} active={side === "b"} team={teamB} teams={teams} picked={sel.b} maxPerSide={maxPerSide}
              onTeam={(id) => setSel(changeTeam(sel, teams, "b", id))}
              onToggle={(id) => setSel(togglePlayer(sel, "b", id, maxPerSide))} />
          )}
        </div>
      </Section>

      {issues.filter((p) => p.code === "same_team" || p.code === "missing_team").map((p) => (
        <Notice key={p.code} tone="caution">{p.text}</Notice>
      ))}
      {previews.map(({ team, pv }) => (pv.overCapacityBy > 0 || pv.positionsOver.length > 0) && (
        <Notice key={team.team_id} tone="caution" title={`${team.name}: roster limits`}>
          {pv.overCapacityBy > 0 && <>{pv.activeAfter} active players for {pv.capacity} active spots, so at least {pv.overCapacityBy} forced
            drop{pv.overCapacityBy > 1 ? "s" : ""}. </>}
          {pv.positionsOver.map((x) => <span key={x.position}>{x.count} {x.position} over ESPN&apos;s limit of {x.limit}. </span>)}
          The evaluator chooses the drops exactly (the legal removal that keeps the most projected starting points).
        </Notice>
      ))}

      <div className={styles.bar} role="region" aria-label="Trade summary">
        <div className={styles.barInner}>
          <div className={styles.summary}>
            <span className={styles.counts} aria-live="polite">{giveLabel} {sel.a.length} <i aria-hidden="true">·</i> {getLabel} {sel.b.length}</span>
            {(aOut.length > 0 || bOut.length > 0) && (
              <span className={styles.picks} title={`${teamA?.name ?? "Team A"} sends ${aOut.map((p) => p.name).join(", ") || "nobody"}; ${teamB?.name ?? "Team B"} sends ${bOut.map((p) => p.name).join(", ") || "nobody"}`}>
                <span className={styles.pickA}>{aOut.map((p) => p.name ?? `#${p.espn_player_id}`).join(", ") || "Nobody yet"}</span>
                <span className={styles.pickArrow} aria-hidden="true">⇄</span>
                <span className={styles.pickB}>{bOut.map((p) => p.name ?? `#${p.espn_player_id}`).join(", ") || "Nobody yet"}</span>
              </span>
            )}
            <span className={styles.meta}>
              {ready ? <span>{shape}</span> : sel.ta !== sel.tb && <span className={styles.hint}>{issues[0].text}</span>}
              {current && !pending && <Badge value="Result below matches this selection" tone="good" />}
              {!current && evaluatedKey !== null && !pending && <Badge value="Selection changed since the last result" tone="warn" />}
            </span>
          </div>
          <div className={styles.controls}>
            <label className={styles.week}>From week
              <select value={sel.sw ?? ""} onChange={(e) => setSel(setStartWeek(sel, e.target.value === "" ? null : Number(e.target.value)))}>
                <option value="">auto ({firstWeek})</option>
                {/* Weeks that have already kicked off are not offered: the API rejects them. */}
                {Array.from({ length: finalWeek - firstWeek + 1 }, (_, i) => firstWeek + i).map((w) => <option key={w} value={w}>{w}</option>)}
              </select>
            </label>
            <button type="button" className={styles.ghost} onClick={() => setSel(clearPicks(sel))} disabled={sel.a.length + sel.b.length === 0}>Clear</button>
            <button type="button" className={styles.evaluate} onClick={evaluate} disabled={pending || !ready}>
              {pending ? "Evaluating…" : "Evaluate"}
            </button>
          </div>
        </div>
      </div>

      <div ref={results} className={styles.results}>
        {pending && (
          <div className="stack" role="status" aria-busy="true">
            <p className={styles.pendingNote}>Evaluating {shape}: rebuilding both teams&apos; optimal lineups for every remaining week…</p>
            <SkeletonStatStrip n={4} />
            <Section title={<Skeleton w={160} h={14} />}><Skeleton h={160} /></Section>
          </div>
        )}
        {!pending && evaluatedKey === null && (
          <Section>
            <EmptyState title="No trade evaluated yet"
              action={<TeamLink href="/trades/discover">Try trade discovery</TeamLink>}>
              Pick players from each roster, then evaluate. Both teams&apos; best lineups are rebuilt for every remaining week, so the result is
              the change in projected starting points: there is no trade grade and no acceptance probability. Not sure what to offer? Discovery
              searches for trades that improve your lineup.
            </EmptyState>
          </Section>
        )}
        {!pending && current && children}
        {!pending && !current && evaluatedKey !== null && (
          <Notice tone="info" title="The selection changed since the last evaluation, so that result is hidden.">
            Press Evaluate to analyze the current {shape}.{" "}
            <button type="button" className={styles.ghost} onClick={() => setShowStale(!showStale)}>
              {showStale ? "Hide" : "Show"} the previous result (a different trade)
            </button>
          </Notice>
        )}
        {!pending && !current && showStale && <div className={styles.previous} aria-label="Previous result for a different selection">{children}</div>}
      </div>
    </div>
  );
}
