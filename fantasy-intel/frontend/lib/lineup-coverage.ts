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
