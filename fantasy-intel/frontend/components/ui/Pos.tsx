/** Position tag. The `pos-*` class adds the position's accent edge (see globals.css); the text carries the meaning. */
export function Pos({ value }: { value?: string | null }) {
  const key = value ? value.replace(/[^A-Z]/gi, "").toUpperCase() : "";
  return <span className={`badge pos${key ? ` pos-${key}` : ""}`}>{value ?? "?"}</span>;
}
