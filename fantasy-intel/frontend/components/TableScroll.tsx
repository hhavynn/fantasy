"use client";

import { useEffect } from "react";

const MARK = "data-kb-scroll";

/** A `.table-wrap` that overflows gets `tabindex="0"`, so a keyboard can focus it and scroll it with the arrow keys
 *  (WCAG 2.1.1; axe `scrollable-region-focusable`). One that fits, or sits in a hidden skeleton, loses it again. */
function markScrollers() {
  for (const el of document.querySelectorAll<HTMLElement>(".table-wrap")) {
    const overflows = el.scrollWidth > el.clientWidth + 1 && !el.closest("[aria-hidden='true']");
    if (overflows && !el.hasAttribute("tabindex")) {
      el.tabIndex = 0;
      el.setAttribute(MARK, "");
    } else if (!overflows && el.hasAttribute(MARK)) {
      el.removeAttribute("tabindex");
      el.removeAttribute(MARK);
    }
  }
}

/**
 * Marks a scrolled `.table-wrap` with `data-scrolled` so globals.css can draw the shadow on its
 * pinned first column. One passive capture listener on the document covers every table on every
 * page (scroll events do not bubble, but they do capture); nothing renders. It also keeps every
 * overflowing table keyboard-scrollable (markScrollers), re-checked once per frame after the DOM
 * or the window size changes.
 */
export function TableScroll() {
  useEffect(() => {
    const onScroll = (e: Event) => {
      const el = e.target;
      if (el instanceof HTMLElement && el.classList.contains("table-wrap")) {
        el.toggleAttribute("data-scrolled", el.scrollLeft > 0);
      }
    };
    let frame = 0;
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; markScrollers(); });
    };
    const observer = new MutationObserver(schedule);
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", schedule, { passive: true });
    schedule();
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => {
      document.removeEventListener("scroll", onScroll, { capture: true });
      window.removeEventListener("resize", schedule);
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, []);
  return null;
}
