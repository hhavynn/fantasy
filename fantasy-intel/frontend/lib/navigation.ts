export type NavGroupId = "insights" | "more";

export type NavItem = {
  label: string;
  href: string;
  exact?: boolean;
};

export type NavGroup = {
  id: NavGroupId;
  label: string;
  items: NavItem[];
};

export const PRIMARY_NAV_ITEMS: NavItem[] = [
  { label: "Overview", href: "/", exact: true },
  { label: "My Team", href: "/my-team/value" },
  { label: "Players", href: "/players" },
  { label: "Waivers", href: "/waivers" },
  { label: "Trades", href: "/trades" },
  { label: "Trade Finder", href: "/trades/discover" },
];

export const NAV_GROUPS: NavGroup[] = [
  {
    id: "insights",
    label: "Insights",
    items: [
      { label: "Schedule Intelligence", href: "/schedule-intelligence" },
      { label: "Playoff Race", href: "/playoff-race" },
      { label: "Luck Lab", href: "/luck-lab" },
    ],
  },
  {
    id: "more",
    label: "More",
    items: [
      { label: "Managers", href: "/managers" },
      { label: "Data Quality", href: "/data" },
    ],
  },
];

export const ALL_NAV_ITEMS: NavItem[] = [
  ...PRIMARY_NAV_ITEMS,
  ...NAV_GROUPS.flatMap((g) => g.items),
];

export type ActiveRoute = {
  activeHref: string | null;
  activeGroupId: NavGroupId | null;
};

/**
 * Determines which navigation item and group (if any) are active for the given pathname.
 * - '/' strictly requires exact match.
 * - Subroutes use boundary-aware matching (e.g. /managers/1 matches /managers).
 * - When multiple routes match (e.g. /trades and /trades/discover), the longest/most specific href wins.
 * - Unknown routes return activeHref: null, activeGroupId: null (never incorrectly highlights Overview).
 */
export function getActiveRoute(pathname: string): ActiveRoute {
  if (!pathname) return { activeHref: null, activeGroupId: null };

  // Normalize: remove trailing slash unless it's the root '/'
  const normalized = pathname.length > 1 && pathname.endsWith("/")
    ? pathname.slice(0, -1)
    : pathname;

  const matches: NavItem[] = [];

  for (const item of ALL_NAV_ITEMS) {
    if (item.exact || item.href === "/") {
      if (normalized === item.href) {
        matches.push(item);
      }
    } else {
      if (normalized === item.href || normalized.startsWith(`${item.href}/`)) {
        matches.push(item);
      }
    }
  }

  if (matches.length === 0) {
    return { activeHref: null, activeGroupId: null };
  }

  // Sort by href length descending to prioritize the most specific route
  matches.sort((a, b) => b.href.length - a.href.length);
  const bestMatch = matches[0];

  let activeGroupId: NavGroupId | null = null;
  for (const group of NAV_GROUPS) {
    if (group.items.some((i) => i.href === bestMatch.href)) {
      activeGroupId = group.id;
      break;
    }
  }

  return {
    activeHref: bestMatch.href,
    activeGroupId,
  };
}
