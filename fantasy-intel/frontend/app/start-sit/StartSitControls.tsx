"use client";

import { useRouter } from "next/navigation";
import { Tabs } from "@/components/ui";
import styles from "./start-sit.module.css";

interface TeamOption {
  id: number;
  name: string;
  abbrev: string;
  is_mine: boolean;
}

interface StartSitControlsProps {
  currentTeamId: number;
  teams: TeamOption[];
  currentWeek: number;
  selectedWeek: number;
  finalWeek: number;
}

/** Team picker and week toggle. Both write to the URL (`?team_id=&week=`), so a view can be shared. */
export function StartSitControls({
  currentTeamId,
  teams,
  currentWeek,
  selectedWeek,
  finalWeek,
}: StartSitControlsProps) {
  const router = useRouter();

  const handleTeamChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    router.push(`/start-sit?team_id=${e.target.value}&week=${selectedWeek}`);
  };

  const weekHref = (w: number) => `/start-sit?team_id=${currentTeamId}&week=${w}`;
  const items = [{ href: weekHref(currentWeek), label: `Week ${currentWeek} (current)` }];
  if (currentWeek < finalWeek) items.push({ href: weekHref(currentWeek + 1), label: `Week ${currentWeek + 1} (preview)` });

  return (
    <div className={styles.controls}>
      <label className={styles.field}>
        <span>Team</span>
        <select className={styles.select} value={currentTeamId} onChange={handleTeamChange}>
          {teams.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name} ({t.abbrev}){t.is_mine ? " (you)" : ""}
            </option>
          ))}
        </select>
      </label>
      <Tabs items={items} current={weekHref(selectedWeek)} label="Scoring week" />
    </div>
  );
}
