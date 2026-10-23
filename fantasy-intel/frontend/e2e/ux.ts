import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";
import { THEME_KEY } from "../lib/theme";
import { A11Y_TRIAGE, CONTENT_TRIAGE } from "./ux-triage";

// Shared checks for the UX acceptance gates (e2e/ux-gates.spec.ts, e2e/ux-multileague.spec.ts, e2e/visual.spec.ts):
// the PRODUCT.md quality bar as reusable assertions. Selectors are roles, landmarks and the app bar's global classes
// (app/layout.tsx, app/globals.css), never CSS-module hashes, so a page can change its markup without touching these.

export const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

/** Titles of BackendProblem and the route error boundary: a page that shows one did not render its data. */
export const FAILURE_TEXT =
  /Can't reach the backend|The backend returned an error|database schema is out of date|Something went wrong|This page couldn't be rendered/;

export type Theme = "light" | "dark";

/** Serve player photos offline (ESPN's CDN) so a synthetic id never logs a 404. */
export async function stubPhotos(page: Page) {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
}

/** Chromium's console line for a document that answered 404: expected on the not-found page, and nothing else. */
export const DOCUMENT_404 = /^console: Failed to load resource: the server responded with a status of 404 \(Not Found\)$/;

/** Console errors and uncaught exceptions, collected for the whole test. */
export function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
  });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  return errors;
}

/** Load with this theme stored, the way a returning visitor's choice is applied (before React hydrates). */
export async function useTheme(page: Page, theme: Theme) {
  await page.addInitScript(([key, value]) => {
    try { window.localStorage.setItem(key, value); } catch { /* storage blocked: the default theme stays */ }
  }, [THEME_KEY, theme] as const);
}

/** Switch the theme in place (the attribute every token block keys on), without a reload. */
export async function applyTheme(page: Page, theme: Theme) {
  await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

/** Navigate and wait until the page has streamed in: network idle, fonts loaded, and a real h1. `status` is the HTTP
 *  status the page must answer with (404 for the not-found page). */
export async function open(page: Page, url: string, status = 200) {
  const res = await page.goto(url, { waitUntil: "load" });
  expect(res?.status(), `${url}: HTTP status`).toBe(status);
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  await expect(page.locator("main h1"), `${url}: page heading`).toHaveText(/\S/, { timeout: 20_000 });
}

/** The path and query with a league id replaced, the key A11Y_TRIAGE matches on. */
export function routeKey(page: Page): string {
  const u = new URL(page.url());
  return u.pathname.replace(/^\/l\/[^/]+/, "/l/:league") + u.search;
}

/** One h1, inside main; the banner, main and the section navigation are landmarks; main is not empty. `problem: true`
 *  is for a page whose expected answer is a backend refusal (a private league for a signed-out visitor): the error
 *  notice is then the content, and the caller asserts what it says. */
export async function expectStructure(page: Page, { problem = false }: { problem?: boolean } = {}) {
  await expect(page.locator("h1"), "exactly one h1").toHaveCount(1);
  await expect(page.locator("main h1"), "the h1 is in main").toHaveCount(1);
  await expect(page.getByRole("banner"), "one banner landmark (the app bar)").toHaveCount(1);
  await expect(page.getByRole("main"), "one main landmark").toHaveCount(1);
  // The app bar's section links on desktop and tablets, the tab bar on phones: one of them is on screen with every
  // group (at least the five of PRODUCT.md; more may be added).
  const sections = page.getByRole("navigation", { name: "Sections" }).filter({ visible: true }).first();
  await expect(sections, "the section navigation is visible").toBeVisible();
  expect(await sections.getByRole("link").count(), "section links in the visible navigation").toBeGreaterThanOrEqual(5);
  if (!problem) await expect(page.locator("main"), "no backend or render error").not.toContainText(FAILURE_TEXT);
}

/**
 * Honest data and plain copy: a missing value is a muted dash, never a leaked `undefined`, `NaN`, `null` or
 * `[object Object]`, and prose never shows an engine identifier or environment variable (SCREAMING_SNAKE_CASE).
 * Lines listed in CONTENT_TRIAGE (e2e/ux-triage.ts) for this route are tolerated.
 */
export async function expectNoBrokenValues(page: Page) {
  const key = routeKey(page);
  const text = await page.locator("main").innerText();
  const hits = text.split("\n")
    .filter((l) => /\b(undefined|NaN|null)\b|\[object Object\]|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/.test(l))
    .filter((l) => !CONTENT_TRIAGE.some((t) => t.route.test(key) && t.text.test(l)));
  expect(hits, `raw undefined/NaN/null or an engine identifier on ${key}; fix it or triage in e2e/ux-triage.ts`).toEqual([]);
}

/** No horizontal page scroll at this width (PRODUCT.md: 320 to 1600px, both themes). */
export async function expectNoSidewaysScroll(page: Page) {
  const o = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(o.scroll, `horizontal scroll at ${o.client}px`).toBeLessThanOrEqual(o.client);
}

/**
 * The app bar's items all fit the viewport and none covers another. Content pushed off the left edge never makes
 * the page scroll sideways, so expectNoSidewaysScroll cannot see it; this can.
 */
export async function expectAppBarFits(page: Page) {
  const problems = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const items = [...document.querySelectorAll<HTMLElement>(".topbar > *, .topbar-end > *")]
      .filter((e) => !e.classList.contains("topbar-end"))
      .map((e) => ({ name: e.getAttribute("aria-label") || e.className.toString().split(" ")[0] || e.tagName, r: e.getBoundingClientRect() }))
      .filter(({ r }) => r.width > 0 && r.height > 0);
    const out: string[] = [];
    for (const { name, r } of items) {
      if (r.left < -1 || r.right > vw + 1) out.push(`${name} spans ${Math.round(r.left)}..${Math.round(r.right)}px of ${vw}px`);
    }
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i].r, b = items[j].r;
        const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (w > 1 && h > 1) out.push(`${items[i].name} overlaps ${items[j].name} by ${Math.round(w)}px`);
      }
    }
    return out;
  });
  expect(problems, "app bar items clipped or overlapping").toEqual([]);
}

/**
 * Decision first, evidence second, method last (PRODUCT.md). The h1 comes before any table, chart or collapsible
 * method; on a decision page the Verdict (components/ui/Verdict.tsx; its CSS-module class keeps the word "verdict")
 * also comes before them, and on desktop it starts in the first screenful.
 */
export async function expectDecisionFirst(page: Page, { verdict, aboveFold = false }: { verdict: boolean; aboveFold?: boolean }) {
  const order = await page.evaluate(() => {
    const main = document.querySelector("main")!;
    const all = [...main.querySelectorAll("*")];
    const first = (sel: string) => { const e = main.querySelector(sel); return e ? all.indexOf(e) : Infinity; };
    const v = main.querySelector('[class*="verdict"]');
    return {
      h1: first("h1"),
      verdict: v ? all.indexOf(v) : -1,
      verdictTop: v ? v.getBoundingClientRect().top + window.scrollY : -1,
      // A ProvenanceBadge (data-provenance) is the headline's source-and-age disclosure (#168), not evidence or method.
      evidence: Math.min(first("table"), first('svg[role="img"]'), first("figure"),
        first("details:not(.filter-box):not([data-provenance])")),
      viewport: window.innerHeight,
    };
  });
  expect(order.h1, "the h1 precedes tables, charts and the method").toBeLessThan(order.evidence);
  if (!verdict) return;
  expect(order.verdict, "the decision page shows a Verdict").toBeGreaterThanOrEqual(0);
  expect(order.verdict, "the Verdict precedes tables, charts and the method").toBeLessThan(order.evidence);
  if (aboveFold) expect(order.verdictTop, "the Verdict starts in the first screenful").toBeLessThan(order.viewport);
}

const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

/**
 * axe-core (WCAG 2.2 A/AA plus best practice: landmarks, accessible names, ARIA, contrast, target size, focusable
 * scroll regions) on the page as it is now. Violations listed in e2e/ux-triage.ts for this route are tolerated;
 * anything else fails with the rule, the element and axe's summary.
 */
export async function expectAccessible(page: Page, label = "") {
  const key = routeKey(page);
  const { violations } = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
  const blocking: string[] = [];
  for (const v of violations) {
    for (const node of v.nodes) {
      const target = node.target.join(" ");
      const triaged = A11Y_TRIAGE.some((t) => t.rule === v.id && t.route.test(key) && (!t.target || t.target.test(target)));
      if (!triaged) blocking.push(`${v.id} [${v.impact}] ${target}: ${(node.failureSummary ?? v.help).replace(/\s+/g, " ").slice(0, 240)}`);
    }
  }
  expect(blocking, `axe violations on ${key}${label ? ` (${label})` : ""}; fix them or triage in e2e/ux-triage.ts`).toEqual([]);
}

export type FocusStop = { where: "banner" | "nav" | "main" | "other"; name: string; visible: boolean; indicator: boolean };

/** Press Tab up to `max` times from the top of the page and describe each element that receives focus. */
export async function tabThrough(page: Page, max: number, stopInMain = 3): Promise<FocusStop[]> {
  await page.evaluate(() => { (document.activeElement as HTMLElement | null)?.blur(); window.scrollTo(0, 0); });
  const stops: FocusStop[] = [];
  let inMain = 0;
  for (let i = 0; i < max && inMain < stopInMain; i++) {
    await page.keyboard.press("Tab");
    const stop = await page.evaluate((): FocusStop | null => {
      const el = document.activeElement as HTMLElement | null;
      if (!el || el === document.body) return null;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      const ring = (s: CSSStyleDeclaration) =>
        (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0) || (s.boxShadow !== "none" && s.boxShadow !== "");
      // A composite control (TeamPerspective's label + select) draws the ring on its frame with :focus-within.
      let frame = el.parentElement;
      let framed = false;
      for (let i = 0; i < 2 && frame && !framed; i++, frame = frame.parentElement) framed = ring(getComputedStyle(frame));
      const where = el.closest("main") ? "main" : el.closest("header") ? "banner" : el.closest("nav") ? "nav" : "other";
      const name = (el.getAttribute("aria-label") || el.innerText || el.getAttribute("title") || el.tagName).trim().slice(0, 40);
      return { where, name, visible: r.width > 0 && r.height > 0 && cs.visibility !== "hidden", indicator: ring(cs) || framed };
    });
    if (!stop) break;
    stops.push(stop);
    if (stop.where === "main") inMain++;
  }
  return stops;
}
