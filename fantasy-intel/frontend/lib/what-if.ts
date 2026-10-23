// What-if lineups on Start/Sit (issue #171). The backend (GET /api/start-sit/what-if, contract start-sit-what-if/v1)
// owns every rule: slot eligibility, kickoff locks, IR and taxi players, duplicates, and the totals. This module only
// reads its answer and builds URLs: the scenario lives in the `whatif` query parameter, so a view can be shared and
// the browser's back button undoes a move. Nothing here talks to ESPN or Sleeper.

import type { LineupCoverage } from "./lineup-coverage.ts";
import type { ProjectionOrigin } from "./projection-source.ts";
import type { ProvenancePayload } from "./signal-provenance.ts";

export const WHATIF_PARAM = "whatif";
/** The backend's own bound (lineup_what_if.MAX_PARAM_LEN); a longer value is never sent. */
export const WHATIF_MAX_LEN = 400;
const WHATIF_CHARS = /^[A-Za-z0-9_/.,~-]+$/;

export type WhatIfTotals = {
  total: number | null;
  known_total: number;
  /** False whenever the total is partial, including slots no engine fills (then see `projections_complete`). */
  complete: boolean;
  projections_complete?: boolean;
  unsupported_slots?: string[];
  missing_projection_ids: number[];
  starters: number;
  starters_with_projection: number;
};

export type WhatIfOption = {
  player_id: number;
  from_slot_id: string | null;
  displaced_player_id: number | null;
  displaced_to_slot_id: string | null;
  total: number | null;
  known_total: number;
  /** False whenever the total is partial, including slots no engine fills (then see `projections_complete`). */
  complete: boolean;
  projections_complete?: boolean;
  unsupported_slots?: string[];
  delta: number | null;
  known_delta: number;
  /** The scenario with this move added, or null at the move limit. */
  whatif: string | null;
};

export type WhatIfSlot = {
  slot_id: string;
  slot: string;
  label: string;
  is_flex: boolean;
  eligible_positions: string[];
  locked: boolean;
  lock_reason: string | null;
  recommended_player_id: number | null;
  scenario_player_id: number | null;
  changed: boolean;
  options: WhatIfOption[];
  blocked: { player_id: number; code: string; message: string }[];
};

export type WhatIfPlayer = {
  player_id: number;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  projected_points: number | null;
  projection_origin: ProjectionOrigin | null;
  lock_status: string;
  kickoff_utc: string | null;
  opponent: string | null;
  is_home: boolean | null;
  injury_status: string | null;
  nfl_ineligible: boolean;
  roster_spot: "starter" | "bench" | "ir" | "taxi";
  recommended_slot_id: string | null;
  scenario_slot_id: string | null;
  movable: boolean;
  restriction: string | null;
  restriction_reason: string | null;
};

export type WhatIfMove = {
  index: number;
  slot_id: string;
  player_id: number;
  from_slot_id: string | null;
  displaced_player_id: number | null;
  displaced_to_slot_id: string | null;
  removable: boolean;
  whatif_without: string | null;
  blocked_by: string | null;
};

export type WhatIfProblem = { code: string; slot_id: string | null; player_id: number | null; message: string };

export type WhatIfResponse = {
  contract: "start-sit-what-if/v1";
  league: { id: number; name: string; season: number; current_week: number; final_scoring_period: number };
  team: { id: number; name: string; abbrev: string; is_mine: boolean };
  scoring_period: number;
  is_future_preview: boolean;
  projection_source: string;
  key: string;
  scenario: { requested: boolean; applied: boolean; whatif: string | null; moves: WhatIfMove[]; move_limit: number };
  errors: WhatIfProblem[];
  changed_slot_ids: string[];
  slots: WhatIfSlot[];
  players: WhatIfPlayer[];
  totals: {
    recommended: WhatIfTotals; scenario: WhatIfTotals; delta: number | null; known_delta: number; complete: boolean;
    /** Slots no engine fills: both totals are partial, the delta stays exact (issue #253 G3). */
    lineup_coverage?: LineupCoverage;
  };
  lineup_lock: {
    rule: { value: string | null; display: string | null; reason: string | null };
    engine_status: "handled" | "unsupported" | "unverified" | null;
    engine_detail: string | null;
  };
  freshness: { lineup_synced_at: string | null; projection_synced_at: string | null; nfl_schedule_synced_at: string | null };
  schedule_warnings: string[];
  limitations: string[];
  /** signal-provenance.v1: the lineup, the projections (coverage: this what-if's starters) and injury reports. */
  provenance?: ProvenancePayload | null;
};

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

/**
 * The `whatif` value to forward to the backend: `value` when it is plausibly a scenario (bounded length, scenario
 * characters only), `invalid` when it is clearly not, `none` when absent. The backend validates the rest (shape, slots,
 * players on this team) and never trusts it either.
 */
export function readWhatIf(sp: SP): { kind: "none" } | { kind: "value"; value: string } | { kind: "invalid" } {
  const raw = first(sp[WHATIF_PARAM]);
  if (raw === undefined || raw === "") return { kind: "none" };
  if (raw.length > WHATIF_MAX_LEN || !WHATIF_CHARS.test(raw)) return { kind: "invalid" };
  return { kind: "value", value: raw };
}

/** The page URL with `whatif` set (or removed with null); every other parameter is kept. */
export function whatIfHref(pathname: string, sp: SP | URLSearchParams, whatif: string | null): string {
  const entries: [string, string][] = sp instanceof URLSearchParams
    ? [...sp.entries()]
    : Object.entries(sp).flatMap(([k, v]) => (v === undefined ? [] : (Array.isArray(v) ? v : [v]).map((x) => [k, x] as [string, string])));
  const qs = new URLSearchParams(entries.filter(([k]) => k !== WHATIF_PARAM));
  if (whatif) qs.set(WHATIF_PARAM, whatif);
  const s = qs.toString();
  return s ? `${pathname}?${s}` : pathname;
}

/** A backend path with the scenario added (or left out for null). */
export function withWhatIf(apiPath: string, whatif: string | null): string {
  if (!whatif) return apiPath;
  const [base, query = ""] = apiPath.split("?");
  const qs = new URLSearchParams(query);
  qs.set(WHATIF_PARAM, whatif);
  return `${base}?${qs}`;
}

/** The other query parameters as hidden form fields, so the no-JavaScript form keeps the team and week. */
export function carriedParams(sp: SP): [string, string][] {
  return Object.entries(sp)
    .filter(([k]) => k !== WHATIF_PARAM)
    .flatMap(([k, v]) => (v === undefined ? [] : (Array.isArray(v) ? v : [v]).map((x) => [k, x] as [string, string])));
}

/** A total as shown: the full total, or the known-only sum marked partial (a missing projection is unknown, not 0). */
export function shownTotal(t: WhatIfTotals): { value: number; partial: boolean } {
  return t.total !== null ? { value: t.total, partial: false } : { value: t.known_total, partial: true };
}

/** The change against the recommendation: the full delta, or the known-only delta marked partial. */
export function shownDelta(full: number | null, known: number): { value: number; partial: boolean } {
  return full !== null ? { value: full, partial: false } : { value: known, partial: true };
}

const fmt = (v: number) => v.toFixed(1);
const signedText = (v: number) => (Math.abs(v) < 0.05 ? "0.0" : `${v > 0 ? "+" : "−"}${fmt(Math.abs(v))}`);

/** One option in a slot's picker: "Wally Bench (WR, NYG) · 10.0 · −1.0 vs best", plus where the occupant goes. */
export function optionLabel(o: WhatIfOption, players: Map<number, WhatIfPlayer>, slots: Map<string, WhatIfSlot>): string {
  const p = players.get(o.player_id);
  const who = `${p?.name ?? `Player ${o.player_id}`} (${[p?.position, p?.pro_team ?? "FA"].filter(Boolean).join(", ")})`;
  const proj = p?.projected_points === null || p?.projected_points === undefined ? "no projection" : fmt(p.projected_points);
  const d = shownDelta(o.delta, o.known_delta);
  const vs = `${signedText(d.value)}${d.partial ? " known-only" : ""} vs best`;
  const from = o.from_slot_id ? ` · swaps with ${slots.get(o.from_slot_id)?.label ?? o.from_slot_id}` : "";
  return `${who} · ${proj} · ${vs}${from}`;
}

/** A move in plain words: "Wally Bench into FLEX; Walt Slot to the bench". */
export function moveText(m: WhatIfMove, players: Map<number, WhatIfPlayer>, slots: Map<string, WhatIfSlot>): string {
  const name = (id: number | null) => (id === null ? "nobody" : players.get(id)?.name ?? `Player ${id}`);
  const slot = (id: string | null) => (id === null ? "the bench" : slots.get(id)?.label ?? id);
  const into = `${name(m.player_id)} into ${slot(m.slot_id)}`;
  if (m.displaced_player_id === null) return into;
  return `${into}; ${name(m.displaced_player_id)} to ${slot(m.displaced_to_slot_id)}`;
}

/** Short words for a restriction code, shown beside the player (the backend's sentence is the full explanation). */
export function restrictionLabel(code: string | null): string | null {
  switch (code) {
    case "LOCKED_PLAYER": return "Locked";
    case "RESERVED": return "Cannot start";
    default: return null;
  }
}
