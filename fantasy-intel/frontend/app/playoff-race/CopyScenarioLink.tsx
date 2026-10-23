"use client";

import { useState } from "react";
import styles from "./playoff.module.css";

/**
 * Copies the what-if's own address (league-scoped, with every set result) so it can be shared or reopened. `href` is
 * built on the server from the page's league (`withLeague`), never from the last-league cookie. The visible status
 * line says whether the copy worked; when the clipboard is unavailable the address bar holds the same link.
 */
export function CopyScenarioLink({ href }: { href: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(new URL(href, window.location.origin).toString());
      setState("copied");
    } catch {
      setState("failed");
    }
  };
  return (
    <span className={styles.copy}>
      <button type="button" onClick={copy}>Copy scenario link</button>
      <span role="status" className={styles.copyStatus}>
        {state === "copied" ? "Link copied" : state === "failed" ? "Copy failed: the address bar has the same link" : ""}
      </span>
    </span>
  );
}
