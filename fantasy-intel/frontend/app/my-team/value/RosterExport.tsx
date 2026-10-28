"use client";

import { useState } from "react";
import styles from "./value.module.css";

/** Share via local clipboard only; never upload, store or create a public report URL. */
export function RosterExport({ text }: { text: string }) {
  const [message, setMessage] = useState("");
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setMessage("Name-free roster summary copied.");
    } catch {
      setMessage("Clipboard unavailable. No information was shared.");
    }
  }
  return (
    <>
      <button type="button" className={styles.exportButton} onClick={() => void copy()}>
        Copy name-free roster summary
      </button>
      <span className={styles.exportStatus} role="status" aria-live="polite">{message}</span>
    </>
  );
}
