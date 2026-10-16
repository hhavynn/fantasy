// Display-only tidying of the engine's reason sentences ("offense_pct down: 94% -> 55% (weeks [1, 2, 3]
// vs [4, 5])"): identifiers become names, week arrays become ranges, "->" becomes an arrow. No number
// or claim changes. No React and no `@/` imports, so it runs under the unit-test runner.

const METRIC_LABELS: Record<string, string> = {
  offense_pct: "Snap share",
  target_share: "Target share",
  air_yards_share: "Air-yard share",
  team_rush_share: "Team rush share",
  backfield_rush_share: "Backfield rush share",
  targets: "Targets",
  carries: "Carries",
  xfp: "xFP",
  ppr: "PPR points",
};

/** A usage metric key as a name for people: "offense_pct" is "Snap share", "xfp" is "xFP". */
export function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? (key.replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase()));
}

/** [1, 2, 3, 5] as "1–3, 5": runs of two or more consecutive numbers collapse to a range. */
export function weekRanges(weeks: readonly number[]): string {
  const sorted = [...weeks].sort((a, b) => a - b);
  const parts: string[] = [];
  for (let i = 0; i < sorted.length; ) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    parts.push(j > i ? `${sorted[i]}–${sorted[j]}` : String(sorted[i]));
    i = j + 1;
  }
  return parts.join(", ");
}

/** One reason sentence for display. Sentences without these patterns pass through unchanged. */
export function tidyReason(text: string): string {
  return text
    .replace(/^([a-z][a-z0-9]*(?:_[a-z0-9]+)*)(?= (?:up|down|flat|steady)\b)/, (_, key: string) => metricLabel(key))
    .replace(/\[(\d+(?:, \d+)*)\]/g, (_, list: string) => weekRanges(list.split(", ").map(Number)))
    .replaceAll("->", "→");
}
