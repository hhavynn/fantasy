/** Provider-scoped injury opportunity evidence is independent of exact future-week stash gains. */
export type StashContingencies<T> = {
  available: boolean;
  items: T[];
  reason?: string | null;
  notices?: { code: string; text: string }[];
  note?: string;
};

export function contingencyStatus<T>(evidence: StashContingencies<T> | null | undefined): string | null {
  if (!evidence) return "Conditional opportunity evidence was not returned for this league snapshot.";
  if (!evidence.available) {
    return evidence.reason
      ? `Conditional opportunities cannot be evaluated: ${evidence.reason}`
      : "Conditional opportunities cannot be evaluated from available injury and role evidence.";
  }
  if (!evidence.items.length) return "No evidence-backed conditional opportunities were identified in this snapshot.";
  return null;
}
