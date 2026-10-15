export interface ScheduleSelectorTeam {
  team_id: number;
  name: string;
  is_mine?: boolean;
}

/**
 * Resolves the next schedule selection when the focus team changes.
 *
 * Rules:
 * - If the selected schedule belongs to the newly selected focus team, clear the schedule selection ("").
 * - If the schedule selection is still valid, preserve it.
 * - Empty schedule selection ("") remains empty.
 */
export function resolveNextSchedule(
  nextTeam: number,
  currentSchedule: number | ""
): number | "" {
  if (currentSchedule !== "" && currentSchedule === nextTeam) {
    return "";
  }
  return currentSchedule;
}

/**
 * Filters teams available in the schedule dropdown.
 *
 * Rule:
 * - The schedule dropdown never offers the currently selected team as an option.
 */
export function filterScheduleOptions<T extends { team_id: number }>(
  teams: T[],
  selectedTeam: number
): T[] {
  return teams.filter((t) => t.team_id !== selectedTeam);
}

/**
 * Builds the URL search parameters to submit for Luck Lab.
 *
 * Rules:
 * - Direct submission always includes the focus team.
 * - Schedule is included only when non-empty and distinct from the focus team.
 * - No duplicate team IDs can be submitted.
 */
export function buildLuckLabSearchParams(
  selectedTeam: number | undefined | null,
  selectedSchedule: number | "" | undefined | null
): URLSearchParams {
  const params = new URLSearchParams();
  if (selectedTeam !== undefined && selectedTeam !== null) {
    params.set("team", String(selectedTeam));
  }
  if (
    selectedSchedule !== "" &&
    selectedSchedule !== undefined &&
    selectedSchedule !== null &&
    Number(selectedSchedule) !== Number(selectedTeam)
  ) {
    params.set("vs", String(selectedSchedule));
  }
  return params;
}

/**
 * Computes initial state for focus team and schedule selection.
 * Clears schedule if it matches the focus team.
 */
export function getInitialSchedule(
  focusTeamId: number,
  scheduleOf: number | null | undefined
): number | "" {
  if (scheduleOf === null || scheduleOf === undefined || scheduleOf === focusTeamId) {
    return "";
  }
  return scheduleOf;
}
