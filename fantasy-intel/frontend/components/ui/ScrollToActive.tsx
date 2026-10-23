"use client";

import { useLayoutEffect, useRef } from "react";

/**
 * Drop inside a horizontally scrolling row of links (tabs, the sub-nav). On mount, and whenever
 * `current` changes, the row scrolls so the active (`aria-current`) link is centred where it can be, so a
 * long row never opens with the active item off-screen on a phone. Only the row scrolls (not the
 * page), and the links work the same without JS.
 */
export function ScrollToActive({ current, selector = "[aria-current]" }: {
  current?: string | null;
  /** The active item; a tablist marks it with `[aria-selected="true"]`. */
  selector?: string;
}) {
  const anchor = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const row = anchor.current?.parentElement;
    const active = row?.querySelector<HTMLElement>(selector);
    if (!row || !active || row.scrollWidth <= row.clientWidth) return;
    const left = active.getBoundingClientRect().left - row.getBoundingClientRect().left + row.scrollLeft;
    const target = left - (row.clientWidth - active.offsetWidth) / 2;
    row.scrollLeft = Math.max(0, Math.min(target, row.scrollWidth - row.clientWidth));
  }, [current, selector]);
  return <span ref={anchor} hidden />;
}
