// The connect-a-league journey on /leagues (issue #167): choose a host, enter the league, follow its first sync to the
// dashboard. Pure functions only (no Next, no fetch, no environment) so node --test covers them. The backend is
// authoritative: capabilities come from GET /v2/providers and the sync state from GET /v2/leagues/{id}; this module
// only turns them into words, steps and the next thing to do. Every step lives in the URL, so a reload resumes it.

import { providerName } from "./labels.ts";
import { issueSentence, isLeagueRef, leagueState, MAX_SEASON, MIN_SEASON, type MyLeague } from "./my-leagues.ts";

export type Host = "espn" | "sleeper";
export const HOSTS: readonly Host[] = ["espn", "sleeper"];

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const first = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

export function parseHost(value: unknown): Host | null {
  return value === "espn" || value === "sleeper" ? value : null;
}

// ---- where the visitor is in the journey (the URL) ----------------------------------------------------------------

export type OnboardingView =
  | { step: "choose" }
  | { step: "details"; host: Host }
  | { step: "private"; league: string; season: number }
  | { step: "progress"; leagueId: string; note: "already" | "joined" | null };

/**
 * `/leagues` -> choose a host; `?host=espn|sleeper` -> the league form; `?host=espn&private=<id>&season=<year>` -> the
 * cookie step for an ESPN league ESPN said is private; `?league=<lg_…>` -> the sync progress of that league. Anything
 * malformed falls back to the first step, so a hand-edited URL never shows a broken form.
 */
export function onboardingView(params: Record<string, string | string[] | undefined>): OnboardingView {
  const league = first(params.league);
  if (isLeagueRef(league)) {
    const note = first(params.already) === "1" ? "already" : first(params.joined) === "1" ? "joined" : null;
    return { step: "progress", leagueId: league, note };
  }
  const host = parseHost(first(params.host));
  if (!host) return { step: "choose" };
  const priv = first(params.private);
  const season = Number(first(params.season));
  if (host === "espn" && priv && /^\d{1,12}$/.test(priv) && Number.isInteger(season) && season >= MIN_SEASON && season <= MAX_SEASON) {
    return { step: "private", league: priv, season };
  }
  return { step: "details", host };
}

export function hostHref(host: Host): string {
  return `/leagues?host=${host}`;
}

export function privateHref(league: string, season: number): string {
  return `/leagues?host=espn&private=${encodeURIComponent(league)}&season=${season}`;
}

export function progressHref(leagueId: string, note: "already" | "joined" | null = null): string {
  return `/leagues?league=${leagueId}${note ? `&${note}=1` : ""}`;
}

// ---- what each host can do (GET /v2/providers) ---------------------------------------------------------------------

/** Canonical order, as the backend's Capability enum. */
export const CAPABILITIES = [
  "discover_leagues", "league_settings", "teams", "rosters", "scoring_rules", "matchups",
  "standings", "free_agents", "transactions", "projections", "private_leagues",
] as const;

const CAPABILITY_LABEL: Record<string, string> = {
  discover_leagues: "Finding your leagues for you",
  league_settings: "League settings",
  teams: "Teams",
  rosters: "Rosters",
  scoring_rules: "Scoring rules",
  matchups: "Matchups",
  standings: "Standings",
  free_agents: "Free agents",
  transactions: "Transactions",
  projections: "Provider projections",
  private_leagues: "Private leagues",
};

export function capabilityLabel(name: string): string {
  const known = CAPABILITY_LABEL[name];
  if (known) return known;
  const words = name.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Unknown capability";
}

export type Capability = { name: string; label: string; support: string; note: string | null };
export type ProviderInfo = { provider: string; enabled: boolean; reason: string | null; capabilities: Capability[] };

/** GET /v2/providers -> one entry per provider, capabilities in canonical order. Malformed entries are dropped. */
export function parseProviders(payload: unknown): ProviderInfo[] {
  const list = isRecord(payload) && Array.isArray(payload.providers) ? payload.providers : [];
  const order = (n: string) => {
    const i = (CAPABILITIES as readonly string[]).indexOf(n);
    return i < 0 ? CAPABILITIES.length : i;
  };
  return list.flatMap((raw): ProviderInfo[] => {
    if (!isRecord(raw) || typeof raw.provider !== "string" || typeof raw.enabled !== "boolean") return [];
    const caps = isRecord(raw.capabilities) ? raw.capabilities : {};
    const capabilities = Object.entries(caps)
      .filter((e): e is [string, Record<string, unknown>] => isRecord(e[1]) && typeof e[1].support === "string")
      .map(([name, s]) => ({ name, label: capabilityLabel(name), support: String(s.support),
        note: typeof s.note === "string" && s.note.trim() ? s.note : null }))
      .sort((a, b) => order(a.name) - order(b.name) || a.name.localeCompare(b.name));
    return [{ provider: raw.provider, enabled: raw.enabled, reason: typeof raw.reason === "string" ? raw.reason : null, capabilities }];
  });
}

export function providerInfo(list: ProviderInfo[], host: Host): ProviderInfo | null {
  return list.find((p) => p.provider === host) ?? null;
}

export function isAvailable(support: string): boolean {
  return support === "supported" || support === "derived";
}

/** What a support level means, in words, for this host. */
export function supportWords(support: string, host: string): string {
  const name = providerName(host);
  switch (support) {
    case "supported": return `Read from ${name}`;
    case "derived": return "Worked out by Fantasy Intel from other league data";
    case "unsupported": return `${name} does not offer this`;
    case "requires_auth": return host === "espn" ? "Needs a league member's ESPN cookies, given with consent" : "Needs a league member's sign-in";
    case "disabled": return "Switched off in this deployment";
    default: return "Unknown";
  }
}

/** Split a host's capabilities into what works and what is limited, for the host cards. */
export function capabilitySummary(info: ProviderInfo): { available: Capability[]; limited: Capability[] } {
  return {
    available: info.capabilities.filter((c) => isAvailable(c.support)),
    limited: info.capabilities.filter((c) => !isAvailable(c.support)),
  };
}

// ---- duplicates -----------------------------------------------------------------------------------------------------

/** ESPN ids are compared as the backend stores them (leading zeros dropped). */
export function normalizeExternalId(id: string): string {
  return id.replace(/^0+(?=\d)/, "");
}

/**
 * A league the visitor already follows with the same host, id and season (Sleeper ids are per season already), so the
 * form can go straight to it instead of registering it again (which would also count against the daily limit).
 */
export function findExisting(leagues: MyLeague[], host: Host, externalId: string, season: number | null): MyLeague | null {
  const id = normalizeExternalId(externalId);
  return leagues.find((l) => l.provider === host && normalizeExternalId(l.externalId) === id
    && (host === "sleeper" || season === null || l.season === season)) ?? null;
}

/**
 * Whether entering a league the visitor already follows can go straight to its progress step. Not for a public league
 * that went dormant or unavailable: importing it again is what wakes it and queues a catch-up sync
 * (fi.import_public_league), and its progress step offers no other way back.
 */
export function canSkipImport(l: Pick<MyLeague, "visibility" | "status">): boolean {
  return !(l.visibility === "public" && (l.status === "dormant" || l.status === "unavailable"));
}

// ---- connect errors: a clear message and the next action --------------------------------------------------------------

export type NextAction = "fix_input" | "retry_later" | "sign_in" | "invite" | "cookies" | "manage" | "none";
export type ConnectProblem = { title: string; message: string; next: NextAction; hint: string | null };

/**
 * A failed import or connect, from the HTTP status and the backend's own message (already plain and free of secrets),
 * as a title, that message, and what to do next. `status` is undefined when the backend could not be reached.
 */
export function connectProblem(status: number | undefined, message: string, host: Host): ConnectProblem {
  const name = providerName(host);
  const p = (title: string, next: NextAction, hint: string | null): ConnectProblem => ({ title, message, next, hint });
  if (status === undefined) return p("The service could not be reached", "retry_later", "Try again in a minute. Nothing was added.");
  if (status === 401) return p("Sign in again", "sign_in", "Your session ended. Sign in, then add the league again.");
  if (status === 404) {
    return host === "espn"
      ? p("League not found", "fix_input", "Check the league id and the season. ESPN looks the league up for that season, so a league that has not renewed for it is not found.")
      : p("League not found", "fix_input", "Check the id: it is the long number after /leagues/ in the sleeper.com address.");
  }
  if (status === 422) return p("Check what you entered", "fix_input", null);
  if (status === 429) return p("Daily limit reached", "retry_later", "You can add more leagues tomorrow. Leagues you already added keep syncing.");
  if (status === 403 && /invite/i.test(message)) {
    return p("Your ESPN account is not in this league", "invite", "Ask a member of the league to send you an invite link from the league's settings.");
  }
  if (status === 403) return p("Not allowed", "none", null);
  if (status === 400) {
    return p(`${name} did not accept these cookies`, "cookies",
      `Copy both values again from a browser that is signed in to ${name} with the account that is in this league.`);
  }
  if (status === 409 && /25 leagues/.test(message)) return p("Your list is full", "manage", "Unpin a league below, then add this one.");
  if (status === 409 && /public/i.test(message)) return p("This league is public", "fix_input", "Add it without cookies.");
  if (status === 409) return p("This league cannot be added right now", "none", null);
  if (status === 503 && /^private leagues are not available/i.test(message)) {
    return p(`Private ${name} leagues are not available here`, "none", "Nothing was added.");
  }
  if (status === 503) return p(`${name} could not be reached`, "retry_later", "Nothing was added. Try again in a few minutes.");
  return p("The league could not be added", "retry_later", null);
}

// ---- the first sync, as steps -----------------------------------------------------------------------------------------

export type StepState = "done" | "current" | "waiting" | "failed" | "todo";
export type ProgressStep = { label: string; state: StepState; detail: string | null };
export type Progress = {
  headline: string;
  tone: "good" | "info" | "warn" | "bad";
  steps: ProgressStep[];
  /** The league has a published, authorized snapshot: its pages answer (no "first sync in progress" 409). */
  ready: boolean;
  /** The page should check again by itself: something is queued or running. */
  polling: boolean;
  /** What the visitor can do now. */
  actions: ("open" | "refresh" | "cookies" | "invite" | "wait")[];
};

/** GET /v2/leagues/{id} -> the three steps from "added" to "dashboard ready", and what to do next. */
export function syncProgress(l: Pick<MyLeague, "provider" | "status" | "syncState" | "syncIssue" | "visibility" | "role">): Progress {
  const name = providerName(l.provider);
  const added: ProgressStep = { label: "Added to your leagues", state: "done", detail: null };
  const build = (state: StepState, detail: string | null = null): ProgressStep => ({ label: "Dashboard ready", state, detail });
  const read = (state: StepState, detail: string | null): ProgressStep => ({ label: `Read the league from ${name}`, state, detail });
  const ready = l.syncState === "ready";
  const privateEspn = l.provider === "espn" && l.visibility === "private";

  if (ready) {
    const state = leagueState(l);
    const paused = l.status === "stale" || l.status === "unavailable";
    return {
      headline: paused ? "Your dashboard is open, with the last data synced" : "Your dashboard is ready",
      tone: paused ? "warn" : "good",
      steps: [added, read("done", paused ? state.detail : null), build("done")],
      ready: true, polling: false,
      actions: paused && privateEspn && l.role ? ["open", "cookies"] : ["open"],
    };
  }
  // No snapshot yet: the league's pages answer "first sync in progress" (409), so nothing links to them.
  if (l.status === "stale" || l.status === "unavailable") {
    const state = leagueState(l);
    return {
      headline: "The first sync cannot finish", tone: "bad",
      steps: [added, read("failed", state.detail), build("todo")],
      ready: false, polling: false,
      actions: privateEspn && l.role ? ["cookies", "invite"] : privateEspn ? ["invite"] : [],
    };
  }
  switch (l.syncState) {
    case "first_sync_running":
      return { headline: `Reading your league from ${name}`, tone: "info",
        steps: [added, read("current", "Teams, rosters, scoring and matchups. This usually takes a few minutes."), build("todo")],
        ready: false, polling: true, actions: ["wait"] };
    case "first_sync_queued":
      if (l.syncIssue) {
        return { headline: "Retrying the first sync", tone: "warn",
          steps: [added, read("waiting", `${issueSentence(l.syncIssue, name)} It is retried automatically, a little later each time.`), build("todo")],
          ready: false, polling: true, actions: ["wait"] };
      }
      return { headline: "Your league is in the queue", tone: "info",
        steps: [added, read("waiting", "Waiting for a sync worker. It usually starts within a few minutes."), build("todo")],
        ready: false, polling: true, actions: ["wait"] };
    case "first_sync_failed": {
      const why = l.syncIssue ? `${issueSentence(l.syncIssue, name)} ` : "";
      const access = l.syncIssue === "access" && privateEspn;
      return { headline: "The first sync stopped", tone: "bad",
        steps: [added, read("failed", `${why}It stopped after repeated tries.`), build("todo")],
        ready: false, polling: false,
        actions: access && l.role ? ["cookies", "refresh"] : ["refresh"] };
    }
    default:
      return { headline: "No sync has started yet", tone: "info",
        steps: [added, read("todo", "Start one with Try again."), build("todo")],
        ready: false, polling: false, actions: ["refresh"] };
  }
}
