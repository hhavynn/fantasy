// signal-provenance.v1: where each signal behind a headline recommendation comes from, how old it is and how complete.
// The backend twin (and the contract's full description) is backend/app/api/signal_provenance.py; keep them in step.
// The status is derived here, when the page renders, never read from the response: league responses are cached per
// data version, so a stored "current" would outlive its truth. Pure functions so node --test covers them.

import { STALE_HOURS, parseTimestamp } from "./sync-status.ts";

export const PROVENANCE_CONTRACT = "signal-provenance.v1";

export type SignalKey = "league" | "projections" | "injuries" | "usage" | "analysis";
export type SignalSource = "espn" | "sleeper" | "nflverse" | "fantasy_intel";

export type SignalCoverage = { available: number; total: number; label: string };

export type SignalProvenance = {
  signal: SignalKey | string;
  /** What the signal is, in plain words. */
  label: string;
  /** Who produced it; null when unknown. */
  source: SignalSource | string | null;
  /** ISO-8601 UTC capture time (a successful sync, a model run, an analysis); null when unknown, never "now". */
  captured_at: string | null;
  /** Opaque id of the immutable data read; compare for equality only. */
  snapshot_version: string | null;
  /** How many of the items the signal should cover it does; null when not measured. Missing is not 0. */
  coverage: SignalCoverage | null;
  /** Why the signal is missing or incomplete. */
  reason_unavailable: string | null;
  /** Set when the data is known to be superseded whatever its age (a newer sync failed, a re-synced roster). */
  stale_reason: string | null;
  stale_after_hours: number;
};

export type ProvenancePayload = { contract: string; signals: SignalProvenance[] };

export type ProvenanceStatus = "current" | "partial" | "stale" | "unknown" | "unavailable";

/** Worst first: a summary of several signals shows the first status any of them has. */
export const STATUS_ORDER: readonly ProvenanceStatus[] = ["unavailable", "unknown", "stale", "partial", "current"];

export const STATUS_LABEL: Record<ProvenanceStatus, string> = {
  current: "Current", partial: "Partial", stale: "Stale", unknown: "Age unknown", unavailable: "Unavailable",
};

const BADGE_LEAD: Record<ProvenanceStatus, string> = {
  current: "Current data", partial: "Partial data", stale: "Stale data", unknown: "Data age unknown", unavailable: "Data unavailable",
};

const SOURCE_NAME: Record<string, string> = {
  espn: "ESPN", sleeper: "Sleeper", nflverse: "nflverse", fantasy_intel: "Fantasy Intel",
};

/** The display name of a source key; an unknown or missing source is said to be unknown. */
export function sourceName(source: string | null | undefined): string {
  if (!source) return "Unknown source";
  return SOURCE_NAME[source] ?? source;
}

/** Hours since capture (0 for a time slightly in the future: clock skew), or null when the time is unknown. */
export function ageHours(s: Pick<SignalProvenance, "captured_at">, now: number = Date.now()): number | null {
  const t = parseTimestamp(s.captured_at);
  return t === null ? null : Math.max(0, (now - t) / 36e5);
}

/** "under 1h ago", "5h ago", "3d ago"; "age unknown" without a time. Never "live" or "just now". */
export function ageText(hours: number | null): string {
  if (hours === null) return "age unknown";
  if (hours < 1) return "under 1h ago";
  if (hours < 48) return `${Math.floor(hours)}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** The derived status of one signal at `now` (precedence in backend/app/api/signal_provenance.py). */
export function provenanceStatus(s: SignalProvenance, now: number = Date.now()): ProvenanceStatus {
  const cov = s.coverage;
  const measured = !!cov && cov.total > 0;
  if (s.reason_unavailable && !(measured && cov!.available > 0)) return "unavailable";
  if (measured && cov!.available === 0) return "unavailable";
  const hours = ageHours(s, now);
  if (hours === null) return "unknown";
  const limit = Number.isFinite(s.stale_after_hours) ? s.stale_after_hours : STALE_HOURS;
  if (s.stale_reason || hours > limit) return "stale";
  if ((measured && cov!.available < cov!.total) || s.reason_unavailable) return "partial";
  return "current";
}

/** "8 of 9 starters with a projection"; null when not measured. */
export function coverageText(c: SignalCoverage | null | undefined): string | null {
  return c ? `${c.available} of ${c.total} ${c.label}` : null;
}

/** The signals of a payload, or an empty list for a missing or unrecognised one (an older backend). */
export function signalsOf(p: ProvenancePayload | null | undefined): SignalProvenance[] {
  return p && p.contract === PROVENANCE_CONTRACT && Array.isArray(p.signals) ? p.signals : [];
}

/** Several payloads (one per endpoint a section read) as one list, the first of each signal + source kept. */
export function mergeSignals(...payloads: (ProvenancePayload | null | undefined)[]): SignalProvenance[] {
  const seen = new Set<string>();
  const out: SignalProvenance[] = [];
  for (const s of payloads.flatMap(signalsOf)) {
    const key = `${s.signal}|${s.source ?? ""}|${s.label}`;
    if (!seen.has(key)) { seen.add(key); out.push(s); }
  }
  return out;
}

export type ProvenanceSummary = {
  status: ProvenanceStatus;
  /** The short line on the badge: the status and what drives it. */
  text: string;
  rows: { signal: SignalProvenance; status: ProvenanceStatus; age: string; coverage: string | null }[];
};

/** One status for a page's signals: the worst any of them has, explained by the signal that has it. */
export function summarizeProvenance(signals: readonly SignalProvenance[], now: number = Date.now()): ProvenanceSummary | null {
  if (signals.length === 0) return null;
  const rows = signals.map((signal) => {
    const hours = ageHours(signal, now);
    return { signal, status: provenanceStatus(signal, now), age: ageText(hours), coverage: coverageText(signal.coverage) };
  });
  const status = STATUS_ORDER.find((st) => rows.some((r) => r.status === st)) ?? "current";
  const lead = rows.find((r) => r.status === status)!;
  const what = shortLabel(lead.signal);
  let detail: string;
  switch (status) {
    case "unavailable":
    case "unknown": detail = what; break;
    case "stale": detail = lead.signal.stale_reason ? `${what} superseded` : `${what} ${lead.age}`; break;
    case "partial": detail = lead.coverage ?? `${what} incomplete`; break;
    default: {
      // Current: the oldest signal's age, so the badge never reads fresher than its oldest input.
      const oldest = rows.reduce((m, r) => ((ageHours(r.signal, now) ?? 0) > (ageHours(m.signal, now) ?? 0) ? r : m));
      detail = `oldest ${oldest.age}`;
    }
  }
  return { status, text: `${BADGE_LEAD[status]} · ${detail}`, rows };
}

/** A few words for a signal on the badge ("ESPN projections", "nflverse injuries"). */
export function shortLabel(s: SignalProvenance): string {
  const noun: Record<string, string> = {
    league: "league data", projections: "projections", injuries: "injury reports", usage: "usage data", analysis: "analysis",
  };
  const what = noun[s.signal] ?? s.signal;
  return s.source ? `${sourceName(s.source)} ${what}` : what[0].toUpperCase() + what.slice(1);
}

/** A response that may carry the provenance block (additive: older backends omit it). */
export type WithProvenance = { provenance?: ProvenancePayload | null };
