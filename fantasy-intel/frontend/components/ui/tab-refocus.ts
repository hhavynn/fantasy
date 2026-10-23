// Focus hand-off for `TabList`: activating a tab navigates, and the page that arrives may mount a fresh tablist, so
// the newly selected tab takes focus back. The hand-off is armed with the exact tab href that was activated and
// expires quickly: if that navigation fails (an error page, no tablist) or another page arrives, nothing later
// steals focus. Plain module (no React) so node --test covers it.

export const REFOCUS_TTL_MS = 10_000;

type Pending = { idBase: string; href: string; at: number };

export function createRefocus(ttl = REFOCUS_TTL_MS) {
  let pending: Pending | null = null;
  return {
    /** A tab of `idBase` linking to `href` was activated at `now`. */
    arm(idBase: string, href: string, now: number) {
      pending = { idBase, href, at: now };
    },
    /**
     * True when a tablist `idBase` whose selected tab links to `selectedHref` should take focus now. Any pending
     * hand-off for that tablist is used up either way, and an expired one is dropped.
     */
    take(idBase: string, selectedHref: string | undefined, now: number): boolean {
      const p = pending;
      if (!p) return false;
      if (now - p.at > ttl) {
        pending = null;
        return false;
      }
      if (p.idBase !== idBase) return false;
      pending = null;
      return selectedHref !== undefined && p.href === selectedHref;
    },
  };
}
