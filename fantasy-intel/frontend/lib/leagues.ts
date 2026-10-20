// View-model logic for the /leagues page: what the backend's /v2 payloads mean for the UI.
// Pure functions only (no Next, no fetch, no environment) so node --test covers them. The backend is
// authoritative; nothing here invents a connection, a capability or a team.

export const PROVIDERS = ["espn", "sleeper", "yahoo"] as const;
export type ProviderId = (typeof PROVIDERS)[number];

export const PROVIDER_LABEL: Record<ProviderId, string> = { espn: "ESPN", sleeper: "Sleeper", yahoo: "Yahoo" };

export function isProviderId(value: unknown): value is ProviderId {
  return typeof value === "string" && (PROVIDERS as readonly string[]).includes(value);
}

export function providerLabel(provider: string): string {
  return isProviderId(provider) ? PROVIDER_LABEL[provider] : provider;
}

export const NOT_ENABLED_REASON = "Multi-league support is not enabled in this deployment.";
export const SIGNED_OUT_REASON = "Sign-in is not available yet (it arrives with the league switcher), so leagues cannot be listed or connected here.";
export const PROVIDERS_UNAVAILABLE_REASON = "Provider information is unavailable, so connecting is disabled.";

/** The feature flag is exactly "1": anything else (unset, "0", "true") is off. */
export function multileagueUiEnabled(value: string | undefined): boolean {
  return value === "1";
}

// ---- Capabilities -------------------------------------------------------------------------------

/** Canonical order, matching the backend's Capability enum. */
export const CAPABILITIES = [
  "discover_leagues", "league_settings", "teams", "rosters", "scoring_rules", "matchups",
  "standings", "free_agents", "transactions", "projections", "private_leagues",
] as const;

const CAPABILITY_LABEL: Record<string, string> = {
  discover_leagues: "Finding your leagues",
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

/** Human label for a capability name; unknown names are shown readably rather than hidden. */
export function capabilityLabel(name: string): string {
  const known = CAPABILITY_LABEL[name];
  if (known) return known;
  const words = name.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Unknown capability";
}

const SUPPORT_LABEL: Record<string, string> = {
  supported: "Supported",
  derived: "Computed from other data",
  unsupported: "Not offered by the provider",
  requires_auth: "Needs credentials this league does not have",
  disabled: "Not enabled in this deployment",
};

export function supportLabel(support: string): string {
  return SUPPORT_LABEL[support] ?? `Unknown support level (${support})`;
}

export function isAvailableSupport(support: string): boolean {
  return support === "supported" || support === "derived";
}

export type CapabilityState = { support: string; note: string | null };

export type ProviderInfo = {
  provider: string;
  enabled: boolean;
  reason: string | null;
  capabilities: Record<string, CapabilityState>;
};

export type MissingCapability = { name: string; label: string; support: string; supportLabel: string; note: string | null };

function orderIndex(name: string): number {
  const i = (CAPABILITIES as readonly string[]).indexOf(name);
  return i < 0 ? CAPABILITIES.length : i;
}

/** Capabilities that are not available, in canonical order, each with the provider's own note. */
export function describeMissing(names: readonly string[], capabilities: Record<string, CapabilityState>): MissingCapability[] {
  return [...new Set(names)]
    .sort((a, b) => orderIndex(a) - orderIndex(b) || a.localeCompare(b))
    .map((name) => {
      const state = capabilities[name];
      const support = state?.support ?? "unsupported";
      // The backend listed it as missing even though its state reads available: say so rather than "Supported".
      const text = isAvailableSupport(support) ? "Reported as unavailable by the backend" : supportLabel(support);
      return { name, label: capabilityLabel(name), support, supportLabel: text, note: state?.note ?? null };
    });
}

// ---- Backend payloads (parsed defensively; malformed entries are dropped, not guessed) -----------

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

function parseCapabilities(raw: unknown): Record<string, CapabilityState> {
  const out: Record<string, CapabilityState> = {};
  if (!isRecord(raw)) return out;
  for (const [name, state] of Object.entries(raw)) {
    if (isRecord(state) && typeof state.support === "string") out[name] = { support: state.support, note: str(state.note) };
  }
  return out;
}

function parseProvider(raw: unknown): ProviderInfo | null {
  if (!isRecord(raw) || typeof raw.provider !== "string" || typeof raw.enabled !== "boolean") return null;
  return { provider: raw.provider, enabled: raw.enabled, reason: str(raw.reason), capabilities: parseCapabilities(raw.capabilities) };
}

/** GET /v2/providers -> {"providers": [...]}. */
export function parseProviders(payload: unknown): ProviderInfo[] {
  const list = isRecord(payload) && Array.isArray(payload.providers) ? payload.providers : [];
  return list.map(parseProvider).filter((p): p is ProviderInfo => p !== null);
}

export type SyncView = { status: string; last_synced_at: string | null; source_updated_at: string | null; last_error: string | null };

export type LeagueView = {
  league_id: string;
  provider: string;
  external_league_id: string;
  season: number;
  name: string | null;
  role: string | null;
  my_team_external_id: string | null;
  sync: SyncView;
  data_origin: string;
  missing_capabilities: string[];
  capabilities: Record<string, CapabilityState>;
};

/** Accept one LeagueView; null when the fields the page depends on are missing. */
export function parseLeague(raw: unknown): LeagueView | null {
  if (!isRecord(raw)) return null;
  const { league_id, provider, external_league_id, season } = raw;
  if (typeof league_id !== "string" || typeof provider !== "string" || typeof external_league_id !== "string" || typeof season !== "number") return null;
  const sync = isRecord(raw.sync) ? raw.sync : {};
  const caps = isRecord(raw.capabilities) ? (isRecord(raw.capabilities.capabilities) ? raw.capabilities.capabilities : raw.capabilities) : {};
  return {
    league_id, provider, external_league_id, season,
    name: str(raw.name),
    role: str(raw.role),
    my_team_external_id: str(raw.my_team_external_id),
    sync: {
      status: str(sync.status) ?? "never",
      last_synced_at: str(sync.last_synced_at),
      source_updated_at: str(sync.source_updated_at),
      last_error: str(sync.last_error),
    },
    data_origin: str(raw.data_origin) ?? "unknown",
    missing_capabilities: Array.isArray(raw.missing_capabilities) ? raw.missing_capabilities.filter((c): c is string => typeof c === "string") : [],
    capabilities: parseCapabilities(caps),
  };
}

/** GET /v2/leagues -> {"leagues": [...]}. */
export function parseLeagues(payload: unknown): LeagueView[] {
  const list = isRecord(payload) && Array.isArray(payload.leagues) ? payload.leagues : [];
  return list.map(parseLeague).filter((l): l is LeagueView => l !== null);
}

export type TeamOption = { id: string; label: string; detail: string | null };

/** GET /v2/leagues/{id}/teams -> {"teams": [...]}. The option value is the provider's team id, as a string. */
export function parseTeams(payload: unknown): TeamOption[] {
  const list = isRecord(payload) && Array.isArray(payload.teams) ? payload.teams : [];
  const out: TeamOption[] = [];
  for (const t of list) {
    if (!isRecord(t)) continue;
    const rawId = t.external_team_id;
    const id = typeof rawId === "string" ? rawId : typeof rawId === "number" ? String(rawId) : null;
    if (!id) continue;
    const managers = Array.isArray(t.manager_display_names) ? t.manager_display_names.filter((m): m is string => typeof m === "string" && m !== "") : [];
    const record = typeof t.wins === "number" && typeof t.losses === "number"
      ? `${t.wins}-${t.losses}${typeof t.ties === "number" && t.ties > 0 ? `-${t.ties}` : ""}`
      : null;
    const detail = [managers.join(", ") || null, record].filter(Boolean).join(" · ") || null;
    out.push({ id, label: str(t.name)?.trim() || `Team ${id}`, detail });
  }
  return out;
}

export function teamOptionText(team: TeamOption): string {
  return team.detail ? `${team.label} (${team.detail})` : team.label;
}

/** The team the user picked as theirs, but only if the provider's team list still contains it. */
export function myTeamOption(league: Pick<LeagueView, "my_team_external_id">, teams: TeamOption[]): TeamOption | null {
  return teams.find((t) => t.id === league.my_team_external_id) ?? null;
}

// ---- Freshness and origin -----------------------------------------------------------------------

export type Tone = "good" | "warn" | "bad" | "info" | "neutral";

// Time helpers mirror lib/sync-status.ts (backend timestamps without an offset are UTC). They are repeated
// here rather than imported because node's test runner needs ".ts" import paths, which tsc rejects.
const STALE_HOURS = 36;

function parseTimestamp(v: string | null | undefined): number | null {
  if (!v) return null;
  const t = new Date(/([zZ]|[+-]\d{2}:?\d{2})$/.test(v) ? v : `${v}Z`).getTime();
  return Number.isNaN(t) ? null : t;
}

function hoursAgo(hours: number): string {
  return hours < 48 ? `${Math.floor(hours)}h ago` : `${Math.floor(hours / 24)}d ago`;
}

/** "just now", "12 min ago", "5h ago", "3d ago"; null when the timestamp cannot be read. Future times read "just now". */
export function relativeTime(timestamp: string | null | undefined, now: number): string | null {
  const t = parseTimestamp(timestamp);
  if (t === null) return null;
  const minutes = Math.floor((now - t) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  return hoursAgo((now - t) / 3_600_000);
}

export type Freshness = {
  status: string;
  /** Short status word shown in the badge: text, never colour alone. */
  statusLabel: string;
  tone: Tone;
  /** One line: when the data was last synced. */
  summary: string;
  /** Extra lines (provider-side update time, sanitized last error). */
  details: string[];
};

export function freshness(sync: SyncView, now: number): Freshness {
  const synced = relativeTime(sync.last_synced_at, now);
  const updated = relativeTime(sync.source_updated_at, now);
  const details: string[] = [];
  if (updated) details.push(`Provider data last changed ${updated}.`);
  if (sync.status === "failed" && sync.last_error) details.push(`Last error: ${sync.last_error}`);

  switch (sync.status) {
    case "ok": {
      const hours = parseTimestamp(sync.last_synced_at);
      const old = hours !== null && (now - hours) / 3_600_000 > STALE_HOURS;
      return {
        status: "ok", statusLabel: old ? "Synced, may be old" : "Up to date", tone: old ? "warn" : "good",
        summary: synced ? `Synced ${synced}.` : "Synced; the time was not recorded.", details,
      };
    }
    case "stale":
      return { status: "stale", statusLabel: "Stale", tone: "warn", summary: synced ? `Last synced ${synced}; the data may be out of date.` : "The data may be out of date.", details };
    case "failed":
      return { status: "failed", statusLabel: "Sync failed", tone: "bad", summary: synced ? `Last successful sync ${synced}.` : "No successful sync yet.", details };
    case "never":
      return { status: "never", statusLabel: "Not synced yet", tone: "neutral", summary: "No data has been synced for this league yet.", details };
    default:
      return { status: sync.status, statusLabel: "Unknown status", tone: "neutral", summary: synced ? `Last synced ${synced}.` : "Sync state is unknown.", details };
  }
}

export type Origin = { label: string; tone: Tone; title: string };

/** Fixture data must never pass for a live league. */
export function dataOrigin(origin: string): Origin {
  if (origin === "fixture") return { label: "Fixture data", tone: "warn", title: "Recorded sample data, not this league's live data" };
  if (origin === "live") return { label: "Live data", tone: "info", title: "Read from the provider" };
  return { label: "Unknown data origin", tone: "neutral", title: `The backend reported data_origin "${origin}"` };
}

// ---- Provider cards -----------------------------------------------------------------------------

export type CardContext = {
  /** MULTILEAGUE_UI_ENABLED === "1" */
  flagEnabled: boolean;
  /** /v2/providers answered. False when it was skipped or failed. */
  providersLoaded: boolean;
  /** There is a signed-in user and the server can sign for them. */
  canConnect: boolean;
  /** Why canConnect is false (shown verbatim). */
  signedOutReason?: string;
};

export type ProviderCard = {
  provider: ProviderId;
  label: string;
  state: "enabled" | "disabled";
  /** Why disabled, as text. The provider's own `reason` is passed through verbatim. */
  reason: string | null;
  reasonSource: "flag" | "unavailable" | "provider" | "signed-out" | null;
  /** What the control looks like when enabled. Yahoo needs OAuth, which this page does not offer. */
  control: "league-id" | "none";
  unavailable: MissingCapability[];
};

/** Which card state applies, in priority order: flag off, no provider data, provider disabled, no user. */
export function providerCard(provider: ProviderId, info: ProviderInfo | undefined, ctx: CardContext): ProviderCard {
  const base = { provider, label: PROVIDER_LABEL[provider], control: provider === "yahoo" ? ("none" as const) : ("league-id" as const) };
  const unavailable = info ? describeMissing(CAPABILITIES.filter((c) => !isAvailableSupport(info.capabilities[c]?.support ?? "unsupported")), info.capabilities) : [];
  const disabled = (reason: string, reasonSource: NonNullable<ProviderCard["reasonSource"]>): ProviderCard =>
    ({ ...base, state: "disabled", reason, reasonSource, unavailable });

  if (!ctx.flagEnabled) return disabled(NOT_ENABLED_REASON, "flag");
  if (!ctx.providersLoaded || !info) return disabled(PROVIDERS_UNAVAILABLE_REASON, "unavailable");
  if (!info.enabled) return disabled(info.reason?.trim() || `${base.label} is not enabled in this deployment.`, "provider");
  if (base.control === "none") return disabled("Yahoo connections need an OAuth sign-in, which this page does not offer yet.", "provider");
  if (!ctx.canConnect) return disabled(ctx.signedOutReason ?? SIGNED_OUT_REASON, "signed-out");
  return { ...base, state: "enabled", reason: null, reasonSource: null, unavailable };
}

export function buildProviderCards(providers: ProviderInfo[], ctx: CardContext): ProviderCard[] {
  return PROVIDERS.map((p) => providerCard(p, providers.find((i) => i.provider === p), ctx));
}

// ---- Connect form validation (advisory on the client, repeated by the server action) ------------

export type ConnectInput = { leagueId: string; season: string };
export type ConnectErrors = { leagueId?: string; season?: string };

export const MIN_SEASON = 1990;
export const MAX_SEASON = 2100;

/** The NFL season a new league most likely belongs to: January and February still belong to last season. */
export function defaultSeason(now: Date): number {
  return now.getUTCMonth() < 2 ? now.getUTCFullYear() - 1 : now.getUTCFullYear();
}

export function validateConnect(input: ConnectInput): { ok: true; leagueId: string; season: number } | { ok: false; errors: ConnectErrors } {
  const errors: ConnectErrors = {};
  const leagueId = input.leagueId.trim();
  if (!leagueId) errors.leagueId = "Enter the league id.";
  else if (!/^\d+$/.test(leagueId)) errors.leagueId = "A league id is digits only.";
  else if (leagueId.length > 64) errors.leagueId = "That league id is too long.";
  const seasonText = input.season.trim();
  const season = /^\d{4}$/.test(seasonText) ? Number(seasonText) : NaN;
  if (!seasonText) errors.season = "Enter the season year.";
  else if (!(season >= MIN_SEASON && season <= MAX_SEASON)) errors.season = `Enter a four-digit season year from ${MIN_SEASON} to ${MAX_SEASON}.`;
  return Object.keys(errors).length ? { ok: false, errors } : { ok: true, leagueId, season };
}

/** An internal league id as the backend issues them (lg_ + url-safe characters). */
export function isInternalLeagueId(value: unknown): value is string {
  return typeof value === "string" && /^lg_[A-Za-z0-9_-]{16,64}$/.test(value);
}

/** A provider team id as it appears in a path-free request body. */
export function validTeamId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9._:|-]{1,128}$/.test(value);
}

// ---- Loading problems ---------------------------------------------------------------------------

export type LoadProblem = { tone: "caution" | "negative"; title: string; body: string };

/** Turn a failed /v2 call into an honest message that includes the backend's own detail. */
export function loadProblem(what: string, failure: { error: string; kind?: string; status?: number }): LoadProblem {
  // The offline message names the backend address; keep that out of the browser and say what happened.
  if (failure.kind === "offline") return { tone: "negative", title: `Could not load ${what}`, body: "The backend did not answer." };
  if (failure.status === 404) {
    return { tone: "caution", title: `${what[0].toUpperCase()}${what.slice(1)} are not available`, body: `The backend does not serve the multi-league API in this deployment. Backend response: ${failure.error}` };
  }
  if (failure.status === 401) {
    return { tone: "negative", title: "The backend did not accept your sign-in", body: `Backend response: ${failure.error}` };
  }
  return { tone: "negative", title: `Could not load ${what}`, body: `Backend response: ${failure.error}` };
}
