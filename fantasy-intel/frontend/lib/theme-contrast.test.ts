// Guards the design tokens in app/globals.css: both themes meet WCAG AA for every text/surface pair
// the UI uses, the explicit light theme and the prefers-color-scheme light block stay identical,
// and the chart palette stays distinguishable. Run with CONTRAST_REPORT=1 to print every ratio.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { type Rgba, type Vision, contrast, deltaE, over, parseColor, simulate, toLab } from "./contrast.ts";

const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");

function tokens(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out[m[1]] = m[2].trim();
  return out;
}

function grab(re: RegExp, what: string): Record<string, string> {
  const m = css.match(re);
  assert.ok(m, `globals.css has no ${what} block`);
  return tokens(m[1]);
}

const darkRaw = grab(/^:root\s*\{([^}]*)\}/m, "dark :root");
const lightRaw = grab(/^:root\[data-theme="light"\]\s*\{([^}]*)\}/m, ':root[data-theme="light"]');
const lightMedia = grab(
  /@media \(prefers-color-scheme: light\)\s*\{\s*:root:not\(\[data-theme="dark"\]\)\s*\{([^}]*)\}/,
  "prefers-color-scheme: light",
);

/** Resolve `var(--x)` references against the theme's own tokens (light tokens fall back to the dark set). */
function resolve(raw: Record<string, string>, base: Record<string, string>): Record<string, string> {
  const merged = { ...base, ...raw };
  const get = (name: string, depth = 0): string => {
    const v = merged[name];
    assert.ok(v !== undefined, `token ${name} is not defined`);
    const ref = v.match(/^var\((--[\w-]+)\)$/);
    assert.ok(depth < 8, `token ${name} loops`);
    return ref ? get(ref[1], depth + 1) : v;
  };
  return Object.fromEntries(Object.keys(merged).map((k) => [k, get(k)]));
}

const THEMES: Record<string, Record<string, string>> = {
  dark: resolve(darkRaw, darkRaw),
  light: resolve(lightRaw, darkRaw),
};

const SURFACES = ["--bg", "--surface", "--surface-raised", "--surface-hover"] as const;
const report = process.env.CONTRAST_REPORT === "1";

type Check = { label: string; fg: string; bgs: [string, Rgba][]; min: number };

function checks(t: Record<string, string>): Check[] {
  const c = (k: string) => parseColor(t[k]);
  const surf = Object.fromEntries(SURFACES.map((k) => [k, c(k)])) as Record<(typeof SURFACES)[number], Rgba>;
  const plain: [string, Rgba][] = SURFACES.map((k) => [k.slice(2), surf[k]]);
  const tinted = (soft: string, on: readonly (typeof SURFACES)[number][] = ["--surface", "--surface-raised", "--surface-hover"]): [string, Rgba][] =>
    on.map((k) => [`${soft.slice(2, -5)}/${k.slice(2)}`, over(c(soft), surf[k])]);
  const out: Check[] = [];
  for (const k of ["--ink", "--ink-muted", "--ink-faint", "--accent", "--positive", "--negative", "--caution"]) {
    out.push({ label: `${k} text`, fg: k, bgs: plain, min: 4.5 });
  }
  out.push(
    { label: "ink on accent-soft", fg: "--ink", bgs: tinted("--accent-soft"), min: 4.5 },
    { label: "ink-muted on accent-soft", fg: "--ink-muted", bgs: tinted("--accent-soft"), min: 4.5 },
    { label: "ink-faint on accent-soft (own-team row)", fg: "--ink-faint", bgs: tinted("--accent-soft", ["--surface"]), min: 4.5 },
    { label: "accent on accent-soft (info badge)", fg: "--accent", bgs: tinted("--accent-soft"), min: 4.5 },
    { label: "positive on positive-soft (good badge)", fg: "--positive", bgs: tinted("--positive-soft"), min: 4.5 },
    { label: "negative on negative-soft (bad badge)", fg: "--negative", bgs: tinted("--negative-soft"), min: 4.5 },
    { label: "caution on caution-soft (warn badge)", fg: "--caution", bgs: tinted("--caution-soft"), min: 4.5 },
    { label: "ink-muted on neutral-soft (badge)", fg: "--ink-muted", bgs: tinted("--neutral-soft"), min: 4.5 },
    { label: "ink on caution-soft (callout)", fg: "--ink", bgs: tinted("--caution-soft"), min: 4.5 },
    { label: "ink on negative-soft (callout)", fg: "--ink", bgs: tinted("--negative-soft"), min: 4.5 },
    { label: "ink on positive-soft (notice)", fg: "--ink", bgs: tinted("--positive-soft"), min: 4.5 },
    { label: "on-accent on accent (button)", fg: "--on-accent", bgs: [["accent", c("--accent")]], min: 4.5 },
    { label: "bg on caution (callout icon)", fg: "--bg", bgs: [["caution", c("--caution")]], min: 4.5 },
    { label: "bg on negative (callout icon)", fg: "--bg", bgs: [["negative", c("--negative")]], min: 4.5 },
    { label: "line-control (control borders)", fg: "--line-control", bgs: plain.slice(0, 3), min: 3 },
    { label: "focus ring", fg: "--focus", bgs: plain, min: 3 },
    { label: "accent as UI colour", fg: "--accent", bgs: plain, min: 3 },
    { label: "positive as UI colour", fg: "--positive", bgs: plain, min: 3 },
    { label: "negative as UI colour", fg: "--negative", bgs: plain, min: 3 },
    { label: "caution as UI colour", fg: "--caution", bgs: plain, min: 3 },
  );
  for (let i = 1; i <= 6; i++) {
    out.push({ label: `chart-${i} on surface`, fg: `--chart-${i}`, bgs: [["surface", surf["--surface"]], ["raised", surf["--surface-raised"]]], min: 3 });
  }
  out.push({ label: "chart-axis text", fg: "--chart-axis", bgs: plain, min: 4.5 });
  return out;
}

for (const [name, t] of Object.entries(THEMES)) {
  test(`${name} theme: every text and UI colour pair meets WCAG AA`, () => {
    const failures: string[] = [];
    for (const { label, fg, bgs, min } of checks(t)) {
      const parts = bgs.map(([n, bg]) => {
        const r = contrast(over(parseColor(t[fg]), bg), bg);
        if (report) console.log(`${name.padEnd(5)} ${label.padEnd(42)} ${n.padEnd(24)} ${r.toFixed(2)} (>= ${min})`);
        if (r < min) failures.push(`${label} on ${n}: ${r.toFixed(2)} < ${min}`);
        return r;
      });
      assert.ok(parts.length > 0);
    }
    assert.deepEqual(failures, []);
  });

  test(`${name} theme: chart colours stay distinguishable under colour-vision simulation`, () => {
    const cols = [1, 2, 3, 4, 5, 6].map((i) => parseColor(t[`--chart-${i}`]));
    const visions: Vision[] = ["normal", "protan", "deutan", "tritan"];
    const failures: string[] = [];
    for (const v of visions) {
      let min = Infinity;
      for (let i = 0; i < cols.length; i++) {
        for (let j = i + 1; j < cols.length; j++) {
          const d = deltaE(simulate(cols[i], v), simulate(cols[j], v));
          min = Math.min(min, d);
          if (d < 10) failures.push(`${v}: chart-${i + 1} vs chart-${j + 1} dE ${d.toFixed(1)}`);
        }
      }
      if (report) console.log(`${name.padEnd(5)} chart min dE (${v}) ${min.toFixed(1)}`);
    }
    if (report) console.log(`${name.padEnd(5)} chart lightness`, cols.map((c) => Math.round(toLab(c)[0])).join(" "));
    assert.deepEqual(failures, []);
  });
}

test("light theme: the explicit and prefers-color-scheme blocks define the same tokens", () => {
  assert.deepEqual(lightMedia, lightRaw);
});

test("light theme redefines every colour token the dark theme sets (nothing falls through)", () => {
  const colourTokens = Object.keys(darkRaw).filter((k) => /^(#[0-9a-f]+|rgba?\(|0 \d)/i.test(darkRaw[k]));
  const missing = colourTokens.filter((k) => !(k in lightRaw) && !/^var\(/.test(darkRaw[k]) && !k.startsWith("--appbar"));
  assert.deepEqual(missing, []);
});
