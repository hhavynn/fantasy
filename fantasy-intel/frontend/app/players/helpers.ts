import { label } from "@/lib/format";
import type { Provider } from "@/lib/labels";
import { rosterStatus } from "@/lib/roster-status";

/** ESPN uses ACTIVE/NORMAL for "no designation"; anything else is worth a badge. */
export function hasInjuryDesignation(status: string | null | undefined): boolean {
  return !!status && !["ACTIVE", "NORMAL"].includes(status);
}

/**
 * Who holds the player: the fantasy team's name, or how a free player can be acquired. With a provider, its own terms
 * apply: Sleeper says only whether a player is rostered, so an unrostered Sleeper player is "Not rostered" (he may be on
 * waivers), never "Free agent" (lib/roster-status.ts).
 */
export function ownerBadge(fantasyTeam: string | null | undefined, availability: string | null | undefined,
  provider?: Provider): { text: string; tone: "info" | "good" | "warn"; title?: string } {
  if (fantasyTeam) return { text: fantasyTeam, tone: "info" };
  if (provider === "sleeper" && availability === "FREEAGENT") {
    const s = rosterStatus(availability, null, provider);
    return { text: s.text, tone: "good", title: s.title };
  }
  if (availability === "WAIVERS") return { text: "On waivers", tone: "warn" };
  if (availability === "FREEAGENT") return { text: "Free agent", tone: "good" };
  return { text: label(availability) || "Unknown", tone: "warn" };
}
