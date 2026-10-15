export function Pos({ value }: { value?: string | null }) {
  return <span className="badge pos">{value ?? "?"}</span>;
}
