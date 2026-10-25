// Ask Fantasy Intel (issue #219): the ask/v1 card the backend returns (GET /api/ask, app/api/ask.py) and the pure
// helpers the /ask page renders it with. The backend parses the question with a deterministic grammar and copies every
// number from an existing analysis; this module never computes a number, only formats the ones it is given (missing
// stays missing) and builds links back to the page that shows the same view.

import { signalsOf, type ProvenancePayload, type SignalProvenance } from "./signal-provenance.ts";
import { parseTimestamp } from "./sync-status.ts";

export const CONTRACT = "ask/v1";
export const MAX_CHARS = 200;
export const PATH = "/ask";

export type AskStatus = "answered" | "clarify" | "abstain" | "unavailable";
export type AskValue = string | number | boolean | null;
export type AskSample = { value: number | null; n: number | null; of: number | null };
export type AskCell = AskValue | AskSample;
export type AskFact = { label: string; value: AskValue; unit: string | null };
export type AskColumn = { key: string; label: string; unit?: string };
export type AskRow = { cells: Record<string, AskCell>; player_id?: number | null; whatif?: string | null };
export type AskLink = { path: string; query: Record<string, string | number | (string | number)[]>; hash?: string };
export type AskOption = {
  player_id: number; name: string; position: string | null; pro_team: string | null; fantasy_team_name: string | null;
  ask: string;
};

export type AskFreshness = {
  data_through_week?: number | null;
  cutoffs?: Record<string, number | null>;
  computed_at?: string | null;
  espn_synced_at?: string | null;
  nfl_synced_at?: string | null;
  lineup_synced_at?: string | null;
  projection_synced_at?: string | null;
  nfl_schedule_synced_at?: string | null;
};

export type AskCard = {
  contract: string;
  status: AskStatus;
  question: string;
  intent: { id: string; label: string } | null;
  interpretation: string | null;
  answer: {
    headline: string; facts: AskFact[]; columns: AskColumn[]; rows: AskRow[]; truncated: boolean;
    status?: string; whatif?: string | null; available?: boolean;
  } | null;
  notes: string[];
  provenance: ProvenancePayload | null;
  freshness: AskFreshness | null;
  link: AskLink | null;
  clarify: { code: string; question: string; reason: string; options: AskOption[] } | null;
  abstain: { code: string | null; reason: string | null } | null;
  examples: string[];
};

/** Evidence for this answer only. Freshness is independent of provenance; never infer a source or a capture time.
 * Capture times already disclosed by the provenance badge are not listed a second time. */
export function answerEvidence(card: AskCard): {
  signals: SignalProvenance[]; week: number | null; timestamps: { label: string; value: string }[];
} {
  if (card.status !== "answered") return { signals: [], week: null, timestamps: [] };
  const signals = signalsOf(card.provenance);
  const freshness = card.freshness;
  const captured = new Set(signals.map((s) => parseTimestamp(s.captured_at)).filter((t) => t !== null));
  const fields = [
    ["computed_at", "Analysis computed"], ["espn_synced_at", "ESPN sync"], ["nfl_synced_at", "NFL sync"],
    ["lineup_synced_at", "Lineup sync"], ["projection_synced_at", "Projection sync"],
    ["nfl_schedule_synced_at", "NFL schedule sync"],
  ] as const;
  const timestamps = fields.flatMap(([key, label]) => {
    const value = freshness?.[key];
    const time = parseTimestamp(value);
    return value && time !== null && !captured.has(time) ? [{ label, value }] : [];
  });
  return { signals, week: freshness?.data_through_week ?? null, timestamps };
}

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

/** The question in the URL (`?q=`), trimmed; `tooLong` when it is past the backend's bound (the page says so and
 *  does not send it). */
export function readQuestion(sp: SP): { q: string; tooLong: boolean } {
  const q = (first(sp.q) ?? "").trim();
  return { q, tooLong: q.length > MAX_CHARS };
}

/** The backend path for a question. */
export function askPath(q: string): string {
  return `/api/ask?${new URLSearchParams({ q })}`;
}

/** This page with a question (an example or a clarification's re-ask), keeping the team in view. */
export function askHref(q: string, team: number | null): string {
  const qs = new URLSearchParams({ q });
  if (team !== null) qs.set("team", String(team));
  return `${PATH}?${qs}`;
}

/** The card's link as an in-app href: repeated params for lists, an optional #fragment. */
export function linkHref(link: AskLink): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(link.query)) {
    for (const x of Array.isArray(v) ? v : [v]) qs.append(k, String(x));
  }
  const query = qs.toString();
  return `${link.path}${query ? `?${query}` : ""}${link.hash ? `#${link.hash}` : ""}`;
}

export function isSample(c: AskCell): c is AskSample {
  return typeof c === "object" && c !== null && "value" in c;
}

/** A value as text with its unit, or null when it is missing (the page shows the missing marker, never 0). */
export function valueText(v: AskValue, unit?: string | null): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") {
    const n = Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 10 ? 2 : 1).replace(/\.?0+$/, "");
    if (unit === "%" || unit === "percent") return `${n}%`;
    if (unit === "pts") return `${n} pts`;
    return n;
  }
  return v;
}

/** "2 of 3 games" for a screener sample, or null when there is none. */
export function sampleText(s: AskSample): string | null {
  if (s.n === null || s.of === null) return null;
  return `${s.n} of ${s.of} ${s.of === 1 ? "game" : "games"}`;
}

/** Why a card has no answer, in words for the notice title. */
export function noAnswerTitle(card: AskCard): string {
  if (card.status === "unavailable") return "The data for this answer is not available";
  if (card.abstain?.code === "empty") return "Ask a question about this league";
  return "Ask can't answer that exactly";
}

/** The contract check the page makes before rendering a card. */
export function isCard(x: unknown): x is AskCard {
  return typeof x === "object" && x !== null && (x as { contract?: unknown }).contract === CONTRACT;
}
