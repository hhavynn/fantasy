"use client";

import { useNameFreeCopy } from "@/components/useNameFreeCopy";
import styles from "./result.module.css";

/** Local only: no uploads, public link, or shared private league data. */
export function TradeExport({ text }: { text: string }) {
  const { outcome, copy } = useNameFreeCopy(text);
  const message = outcome === "copied" ? "Name-free trade summary copied."
    : outcome === "unavailable" ? "Clipboard unavailable. Print the evaluation instead." : "";
  return (
    <div className={styles.exportActions}>
      <button type="button" onClick={copy}>Copy name-free trade summary</button>
      <button type="button" onClick={() => window.print()}>Print full evaluation (includes names)</button>
      <p className={styles.printDisclosure}>Printing this page includes team and player names. Use the name-free copy option when sharing.</p>
      <span className={styles.exportMessage} role="status" aria-live="polite">{message}</span>
    </div>
  );
}
