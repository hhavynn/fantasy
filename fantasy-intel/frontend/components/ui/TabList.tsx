"use client";

import Link from "@/components/LeagueLink";
import { useEffect, useRef, type KeyboardEvent } from "react";
import { cx } from "@/lib/cx";
import { ScrollToActive } from "./ScrollToActive";
import { tabDomId, tabPanelDomId } from "./tab-ids";
import { createRefocus } from "./tab-refocus";
import styles from "./Tabs.module.css";

export type TabListItem = { id: string; href: string; label: string };

// The tab just activated. A tab is a navigation, and the new page can mount a fresh tablist (the route's loading
// state replaces the old one), so the newly selected tab takes focus back when it arrives: only that exact tab, and
// only shortly after (tab-refocus.ts), so a failed navigation never moves focus on a later page.
const refocus = createRefocus();

/**
 * A URL-backed ARIA tablist: each tab is a link (`href: "?id=201&tab=usage"`), so a tab can be deep-linked, opened
 * in a new tab, shared, and server-rendered, and back/forward just work. The selected tab is the one tab stop
 * (roving tabindex); Left/Right move focus with wrap-around, Home/End jump to the ends, Enter or Space opens the
 * focused tab (manual activation: each tab is a server navigation). Pair it with `TabPanel` using the same `idBase`.
 * For a row of view links that are not one panel (filters, sort views) use `Tabs`.
 */
export function TabList({ items, current, label, idBase, className }: {
  items: readonly TabListItem[];
  /** The `id` of the selected item. */
  current: string;
  /** Names the tab group for assistive tech. */
  label: string;
  /** Prefix for the tab and panel DOM ids. */
  idBase: string;
  className?: string;
}) {
  const list = useRef<HTMLDivElement>(null);

  const selectedHref = items.find((item) => item.id === current)?.href;
  useEffect(() => {
    if (!refocus.take(idBase, selectedHref, Date.now())) return;
    list.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus({ preventScroll: true });
  }, [idBase, selectedHref]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const tabs = [...(list.current?.querySelectorAll<HTMLAnchorElement>('[role="tab"]') ?? [])];
    const at = tabs.indexOf(document.activeElement as HTMLAnchorElement);
    if (at < 0) return;
    if (e.key === " ") {
      e.preventDefault();
      tabs[at].click();
      return;
    }
    const n = tabs.length;
    const next = e.key === "ArrowRight" ? (at + 1) % n
      : e.key === "ArrowLeft" ? (at - 1 + n) % n
        : e.key === "Home" ? 0
          : e.key === "End" ? n - 1
            : -1;
    if (next < 0) return;
    e.preventDefault();
    tabs[next].focus();
  };

  return (
    <div ref={list} role="tablist" aria-label={label} className={cx(styles.tabs, styles.tablist, className)} onKeyDown={onKeyDown}>
      {items.map((item) => {
        const selected = item.id === current;
        return (
          <Link
            key={item.id}
            id={tabDomId(idBase, item.id)}
            href={item.href}
            scroll={false}
            role="tab"
            aria-selected={selected}
            aria-controls={selected ? tabPanelDomId(idBase) : undefined}
            tabIndex={selected ? 0 : -1}
            className={styles.tab}
            onClick={(e) => {
              // A modified click opens a new browser tab: this page stays, so nothing should take focus later.
              if (!selected && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) refocus.arm(idBase, item.href, Date.now());
            }}
          >
            {item.label}
          </Link>
        );
      })}
      <ScrollToActive current={current} selector='[aria-selected="true"]' />
    </div>
  );
}
