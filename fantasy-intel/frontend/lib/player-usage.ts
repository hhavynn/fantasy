// Pure helpers for the player profile's usage charts: which charts a position gets, the week axis,
// and series aligned to it. Kept free of React and `@/` imports so they can be unit-tested directly.

export type UsageField =
  | "offense_pct" | "target_share" | "air_yards_share" | "team_rush_share" | "backfield_rush_share"
  | "fantasy_points_ppr" | "xfp";

export type ChartFormat = "pct" | "pts";

export type ChartSeriesSpec = { id: string; label: string; field: UsageField };

export type ChartSpec = {
  id: string;
  title: string;
  format: ChartFormat;
  series: ChartSeriesSpec[];
  /** One line under the chart saying what it measures. */
  note?: string;
};

const SNAP: ChartSpec = {
  id: "snap", title: "Snap share", format: "pct",
  series: [{ id: "offense_pct", label: "Snap share", field: "offense_pct" }],
  note: "Share of the team's offensive snaps.",
};

const POINTS: ChartSpec = {
  id: "points", title: "PPR points vs expected", format: "pts",
  series: [
    { id: "fantasy_points_ppr", label: "PPR points", field: "fantasy_points_ppr" },
    { id: "xfp", label: "Expected (xFP)", field: "xfp" },
  ],
  note: "Actual PPR points against the points the role was worth (nflverse xFP).",
};

/**
 * Charts for a position's usage trend. Only measures that mean something for the position are
 * included: a WR gets no rushing charts and an RB no air yards. Order is the order on the page.
 */
export function usageChartSpecs(position: string | null | undefined): ChartSpec[] {
  switch (position) {
    case "RB":
      return [
        SNAP,
        {
          id: "rush", title: "Rushing share", format: "pct",
          series: [
            { id: "team_rush_share", label: "Team rush share", field: "team_rush_share" },
            { id: "backfield_rush_share", label: "Backfield share", field: "backfield_rush_share" },
          ],
          note: "Share of all team carries, and of designed carries among the team's running backs.",
        },
        {
          id: "target", title: "Target share", format: "pct",
          series: [{ id: "target_share", label: "Target share", field: "target_share" }],
          note: "Share of the team's targets.",
        },
        POINTS,
      ];
    case "WR":
    case "TE":
      return [
        SNAP,
        {
          id: "target", title: "Target and air-yard share", format: "pct",
          series: [
            { id: "target_share", label: "Target share", field: "target_share" },
            { id: "air_yards_share", label: "Air-yard share", field: "air_yards_share" },
          ],
          note: "Share of the team's targets and of its air yards.",
        },
        POINTS,
      ];
    case "QB":
      return [
        SNAP,
        {
          id: "rush", title: "Rushing share", format: "pct",
          series: [{ id: "team_rush_share", label: "Team rush share", field: "team_rush_share" }],
          note: "Share of all team carries, scrambles included.",
        },
        POINTS,
      ];
    default:
      return [];
  }
}

/** Weeks 1..N where N is the latest week with a record or `through` (the last completed week), so a missed week shows as a gap. */
export function weekAxis(weeks: readonly number[], through?: number | null): number[] {
  const finite = weeks.filter((w) => Number.isInteger(w) && w > 0);
  const last = Math.max(0, ...finite, through ?? 0);
  return Array.from({ length: last }, (_, i) => i + 1);
}

/** One value per axis week; a week with no row, or a null value, is `null` (a gap), never zero. */
export function alignToAxis<T extends { week: number }>(
  rows: readonly T[],
  axis: readonly number[],
  pick: (row: T) => number | null | undefined,
): (number | null)[] {
  const byWeek = new Map<number, T>();
  for (const row of rows) byWeek.set(row.week, row);
  return axis.map((week) => {
    const row = byWeek.get(week);
    if (!row) return null;
    const v = pick(row);
    return typeof v === "number" && Number.isFinite(v) ? v : null;
  });
}

/** True when at least one value is a real number. */
export function hasData(points: readonly (number | null)[]): boolean {
  return points.some((p) => p !== null && Number.isFinite(p));
}

/** Percent-axis label for a 0–1 share: 0.35 -> "35%". */
export function pctTick(v: number): string {
  return `${Math.round(v * 100)}%`;
}

/**
 * A fixed y-axis that starts at zero so a small wobble is not drawn as a collapse: shares top out at
 * the next 10% above the data (never above 100%), points at the next whole number with 10% headroom.
 * Returns undefined when there is nothing to chart.
 */
export function valueDomain(values: readonly (number | null)[], format: ChartFormat): [number, number] | undefined {
  const real = values.filter((v): v is number => v !== null && Number.isFinite(v));
  if (real.length === 0) return undefined;
  const max = Math.max(...real);
  const min = Math.min(...real);
  if (format === "pct") return [0, Math.min(1, Math.max(0.1, Math.ceil(max * 10 - 1e-9) / 10))];
  const hi = Math.max(1, Math.ceil(max * 1.1 - 1e-9));
  const lo = min < 0 ? Math.floor(min) : 0;
  return [lo, hi];
}
