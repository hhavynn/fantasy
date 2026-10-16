export type NavPage = { href: string; label: string };
export type NavGroup = { label: string; pages: NavPage[] };

// Section groups; the app bar shows the groups and a sub-nav for the active group's pages.
// Data quality is reached from the sync status, not the nav.
export const NAV_GROUPS: NavGroup[] = [
  {
    label: "My team",
    pages: [
      { href: "/", label: "Overview" },
      { href: "/matchup", label: "Matchup" },
      { href: "/start-sit", label: "Start/Sit" },
      { href: "/my-team/value", label: "Team value" },
    ],
  },
  {
    label: "Players",
    pages: [
      { href: "/players", label: "Players" },
      { href: "/research", label: "Research lab" },
      { href: "/waivers", label: "Waivers" },
    ],
  },
  { label: "Trades", pages: [{ href: "/trades", label: "Analyzer" }, { href: "/trades/discover", label: "Discover" }] },
  {
    label: "League",
    pages: [
      { href: "/schedule-intelligence", label: "Schedule" },
      { href: "/weekly-recap", label: "Weekly recap" },
      { href: "/luck-lab", label: "Luck lab" },
      { href: "/playoff-race", label: "Playoff race" },
      { href: "/managers", label: "Managers" },
    ],
  },
];

export const ALL_NAV_PAGES: NavPage[] = NAV_GROUPS.flatMap((g) => g.pages);

export type ActiveRoute = { activeHref: string | null; activeGroup: NavGroup | null };

/**
 * The nav page and group that `pathname` belongs to.
 * - "/" matches only itself, so unknown routes never highlight Overview.
 * - Other pages match themselves and their sub-paths on a "/" boundary (/managers/1, not /managersx).
 * - The longest match wins, so /trades/discover marks Discover, not Analyzer.
 * - Unknown routes return nulls.
 */
export function getActiveRoute(pathname: string | null | undefined): ActiveRoute {
  if (!pathname) return { activeHref: null, activeGroup: null };
  const path = pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
  let best: string | null = null;
  for (const { href } of ALL_NAV_PAGES) {
    const hit = href === "/" ? path === "/" : path === href || path.startsWith(`${href}/`);
    if (hit && (!best || href.length > best.length)) best = href;
  }
  return { activeHref: best, activeGroup: NAV_GROUPS.find((g) => g.pages.some((p) => p.href === best)) ?? null };
}
