"use client";

import { useState } from "react";
import { outcomeFor, writeClipboard, type CopyOutcome, type CopyResult } from "@/lib/name-free-copy";

/**
 * Copy `text` on demand. The outcome is reported only while `text` is still the summary on screen: a soft navigation to
 * another team, week, phase or trade keeps this component mounted, and its old "copied" must not carry over.
 */
export function useNameFreeCopy(text: string): { outcome: CopyOutcome | null; copy: () => void } {
  const [result, setResult] = useState<CopyResult | null>(null);
  return {
    outcome: outcomeFor(result, text),
    copy: () => {
      void writeClipboard(text, navigator.clipboard).then((outcome) => setResult({ text, outcome }));
    },
  };
}
