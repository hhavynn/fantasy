"use client";

import { LocalReportShare } from "@/components/LocalReportShare";
import { useNameFreeCopy } from "@/components/useNameFreeCopy";
import styles from "./weekly-recap.module.css";

/** Clipboard is explicitly user-initiated. No uploaded file, public route, or share token is created. */
export function RecapExport({ text }: { text: string }) {
  const { outcome, copy } = useNameFreeCopy(text);
  const message = outcome === "copied" ? "Name-free summary copied."
    : outcome === "unavailable" ? "Clipboard unavailable. Print this page instead." : "";
  return (
    <div className={styles.exportActions}>
      <button type="button" onClick={copy}>Copy name-free summary</button>
      <button type="button" onClick={() => window.print()}>Print full report (includes names)</button>
      <p className={styles.printDisclosure}>Printing this page includes team and player names. Use the name-free copy option when sharing.</p>
      <LocalReportShare text={text} />
      <span className={styles.exportMessage} role="status" aria-live="polite">{message}</span>
    </div>
  );
}
