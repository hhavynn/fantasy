/** Tiny inline line chart. `null` points are gaps (missing), never zero. */
export function Sparkline({ points, width = 90, height = 22, max }: {
  points: (number | null)[]; width?: number; height?: number; max?: number;
}) {
  const vals = points.filter((p): p is number => p !== null);
  if (vals.length < 2) return <span className="missing" title="Not enough games">—</span>;
  const hi = max ?? Math.max(...vals, 0.0001);
  const step = width / Math.max(1, points.length - 1);
  const segs: string[] = [];
  let cur = "";
  points.forEach((p, i) => {
    if (p === null) {
      if (cur) segs.push(cur);
      cur = "";
      return;
    }
    const x = (i * step).toFixed(1);
    const y = (height - 2 - (p / hi) * (height - 4)).toFixed(1);
    cur += `${cur ? "L" : "M"}${x},${y}`;
  });
  if (cur) segs.push(cur);
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {segs.map((d, i) => (
        <path key={i} d={d} fill="none" stroke="var(--chart-1)" strokeWidth={1.5} />
      ))}
    </svg>
  );
}
