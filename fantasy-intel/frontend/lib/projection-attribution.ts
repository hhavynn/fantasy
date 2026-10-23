// Types and wording for "Why it changed" on the Projection movers page (contract "projection-attribution/v1", one block
// per mover, from app/analytics/projection_attribution.py). Every number comes from the backend: these helpers only
// order, format and phrase it. They never compute a contribution, never fill a missing value and never name a cause.

export const ATTRIBUTION_CONTRACT = "projection-attribution/v1";

export type AttributionStatus = "explained" | "partial" | "model_changed" | "not_decomposed" | "unavailable";
export type AttributionKind = "status" | "availability" | "input" | "model";
export type AttributionUnit = "share" | "flag" | "number" | "points";

export type AttributionPart = {
  key: string;
  kind: AttributionKind;
  label: string;
  points: number;
  weeks: number[];
  exact: boolean;
  feature?: string;
  unit?: AttributionUnit;
  before?: number | null;
  after?: number | null;
  before_missing?: string | null;
  after_missing?: string | null;
  varies?: boolean;
  events?: { forecast: "earlier" | "latest"; text: string; source: string }[];
};

export type AttributionInput = {
  feature: string;
  label: string;
  unit: AttributionUnit;
  weeks: number[];
  before: number | null;
  after: number | null;
  before_missing: string | null;
  after_missing: string | null;
  varies: boolean;
};

export type AttributionSide = {
  created_at: string | null;
  data_through: { season: number; week: number } | null;
  cutoff_week: number | null;
  model_versions: string[];
  artifact_ids: string[];
  run_ids: string[];
};

export type Attribution = {
  contract: string;
  status: AttributionStatus;
  source_label: string;
  method: "exact_linear" | "approximate" | "model_changed" | null;
  method_note: string | null;
  approximation_note: string | null;
  total: number | null;
  parts: AttributionPart[];
  residual: { points: number; reasons: { key: string; label: string; points: number; weeks: number[] }[] } | null;
  reconciles: boolean | null;
  tolerance: number | null;
  inputs: AttributionInput[];
  missing_inputs: { before: { text: string; weeks: number[] }[]; after: { text: string; weeks: number[] }[] };
  model: { changed: boolean; reasons: { text: string; weeks: number[] }[] };
  as_of: { before: AttributionSide; after: AttributionSide; refused: { week: number; forecast: string; reason: string }[] } | null;
  reason: string | null;
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** [7, 8, 9, 11] as "weeks 7–9, 11"; one week as "week 7"; none as "". */
export function weeksText(weeks: readonly number[]): string {
  const sorted = [...new Set(weeks)].sort((a, b) => a - b);
  if (sorted.length === 0) return "";
  const parts: string[] = [];
  for (let i = 0; i < sorted.length; ) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    parts.push(j > i ? `${sorted[i]}–${sorted[j]}` : String(sorted[i]));
    i = j + 1;
  }
  return `${sorted.length === 1 ? "week" : "weeks"} ${parts.join(", ")}`;
}

/** An input value for display by unit, or null when it is missing (the caller shows the reason, never 0). */
export function inputValue(v: number | null | undefined, unit: AttributionUnit | undefined): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  if (unit === "flag") return v >= 0.5 ? "yes" : "no";
  if (unit === "share") return `${Math.round(v * 100)}%`;
  const a = Math.abs(v);
  if (a !== 0 && a < 1) return String(Number(v.toFixed(3)));
  return String(Number(v.toFixed(1)));
}

/** The words for a part's kind: shown beside its number so status is never colour alone. */
export function kindText(kind: AttributionKind, exact = true): string {
  switch (kind) {
    case "input": return exact ? "Measured input" : "Input (approximate)";
    case "availability": return "Measured";
    case "status": return "Stored status";
    case "model": return "Model changed";
    default: return "Measured";
  }
}

/** One sentence that says how much of the change the stored forecasts account for. */
export function attributionHeadline(a: Attribution): string {
  if (a.status === "unavailable") return a.reason ?? "This change cannot be split into parts.";
  const residual = a.residual?.points ?? 0;
  switch (a.status) {
    case "explained":
      return "The parts below account for the whole change, within the rounding of the stored numbers.";
    case "partial":
      return `The measured parts leave ${signedText(residual)} points that the stored forecasts cannot assign.`;
    case "model_changed":
      return "The model changed between the two forecasts, so that part of the change is not split between inputs.";
    case "not_decomposed":
      return "No part of this change could be measured from the stored forecasts.";
    default:
      return "";
  }
}

/** "+1.25" or "−0.40" with a real minus sign, two decimals; "0.00" for a measured zero. */
export function signedText(v: number, digits = 2): string {
  const s = Math.abs(v).toFixed(digits);
  if (Number(s) === 0) return (0).toFixed(digits);
  return `${v > 0 ? "+" : "−"}${s}`;
}

/** The check line: the parts plus the residual against the change, and the tolerance of the stored rounding. */
export function reconcileText(a: Attribution): string | null {
  if (a.total === null || !a.residual) return null;
  const sum = a.parts.reduce((s, p) => s + p.points, 0) + a.residual.points;
  const shown = Math.round(sum * 100) / 100;
  const tol = a.tolerance === null ? "" : ` Stored values are rounded to 2 decimals (up to ±${a.tolerance.toFixed(2)} here).`;
  const ok = a.reconciles && Math.abs(shown - a.total) < 0.005;
  return ok
    ? `Parts and residual add up to the change of ${signedText(a.total)} points.${tol}`
    : `Parts and residual add up to ${signedText(shown)} against a change of ${signedText(a.total)} points.${tol}`;
}

function day(iso: string | null): string | null {
  const m = iso ? /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso) : null;
  return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])} ${m[4]}:${m[5]} UTC` : null;
}

/** "made Oct 14 12:00 UTC, games through week 5" for one forecast; missing parts are said, never guessed. */
export function sideText(s: AttributionSide | null | undefined): string {
  if (!s) return "not stored";
  const made = day(s.created_at);
  const through = s.data_through ? `games through ${s.data_through.season} week ${s.data_through.week}` : "data cutoff not recorded";
  return `${made ? `made ${made}` : "creation time unknown"}, ${through}`;
}

/** The part rows in the backend's order (largest first). A measured zero stays a row: it is a result, not a gap. */
export function attributionRows(a: Attribution): AttributionPart[] {
  return [...a.parts];
}

/** Inputs recorded in either forecast that the parts above do not already show with points. */
export function unattributedInputs(a: Attribution): AttributionInput[] {
  const shown = new Set(attributionRows(a).filter((p) => p.feature).map((p) => p.feature));
  return a.inputs.filter((i) => !shown.has(i.feature));
}
