import { label } from "@/lib/format";

/** ESPN uses ACTIVE/NORMAL for "no designation"; anything else is worth a badge. */
export function hasInjuryDesignation(status: string | null | undefined): boolean {
  return !!status && !["ACTIVE", "NORMAL"].includes(status);
}

/** Who holds the player: the fantasy team's name, or how a free player can be acquired. */
export function ownerBadge(fantasyTeam: string | null | undefined, availability: string | null | undefined): { text: string; tone: "info" | "good" | "warn" } {
  if (fantasyTeam) return { text: fantasyTeam, tone: "info" };
  if (availability === "WAIVERS") return { text: "On waivers", tone: "warn" };
  if (availability === "FREEAGENT") return { text: "Free agent", tone: "good" };
  return { text: label(availability) || "Unknown", tone: "warn" };
}
