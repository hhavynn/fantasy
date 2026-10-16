// Greedy collision-avoiding label placement for scatter-style charts (ported from the Luck lab
// scatter). For each point, try a few offsets around it and keep the first whose text box clears
// every label already placed, every other point's marker, and the chart bounds.

export type LabelInput = {
  id: string | number;
  text: string;
  /** Point position in chart units. */
  px: number;
  py: number;
  /** Placed first, gets the roomiest offsets, and is never dropped. */
  priority?: boolean;
};

export type Anchor = "start" | "end" | "middle";
/** `text` is what to draw: the original, or an ellipsized form when the full text could not fit. */
export type PlacedLabel = LabelInput & { x: number; y: number; anchor: Anchor; truncated?: boolean };

export type PlaceOptions = {
  /** Estimated width of one character in chart units. */
  charWidth?: number;
  /** Estimated text height in chart units. */
  textHeight?: number;
  /** Marker radius used as an obstacle for every point. */
  markerRadius?: number;
  /** Omit non-priority labels that cannot be placed without overlap (default true).
   *  When false they are placed at the first offset anyway. */
  dropOnCollision?: boolean;
  /** Further points to keep labels off (for example unlabeled points). A label's own point is ignored. */
  obstacles?: readonly { id: string | number; px: number; py: number }[];
  /** Shortest text (characters, including the ellipsis) a label may be cut to before it is dropped. */
  minChars?: number;
};

const ellipsize = (text: string, max: number) => (text.length <= max ? text : `${text.slice(0, Math.max(1, max - 1)).trimEnd()}…`);

type Box = { x0: number; x1: number; y0: number; y1: number };

const hit = (a: Box, b: Box) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;

/** [dx, dy, anchor] candidates, in order of preference. dy is the text baseline offset from the point. */
function offsets(priority: boolean): [number, number, Anchor][] {
  return priority
    ? [[0, -10, "middle"], [0, 18, "middle"], [9, 4, "start"], [-9, 4, "end"]]
    : [[9, 4, "start"], [9, -8, "start"], [9, 16, "start"], [-9, 4, "end"], [-9, -8, "end"], [-9, 16, "end"]];
}

/**
 * Place `labels` inside `bounds` ({width, height} of the chart's coordinate space). Priority
 * labels go first (then top-to-bottom). Returns placed labels in that order; dropped labels are
 * not returned (their points still carry a tooltip).
 */
export function placeLabels(
  labels: readonly LabelInput[],
  bounds: { width: number; height: number },
  options: PlaceOptions = {},
): PlacedLabel[] {
  const { charWidth = 6.2, textHeight = 11, markerRadius = 5, dropOnCollision = true, obstacles: extra = [], minChars = 4 } = options;
  const obstacles = [...labels, ...extra].map((l) => ({
    id: l.id,
    box: { x0: l.px - markerRadius, x1: l.px + markerRadius, y0: l.py - markerRadius, y1: l.py + markerRadius },
  }));
  const placed: Box[] = [];
  const out: PlacedLabel[] = [];
  const order = [...labels].sort((a, b) => Number(!!b.priority) - Number(!!a.priority) || a.py - b.py);

  // The first offset whose text box clears the bounds, placed labels and other points; edge labels
  // therefore flip to the other side of their point.
  const fit = (l: LabelInput, text: string): { pick: [number, number, Anchor]; box: Box } | null => {
    const w = text.length * charWidth;
    for (const t of offsets(!!l.priority)) {
      const x0 = t[2] === "start" ? l.px + t[0] : t[2] === "end" ? l.px + t[0] - w : l.px - w / 2;
      const box = { x0, x1: x0 + w, y0: l.py + t[1] - textHeight + 2, y1: l.py + t[1] + 2 };
      if (box.x0 < 0 || box.x1 > bounds.width || box.y0 < 0 || box.y1 > bounds.height) continue;
      if (placed.some((b) => hit(box, b))) continue;
      if (obstacles.some((o) => o.id !== l.id && hit(box, o.box))) continue;
      return { pick: t, box };
    }
    return null;
  };

  for (const l of order) {
    let text = l.text;
    let found = fit(l, text);
    // Never clip silently: shorten with an ellipsis until it fits (the caller keeps the full text in a tooltip).
    for (let n = l.text.length - 1; !found && n >= minChars; n--) {
      text = ellipsize(l.text, n);
      found = fit(l, text);
    }
    if (found) {
      placed.push(found.box);
      out.push({ ...l, text, x: l.px + found.pick[0], y: l.py + found.pick[1], anchor: found.pick[2], truncated: text !== l.text });
      continue;
    }
    if (dropOnCollision && !l.priority) continue;
    // A priority label is never dropped: keep it inside the bounds, cut to the width if need be.
    const maxChars = Math.max(minChars, Math.floor(bounds.width / charWidth));
    const t = ellipsize(l.text, maxChars);
    const w = t.length * charWidth;
    const x0 = Math.min(Math.max(l.px - w / 2, 0), Math.max(0, bounds.width - w));
    const y = Math.min(Math.max(l.py - 10, textHeight - 2), bounds.height - 2);
    placed.push({ x0, x1: x0 + w, y0: y - textHeight + 2, y1: y + 2 });
    out.push({ ...l, text: t, x: x0, y, anchor: "start", truncated: t !== l.text });
  }
  return out;
}
