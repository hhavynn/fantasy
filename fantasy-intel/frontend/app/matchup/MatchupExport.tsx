"use client";

import { useNameFreeCopy } from "@/components/useNameFreeCopy";
import styles from "./matchup.module.css";

/** Local sharing only: never creates a public URL, stores private data, or uploads a report. */
export function MatchupExport({ text }: { text: string }) {
  const { outcome, copy } = useNameFreeCopy(text);
  const message = outcome === "copied" ? "Name-free summary copied."
    : outcome === "unavailable" ? "Clipboard unavailable. Print this page instead." : "";
  return (
    <div className={styles.exportActions}>
      <button type="button" onClick={copy}>Copy name-free summary</button>
      <button type="button" onClick={() => window.print()}>Print full report (includes names)</button>
      <p className={styles.printDisclosure}>Printing this page includes team and player names. Use the name-free copy option when sharing.</p>
      <span className={styles.exportMessage} role="status" aria-live="polite">{message}</span>
    </div>
  );
}
