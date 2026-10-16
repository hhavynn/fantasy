// Colour maths for checking the design tokens: WCAG contrast, alpha compositing, and CIELAB
// distance under colour-vision-deficiency simulation. No React and no `@/` imports, so it runs
// under the unit-test runner (see theme-contrast.test.ts).

export type Rgba = readonly [number, number, number, number];

/** Parse `#rgb`, `#rrggbb`, `#rrggbbaa` or `rgba(r, g, b, a)`. */
export function parseColor(input: string): Rgba {
  const v = input.trim().toLowerCase();
  const rgba = v.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/);
  if (rgba) return [Number(rgba[1]), Number(rgba[2]), Number(rgba[3]), rgba[4] === undefined ? 1 : Number(rgba[4])];
  const m = v.match(/^#([0-9a-f]{3,8})$/);
  if (!m) throw new Error(`Unsupported colour: ${input}`);
  let h = m[1];
  if (h.length === 3 || h.length === 4) h = [...h].map((c) => c + c).join("");
  if (h.length !== 6 && h.length !== 8) throw new Error(`Unsupported colour: ${input}`);
  const n = (i: number) => parseInt(h.slice(i, i + 2), 16);
  return [n(0), n(2), n(4), h.length === 8 ? n(6) / 255 : 1];
}

/** `fg` painted over an opaque `bg`. */
export function over(fg: Rgba, bg: Rgba): Rgba {
  const [r, g, b, a] = fg;
  return [
    Math.round(a * r + (1 - a) * bg[0]),
    Math.round(a * g + (1 - a) * bg[1]),
    Math.round(a * b + (1 - a) * bg[2]),
    1,
  ];
}

function lin(c: number): number {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

export function luminance(c: Rgba): number {
  return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
}

/** WCAG 2 contrast ratio between two opaque colours. */
export function contrast(a: Rgba, b: Rgba): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

export type Lab = readonly [number, number, number];

export function toLab(c: Rgba): Lab {
  const [r, g, b] = [lin(c[0]), lin(c[1]), lin(c[2])];
  const x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b;
  const y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b;
  const z = 0.0193339 * r + 0.119192 * g + 0.9503041 * b;
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const [fx, fy, fz] = [f(x / 0.95047), f(y), f(z / 1.08883)];
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

export function deltaE(a: Rgba, b: Rgba): number {
  const [la, lb] = [toLab(a), toLab(b)];
  return Math.hypot(la[0] - lb[0], la[1] - lb[1], la[2] - lb[2]);
}

export type Vision = "normal" | "protan" | "deutan" | "tritan";

// Machado et al. 2009, full severity, applied in linear RGB.
const MACHADO: Record<Exclude<Vision, "normal">, number[][]> = {
  protan: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  deutan: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.01182, 0.04294, 0.968881]],
  tritan: [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.3039]],
};

/** How `c` looks to a viewer with the given colour vision. */
export function simulate(c: Rgba, vision: Vision): Rgba {
  if (vision === "normal") return c;
  const l = [lin(c[0]), lin(c[1]), lin(c[2])];
  const m = MACHADO[vision];
  const enc = (v: number) => {
    const x = Math.max(0, Math.min(1, v));
    return Math.round(255 * (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055));
  };
  return [0, 1, 2].map((i) => enc(m[i][0] * l[0] + m[i][1] * l[1] + m[i][2] * l[2])).concat(1) as unknown as Rgba;
}
