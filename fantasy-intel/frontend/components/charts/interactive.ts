// Pure geometry for the interactive scatter: nearest-point hit testing, bubble radii and tooltip
// placement. No imports, so it is unit-testable.

export type Pt = { px: number; py: number; r?: number };

/**
 * Index of the point nearest (x, y) within `maxDist` chart pixels of its edge, or null. Dense
 * scatters stay usable with a mouse or a finger: the target is the nearest marker, not the exact pixel.
 */
export function nearestPoint(points: readonly Pt[], x: number, y: number, maxDist: number): number | null {
  let best: number | null = null;
  let bestD = Infinity;
  points.forEach((p, i) => {
    const d = Math.hypot(p.px - x, p.py - y) - (p.r ?? 0);
    if (d <= maxDist && d < bestD) {
      best = i;
      bestD = d;
    }
  });
  return best;
}

/** Area-true bubble radius: sqrt scale from [0, max] onto [rMin, rMax]. Missing size -> rMin. */
export function bubbleRadius(value: number | null | undefined, max: number, rMin = 4, rMax = 14): number {
  if (value === null || value === undefined || !Number.isFinite(value) || value <= 0 || !(max > 0)) return rMin;
  return rMin + (rMax - rMin) * Math.sqrt(Math.min(value, max) / max);
}

/**
 * Top-left corner for a tooltip of `w` x `h` next to a point, flipped to the side with room and clamped
 * inside the chart so it never overflows a phone screen.
 */
export function tooltipPosition(px: number, py: number, w: number, h: number, width: number, height: number, gap = 14): { left: number; top: number } {
  const right = px + gap + w <= width;
  let left = right ? px + gap : px - gap - w;
  left = Math.max(4, Math.min(left, width - w - 4));
  let top = py - h / 2;
  top = Math.max(4, Math.min(top, height - h - 4));
  return { left, top };
}

/** Chart height for a measured width: a landscape plot on desktop, taller on phones so markers spread out. */
export function chartHeight(width: number): number {
  if (width < 520) return Math.round(Math.max(300, width * 0.95));
  return Math.round(Math.min(520, Math.max(320, width * 0.6)));
}
