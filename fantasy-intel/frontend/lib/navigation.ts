export type NavPage = {
  href: string;
  label: string;
  /** One line on the question the page answers: link titles and palette search. Optional, so an entry can be
   *  appended as `{ href, label }`. */
  summary?: string;
  /** A labelled cluster inside the group (the advanced "Research" tools); absent = the group's everyday pages. */
  section?: string;
  /** Owner research (the model's evidence): on the multi-league site only platform admins see it (the API answers
   *  everyone else 404). */
  ownerOnly?: boolean;
};
export type NavGroup = {
  label: string;
  /** The job the group is for, in a few words: the desktop workflow rail prints it under the label so a first-time
   *  visitor sees the five main tasks without opening anything. Optional, so a group can be added as `{ label, pages }`. */
  task?: string;
  pages: NavPage[];
};

// Section groups, organised by the decision a manager came to make rather than by when a feature was built:
// - My team: this week's lineup decisions for the analyzed roster (every page here is team-scoped).
// - Waivers: who to add (and stream) and who to drop. Its own destination because it is the other weekly job.
// - Players: find, rank and compare players; the "Research" cluster holds the model and opportunity tools.
// - Trades: find a trade, then evaluate it.
// - League: what happened and where the league stands.
// Five groups fit a phone tab bar one-to-one. Wide desktops show them in the workflow rail (label and task) with the
// active group's pages beneath; narrower screens show the groups in the app bar and a sub-nav for the active group's
// pages. Data quality is reached from the sync status, not the nav, and the multi-league prototype (/leagues)
// stays out of it while it is feature-flagged.
export const NAV_GROUPS: NavGroup[] = [
  {
    label: "My team",
    task: "Set this week's lineup",
    pages: [
      { href: "/", label: "Overview", summary: "This week at a glance: matchup, action items, roster and standings" },
      { href: "/start-sit", label: "Start/Sit", summary: "The submitted lineup against the best projected one" },
      { href: "/matchup", label: "Matchup", summary: "This week's head-to-head, lineup against lineup" },
      { href: "/injury-impact", label: "Injuries", summary: "Who is out or questionable this week, and whose role could grow" },
      { href: "/injury-impact/timeline", label: "Availability", summary: "Every injury report on your roster in order, with its source and time" },
      { href: "/my-team/value", label: "Team value", summary: "Roster value and lineup strength against the league" },
    ],
  },
  {
    label: "Waivers",
    task: "Add and drop players",
    pages: [
      { href: "/waivers", label: "Waivers", summary: "Free agents the analysis supports for this roster, with the drop" },
      { href: "/kickers", label: "Kickers", summary: "Weekly kicker projections with game context, free agents included" },
      { href: "/defenses", label: "D/ST", summary: "Weekly D/ST projections with the betting line, free agents included" },
    ],
  },
  {
    label: "Players",
    task: "Rank and research players",
    pages: [
      { href: "/rankings", label: "Rankings", summary: "Every player ranked by projection, filtered by position and horizon" },
      { href: "/players", label: "Player profile", summary: "One player's usage, game log and outlook" },
      { href: "/compare", label: "Compare", summary: "Players side by side on projection, usage and schedule" },
      { href: "/research", label: "Research lab", section: "Research", summary: "Opportunity against production for WR, RB and TE" , ownerOnly: true },
      { href: "/screener", label: "Screener", section: "Research", summary: "Every WR, RB and TE filtered on several usage and production metrics at once" },
      { href: "/schedule-intelligence", label: "Schedule", section: "Research", summary: "Upcoming opponents for each rostered player" },
      { href: "/schedule-intelligence/sos", label: "Strength of schedule", section: "Research", summary: "Opponent pass defense (QB, WR, TE) and run defense (RB) by week, for the rest of the season and the fantasy playoffs" },
      { href: "/rankings/compare", label: "Source comparison", section: "Research", summary: "Where the provider's projections and Fantasy Intel's own rank a player differently, with the evidence behind large gaps" },
      { href: "/movers", label: "Projection movers", section: "Research", summary: "Who moved between two stored forecasts" },
      { href: "/projection-performance", label: "Projection accuracy", section: "Research", summary: "How past projections compared with results" , ownerOnly: true },
    ],
  },
  {
    label: "Trades",
    task: "Find and weigh trades",
    pages: [
      { href: "/trades", label: "Analyzer", summary: "Build a trade and see its exact lineup impact, week by week" },
      { href: "/trades/discover", label: "Discover", summary: "Trade ideas found by searching every roster" },
      { href: "/trades/values", label: "Trade values", summary: "Every rostered player's model value above replacement, with its calculation" },
    ],
  },
  {
    label: "League",
    task: "Results, standings and rules",
    pages: [
      { href: "/weekly-recap", label: "Weekly recap", summary: "Scores, standouts and moves from a completed week" },
      { href: "/transactions", label: "Transactions", summary: "Every add, drop and trade this season, filtered by manager, type, position and week" },
      { href: "/power-rankings", label: "Power rankings", summary: "Which rosters are strongest now, and who moved since last week" },
      { href: "/league-analyzer", label: "League analyzer", summary: "Every roster's strength position by position, and any two teams side by side" },
      { href: "/playoff-race", label: "Playoff race", summary: "Standings, remaining games and the playoff line" },
      { href: "/managers", label: "Managers", summary: "How each manager drafts, trades and sets lineups" },
      { href: "/luck-lab", label: "Luck lab", summary: "Expected wins and schedule luck for every team" },
      { href: "/league-rules", label: "Rules", summary: "Scoring, lineup slots, waivers and trades as the league set them" },
    ],
  },
];

export const ALL_NAV_PAGES: NavPage[] = NAV_GROUPS.flatMap((g) => g.pages);

/** The groups a visitor may see: without the owner research pages when `hideOwnerOnly`. */
export function visibleGroups(hideOwnerOnly: boolean): NavGroup[] {
  if (!hideOwnerOnly) return NAV_GROUPS;
  return NAV_GROUPS.map((g) => ({ ...g, pages: g.pages.filter((p) => !p.ownerOnly) }));
}

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

/** Where a page sits in the registry, for page-context lines: its group and, inside the group, its labelled cluster. */
export type PageContext = { group: string; section: string | null; page: NavPage };

/** The registry context of `pathname` (the same matching as `getActiveRoute`), or null for a route in no group. */
export function pageContext(pathname: string | null | undefined): PageContext | null {
  const { activeHref, activeGroup } = getActiveRoute(pathname);
  const page = activeGroup?.pages.find((p) => p.href === activeHref);
  return activeGroup && page ? { group: activeGroup.label, section: page.section ?? null, page } : null;
}

/** The context as an eyebrow line: "Players · Research", "Trades". */
export function pageContextLabel(ctx: PageContext | null): string | null {
  return ctx ? [ctx.group, ctx.section].filter(Boolean).join(" · ") : null;
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

// ---------- Desktop workflow rail ----------
// Wide desktops (see `.rail` in app/globals.css) show the five groups in a left rail. Collapsing it leaves the group
// icons and brings back the horizontal sub-nav, so no page is ever one hidden menu away. The choice is a per-browser
// convenience (localStorage), applied to <html data-rail> before the first paint like the theme.

export const RAIL_KEY = "fantasy-intel:rail";
export type RailState = "expanded" | "collapsed";

/** A stored rail state; anything else (nothing stored, blocked storage, junk) is the default, expanded. */
export function parseRail(raw: string | null | undefined): RailState {
  return raw === "collapsed" ? "collapsed" : "expanded";
}

/** Runs in <head> before paint: a collapsed rail is on <html> before any content renders (no flash, no shift). */
export const RAIL_INIT_SCRIPT =
  `(function(){try{if(localStorage.getItem(${JSON.stringify(RAIL_KEY)})==="collapsed")` +
  `document.documentElement.setAttribute("data-rail","collapsed")}catch(e){}})()`;
