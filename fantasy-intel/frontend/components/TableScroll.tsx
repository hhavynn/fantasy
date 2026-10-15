"use client";

import { useEffect } from "react";

/**
 * Marks a scrolled `.table-wrap` with `data-scrolled` so globals.css can draw the shadow on its
 * pinned first column. One passive capture listener on the document covers every table on every
 * page (scroll events do not bubble, but they do capture); nothing renders.
 */
export function TableScroll() {
  useEffect(() => {
    const onScroll = (e: Event) => {
      const el = e.target;
      if (el instanceof HTMLElement && el.classList.contains("table-wrap")) {
        el.toggleAttribute("data-scrolled", el.scrollLeft > 0);
      }
    };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => document.removeEventListener("scroll", onScroll, { capture: true });
  }, []);
  return null;
}
