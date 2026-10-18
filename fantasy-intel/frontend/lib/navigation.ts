export type NavPage = {
  href: string;
  label: string;
  /** One line on the question the page answers: link titles and palette search. Optional, so an entry can be
   *  appended as `{ href, label }`. */
  summary?: string;
  /** A labelled cluster inside the group (the advanced "Research" tools); absent = the group's everyday pages. */
  section?: string;
};
export type NavGroup = { label: string; pages: NavPage[] };

// Section groups, organised by what a manager came to do rather than by when a feature was built:
// - My team: this week's decisions for the analyzed roster (every page here is team-scoped).
// - Players: find, rank and compare players; the "Research" cluster holds the model and opportunity tools.
// - Trades: find a trade, then evaluate it.
// - League: what happened and where the league stands.
// The app bar shows the groups and a sub-nav for the active group's pages. Data quality is reached from the sync
// status, not the nav, and the multi-league prototype (/leagues) stays out of it while it is feature-flagged.
export const NAV_GROUPS: NavGroup[] = [
  {
    label: "My team",
    pages: [
      { href: "/", label: "Overview", summary: "This week at a glance: matchup, action items, roster and standings" },
      { href: "/matchup", label: "Matchup", summary: "This week's head-to-head, lineup against lineup" },
      { href: "/start-sit", label: "Start/Sit", summary: "The submitted lineup against the best projected one" },
      { href: "/waivers", label: "Waivers", summary: "Free agents the analysis supports for this roster, with the drop" },
      { href: "/my-team/value", label: "Team value", summary: "Roster value and lineup strength against the league" },
    ],
  },
  {
    label: "Players",
    pages: [
      { href: "/rankings", label: "Rankings", summary: "Every player ranked by projection, filtered by position and horizon" },
      { href: "/players", label: "Players", summary: "One player's usage, game log and outlook" },
      { href: "/compare", label: "Compare", summary: "Players side by side on projection, usage and schedule" },
      { href: "/kickers", label: "Kicker outlook", summary: "Weekly kicker projections with game context" },
      { href: "/research", label: "Research lab", section: "Research", summary: "Opportunity against production for WR, RB and TE" },
      { href: "/schedule-intelligence", label: "Schedule", section: "Research", summary: "Upcoming opponents for each rostered player" },
      { href: "/movers", label: "Projection movers", section: "Research", summary: "Who moved between two stored forecasts" },
      { href: "/projection-performance", label: "Projection accuracy", section: "Research", summary: "How past projections compared with results" },
    ],
  },
  {
    label: "Trades",
    pages: [
      { href: "/trades", label: "Analyzer", summary: "Build a trade and see its exact lineup impact, week by week" },
      { href: "/trades/discover", label: "Discover", summary: "Trade ideas found by searching every roster" },
    ],
  },
  {
    label: "League",
    pages: [
      { href: "/weekly-recap", label: "Weekly recap", summary: "Scores, standouts and moves from a completed week" },
      { href: "/power-rankings", label: "Power rankings", summary: "Which rosters are strongest now, and who moved since last week" },
      { href: "/playoff-race", label: "Playoff race", summary: "Standings, remaining games and the playoff line" },
      { href: "/managers", label: "Managers", summary: "How each manager drafts, trades and sets lineups" },
      { href: "/luck-lab", label: "Luck lab", summary: "Expected wins and schedule luck for every team" },
    ],
  },
];

export const ALL_NAV_PAGES: NavPage[] = NAV_GROUPS.flatMap((g) => g.pages);

export type NavCluster = { title: string | null; pages: NavPage[] };

/** A group's pages split into its unlabelled everyday pages and its labelled clusters, in nav order. */
export function navClusters(group: NavGroup): NavCluster[] {
  const out: NavCluster[] = [];
  for (const p of group.pages) {
    const title = p.section ?? null;
    const last = out[out.length - 1];
    if (last && last.title === title) last.pages.push(p);
    else out.push({ title, pages: [p] });
  }
  return out;
}

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

// ---------- Phone tab bar memory ----------
// Each bottom tab returns to the page last open in its group (with that page's filters), the way native tab bars
// keep their place; tapping the tab of the group you are already in goes back to the group's first page.

/** The team params (`TEAM_PARAM` and its legacy alias in lib/team-perspective.ts). Kept out of the memory so the
 *  current team is re-applied by `withTeam`, never restored from an older visit. */
const TEAM_KEYS = ["team", "team_id"];

/** One remembered page: where it was (minus the team) and which team was analyzed there. */
export type TabMemoryEntry = { href: string; team: string | null };
export type TabMemory = Record<string, TabMemoryEntry>;

/** The memory entry for the page at `pathname?search`, or null when the path is in no group. */
export function tabMemoryEntry(pathname: string, search: string, team: string | null): { group: string; entry: TabMemoryEntry } | null {
  const { activeGroup } = getActiveRoute(pathname);
  if (!activeGroup) return null;
  const qs = new URLSearchParams(search);
  for (const k of TEAM_KEYS) qs.delete(k);
  const query = qs.toString();
  return { group: activeGroup.label, entry: { href: query ? `${pathname}?${query}` : pathname, team } };
}

/**
 * Where a group's tab goes: its first page when the group is already active (or nothing is remembered), else the
 * remembered page. A page remembered under another team keeps only its path, so an opponent, trade or schedule
 * picked for one team is never replayed for another. The caller adds the current team with `withTeam`.
 */
export function tabTarget(group: NavGroup, isActive: boolean, remembered: TabMemoryEntry | undefined, team: string | null): string {
  const root = group.pages[0].href;
  if (isActive || !remembered) return root;
  const [path] = remembered.href.split("?");
  if (getActiveRoute(path).activeGroup?.label !== group.label) return root;
  return remembered.team === team ? remembered.href : path;
}

/** Parse the stored memory defensively: anything malformed or off-site is dropped. */
export function parseTabMemory(raw: string | null): TabMemory {
  if (!raw) return {};
  try {
    const data: unknown = JSON.parse(raw);
    if (!data || typeof data !== "object" || Array.isArray(data)) return {};
    const out: TabMemory = {};
    for (const [k, v] of Object.entries(data as Record<string, unknown>)) {
      if (!v || typeof v !== "object") continue;
      const { href, team } = v as Record<string, unknown>;
      if (typeof href !== "string" || !href.startsWith("/") || href.startsWith("//")) continue;
      if (team !== null && typeof team !== "string") continue;
      out[k] = { href, team: team as string | null };
    }
    return out;
  } catch {
    return {};
  }
}
