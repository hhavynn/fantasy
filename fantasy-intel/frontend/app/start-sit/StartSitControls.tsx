"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
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

export function StartSitControls({
  currentTeamId,
  teams,
  currentWeek,
  selectedWeek,
  finalWeek,
}: StartSitControlsProps) {
  const router = useRouter();

  const handleTeamChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const newTeamId = e.target.value;
    router.push(`/start-sit?team_id=${newTeamId}&week=${selectedWeek}`);
  };

  const isNextWeekAvailable = currentWeek < finalWeek;

  return (
    <div className={styles.controls}>
      <div className={styles.controlGroup}>
        <span className={styles.controlLabel}>Team:</span>
        <select
          className={styles.select}
          value={currentTeamId}
          onChange={handleTeamChange}
          aria-label="Select fantasy team"
        >
          {teams.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name} ({t.abbrev}){t.is_mine ? " ★ (My Team)" : ""}
            </option>
          ))}
        </select>
      </div>

      <div className={styles.controlGroup}>
        <span className={styles.controlLabel}>Period:</span>
        <div className={styles.weekToggle}>
          <Link
            href={`/start-sit?team_id=${currentTeamId}&week=${currentWeek}`}
            className={`chip ${selectedWeek === currentWeek ? "active" : ""}`}
          >
            Week {currentWeek} (Current)
          </Link>
          {isNextWeekAvailable && (
            <Link
              href={`/start-sit?team_id=${currentTeamId}&week=${currentWeek + 1}`}
              className={`chip ${selectedWeek === currentWeek + 1 ? "active" : ""}`}
            >
              Week {currentWeek + 1} (Preview)
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}
