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
}

/**
 * The one-sentence answer the Start/Sit page opens with. It only restates backend facts
 * (status, gain, move counts); it never computes a gain of its own.
 */
export function startSitVerdict(v: VerdictInput): Verdict {
  if (v.status === "future_preview") {
    return {
      tone: "info",
      headline: `Week ${v.scoringPeriod} preview: no submitted lineup to compare`,
      detail: "This is the best projected lineup for the current roster. Moves are not actionable until the current week ends.",
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
    return v.status === "all_locked"
      ? { tone: "info", headline: "Every relevant game has kicked off", detail: "No lineup changes are possible under kickoff locks." }
      : { tone: "positive", headline: "Your lineup is optimal for the projections", detail: "No swap improves the projected total under kickoff locks." };
  }
  const gain = v.projectedGain !== null && v.projectedGain > 0 ? ` for ${formatGain(v.projectedGain)}` : "";
  const headline = `${v.moves} recommended ${v.moves === 1 ? "move" : "moves"}${gain}`;
  const detail = v.actionableMoves === v.moves
    ? "All can be made now."
    : v.actionableMoves === 0
      ? "None can be made yet: see the badge on each move."
      : `${v.actionableMoves} can be made now.`;
  return { tone: "info", headline, detail };
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
