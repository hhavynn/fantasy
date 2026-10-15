"use client";

import { type RefObject, useEffect } from "react";

/** While `open`: Escape closes and returns focus to `trigger`; a press outside `root` closes. */
export function useDismiss(open: boolean, close: () => void, root: RefObject<HTMLElement | null>,
                           trigger: RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      close();
      trigger.current?.focus();
    };
    const onPointer = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) close();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open, close, root, trigger]);
}
