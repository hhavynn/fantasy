// Counteroffers (#214) for the trade analyzer: the POST /api/trades/counteroffers contract and the pure helpers the
// panel renders with. Every number is the backend's exact two-team evaluation; nothing here scores, weights or
// estimates acceptance.

import { plural } from "./plural.ts";
import { toQuery, type Selection } from "./trade-builder.ts";

export const MAX_LOCKS = 20;

export type CounterofferEdit = { kind: "add" | "remove" | "swap"; team_id: number; out: number | null; in: number | null };

export type CounterofferSide = {
  ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null;
  known_ros_delta: number; projection_complete: boolean; weeks_incomplete: number[];
  forced_drops: number[]; change_vs_original: number | null;
};

export type Counteroffer = {
  key: string; team_a_sends: number[]; team_b_sends: number[]; edits: CounterofferEdit[];
  team_a: CounterofferSide; team_b: CounterofferSide; min_side_ros_delta: number; combined_ros_delta: number;
  improves: { team_a: boolean; team_b: boolean }; consistent_with_manual_evaluator: boolean;
  analyzer_link: { ta: number; tb: number; a: number[]; b: number[] };
};

export type CounterofferStatus = "found" | "none_better" | "no_legal_edit" | "none_within_budget" | "original_unknown";

export type CounterofferSearch = {
  packages_evaluated: number; packages_not_evaluated: number; termination: "all_candidates_evaluated" | "work_budget" | "time_cap";
  exhaustive: boolean; work_spent: number; work_budget: number; stop_reason: string | null; scope: string;
  equivalent_hidden?: number; inconsistent_dropped?: number;
};

export type CounterofferResponse = {
  contract: "trade-counteroffers/v1";
  status: CounterofferStatus; reason: string | null; note: string;
  team_a_id: number; team_b_id: number;
  original: { team_a_sends: number[]; team_b_sends: number[]; status: string; team_a?: CounterofferSide;
    team_b?: CounterofferSide; min_side_ros_delta?: number };
  results: Counteroffer[];
  search: CounterofferSearch;
  constraints: { locked: number[]; excluded: number[]; positions: string[] | null; max_added_per_side: number; limit: number };
  names: Record<string, { name: string | null; position: string | null; pro_team: string | null }>;
  team_names: Record<string, string | null>;
  data_version: string | null;
};

/** The players to keep in the deal: ids from the URL that are in the offer, once each, in order, capped. */
export function parseLocks(raw: number[], offer: readonly number[]): number[] {
  const inOffer = new Set(offer);
  return [...new Set(raw.filter((id) => inOffer.has(id)))].sort((p, q) => p - q).slice(0, MAX_LOCKS);
}

/** The analyzer URL for this selection with the counteroffer panel open and these players kept in the deal. */
export function counterofferHref(s: Selection, locks: readonly number[]): string {
  const q = new URLSearchParams(toQuery(s));
  q.set("co", "1");
  [...locks].sort((p, q2) => p - q2).forEach((id) => q.append("lock", String(id)));
  return `/trades?${q.toString()}`;
}

/** The same panel with `id` kept in the deal or released. */
export function toggleLockHref(s: Selection, locks: readonly number[], id: number): string {
  return counterofferHref(s, locks.includes(id) ? locks.filter((x) => x !== id) : [...locks, id]);
}

/** The analyzer URL for a counteroffer (the builder loads it, ready to edit or evaluate). */
export function counterofferAnalyzerHref(c: Counteroffer): string {
  return `/trades?${toQuery({ ta: c.analyzer_link.ta, tb: c.analyzer_link.tb, a: c.analyzer_link.a, b: c.analyzer_link.b, sw: null })}`;
}

const nameOf = (r: Pick<CounterofferResponse, "names">, id: number | null) =>
  (id !== null && r.names[String(id)]?.name) || `Player ${id}`;
const teamOf = (r: Pick<CounterofferResponse, "team_names">, id: number) => r.team_names[String(id)] || `Team ${id}`;

/** One edit in words: who changes what, relative to the original offer. */
export function editText(r: Pick<CounterofferResponse, "names" | "team_names">, e: CounterofferEdit): string {
  const team = teamOf(r, e.team_id);
  if (e.kind === "add") return `${team} also sends ${nameOf(r, e.in)}`;
  if (e.kind === "remove") return `${team} keeps ${nameOf(r, e.out)}`;
  return `${team} sends ${nameOf(r, e.in)} instead of ${nameOf(r, e.out)}`;
}

/** The heading of the panel's answer. */
export function statusTitle(s: CounterofferStatus, n: number): string {
  switch (s) {
    case "found": return `${n} ${plural(n, "counteroffer helps", "counteroffers help")} the side this offer treats worse`;
    case "none_better": return "No counteroffer within these limits does better";
    case "no_legal_edit": return "No counteroffer can be built under these limits";
    case "none_within_budget": return "None found before the search limit";
    case "original_unknown": return "This offer's impact is unknown, so there is nothing exact to improve on";
  }
}

/** What the search covered, in one sentence. */
export function coverageText(s: CounterofferSearch): string {
  const done = `${s.packages_evaluated} ${plural(s.packages_evaluated, "package")} evaluated exactly`;
  if (s.exhaustive) return `${done}: every ${s.scope} within the limits.`;
  return `${done}; ${s.packages_not_evaluated} more not evaluated (${s.stop_reason ?? "search limit reached"}).`;
}
