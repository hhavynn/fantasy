// Pure logic for the command palette: the static page/action entries, result ranking and merging,
// recent selections, and keyboard index wrap. No React and no `@/` imports, so it runs under the
// unit-test runner.
import type { NavGroup } from "./navigation.ts";

export type PaletteKind = "action" | "page" | "player";

export type PaletteItem = {
  id: string;
  kind: PaletteKind;
  label: string;
  /** Muted second line or suffix: the nav group for pages, "QB · KC" for players. */
  hint?: string;
  href: string;
  /** Extra words that should find this item (page names an action stands in for). */
  keywords?: string[];
};

export type PaletteSection = { key: "recent" | "actions" | "pages" | "players"; title: string; items: PaletteItem[] };

/** Quick actions: verbs for the things people open the app to do. Each stands in for its page. */
export const QUICK_ACTIONS: PaletteItem[] = [
  { id: "action:matchup", kind: "action", label: "This week's matchup", hint: "Head to head", href: "/matchup", keywords: ["matchup", "opponent", "lineups"] },
  { id: "action:start-sit", kind: "action", label: "Decide who to start", hint: "Start/Sit", href: "/start-sit", keywords: ["start", "sit", "lineup", "bench"] },
  { id: "action:trade", kind: "action", label: "Evaluate a trade", hint: "Trade analyzer", href: "/trades", keywords: ["trade", "analyzer", "offer"] },
  { id: "action:discover", kind: "action", label: "Find trade targets", hint: "Trade discovery", href: "/trades/discover", keywords: ["trade", "discover", "targets", "partners"] },
  { id: "action:waivers", kind: "action", label: "Find waiver pickups", hint: "Waivers", href: "/waivers", keywords: ["waiver", "waivers", "free agents", "claims", "pickups"] },
  { id: "action:players", kind: "action", label: "Look up a player", hint: "Players", href: "/players", keywords: ["players", "search", "profile"] },
  { id: "action:data", kind: "action", label: "Check data freshness", hint: "Data quality", href: "/data", keywords: ["data", "quality", "sync", "stale", "health"] },
];

/** Every nav page, minus the ones a quick action stands in for. (Data quality is an action: it is reached from the sync pill, not the nav.) */
export function pageItems(groups: readonly NavGroup[]): PaletteItem[] {
  const covered = new Set(QUICK_ACTIONS.map((a) => a.href));
  const pages: PaletteItem[] = [];
  for (const g of groups) {
    for (const p of g.pages) {
      if (covered.has(p.href)) continue;
      pages.push({ id: `page:${p.href}`, kind: "page", label: p.label, hint: g.label, href: p.href });
    }
  }
  return pages;
}

/** Lowercase, accent-free, whitespace-collapsed, for matching. */
export function normalize(s: string): string {
  return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
}

function isSubsequence(needle: string, hay: string): boolean {
  let i = 0;
  for (let j = 0; j < hay.length && i < needle.length; j++) if (hay[j] === needle[i]) i++;
  return i === needle.length;
}

function wordStart(hay: string, needle: string): boolean {
  return hay.split(/[\s/\-_.]+/).some((w) => w.startsWith(needle));
}

/** The score of a subsequence-only match: the weakest thing that still counts as a match. */
export const FUZZY_SCORE = 15;

/** How well `query` matches `item`; 0 means no match. Exact > prefix > word start > substring > keyword > fuzzy. */
export function scoreItem(query: string, item: PaletteItem): number {
  const q = normalize(query);
  if (!q) return 1;
  const label = normalize(item.label);
  if (label === q) return 100;
  if (label.startsWith(q)) return 85;
  if (wordStart(label, q)) return 70;
  if (label.includes(q)) return 50;
  const words = [...(item.keywords ?? []), item.hint ?? ""].map(normalize);
  if (words.some((w) => w.startsWith(q) || wordStart(w, q))) return 40;
  if (words.some((w) => w.includes(q))) return 30;
  if (q.length >= 2 && isSubsequence(q.replace(/ /g, ""), label.replace(/ /g, ""))) return FUZZY_SCORE;
  return 0;
}

/**
 * Items that match `query`, best first; ties keep their original order. Subsequence-only matches
 * ("chase" inside "Check data freshness") are returned only when nothing matches better.
 */
export function rankItems(query: string, items: readonly PaletteItem[]): PaletteItem[] {
  const scored = items
    .map((item, i) => ({ item, i, score: scoreItem(query, item) }))
    .filter((r) => r.score > 0);
  const real = scored.filter((r) => r.score > FUZZY_SCORE);
  return (real.length > 0 ? real : scored)
    .sort((a, b) => b.score - a.score || a.i - b.i)
    .map((r) => r.item);
}

const LOCAL_LIMIT = 6;
const PLAYER_LIMIT = 8;

/**
 * The sections to show. Without a query: recent selections, quick actions, pages. With one:
 * matching actions and pages merged (best match first) and the player results the backend
 * returned. Players lead only when no action or page matches the text at least as a word start,
 * so "wai" finds Waivers first and "lloyd" finds the player first. A subsequence-only page match
 * is a last resort: it is hidden while players exist or are still being searched, so a name such
 * as "chase" never lands on "Check data freshness" ahead of Ja'Marr Chase.
 */
export function buildSections(
  query: string, pages: readonly PaletteItem[], players: readonly PaletteItem[], recents: readonly PaletteItem[],
  playersPending = false,
): PaletteSection[] {
  const q = query.trim();
  if (!q) {
    const recentHrefs = new Set(recents.map((r) => r.href));
    const rest = (items: readonly PaletteItem[]) => items.filter((i) => !recentHrefs.has(i.href));
    const sections: PaletteSection[] = [
      { key: "recent", title: "Recent", items: recents.slice() },
      { key: "actions", title: "Quick actions", items: rest(QUICK_ACTIONS) },
      { key: "pages", title: "Pages", items: rest(pages) },
    ];
    return sections.filter((s) => s.items.length > 0);
  }
  let matched = rankItems(q, [...QUICK_ACTIONS, ...pages]);
  const weakOnly = matched.length > 0 && scoreItem(q, matched[0]) <= FUZZY_SCORE;
  if (weakOnly && (players.length > 0 || playersPending)) matched = [];
  const strongLocal = matched.length > 0 && scoreItem(q, matched[0]) >= 70;
  const local: PaletteSection = { key: "pages", title: "Go to", items: matched.slice(0, LOCAL_LIMIT) };
  const found: PaletteSection = { key: "players", title: "Players", items: players.slice(0, PLAYER_LIMIT) };
  return (strongLocal ? [local, found] : [found, local]).filter((s) => s.items.length > 0);
}

/** Flat list of every selectable item, in on-screen order. */
export function flattenSections(sections: readonly PaletteSection[]): PaletteItem[] {
  return sections.flatMap((s) => s.items);
}

/** Next index after moving `delta` steps through `length` options, wrapping at both ends. -1 means nothing is active yet. */
export function wrapIndex(current: number, delta: number, length: number): number {
  if (length <= 0) return -1;
  if (current < 0) return delta >= 0 ? 0 : length - 1;
  return (((current + delta) % length) + length) % length;
}

export const RECENT_LIMIT = 5;

/** Add `item` to the front of `recents` (de-duplicated by destination, capped). */
export function pushRecent(recents: readonly PaletteItem[], item: PaletteItem): PaletteItem[] {
  const slim: PaletteItem = { id: item.id, kind: item.kind, label: item.label, hint: item.hint, href: item.href };
  return [slim, ...recents.filter((r) => r.href !== item.href)].slice(0, RECENT_LIMIT);
}

/** Parse the stored recents defensively: anything malformed or off-site is dropped. */
export function parseRecents(raw: string | null): PaletteItem[] {
  if (!raw) return [];
  try {
    const data: unknown = JSON.parse(raw);
    if (!Array.isArray(data)) return [];
    const out: PaletteItem[] = [];
    for (const r of data) {
      if (r && typeof r === "object" && typeof r.id === "string" && typeof r.label === "string"
          && typeof r.href === "string" && r.href.startsWith("/") && !r.href.startsWith("//")
          && (r.kind === "action" || r.kind === "page" || r.kind === "player")) {
        out.push({ id: r.id, kind: r.kind, label: r.label, hint: typeof r.hint === "string" ? r.hint : undefined, href: r.href });
      }
    }
    return out.slice(0, RECENT_LIMIT);
  } catch {
    return [];
  }
}

/** The slim player shape `/api/search` returns. */
export type SearchPlayer = { id: number; name: string; position: string | null; team: string | null; owner: string | null };

export function playerItem(p: SearchPlayer): PaletteItem {
  const meta = [p.position, p.team ?? "FA"].filter(Boolean).join(" · ");
  return { id: `player:${p.id}`, kind: "player", label: p.name, hint: p.owner ? `${meta} · ${p.owner}` : meta, href: `/players?id=${p.id}` };
}
