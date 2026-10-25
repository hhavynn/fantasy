"use client";

import { useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useLeagueHref } from "@/components/useAppPathname";
import {
  type ScheduleSelectorTeam,
  filterScheduleOptions,
  getInitialSchedule,
  resolveNextSchedule,
  buildLuckLabSearchParams,
} from "./schedule-selector";

export type { ScheduleSelectorTeam };

export interface ScheduleSelectorProps {
  teams: ScheduleSelectorTeam[];
  focusTeamId: number;
  scheduleOf: number | null;
  children?: ReactNode;
}

export function ScheduleSelector({
  teams,
  focusTeamId,
  scheduleOf,
  children,
}: ScheduleSelectorProps) {
  const router = useRouter();
  const leagueHref = useLeagueHref();
  const [selectedTeam, setSelectedTeam] = useState<number>(focusTeamId);
  const [selectedSchedule, setSelectedSchedule] = useState<number | "">(
    getInitialSchedule(focusTeamId, scheduleOf)
  );

  const handleTeamChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const nextTeam = Number(e.target.value);
    if (!Number.isNaN(nextTeam)) {
      setSelectedTeam(nextTeam);
      setSelectedSchedule((prev) => resolveNextSchedule(nextTeam, prev));
    }
  };

  const handleScheduleChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const val = e.target.value;
    if (val === "") {
      setSelectedSchedule("");
    } else {
      const num = Number(val);
      if (!Number.isNaN(num)) {
        setSelectedSchedule(num);
      }
    }
  };

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const params = buildLuckLabSearchParams(selectedTeam, selectedSchedule);
    const query = params.toString();
    // On a league page stay in it (/l/<lg>/luck-lab), not in whichever league another tab viewed last.
    router.push(leagueHref(query ? `/luck-lab?${query}` : "/luck-lab"));
  };

  const scheduleOptions = filterScheduleOptions(teams, selectedTeam);

  return (
    <form action={leagueHref("/luck-lab")} method="GET" onSubmit={handleSubmit} className="row-flex">
      <label>
        Team{" "}
        <select
          name="team"
          value={selectedTeam}
          onChange={handleTeamChange}
        >
          {teams.map((t) => (
            <option key={t.team_id} value={t.team_id}>
              {t.name}
              {t.is_mine ? " (default)" : ""}
            </option>
          ))}
        </select>
      </label>
      <label>
        With the schedule of{" "}
        <select
          name="vs"
          value={selectedSchedule}
          onChange={handleScheduleChange}
        >
          <option value="">—</option>
          {scheduleOptions.map((t) => (
            <option key={t.team_id} value={t.team_id}>
              {t.name}
            </option>
          ))}
        </select>
      </label>
      <button type="submit">Show</button>
      {children}
    </form>
  );
}
