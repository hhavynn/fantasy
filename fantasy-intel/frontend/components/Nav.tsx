"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS: { href: string; label: string }[] = [
  { href: "/", label: "Overview" },
  { href: "/players", label: "Players" },
  { href: "/waivers", label: "Waivers" },
  { href: "/trades", label: "Trades" },
  { href: "/trades/discover", label: "Discover trades" },
  { href: "/schedule-intelligence", label: "Schedule" },
  { href: "/luck-lab", label: "Luck lab" },
  { href: "/playoff-race", label: "Playoff race" },
  { href: "/my-team/value", label: "Team value" },
  { href: "/managers", label: "Managers" },
  { href: "/data", label: "Data quality" },
];

/** Longest matching prefix wins, so /trades/discover marks only "Discover trades". */
function activeHref(pathname: string): string | null {
  let best: string | null = null;
  for (const { href } of LINKS) {
    const hit = href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
    if (hit && (!best || href.length > best.length)) best = href;
  }
  return best;
}

export function Nav() {
  const active = activeHref(usePathname());
  return (
    <nav className="nav" aria-label="Main">
      {LINKS.map(({ href, label }) => (
        <Link key={href} href={href} aria-current={href === active ? "page" : undefined}>
          {label}
        </Link>
      ))}
    </nav>
  );
}
