"use client";

import { useState } from "react";
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
      <button type="button" onClick={() => window.print()}>Print evaluation</button>
      <span className={styles.exportMessage} role="status" aria-live="polite">{message}</span>
    </div>
  );
}
