// Who holds a player in this league, in words, for search results (issue #200): the command palette and the Players
// search table. Pure functions so node --test covers them.
//
// The backend's `availability_status` is the provider's own: ESPN says ONTEAM, WAIVERS or FREEAGENT. Sleeper says only
// whether a player is rostered (app/sleeper/write.py writes ONTEAM or FREEAGENT), so an unrostered Sleeper player may
// still be on waivers: he is "Not rostered", never "Free agent". Anything else (no status, an unknown value) is
// "Roster status unknown", never assumed free.
//
// `provider` null means the provider is not confirmed for the league the data came from (the command palette right after
// a client-side league switch, before the layout re-renders): then only wording true for every provider is used, so an
// unrostered player is "Not rostered" (never "Free agent").

import { normalizeProvider, type Provider } from "./labels.ts";

export type RosterKind = "rostered" | "waivers" | "free" | "unrostered" | "unknown";
export type RosterStatus = { kind: RosterKind; text: string; title: string };

export function rosterStatus(availability: string | null | undefined, owner: string | null | undefined,
  provider: Provider | string | null | undefined = "espn"): RosterStatus {
  const pv = provider === null ? null : normalizeProvider(provider);
  const name = pv === "sleeper" ? "Sleeper" : pv === "espn" ? "ESPN" : "The provider";
  if (availability === "ONTEAM") {
    return owner
      ? { kind: "rostered", text: owner, title: `Rostered by ${owner}` }
      : { kind: "rostered", text: "Rostered", title: "Rostered in this league; the team's name is not in the synced data" };
  }
  if (availability === "WAIVERS") return { kind: "waivers", text: "On waivers", title: `${name} lists him on waivers: a claim is needed` };
  if (availability === "FREEAGENT") {
    if (pv === "espn") return { kind: "free", text: "Free agent", title: "ESPN lists him as a free agent: no claim needed" };
    return pv === "sleeper"
      ? { kind: "unrostered", text: "Not rostered", title: "Not on a roster in this league. Sleeper does not say whether he is on waivers" }
      : { kind: "unrostered", text: "Not rostered", title: "Not on a roster in this league" };
  }
  // No status from the provider: a team name alone still means rostered (older payloads), otherwise unknown.
  if (owner) return { kind: "rostered", text: owner, title: `Rostered by ${owner}` };
  return { kind: "unknown", text: "Roster status unknown", title: `${name} supplied no roster status for this player` };
}

/** True when the player can be added without a trade (free, on waivers, or unrostered with the waiver state unknown). */
export function addable(kind: RosterKind): boolean {
  return kind === "free" || kind === "waivers" || kind === "unrostered";
}

const INJURY_TEXT: Record<string, string> = {
  INJURY_RESERVE: "IR", OUT: "Out", DOUBTFUL: "Doubtful", QUESTIONABLE: "Questionable", PROBABLE: "Probable",
  SUSPENSION: "Suspended", DAY_TO_DAY: "Day to day",
};

/** A designation worth showing ("Out", "IR"), or null for none (ESPN's ACTIVE and NORMAL mean no designation). */
export function injuryText(status: string | null | undefined): string | null {
  const s = (status ?? "").trim().toUpperCase();
  if (!s || s === "ACTIVE" || s === "NORMAL") return null;
  if (INJURY_TEXT[s]) return INJURY_TEXT[s];
  const words = s.replaceAll("_", " ").toLowerCase();
  return `${words[0].toUpperCase()}${words.slice(1)}`;
}
