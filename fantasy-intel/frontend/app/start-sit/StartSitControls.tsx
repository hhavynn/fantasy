"use client";

import { Tabs } from "@/components/ui";
import styles from "./start-sit.module.css";

interface StartSitControlsProps {
  /** The selected team (`?team=`), or null for the default team. */
  team: string | null;
  currentWeek: number;
  selectedWeek: number;
  finalWeek: number;
}

/** Week toggle. It writes to the URL (`?team=&week=`), so a view can be shared; the team picker is TeamPerspective. */
export function StartSitControls({
  team,
  currentWeek,
  selectedWeek,
  finalWeek,
}: StartSitControlsProps) {
  const weekHref = (w: number) => `/start-sit?${new URLSearchParams({ ...(team ? { team } : {}), week: String(w) })}`;
  const items = [{ href: weekHref(currentWeek), label: `Week ${currentWeek} (current)` }];
  if (currentWeek < finalWeek) items.push({ href: weekHref(currentWeek + 1), label: `Week ${currentWeek + 1} (preview)` });

  return (
    <div className={styles.controls}>
      <Tabs items={items} current={weekHref(selectedWeek)} label="Scoring week" />
    </div>
  );
}
