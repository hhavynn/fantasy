// Linear scales and "nice" axis ticks for the SVG chart primitives. Pure functions, no imports.

export type Domain = readonly [number, number];

/**
 * A linear map from `domain` to `range`. A zero-width domain maps everything to the middle of
 * the range (a single point has no scale; centring it is more honest than pinning it to an edge).
 */
export function linearScale(domain: Domain, range: Domain): (v: number) => number {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  if (d1 === d0) return () => (r0 + r1) / 2;
  const k = (r1 - r0) / (d1 - d0);
  return (v) => r0 + (v - d0) * k;
}

/** [min, max] of the finite numbers in `values`, or null when there are none. */
export function extent(values: Iterable<number | null | undefined>): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (v === null || v === undefined || !Number.isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return lo <= hi ? [lo, hi] : null;
}

/** The 1/2/5 × 10^k step closest to `span / count`, so about `count` intervals across `span`. */
export function niceStep(span: number, count: number): number {
  const raw = span / Math.max(1, count);
  const exp = Math.floor(Math.log10(raw));
  const f = raw / 10 ** exp;
  const nice = f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10;
  return nice * 10 ** exp;
}

/** Decimal places needed to print multiples of a 1/2/5 × 10^k step exactly. */
const decimalsOf = (step: number) => Math.max(0, -Math.floor(Math.log10(step)));

export type Ticks = { ticks: number[]; min: number; max: number; step: number };

/**
 * Round-number ticks covering [min, max] with about `count` intervals. `min`/`max` of the result
 * are the outer ticks (the "nice" domain), so a chart can use them as its axis range.
 * A degenerate input (min === max) is widened around the value so the axis still has a span.
 */
export function niceTicks(min: number, max: number, count = 4): Ticks {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { ticks: [0, 1], min: 0, max: 1, step: 1 };
  if (min > max) [min, max] = [max, min];
  if (min === max) {
    const pad = min === 0 ? 1 : Math.abs(min) * 0.1;
    min -= pad;
    max += pad;
  }
  const step = niceStep(max - min, count);
  const lo = Math.floor(min / step + 1e-9) * step;
  const hi = Math.ceil(max / step - 1e-9) * step;
  const places = Math.min(10, decimalsOf(step));
  const ticks: number[] = [];
  for (let i = 0; lo + i * step <= hi + step * 1e-9; i++) ticks.push(Number((lo + i * step).toFixed(places)));
  return { ticks, min: ticks[0], max: ticks[ticks.length - 1], step };
}

/** Ticks inside a fixed domain (when the caller pins the axis): round values from `min` to `max`. */
export function ticksWithin(min: number, max: number, count = 4): number[] {
  if (!(max > min)) return [min];
  const step = niceStep(max - min, count);
  const places = Math.min(10, decimalsOf(step));
  const out: number[] = [];
  for (let v = Math.ceil(min / step - 1e-9) * step; v <= max + step * 1e-9; v += step) out.push(Number(v.toFixed(places)));
  return out;
}
