"use client";

import { useState } from "react";
import styles from "./weekly-recap.module.css";

/** Clipboard is explicitly user-initiated. No uploaded file, public route, or share token is created. */
export function RecapExport({ text }: { text: string }) {
  const [message, setMessage] = useState("");
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setMessage("Name-free summary copied.");
    } catch {
      setMessage("Clipboard unavailable. Print this page instead.");
    }
  }
  return (
    <div className={styles.exportActions}>
      <button type="button" onClick={() => void copy()}>Copy name-free summary</button>
      <button type="button" onClick={() => window.print()}>Print report</button>
      <span className={styles.exportMessage} role="status" aria-live="polite">{message}</span>
    </div>
  );
}
