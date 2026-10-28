"use client";

import { useState } from "react";

/** Only shares a pre-sanitized text allowlist supplied by the authorized report page. */
export function LocalReportShare({ text }: { text: string }) {
  const [status, setStatus] = useState("");
  async function share() {
    // A missing Web Share API never falls back to a public URL, upload or private raw data.
    if (typeof navigator.share !== "function") {
      setStatus("Device sharing is unavailable. Use the name-free copy option.");
      return;
    }
    try {
      await navigator.share({ title: "Fantasy Intel summary", text });
      setStatus("Device share sheet closed.");
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        setStatus("Sharing canceled.");
      } else {
        setStatus("Device sharing is unavailable. Use the name-free copy option.");
      }
    }
  }
  return (
    <>
      <button type="button" onClick={() => void share()}>Share name-free summary</button>
      <span role="status" aria-live="polite" style={{ fontSize: 12 }}>{status}</span>
    </>
  );
}
