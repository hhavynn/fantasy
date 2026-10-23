// Player profile tabs (#187): which tab and week window a URL asks for, and the hrefs that switch them while keeping
// the rest of the profile's context. Pure functions (no React, no `@/` imports) so node --test covers them.
//
// Every href is a relative query string ("?id=201&tab=usage"). The browser resolves it against the current path, so a
// profile served under a league (/l/<lg>/players) stays in that league, and the classic site stays unscoped.

import { weekAxis } from "./player-usage.ts";

export const PLAYER_TABS = [
  { id: "overview", label: "Overview" },
  { id: "usage", label: "Usage" },
  { id: "game-log", label: "Game log" },
  { id: "matchups", label: "Matchups" },
  { id: "outlook", label: "Outlook" },
] as const;

export type PlayerTab = (typeof PLAYER_TABS)[number]["id"];

/** `/players?id=` with no tab lands here, and its links leave `tab` out so the old URL stays the canonical one. */
export const DEFAULT_TAB: PlayerTab = "overview";

/** The week windows a usage chart or game log can be narrowed to; null is the whole season. */
export const WEEK_WINDOWS = [null, 8, 5, 3] as const;
export type WeekWindow = (typeof WEEK_WINDOWS)[number];

/** The tabs a week window applies to. */
export const WINDOWED_TABS: readonly PlayerTab[] = ["usage", "game-log"];

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export function parsePlayerTab(raw: string | string[] | undefined): PlayerTab {
  const v = first(raw);
  return PLAYER_TABS.find((t) => t.id === v)?.id ?? DEFAULT_TAB;
}

export function parseWeekWindow(raw: string | string[] | undefined): WeekWindow {
  const v = first(raw);
  return WEEK_WINDOWS.find((w) => w !== null && String(w) === v) ?? null;
}

export function weekWindowLabel(w: WeekWindow): string {
  return w === null ? "Season" : `Last ${w} weeks`;
}

/** What a profile URL asks for, normalised: anything unrecognised falls back to the default. */
export type ProfileState = {
  id: string;
  tab: PlayerTab;
  weeks: WeekWindow;
  cols: "all" | "core";
  /** The search box's text, kept so the compact search in the header stays filled in. */
  q: string;
  /** The analyzed team, when the URL carries one (the profile is league-wide; it only passes it along). */
  team: string | null;
};

export function readProfileState(sp: SP): ProfileState | null {
  const id = first(sp.id);
  if (typeof id !== "string" || !/^-?\d+$/.test(id)) return null;
  const team = first(sp.team);
  return {
    id,
    tab: parsePlayerTab(sp.tab),
    weeks: parseWeekWindow(sp.weeks),
    cols: first(sp.cols) === "all" ? "all" : "core",
    q: (first(sp.q) ?? "").trim(),
    team: team !== undefined && /^\d{1,6}$/.test(team.trim()) ? team.trim() : null,
  };
}

/**
 * The relative href of the profile with `updates` applied. Defaults are left out (`tab=overview`, the season window,
 * the core columns), so `/players?id=201` stays the Overview's URL. The id comes first.
 */
export function profileHref(state: ProfileState, updates: Partial<Omit<ProfileState, "id">> = {}): string {
  const s = { ...state, ...updates };
  const qs = new URLSearchParams({ id: s.id });
  if (s.tab !== DEFAULT_TAB) qs.set("tab", s.tab);
  if (s.weeks !== null) qs.set("weeks", String(s.weeks));
  if (s.cols === "all") qs.set("cols", "all");
  if (s.q) qs.set("q", s.q);
  if (s.team) qs.set("team", s.team);
  return `?${qs}`;
}

/** One tab per entry, each linking to that tab with everything else in the URL kept. */
export function profileTabItems(state: ProfileState): { id: PlayerTab; label: string; href: string }[] {
  return PLAYER_TABS.map((t) => ({ id: t.id, label: t.label, href: profileHref(state, { tab: t.id }) }));
}

export type WeekRange = { from: number; to: number };

/**
 * The weeks a window covers: the last `n` calendar weeks of the season axis (week 1 to the latest record or completed
 * week). Byes and missed games stay inside the window as gaps, so "last 5 weeks" never quietly reaches back further.
 * Null for the whole season, or when there is no week to show.
 */
export function windowRange(weeks: number[], lastCompleted: number | null, n: WeekWindow): WeekRange | null {
  if (n === null) return null;
  const axis = weekAxis(weeks, lastCompleted);
  if (axis.length === 0) return null;
  const to = axis[axis.length - 1];
  return { from: Math.max(axis[0], to - n + 1), to };
}

export function inRange(week: number, range: WeekRange | null): boolean {
  return range === null || (week >= range.from && week <= range.to);
}

/** "wk 4–8", "wk 5", or null for an empty list. */
export function weekSpan(weeks: readonly number[]): string | null {
  if (weeks.length === 0) return null;
  const lo = Math.min(...weeks);
  const hi = Math.max(...weeks);
  return lo === hi ? `wk ${lo}` : `wk ${lo}–${hi}`;
}
