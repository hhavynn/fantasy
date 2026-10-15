export function TrendArrow({ label }: { label?: string | null }) {
  if (label === "UP") return <span className="up" title="Usage trending up">▲</span>;
  if (label === "DOWN") return <span className="down" title="Usage trending down">▼</span>;
  if (label === "FLAT") return <span className="faint" title="Usage flat">▬</span>;
  return <span className="missing" title="Not enough games for a trend">·</span>;
}
