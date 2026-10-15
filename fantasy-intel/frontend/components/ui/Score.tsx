/** 0–max value as the number plus ten ticks (countable at a glance, comparable across rows). */
export function Score({ value, max = 10 }: { value?: number | null; max?: number }) {
  if (value === null || value === undefined) return <span className="missing" title="Not computed">—</span>;
  const on = Math.round(Math.max(0, Math.min(1, value / max)) * 10);
  return (
    <span className="score">
      <span className="v">{value.toFixed(1)}</span>
      <span className="ticks" role="img" aria-label={`${value.toFixed(1)} of ${max}`}>
        {Array.from({ length: 10 }, (_, i) => <i key={i} className={i < on ? "on" : undefined} />)}
      </span>
    </span>
  );
}
