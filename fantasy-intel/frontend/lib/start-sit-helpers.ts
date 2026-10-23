export function formatGain(gain: number | null | undefined): string {
  if (gain === null || gain === undefined) return "—";
  if (gain > 0) return `+${gain.toFixed(1)} pts`;
  return `${gain.toFixed(1)} pts`;
}

export function actionabilityBadgeTone(actionability: string): "info" | "warn" | "accent" | "mute" {
  switch (actionability) {
    case "ACTIONABLE":
      return "accent";
    case "LOCKED":
      return "mute";
    case "UNCERTAIN":
    case "IR_BLOCKED":
      return "warn";
    case "NO_CHANGE":
    default:
      return "info";
  }
}

export function lockStatusBadge(
  status: string,
  kickoffUtc: string | null,
  hasFinalScore?: boolean,
  homeScore?: number | null,
  awayScore?: number | null,
): { label: string; tone: "mute" | "info" | "warn" } {
  switch (status) {
    case "LOCKED":
      if (hasFinalScore && homeScore !== null && homeScore !== undefined && awayScore !== null && awayScore !== undefined) {
        return { label: `Final (${awayScore}-${homeScore})`, tone: "mute" };
      }
      return { label: "Locked", tone: "mute" };
    case "UNLOCKED":
      if (kickoffUtc) {
        try {
          const d = new Date(kickoffUtc);
          const timeStr = d.toLocaleTimeString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
          return { label: `Kickoff: ${timeStr}`, tone: "info" };
        } catch {
          return { label: "Upcoming", tone: "info" };
        }
      }
      return { label: "Upcoming", tone: "info" };
    case "BYE":
      return { label: "NFL Bye", tone: "mute" };
    case "TEAMLESS":
      return { label: "Free Agent", tone: "mute" };
    case "UNKNOWN":
    default:
      return { label: "Kickoff Unknown", tone: "warn" };
  }
}

export interface TotalDisplay {
  /** The number to show, or null when there is nothing to show. */
  value: number | null;
  /** True when `value` is the known part of an incomplete projection, not a full total. */
  partial: boolean;
}

/**
 * A lineup total for display. A full total is used only when every starter is projected;
 * otherwise the known partial sum is returned and flagged, so a partial is never shown as complete.
 * `hasLineup` false (no submitted lineup) yields no value at all.
 */
export function lineupTotal(
  hasLineup: boolean,
  complete: boolean,
  total: number | null | undefined,
  known: number | null | undefined,
): TotalDisplay {
  if (!hasLineup) return { value: null, partial: false };
  if (complete && total !== null && total !== undefined) return { value: total, partial: false };
  return { value: known ?? null, partial: true };
}

/** Start/Sit's projection source label when the week has no projection at all (backend start_sit.NO_PROJECTIONS). */
export const NO_PROJECTIONS = "Unavailable";
/** What a total shows (as a `Missing` reason) when the week has no projection. */
export const NO_PROJECTIONS_TEXT = "No projections for this week";

/**
 * True when the week has nothing to recommend from: the backend says no projection exists, or no recommended starter has
 * one. A total is then missing, never a measured 0.0.
 */
export function noProjections(
  source: string | null | undefined,
  rows: readonly { player_id: number | null; projected_points: number | null }[],
): boolean {
  return source === NO_PROJECTIONS || !rows.some((r) => r.player_id !== null && r.projected_points !== null);
}

/**
 * A Start/Sit total (submitted or recommended) for display. When the week has no projection (`noProjections`) there is
 * no total at all, never a 0.0 or a partial 0.0; otherwise lineupTotal's rule.
 */
export function projectedTotal(
  hasLineup: boolean,
  noProj: boolean,
  complete: boolean,
  total: number | null | undefined,
  known: number | null | undefined,
): TotalDisplay {
  return noProj ? { value: null, partial: false } : lineupTotal(hasLineup, complete, total, known);
}

/**
 * The page's "· … projections" meta text: whose projections, or that the week has none. `noProj` (this team's lineup
 * has no projected starter while the league's source has numbers) keeps the source and says so for this team.
 */
export function projectionsMeta(source: string, noProj = false): string {
  if (source === NO_PROJECTIONS) return "no projections";
  return noProj ? `${source} projections, none for this team this week` : `${source} projections`;
}

export type VerdictTone = "info" | "caution" | "positive";

export interface Verdict {
  tone: VerdictTone;
  headline: string;
  detail: string;
}

/** Inputs the verdict needs; plain values taken from the API summary and decisions. */
export interface VerdictInput {
  status: string;
  startersCount: number;
  projectedGain: number | null;
  missingProjections: number;
  scoringPeriod: number;
  moves: number;
  actionableMoves: number;
  /** Starting slots of the recommended lineup with no player (see `emptyStartingSlots`). */
  emptySlots: string[];
}

/** Slot names of recommended-lineup rows that hold no player: a plain "no player in a starting slot". */
export function emptyStartingSlots(rows: readonly { slot: string; player_id: number | null }[]): string[] {
  return rows.filter((r) => r.player_id === null).map((r) => r.slot);
}

const EMPTY_REASON = "no eligible rostered player is available";

/**
 * The one-sentence answer the Start/Sit page opens with. It only restates backend facts
 * (status, gain, move counts); it never computes a gain of its own.
 */
export function startSitVerdict(v: VerdictInput): Verdict {
  const empty = v.emptySlots.length > 0;
  const emptyList = v.emptySlots.join(", ");
  if (v.status === "future_preview") {
    return {
      tone: empty ? "caution" : "info",
      headline: `Week ${v.scoringPeriod} preview: no submitted lineup to compare`,
      detail: `This is the best projected lineup for the current roster. Moves are not actionable until the current week ends.${empty ? ` ${emptyList} stays empty: ${EMPTY_REASON}.` : ""}`,
    };
  }
  if (v.status === "incomplete_data") {
    return {
      tone: "caution",
      headline: "Projections are incomplete, so no gain is claimed",
      detail: `${v.missingProjections} ${v.missingProjections === 1 ? "player is" : "players are"} missing a projection. Totals below are marked as partial.`,
    };
  }
  if (v.startersCount === 0) {
    return {
      tone: "info",
      headline: "No submitted lineup for this week",
      detail: "There is nothing to compare the recommendation against.",
    };
  }
  if (v.moves === 0) {
    if (empty) {
      return {
        tone: "caution",
        headline: `Best available lineup leaves ${emptyList} empty: ${EMPTY_REASON}`,
        detail: "No swap improves the projected total under kickoff locks.",
      };
    }
    return v.status === "all_locked"
      ? { tone: "info", headline: "Every relevant game has kicked off", detail: "No lineup changes are possible under kickoff locks." }
      : { tone: "positive", headline: "The submitted lineup is optimal for the projections", detail: "No swap improves the projected total under kickoff locks." };
  }
  const gain = v.projectedGain !== null && v.projectedGain > 0 ? ` for ${formatGain(v.projectedGain)}` : "";
  const headline = `${v.moves} recommended ${v.moves === 1 ? "move" : "moves"}${gain}`;
  const detail = v.actionableMoves === v.moves
    ? "All can be made now."
    : v.actionableMoves === 0
      ? "None can be made yet: see the badge on each move."
      : `${v.actionableMoves} can be made now.`;
  return {
    tone: empty ? "caution" : "info",
    headline,
    detail: empty ? `${detail} ${emptyList} stays empty: ${EMPTY_REASON}.` : detail,
  };
}

/** Text badge for a decision's actionability class. */
export function actionabilityBadge(actionability: string): { label: string; tone: "good" | "warn" | "info" | "" } {
  const label = actionability === "IR_BLOCKED"
    ? "IR blocked"
    : actionability.replaceAll("_", " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
  switch (actionabilityBadgeTone(actionability)) {
    case "accent":
      return { label, tone: "good" };
    case "warn":
      return { label, tone: "warn" };
    case "mute":
      return { label, tone: "" };
    default:
      return { label, tone: "info" };
  }
}

export interface ChangeInput {
  slot: string;
  playerId: number | null;
  isCurrentlyStarting: boolean;
  currentSlot: string | null;
}

/**
 * How a recommended-lineup row differs from the submitted lineup. Recommended slot ids can differ
 * from the submitted ones only by relabelling (a WR sitting in FLEX), which is not a roster change.
 */
export function slotChange(r: ChangeInput): { kind: "empty" | "same" | "relabel" | "promote"; text: string } {
  if (r.playerId === null) return { kind: "empty", text: "Empty slot" };
  if (!r.isCurrentlyStarting) {
    const from = r.currentSlot && r.currentSlot !== "BE" ? r.currentSlot : "bench";
    return { kind: "promote", text: from === "bench" ? "Promoted from bench" : `Promoted from ${from}` };
  }
  if (r.currentSlot && r.currentSlot !== r.slot) return { kind: "relabel", text: `Starts, moves from ${r.currentSlot}` };
  return { kind: "same", text: "Starts, no change" };
}
