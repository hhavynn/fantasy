// Pure logic for the command palette: the static page/action entries, result ranking and merging,
// recent selections, and keyboard index wrap. No React and no `@/` imports, so it runs under the
// unit-test runner.
import type { ApiResult } from "./api.ts";
import type { Provider } from "./labels.ts";
import { isLeagueId } from "./league-scope.ts";
import { ALL_NAV_PAGES, type NavGroup } from "./navigation.ts";
import { injuryText, rosterStatus } from "./roster-status.ts";

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
  /**
   * A player's hint as Recent may keep it: only what is the same in every league (his position). The live hint names
   * this league's holder, roster status and injury, which must not follow the visitor into another league.
   */
  recentHint?: string;
};

export type PaletteSection = { key: "recent" | "actions" | "pages" | "players"; title: string; items: PaletteItem[] };

/** Quick actions: verbs for the things people open the app to do. Each stands in for its page. */
export const QUICK_ACTIONS: PaletteItem[] = [
  { id: "action:matchup", kind: "action", label: "This week's matchup", hint: "Head to head", href: "/matchup", keywords: ["matchup", "opponent", "lineups"] },
  { id: "action:start-sit", kind: "action", label: "Decide who to start", hint: "Start/Sit", href: "/start-sit", keywords: ["start", "sit", "lineup", "bench"] },
  { id: "action:trade", kind: "action", label: "Evaluate a trade", hint: "Trade analyzer", href: "/trades", keywords: ["trade", "analyzer", "offer"] },
  { id: "action:discover", kind: "action", label: "Find trade targets", hint: "Trade discovery", href: "/trades/discover", keywords: ["trade", "discover", "targets", "partners"] },
  { id: "action:waivers", kind: "action", label: "Find waiver pickups", hint: "Waivers", href: "/waivers", keywords: ["waiver", "waivers", "free agents", "claims", "pickups"] },
  { id: "action:research", kind: "action", label: "Research player opportunity", hint: "Research lab", href: "/research", keywords: ["research", "xfp", "fpoe", "opportunity", "breakout", "targets", "usage", "wr", "rb", "te"] },
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
      pages.push({
        id: `page:${p.href}`, kind: "page", label: p.label, hint: p.section ? `${g.label} · ${p.section}` : g.label, href: p.href,
        keywords: p.summary ? [p.summary] : undefined,
      });
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
/** True when `href` is one of `hidden` or under it (`/research` hides `/research/wr?view=…`). */
export function isHidden(href: string, hidden: ReadonlySet<string>): boolean {
  const path = href.split(/[?#]/)[0];
  for (const h of hidden) if (path === h || path.startsWith(`${h}/`)) return true;
  return false;
}

export function buildSections(
  query: string, pages: readonly PaletteItem[], players: readonly PaletteItem[], recents: readonly PaletteItem[],
  playersPending = false, hidden: ReadonlySet<string> = new Set(),
): PaletteSection[] {
  // Routes this visitor may not open (the owner research on a league page) leave every source: actions, recents, pages.
  if (hidden.size) {
    const ok = (i: PaletteItem) => !isHidden(i.href, hidden);
    pages = pages.filter(ok);
    recents = recents.filter(ok);
  }
  const actions = hidden.size ? QUICK_ACTIONS.filter((a) => !isHidden(a.href, hidden)) : QUICK_ACTIONS;
  const q = query.trim();
  if (!q) {
    const recentHrefs = new Set(recents.map((r) => r.href));
    const rest = (items: readonly PaletteItem[]) => items.filter((i) => !recentHrefs.has(i.href));
    const sections: PaletteSection[] = [
      { key: "recent", title: "Recent", items: recents.slice() },
      { key: "actions", title: "Quick actions", items: rest(actions) },
      { key: "pages", title: "Pages", items: rest(pages) },
    ];
    return sections.filter((s) => s.items.length > 0);
  }
  let matched = rankItems(q, [...actions, ...pages]);
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

/** A player hint Recent may hold: a bare position code ("RB", "D/ST"), never a holder, status or injury. */
const PLAYER_RECENT_HINT = /^[A-Z][A-Z/]{0,4}$/;
const playerRecentHint = (hint: unknown) => (typeof hint === "string" && PLAYER_RECENT_HINT.test(hint) ? hint : undefined);

/**
 * Add `item` to the front of `recents` (de-duplicated by destination, capped). A player keeps only his league-independent
 * hint (`recentHint`, his position): who holds him, his roster status and injury belong to the league searched.
 */
export function pushRecent(recents: readonly PaletteItem[], item: PaletteItem): PaletteItem[] {
  const hint = item.kind === "player" ? playerRecentHint(item.recentHint) : item.hint;
  const slim: PaletteItem = { id: item.id, kind: item.kind, label: item.label, hint, href: item.href };
  return [slim, ...recents.filter((r) => r.href !== item.href)].slice(0, RECENT_LIMIT);
}

/** The only player destination a recent may hold: the profile, by a bare provider id (`/players?id=-16002`). */
const PLAYER_HREF = /^\/players\?id=(-?[1-9]\d{0,11})$/;

/**
 * True when a stored recent's destination is one the palette itself offers for that kind, so it can only open a page of
 * the current league: an action's or nav page's exact unscoped href, or a player profile by id (with the matching item
 * id). Stored recents are untrusted: anything else (a `/l/<lg>/...` path in another league, `//host`, a backslash, an
 * absolute URL, `?league=clear`, an unknown path) is refused, since `withLeague` keeps an already-scoped or unscoped
 * destination as it is.
 */
export function isRecentDestination(kind: unknown, id: unknown, href: unknown): boolean {
  if (typeof href !== "string" || typeof id !== "string") return false;
  if (kind === "action") return QUICK_ACTIONS.some((a) => a.href === href && a.id === id);
  if (kind === "page") return ALL_NAV_PAGES.some((p) => p.href === href) && id === `page:${href}`;
  if (kind === "player") {
    const m = PLAYER_HREF.exec(href);
    return !!m && id === `player:${m[1]}`;
  }
  return false;
}

function recentItems(data: unknown): PaletteItem[] {
  if (!Array.isArray(data)) return [];
  const out: PaletteItem[] = [];
  for (const r of data) {
    if (r && typeof r === "object" && typeof r.label === "string" && isRecentDestination(r.kind, r.id, r.href)) {
      const hint = r.kind === "player" ? playerRecentHint(r.hint) : typeof r.hint === "string" ? r.hint : undefined;
      out.push({ id: r.id, kind: r.kind, label: r.label, hint, href: r.href });
    }
  }
  return out.slice(0, RECENT_LIMIT);
}

/** Parse a stored recents list defensively: anything malformed or off-site is dropped, a player keeps only his position. */
export function parseRecents(raw: string | null): PaletteItem[] {
  if (!raw) return [];
  try {
    return recentItems(JSON.parse(raw));
  } catch {
    return [];
  }
}

/**
 * The browser-wide recents key used before issue #200. Its player hints could name one league's holders, so it is never
 * read again (nothing from it is migrated) and is removed when a league's recents are written.
 */
export const LEGACY_RECENT_KEY = "fantasy-intel:palette-recent";

/** Recents per league, like the other device-local stores; none outside a league (no shared fallback). */
export function recentsKey(leagueId: string | null | undefined): string | null {
  return isLeagueId(leagueId) ? `fi:palette-recent:v1:${leagueId}` : null;
}

/** A league's recents. The payload names its league, so a value copied under another league's key is refused. */
export function parseLeagueRecents(raw: string | null, leagueId: string): PaletteItem[] {
  try {
    const v = JSON.parse(raw ?? "null") as { v?: unknown; league?: unknown; items?: unknown } | null;
    if (!v || typeof v !== "object" || v.v !== 1 || v.league !== leagueId) return [];
    return recentItems(v.items);
  } catch {
    return [];
  }
}

export function readLeagueRecents(storage: Pick<Storage, "getItem">, leagueId: string | null): PaletteItem[] {
  const key = recentsKey(leagueId);
  if (!key) return [];
  try {
    return parseLeagueRecents(storage.getItem(key), leagueId!);
  } catch {
    return [];
  }
}

/** Store a league's recents (each re-slimmed by pushRecent's rules) and drop the legacy browser-wide list. */
export function writeLeagueRecents(storage: Pick<Storage, "setItem" | "removeItem">, leagueId: string | null, items: readonly PaletteItem[]): boolean {
  const key = recentsKey(leagueId);
  if (!key) return false;
  try {
    storage.removeItem(LEGACY_RECENT_KEY);
    storage.setItem(key, JSON.stringify({ v: 1, league: leagueId, items: recentItems(items) }));
    return true;
  } catch {
    return false;
  }
}

/** The provider's roster states the search forwards; anything else is sent as null (unknown). */
export type SearchAvailability = "ONTEAM" | "WAIVERS" | "FREEAGENT";
const AVAILABILITY: readonly string[] = ["ONTEAM", "WAIVERS", "FREEAGENT"];

/**
 * The slim player shape `/api/search` returns. `availability` is the provider's roster state in this league (null:
 * not supplied) and `injury` its designation (null: none or not supplied); both optional for older callers.
 */
export type SearchPlayer = {
  id: number; name: string; position: string | null; team: string | null; owner: string | null;
  availability?: SearchAvailability | null; injury?: string | null;
};

const SEARCH_MAX_QUERY = 80;
const SEARCH_LIMIT = 8;

/**
 * The browser's player-search URL (the app/api/search route handler). On a league page the caller wraps it in
 * `leagueRouteHref` (lib/league-scope.ts) so the proxy hands the handler that league; a bare `/api/...` fetch is
 * unscoped and would answer for the classic league.
 */
export function searchSrc(q: string): string {
  return `/api/search?q=${encodeURIComponent(q)}`;
}

/**
 * The backend path the search handler calls for the browser's query, or null for an empty one. Only the trimmed,
 * capped query is forwarded. `api()` moves it to `/v2/leagues/<lg>/...` (with the visitor's token) when the proxy set
 * the league header, so the handler never chooses a league itself.
 */
export function searchPath(params: URLSearchParams): string | null {
  // Capped by code points, not UTF-16 units: a cut through an emoji would leave a lone surrogate, which
  // encodeURIComponent rejects (a 500 instead of results).
  const q = Array.from((params.get("q") ?? "").trim()).slice(0, SEARCH_MAX_QUERY).join("");
  return q ? `/api/players?q=${encodeURIComponent(q)}&limit=${SEARCH_LIMIT}` : null;
}

/** The backend's player row as `GET /api/players` returns it (only the fields the pickers show). */
export type SearchBackendPlayer = {
  espn_player_id: number;
  full_name: string | null;
  position: string | null;
  pro_team: string | null;
  fantasy_team: string | null;
  availability_status?: string | null;
  injury_status?: string | null;
};

/**
 * The search handler's response for a backend result (`null`: an empty query, no backend call). Never cacheable by a
 * shared cache: the rows name a private league's managers, and the same URL answers per league and per visitor.
 */
export function searchResponse(res: ApiResult<SearchBackendPlayer[]> | null): Response {
  const headers = { "Cache-Control": "private, no-store" };
  if (res === null) return Response.json({ players: [] satisfies SearchPlayer[] }, { headers });
  if (!res.ok) return Response.json({ players: [], error: res.kind ?? "unknown" }, { status: 502, headers });
  const players: SearchPlayer[] = res.data
    .filter((p) => p.full_name)
    .map((p) => ({
      id: p.espn_player_id, name: p.full_name as string, position: p.position, team: p.pro_team, owner: p.fantasy_team,
      availability: AVAILABILITY.includes(p.availability_status ?? "") ? p.availability_status as SearchAvailability : null,
      // Only a short designation code is forwarded (OUT, INJURY_RESERVE); free text from a provider is not.
      injury: typeof p.injury_status === "string" && /^[A-Z_]{1,24}$/.test(p.injury_status) ? p.injury_status : null,
    }));
  return Response.json({ players }, { headers });
}

/**
 * A player result: name, then position, NFL team ("No NFL team" when he has none; "FA" would read as a fantasy free
 * agent), any injury designation and who holds him in this league, in the provider's own terms (lib/roster-status.ts:
 * a Sleeper player off every roster is "Not rostered", never "Free agent"). `provider` null (not confirmed for the
 * league searched) uses only wording true for every provider. A payload without a roster state (an older caller) falls
 * back to the team name alone.
 */
export function playerItem(p: SearchPlayer, provider: Provider | null = "espn"): PaletteItem {
  const roster = p.availability === undefined ? (p.owner ?? null) : rosterStatus(p.availability, p.owner, provider).text;
  const hint = [p.position, p.team ?? "No NFL team", injuryText(p.injury), roster].filter(Boolean).join(" · ");
  return { id: `player:${p.id}`, kind: "player", label: p.name, hint, href: `/players?id=${p.id}`, recentHint: playerRecentHint(p.position) };
}

/**
 * Player results as palette items. Two players with the same name and the same hint (same position, NFL team, status
 * and holder) would be indistinguishable, so each of those also shows its player ID.
 */
export function playerItems(players: readonly SearchPlayer[], provider: Provider | null = "espn"): PaletteItem[] {
  const items = players.map((p) => playerItem(p, provider));
  const sig = (i: PaletteItem) => `${normalize(i.label)}|${i.hint ?? ""}`;
  const counts = new Map<string, number>();
  for (const i of items) counts.set(sig(i), (counts.get(sig(i)) ?? 0) + 1);
  return items.map((i, n) => ((counts.get(sig(i)) ?? 0) > 1 ? { ...i, hint: `${i.hint ? `${i.hint} · ` : ""}ID ${players[n].id}` } : i));
}
