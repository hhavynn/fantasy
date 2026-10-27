/** Supported future-week windows of the backend's stash-plan/v1 contract. */
export const STASH_HORIZONS = [2, 4, 6] as const;
export type StashHorizon = (typeof STASH_HORIZONS)[number];

export function readStashHorizon(raw: string | string[] | undefined): StashHorizon {
  const first = Array.isArray(raw) ? raw[0] : raw;
  return first === "2" ? 2 : first === "6" ? 6 : 4;
}

/** A missing projected delta is unknown, not zero; signed positive and negative numbers are projected points. */
export function stashGain(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const rounded = value.toFixed(1);
  return `${value > 0 && Number(rounded) !== 0 ? "+" : ""}${rounded}`;
}
