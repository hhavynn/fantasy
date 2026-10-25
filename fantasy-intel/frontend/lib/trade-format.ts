// Pure text helpers for the trade pages (no React), unit-tested in lib/trade-format.test.ts.

import { projectionWord } from "./labels.ts";

const FIXED: Record<string, string> = {
  MUTUAL_PROJECTED_GAIN: "Both lineups project to gain",
  BOTH_LOSE: "Both lineups project to lose",
  NO_PROJECTED_CHANGE: "Neither lineup changes",
};

const VERB: Record<string, string> = { GAIN: "gains", LOSS: "loses", UNCHANGED: "is unchanged" };

/**
 * Human sentence for a backend result label (`MUTUAL_PROJECTED_GAIN`, `TEAM_A_GAIN_TEAM_B_LOSS`, ...). `proj` names
 * the projections the evaluation ran on ("ESPN" unless the league is on Ours).
 */
export function resultText(code: string, a: string, b: string, proj: string = "ESPN"): string {
  if (code === "INCOMPLETE_DATA") return `Incomplete ${projectionWord(proj)}projections: no canonical total`;
  if (FIXED[code]) return FIXED[code];
  const m = code.match(/^TEAM_A_(\w+?)_TEAM_B_(\w+)$/);
  if (!m) return code;
  return `${a} ${VERB[m[1]] ?? m[1].toLowerCase()} · ${b} ${VERB[m[2]] ?? m[2].toLowerCase()}`;
}

/** Badge tone for a result label: good, bad, warn or info. */
export function resultTone(code: string): "good" | "bad" | "warn" | "info" {
  return code === "MUTUAL_PROJECTED_GAIN" ? "good" : code === "BOTH_LOSE" ? "bad" : code === "INCOMPLETE_DATA" ? "warn" : "info";
}

/** "+1.2" / "−1.2" / "0.0" as plain text (a value that rounds to zero carries no sign). `null` gives "—". */
export function signedText(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const text = Math.abs(v).toFixed(digits);
  if (Number(text) === 0) return text;
  return `${v > 0 ? "+" : "−"}${text}`;
}

/** `[a, b, c]` -> "a, b and c" for compact sentences. */
export function joinList(items: readonly string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}
