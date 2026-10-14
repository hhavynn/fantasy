// Formatting helpers. `null`/`undefined` always renders as a muted "—" with a
// tooltip so missing data can never be mistaken for a measured zero.
import type { ReactNode } from "react";

export function Missing({ why = "No data available" }: { why?: string }) {
  return (
    <span className="missing" title={why}>
      —
    </span>
  );
}

type Num = number | null | undefined;

export function num(v: Num, digits = 1, why?: string): ReactNode {
  if (v === null || v === undefined || Number.isNaN(v)) return <Missing why={why} />;
  return v.toFixed(digits);
}

export function pct(v: Num, digits = 0, why?: string): ReactNode {
  if (v === null || v === undefined || Number.isNaN(v)) return <Missing why={why} />;
  return `${(v * 100).toFixed(digits)}%`;
}

export function int(v: Num, why?: string): ReactNode {
  if (v === null || v === undefined) return <Missing why={why} />;
  return String(Math.round(v));
}

export function signed(v: Num, digits = 1): ReactNode {
  if (v === null || v === undefined) return <Missing />;
  const s = `${v > 0 ? "+" : ""}${v.toFixed(digits)}`;
  return <span className={v > 0 ? "up" : v < 0 ? "down" : ""}>{s}</span>;
}

export function when(v: string | null | undefined): ReactNode {
  if (!v) return <Missing why="Never synced" />;
  const d = new Date(v.endsWith("Z") || v.includes("+") ? v : `${v}Z`);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function date(v: string | null | undefined): ReactNode {
  if (!v) return <Missing why="No timestamp from ESPN" />;
  const d = new Date(v.endsWith("Z") || v.includes("+") ? v : `${v}Z`);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function label(s: string | null | undefined): string {
  return (s ?? "").replaceAll("_", " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}
