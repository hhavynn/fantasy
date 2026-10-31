"use client";

import { LocalReportShare } from "@/components/LocalReportShare";
import { useNameFreeCopy } from "@/components/useNameFreeCopy";
import styles from "./value.module.css";

/** Share via local clipboard only; never upload, store or create a public report URL. */
export function RosterExport({ text }: { text: string }) {
  const { outcome, copy } = useNameFreeCopy(text);
  const message = outcome === "copied" ? "Name-free roster summary copied."
    : outcome === "unavailable" ? "Clipboard unavailable. No information was shared." : "";
  return (
    <>
      <button type="button" className={styles.exportButton} onClick={copy}>
        Copy name-free roster summary
      </button>
      <LocalReportShare text={text} />
      <span className={styles.exportStatus} role="status" aria-live="polite">{message}</span>
    </>
  );
}
