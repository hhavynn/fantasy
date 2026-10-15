export function formatGain(gain: number | null | undefined): string {
  if (gain === null || gain === undefined) return "—";
  if (gain > 0) return `+${gain.toFixed(1)} pts`;
  return `${gain.toFixed(1)} pts`;
}

export function actionabilityBadgeTone(actionability: string): "info" | "warn" | "accent" | "mute" {
  switch (actionability) {
    case "ACTIONABLE":
      return "accent";
    case "LOCKED":
      return "mute";
    case "UNCERTAIN":
    case "IR_BLOCKED":
      return "warn";
    case "NO_CHANGE":
    default:
      return "info";
  }
}

export function lockStatusBadge(
  status: string,
  kickoffUtc: string | null,
  hasFinalScore?: boolean,
  homeScore?: number | null,
  awayScore?: number | null,
): { label: string; tone: "mute" | "info" | "warn" } {
  switch (status) {
    case "LOCKED":
      if (hasFinalScore && homeScore !== null && homeScore !== undefined && awayScore !== null && awayScore !== undefined) {
        return { label: `Final (${awayScore}-${homeScore})`, tone: "mute" };
      }
      return { label: "Locked", tone: "mute" };
    case "UNLOCKED":
      if (kickoffUtc) {
        try {
          const d = new Date(kickoffUtc);
          const timeStr = d.toLocaleTimeString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
          return { label: `Kickoff: ${timeStr}`, tone: "info" };
        } catch {
          return { label: "Upcoming", tone: "info" };
        }
      }
      return { label: "Upcoming", tone: "info" };
    case "BYE":
      return { label: "NFL Bye", tone: "mute" };
    case "TEAMLESS":
      return { label: "Free Agent", tone: "mute" };
    case "UNKNOWN":
    default:
      return { label: "Kickoff Unknown", tone: "warn" };
  }
}
