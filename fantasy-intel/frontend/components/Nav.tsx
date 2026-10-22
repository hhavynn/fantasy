"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useAppPathname, useLeagueHref, useLeagueId } from "./useAppPathname";
import {
  type ChangeEvent, type CSSProperties, type FocusEvent, type ReactNode, useCallback, useEffect, useMemo, useRef, useState,
  useSyncExternalStore, useTransition,
} from "react";
import {
  NAV_GROUPS, RAIL_KEY, getActiveRoute, navClusters, parseRail, parseTabMemory, tabMemoryEntry, tabTarget,
  type NavGroup, type RailState, type TabMemoryEntry,
} from "@/lib/navigation";
import type { ProjectionSource } from "@/lib/projection-source";
import type { SyncState } from "@/lib/sync-status";
import { isTeamScoped, perspectiveHref, teamOptionLabel, withTeam } from "@/lib/team-perspective";
import { useNavGroups } from "./NavVisibility";
import { ProjectionSwitch } from "./ProjectionSwitch";
import { ThemeSwitch } from "./ThemeSwitch";
import { ScrollToActive } from "./ui/ScrollToActive";
import { useDismiss } from "./useDismiss";
import { rememberTeam, useTeam } from "./useTeam";

/** The wordmark: home is the Overview, so it keeps the analyzed team like every other link to a team-scoped page. */
export function BrandLink({ children }: { children: ReactNode }) {
  const team = useTeam();
  const leagueHref = useLeagueHref();
  return <Link href={leagueHref(withTeam("/", team))} className="brand" aria-label="Fantasy Intel home">{children}</Link>;
}

export function NavGroups() {
  const { activeGroup } = getActiveRoute(useAppPathname());
  const team = useTeam();
  const leagueHref = useLeagueHref();
  return (
    <nav className="nav-groups" aria-label="Sections">
      {NAV_GROUPS.map((g) => (
        <Link key={g.label} href={leagueHref(withTeam(g.pages[0].href, team))} aria-current={g === activeGroup ? "true" : undefined}>
          {g.label}
        </Link>
      ))}
    </nav>
  );
}

/** The current route's group as this visitor sees it (owner research hidden on league pages), and its current page. */
function useActiveGroup(): { activeHref: string | null; activeGroup: NavGroup | null } {
  const { activeHref, activeGroup: found } = getActiveRoute(useAppPathname());
  const groups = useNavGroups();
  return { activeHref, activeGroup: found ? groups.find((g) => g.label === found.label) ?? found : null };
}

/**
 * The active group's pages as a sideways band under the app bar: tablets and narrow desktops, phones (scrolling away
 * with the page) and wide desktops with the workflow rail collapsed. Wide desktops with the rail open list the same
 * pages in the rail instead (globals.css hides one or the other, so exactly one "<group> pages" nav is visible).
 */
export function SubNav() {
  const { activeHref, activeGroup } = useActiveGroup();
  const team = useTeam();
  const leagueHref = useLeagueHref();
  if (!activeGroup) return null;
  return (
    <div className="subnav-wrap">
      <nav className="subnav" aria-label={`${activeGroup.label} pages`}>
        <span className="subnav-label" aria-hidden="true">{activeGroup.label}</span>
        {navClusters(activeGroup).map((c) => (
          <span key={c.title ?? ""} className="subnav-cluster">
            {c.title && <span className="subnav-section">{c.title}</span>}
            {c.pages.map((p) => (
              <Link key={p.href} href={leagueHref(withTeam(p.href, team))} title={p.summary}
                    aria-current={p.href === activeHref ? "page" : undefined}>
                {p.label}
              </Link>
            ))}
          </span>
        ))}
        <ScrollToActive current={activeHref} />
      </nav>
    </div>
  );
}

// ---------- Desktop workflow rail ----------
// The rail state is the `data-rail` attribute on <html> (set before paint by RAIL_INIT_SCRIPT in the root layout), so
// CSS lays out the right width from the first frame; this store reads and writes it. Storage can be blocked: the
// choice then lasts for this visit.
const railListeners = new Set<() => void>();
const subscribeRail = (cb: () => void) => {
  railListeners.add(cb);
  return () => { railListeners.delete(cb); };
};
const readRail = (): RailState => parseRail(document.documentElement.getAttribute("data-rail"));
function setRail(state: RailState) {
  const root = document.documentElement;
  if (state === "collapsed") root.setAttribute("data-rail", "collapsed");
  else root.removeAttribute("data-rail");
  try {
    if (state === "collapsed") window.localStorage.setItem(RAIL_KEY, state);
    else window.localStorage.removeItem(RAIL_KEY);
  } catch {
    // Blocked storage: the rail still collapses for this visit.
  }
  railListeners.forEach((l) => l());
}

/** The league's teams for the rail's picker, tagged with the league they were read for (null = the classic site). */
export type RailTeams = {
  league: string | null;
  defaultId: number | null;
  teams: { id: number; name: string; isDefault: boolean }[];
};

/**
 * The persistent team picker. On a team-scoped page a pick reloads the page for that team (the same URL the page's
 * own "Analyzing" picker builds, so back works and the link can be shared); on a league-wide page it changes the team
 * the nav links carry, without leaving the page. Read-only either way: nothing is sent to the provider. A list read for
 * another league (the moment between a league switch and the layout's refresh) is never offered.
 */
function RailTeamPicker({ teams }: { teams: RailTeams }) {
  const league = useLeagueId();
  const pathname = useAppPathname();
  const search = useSearchParams();
  const team = useTeam();
  const leagueHref = useLeagueHref();
  const router = useRouter();
  const [pending, start] = useTransition();
  if (teams.league !== league || teams.teams.length === 0) return null;
  const wanted = team ?? (teams.defaultId === null ? "" : String(teams.defaultId));
  const value = teams.teams.some((t) => String(t.id) === wanted) ? wanted : "";
  const onChange = (e: ChangeEvent<HTMLSelectElement>) => {
    const id = Number(e.target.value);
    const next = id === teams.defaultId ? null : id;
    if (isTeamScoped(pathname)) start(() => router.push(leagueHref(perspectiveHref(pathname, search, next))));
    else rememberTeam(league, next === null ? null : String(next));
  };
  return (
    <label className="rail-field">
      <span className="rail-field-label">Team</span>
      <select value={value} onChange={onChange} aria-busy={pending || undefined}>
        {value === "" && <option value="" disabled>Pick a team</option>}
        {teams.teams.map((t) => (
          <option key={t.id} value={String(t.id)}>{teamOptionLabel({ name: t.name, is_owner_default: t.isDefault })}</option>
        ))}
      </select>
    </label>
  );
}

/**
 * Wide desktops: the workspace rail beside the page. The league week and the analyzed team on top, the five decision
 * groups with the job each is for (all visible, nothing to open), then the current group's pages. Collapsing it keeps
 * the group icons and returns the page list to the sub-nav band. Hidden below the rail breakpoint (globals.css), where
 * the app bar and the phone tab bar take over unchanged.
 */
export function WorkspaceRail({ teams, week }: { teams: RailTeams | null; week: number | null }) {
  const { activeHref, activeGroup } = useActiveGroup();
  const team = useTeam();
  const leagueHref = useLeagueHref();
  const rail = useSyncExternalStore(subscribeRail, readRail, () => "expanded" as const);
  const collapsed = rail === "collapsed";
  return (
    <aside id="workspace-rail" className="rail" aria-label="Workspace">
      {(teams || week !== null) && (
        <div className="rail-context">
          {week !== null && <p className="rail-week">Week {week}</p>}
          {teams && <RailTeamPicker teams={teams} />}
        </div>
      )}
      <nav className="rail-groups" aria-label="Sections">
        <ul>
          {NAV_GROUPS.map((g, i) => (
            <li key={g.label}>
              <Link href={leagueHref(withTeam(g.pages[0].href, team))} title={g.label}
                    aria-current={g.label === activeGroup?.label ? "true" : undefined}
                    aria-describedby={g.task ? `rail-task-${i}` : undefined}>
                <svg viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  {GROUP_ICONS[g.label] ?? <circle cx="9" cy="9" r="3" />}
                </svg>
                <span className="rail-label">{g.label}</span>
                {g.task && <span id={`rail-task-${i}`} className="rail-task" aria-hidden="true">{g.task}</span>}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      {activeGroup && (
        <nav className="rail-pages" aria-label={`${activeGroup.label} pages`}>
          <span className="rail-pages-title" aria-hidden="true">{activeGroup.label}</span>
          {navClusters(activeGroup).map((c) => (
            <div key={c.title ?? ""} className="rail-cluster">
              {c.title && <span className="rail-section">{c.title}</span>}
              {c.pages.map((p) => (
                <Link key={p.href} href={leagueHref(withTeam(p.href, team))} title={p.summary}
                      aria-current={p.href === activeHref ? "page" : undefined}>
                  {p.label}
                </Link>
              ))}
            </div>
          ))}
        </nav>
      )}
      <button type="button" className="rail-toggle" aria-controls="workspace-rail" aria-expanded={!collapsed}
              aria-label={collapsed ? "Expand navigation" : "Collapse navigation"} title={collapsed ? "Expand navigation" : "Collapse navigation"}
              onClick={() => setRail(collapsed ? "expanded" : "collapsed")}>
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path d={collapsed ? "M5.5 2.75v10.5M8.5 6l2 2-2 2" : "M5.5 2.75v10.5M10.5 6l-2 2 2 2"} fill="none" stroke="currentColor"
                strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span className="rail-toggle-text" aria-hidden="true">Collapse</span>
      </button>
    </aside>
  );
}

/** Line icons for the phone tab bar, keyed by group label; a group without one gets a plain dot. */
const GROUP_ICONS: Record<string, ReactNode> = {
  "My team": <path d="M9 2.5 15 5v4.2c0 3.6-2.5 5.6-6 6.8-3.5-1.2-6-3.2-6-6.8V5l6-2.5Z" />,
  Waivers: <><circle cx="9" cy="9" r="6.5" /><path d="M9 6v6M6 9h6" /></>,
  Players: <><circle cx="9" cy="6" r="3" /><path d="M3.5 15.5c.6-2.9 2.8-4.5 5.5-4.5s4.9 1.6 5.5 4.5" /></>,
  Trades: <><path d="M3 6h11l-3-3" /><path d="M15 12H4l3 3" /></>,
  League: <><path d="M3 15.5h12" /><path d="M5 15.5V9" /><path d="M9 15.5V4" /><path d="M13 15.5v-4" /></>,
};

// The tab bar's memory of the last page in each group lives for the browser tab (sessionStorage), so a new visit
// starts from each group's first page. Storage can be blocked; the tabs then simply open each group's first page.
const TAB_MEMORY_KEY = "fantasy-intel:tab-memory";
const memoryListeners = new Set<() => void>();
const subscribeMemory = (cb: () => void) => {
  memoryListeners.add(cb);
  return () => { memoryListeners.delete(cb); };
};
function readMemory(): string | null {
  try {
    return window.sessionStorage.getItem(TAB_MEMORY_KEY);
  } catch {
    return null;
  }
}
function rememberPage(group: string, entry: TabMemoryEntry) {
  const memory = parseTabMemory(readMemory());
  const prev = memory[group];
  if (prev && prev.href === entry.href && prev.team === entry.team) return;
  try {
    window.sessionStorage.setItem(TAB_MEMORY_KEY, JSON.stringify({ ...memory, [group]: entry }));
  } catch {
    return;
  }
  memoryListeners.forEach((l) => l());
}

/**
 * Phones: one tap to any section. A tab reopens the page last used in its group (with its filters, for the same
 * team); the tab of the current group goes back to the group's first page. The sub-nav lists the group's pages.
 */
export function TabBar() {
  const pathname = useAppPathname();
  const search = useSearchParams();
  const team = useTeam();
  const leagueHref = useLeagueHref();
  const query = search.toString();
  const { activeGroup } = getActiveRoute(pathname);
  const raw = useSyncExternalStore(subscribeMemory, readMemory, () => null);
  const memory = useMemo(() => parseTabMemory(raw), [raw]);

  useEffect(() => {
    const hit = tabMemoryEntry(pathname, query, team);
    if (hit) rememberPage(hit.group, hit.entry);
  }, [pathname, query, team]);

  return (
    <nav className="tabbar" aria-label="Sections" style={{ "--tabs": NAV_GROUPS.length } as CSSProperties}>
      {NAV_GROUPS.map((g) => (
        <Link key={g.label} href={leagueHref(withTeam(tabTarget(g, g === activeGroup, memory[g.label], team), team))}
              aria-current={g === activeGroup ? "true" : undefined}>
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

/** Phones: the section row collapses into a menu button with every group's pages, the switches and data freshness. */
export function MobileNav({ projection, sync }: { projection?: ProjectionSource; sync?: { state: SyncState; text: string } } = {}) {
  const pathname = useAppPathname();
  const search = useSearchParams();
  const { activeHref } = getActiveRoute(pathname);
  const groups = useNavGroups();
  const team = useTeam();
  const leagueHref = useLeagueHref();
  const [open, setOpen] = useState(false);
  // The layout persists across navigation, so the menu closes whenever the URL changes, including back/forward and
  // query-only changes (a team switch), not only after a click inside it. Adjusting state during render on a changed
  // value is React's documented alternative to an effect here: no extra paint with a stale open menu.
  const url = `${pathname}?${search.toString()}`;
  const [shownFor, setShownFor] = useState(url);
  if (shownFor !== url) {
    setShownFor(url);
    setOpen(false);
  }
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root, toggle);
  // Tabbing out of the menu closes it, so it never stays open over the page for a keyboard user.
  const onBlur = (e: FocusEvent<HTMLDivElement>) => {
    if (open && e.relatedTarget instanceof Node && !e.currentTarget.contains(e.relatedTarget)) close();
  };

  return (
    <div className="mobile-nav" ref={root} onBlur={onBlur}>
      <button ref={toggle} type="button" className="menu-toggle" aria-expanded={open} aria-controls="mobile-nav-panel"
              data-sync={sync?.state}
              aria-label={`${open ? "Close menu" : "Open menu"}${sync && sync.state !== "ok" ? ` (${sync.text})` : ""}`}
              onClick={() => setOpen((o) => !o)}>
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
          <path d={open ? "M4 4l10 10M14 4L4 14" : "M3 5h12M3 9h12M3 13h12"} fill="none" stroke="currentColor"
                strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      </button>
      {open && (
        <nav id="mobile-nav-panel" className="mobile-nav-panel" aria-label="All pages">
          {sync && (
            <Link href={leagueHref("/data")} className="mobile-nav-sync" data-state={sync.state} onClick={close}>
              <span><b>Data</b> · {sync.text}</span>
              <span aria-hidden="true">→</span>
            </Link>
          )}
          {groups.map((g) => (
            <div key={g.label} className="mobile-nav-group" role="group" aria-labelledby={`mnav-${g.label}`}>
              <span id={`mnav-${g.label}`} className="mobile-nav-title">{g.label}</span>
              {navClusters(g).map((c) => (
                <div key={c.title ?? ""} className="mobile-nav-cluster">
                  {c.title && <span className="mobile-nav-section">{c.title}</span>}
                  {c.pages.map((p) => (
                    <Link key={p.href} href={leagueHref(withTeam(p.href, team))} aria-current={p.href === activeHref ? "page" : undefined} onClick={close}>
                      {p.label}
                    </Link>
                  ))}
                </div>
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
          <div className="mobile-nav-foot">
            <Link href={leagueHref("/data")} aria-current={pathname === "/data" ? "page" : undefined} onClick={close}>Data quality</Link>
          </div>
        </nav>
      )}
    </div>
  );
}
