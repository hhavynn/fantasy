// Theme preference: System (follow the OS), Light or Dark. The choice lives in localStorage and is
// applied as `data-theme` on <html>; System leaves the attribute off so the prefers-color-scheme
// block in globals.css decides. No React and no `@/` imports, so it runs under the unit-test runner.

export const THEME_KEY = "fantasy-intel:theme";

export type ThemeChoice = "system" | "light" | "dark";

export const THEME_CHOICES: readonly { value: ThemeChoice; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

/** A stored or attribute value as a choice; anything unknown is System. */
export function parseTheme(value: string | null | undefined): ThemeChoice {
  return value === "light" || value === "dark" ? value : "system";
}

/** The choice that follows `current` when moving `delta` steps through the list, wrapping at both ends. */
export function stepTheme(current: ThemeChoice, delta: number): ThemeChoice {
  const i = THEME_CHOICES.findIndex((c) => c.value === current);
  return THEME_CHOICES[(i + delta + THEME_CHOICES.length) % THEME_CHOICES.length].value;
}

/**
 * Runs synchronously in <head>, before the first paint, so a stored Light or Dark choice is on
 * <html> before any content renders (no flash of the wrong theme). Storage can throw (blocked site
 * data), in which case the system preference applies.
 */
export const THEME_INIT_SCRIPT =
  `(function(){try{var t=localStorage.getItem(${JSON.stringify(THEME_KEY)});` +
  `if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}catch(e){}})()`;
