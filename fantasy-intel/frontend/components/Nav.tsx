"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useRef, useState } from "react";
import { NAV_GROUPS, getActiveRoute } from "@/lib/navigation";
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
    <nav className="subnav" aria-label={`${activeGroup.label} pages`}>
      {activeGroup.pages.map((p) => (
        <Link key={p.href} href={p.href} aria-current={p.href === activeHref ? "page" : undefined}>
          {p.label}
        </Link>
      ))}
      <ScrollToActive current={activeHref} />
    </nav>
  );
}

/** Phones: the section row collapses into a menu button with every group's pages. */
export function MobileNav() {
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
        </nav>
      )}
    </div>
  );
}
