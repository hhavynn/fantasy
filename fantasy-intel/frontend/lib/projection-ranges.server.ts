import { api } from "./api";
import { EMPTY_RANGES, rangeMap, type RangeMap, type RangesResponse } from "./projection-ranges";

/** Floor / ceiling ranges for the week's served Ours run. A failed call shows no ranges; it never breaks the page. */
export async function getRanges(week: number | null | undefined): Promise<RangeMap> {
  if (!week) return EMPTY_RANGES;
  const res = await api<RangesResponse>(`/api/projections/ranges?week=${week}`);
  return res.ok ? rangeMap(res.data) : EMPTY_RANGES;
}
