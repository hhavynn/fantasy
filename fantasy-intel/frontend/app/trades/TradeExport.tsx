"use client";

import { useState } from "react";
import { LocalReportShare } from "@/components/LocalReportShare";
import styles from "./result.module.css";

/** Local only: no uploads, public link, or shared private league data. */
export function TradeExport({ text }: { text: string }) {
  const [message, setMessage] = useState("");
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setMessage("Name-free trade summary copied.");
    } catch {
      setMessage("Clipboard unavailable. Print the evaluation instead.");
    }
  }
  return (
    <div className={styles.exportActions}>
      <button type="button" onClick={() => void copy()}>Copy name-free trade summary</button>
      <button type="button" onClick={() => window.print()}>Print full evaluation (includes names)</button>
      <p className={styles.printDisclosure}>Printing this page includes team and player names. Use the name-free copy option when sharing.</p>
      <LocalReportShare text={text} />
      <span className={styles.exportMessage} role="status" aria-live="polite">{message}</span>
    </div>
  );
}
