"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { NAV_GROUPS, getActiveRoute } from "@/lib/navigation";
import type { ProjectionSource } from "@/lib/projection-source";
import { ProjectionSwitch } from "./ProjectionSwitch";
import { ThemeSwitch } from "./ThemeSwitch";
import { ScrollToActive } from "./ui/ScrollToActive";
import { useDismiss } from "./useDismiss";

export function NavGroups() {
  const { activeGroup } = getActiveRoute(usePathname());
  return (
    <nav className="nav-groups" aria-label="Sections">
      {NAV_GROUPS.map((g) => (
        <Link key={g.label} href={g.pages[0].href} aria-current={g === activeGroup ? "true" : undefined}>
          {g.label}
        </Link>
      ))}
    </nav>
  );
}

export function SubNav() {
  const { activeHref, activeGroup } = getActiveRoute(usePathname());
  if (!activeGroup) return null;
  return (
    <div className="subnav-wrap">
      <nav className="subnav" aria-label={`${activeGroup.label} pages`}>
        <span className="subnav-label" aria-hidden="true">{activeGroup.label}</span>
        {activeGroup.pages.map((p) => (
          <Link key={p.href} href={p.href} aria-current={p.href === activeHref ? "page" : undefined}>
            {p.label}
          </Link>
        ))}
        <ScrollToActive current={activeHref} />
      </nav>
    </div>
  );
}

/** Line icons for the phone tab bar, keyed by group label; a group without one gets a plain dot. */
const GROUP_ICONS: Record<string, ReactNode> = {
  "My team": <path d="M9 2.5 15 5v4.2c0 3.6-2.5 5.6-6 6.8-3.5-1.2-6-3.2-6-6.8V5l6-2.5Z" />,
  Players: <><circle cx="9" cy="6" r="3" /><path d="M3.5 15.5c.6-2.9 2.8-4.5 5.5-4.5s4.9 1.6 5.5 4.5" /></>,
  Trades: <><path d="M3 6h11l-3-3" /><path d="M15 12H4l3 3" /></>,
  League: <><path d="M3 15.5h12" /><path d="M5 15.5V9" /><path d="M9 15.5V4" /><path d="M13 15.5v-4" /></>,
};

/** Phones: one tap to any section. Each tab opens the section's first page; the sub-nav lists the rest. */
export function TabBar() {
  const { activeGroup } = getActiveRoute(usePathname());
  return (
    <nav className="tabbar" aria-label="Sections" style={{ "--tabs": NAV_GROUPS.length } as CSSProperties}>
      {NAV_GROUPS.map((g) => (
        <Link key={g.label} href={g.pages[0].href} aria-current={g === activeGroup ? "true" : undefined}>
          <svg viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            {GROUP_ICONS[g.label] ?? <circle cx="9" cy="9" r="3" />}
          </svg>
          {g.label}
        </Link>
      ))}
    </nav>
  );
}

/** The Fantasy Intel mark: ascending bars inside a rounded frame, drawn in the accent. */
export function BrandMark({ size = 22 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 22 22" fill="none" aria-hidden="true">
      <rect x="1.25" y="1.25" width="19.5" height="19.5" rx="5.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6.5 15.5v-3M11 15.5v-6M15.5 15.5V6.5" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  );
}

/** Phones: the section row collapses into a menu button with every group's pages. */
export function MobileNav({ projection }: { projection?: ProjectionSource } = {}) {
  const pathname = usePathname();
  const { activeHref } = getActiveRoute(pathname);
  const [open, setOpen] = useState(false);
  const [shownFor, setShownFor] = useState(pathname);
  if (shownFor !== pathname) {          // the layout persists across navigation: close on route change
    setShownFor(pathname);
    setOpen(false);
  }
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root, toggle);

  return (
    <div className="mobile-nav" ref={root}>
      <button ref={toggle} type="button" className="menu-toggle" aria-expanded={open} aria-controls="mobile-nav-panel"
              aria-label={open ? "Close menu" : "Open menu"} onClick={() => setOpen((o) => !o)}>
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
          <path d={open ? "M4 4l10 10M14 4L4 14" : "M3 5h12M3 9h12M3 13h12"} fill="none" stroke="currentColor"
                strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      </button>
      {open && (
        <nav id="mobile-nav-panel" className="mobile-nav-panel" aria-label="All pages">
          {NAV_GROUPS.map((g) => (
            <div key={g.label} className="mobile-nav-group">
              <span className="mobile-nav-title">{g.label}</span>
              {g.pages.map((p) => (
                <Link key={p.href} href={p.href} aria-current={p.href === activeHref ? "page" : undefined} onClick={close}>
                  {p.label}
                </Link>
              ))}
            </div>
          ))}
          <div className="mobile-nav-theme">
            <span className="mobile-nav-title">Theme</span>
            <ThemeSwitch labels />
          </div>
          {projection && (
            <div className="mobile-nav-theme">
              <span className="mobile-nav-title">Weekly projections</span>
              <ProjectionSwitch value={projection} labels />
            </div>
          )}
        </nav>
      )}
    </div>
  );
}
