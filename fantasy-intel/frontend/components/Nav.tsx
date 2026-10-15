"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";

type Page = { href: string; label: string };
type Group = { label: string; pages: Page[] };

// Data quality is reached from the sync pill, not the nav.
const GROUPS: Group[] = [
  {
    label: "My team",
    pages: [{ href: "/", label: "Overview" }, { href: "/start-sit", label: "Start/Sit" }, { href: "/my-team/value", label: "Team value" }],
  },
  { label: "Players", pages: [{ href: "/players", label: "Players" }, { href: "/waivers", label: "Waivers" }] },
  { label: "Trades", pages: [{ href: "/trades", label: "Analyzer" }, { href: "/trades/discover", label: "Discover" }] },
  {
    label: "League",
    pages: [
      { href: "/schedule-intelligence", label: "Schedule" },
      { href: "/luck-lab", label: "Luck lab" },
      { href: "/playoff-race", label: "Playoff race" },
      { href: "/managers", label: "Managers" },
    ],
  },
];

/** Longest matching prefix wins, so /trades/discover marks only "Discover". */
function activeHref(pathname: string): string | null {
  let best: string | null = null;
  for (const { href } of GROUPS.flatMap((g) => g.pages)) {
    const hit = href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
    if (hit && (!best || href.length > best.length)) best = href;
  }
  return best;
}

function useActive() {
  const href = activeHref(usePathname());
  const group = GROUPS.find((g) => g.pages.some((p) => p.href === href)) ?? null;
  return { href, group };
}

export function NavGroups() {
  const { group } = useActive();
  const pathname = usePathname();
  // The layout persists across client navigation, so close any open popover (sync details) on route change.
  useEffect(() => {
    document.querySelectorAll("details.sync[open]").forEach((d) => d.removeAttribute("open"));
  }, [pathname]);
  return (
    <nav className="nav-groups" aria-label="Sections">
      {GROUPS.map((g) => (
        <Link key={g.label} href={g.pages[0].href} aria-current={g === group ? "true" : undefined}>
          {g.label}
        </Link>
      ))}
    </nav>
  );
}

export function SubNav() {
  const { href, group } = useActive();
  if (!group) return null;
  return (
    <nav className="subnav" aria-label={`${group.label} pages`}>
      {group.pages.map((p) => (
        <Link key={p.href} href={p.href} aria-current={p.href === href ? "page" : undefined}>
          {p.label}
        </Link>
      ))}
    </nav>
  );
}
