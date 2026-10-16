// Helpers shared by the SVG chart primitives. No imports, so they are unit-testable.

/** Axis and label size in chart units at the chart's natural width. */
export const BASE_FONT = 11;

/**
 * Charts scale with their container (viewBox), so text shrinks on narrow screens. On phones the
 * chart CSS swaps in a larger font size, in chart units, that lands near 10.5px on screen for a
 * ~309px-wide chart (a 375px viewport minus page and section padding).
 */
export function phoneFont(width: number): number {
  return Math.max(BASE_FONT, Math.round((10.5 * width) / 309));
}

/** Average glyph width as a fraction of the font size (IBM Plex Sans, tabular figures). */
export const CHAR_EM = 0.58;

export const textWidth = (chars: number, font: number): number => chars * font * CHAR_EM;

/** Series colour: an explicit CSS colour, else the next `--chart-n` token in order. */
export function seriesColor(index: number, explicit?: string): string {
  return explicit ?? `var(--chart-${(index % 6) + 1})`;
}

/** Compact default number format: up to 2 decimals, thousands separators, real minus sign. */
export function defaultFormat(v: number): string {
  const s = Number(v.toFixed(2)).toLocaleString("en-US", { maximumFractionDigits: 2 });
  return s.replace("-", "−");
}

/** Signed format for diverging values: "+3.2", "−1.0", "0". */
export function signedFormat(v: number): string {
  const s = defaultFormat(Math.abs(v));
  return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s;
}

/** Show every `step`th category label so that labels `pitch` units wide fit across `plotWidth`. */
export function thinStep(count: number, plotWidth: number, pitch: number): number {
  if (count <= 1) return 1;
  const slot = plotWidth / (count - 1);
  return Math.max(1, Math.ceil(pitch / slot));
}

/** Truncate with an ellipsis so the result is at most `max` characters. */
export function truncate(text: string, max: number): string {
  if (max < 2) return "";
  return text.length <= max ? text : `${text.slice(0, Math.max(1, max - 1)).trimEnd()}…`;
}
