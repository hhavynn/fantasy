// Starting slots no engine fills (individual defensive players, punter, head coach; issue #253 G2-G4). The backend
// leaves them out of every lineup, so a total that covers the other slots is partial, never complete, and roster
// legality in such a league is unverified, never "legal". This module only words what the backend reports.

/** Start/Sit `summary.lineup_coverage` (absent in a league without such slots). */
export interface LineupCoverage {
  status: "partial";
  unsupported_slots: string[];
  /** Starters kept in those slots in both lineups (never moved, never in a total). */
  held_starters: { player_id: number; name: string | null; position: string | null; slot: string | null; projected_points: number | null }[];
  /** Starters the provider stores but Fantasy Intel does not (Sleeper IDP players). */
  starters_not_in_player_data: number;
  reason: string;
}

/** trades.roster_legality: present only when a roster move cannot be verified as legal. */
export interface RosterLegality {
  status: "unverified";
  code: string;
  unsupported_slots: string[];
  players_missing: number;
  reason: string;
}

/** "LB, DL" */
export function slotList(slots: readonly string[] | null | undefined): string {
  return (slots ?? []).join(", ");
}

/** The note under a total that leaves out unsupported slots, or null when nothing is left out. */
export function partialSlotsNote(slots: readonly string[] | null | undefined): string | null {
  if (!slots || slots.length === 0) return null;
  return `*Partial: leaves out ${slotList(slots)} (no engine fills ${slots.length === 1 ? "this slot" : "these slots"})`;
}

/** A Notice title for a league with unsupported slots. */
export function unsupportedSlotsTitle(slots: readonly string[]): string {
  return `${slotList(slots)}: ${slots.length === 1 ? "a slot" : "slots"} no engine fills`;
}

/** What a roster legality flag shows: "Unverified" with the reason, or null when legality is checked normally. */
export function legalityLabel(legality: RosterLegality | null | undefined): { label: string; reason: string } | null {
  return legality ? { label: "Unverified", reason: legality.reason } : null;
}

/** The active-roster line ("14 → 15 of 17"), or "unverified" when the count leaves players out (legality present). */
export function activeRosterText(
  ro: { active_before: number | null; active_after: number | null; active_capacity: number },
  legality: RosterLegality | null | undefined,
): string {
  if (legality || ro.active_before === null || ro.active_after === null) return "unverified";
  return `${ro.active_before} → ${ro.active_after} of ${ro.active_capacity}`;
}

/** The pre-drop capacity facts, or null when the count is unverified: never "within capacity" on an unknown count. */
export function capacityFacts(
  iss: { active_before_drops: number | null; active_capacity: number; over_capacity_by: number | null },
  legality: RosterLegality | null | undefined,
): { used: number; capacity: number; over: number } | null {
  if (legality || iss.active_before_drops === null || iss.over_capacity_by === null) return null;
  return { used: iss.active_before_drops, capacity: iss.active_capacity, over: iss.over_capacity_by };
}

/** "No mandatory drop" only when the count is verified; otherwise none was found on an unverified count. */
export function noDropText(legality: RosterLegality | null | undefined): string {
  return legality ? "none found on an unverified roster count" : "none";
}

/** A forced or suggested drop's lineup cost: exact, known-only, or not valued at all (never a 0.0 for an unvalued player). */
export function dropCostText(full: number | null, known: number | null): string {
  if (full !== null) return full.toFixed(1);
  if (known !== null) return `${known.toFixed(1)} known only`;
  return "not valued (a player no engine values)";
}
