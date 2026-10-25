import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import { tabDomId, tabPanelDomId } from "./tab-ids";
import styles from "./Tabs.module.css";

/**
 * The panel of the selected `TabList` tab. Only the selected panel is rendered (the others are a navigation away),
 * so a heavy tab costs nothing until it is opened. Focusable, so a keyboard user can move from the tabs to the
 * panel in one step.
 */
export function TabPanel({ idBase, current, className, children }: {
  idBase: string;
  current: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div role="tabpanel" id={tabPanelDomId(idBase)} aria-labelledby={tabDomId(idBase, current)} tabIndex={0} className={cx(styles.panel, className)}>
      {children}
    </div>
  );
}
