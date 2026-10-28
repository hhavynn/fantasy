// The name-free clipboard exports (#197: weekly recap, matchup, trade evaluation, Team Value): writing the summary and
// deciding which summary a status message describes. No `@/` value imports, so it runs under the unit-test runner.

export type CopyOutcome = "copied" | "unavailable";
export type CopyResult = { text: string; outcome: CopyOutcome };
type ClipboardLike = { writeText?: (text: string) => Promise<void> } | null | undefined;

/** Write `text`; a missing clipboard API (an insecure context, an older browser) or a refusal is "unavailable", never a throw. */
export async function writeClipboard(text: string, clipboard: ClipboardLike): Promise<CopyOutcome> {
  try {
    if (!clipboard?.writeText) return "unavailable";
    await clipboard.writeText(text);
    return "copied";
  } catch {
    return "unavailable";
  }
}

/**
 * The outcome to show beside the summary now on screen. A result belongs to the exact text that was copied: once the
 * page moves to another league, team, week, phase or trade (a new `text`, same mounted component), an earlier
 * "copied" no longer describes what the reader sees, and a copy that resolves after the switch is not reported.
 */
export function outcomeFor(result: CopyResult | null, text: string): CopyOutcome | null {
  return result !== null && result.text === text ? result.outcome : null;
}
