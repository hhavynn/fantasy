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
export type PlacedLabel = LabelInput & { x: number; y: number; anchor: Anchor };

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
};

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
  const { charWidth = 6.2, textHeight = 11, markerRadius = 5, dropOnCollision = true } = options;
  const obstacles = labels.map((l) => ({
    id: l.id,
    box: { x0: l.px - markerRadius, x1: l.px + markerRadius, y0: l.py - markerRadius, y1: l.py + markerRadius },
  }));
  const placed: Box[] = [];
  const out: PlacedLabel[] = [];
  const order = [...labels].sort((a, b) => Number(!!b.priority) - Number(!!a.priority) || a.py - b.py);

  for (const l of order) {
    const w = l.text.length * charWidth;
    let pick: [number, number, Anchor] | null = null;
    let pickBox: Box | null = null;
    for (const t of offsets(!!l.priority)) {
      const x0 = t[2] === "start" ? l.px + t[0] : t[2] === "end" ? l.px + t[0] - w : l.px - w / 2;
      const box = { x0, x1: x0 + w, y0: l.py + t[1] - textHeight + 2, y1: l.py + t[1] + 2 };
      if (box.x0 < 0 || box.x1 > bounds.width || box.y0 < 0 || box.y1 > bounds.height) continue;
      if (placed.some((b) => hit(box, b))) continue;
      if (obstacles.some((o) => o.id !== l.id && hit(box, o.box))) continue;
      pick = t;
      pickBox = box;
      break;
    }
    if (!pick) {
      if (dropOnCollision && !l.priority) continue;
      pick = offsets(!!l.priority)[0];
    }
    if (pickBox) placed.push(pickBox);
    out.push({ ...l, x: l.px + pick[0], y: l.py + pick[1], anchor: pick[2] });
  }
  return out;
}
